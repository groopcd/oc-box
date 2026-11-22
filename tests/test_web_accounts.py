import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from oc_box import web_accounts as accounts


HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
PHRASE = "synthetic store unlock phrase"
USERNAME = "synthetic-account"
WORKSPACE = "1234567890abcdef" * 2
OTHER_WORKSPACE = "fedcba0987654321" * 2
SETUP = b"\xff\0synthetic-protocol-server-setup"
REGISTRATION = b"\xfe\0synthetic-protocol-registration"


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-web-accounts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "private"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "accounts.store"
        environment = patch.dict(os.environ, {"OC_BOX_CONTAINER": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def create(self, *, setup=SETUP, phrase=PHRASE):
        accounts.initialize(self.path, phrase, setup=setup)

    def add(
        self,
        name=USERNAME,
        *,
        workspace_ids=(WORKSPACE,),
        record=REGISTRATION,
        phrase=PHRASE,
    ):
        accounts.add_account(
            self.path, phrase, name, workspace_ids=workspace_ids, registration=record
        )

    def write_fixture(self, raw):
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.write_bytes(raw)

    def assert_unchanged(self, raw):
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.directory.iterdir()), [self.path])


class StoreRefusalTests(StoreFixture):
    def test_absence_and_invalid_paths_never_create_state(self):
        with self.assertRaisesRegex(accounts.WebAccountsError, "not initialized"):
            accounts.load(self.path, PHRASE)
        with self.assertRaisesRegex(accounts.WebAccountsError, "not initialized"):
            self.add()
        with self.assertRaisesRegex(accounts.WebAccountsError, "not initialized"):
            accounts.remove_account(self.path, PHRASE, USERNAME)
        for target in (
            "some/file",
            Path("relative"),
            self.directory / "../elsewhere",
            self.root / "missing/accounts.store",
            self.directory / "nul\0",
        ):
            with (
                self.subTest(target=target),
                self.assertRaises(accounts.WebAccountsError),
            ):
                accounts.initialize(target, PHRASE, setup=SETUP)
        self.assertEqual(list(self.root.iterdir()), [self.directory])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_all_operations_refuse_container_before_filesystem_access(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}),
            patch.object(accounts, "_open_directory", side_effect=AssertionError),
        ):
            for operation in (
                self.create,
                lambda: accounts.load(self.path, PHRASE),
                self.add,
                lambda: accounts.remove_account(self.path, PHRASE, USERNAME),
            ):
                with self.assertRaisesRegex(accounts.WebAccountsError, "host-only"):
                    operation()

    def test_names_scopes_and_protocol_records_have_closed_shapes(self):
        for name in (
            "",
            "../ab",
            "UPPER",
            "_prefix",
            "áccount",
            "abc\n",
            "a" * 33,
            b"name",
            None,
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(accounts.WebAccountsError, "usernames"),
            ):
                self.add(name)
        for scope in (
            (),
            ("*",),
            (WORKSPACE.upper(),),
            (WORKSPACE + "\n",),
            ("a" * 31,),
            ("../bad",),
            (WORKSPACE, WORKSPACE),
            ("a" * 32, None),
            WORKSPACE,
            {WORKSPACE},
            [f"{ident:032x}" for ident in range(accounts.MAX_WORKSPACES + 1)],
        ):
            with (
                self.subTest(scope=scope),
                self.assertRaisesRegex(accounts.WebAccountsError, "distinct, explicit"),
            ):
                self.add(workspace_ids=scope)
        for invalid in ("text-not-protocol-bytes", b"", bytearray(b"binary"), None):
            with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
                self.add(record=invalid)
            with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
                self.create(setup=invalid)
        with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
            self.add(record=b"a" * (accounts.MAX_REGISTRATION_BYTES + 1))
        with self.assertRaisesRegex(accounts.WebAccountsError, "protocol record"):
            self.create(setup=b"a" * (accounts.MAX_SETUP_BYTES + 1))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_passphrases_not_echoed_and_cannot_create_store(self):
        for phrase in (
            "",
            b"not-text",
            "7-chars",
            "pass\0phrase",
            "\ud800" * 8,
            "é" * 513,
        ):
            with (
                self.subTest(phrase_type=type(phrase)),
                self.assertRaisesRegex(
                    accounts.WebAccountsError, "passphrase"
                ) as raised,
            ):
                self.create(phrase=phrase)
            self.assertTrue(
                raised.exception.__suppress_context__
                or raised.exception.__context__ is None
            )
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_refuses_symlink_parents_and_nonprivate_directory(self):
        link = self.root / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        inner = self.directory / "inner"
        inner.mkdir(mode=0o700)
        for target in (link / self.path.name, link / "inner" / self.path.name):
            with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                accounts.initialize(target, PHRASE, setup=SETUP)
        self.assertEqual(list(inner.iterdir()), [])
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
            self.create()
        self.assertEqual(list(self.directory.iterdir()), [inner])

    def test_refuses_unsafe_entries_without_crypto_or_replacement(self):
        outside = self.root / "outside"
        outside.write_bytes(b"unrelated file")
        outside.chmod(0o600)
        for kind in ("symlink", "hardlink", "fifo", "wide mode", "oversized"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.path.symlink_to(outside)
                elif kind == "hardlink":
                    self.path.hardlink_to(outside)
                elif kind == "fifo":
                    os.mkfifo(self.path, 0o600)
                else:
                    self.write_fixture(b"unrelated")
                    if kind == "wide mode":
                        self.path.chmod(0o640)
                    else:
                        with self.path.open("r+b") as stream:
                            stream.truncate(accounts.MAX_FILE + 1)
                info = self.path.lstat()
                with patch.object(accounts, "_cipher", side_effect=AssertionError):
                    with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                        accounts.load(self.path, PHRASE)
                    with self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"):
                        self.add()
                    with self.assertRaisesRegex(
                        accounts.WebAccountsError, "not overwrite"
                    ):
                        self.create()
                self.assertEqual(info, self.path.lstat())
                self.path.unlink()
        self.assertEqual(outside.read_bytes(), b"unrelated file")

    def test_lock_wait_is_bounded_and_read_creates_no_lock_file(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                patch.object(accounts, "_LOCK_TIMEOUT", 0.02),
                self.assertRaisesRegex(accounts.WebAccountsError, "busy"),
            ):
                accounts.load(self.path, PHRASE)
        finally:
            os.close(fd)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_missing_optional_crypto_is_a_fixed_refusal_and_leaves_nothing(self):
        def import_without_crypto(name, *args, **kwargs):
            if name.startswith("cryptography."):
                raise ImportError("private dependency detail")
            return original_import(name, *args, **kwargs)

        original_import = __import__
        with (
            patch("builtins.__import__", side_effect=import_without_crypto),
            self.assertRaisesRegex(
                accounts.WebAccountsError, "optional cryptography"
            ) as raised,
        ):
            self.create()
        self.assertNotIn("private dependency detail", str(raised.exception))
        self.assertEqual(list(self.directory.iterdir()), [])


@unittest.skipUnless(HAS_CRYPTO, "optional cryptography not installed")
class EncryptedStoreTests(StoreFixture):
    def test_roundtrip_at_record_and_scope_limits_and_no_plaintext(self):
        maximum_setup = SETUP.ljust(accounts.MAX_SETUP_BYTES, b"x")
        maximum_record = REGISTRATION.ljust(accounts.MAX_REGISTRATION_BYTES, b"y")
        name = USERNAME.ljust(32, "z")
        scope = [f"{number:032x}" for number in range(accounts.MAX_WORKSPACES, 0, -1)]
        self.create(setup=maximum_setup)
        empty = self.path.read_bytes()
        self.assertEqual(accounts.load(self.path, PHRASE).accounts, ())
        self.add(name, workspace_ids=scope, record=maximum_record)
        scope.reverse()
        self.add("another-account", workspace_ids=(WORKSPACE, OTHER_WORKSPACE))

        loaded = accounts.load(self.path, PHRASE)
        self.assertEqual(loaded.setup, maximum_setup)
        self.assertEqual(
            loaded.accounts,
            (
                accounts.Account(
                    "another-account", (WORKSPACE, OTHER_WORKSPACE), REGISTRATION
                ),
                accounts.Account(name, tuple(scope), maximum_record),
            ),
        )
        raw = self.path.read_bytes()
        self.assertNotEqual(
            raw[: accounts._HEADER_SIZE], empty[: accounts._HEADER_SIZE]
        )
        for secret in (
            PHRASE.encode(),
            name.encode(),
            WORKSPACE.encode(),
            SETUP,
            REGISTRATION,
            base64.b64encode(maximum_setup),
            base64.b64encode(maximum_record),
        ):
            self.assertNotIn(secret, raw)
        for value in (name, WORKSPACE, "synthetic-protocol", PHRASE):
            self.assertNotIn(value, repr(loaded))
            self.assertNotIn(value, repr(loaded.accounts))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.stat().st_nlink, 1)
        self.assert_unchanged(raw)

    def test_duplicate_missing_and_wrong_passphrase_do_not_rewrite_records(self):
        self.create()
        self.add()
        before = self.path.read_bytes()
        for operation, message in (
            (self.create, "not overwrite"),
            (lambda: self.add(workspace_ids=(OTHER_WORKSPACE,)), "already exists"),
            (
                lambda: accounts.remove_account(self.path, PHRASE, "absent"),
                "not present",
            ),
            (lambda: self.add("another", phrase="incorrect"), "incorrect passphrase"),
        ):
            with self.assertRaisesRegex(accounts.WebAccountsError, message):
                operation()
            self.assert_unchanged(before)
        accounts.remove_account(self.path, PHRASE, USERNAME)
        self.assertEqual(
            accounts.load(self.path, PHRASE), accounts.AccountStore(SETUP, ())
        )
        self.assertNotEqual(self.path.read_bytes(), before)

    def test_modified_header_ciphertext_and_truncation_fail_closed(self):
        self.create()
        raw = self.path.read_bytes()
        for index in (
            0,
            len(accounts._FORMAT) - 1,
            len(accounts._FORMAT),
            accounts._HEADER_SIZE - 1,
            len(raw) - 1,
        ):
            with self.subTest(index=index):
                damaged = raw[:index] + bytes([raw[index] ^ 1]) + raw[index + 1 :]
                self.write_fixture(damaged)
                with self.assertRaises(accounts.WebAccountsError):
                    self.add()
                self.assert_unchanged(damaged)
        short = raw[: accounts._HEADER_SIZE]
        self.write_fixture(short)
        with (
            patch.object(accounts, "_cipher", side_effect=AssertionError),
            self.assertRaisesRegex(accounts.WebAccountsError, "invalid or unsupported"),
        ):
            accounts.load(self.path, PHRASE)
        self.assert_unchanged(short)

    def test_real_vault_and_web_account_formats_are_not_interchangeable(self):
        from oc_box import vault

        vault_path = self.directory / "vault.json"
        with vault.locked(vault_path, create=True):
            vault.write_encrypted(vault_path, {"EXAMPLE_TOKEN": "synthetic"}, PHRASE)
        self.create()
        with self.assertRaises(vault.VaultError):
            vault.parse_envelope(self.path.read_bytes())
        with (
            patch.object(accounts, "_cipher", side_effect=AssertionError),
            self.assertRaisesRegex(accounts.WebAccountsError, "invalid or unsupported"),
        ):
            accounts.load(vault_path, PHRASE)

    def write_authenticated_fixture(self, plaintext):
        salt, nonce = os.urandom(16), os.urandom(12)
        prefix = accounts._FORMAT + salt + nonce
        cipher, _ = accounts._cipher(PHRASE.encode(), salt)
        self.write_fixture(prefix + cipher.encrypt(nonce, plaintext, prefix))
        return self.path.read_bytes()

    def test_authentication_does_not_waive_schema_or_scope_validation(self):
        current = {
            "username": USERNAME,
            "workspace_ids": [WORKSPACE],
            "registration": "Zg==",
        }
        for description, plaintext in (
            ("duplicate key", b'{"setup":"Zg==","setup":"Zg==","accounts":[]}'),
            ("deep nesting", b"[" * 1500 + b"]" * 1500),
            (
                "duplicate name",
                json.dumps({"setup": "Zg==", "accounts": [current, current]}),
            ),
            (
                "wildcard",
                json.dumps(
                    {"setup": "Zg==", "accounts": [dict(current, workspace_ids=["*"])]}
                ),
            ),
            ("noncanonical base64", json.dumps({"setup": "Zh==", "accounts": []})),
            (
                "unknown field",
                json.dumps(
                    {
                        "setup": "Zg==",
                        "accounts": [dict(current, password="LEAK_SENTINEL")],
                    }
                ),
            ),
        ):
            with self.subTest(description=description):
                before = self.write_authenticated_fixture(
                    plaintext.encode("ascii")
                    if isinstance(plaintext, str)
                    else plaintext
                )
                with self.assertRaisesRegex(
                    accounts.WebAccountsError, "invalid or unsupported"
                ) as raised:
                    self.add("another")
                self.assertNotIn("LEAK_SENTINEL", str(raised.exception))
                self.assert_unchanged(before)

    def test_account_limit_does_not_evict_or_overwrite(self):
        rows = [
            {
                "username": f"account-{index}",
                "workspace_ids": [WORKSPACE],
                "registration": "Zg==",
            }
            for index in range(accounts.MAX_ACCOUNTS)
        ]
        full = self.write_authenticated_fixture(
            json.dumps({"setup": "Zg==", "accounts": rows}).encode("ascii")
        )
        with self.assertRaisesRegex(accounts.WebAccountsError, "account limit"):
            self.add()
        self.assert_unchanged(full)
        oversized = self.write_authenticated_fixture(
            json.dumps({"setup": "Zg==", "accounts": [*rows, rows[0]]}).encode("ascii")
        )
        with self.assertRaisesRegex(
            accounts.WebAccountsError, "invalid or unsupported"
        ):
            accounts.load(self.path, PHRASE)
        self.assert_unchanged(oversized)

    def test_racing_cooperating_writers_keep_both_records(self):
        self.create()
        ready = threading.Barrier(2, timeout=3)

        def write(name):
            ready.wait()
            self.add(name)

        with ThreadPoolExecutor(2) as executor:
            writers = [executor.submit(write, name) for name in ("first", "second")]
            for writer in writers:
                writer.result(timeout=10)
        self.assertEqual(
            [entry.username for entry in accounts.load(self.path, PHRASE).accounts],
            ["first", "second"],
        )

    def test_detected_inode_substitution_is_not_overwritten(self):
        self.create()
        original = self.path.read_bytes()
        substitute = self.directory / "substitute"
        substitute.touch(mode=0o600)
        substitute.write_bytes(b"external replacement")
        seal = accounts._seal

        def replace_after_sealing(*args):
            result = seal(*args)
            substitute.replace(self.path)
            return result

        with (
            patch.object(accounts, "_seal", side_effect=replace_after_sealing),
            self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"),
        ):
            self.add()
        self.assertNotEqual(original, self.path.read_bytes())
        self.assert_unchanged(b"external replacement")

    def test_detected_parent_substitution_is_not_used_for_publication(self):
        self.create()
        before = self.path.read_bytes()
        detached = self.root / "detached"
        seal = accounts._seal

        def move_parent_after_sealing(*args):
            result = seal(*args)
            self.directory.rename(detached)
            self.directory.mkdir(mode=0o700)
            return result

        with (
            patch.object(accounts, "_seal", side_effect=move_parent_after_sealing),
            self.assertRaisesRegex(accounts.WebAccountsError, "unsafe"),
        ):
            self.add()
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(list(detached.iterdir()), [detached / self.path.name])
        self.assertEqual((detached / self.path.name).read_bytes(), before)

    def test_unexpected_destination_at_first_publication_is_never_replaced(self):
        link = os.link

        def conflicting_link(*args, **kwargs):
            self.write_fixture(b"external earlier file")
            return link(*args, **kwargs)

        with (
            patch.object(accounts.os, "link", side_effect=conflicting_link),
            self.assertRaisesRegex(accounts.WebAccountsError, "not overwrite"),
        ):
            self.create()
        self.assert_unchanged(b"external earlier file")

    def test_prepublication_io_failure_and_postpublication_uncertainty(self):
        self.create()
        before = self.path.read_bytes()
        with (
            patch.object(accounts.os, "replace", side_effect=OSError("private path")),
            self.assertRaisesRegex(accounts.WebAccountsError, "unsafe") as raised,
        ):
            self.add()
        self.assertNotIn("private path", str(raised.exception))
        self.assert_unchanged(before)

        fsync = os.fsync

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("private path")
            return fsync(fd)

        with (
            patch.object(accounts.os, "fsync", side_effect=fail_directory_sync),
            self.assertRaisesRegex(accounts.WebAccountsError, "reload before retrying"),
        ):
            self.add()
        self.assertNotEqual(self.path.read_bytes(), before)
        self.assertEqual(
            accounts.load(self.path, PHRASE).accounts[0].username, USERNAME
        )
