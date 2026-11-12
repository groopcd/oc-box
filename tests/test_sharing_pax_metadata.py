"""Corrected current-oracle PAX checks; not encrypted-backup/host acceptance.

PAX lengths count raw bytes, as archive/tar does for SCHILY xattrs. In
particular, these fixtures must not UTF-8 encode an already-binary value.
Only valid SCHILY.xattr.<nonempty suffix> fields have opaque binary values;
stdlib tarfile independently establishes that the fixtures preserve bytes.
Raw bounded extension payloads remain subject to the forbidden-content check.
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


def marker_extensions():
    """Markers in raw metadata, not ordinary regular-file contents."""
    marker = scanner.FORBIDDEN_CONTENT
    cases = (
        ("binary-xattr", (b"SCHILY.xattr.user.marker", b"\x00\xff" + marker + b"\x00")),
        ("text-xattr", (b"SCHILY.xattr.user.marker", marker)),
        ("unknown-text", (b"vendor.note", b"prefix-" + marker + b"-suffix")),
        ("unknown-key", (b"vendor." + marker, b"ordinary text")),
        ("xattr-key", (b"SCHILY.xattr.user." + marker, CAPABILITY)),
        ("semantic-path", (b"path", b"root/" + marker + b"/asset")),
        ("semantic-linkpath", (b"linkpath", b"root/" + marker + b"/asset")),
    )
    for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
        for name, field in cases:
            yield (name, kind), extension((field,), kind=kind)
    for kind in (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
        yield (
            ("gnu-long-payload", kind),
            record("gnu-metadata", b"root/" + marker + b"/asset\x00", kind=kind),
        )


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

    def test_binary_xattr_local_global_and_solaris_success_matrix(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            for key, value in XATTRS:
                with self.subTest(kind=kind, key=key):
                    self.scan(pax_tar(((key, value),), kind=kind))
            with self.subTest(kind=kind, case="combined"):
                self.scan(pax_tar(XATTRS, kind=kind))

    def test_binary_xattrs_remain_lossless_mapping_entries(self):
        fields = (
            *TEXT_XATTRS,
            *XATTRS,
            ("SCHILY.xattr.user.\N{SNOWMAN}".encode("utf-8"), bytes(range(256))),
            (b"path", b"root/safe"),
            (b"uid", b"1000"),
        )
        parsed = scanner.pax_fields(b"".join(raw_pax(*field) for field in fields))
        self.assertIsInstance(parsed, dict)
        self.assertEqual(len(parsed), len(fields))
        self.assertEqual(list(parsed), [key.decode("utf-8") for key, _ in fields])
        for key, value in fields:
            with self.subTest(key=key):
                self.assertEqual(
                    parsed[key.decode("utf-8")].encode("utf-8", "surrogateescape"),
                    value,
                )

    def test_nonempty_unicode_xattr_suffix_and_empty_values_are_valid(self):
        key = "SCHILY.xattr.user.\N{SNOWMAN}".encode("utf-8")
        for value in (
            b"",
            CAPABILITY,
            IMA,
            b"\x00\xff\n=\r\x80\x00",
            bytes(range(256)),
        ):
            with self.subTest(value=value):
                self.scan(pax_tar(((key, value),)))

    def test_text_xattr_local_global_and_solaris_success_matrix(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            for key, value in TEXT_XATTRS:
                with self.subTest(kind=kind, key=key):
                    self.scan(pax_tar(((key, value),), kind=kind))

    def test_text_and_binary_xattrs_coexist_with_strict_text_semantics(self):
        semantics = (
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
        for metadata in (TEXT_XATTRS, XATTRS):
            with self.subTest(metadata=metadata):
                self.scan(
                    pax_tar((*metadata, *semantics), body=record("raw-safe", b"safe"))
                )

    def test_text_and_binary_xattrs_preserve_effective_size_and_stream_alignment(self):
        # The PAX size, not the raw header's zero size, owns these four bytes.
        body = record("raw-safe", b"safe", declared_size=0) + record("after", b"ok")
        for metadata in (TEXT_XATTRS, XATTRS):
            with self.subTest(metadata=metadata):
                self.scan(pax_tar((*metadata, (b"size", b"4")), body=body))

    def test_binary_metadata_content_check_in_every_field_position(self):
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
                        self.scan(
                            pax_tar(fields),
                            category="forbidden-content" if marker in value else None,
                        )

    def test_binary_metadata_marker_with_short_reader_chunks(self):
        for offset in (0, 1, 16, 17, 18):
            fields = (
                *TEXT_XATTRS,
                (
                    b"SCHILY.xattr.user.marker",
                    b"\x00\xff" + b"x" * offset + scanner.FORBIDDEN_CONTENT + b"\x00",
                ),
            )
            with self.subTest(offset=offset), patch.object(scanner, "CHUNK_BYTES", 17):
                self.scan(pax_tar(fields), category="forbidden-content", chunk=13)

    def test_raw_text_unknown_key_and_gnu_extension_payloads_are_scanned(self):
        for case, metadata in marker_extensions():
            payload = metadata + record("root/safe", b"ordinary history") + bytes(1024)
            with self.subTest(case=case), patch.object(scanner, "CHUNK_BYTES", 17):
                self.scan(payload, category="forbidden-content", chunk=13)

    def test_raw_contiguous_marker_across_pax_record_boundary_is_not_lost(self):
        first = raw_pax(b"vendor.first", b"prefix-tail")
        second = raw_pax(b"vendor.second", b"head-suffix")
        raw = first + second
        # The production marker contains no framing bytes and cannot itself
        # cross a PAX record boundary. A synthetic marker that includes the
        # literal newline, length and next key proves raw contiguous matching.
        marker = first[-5:] + second[: second.index(b"=") + 1] + b"head"
        self.assertIn(marker, raw)
        self.assertNotIn(marker, b"prefix-tailhead-suffix")
        payload = (
            record("pax", raw, kind=tarfile.XHDTYPE) + record("safe") + bytes(1024)
        )
        with (
            patch.object(scanner, "FORBIDDEN_CONTENT", marker),
            patch.object(scanner, "CHUNK_BYTES", 7),
        ):
            self.scan(payload, category="forbidden-content", chunk=5)

    def test_metadata_marker_is_recorded_before_strict_extension_validation(self):
        marker = raw_pax(
            b"SCHILY.xattr.user.marker", b"\x00\xff" + scanner.FORBIDDEN_CONTENT
        )
        cases = (
            ("bad-framing", marker + b"junk", tarfile.XHDTYPE, "malformed-pax"),
            (
                "strict-unknown-value",
                marker + raw_pax(b"vendor.note", b"\xff"),
                tarfile.XHDTYPE,
                "malformed-pax",
            ),
            (
                "duplicate-binary-key",
                marker + raw_pax(b"SCHILY.xattr.user.marker", CAPABILITY),
                tarfile.XHDTYPE,
                "unsupported-tar",
            ),
            (
                "strict-numeric",
                marker + raw_pax(b"size", b"-1"),
                tarfile.XHDTYPE,
                "malformed-pax",
            ),
            (
                "sparse",
                marker + raw_pax(b"GNU.sparse.map", b"0,4"),
                tarfile.XHDTYPE,
                "unsupported-tar",
            ),
            (
                "gnu-interior-nul",
                scanner.FORBIDDEN_CONTENT + b"\x00hidden\x00",
                tarfile.GNUTYPE_LONGNAME,
                "malformed-tar",
            ),
        )
        for name, raw, kind, category in cases:
            state = scanner.State()
            payload = record("metadata", raw, kind=kind) + record("safe") + bytes(1024)
            with (
                self.subTest(case=name),
                patch.object(scanner, "State", return_value=state),
            ):
                self.scan(payload, category=category)
                self.assertEqual(state.violation, "forbidden-content")
        state = scanner.State()
        bad_padding = bytearray(record("pax", marker, kind=tarfile.XHDTYPE))
        bad_padding[512 + len(marker)] = 1
        with patch.object(scanner, "State", return_value=state):
            self.scan(
                bytes(bad_padding) + record("safe") + bytes(1024),
                category="malformed-pax",
            )
            self.assertEqual(state.violation, "forbidden-content")

    def test_accepted_binary_metadata_does_not_hide_later_record_refusals(self):
        prefix = extension(XATTRS) + record("root/first", b"ordinary history")
        bad_header = bytearray(record("root/later"))
        bad_header[0] ^= 1  # Retain the old checksum, making this header invalid.
        cases = (
            ("malformed-tar", bytes(bad_header), "malformed-tar"),
            (
                "malformed-pax",
                record("later-pax", b"junk", kind=tarfile.XHDTYPE) + record("later"),
                "malformed-pax",
            ),
            (
                "ordinary-binary-value",
                extension(((b"comment", b"\xff"),)) + record("later"),
                "malformed-pax",
            ),
            (
                "duplicate-binary-key",
                extension((XATTRS[0], XATTRS[0])) + record("later"),
                "unsupported-tar",
            ),
            ("raw-path", record("root/share-input-a/later"), "forbidden-path"),
            (
                "pax-path",
                extension(((b"path", b"root/share-input-b/later"),)) + record("later"),
                "forbidden-path",
            ),
            (
                "regular-content",
                record("root/later", scanner.FORBIDDEN_CONTENT),
                "forbidden-content",
            ),
            (
                "metadata-content",
                extension(((b"vendor.note", scanner.FORBIDDEN_CONTENT),))
                + record("later"),
                "forbidden-content",
            ),
        )
        for name, later, category in cases:
            with self.subTest(case=name):
                self.scan(prefix + later + bytes(1024), category=category)

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

    def test_noncontiguous_binary_unknown_and_extension_values_do_not_form_markers(
        self,
    ):
        marker = scanner.FORBIDDEN_CONTENT
        cases = (
            pax_tar(
                (
                    (b"SCHILY.xattr.user.first", b"\x00\xff" + marker[:8]),
                    (b"SCHILY.xattr.user.last", marker[8:] + b"\x00\x80"),
                )
            ),
            pax_tar(((b"vendor.first", marker[:8]), (b"vendor.last", marker[8:]))),
            pax_tar(((b"vendor." + marker[:8], marker[8:]),)),
            extension(((b"vendor.first", marker[:8]),))
            + extension(((b"vendor.last", marker[8:]),))
            + record("safe")
            + bytes(1024),
        )
        for index, payload in enumerate(cases):
            with self.subTest(case=index), patch.object(scanner, "CHUNK_BYTES", 17):
                self.assertNotIn(marker, payload)
                self.scan(payload, chunk=13)

    def test_ordinary_or_similarly_named_fields_cannot_carry_binary_values(self):
        for key in (
            b"comment",
            b"uname",
            b"gname",
            b"vendor.binary",
            b"LIBARCHIVE.xattr.user.binary",
            b"SCHILY.xattr",
            b"SCHILY.xattr.",
            b"schily.xattr.user.binary",
            b"Schily.xattr.user.binary",
            b"SCHILY.xattrx.user.binary",
        ):
            for value in (b"a\x00b", b"\xff\x80"):
                for metadata in (TEXT_XATTRS, XATTRS):
                    with self.subTest(key=key, value=value, metadata=metadata):
                        self.scan(
                            pax_tar((*metadata, (key, value))), category="malformed-pax"
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
                for metadata in (TEXT_XATTRS, XATTRS):
                    with self.subTest(key=key, value=value, metadata=metadata):
                        self.scan(
                            pax_tar((*metadata, (key, value))), category="malformed-pax"
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
            for metadata in (TEXT_XATTRS, XATTRS):
                with self.subTest(key=key, value=value, metadata=metadata):
                    self.scan(pax_tar((*metadata, (key, value))), category=category)

    def test_pax_keys_remain_nonempty_utf8_without_nuls_or_linebreaks(self):
        for key in (
            b"",
            b"SCHILY.xattr.user.\xff",
            b"SCHILY.xattr.user.\x00hidden",
            b"SCHILY.xattr.user.\nhidden",
            b"SCHILY.xattr.user.\rhidden",
            b"\xff",
            b"comment\x00path",
            b"comment\npath",
            b"comment\rpath",
        ):
            for value in (b"safe", CAPABILITY, IMA):
                with self.subTest(key=key, value=value):
                    self.scan(pax_tar(((key, value),)), category="malformed-pax")

    def test_empty_xattr_suffix_binary_values_are_refused(self):
        # A bare prefix is not an opaque-value exemption.
        for value in (CAPABILITY, IMA):
            with self.subTest(value=value):
                self.scan(
                    pax_tar(((b"SCHILY.xattr.", value),)), category="malformed-pax"
                )

    def test_equals_delimits_the_key_before_binary_value_exemption(self):
        # '=' ends the actual key: an apparent later suffix cannot make the
        # empty-suffix or ordinary key before it eligible for binary values.
        for key in (b"SCHILY.xattr.=user.hidden", b"comment=SCHILY.xattr.user.hidden"):
            for value in (CAPABILITY, IMA):
                with self.subTest(key=key, value=value):
                    self.scan(pax_tar(((key, value),)), category="malformed-pax")

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

    def test_duplicate_binary_xattr_keys_are_not_skipped(self):
        for key, first, second in (
            (b"SCHILY.xattr.security.capability", CAPABILITY, CAPABILITY),
            (b"SCHILY.xattr.security.ima", IMA, IMA),
            (b"SCHILY.xattr.user.binary", CAPABILITY, IMA),
            (b"SCHILY.xattr.user.binary", b"text", CAPABILITY),
            (b"SCHILY.xattr.user.binary", IMA, b"text"),
            (b"SCHILY.xattr.user.binary", b"", CAPABILITY),
        ):
            for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
                with self.subTest(key=key, first=first, second=second, kind=kind):
                    self.scan(
                        pax_tar(((key, first), (key, second)), kind=kind),
                        category="unsupported-tar",
                    )

    def test_text_and_binary_pax_records_with_malformed_framing_are_refused(self):
        # Both valid text and binary controls must reach the framing checks.
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

    def test_forbidden_pax_paths_and_links_with_text_and_binary_metadata(self):
        for component in sorted(scanner.FORBIDDEN_COMPONENTS):
            forbidden = ("root/" + component + "/asset").encode()
            for key in (b"path", b"linkpath"):
                for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
                    for metadata in (TEXT_XATTRS, XATTRS):
                        with self.subTest(
                            component=component, key=key, kind=kind, metadata=metadata
                        ):
                            self.scan(
                                pax_tar((*metadata, (key, forbidden)), kind=kind),
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

    def test_global_text_and_binary_metadata_preserves_local_override_lifetimes(self):
        for metadata in (TEXT_XATTRS, XATTRS):
            payload = (
                extension(metadata, kind=tarfile.XGLTYPE)
                + extension(((b"path", b"root/first"),))
                + record("safe-one", b"one")
                + extension(((b"path", b"root/second"),))
                + record("safe-two", b"two")
                + record("root/share-input-a/third", b"three")
                + bytes(1024)
            )
            with self.subTest(metadata=metadata):
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

    def test_binary_metadata_field_and_byte_budgets_accept_exact_caps(self):
        fields = tuple(
            (f"SCHILY.xattr.user.field-{index}".encode(), b"\x00\xff")
            for index in range(scanner.MAX_PAX_FIELDS + 1)
        )
        self.scan(pax_tar(fields[:-1]))
        self.scan(pax_tar(fields), category="resource-limit")
        combined = extension(fields[:1], kind=tarfile.XGLTYPE)
        self.scan(combined + extension(fields[1:-1]) + record("safe") + bytes(1024))
        self.scan(
            combined + extension(fields[1:]) + record("safe") + bytes(1024),
            category="resource-limit",
        )

        key = b"SCHILY.xattr.user.large"
        cap = scanner.MAX_PAX_BYTES
        framing_bytes = len(str(cap)) + 1 + len(key) + 1 + 1
        value = b"\x00\xff" + b"a" * (cap - framing_bytes - 2)
        self.assertEqual(len(raw_pax(key, value)), cap)
        self.scan(pax_tar(((key, value),)))
        self.scan(pax_tar(((key, value + b"a"),)), category="resource-limit")
        small_field = XATTRS[0]
        field_bytes = len(raw_pax(*small_field))
        with patch.object(scanner, "MAX_PAX_BYTES", field_bytes):
            self.scan(pax_tar((small_field,)))
        with patch.object(scanner, "MAX_PAX_BYTES", field_bytes - 1):
            self.scan(pax_tar((small_field,)), category="resource-limit")

    def test_oci_binary_layer_success_all_compressions_and_reference_modes(
        self,
    ):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            layer = pax_tar(XATTRS, kind=kind)
            for compression, encode in ENCODE.items():
                for referenced in (True, False):
                    payload = encode(layer)
                    entries = (
                        oci_entries([(payload, LAYER_TYPES[compression])], nested=True)
                        if referenced
                        else oci_entries(extras=(payload,), nested=True)
                    )
                    with self.subTest(
                        kind=kind, compression=compression, referenced=referenced
                    ):
                        self.scan(make_tar(entries), "oci")

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

    def test_oci_raw_metadata_marker_all_compressions_and_reference_modes(self):
        for case, metadata in marker_extensions():
            layer = metadata + record("root/safe", b"ordinary history") + bytes(1024)
            for compression, encode in ENCODE.items():
                for referenced in (True, False):
                    blob = encode(layer)
                    entries = (
                        oci_entries([(blob, LAYER_TYPES[compression])], nested=True)
                        if referenced
                        else oci_entries(extras=(blob,), nested=True)
                    )
                    with (
                        self.subTest(
                            case=case, compression=compression, referenced=referenced
                        ),
                        patch.object(scanner, "CHUNK_BYTES", 17),
                    ):
                        self.scan(
                            make_tar(entries),
                            "oci",
                            category="forbidden-content",
                            chunk=13,
                        )

    def test_oci_outer_text_and_binary_metadata_success(self):
        entries = oci_entries([(pax_tar(), LAYER_TYPES["tar"])])
        body = b"".join(record(name, value) for name, value in entries)
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE):
            with self.subTest(kind=kind, case="safe"):
                self.scan(pax_tar(kind=kind, body=body), "oci")
            with self.subTest(kind=kind, case="binary"):
                self.scan(
                    pax_tar(XATTRS, kind=kind, body=body),
                    "oci",
                )

    def test_oci_outer_raw_metadata_markers_are_scanned(self):
        entries = oci_entries([(pax_tar(XATTRS), LAYER_TYPES["tar"])])
        body = b"".join(record(name, value) for name, value in entries)
        for case, metadata in marker_extensions():
            name, kind = case
            # Outer paths must remain valid OCI entry names; path overrides
            # are already exercised in the nested-layer matrix above.
            if name == "semantic-path" or kind == tarfile.GNUTYPE_LONGNAME:
                continue
            with self.subTest(case=case):
                self.scan(
                    metadata + body + bytes(1024), "oci", category="forbidden-content"
                )

    def test_cli_binary_metadata_diagnostics_are_fixed_and_non_sensitive(self):
        private_name = b"synthetic-private-xattr-name"
        for value, category in (
            (b"safe text", None),
            (CAPABILITY, None),
            (IMA, None),
            (b"\x00\xff" + scanner.FORBIDDEN_CONTENT + b"\x00", "forbidden-content"),
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
