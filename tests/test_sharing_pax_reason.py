"""Offline in-memory observation of the unchanged, source-pinned old scanner.

No encrypted-backup/Podman/host acceptance is implied. Archive plaintext stays
in memory, including the isolated worker's stdin; no test runs an engine,
OpenCode, external PATH commands, network, extraction, or interactive prompts.
"""

from contextlib import contextmanager, ExitStack
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import sharing_archive_scan as original
from tools import sharing_pax_reason as worker


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "tools/sharing_archive_scan.py"
WORKER = ROOT / "tools/sharing_pax_reason.py"
PRIVATE = "SYNTHETIC_PRIVATE_PAX_REASON"
EXPECTED_SHA256 = "455abf6daab5f3777471fc960103350ed316d224dd598c20b9e08b3f4670193c"


def record(name, value=b"", *, kind=tarfile.REGTYPE):
    member = tarfile.TarInfo(name)
    member.type = kind
    member.size = len(value)
    return (
        member.tobuf(format=tarfile.USTAR_FORMAT) + value + bytes((-len(value)) % 512)
    )


def make_tar(entries=()):
    return b"".join(record(name, value) for name, value in entries) + bytes(1024)


def pax_body(body):
    length = len(body) + 2
    while True:
        new_length = len(body) + len(str(length)) + 1
        if new_length == length:
            return str(length).encode() + b" " + body
        length = new_length


def pax(key, value):
    return pax_body(key + b"=" + value + b"\n")


def pax_tar(payload, *, content=b"", name="safe", kind=tarfile.XHDTYPE):
    return record("pax", payload, kind=kind) + make_tar([(name, content)])


