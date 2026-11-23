"""In-memory scanner checks; these are not encrypted-backup/Podman acceptance."""

import bz2
from contextlib import ExitStack
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools/sharing_archive_scan.py"
SPEC = importlib.util.spec_from_file_location("sharing_archive_scan", SCRIPT)
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER_TYPES = {
    "tar": "application/vnd.oci.image.layer.v1.tar",
    "gzip": "application/vnd.oci.image.layer.v1.tar+gzip",
    "bz2": "application/vnd.oci.image.layer.v1.tar+bzip2",
    "xz": "application/vnd.oci.image.layer.v1.tar+xz",
}
ENCODE = {
    "tar": lambda value: value,
    "gzip": lambda value: gzip.compress(value, mtime=0),
    "bz2": bz2.compress,
    "xz": lzma.compress,
}


def encoded_json(value):
    return json.dumps(value, separators=(",", ":")).encode()


def make_tar(entries, *, format=tarfile.PAX_FORMAT):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=format) as archive:
        for name, value in entries:
            if isinstance(name, tarfile.TarInfo):
                member = name
            else:
                member = tarfile.TarInfo(name)
                member.size = len(value)
            archive.addfile(member, io.BytesIO(value) if member.isreg() else None)
    return output.getvalue()


def record(name, value=b"", *, kind=tarfile.REGTYPE, declared_size=None):
    member = tarfile.TarInfo(name)
    member.type = kind
    member.size = len(value) if declared_size is None else declared_size
    return (
        member.tobuf(format=tarfile.USTAR_FORMAT) + value + bytes((-len(value)) % 512)
    )


def pax_record(key, value):
    body = (key + "=" + value + "\n").encode()
    length = len(body) + 2
    while True:
        new_length = len(body) + len(str(length)) + 1
        if new_length == length:
            return str(length).encode() + b" " + body
        length = new_length


def descriptor(payload, media):
    digest = hashlib.sha256(payload).hexdigest()
    return {"mediaType": media, "digest": "sha256:" + digest, "size": len(payload)}


def oci_entries(layers=(), *, config=None, extras=(), nested=False):
    """An actual OCI layout, with descriptors over the actual blob bytes."""
    config = (
        encoded_json({"architecture": "amd64", "os": "linux"})
        if config is None
        else config
    )
    config_descriptor = descriptor(config, OCI_CONFIG)
    blobs = [(config_descriptor, config)]
    layer_descriptors = []
    for payload, media in layers:
        item = descriptor(payload, media)
        layer_descriptors.append(item)
        blobs.append((item, payload))
    manifest = encoded_json(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": config_descriptor,
            "layers": layer_descriptors,
        }
    )
    root = descriptor(manifest, OCI_MANIFEST)
    blobs.append((root, manifest))
    if nested:
        index_blob = encoded_json({"schemaVersion": 2, "manifests": [root]})
        root = descriptor(index_blob, OCI_INDEX)
        blobs.append((root, index_blob))
    entries = [
        ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
        ("index.json", encoded_json({"schemaVersion": 2, "manifests": [root]})),
    ]
    entries.extend(
        ("blobs/sha256/" + item["digest"][7:], payload) for item, payload in blobs
    )
    for payload in extras:
        entries.append(("blobs/sha256/" + hashlib.sha256(payload).hexdigest(), payload))
    return entries


class Pipe:
    """A short-reading input that refuses all disk-like positioning."""

    def __init__(self, value, chunk=733):
        self.source = io.BytesIO(value)
        self.chunk = chunk
        self.eof = False
        self.maximum_requested = 0

    def read(self, size):
        if size < 0:
            raise AssertionError("unbounded-read")
        self.maximum_requested = max(self.maximum_requested, size)
        value = self.source.read(min(size, self.chunk))
        if not value:
            self.eof = True
        return value

    def seek(self, *args):
        raise AssertionError("input-seek")

    def tell(self):
        raise AssertionError("input-tell")


