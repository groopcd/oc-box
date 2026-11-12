"""Current-oracle PAX baseline; not a diagnosis or host acceptance.

PAX lengths count raw bytes, as archive/tar does for SCHILY xattrs. In
particular, these fixtures must not UTF-8 encode an already-binary value.
The unchanged strict scanner refuses valid binary xattrs as malformed-pax;
stdlib tarfile independently establishes that the fixtures preserve bytes.
"""

import io
import tarfile
import unittest
from unittest.mock import patch

from tools import sharing_archive_scan as scanner
from test_sharing_archive_scan import (
    ENCODE,
    LAYER_TYPES,
    Pipe,
    make_tar,
    oci_entries,
    record,
)


# Linux revision-2 vfs_cap_data: effective flag and CAP_NET_BIND_SERVICE.
# This is a real 20-byte shape, not text containing escaped NUL characters.
CAPABILITY = bytes.fromhex("0100000200040000000000000000000000000000")
IMA = b"\x03\x02\xff\x80synthetic-ima-digest\xfe"
XATTRS = (
    (b"SCHILY.xattr.security.capability", CAPABILITY),
    (b"SCHILY.xattr.security.ima", IMA),
    (b"SCHILY.xattr.user.binary", b"\x00\xff\n=\x80\x00"),
)
TEXT_XATTRS = (
    (b"SCHILY.xattr.user.note", b"ordinary text metadata"),
    (b"SCHILY.xattr.user.second", b"more text"),
    (b"SCHILY.xattr.user.empty", b""),
)


def raw_pax(key, value):
    """Frame bytes without decoding, escaping, or normalizing their contents."""
    body = key + b"=" + value + b"\n"
    length = len(body) + 2
    while True:
        actual = len(str(length)) + 1 + len(body)
        if actual == length:
            return str(length).encode("ascii") + b" " + body
        length = actual


def extension(fields, *, kind=tarfile.XHDTYPE, name="pax-metadata"):
    return record(name, b"".join(raw_pax(*field) for field in fields), kind=kind)


def pax_tar(fields=TEXT_XATTRS, *, kind=tarfile.XHDTYPE, body=None):
    if body is None:
        body = record("root/safe", b"intentionally disclosed history")
    return extension(fields, kind=kind) + body + bytes(1024)


