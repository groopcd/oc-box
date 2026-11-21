"""Offline PNG fixtures; all payloads are generated using the stdlib."""

import random
import struct
import unittest
import zlib
from unittest.mock import patch

from oc_box import file_preview_png as png

SIGNATURE = b"\x89PNG\r\n\x1a\n"
OPAQUE_PIXEL = bytes.fromhex("123456ff")
RGBA_ROW = b"\0" + OPAQUE_PIXEL


def chunk(kind, body=b""):
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body))
    )


def header(
    width=1, height=1, *, depth=8, color=6, compression=0, filters=0, interlace=0
):
    return chunk(
        b"IHDR",
        struct.pack(
            ">IIBBBBB", width, height, depth, color, compression, filters, interlace
        ),
    )


def image(raw, *, before=b"", after=b"", **options):
    return (
        SIGNATURE
        + header(**options)
        + before
        + chunk(b"IDAT", zlib.compress(raw))
        + after
        + chunk(b"IEND")
    )


def inspect_canonical(data):
    """Read the emitted header and pixels independently of the preview parser."""
    assert data[:8] == SIGNATURE
    chunks = []
    position = 8
    while position < len(data):
        size, kind = struct.unpack_from(">I4s", data, position)
        stop = position + 8 + size
        crc = struct.unpack_from(">I", data, stop)[0]
        assert crc == zlib.crc32(data[position + 4 : stop])
        chunks.append((kind, data[position + 8 : stop]))
        position = stop + 4
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert chunks[-1][1] == b""
    width, height, *options = struct.unpack(">IIBBBBB", chunks[0][1])
    assert options == [8, 6, 0, 0, 0]
    return (width, height), zlib.decompress(chunks[1][1])