def make_oci(layer, *, compressed=False, extras=()):
    entries = [("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')]

    def blob(payload, media):
        digest = hashlib.sha256(payload).hexdigest()
        entries.append(("blobs/sha256/" + digest, payload))
        return {"mediaType": media, "digest": "sha256:" + digest, "size": len(payload)}

    config = blob(b"{}", "application/vnd.oci.image.config.v1+json")
    layer = gzip.compress(layer, mtime=0) if compressed else layer
    media = "application/vnd.oci.image.layer.v1.tar" + ("+gzip" if compressed else "")
    layer_descriptor = blob(layer, media)
    manifest = json.dumps(
        {"schemaVersion": 2, "config": config, "layers": [layer_descriptor]},
        separators=(",", ":"),
    ).encode()
    root = blob(manifest, "application/vnd.oci.image.manifest.v1+json")
    entries.append(
        ("index.json", json.dumps({"schemaVersion": 2, "manifests": [root]}).encode())
    )
    for payload in extras:
        blob(payload, "unused")
    return make_tar(entries)


class Pipe:
    def __init__(self, value, chunk=733):
        self.source = io.BytesIO(value)
        self.chunk = chunk
        self.eof = False
        self.requests = []

    def read(self, size):
        if not 0 <= size <= original.CHUNK_BYTES:
            raise AssertionError("unbounded-read")
        self.requests.append(size)
        value = self.source.read(min(size, self.chunk))
        if not value:
            self.eof = True
        return value

    def seek(self, *args):
        raise AssertionError("archive-seek")

    def tell(self):
        raise AssertionError("archive-tell")


@contextmanager
def memory_source(value, *, mode=stat.S_IFREG | 0o600, size=None):
    """Mock only source-file I/O; no archive plaintext file is ever created."""
    source = io.BytesIO(value)
    with (
        patch.object(worker.os, "open", return_value=731) as opener,
        patch.object(
            worker.os,
            "fstat",
            return_value=SimpleNamespace(
                st_mode=mode, st_size=len(value) if size is None else size
            ),
        ),
        patch.object(
            worker.os, "read", side_effect=lambda fd, size: source.read(size)
        ) as reader,
        patch.object(worker.os, "close") as closer,
    ):
        yield opener, reader, closer


class PaxReasonTests(unittest.TestCase):
    def invoke(self, payload, mode="tar", *, chunk=733, path=SCANNER, argv=None):
        output, errors = io.StringIO(), io.StringIO()
        source = Pipe(payload, chunk=chunk)
        code = worker.main(
            [str(path), mode] if argv is None else argv,
            stdin=source,
            stdout=output,
            stderr=errors,
        )
        return code, output.getvalue(), errors.getvalue(), source

    def assert_parity(
        self, payload, mode="tar", *, reason=None, category=None, chunk=733
    ):
        old_output, old_errors = io.StringIO(), io.StringIO()
        old_source = Pipe(payload, chunk=chunk)
        old_code = original.main(
            [mode], stdin=old_source, stdout=old_output, stderr=old_errors
        )
        code, output, errors, source = self.invoke(payload, mode, chunk=chunk)
        self.assertEqual(code, old_code)
        self.assertEqual(output, old_output.getvalue())
        self.assertEqual(output, worker.REFUSAL if category else worker.SUCCESS)
        expected = "archive-scan:" + category + "\n" if category else ""
        self.assertEqual(old_errors.getvalue(), expected)
        self.assertEqual(
            errors, expected + ("pax-reason:" + reason + "\n" if reason else "")
        )
        self.assertEqual(source.eof, old_source.eof)
        if mode in ("tar", "oci"):
            self.assertTrue(source.eof, "refusal must still let the producer reach EOF")
        return code, output, errors

    def assert_worker_failure(self, result):
        code, output, errors, _ = result
        self.assertEqual(
            (code, output, errors),
            (1, worker.REFUSAL, "archive-scan:malformed-input\n"),
        )
        self.assertNotIn(PRIVATE, output + errors)

    def test_original_source_identity_is_fixed_and_unchanged(self):
        before = SCANNER.read_bytes()
        self.assertEqual(hashlib.sha256(before).hexdigest(), EXPECTED_SHA256)
        self.assertEqual(worker.ORIGINAL_SCANNER_SHA256, EXPECTED_SHA256)
        module = worker._load_scanner(SCANNER)
        self.assertEqual(
            module.pax_fields(pax(b"comment", b"safe")), {"comment": "safe"}
        )
        self.assertEqual(SCANNER.read_bytes(), before)

    def test_exported_reasons_are_only_the_finite_protocol_labels(self):
        self.assertEqual(
            worker.PAX_REASONS,
            (
                "record-length",
                "length-prefix",
                "delimiter",
                "key-nul",
                "key-utf8",
                "xattr-value-nul",
                "xattr-value-utf8",
                "text-value-nul",
                "text-value-utf8",
                "size",
                "owner",
                "timestamp",
                "outside-pax-fields",
                "not-observed",
            ),
        )

    def test_positive_tar_oci_and_utf8_xattr_parity(self):
        layer = pax_tar(
            pax(b"SCHILY.xattr.user.safe", "synthetic-\u00e9".encode()),
            content=b"disclosed history",
        )
        for case, mode, payload in (
            ("empty", "tar", make_tar()),
            ("utf8-xattr", "tar", layer),
            ("raw-oci", "oci", make_oci(layer)),
            ("gzip-oci", "oci", make_oci(layer, compressed=True)),
        ):
            with self.subTest(case=case):
                self.assert_parity(payload, mode)

    def test_raw_binary_xattr_refusal_parity_in_tar_and_oci_layers(self):
        for value, reason in (
            (b"binary\0\xff", "xattr-value-nul"),
            (b"binary\xff", "xattr-value-utf8"),
        ):
            layer = pax_tar(pax(b"SCHILY.xattr.security.synthetic", value))
            for mode, payload in (
                ("tar", layer),
                ("oci", make_oci(layer)),
                ("oci", make_oci(layer, compressed=True)),
            ):
                with self.subTest(mode=mode, reason=reason):
                    self.assert_parity(
                        payload, mode, category="malformed-pax", reason=reason
                    )

    def test_framing_prefix_and_record_length_branches(self):
        for name, payload, reason in (
            ("no-space", b"nonsense", "length-prefix"),
            ("empty-prefix", b" x=y\n", "length-prefix"),
            ("nondigit", b"a x=y\n", "length-prefix"),
            ("negative", b"-5 x=y\n", "length-prefix"),
            ("prefix-window", b"123456789012 x=y\n", "length-prefix"),
            ("short-length", b"4 x=\n", "record-length"),
            ("past-end", b"99 path=x\n", "record-length"),
            ("missing-newline", pax(b"path", b"safe")[:-1] + b"!", "record-length"),
            ("after-valid", pax(b"comment", b"safe") + b"tail", "length-prefix"),
        ):
            with self.subTest(case=name):
                self.assert_parity(
                    pax_tar(payload), category="malformed-pax", reason=reason
                )

    def test_delimiter_branch_precedes_nul_and_utf8(self):
        for body in (b"missing-equals\n", b"=value\n", b"\0\xff\n", b"=\0\xff\n"):
            with self.subTest(body=body):
                self.assert_parity(
                    pax_tar(pax_body(body)),
                    category="malformed-pax",
                    reason="delimiter",
                )

    def test_key_and_text_encoding_branches(self):
        for key, value, reason in (
            (b"key\0", b"value", "key-nul"),
            (b"key\xff", b"value", "key-utf8"),
            (b"comment", b"value\0", "text-value-nul"),
            (b"comment", b"value\xff", "text-value-utf8"),
        ):
            with self.subTest(reason=reason):
                self.assert_parity(
                    pax_tar(pax(key, value)), category="malformed-pax", reason=reason
                )

    def test_nul_branch_order_is_before_either_utf8_decode(self):
        for key, value, reason in (
            (b"\xff\0", b"\0\xff", "key-nul"),
            (b"\xff", b"\0\xff", "text-value-nul"),
            (b"SCHILY.xattr.\xff", b"\0\xff", "text-value-nul"),
            (b"SCHILY.xattr.valid", b"\0\xff", "xattr-value-nul"),
            (b"\xff", b"\xff", "key-utf8"),
        ):
            with self.subTest(reason=reason, key=key):
                self.assert_parity(
                    pax_tar(pax(key, value)), category="malformed-pax", reason=reason
                )

    def test_only_clean_utf8_nonempty_exact_xattr_prefix_is_marked(self):
        for key in (
            b"SCHILY.xattr.",
            b"SCHILY.xattr",
            b"prefix.SCHILY.xattr.user.key",
            b"schily.xattr.user.key",
            b"SCHILY.xattr.\xff",
        ):
            with self.subTest(key=key):
                self.assert_parity(
                    pax_tar(pax(key, b"\0")),
                    category="malformed-pax",
                    reason="text-value-nul",
                )
        self.assert_parity(
            pax_tar(pax("SCHILY.xattr.\u00e9".encode(), b"\xff")),
            category="malformed-pax",
            reason="xattr-value-utf8",
        )

    def test_size_numeric_branch(self):
        for value in (b"", b"-1", b"+1", b"1.0", b"9" * 21, b"\xd9\xa1"):
            with self.subTest(value=value):
                self.assert_parity(
                    pax_tar(pax(b"size", value)),
                    category="malformed-pax",
                    reason="size",
                )

    def test_xattr_classification_requires_the_complete_clean_raw_key(self):
        for field in (
            b"SCHILY.xattr.",
            b"SCHILY.xattr.user\0key",
            b"SCHILY.xattr.user\nkey",
            b"SCHILY.xattr.user\rkey",
            b"SCHILY.xattr.user=key",
            b"SCHILY.xattr.\xff",
            b"schily.xattr.user.key",
            b"prefix.SCHILY.xattr.user.key",
        ):
            with self.subTest(field=field):
                self.assertFalse(worker._is_xattr_key(field))
        self.assertTrue(worker._is_xattr_key(b"SCHILY.xattr.user.key"))
        self.assertTrue(worker._is_xattr_key("SCHILY.xattr.\u00e9".encode()))
        for field in (b"SCHILY.xattr.user\nkey", b"SCHILY.xattr.user\rkey"):
            for value, reason in (
                (b"\0", "text-value-nul"),
                (b"\xff", "text-value-utf8"),
            ):
                with self.subTest(field=field, reason=reason):
                    self.assert_parity(
                        pax_tar(pax(field, value)),
                        category="malformed-pax",
                        reason=reason,
                    )

    def test_owner_numeric_branch(self):
        for key in (b"uid", b"gid"):
            for value in (b"", b"-1", b"1.0", b"9" * 21):
                with self.subTest(key=key, value=value):
                    self.assert_parity(
                        pax_tar(pax(key, value)),
                        category="malformed-pax",
                        reason="owner",
                    )

    def test_timestamp_branch(self):
        for key in (b"mtime", b"atime", b"ctime"):
            for value in (b"", b"invalid", b"NaN", b"Infinity", b"-Inf", b"1e9999"):
                with self.subTest(key=key, value=value):
                    self.assert_parity(
                        pax_tar(pax(key, value)),
                        category="malformed-pax",
                        reason="timestamp",
                    )

    def test_valid_numeric_pax_fields_are_not_changed(self):
        fields = (
            (b"size", b"0"),
            (b"uid", b"123"),
            (b"gid", b"00012"),
            (b"mtime", b"-1.25"),
            (b"atime", b"1e2"),
            (b"ctime", b"0"),
        )
        self.assert_parity(pax_tar(b"".join(pax(key, value) for key, value in fields)))

    def test_other_pax_categories_have_no_reason_suffix(self):
        for payload, category in (
            (pax(b"comment", b"a") + pax(b"comment", b"b"), "unsupported-tar"),
            (pax(b"size", b"0") + pax(b"size", b"-1"), "unsupported-tar"),
            (pax(b"GNU.sparse.map", b"0,4"), "unsupported-tar"),
            (pax(b"SCHILY.realsize", b"1"), "unsupported-tar"),
            (pax(b"hdrcharset", b"BINARY"), "unsupported-tar"),
            (
                pax(b"size", str(original.MAX_MEMBER_BYTES + 1).encode()),
                "resource-limit",
            ),
        ):
            with self.subTest(category=category):
                self.assert_parity(pax_tar(payload), category=category)

    def test_nonparser_pax_padding_uses_outside_label(self):
        value = bytearray(pax_tar(pax(b"comment", b"safe")))
        value[512 + len(pax(b"comment", b"safe"))] = 1
        for mode, payload in (("tar", bytes(value)), ("oci", make_oci(bytes(value)))):
            with self.subTest(mode=mode):
                self.assert_parity(
                    payload, mode, category="malformed-pax", reason="outside-pax-fields"
                )

    def test_overridden_global_local_and_raw_header_paths_still_refuse(self):
        for payload in (
            record("global", pax(b"path", b"share-input-a/asset"), kind=tarfile.XGLTYPE)
            + pax_tar(pax(b"path", b"safe")),
            pax_tar(pax(b"path", b"safe"), name="share-input-b/asset"),
            pax_tar(pax(b"linkpath", b"share-input-a/asset")),
        ):
            for mode, archive in (("tar", payload), ("oci", make_oci(payload))):
                with self.subTest(mode=mode):
                    self.assert_parity(archive, mode, category="forbidden-path")

    def test_raw_never_read_content_marker_remains_false_not_success(self):
        layer = pax_tar(
            pax(b"comment", b"safe"),
            content=original.FORBIDDEN_CONTENT + PRIVATE.encode(),
        )
        for mode, payload in (("tar", layer), ("oci", make_oci(layer))):
            with self.subTest(mode=mode):
                self.assert_parity(
                    payload, mode, category="forbidden-content", chunk=17
                )

    def test_other_actual_scanner_refusal_categories_preserve_exact_output(self):
        for mode, payload, category in (
            ("tar", b"short", "malformed-tar"),
            ("tar", record("safe", kind=b"Z") + bytes(1024), "unsupported-tar"),
            ("oci", make_tar(), "malformed-oci"),
            ("oci", make_oci(make_tar(), extras=(b"\x89opaque",)), "unsupported-blob"),
            (
                "oci",
                make_oci(make_tar(), extras=(b'{"unfinished":',)),
                "malformed-json",
            ),
            ("invalid", b"", "invalid-mode"),
        ):
            with self.subTest(category=category):
                self.assert_parity(payload, mode, category=category)

    def test_eof_drain_on_positive_parser_and_nonparser_refusals(self):
        for layer, category, reason in (
            (make_tar([("safe", b"content")]), None, None),
            (
                pax_tar(pax(b"SCHILY.xattr.user.key", b"\xff")),
                "malformed-pax",
                "xattr-value-utf8",
            ),
            (
                b"invalid-header" + b"x" * (2 * original.CHUNK_BYTES),
                "malformed-tar",
                None,
            ),
        ):
            for chunk in (1, 17, 733):
                with self.subTest(category=category, chunk=chunk):
                    self.assert_parity(
                        layer, category=category, reason=reason, chunk=chunk
                    )

    def test_original_parser_runs_first_and_same_exception_is_reraised(self):
        module = worker._load_scanner(SCANNER)
        failure = module.ScanError("malformed-pax")
        calls = []

        def refusing(payload):
            calls.append("original")
            raise failure

        def classify(payload):
            self.assertEqual(calls, ["original"])
            calls.append("observer")
            return "length-prefix"

        module.pax_fields = refusing
        observation = worker._wrap_pax_fields(module)
        with patch.object(worker, "_classify_pax", side_effect=classify):
            with self.assertRaises(module.ScanError) as caught:
                module.pax_fields(b"bad")
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, ["original", "observer"])
        self.assertEqual(observation.reason, "length-prefix")

    def test_success_and_other_exceptions_never_invoke_classifier(self):
        module = worker._load_scanner(SCANNER)
        observation = worker._wrap_pax_fields(module)
        with patch.object(
            worker, "_classify_pax", side_effect=AssertionError("observer-called")
        ) as classify:
            self.assertEqual(
                module.pax_fields(pax(b"comment", b"safe")), {"comment": "safe"}
            )
            with self.assertRaises(module.ScanError) as caught:
                module.pax_fields(pax(b"GNU.sparse.map", b"0,1"))
        self.assertEqual(caught.exception.args, ("unsupported-tar",))
        classify.assert_not_called()
        self.assertEqual(observation.reason, "not-observed")

    def test_only_first_fixed_reason_is_retained(self):
        module = worker._load_scanner(SCANNER)
        observation = worker._wrap_pax_fields(module)
        for payload in (b"bad-prefix", pax(b"comment", b"\xff")):
            with self.assertRaises(module.ScanError):
                module.pax_fields(payload)
        self.assertEqual(observation.reason, "length-prefix")

    def test_observer_failure_preserves_exception_but_worker_refuses_fixed(self):
        module = worker._load_scanner(SCANNER)
        failure = module.ScanError("malformed-pax")
        module.pax_fields = lambda payload: (_ for _ in ()).throw(failure)
        observation = worker._wrap_pax_fields(module)
        for problem in (
            ValueError(PRIVATE),
            KeyboardInterrupt(PRIVATE),
            SystemExit(PRIVATE),
        ):
            with self.subTest(problem=type(problem).__name__):
                with patch.object(worker, "_classify_pax", side_effect=problem):
                    with self.assertRaises(module.ScanError) as caught:
                        module.pax_fields(b"bad")
                self.assertIs(caught.exception, failure)
                self.assertTrue(observation.failed)
                with patch.object(worker, "_classify_pax", side_effect=problem):
                    self.assert_worker_failure(self.invoke(pax_tar(b"bad")))

    def test_input_failure_supersedes_observed_pax_without_reason_suffix(self):
        class BrokenAfterArchive(Pipe):
            def read(self, size):
                if self.source.tell() == len(self.source.getvalue()):
                    raise OSError(PRIVATE)
                return super().read(size)

        output, errors = io.StringIO(), io.StringIO()
        code = worker.main(
            [str(SCANNER), "tar"],
            stdin=BrokenAfterArchive(pax_tar(b"bad")),
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(
            (code, output.getvalue(), errors.getvalue()),
            (1, worker.REFUSAL, "archive-scan:input-error\n"),
        )

    def test_source_open_is_nofollow_regular_bounded_and_read_only(self):
        source = SCANNER.read_bytes()
        with memory_source(source) as (opener, reader, closer):
            module = worker._load_scanner("private-source-path")
        opener.assert_called_once_with(
            "private-source-path",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        closer.assert_called_once_with(731)
        self.assertTrue(reader.call_args_list)
        self.assertLessEqual(
            sum(call.args[1] for call in reader.call_args_list), worker.MAX_SOURCE_BYTES
        )
        self.assertEqual(
            module.pax_fields(pax(b"comment", b"safe")), {"comment": "safe"}
        )

    def test_unknown_changed_and_malformed_source_never_compile(self):
        original_bytes = SCANNER.read_bytes()
        for value in (
            b"",
            b"not Python\xff",
            ("raise RuntimeError('" + PRIVATE + "')\n").encode(),
            original_bytes + b"\n",
            original_bytes.replace(b"malformed-pax", b"changed-pax"),
        ):
            with self.subTest(size=len(value)), memory_source(value):
                with patch("builtins.compile") as compile_source:
                    self.assert_worker_failure(self.invoke(make_tar()))
                compile_source.assert_not_called()

    def test_special_oversized_and_missing_source_refuse_without_reading(self):
        for mode, size in (
            (stat.S_IFIFO | 0o600, 1),
            (stat.S_IFDIR | 0o700, 1),
            (stat.S_IFCHR | 0o600, 1),
            (stat.S_IFREG | 0o600, worker.MAX_SOURCE_BYTES + 1),
        ):
            with (
                self.subTest(mode=mode, size=size),
                memory_source(b"x", mode=mode, size=size) as (_, reader, closer),
            ):
                self.assert_worker_failure(self.invoke(make_tar()))
                reader.assert_not_called()
                closer.assert_called_once_with(731)
        with patch.object(worker.os, "open", side_effect=OSError(PRIVATE)):
            self.assert_worker_failure(self.invoke(make_tar()))

    def test_growing_or_short_read_source_is_bounded_and_verified(self):
        for value in (b"x" * worker.MAX_SOURCE_BYTES, SCANNER.read_bytes()[:-1]):
            with (
                self.subTest(size=len(value)),
                memory_source(value, size=1) as (_, reader, _),
            ):
                self.assert_worker_failure(self.invoke(make_tar()))
            # Requested bytes, not just returned bytes, remain bounded too.
            self.assertLessEqual(
                sum(call.args[1] for call in reader.call_args_list),
                worker.MAX_SOURCE_BYTES,
            )

    def test_verified_byte_execution_ignores_import_and_bytecode_cache(self):
        poisoned = SimpleNamespace(pax_fields=lambda payload: {PRIVATE: PRIVATE})
        with patch.dict(sys.modules, {"_sharing_verified_archive_scan": poisoned}):
            first = worker._load_scanner(SCANNER)
            first.pax_fields = lambda payload: {PRIVATE: PRIVATE}
            second = worker._load_scanner(SCANNER)
        self.assertIsNot(first, second)
        self.assertEqual(
            second.pax_fields(pax(b"comment", b"safe")), {"comment": "safe"}
        )
        self.assertIs(sys.modules.get("tools.sharing_archive_scan"), original)

    def test_protocol_errors_fail_closed_without_reflecting_any_text(self):
        cases = (
            (0, worker.SUCCESS + PRIVATE, ""),
            (0, worker.SUCCESS, PRIVATE),
            (0, worker.REFUSAL, "archive-scan:malformed-pax\n"),
            (1, worker.SUCCESS, "archive-scan:malformed-pax\n"),
            (2, worker.REFUSAL, "archive-scan:forbidden-path\n"),
            (False, worker.SUCCESS, ""),
            (1, worker.REFUSAL, "archive-scan:" + PRIVATE + "\n"),
            (1, worker.REFUSAL, "archive-scan:forbidden-path\n" + PRIVATE),
            (1, worker.REFUSAL, "archive-scan:forbidden-path"),
            (1, worker.REFUSAL, "archive-scan:malformed-pax\npax-reason:key-nul\n"),
        )
        for code, output, errors in cases:
            module = worker._load_scanner(SCANNER)

            def invalid_main(argv, *, stdin, stdout, stderr):
                stdout.write(output)
                stderr.write(errors)
                return code

            module.main = invalid_main
            with (
                self.subTest(code=code, errors=errors),
                patch.object(worker, "_load_scanner", return_value=module),
            ):
                self.assert_worker_failure(self.invoke(make_tar()))

    def test_every_allowlisted_category_is_literal_and_nonpax_has_no_suffix(self):
        for category in worker.SCANNER_CATEGORIES:
            module = worker._load_scanner(SCANNER)

            def fixed_main(argv, *, stdin, stdout, stderr):
                stdout.write(worker.REFUSAL)
                stderr.write("archive-scan:" + category + "\n")
                return 1

            module.main = fixed_main
            with (
                self.subTest(category=category),
                patch.object(worker, "_load_scanner", return_value=module),
            ):
                code, output, errors, _ = self.invoke(make_tar())
            expected = "archive-scan:" + category + "\n"
            if category == "malformed-pax":
                expected += "pax-reason:outside-pax-fields\n"
            self.assertEqual((code, output, errors), (1, worker.REFUSAL, expected))

    def test_exception_and_argument_failures_have_only_fixed_output(self):
        for failure in (
            OSError(PRIVATE),
            ValueError(PRIVATE),
            KeyboardInterrupt(PRIVATE),
            SystemExit(PRIVATE),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(worker, "_load_scanner", side_effect=failure),
            ):
                self.assert_worker_failure(self.invoke(make_tar()))
        for argv in ([], [PRIVATE], [PRIVATE, "tar", PRIVATE]):
            with self.subTest(argv=argv), patch.object(worker, "_load_scanner") as load:
                self.assert_worker_failure(self.invoke(make_tar(), argv=argv))
                load.assert_not_called()

    def test_output_contains_no_names_values_paths_payload_digests_or_counts(self):
        key = b"SCHILY.xattr." + PRIVATE.encode()
        layer = pax_tar(
            pax(key, PRIVATE.encode() + b"\xff"), name=PRIVATE, content=PRIVATE.encode()
        )
        payload = make_oci(layer)
        code, output, errors, _ = self.invoke(payload, "oci")
        self.assertEqual(code, 1)
        self.assertEqual(output, worker.REFUSAL)
        self.assertEqual(
            errors, "archive-scan:malformed-pax\npax-reason:xattr-value-utf8\n"
        )
        for sensitive in (
            PRIVATE,
            str(SCANNER),
            hashlib.sha256(payload).hexdigest(),
            str(len(payload)),
        ):
            self.assertNotIn(sensitive, output + errors)

    def test_no_extraction_staging_network_or_process_calls_in_worker(self):
        # Permit precisely the source read before installing stricter boundaries.
        for mode, payload in (("tar", make_tar()), ("oci", make_oci(make_tar()))):
            module = worker._load_scanner(SCANNER)
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(worker, "_load_scanner", return_value=module)
                )
                for target in (
                    "builtins.open",
                    "io.open",
                    "os.open",
                    "os.write",
                    "os.system",
                    "subprocess.Popen",
                    "socket.socket",
                    "tempfile.TemporaryFile",
                    "tempfile.NamedTemporaryFile",
                    "tempfile.SpooledTemporaryFile",
                    "tempfile.mkstemp",
                    "tempfile.mkdtemp",
                    "tarfile.TarFile.extract",
                    "tarfile.TarFile.extractall",
                    "tarfile.TarFile.extractfile",
                ):
                    stack.enter_context(
                        patch(
                            target, side_effect=AssertionError("forbidden-side-effect")
                        )
                    )
                self.assert_parity(payload, mode)

    def test_isolated_cli_uses_stdin_and_exact_boolean_category_reason(self):
        for mode, payload, code, errors in (
            ("tar", make_tar(), 0, b""),
            ("oci", make_oci(make_tar()), 0, b""),
            (
                "oci",
                make_oci(pax_tar(pax(b"SCHILY.xattr.user.key", b"\xff"))),
                1,
                b"archive-scan:malformed-pax\npax-reason:xattr-value-utf8\n",
            ),
            (
                "tar",
                make_tar([("safe", original.FORBIDDEN_CONTENT)]),
                1,
                b"archive-scan:forbidden-content\n",
            ),
        ):
            with self.subTest(mode=mode, code=code):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(WORKER), str(SCANNER), mode],
                    input=payload,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, code)
                self.assertEqual(
                    result.stdout, (worker.REFUSAL if code else worker.SUCCESS).encode()
                )
                self.assertEqual(result.stderr, errors)

    def test_isolated_cli_unknown_source_never_exposes_paths_or_exception(self):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(WORKER), str(ROOT / PRIVATE), "oci"],
            input=make_oci(make_tar()),
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, worker.REFUSAL.encode())
        self.assertEqual(result.stderr, b"archive-scan:malformed-input\n")


if __name__ == "__main__":
    unittest.main()
