"""Bounded, payload-only PNG normalization for preview and explicit export.

Support static non-interlaced PNGs: RGB, RGBA and gray+alpha at 8 bits,
and grayscale/indexed color at 1, 2, 4 or 8 bits. Interpret PLTE and tRNS,
but discard other ancillary data without decoding it (no color management).
Reject animation, unknown critical chunks and images outside the limits.
"""

import re
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_INPUT = 4 * 1024 * 1024
MAX_CANONICAL = 5 * 1024 * 1024
MAX_PIXELS = 4_194_304
MAX_DIMENSION = 4096
MAX_CHUNKS = 4096
RASTER_WIDTH = 60
RASTER_HEIGHT = 32
BACKGROUND = 0x20

# Signature plus 13-byte IHDR and three 12-byte chunk frames.
_CANONICAL_OVERHEAD = 57
_CHUNK_TYPE = re.compile(rb"[A-Za-z]{2}[A-Z][A-Za-z]")
_FORMATS = {
    # Color type: (samples per pixel, supported sample depths)
    0: (1, (1, 2, 4, 8)),
    2: (3, (8,)),
    3: (1, (1, 2, 4, 8)),
    4: (2, (8,)),
    6: (4, (8,)),
}
_ANIMATION = {b"acTL", b"fcTL", b"fdAT"}
_BEFORE_PALETTE = {
    b"cHRM",
    b"gAMA",
    b"iCCP",
    b"sBIT",
    b"sRGB",
    b"cICP",
    b"mDCV",
    b"cLLI",
}
_BEFORE_DATA = {b"bKGD", b"eXIf", b"hIST", b"pHYs", b"sPLT"}
_SINGLE_METADATA = (_BEFORE_PALETTE | _BEFORE_DATA | {b"tIME"}) - {b"sPLT"}


class InvalidPNG(ValueError):
    """Fixed PNG refusal; messages never include input data."""


def _chunks(data, limit):
    if not isinstance(data, (bytes, bytearray)) or len(data) > limit:
        raise InvalidPNG("PNG byte limit exceeded or invalid input type")
    data = bytes(data)
    if not data.startswith(PNG_SIGNATURE):
        raise InvalidPNG("Invalid PNG signature")
    view = memoryview(data)
    offset = len(PNG_SIGNATURE)
    for _ in range(MAX_CHUNKS):
        if len(data) - offset < 12:
            raise InvalidPNG("Incomplete PNG chunk")
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 8 + size
        if end + 4 > len(data) or not _CHUNK_TYPE.fullmatch(kind):
            raise InvalidPNG("Invalid PNG chunk")
        if zlib.crc32(view[offset + 4 : end]) != struct.unpack_from(">I", data, end)[0]:
            raise InvalidPNG("Invalid PNG chunk CRC")
        body = view[offset + 8 : end]
        offset = end + 4
        if kind == b"IEND":
            if size or offset != len(data):
                raise InvalidPNG("Invalid PNG end")
            yield kind, body
            return
        yield kind, body
    raise InvalidPNG("PNG chunk count exceeded")


def _header(header, canonical):
    if len(header) != 13:
        raise InvalidPNG("Invalid PNG header")
    width, height, depth, color, compression, filters, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if not (
        0 < width <= MAX_DIMENSION
        and 0 < height <= MAX_DIMENSION
        and width * height <= MAX_PIXELS
    ):
        raise InvalidPNG("PNG dimensions exceed limits")
    if (
        color not in _FORMATS
        or depth not in _FORMATS[color][1]
        or compression
        or filters
        or interlace
        or (canonical and (depth, color) != (8, 6))
    ):
        raise InvalidPNG("Unsupported PNG format")
    return width, height, depth, color


def _transparency(body, color, depth, palette):
    if color == 3 and palette is not None and 0 < len(body) <= len(palette) // 3:
        return bytes(body)
    if color in (0, 2) and len(body) == 2 * _FORMATS[color][0]:
        # Below 16 bits PNG decoders use only the low sample-depth bits.
        mask = (1 << depth) - 1
        return bytes(sample & mask for sample in body[1::2])
    raise InvalidPNG("Invalid PNG transparency")


