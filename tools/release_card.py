"""Local, deterministic release artwork; Pillow is optional until rendering.

Only public release text belongs here. Nothing is evaluated, fetched, or read
from the environment. Text is normalized, bounded, and clipped to fixed regions.
The same inputs, Pillow version, and installed fonts produce the same PNG bytes.
"""

from pathlib import Path
import unicodedata


_FONT_DIRS = (
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
)
_SIZE = (1200, 630)
_WHITE = "#f0f7ff"
_MUTED = "#a9bed3"
_TEAL = "#47dfc2"


def _plain_text(value, limit):
    if not isinstance(value, str):
        raise ValueError("Release card text must be a string")
    # Strip controls, bidi overrides, and invalid surrogate code points. Slice
    # before normalization so arbitrarily large public notes cannot grow layout.
    text = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in value[:limit]
    )
    text = " ".join(text.split())
    if not text:
        raise ValueError("Release card text must not be empty")
    return text


def _font(image_font, size, *, bold=False):
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for directory in _FONT_DIRS:
        try:
            return image_font.truetype(str(directory / filename), size=size)
        except OSError:
            pass
    try:
        return image_font.load_default(size=size)
    except TypeError:  # Older Pillow has a fixed-size bitmap default.
        return image_font.load_default()


def _width(draw, text, font):
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def _ellipsis(draw, text, font, width):
    if _width(draw, text, font) <= width:
        return text
    suffix = "..."
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _width(draw, text[:middle].rstrip() + suffix, font) <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + suffix


def _lines(draw, text, font, width, count):
    result = []
    while text and len(result) < count:
        if len(result) == count - 1:
            result.append(_ellipsis(draw, text, font, width))
            break
        end = 0
        while end < len(text) and _width(draw, text[: end + 1], font) <= width:
            end += 1
        if end == len(text):
            result.append(text)
            break
        # Wrap words where possible, splitting a single long token if needed.
        space = text.rfind(" ", 0, end + 1)
        end = space if space > 0 else max(1, end)
        result.append(text[:end].rstrip())
        text = text[end:].lstrip()
    return result


def _text(image, image_module, draw_module, box, text, font, fill, *, lines=1):
    """Render into a clipped tile, including fonts with tall/negative bearings."""
    x, y, width, height = box
    tile = image_module.new("RGBA", (width, height))
    draw = draw_module.Draw(tile)
    try:
        font.getmask(text)
    except UnicodeEncodeError:
        # Old bitmap fallback fonts support Latin-1 only; keep them usable.
        text = text.encode("latin-1", errors="replace").decode("latin-1")
    step = height // lines
    for index, line in enumerate(_lines(draw, text, font, width, lines)):
        left, top, _, _ = draw.textbbox((0, 0), line, font=font)
        draw.text((-left, index * step - top), line, font=font, fill=fill)
    image.paste(tile, (x, y), tile)


def _cube(draw, x, y, size, color):
    half, quarter = size // 2, size // 4
    points = (
        (x + half, y),
        (x + size, y + quarter),
        (x + size, y + 3 * quarter),
        (x + half, y + size),
        (x, y + 3 * quarter),
        (x, y + quarter),
    )
    draw.line((*points, points[0]), fill=color, width=2)
    draw.line((points[5], (x + half, y + half), points[1]), fill=color, width=2)
    draw.line(((x + half, y + half), points[3]), fill=color, width=2)


