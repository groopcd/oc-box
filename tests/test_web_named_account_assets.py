"""Unserved login projection, exact paths, and immutable checked local inputs."""

import hashlib
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

from oc_box import (
    private_memory,
    web_local_origins,
    web_named_account_assets as assets,
    web_named_bootstrap,
    web_named_headers,
)

WASM = b"\0asm\x01\0\0\0synthetic unit-test bytes (not executed)"
VERIFIER = b"\x7fELF synthetic unit-test bytes (not executed)"
ACCOUNT = "A" * 43
WORKSPACE = "B" * 42 + "E"
COOKIE = (
    "__Host-ocbox-account=" + ACCOUNT + "; Secure; HttpOnly; SameSite=Strict; Path=/"
)


def pin(data):
    return assets.ReviewedArtifactPin(len(data), hashlib.sha256(data).digest())


class Document(HTMLParser):
    def __init__(self, data):
        super().__init__()
        self.tags = []
        self.feed(data.decode())

    def handle_starttag(self, name, attrs):
        self.tags.append((name, dict(attrs)))


class NamedAccountAssetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.origins = web_local_origins.LocalOrigins(10443, 11443)
        self.wasm = self.root / "browser.wasm"
        self.worker = self.root / "verifier"
        self.wasm.write_bytes(WASM)
        self.worker.write_bytes(VERIFIER)
        self.wasm.chmod(0o600)
        self.worker.chmod(0o600)

    def prepare(self, **changes):
        return assets.prepare_account_assets(
            **(
                dict(
                    origins=self.origins,
                    wasm_file=self.wasm,
                    wasm_pin=pin(WASM),
                    verifier_file=self.worker,
                    verifier_pin=pin(VERIFIER),
                )
                | changes
            )
        )

    def asset(
        self,
        bundle,
        path="/account-assets/client.wasm",
        *,
        mode="same-origin",
        dest="empty",
        callback=lambda nonce: WORKSPACE if nonce == ACCOUNT else None,
        fields=(),
        **changes,
    ):
        return bundle.asset(
            **(
                dict(
                    method="GET",
                    target=path,
                    actual_tls=True,
                    body=b"",
                    translate_nonce=callback,
                    headers=[
                        ("Host", self.origins.account.value.removeprefix("https://")),
                        ("Cookie", "__Host-ocbox-account=" + ACCOUNT),
                        ("Sec-Fetch-Site", "same-origin"),
                        ("Sec-Fetch-Mode", mode),
                        ("Sec-Fetch-Dest", dest),
                        *fields,
                    ],
                )
                | changes
            )
        )

    def test_exact_pins_snapshot_files_and_seal_the_verifier(self):
        with self.prepare() as prepared:
            fd = prepared.verifier_fd
            self.assertTrue(private_memory.is_sealed(fd))
            self.assertFalse(os.get_inheritable(fd))
            self.assertEqual(prepared.verifier_sha256, pin(VERIFIER).sha256)
            self.assertEqual(os.pread(fd, len(VERIFIER), 0), VERIFIER)
            with self.assertRaises(PermissionError):
                os.pwrite(fd, b"A", 0)
            # Later replacement or edits are not part of this lifetime.
            self.wasm.write_bytes(b"not the approved wasm")
            self.worker.unlink()
            self.worker.write_bytes(b"not the approved verifier")
            self.assertEqual(self.asset(prepared.bundle).body, WASM)
            self.assertEqual(os.pread(fd, len(VERIFIER), 0), VERIFIER)
        with self.assertRaises(OSError):
            os.fstat(fd)

    def test_missing_bad_or_wrong_type_pins_never_prepare_an_owner(self):
        for overrides in (
            {"wasm_pin": None},
            {"verifier_pin": None},
            {"wasm_pin": pin(WASM + b"x")},
            {"verifier_pin": pin(VERIFIER + b"x")},
            {"wasm_pin": assets.ReviewedArtifactPin(len(WASM), b"x" * 32)},
            {"verifier_pin": assets.ReviewedArtifactPin(len(VERIFIER), b"x" * 32)},
            {"wasm_file": Path("relative")},
            {"verifier_file": self.root / "missing"},
            {"origins": "https://accounts.ocbox.test:9443"},
        ):
            with (
                self.subTest(keys=tuple(overrides)),
                self.assertRaises(assets.AccountAssetError),
            ):
                with self.prepare(**overrides):
                    self.fail("An invalid pin or host configuration was accepted.")
        for filename, role in ((self.wasm, "wasm"), (self.worker, "verifier")):
            original = filename.read_bytes()
            other = b"invalid file magic"
            filename.write_bytes(other)
            with (
                self.assertRaises(assets.AccountAssetError),
                self.prepare(**{f"{role}_pin": pin(other)}),
            ):
                self.fail("Wrong artifact type accepted despite the hash match.")
            filename.write_bytes(original)

    def test_no_follow_inputs_and_no_special_or_mutably_shared_leaves(self):
        link = self.root / "symlink"
        link.symlink_to(self.wasm)
        alias = self.root / "aliased-directory"
        alias.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        for path in (link, alias / self.wasm.name, fifo, self.root):
            with (
                self.subTest(kind=path.name),
                self.assertRaises(assets.AccountAssetError),
            ):
                with self.prepare(wasm_file=path):
                    self.fail("Followed a link or tried to read a nonregular file.")
        os.link(self.worker, self.root / "hardlink")
        with self.assertRaises(assets.AccountAssetError), self.prepare():
            self.fail("Accepted a hardlinked executable.")
        (self.root / "hardlink").unlink()
        self.wasm.chmod(0o622)
        with self.assertRaises(assets.AccountAssetError), self.prepare():
            self.fail("Accepted a writable-by-others artifact.")

    def test_even_observed_same_bytes_file_mutation_is_refused(self):
        previous, stamp = os.pread, self.wasm.stat()
        exercised = False

        def pread(fd, size, offset):
            nonlocal exercised
            data = previous(fd, size, offset)
            info = os.fstat(fd)
            if not exercised and (info.st_dev, info.st_ino) == (
                stamp.st_dev,
                stamp.st_ino,
            ):
                exercised = True
                os.utime(
                    self.wasm, ns=(stamp.st_atime_ns, stamp.st_mtime_ns - 1_000_000_000)
                )
            return data

        with patch.object(os, "pread", pread):
            with self.assertRaises(assets.AccountAssetError), self.prepare():
                self.fail("Accepted a file that changed during the read.")
        self.assertTrue(exercised)

    def test_route_document_has_only_fixed_assets_and_escaped_display_text(self):
        with self.prepare(
            workspace_label="<script>alert(1)</script> __WASM_SHA256__"
        ) as prepared:
            reply = prepared.bundle.document(web_named_bootstrap.AccountEntry(COOKIE))
            headers = dict(reply.headers)
            self.assertEqual(headers["Set-Cookie"], COOKIE)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn(
                b"&lt;script&gt;alert(1)&lt;/script&gt; __WASM_SHA256__", reply.body
            )
            self.assertNotIn(ACCOUNT.encode(), reply.body)
            self.assertNotIn(b"demo", reply.body.lower())
            self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
            self.assertTrue(
                headers["Content-Security-Policy"].endswith(
                    f"form-action {self.origins.workspace.value}/oc-box/handoff"
                )
            )
            tags = Document(reply.body).tags
            self.assertEqual(
                [
                    (kind, field.get("src", field.get("href")))
                    for kind, field in tags
                    if kind in ("script", "link")
                ],
                [
                    ("link", "/account-assets/sign-in.css"),
                    ("script", "/account-assets/sign-in.mjs"),
                ],
            )
            credential = [(kind, attrs) for kind, attrs in tags if kind == "input"]
            self.assertEqual(
                [field["id"] for _, field in credential],
                ["login-user", "login-password"],
            )
            self.assertTrue(
                all(
                    "name" not in field and "form" not in field and "disabled" in field
                    for _, field in credential
                )
            )
            self.assertFalse(
                any("action" in attrs for kind, attrs in tags if kind == "form")
            )
            self.assertTrue(
                all(
                    attrs["type"] == "button"
                    for kind, attrs in tags
                    if kind == "button"
                )
            )
            for entry in (
                "plain text",
                web_named_bootstrap.AccountEntry(COOKIE + "\r\nX-Test: injected"),
            ):
                with self.assertRaises(web_named_headers.NamedRequestError):
                    prepared.bundle.document(entry)

    def test_only_exact_subresources_with_matching_live_account_cookie_are_projected(
        self,
    ):
        with self.prepare() as prepared:
            bundle = prepared.bundle
            for path in assets.ACCOUNT_ASSET_PATHS:
                if path.endswith(".css"):
                    mode, dest, kind = "no-cors", "style", "text/css; charset=utf-8"
                elif path.endswith(".mjs"):
                    mode, dest, kind = (
                        "cors",
                        "script",
                        "text/javascript; charset=utf-8",
                    )
                else:
                    mode, dest, kind = "same-origin", "empty", "application/wasm"
                result = self.asset(bundle, path, mode=mode, dest=dest)
                self.assertEqual(dict(result.headers)["Content-Type"], kind)
                self.assertNotIn("Set-Cookie", dict(result.headers))
            callback = Mock(return_value=WORKSPACE)
            for overrides in (
                {"method": "HEAD"},
                {"path": "/account-assets/client.wasm?x=1"},
                {"path": "/account-assets/%63lient.wasm"},
                {"path": "/account-assets/../transport.mjs"},
                {"path": "/account-assets/Verifier"},
                {"actual_tls": False},
                {"body": b"raw"},
                {"dest": "document"},
                {"mode": "navigate"},
                {"fields": (("Origin", self.origins.workspace.value),)},
                {"fields": (("Range", "bytes=0-4"),)},
                {"fields": (("Content-Length", "00"),)},
                {
                    "headers": [
                        ("Host", self.origins.account.value.removeprefix("https://"))
                    ]
                },
            ):
                with (
                    self.subTest(keys=tuple(overrides)),
                    self.assertRaises(web_named_headers.NamedRequestError),
                ):
                    self.asset(bundle, callback=callback, **overrides)
            callback.assert_not_called()
            for unavailable in (None, ACCOUNT, "wrong-shape"):
                with self.assertRaises(web_named_headers.NamedRequestError):
                    self.asset(bundle, callback=lambda _: unavailable)

    def test_versioned_source_assets_are_not_arbitrary_trusted_files(self):
        root = self.root / "source"
        root.mkdir()
        for filename, *_ in [assets._DOCUMENT] + [item[4:] for item in assets._SOURCES]:
            (root / filename).write_bytes((assets._SOURCE_ROOT / filename).read_bytes())
        with patch.object(assets, "_SOURCE_ROOT", root):
            with self.prepare():
                pass
            replacement = root / "web_named_login.mjs"
            before = replacement.read_bytes()
            replacement.write_bytes(b"!" + before[1:])
            with self.assertRaises(assets.AccountAssetError), self.prepare():
                self.fail("An unapproved source asset was served.")


if __name__ == "__main__":
    unittest.main()