def encode_filter(kind, row, prior, stride):
    result = bytearray([kind])
    for i, value in enumerate(row):
        left = row[i - stride] if i >= stride else 0
        above = prior[i]
        diagonal = prior[i - stride] if i >= stride else 0
        p = left + above - diagonal
        paeth = min((left, above, diagonal), key=lambda v: abs(v - p))
        predictor = (0, left, above, (left + above) // 2, paeth)[kind]
        result.append((value - predictor) % 256)
    return result


class NormalizationTests(unittest.TestCase):
    def test_split_rgb_idat_and_metadata_normalize_to_exact_rgba(self):
        stream = zlib.compress(b"\0\x01\x23\x45\xab\xcd\xef")
        source = (
            SIGNATURE
            + header(width=2, color=2)
            + chunk(b"gAMA", b"\0\0\xb1\x8f")
            + chunk(b"iTXt", b"Comment\0\0\0\0\0synthetic")
            + chunk(b"IDAT", stream[:2])
            + chunk(b"IDAT")
            + chunk(b"IDAT", stream[2:])
            + chunk(b"vpAg", b"unknown ancillary")
            + chunk(b"IEND")
        )
        canonical, raster = png.normalize(bytearray(source))
        self.assertEqual(
            inspect_canonical(canonical), ((2, 1), bytes.fromhex("00012345ffabcdefff"))
        )
        self.assertEqual(raster, ["012345abcdef"])
        self.assertEqual(png.normalize(canonical), (canonical, raster))
        self.assertIsNone(png.validate_canonical(canonical))

    def test_all_filters_reconstruct_rows_including_edges_and_wraparound(self):
        for color, stride in ((2, 3), (6, 4)):
            with self.subTest(color=color):
                rows = [
                    bytes(
                        (137 * x + 97 * y + 31 * x * y) % 256 for x in range(9 * stride)
                    )
                    for y in range(6)
                ]
                prior_rows = [b"\0" * (9 * stride)] + rows
                raw = b"".join(
                    encode_filter(kind, row, prior, stride)
                    for kind, row, prior in zip((4, 0, 1, 2, 3, 4), rows, prior_rows)
                )
                expected = b"".join(
                    b"\0"
                    + b"".join(
                        row[i : i + stride].ljust(4, b"\xff")
                        for i in range(0, len(row), stride)
                    )
                    for row in rows
                )
                canonical, _ = png.normalize(image(raw, width=9, height=6, color=color))
                self.assertEqual(inspect_canonical(canonical), ((9, 6), expected))

    def test_alpha_is_composited_only_in_raster(self):
        raw = bytes.fromhex("00ff000000ff000080ff0000ff")
        canonical, raster = png.normalize(image(raw, width=3))
        self.assertEqual(inspect_canonical(canonical), ((3, 1), raw))
        self.assertEqual(raster, ["202020901010ff0000"])

    def test_palette_depths_partial_alpha_and_nonpixel_padding(self):
        colors = bytes.fromhex("ff000000ff000000ff")
        for depth, packed, entries, width, expected, raster in (
            (1, b"\x7f", 2, 2, "00ff00000000ff00ff", "20202000ff00"),
            (2, b"\x1b", 3, 3, "00ff00000000ff00ff0000ffff", "20202000ff000000ff"),
            (4, b"\x01\x2f", 3, 3, "00ff00000000ff00ff0000ffff", "20202000ff000000ff"),
            (
                8,
                b"\0\x01\x02",
                3,
                3,
                "00ff00000000ff00ff0000ffff",
                "20202000ff000000ff",
            ),
        ):
            with self.subTest(depth=depth):
                source = image(
                    b"\0" + packed,
                    width=width,
                    color=3,
                    depth=depth,
                    before=chunk(b"PLTE", colors[: 3 * entries])
                    + chunk(b"tRNS", b"\0"),
                )
                canonical, rows = png.normalize(source)
                self.assertEqual(
                    inspect_canonical(canonical), ((width, 1), bytes.fromhex(expected))
                )
                self.assertEqual(rows, [raster])

    def test_grayscale_gray_alpha_and_rgb_color_key(self):
        fixtures = (
            # Packed grayscale filtering uses a byte stride of one, not a sample stride.
            (
                9,
                0,
                1,
                "01552a",
                b"",
                "00000000ff" + "ffffffff000000ff" * 4,
                "000000" + "ffffff000000" * 4,
            ),
            (
                3,
                0,
                2,
                "001b",
                chunk(b"tRNS", b"\xff\xfd"),
                "00000000ff55555500aaaaaaff",
                "000000202020aaaaaa",
            ),
            (2, 0, 4, "002f", b"", "00222222ffffffffff", "222222ffffff"),
            (1, 0, 8, "009a", b"", "009a9a9aff", "9a9a9a"),
            (2, 4, 8, "00aa80ccff", b"", "00aaaaaa80ccccccff", "656565cccccc"),
            (
                2,
                2,
                8,
                "000a14281e3c5a",
                chunk(b"tRNS", b"\x01\x0a\xfe\x14\xff\x28"),
                "000a1428001e3c5aff",
                "2020201e3c5a",
            ),
        )
        for width, color, depth, raw, before, expected, raster in fixtures:
            with self.subTest(color=color, depth=depth):
                canonical, rows = png.normalize(
                    image(
                        bytes.fromhex(raw),
                        width=width,
                        color=color,
                        depth=depth,
                        before=before,
                    )
                )
                self.assertEqual(
                    inspect_canonical(canonical), ((width, 1), bytes.fromhex(expected))
                )
                self.assertEqual(rows, [raster])

    def test_raster_preserves_aspect_and_samples_pixel_centers(self):
        for width, height, out_width, out_height in (
            (120, 64, 60, 32),
            (100, 20, 60, 12),
            (20, 64, 10, 32),
            (1, 128, 1, 32),
            (128, 1, 60, 1),
        ):
            with self.subTest(size=(width, height)):
                raw = b"".join(
                    b"\0" + b"".join(bytes([x, y, 0, 255]) for x in range(width))
                    for y in range(height)
                )
                canonical, raster = png.normalize(
                    image(raw, width=width, height=height)
                )
                self.assertEqual(inspect_canonical(canonical), ((width, height), raw))
                self.assertEqual(png.canonical_raster(canonical), raster)
                self.assertEqual(len(raster), out_height)
                self.assertTrue(all(len(row) == out_width * 6 for row in raster))
                self.assertEqual(
                    raster[0][:6],
                    f"{width // (2 * out_width):02x}{height // (2 * out_height):02x}00",
                )
                self.assertEqual(
                    raster[-1][-6:],
                    f"{width - 1 - (width - 1) // (2 * out_width):02x}"
                    f"{height - 1 - (height - 1) // (2 * out_height):02x}00",
                )

    def test_host_raster_derives_alpha_and_row_order_without_reencoding(self):
        raw = bytes.fromhex("00ff000000ff000080000000ffff00ff00ff")
        data, _ = png.normalize(image(raw, width=2, height=2))
        with patch.object(png.zlib, "compressobj", side_effect=AssertionError):
            self.assertEqual(
                png.canonical_raster(data), ["202020901010", "0000ff00ff00"]
            )


class RefusalTests(unittest.TestCase):
    def test_strict_chunk_boundaries_types_crc_and_order(self):
        valid = image(RGBA_ROW)
        idat = chunk(b"IDAT", zlib.compress(RGBA_ROW))
        iend = chunk(b"IEND")
        ihdr = SIGNATURE + header()
        fixtures = {
            "not bytes": "PNG",
            "wrong signature": b"x" + valid[1:],
            "invalid length": ihdr + b"\xff\xff\xff\xff" + idat[4:] + iend,
            "truncated": valid[:-1],
            "missing IEND": valid[:-12],
            "trailing bytes": valid + b"\0",
            "nonempty IEND": valid[:-12] + chunk(b"IEND", b"\0"),
            "missing IHDR": SIGNATURE + idat + iend,
            "empty IHDR": SIGNATURE + chunk(b"IHDR") + idat + iend,
            "late IHDR": SIGNATURE + chunk(b"tEXt") + header() + idat + iend,
            "duplicate IHDR": ihdr + header() + idat + iend,
            "missing IDAT": ihdr + iend,
            "nonconsecutive IDAT": ihdr + idat + chunk(b"tEXt") + chunk(b"IDAT") + iend,
            "unknown critical": ihdr + chunk(b"VpAg") + idat + iend,
            "nonalphabetic type": ihdr + chunk(b"vpA1") + idat + iend,
            "reserved chunk bit": ihdr + chunk(b"vpag") + idat + iend,
            "chunk after IEND": valid + chunk(b"vpAg"),
            "IDAT CRC": ihdr + idat[:-1] + bytes([idat[-1] ^ 1]) + iend,
            "ancillary CRC": ihdr + b"\0\0\0\0tEXt\0\0\0\0" + idat + iend,
            "late gamma": ihdr + idat + chunk(b"gAMA", b"\0\0\xb1\x8f") + iend,
            "late Exif": ihdr + idat + chunk(b"eXIf") + iend,
            "HDR metadata after PLTE": image(
                RGBA_ROW, before=chunk(b"PLTE", b"\0" * 3) + chunk(b"mDCV")
            ),
            "duplicate pHYs": image(RGBA_ROW, before=chunk(b"pHYs", b"\0" * 9) * 2),
            "hIST without palette": image(RGBA_ROW, before=chunk(b"hIST", b"\0\0")),
        }
        for kind in (b"acTL", b"fcTL", b"fdAT"):
            fixtures[kind] = image(RGBA_ROW, before=chunk(kind))
        for name, data in fixtures.items():
            with self.subTest(name=name), self.assertRaises(png.InvalidPNG):
                png.normalize(data)

    def test_unsupported_headers_and_invalid_scanline_format(self):
        for options in (
            {"width": 0},
            {"height": 0},
            {"width": png.MAX_DIMENSION + 1},
            {"width": 2049, "height": 2048},
            {"width": 2**31},
            {"depth": 16},
            {"depth": 4},
            {"depth": 3, "color": 3},
            {"color": 1},
            {"color": 5},
            {"color": 7},
            {"compression": 1},
            {"filters": 1},
            {"interlace": 1},
        ):
            with self.subTest(options=options), self.assertRaises(png.InvalidPNG):
                png.normalize(image(RGBA_ROW, **options))
        with self.assertRaises(png.InvalidPNG):
            png.normalize(image(b"\x05" + OPAQUE_PIXEL))

    def test_palette_and_transparency_structure(self):
        plte = chunk(b"PLTE", b"\0\0\0")
        trans = chunk(b"tRNS", b"\0")
        for label, source in (
            ("missing PLTE", image(b"\0\0", color=3)),
            ("duplicate PLTE", image(b"\0\0", color=3, before=plte * 2)),
            ("late PLTE", image(RGBA_ROW, after=plte)),
            (
                "malformed PLTE",
                image(b"\0\0", color=3, before=chunk(b"PLTE", b"\0" * 4)),
            ),
            (
                "oversized PLTE",
                image(b"\0\0", color=3, depth=1, before=chunk(b"PLTE", b"\0" * 9)),
            ),
            ("unexpected PLTE", image(b"\0\0", color=0, before=plte)),
            ("bad index", image(b"\0\x01", color=3, before=plte)),
            ("tRNS before PLTE", image(b"\0\0", color=3, before=trans + plte)),
            ("late tRNS", image(b"\0\0", color=3, before=plte, after=trans)),
            ("duplicate tRNS", image(b"\0\0", color=3, before=plte + trans + trans)),
            ("empty tRNS", image(b"\0\0", color=3, before=plte + chunk(b"tRNS"))),
            (
                "long tRNS",
                image(b"\0\0", color=3, before=plte + chunk(b"tRNS", b"\0\0")),
            ),
            ("tRNS with alpha", image(RGBA_ROW, before=trans)),
            (
                "short grayscale tRNS",
                image(b"\0\0", color=0, depth=1, before=chunk(b"tRNS", b"\x02")),
            ),
        ):
            with self.subTest(label=label), self.assertRaises(png.InvalidPNG):
                png.normalize(source)

    def test_single_exactly_bounded_complete_zlib_stream(self):
        good = zlib.compress(RGBA_ROW)
        for payload in (
            b"",
            good[:-1],
            good[:-4],
            good + b"\0",
            good + zlib.compress(b""),
            good[:-1] + bytes([good[-1] ^ 1]),
            zlib.compress(b""),
            zlib.compress(RGBA_ROW + b"\0"),
            zlib.compress(b"\0" * (2 * png.MAX_INPUT)),
        ):
            data = SIGNATURE + header() + chunk(b"IDAT", payload) + chunk(b"IEND")
            with self.subTest(length=len(payload)), self.assertRaises(png.InvalidPNG):
                png.normalize(data)
            with self.assertRaises(png.InvalidPNG):
                png.validate_canonical(data)

    def test_exact_input_chunk_and_pixel_limits(self):
        ancillary_size = png.MAX_INPUT - len(image(RGBA_ROW)) - 12
        for extra_bytes in (0, 1):
            source = image(
                RGBA_ROW, before=chunk(b"vpAg", b"\0" * (ancillary_size + extra_bytes))
            )
            if extra_bytes:
                with self.assertRaises(png.InvalidPNG):
                    png.normalize(source)
            else:
                self.assertEqual(png.normalize(source)[1], ["123456"])
        just_allowed = chunk(b"vpAg") * (png.MAX_CHUNKS - 3)
        self.assertEqual(
            png.normalize(image(RGBA_ROW, before=just_allowed))[1], ["123456"]
        )
        with self.assertRaises(png.InvalidPNG):
            png.normalize(image(RGBA_ROW, before=just_allowed + chunk(b"vpAg")))
        width = png.MAX_DIMENSION
        height = png.MAX_PIXELS // width
        raw = (b"\0" + OPAQUE_PIXEL * width) * height
        canonical, _ = png.normalize(image(raw, width=width, height=height))
        self.assertEqual(inspect_canonical(canonical), ((width, height), raw))
        png.validate_canonical(canonical)

    def test_exact_output_limit_for_normalizer_and_validator(self):
        data = image(RGBA_ROW)
        output, _ = png.normalize(data)
        with patch.object(png, "MAX_CANONICAL", len(output)):
            self.assertEqual(png.normalize(data)[0], output)
            png.validate_canonical(output)
        with patch.object(png, "MAX_CANONICAL", len(output) - 1):
            with self.assertRaises(png.InvalidPNG):
                png.normalize(data)
            with self.assertRaises(png.InvalidPNG):
                png.validate_canonical(output)

    def test_compact_input_can_exceed_the_real_output_bound(self):
        width = 4096
        height = 400
        # Alternating two pseudorandom rows is cheap with the Up filter. Their
        # zero-filter repetition is farther apart than zlib's 32 KiB window.
        first = random.Random(0).randbytes(4 * width)
        up = b"\x02" + b"\x01" * (4 * width)
        down = b"\x02" + b"\xff" * (4 * width)
        source = image(
            b"\0" + first + (up + down) * (height // 2 - 1) + up,
            width=width,
            height=height,
        )
        self.assertLess(len(source), png.MAX_INPUT)
        with self.assertRaisesRegex(png.InvalidPNG, "Canonical PNG exceeds byte limit"):
            png.normalize(source)

    def test_host_requires_canonical_shape_not_one_compressor_encoding(self):
        uncompressed = chunk(b"IDAT", zlib.compress(RGBA_ROW, level=0))
        valid = SIGNATURE + header() + uncompressed + chunk(b"IEND")
        self.assertIsNone(png.validate_canonical(valid))
        self.assertEqual(png.canonical_raster(valid), ["123456"])
        for source in (
            image(b"\0\x12\x34\x56", color=2),
            image(RGBA_ROW, before=chunk(b"tEXt", b"Comment\0synthetic")),
            image(RGBA_ROW, after=chunk(b"vpAg")),
            image(b"\x01" + OPAQUE_PIXEL),
            SIGNATURE + header() + uncompressed + chunk(b"IDAT") + chunk(b"IEND"),
        ):
            png.normalize(source)  # All are otherwise accepted PNG inputs.
            for check in (png.validate_canonical, png.canonical_raster):
                with (
                    self.subTest(check=check.__name__),
                    self.assertRaises(png.InvalidPNG),
                ):
                    check(source)


if __name__ == "__main__":
    unittest.main()