def render_card(
    output: Path, *, version: str, channel: str, highlights: list[str]
) -> None:
    """Write a 1200x630 branded PNG, without network or command execution.

    ``channel`` must be exactly ``stable`` or ``devel``; ``highlights`` must
    contain one to three nonempty strings. Version text uses at most its first
    128 characters and each highlight its first 2048, with visual ellipses for
    overflow. Control characters become spaces. No release-text markup is used.
    Parent directories must already exist. Missing Pillow raises RuntimeError.
    """
    if channel not in ("stable", "devel"):
        raise ValueError("Release card channel must be stable or devel")
    if not isinstance(highlights, list) or not 1 <= len(highlights) <= 3:
        raise ValueError("Release card requires one to three highlights")
    version = _plain_text(version, 128)
    highlights = [_plain_text(item, 2048) for item in highlights]
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError(
            "Release card rendering requires the optional Pillow dependency"
        ) from None

    image = Image.new("RGB", _SIZE)
    draw = ImageDraw.Draw(image)
    # A quiet navy gradient and right-hand blueprint grid remain legible even
    # when LinkedIn downscales the image. All artwork is drawn locally.
    for y in range(_SIZE[1]):
        draw.line(
            (0, y, 1199, y),
            fill=(10 + 3 * y // 630, 20 + 10 * y // 630, 36 + 15 * y // 630),
        )
    draw.rectangle((0, 0, 1199, 7), fill=_TEAL)
    for x in range(780, 1160, 36):
        draw.line((x, 178, x, 533), fill="#182d42")
    for y in range(178, 534, 36):
        draw.line((780, y, 1150, y), fill="#182d42")

    def text(box, value, size, fill=_WHITE, *, bold=False, lines=1):
        _text(
            image,
            Image,
            ImageDraw,
            box,
            value,
            _font(ImageFont, size, bold=bold),
            fill,
            lines=lines,
        )

    _cube(draw, 64, 51, 40, _TEAL)
    text((120, 53, 300, 49), "oc-box", 42, bold=True)
    accent = _TEAL if channel == "stable" else "#ffba76"
    draw.rounded_rectangle(
        (785, 55, 1136, 105), radius=25, fill="#192d40", outline=accent, width=1
    )
    text(
        (807, 69, 311, 24),
        "STABLE" if channel == "stable" else "DEVEL / EXPERIMENTAL",
        21,
        accent,
        bold=True,
    )
    text((67, 137, 650, 22), "A NEW RELEASE", 18, _MUTED, bold=True)
    title = version if version.startswith("v") else "v" + version
    text((63, 177, 688, 85), title, 72, bold=True)

    for index, highlight in enumerate(highlights):
        y = 293 + index * 78
        draw.rounded_rectangle((66, y + 7, 73, y + 32), radius=3, fill=_TEAL)
        text((94, y, 637, 64), highlight, 25, lines=2)

    # Stacked independent workspace windows flow into a release pipeline.
    for x, y in ((838, 220), (818, 239), (798, 258)):
        draw.rounded_rectangle(
            (x, y, x + 280, y + 170),
            radius=13,
            fill="#112438",
            outline="#34536a",
            width=2,
        )
        draw.line((x, y + 33, x + 280, y + 33), fill="#34536a", width=1)
        for offset in (17, 31, 45):
            draw.ellipse((x + offset, y + 14, x + offset + 5, y + 19), fill=_TEAL)
    text((818, 309, 240, 24), "ISOLATED WORKSPACES", 17, _MUTED, bold=True)
    _cube(draw, 823, 352, 47, _TEAL)
    for y, length in ((354, 136), (373, 106), (392, 122)):
        draw.rounded_rectangle((894, y, 894 + length, y + 5), radius=2, fill="#52758e")
    draw.line((824, 471, 1092, 471), fill="#34536a", width=3)
    for x in (824, 958, 1092):
        draw.ellipse((x - 8, 463, x + 8, 479), fill=accent)
    for x, label in ((796, "ISOLATE"), (936, "BUILD"), (1074, "SHIP")):
        text((x, 493, 85, 23), label, 15, _MUTED, bold=True)

    draw.line((64, 553, 1136, 553), fill="#294256", width=1)
    text(
        (65, 575, 800, 27),
        "OpenCode. Isolated workspaces. Your workflow.",
        21,
        _MUTED,
    )
    text((1019, 575, 120, 27), "oc-box", 21, _TEAL, bold=True)
    image.save(output, format="PNG", optimize=False, compress_level=9)