def _inflate(compressed, expected):
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(compressed, expected + 1)
    except zlib.error:
        raise InvalidPNG("Invalid PNG compressed data") from None
    # No flush: the exact output bound and a complete stream are both required.
    if (
        len(decoded) != expected
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise InvalidPNG("Invalid PNG compressed data length")
    return decoded


def _decode(data, *, canonical):
    chunks = _chunks(data, MAX_CANONICAL if canonical else MAX_INPUT)
    kind, body = next(chunks)
    if kind != b"IHDR":
        raise InvalidPNG("PNG header must come first")
    width, height, depth, color = _header(body, canonical)
    palette = transparency = None
    seen = set()
    in_idat = has_idat = False
    compressed = bytearray()
    for kind, body in chunks:
        if kind == b"IDAT":
            if (has_idat and (canonical or not in_idat)) or (
                color == 3 and palette is None
            ):
                raise InvalidPNG("Invalid PNG data chunk order")
            in_idat = has_idat = True
            compressed.extend(body)
            continue
        in_idat = False
        if kind == b"IEND":
            continue
        if canonical:
            raise InvalidPNG("Noncanonical PNG chunk")
        if kind == b"PLTE":
            entries = len(body) // 3
            if (
                palette is not None
                or has_idat
                or transparency is not None
                or b"bKGD" in seen
                or color in (0, 4)
                or len(body) % 3
                or not 1 <= entries <= (1 << depth if color == 3 else 256)
            ):
                raise InvalidPNG("Invalid PNG palette")
            palette = bytes(body)
        elif kind == b"tRNS":
            if transparency is not None or has_idat:
                raise InvalidPNG("Invalid PNG transparency order")
            transparency = _transparency(body, color, depth, palette)
        elif kind[0] < ord("a") or kind in _ANIMATION:
            raise InvalidPNG("Unsupported PNG chunk")
        else:
            # Enforce known placement without interpreting metadata payloads.
            if (
                (kind in _SINGLE_METADATA and kind in seen)
                or (kind in _BEFORE_PALETTE and (palette is not None or has_idat))
                or (kind in _BEFORE_DATA and has_idat)
                or (kind == b"hIST" and palette is None)
                or (kind == b"bKGD" and color == 3 and palette is None)
            ):
                raise InvalidPNG("Invalid PNG metadata order")
            if kind in _SINGLE_METADATA:
                seen.add(kind)
    if not has_idat:
        raise InvalidPNG("Missing PNG image data")
    row_bytes = (width * _FORMATS[color][0] * depth + 7) // 8
    decoded = _inflate(compressed, height * (row_bytes + 1))
    return width, height, depth, color, row_bytes, palette, transparency, decoded


def _paeth(left, above, diagonal):
    a = abs(above - diagonal)
    b = abs(left - diagonal)
    c = abs(left + above - 2 * diagonal)
    if a <= b and a <= c:
        return left
    return above if b <= c else diagonal


def _unfilter(kind, row, prior, stride):
    if kind == 0:
        return row
    if not 1 <= kind <= 4:
        raise InvalidPNG("Unsupported PNG scanline filter")
    row = bytearray(row)
    if kind == 1:
        for i in range(stride, len(row)):
            row[i] = (row[i] + row[i - stride]) & 255
    elif kind == 2:
        return bytearray((value + above) & 255 for value, above in zip(row, prior))
    elif kind == 3:
        for i in range(stride):
            row[i] = (row[i] + prior[i] // 2) & 255
        for i in range(stride, len(row)):
            row[i] = (row[i] + (row[i - stride] + prior[i]) // 2) & 255
    else:
        for i in range(stride):
            row[i] = (row[i] + prior[i]) & 255
        for i in range(stride, len(row)):
            row[i] = (
                row[i] + _paeth(row[i - stride], prior[i], prior[i - stride])
            ) & 255
    return row


def _samples(row, width, depth):
    if depth == 8:
        return row
    mask = (1 << depth) - 1
    return (
        (row[bit // 8] >> (8 - depth - bit % 8)) & mask
        for bit in range(0, width * depth, depth)
    )


def _rgba_rows(width, height, depth, color, row_bytes, palette, transparency, decoded):
    stride = max(1, (_FORMATS[color][0] * depth + 7) // 8)
    if color == 0:
        maximum = (1 << depth) - 1
        transparent = transparency[0] if transparency is not None else -1
        lookup = [
            bytes([value * 255 // maximum] * 3 + [0 if value == transparent else 255])
            for value in range(maximum + 1)
        ]
    elif color == 3:
        alpha = (transparency or b"").ljust(len(palette) // 3, b"\xff")
        lookup = [
            palette[i : i + 3] + bytes([alpha[i // 3]])
            for i in range(0, len(palette), 3)
        ]
    prior = bytes(row_bytes)
    if color == 2:
        opaque = b"\xff" * width
    for y in range(height):
        start = y * (row_bytes + 1)
        row = _unfilter(
            decoded[start], decoded[start + 1 : start + row_bytes + 1], prior, stride
        )
        prior = row
        if color == 6:
            rgba = row
        elif color in (0, 3):
            try:
                rgba = b"".join(lookup[value] for value in _samples(row, width, depth))
            except IndexError:
                raise InvalidPNG("PNG palette index out of range") from None
        else:
            rgba = bytearray(4 * width)
            if color == 2:
                rgba[0::4], rgba[1::4], rgba[2::4] = row[0::3], row[1::3], row[2::3]
                rgba[3::4] = opaque
                if transparency is not None:
                    for x in range(width):
                        if row[3 * x : 3 * x + 3] == transparency:
                            rgba[4 * x + 3] = 0
            else:
                gray = row[::2]
                rgba[0::4] = rgba[1::4] = rgba[2::4] = gray
                rgba[3::4] = row[1::2]
        yield rgba


def _raster_samples(width, height):
    out_width, out_height = width, height
    if width > RASTER_WIDTH or height > RASTER_HEIGHT:
        if width * RASTER_HEIGHT >= height * RASTER_WIDTH:
            out_width = RASTER_WIDTH
            out_height = max(1, (height * RASTER_WIDTH + width // 2) // width)
        else:
            out_height = RASTER_HEIGHT
            out_width = max(1, (width * RASTER_HEIGHT + height // 2) // height)
    xs = [4 * ((2 * x + 1) * width // (2 * out_width)) for x in range(out_width)]
    ys = {(2 * y + 1) * height // (2 * out_height) for y in range(out_height)}
    return xs, ys


def _raster_row(rgba, xs):
    rgb = bytearray()
    for x in xs:
        r, g, b, alpha = rgba[x : x + 4]
        bg = BACKGROUND * (255 - alpha) + 127
        rgb.extend((channel * alpha + bg) // 255 for channel in (r, g, b))
    return rgb.hex()


def _chunk(kind, body):
    crc = zlib.crc32(body, zlib.crc32(kind))
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def normalize(data: bytes | bytearray) -> tuple[bytes, list[str]]:
    """Return a metadata-free RGBA PNG and nearest-neighbor sampled preview.

    Raster rows are lowercase RGB hex, six characters per pixel, composited
    over #202020. The export retains the full image and its alpha samples.
    Oversized exports are refused rather than silently resizing the image.
    Raises InvalidPNG (a ValueError) on rejection.
    """
    parsed = _decode(data, canonical=False)
    width, height = parsed[:2]
    xs, ys = _raster_samples(width, height)
    raster = []
    compressor = zlib.compressobj()
    compressed = bytearray()

    def add(block):
        if len(compressed) + len(block) + _CANONICAL_OVERHEAD > MAX_CANONICAL:
            raise InvalidPNG("Canonical PNG exceeds byte limit")
        compressed.extend(block)

    # Compress zero-filter rows as they are reconstructed; no second
    # full-resolution uncompressed buffer is needed for the RGBA export.
    for y, rgba in enumerate(_rgba_rows(*parsed)):
        add(compressor.compress(b"\0" + rgba))
        if y in ys:
            raster.append(_raster_row(rgba, xs))
    add(compressor.flush())
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    canonical = b"".join(
        (
            PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", compressed),
            _chunk(b"IEND", b""),
        )
    )
    return canonical, raster


def _canonical_scanlines(data):
    width, height, _, _, row_bytes, _, _, decoded = _decode(data, canonical=True)
    if any(decoded[:: row_bytes + 1]):
        raise InvalidPNG("Noncanonical PNG scanline filter")
    return width, height, row_bytes + 1, decoded


def validate_canonical(data: bytes | bytearray) -> None:
    """Raise InvalidPNG unless data has the exact export shape.

    Accept any complete zlib encoding of the same shape, not just the bytes
    produced by this host's particular zlib encoder.
    """
    _canonical_scanlines(data)


def canonical_raster(data: bytes | bytearray) -> list[str]:
    """Validate an export and derive the preview directly from its pixels."""
    width, height, stride, decoded = _canonical_scanlines(data)
    xs, ys = _raster_samples(width, height)
    rows = memoryview(decoded)
    return [
        _raster_row(rows[y * stride + 1 : (y + 1) * stride], xs) for y in sorted(ys)
    ]