class SharingArchiveScanTests(unittest.TestCase):
    def scan(self, payload, mode="tar", *, category=None, chunk=733):
        source = Pipe(payload, chunk=chunk)
        if category is None:
            self.assertIsNone(scanner.scan_archive(source, mode))
        else:
            with self.assertRaises(scanner.ScanError) as caught:
                scanner.scan_archive(source, mode)
            self.assertEqual(str(caught.exception), category)
        self.assertTrue(
            source.eof, "producer EOF must be consumed, including on refusal"
        )
        self.assertLessEqual(source.maximum_requested, scanner.CHUNK_BYTES)

    def test_tar_format_and_intentionally_disclosed_history_matrix(self):
        for name, format in (
            ("ustar", tarfile.USTAR_FORMAT),
            ("pax", tarfile.PAX_FORMAT),
            ("gnu", tarfile.GNU_FORMAT),
        ):
            with self.subTest(format=name):
                entries = [
                    ("history/native.json", b'{"shared":"synthetic disclosed text"}'),
                    ("history/shared.png", b"\x89PNG\r\nsynthetic disclosed image"),
                    ("safe/share-input-a-suffix", b"SHARING_VISIBLE_CONTENT_"),
                ]
                if format != tarfile.USTAR_FORMAT:
                    entries.append(("long/" + "x" * 180 + "/asset", b"allowed"))
                self.scan(make_tar(entries, format=format))

    def test_forbidden_path_component_and_link_target_matrix(self):
        for component in sorted(scanner.FORBIDDEN_COMPONENTS):
            for name in (component, "root/" + component + "/asset"):
                with self.subTest(component=component, location=name):
                    self.scan(make_tar([(name, b"unread")]), category="forbidden-path")
            for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                with self.subTest(component=component, link=kind):
                    member = tarfile.TarInfo("safe-link")
                    member.type = kind
                    member.linkname = "elsewhere/" + component + "/asset"
                    self.scan(make_tar([(member, b"")]), category="forbidden-path")

    def test_never_read_marker_cross_chunk_matrix_and_every_regular_member(self):
        marker = scanner.FORBIDDEN_CONTENT
        for offset in (0, 1, 16, 17, 18, 4095):
            with self.subTest(offset=offset), patch.object(scanner, "CHUNK_BYTES", 17):
                self.scan(
                    make_tar(
                        [
                            ("before", b"safe"),
                            (
                                "history/native.json",
                                b"x" * offset + marker + b"private",
                            ),
                            ("after", b"safe"),
                        ]
                    ),
                    category="forbidden-content",
                    chunk=13,
                )
        # A marker must not be manufactured by joining distinct files.
        self.scan(make_tar([("one", marker[:8]), ("two", marker[8:])]))

    def test_oci_real_layer_compression_order_and_nested_index_matrix(self):
        layer = make_tar([("root/native.json", b"intentionally disclosed history")])
        for compression, encode in ENCODE.items():
            for order in ("index-first", "blobs-first"):
                with self.subTest(compression=compression, order=order):
                    entries = oci_entries(
                        [(encode(layer), LAYER_TYPES[compression])], nested=True
                    )
                    if order == "blobs-first":
                        entries.reverse()
                    self.scan(make_tar(entries), "oci")

    def test_oci_every_layer_and_unreferenced_blob_is_scanned_matrix(self):
        safe = make_tar([("root/safe", b"visible shared bytes")])
        for compression, encode in ENCODE.items():
            for case, bad_layer, category in (
                (
                    "filename",
                    make_tar([("opt/share-input-b/asset", b"safe")]),
                    "forbidden-path",
                ),
                (
                    "content",
                    make_tar([("root/asset", scanner.FORBIDDEN_CONTENT)]),
                    "forbidden-content",
                ),
            ):
                for referenced in (True, False):
                    with self.subTest(
                        compression=compression, case=case, referenced=referenced
                    ):
                        layers = [(safe, LAYER_TYPES["tar"])]
                        extras = ()
                        if referenced:
                            layers.append((encode(bad_layer), LAYER_TYPES[compression]))
                        else:
                            extras = (encode(bad_layer),)
                        self.scan(
                            make_tar(oci_entries(layers, extras=extras)),
                            "oci",
                            category=category,
                        )

    def test_oci_json_config_and_artifact_config_marker_matrix(self):
        for role in ("config", "unreferenced-artifact-config"):
            for marker in (False, True):
                with self.subTest(role=role, marker=marker):
                    payload = encoded_json(
                        {
                            "artifactType": "application/vnd.synthetic.artifact",
                            "value": (
                                scanner.FORBIDDEN_CONTENT.decode()
                                if marker
                                else "disclosed-text"
                            ),
                        }
                    )
                    entries = (
                        oci_entries(config=payload)
                        if role == "config"
                        else oci_entries(extras=(payload,))
                    )
                    self.scan(
                        make_tar(entries),
                        "oci",
                        category="forbidden-content" if marker else None,
                    )

    def test_oci_unknown_blob_and_malformed_json_matrix(self):
        for name, payload, category in (
            ("opaque", b"\x89opaque-binary", "unsupported-blob"),
            ("zstd", b"\x28\xb5\x2f\xfdsynthetic", "unsupported-compression"),
            ("malformed-json", b'{"unfinished":', "malformed-json"),
            ("duplicate-key", b'{"x":1,"x":2}', "malformed-json"),
            ("nonfinite", b'{"x":NaN}', "malformed-json"),
            ("overflow", b'{"x":1e9999}', "malformed-json"),
            ("invalid-utf8", b'{"x":"\xff"}', "malformed-json"),
            ("invalid-tar", bytes(512) + b"not-a-second-zero-block", "malformed-tar"),
        ):
            with self.subTest(case=name):
                self.scan(
                    make_tar(oci_entries(extras=(payload,))), "oci", category=category
                )

    def test_oci_compressed_trailer_and_truncation_matrix(self):
        layer = make_tar([("root/safe", b"safe")])
        for compression in ("gzip", "bz2", "xz"):
            compressed = ENCODE[compression](layer)
            for corruption, payload in (
                ("truncated", compressed[:-5]),
                ("binary-suffix", compressed + b"opaque-trailer"),
                ("checksum", compressed[:-8] + bytes(8)),
            ):
                with self.subTest(compression=compression, corruption=corruption):
                    self.scan(
                        make_tar(oci_entries(extras=(payload,))),
                        "oci",
                        category="malformed-compression",
                    )

    def test_oci_concatenated_compression_cannot_hide_a_second_tar(self):
        for compression in ("gzip", "bz2", "xz"):
            encode = ENCODE[compression]
            hidden = encode(make_tar([])) + encode(
                make_tar([("share-input-a/asset", b"safe")])
            )
            with self.subTest(compression=compression):
                self.scan(
                    make_tar(oci_entries(extras=(hidden,))),
                    "oci",
                    category="malformed-tar",
                )

    def test_oci_outer_entry_type_and_layout_matrix(self):
        entries = oci_entries()
        for kind in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.FIFOTYPE,
            tarfile.CHRTYPE,
        ):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo("blobs/sha256/" + "a" * 64)
                member.type = kind
                member.linkname = "synthetic-target"
                self.scan(
                    make_tar(entries + [(member, b"")]),
                    "oci",
                    category="unexpected-oci-entry",
                )
        for case, value, category in (
            (
                "unexpected-file",
                entries + [("manifest.json", b"[]")],
                "unexpected-oci-entry",
            ),
            ("duplicate-index", entries + [entries[1]], "malformed-oci"),
            ("missing-layout", entries[1:], "malformed-oci"),
            ("missing-index", [entries[0], *entries[2:]], "malformed-oci"),
            (
                "wrong-layout",
                [("oci-layout", b'{"imageLayoutVersion":"9"}'), *entries[1:]],
                "malformed-oci",
            ),
        ):
            with self.subTest(case=case):
                self.scan(make_tar(value), "oci", category=category)

    def test_oci_descriptor_digest_size_and_reference_matrix(self):
        entries = oci_entries([(make_tar([]), LAYER_TYPES["tar"])])
        index = json.loads(entries[1][1])
        for case in ("missing", "wrong-size", "wrong-kind"):
            value = json.loads(json.dumps(index))
            if case == "missing":
                value["manifests"][0]["digest"] = "sha256:" + "a" * 64
            elif case == "wrong-size":
                value["manifests"][0]["size"] += 1
            else:
                value["manifests"][0]["mediaType"] = OCI_INDEX
            with self.subTest(case=case):
                self.scan(
                    make_tar(
                        [entries[0], ("index.json", encoded_json(value)), *entries[2:]]
                    ),
                    "oci",
                    category="malformed-oci",
                )
        corrupted = list(entries)
        name, payload = corrupted[2]
        corrupted[2] = (name, payload.replace(b"amd64", b"arm64"))
        self.scan(make_tar(corrupted), "oci", category="malformed-oci")

    def test_tar_malformed_headers_payload_end_and_trailing_matrix(self):
        valid_record = record("safe", b"safe")
        bad_checksum = bytearray(record("safe"))
        bad_checksum[0] ^= 1
        for name, payload in (
            ("empty-input", b""),
            ("short-header", b"short"),
            ("first-checksum", bytes(bad_checksum) + bytes(1024)),
            ("later-checksum", valid_record + bytes(bad_checksum) + bytes(1024)),
            ("short-payload", record("safe", declared_size=1024) + b"short"),
            ("missing-second-end-block", valid_record + bytes(512)),
            ("nonzero-tail", valid_record + bytes(1024) + b"hidden"),
            ("partial-zero-tail", valid_record + bytes(1025)),
        ):
            with self.subTest(case=name):
                self.scan(payload, category="malformed-tar")

    def test_tar_unsupported_member_types_fail_closed_matrix(self):
        for kind in (b"Z", tarfile.GNUTYPE_SPARSE, tarfile.CONTTYPE):
            with self.subTest(kind=kind):
                self.scan(
                    record("unknown", kind=kind) + bytes(1024),
                    category="unsupported-tar",
                )

    def test_input_exception_never_exposes_native_exception_content(self):
        class BrokenPipe:
            def read(self, size):
                raise OSError("synthetic-private-filename-and-content")

        stdout, stderr = io.StringIO(), io.StringIO()
        self.assertEqual(
            scanner.main(["tar"], stdin=BrokenPipe(), stdout=stdout, stderr=stderr), 1
        )
        self.assertEqual(stdout.getvalue(), scanner.REFUSAL)
        self.assertEqual(stderr.getvalue(), "archive-scan:input-error\n")

    def test_pax_strict_framing_sparse_and_longname_matrix(self):
        safe = record("safe", b"safe") + bytes(1024)
        for name, payload, category in (
            ("short-length", b"4 x=\n", "malformed-pax"),
            ("out-of-range", b"99 path=x\n", "malformed-pax"),
            ("missing-newline", b"9 path=x!", "malformed-pax"),
            ("trailing-garbage", pax_record("path", "safe") + b"junk", "malformed-pax"),
            (
                "duplicate-key",
                pax_record("path", "a") + pax_record("path", "b"),
                "unsupported-tar",
            ),
            ("sparse", pax_record("GNU.sparse.map", "0,4"), "unsupported-tar"),
            ("negative-size", pax_record("size", "-1"), "malformed-pax"),
            (
                "oversized-size",
                pax_record("size", str(scanner.MAX_MEMBER_BYTES + 1)),
                "resource-limit",
            ),
            (
                "forbidden-name",
                pax_record("path", "share-input-a/asset"),
                "forbidden-path",
            ),
        ):
            with self.subTest(case=name):
                self.scan(
                    record("pax", payload, kind=tarfile.XHDTYPE) + safe,
                    category=category,
                )
        self.scan(
            record("long", b"not-terminated", kind=tarfile.GNUTYPE_LONGNAME) + safe,
            category="malformed-tar",
        )

    def test_resource_limits_named_matrix(self):
        cases = (
            (
                "member-count",
                "MAX_MEMBERS",
                1,
                make_tar([("a", b""), ("b", b"")]),
                "tar",
            ),
            ("member-size", "MAX_MEMBER_BYTES", 3, make_tar([("a", b"four")]), "tar"),
            ("name", "MAX_NAME_BYTES", 4, make_tar([("long-name", b"")]), "tar"),
            (
                "pax-size",
                "MAX_PAX_BYTES",
                4,
                record("pax", pax_record("path", "safe"), kind=tarfile.XHDTYPE)
                + record("safe")
                + bytes(1024),
                "tar",
            ),
            (
                "pax-chain",
                "MAX_EXTENSION_HEADERS",
                1,
                record("pax", pax_record("mtime", "1"), kind=tarfile.XGLTYPE) * 2
                + record("safe")
                + bytes(1024),
                "tar",
            ),
            ("json-size", "MAX_JSON_BYTES", 16, make_tar(oci_entries()), "oci"),
            (
                "json-depth",
                "MAX_JSON_DEPTH",
                2,
                make_tar(oci_entries(extras=(b"[[[[[0]]]]]",))),
                "oci",
            ),
            ("blob-count", "MAX_BLOBS", 1, make_tar(oci_entries()), "oci"),
            ("descriptor-count", "MAX_DESCRIPTORS", 1, make_tar(oci_entries()), "oci"),
        )
        for name, setting, limit, payload, mode in cases:
            with self.subTest(case=name), patch.object(scanner, setting, limit):
                self.scan(payload, mode, category="resource-limit")
        # A byte-budget refusal intentionally cannot promise EOF on an
        # adversarially unbounded producer; only a one-byte EOF probe may
        # exceed the cap, not an entire input chunk.
        with patch.object(scanner, "MAX_TOTAL_BYTES", 1024):
            source = Pipe(make_tar([]))
            with self.assertRaisesRegex(scanner.ScanError, "^resource-limit$"):
                scanner.scan_archive(source, "tar")
            self.assertLessEqual(source.source.tell(), 1025)

    def test_cli_fixed_boolean_output_and_no_content_or_filename_diagnostic(self):
        private_name = "synthetic-private-name"
        for mode, payload, expected in (
            ("tar", make_tar([(private_name, b"safe")]), None),
            (
                "tar",
                make_tar([(private_name, scanner.FORBIDDEN_CONTENT)]),
                "forbidden-content",
            ),
            (
                "oci",
                make_tar(oci_entries(extras=(b"\x89synthetic-private-content",))),
                "unsupported-blob",
            ),
        ):
            with self.subTest(mode=mode, expected=expected):
                stdout, stderr = io.StringIO(), io.StringIO()
                source = Pipe(payload)
                code = scanner.main([mode], stdin=source, stdout=stdout, stderr=stderr)
                self.assertEqual(code, int(expected is not None))
                self.assertEqual(
                    stdout.getvalue(), scanner.REFUSAL if expected else scanner.SUCCESS
                )
                self.assertEqual(
                    stderr.getvalue(),
                    "archive-scan:" + expected + "\n" if expected else "",
                )
                self.assertTrue(source.eof)
                self.assertNotIn(private_name, stdout.getvalue() + stderr.getvalue())
        stdout, stderr = io.StringIO(), io.StringIO()
        self.assertEqual(
            scanner.main(
                ["synthetic-private-name"],
                stdin=Pipe(b""),
                stdout=stdout,
                stderr=stderr,
            ),
            1,
        )
        self.assertEqual(stderr.getvalue(), "archive-scan:invalid-mode\n")

    def test_isolated_cli_actual_stdin_and_fixed_exit_status(self):
        for payload, status, output in (
            (make_tar([]), 0, scanner.SUCCESS),
            (make_tar([("asset", scanner.FORBIDDEN_CONTENT)]), 1, scanner.REFUSAL),
        ):
            with self.subTest(status=status):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", str(SCRIPT), "tar"],
                    input=payload,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, status)
                self.assertEqual(result.stdout, output.encode())
                self.assertEqual(
                    result.stderr,
                    b"archive-scan:forbidden-content\n" if status else b"",
                )

    def test_no_disk_staging_extraction_writes_or_input_seeking(self):
        payloads = [
            ("tar", make_tar([("history/native", b"disclosed bytes")])),
            (
                "oci",
                make_tar(
                    oci_entries(
                        [
                            (
                                gzip.compress(
                                    make_tar([("history/native", b"disclosed bytes")])
                                ),
                                LAYER_TYPES["gzip"],
                            ),
                        ]
                    )
                ),
            ),
        ]
        forbidden = (
            "builtins.open",
            "io.open",
            "os.open",
            "os.write",
            "tempfile.TemporaryFile",
            "tempfile.NamedTemporaryFile",
            "tempfile.SpooledTemporaryFile",
            "tempfile.mkdtemp",
            "tempfile.mkstemp",
            "tarfile.TarFile.extract",
            "tarfile.TarFile.extractall",
            "tarfile.TarFile.extractfile",
        )
        for mode, payload in payloads:
            with self.subTest(mode=mode), ExitStack() as stack:
                for target in forbidden:
                    stack.enter_context(
                        patch(target, side_effect=AssertionError("disk-operation"))
                    )
                self.scan(payload, mode)


if __name__ == "__main__":
    unittest.main()