class SharingPaxMetadataTests(unittest.TestCase):
    def scan(self, payload, mode="tar", *, category=None, chunk=733):
        source = Pipe(payload, chunk=chunk)
        if category is None:
            self.assertIsNone(scanner.scan_archive(source, mode))
        else:
            with self.assertRaises(scanner.ScanError) as caught:
                scanner.scan_archive(source, mode)
            self.assertEqual(str(caught.exception), category)
        self.assertTrue(source.eof, "refusal must still consume producer EOF")
        self.assertLessEqual(source.maximum_requested, scanner.CHUNK_BYTES)

    def test_stdlib_independently_recovers_valid_binary_pax_bytes(self):
        self.assertEqual(len(CAPABILITY), 20)
        self.assertIn(b"\x00", CAPABILITY)
        self.assertIn(b"\xff", IMA)
        self.assertNotIn(b"\x00", IMA)
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            payload = pax_tar(XATTRS, kind=kind)
            with (
                self.subTest(kind=kind),
                tarfile.open(
                    fileobj=io.BytesIO(payload),
                    mode="r|",
                    encoding="utf-8",
                    errors="surrogateescape",
                ) as archive,
            ):
                member = archive.next()
                self.assertEqual(member.name, "root/safe")
                self.assertEqual(member.size, len(b"intentionally disclosed history"))
                for key, value in XATTRS:
                    self.assertEqual(
                        member.pax_headers[key.decode("utf-8")].encode(
                            "utf-8", "surrogateescape"
                        ),
                        value,
                    )
                self.assertIsNone(archive.next())

    def test_current_binary_xattr_local_global_and_solaris_refusal_matrix(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            for key, value in XATTRS:
                with self.subTest(kind=kind, key=key):
                    self.scan(
                        pax_tar(((key, value),), kind=kind), category="malformed-pax"
                    )

    def test_text_xattr_local_global_and_solaris_success_matrix(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            for key, value in TEXT_XATTRS:
                with self.subTest(kind=kind, key=key):
                    self.scan(pax_tar(((key, value),), kind=kind))

    def test_text_xattrs_coexist_with_strict_text_semantics(self):
        fields = (
            *TEXT_XATTRS,
            (b"path", "root/renamed-\N{LATIN SMALL LETTER E WITH ACUTE}".encode()),
            (b"size", b"4"),
            (b"uid", b"1000"),
            (b"gid", b"1000"),
            (b"mtime", b"1.25"),
            (b"atime", b"2.5"),
            (b"ctime", b"3"),
            (b"hdrcharset", b"ISO-IR 10646 2000 UTF-8"),
            (b"comment", "ordinary UTF-8 \N{SNOWMAN}".encode()),
        )
        self.scan(pax_tar(fields, body=record("raw-safe", b"safe")))

    def test_text_xattrs_do_not_change_effective_size_or_stream_alignment(self):
        # The PAX size, not the raw header's zero size, owns these four bytes.
        body = record("raw-safe", b"safe", declared_size=0) + record("after", b"ok")
        self.scan(pax_tar((*TEXT_XATTRS, (b"size", b"4")), body=body))

    def test_current_binary_metadata_refusal_in_every_field_position(self):
        marker = scanner.FORBIDDEN_CONTENT
        for key in (
            b"SCHILY.xattr.security.capability",
            b"SCHILY.xattr.security.ima",
            b"SCHILY.xattr.user.binary",
        ):
            values = (b"\x00\xff" + marker + b"\x00\x80", CAPABILITY, IMA)
            for value in values:
                for position in (0, 1, 3):
                    fields = list(TEXT_XATTRS)
                    fields.insert(position, (key, value))
                    with self.subTest(key=key, value=value, position=position):
                        self.scan(pax_tar(fields), category="malformed-pax")

    def test_current_binary_metadata_refusal_with_short_reader_chunks(self):
        for offset in (0, 1, 16, 17, 18):
            fields = (
                *TEXT_XATTRS,
                (
                    b"SCHILY.xattr.user.marker",
                    b"\x00\xff" + b"x" * offset + scanner.FORBIDDEN_CONTENT + b"\x00",
                ),
            )
            with self.subTest(offset=offset), patch.object(scanner, "CHUNK_BYTES", 17):
                self.scan(pax_tar(fields), category="malformed-pax", chunk=13)

    def test_text_metadata_preserves_regular_member_content_checks(self):
        for offset in (0, 1, 16, 17, 18):
            with self.subTest(offset=offset), patch.object(scanner, "CHUNK_BYTES", 17):
                self.scan(
                    pax_tar(
                        body=record(
                            "root/safe", b"x" * offset + scanner.FORBIDDEN_CONTENT
                        )
                    ),
                    category="forbidden-content",
                    chunk=13,
                )

    def test_distinct_text_fields_do_not_manufacture_a_content_marker(self):
        marker = scanner.FORBIDDEN_CONTENT
        # Framing remains between fields. Concatenating decoded field values
        # would invent bytes that are not present in the archive.
        fields = (
            *TEXT_XATTRS,
            (b"SCHILY.xattr.user.first", marker[:8]),
            (b"SCHILY.xattr.user.last", marker[8:]),
        )
        self.scan(pax_tar(fields))

    def test_ordinary_or_similarly_named_fields_cannot_carry_binary_values(self):
        for key in (
            b"comment",
            b"uname",
            b"gname",
            b"vendor.binary",
            b"LIBARCHIVE.xattr.user.binary",
            b"SCHILY.xattr",
            b"schily.xattr.user.binary",
            b"SCHILY.xattrx.user.binary",
        ):
            for value in (b"a\x00b", b"\xff\x80"):
                with self.subTest(key=key, value=value):
                    self.scan(
                        pax_tar((*TEXT_XATTRS, (key, value))), category="malformed-pax"
                    )

    def test_semantic_fields_remain_utf8_without_nuls(self):
        for key in (
            b"path",
            b"linkpath",
            b"size",
            b"uid",
            b"gid",
            b"mtime",
            b"atime",
            b"ctime",
            b"hdrcharset",
        ):
            for value in (b"safe\x00ignored", b"\xff"):
                with self.subTest(key=key, value=value):
                    self.scan(
                        pax_tar((*TEXT_XATTRS, (key, value))), category="malformed-pax"
                    )

    def test_semantic_numbers_and_charset_keep_their_existing_validation(self):
        for key, value, category in (
            (b"size", b"-1", "malformed-pax"),
            (b"size", b"1.5", "malformed-pax"),
            (b"size", str(scanner.MAX_MEMBER_BYTES + 1).encode(), "resource-limit"),
            (b"uid", b"-1", "malformed-pax"),
            (b"gid", b"1.5", "malformed-pax"),
            (b"mtime", b"NaN", "malformed-pax"),
            (b"atime", b"inf", "malformed-pax"),
            (b"ctime", b"not-a-time", "malformed-pax"),
            (b"hdrcharset", b"BINARY", "unsupported-tar"),
            (b"GNU.sparse.map", b"0,4", "unsupported-tar"),
            (b"SCHILY.realsize", b"4", "unsupported-tar"),
        ):
            with self.subTest(key=key, value=value):
                self.scan(pax_tar((*TEXT_XATTRS, (key, value))), category=category)

    def test_pax_keys_remain_nonempty_utf8_without_nuls(self):
        for key in (
            b"",
            b"SCHILY.xattr.user.\xff",
            b"SCHILY.xattr.user.\x00hidden",
            b"\xff",
            b"comment\x00path",
        ):
            for value in (b"safe", CAPABILITY):
                with self.subTest(key=key, value=value):
                    self.scan(pax_tar(((key, value),)), category="malformed-pax")

    def test_current_empty_xattr_suffix_binary_values_are_refused(self):
        # An empty suffix has no special validation in the current scanner;
        # this records only its existing binary-value refusal.
        for value in (CAPABILITY, IMA):
            with self.subTest(value=value):
                self.scan(
                    pax_tar(((b"SCHILY.xattr.", value),)), category="malformed-pax"
                )

    def test_duplicate_text_xattr_and_semantic_keys_are_refused(self):
        for key, first, second in (
            (b"SCHILY.xattr.user.note", b"first", b"second"),
            (b"SCHILY.xattr.user.empty", b"", b"later"),
            (b"path", b"root/first", b"root/second"),
            (b"size", b"4", b"4"),
        ):
            with self.subTest(key=key):
                self.scan(
                    pax_tar(((key, first), (key, second))), category="unsupported-tar"
                )

    def test_text_and_binary_pax_records_with_malformed_framing_are_refused(self):
        # Text controls ensure binary-value rejection cannot hide a framing
        # regression while this file still records the original strict oracle.
        for field in (TEXT_XATTRS[0], XATTRS[0]):
            valid = raw_pax(*field)
            for name, payload in (
                ("missing-newline", valid[:-1] + b"!"),
                ("missing-equals", valid.replace(b"=", b":", 1)),
                ("out-of-range", b"9999 " + valid.split(b" ", 1)[1]),
                ("short-length", b"4 x=\n"),
                ("nondigit-length", b"x " + valid.split(b" ", 1)[1]),
                ("trailing-garbage", valid + b"junk"),
                ("trailing-nul", valid + b"\x00"),
            ):
                with self.subTest(field=field[0], case=name):
                    payload = record("pax", payload, kind=tarfile.XHDTYPE)
                    self.scan(
                        payload + record("safe") + bytes(1024), category="malformed-pax"
                    )
            # Padding is framing, not an opaque part of an xattr value.
            payload = bytearray(record("pax", valid, kind=tarfile.XHDTYPE))
            payload[512 + len(valid)] = 1
            with self.subTest(field=field[0], case="nonzero-padding"):
                self.scan(
                    bytes(payload) + record("safe") + bytes(1024),
                    category="malformed-pax",
                )

    def test_forbidden_pax_paths_and_links_with_text_metadata(self):
        for component in sorted(scanner.FORBIDDEN_COMPONENTS):
            forbidden = ("root/" + component + "/asset").encode()
            for key in (b"path", b"linkpath"):
                for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE):
                    with self.subTest(component=component, key=key, kind=kind):
                        self.scan(
                            pax_tar((*TEXT_XATTRS, (key, forbidden)), kind=kind),
                            category="forbidden-path",
                        )

    def test_overridden_local_global_and_multiple_extension_paths_are_checked(self):
        forbidden = b"root/share-input-a/asset"
        safe = b"root/safe"
        for key in (b"path", b"linkpath"):
            for kinds in (
                (tarfile.XGLTYPE, tarfile.XHDTYPE),
                (tarfile.XHDTYPE, tarfile.XGLTYPE),
                (tarfile.XHDTYPE, tarfile.XHDTYPE),
                (tarfile.XGLTYPE, tarfile.XGLTYPE),
            ):
                for bad_first in (True, False):
                    values = (forbidden, safe) if bad_first else (safe, forbidden)
                    payload = b"".join(
                        extension((*TEXT_XATTRS, (key, value)), kind=kind)
                        for kind, value in zip(kinds, values)
                    )
                    with self.subTest(key=key, kinds=kinds, bad_first=bad_first):
                        self.scan(
                            payload + record("safe") + bytes(1024),
                            category="forbidden-path",
                        )

    def test_raw_member_extension_names_and_link_targets_are_still_checked(self):
        forbidden = "root/share-input-b/asset"
        cases = {
            "raw-member-name": extension(TEXT_XATTRS) + record(forbidden),
            "raw-extension-name": extension(TEXT_XATTRS, name=forbidden)
            + record("safe"),
        }
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            member = tarfile.TarInfo("raw-safe-link")
            member.type = kind
            member.linkname = forbidden
            cases["raw-link-" + kind.decode()] = extension(
                (*TEXT_XATTRS, (b"linkpath", b"safe-target"))
            ) + member.tobuf(format=tarfile.USTAR_FORMAT)
        # A harmless PAX override must not erase the raw member's bad name.
        cases["overridden-raw-name"] = extension(
            (*TEXT_XATTRS, (b"path", b"safe"))
        ) + record(forbidden)
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.scan(payload + bytes(1024), category="forbidden-path")

    def test_global_text_metadata_persists_without_consuming_local_overrides(self):
        payload = (
            extension(TEXT_XATTRS, kind=tarfile.XGLTYPE)
            + extension(((b"path", b"root/first"),))
            + record("safe-one", b"one")
            + extension(((b"path", b"root/second"),))
            + record("safe-two", b"two")
            + record("root/share-input-a/third", b"three")
            + bytes(1024)
        )
        self.scan(payload, category="forbidden-path")

    def test_text_metadata_field_and_byte_budgets_are_enforced(self):
        fields = tuple(
            (f"SCHILY.xattr.user.field-{index}".encode(), b"safe")
            for index in range(scanner.MAX_PAX_FIELDS + 1)
        )
        self.scan(pax_tar(fields), category="resource-limit")
        self.scan(
            pax_tar(((b"SCHILY.xattr.user.large", b"a" * scanner.MAX_PAX_BYTES),)),
            category="resource-limit",
        )
        payload = pax_tar((TEXT_XATTRS[0],))
        field_bytes = len(raw_pax(*TEXT_XATTRS[0]))
        with patch.object(scanner, "MAX_PAX_BYTES", field_bytes):
            self.scan(payload)
        with patch.object(scanner, "MAX_PAX_BYTES", field_bytes - 1):
            self.scan(payload, category="resource-limit")
        combined = (
            extension((TEXT_XATTRS[0],), kind=tarfile.XGLTYPE)
            + extension((TEXT_XATTRS[1], TEXT_XATTRS[2]))
            + record("safe")
            + bytes(1024)
        )
        with patch.object(scanner, "MAX_PAX_FIELDS", 2):
            self.scan(combined, category="resource-limit")
        chained = extension((TEXT_XATTRS[0],)) * 3 + record("safe") + bytes(1024)
        with patch.object(scanner, "MAX_EXTENSION_HEADERS", 2):
            self.scan(chained, category="resource-limit")

    def test_current_oci_binary_layer_refusal_all_compressions_and_reference_modes(
        self,
    ):
        layer = pax_tar(XATTRS)
        for compression, encode in ENCODE.items():
            for referenced in (True, False):
                payload = encode(layer)
                entries = (
                    oci_entries([(payload, LAYER_TYPES[compression])], nested=True)
                    if referenced
                    else oci_entries(extras=(payload,), nested=True)
                )
                with self.subTest(compression=compression, referenced=referenced):
                    self.scan(make_tar(entries), "oci", category="malformed-pax")

    def test_oci_text_metadata_preserves_layer_path_and_content_checks(self):
        bad_layers = (
            (
                pax_tar((*TEXT_XATTRS, (b"path", b"root/share-input-a/asset"))),
                "forbidden-path",
            ),
            (
                pax_tar(body=record("root/safe", scanner.FORBIDDEN_CONTENT)),
                "forbidden-content",
            ),
        )
        for compression, encode in ENCODE.items():
            for layer, category in bad_layers:
                for referenced in (True, False):
                    payload = encode(layer)
                    entries = (
                        oci_entries([(payload, LAYER_TYPES[compression])])
                        if referenced
                        else oci_entries(extras=(payload,))
                    )
                    with self.subTest(
                        compression=compression,
                        category=category,
                        referenced=referenced,
                    ):
                        self.scan(make_tar(entries), "oci", category=category)

    def test_oci_outer_text_and_binary_metadata_current_oracle(self):
        entries = oci_entries([(pax_tar(), LAYER_TYPES["tar"])])
        body = b"".join(record(name, value) for name, value in entries)
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE):
            with self.subTest(kind=kind, case="safe"):
                self.scan(pax_tar(kind=kind, body=body), "oci")
            with self.subTest(kind=kind, case="binary"):
                self.scan(
                    pax_tar(XATTRS, kind=kind, body=body),
                    "oci",
                    category="malformed-pax",
                )

    def test_cli_current_binary_metadata_diagnostics_are_fixed_and_non_sensitive(self):
        private_name = b"synthetic-private-xattr-name"
        for value, category in (
            (b"safe text", None),
            (CAPABILITY, "malformed-pax"),
            (IMA, "malformed-pax"),
            (b"\x00\xff" + scanner.FORBIDDEN_CONTENT + b"\x00", "malformed-pax"),
        ):
            payload = pax_tar(((b"SCHILY.xattr.user." + private_name, value),))
            stdout, stderr = io.StringIO(), io.StringIO()
            source = Pipe(payload)
            with self.subTest(category=category):
                self.assertEqual(
                    scanner.main(["tar"], stdin=source, stdout=stdout, stderr=stderr),
                    int(category is not None),
                )
                self.assertEqual(
                    stdout.getvalue(), scanner.REFUSAL if category else scanner.SUCCESS
                )
                self.assertEqual(
                    stderr.getvalue(),
                    "archive-scan:" + category + "\n" if category else "",
                )
                self.assertTrue(source.eof)
                self.assertNotIn(
                    private_name.decode(), stdout.getvalue() + stderr.getvalue()
                )


if __name__ == "__main__":
    unittest.main()
