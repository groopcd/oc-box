"""Synthetic, local-only checks for optional release artwork."""

import builtins
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import release_card

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ImportError:
    Image = None


class ReleaseCardInputTests(unittest.TestCase):
    def test_invalid_inputs_refuse_without_creating_output(self):
        cases = (
            {"channel": "preview"},
            {"channel": "STABLE"},
            {"channel": ""},
            {"version": ""},
            {"version": "\x00\n\u202e"},
            {"version": None},
            {"highlights": []},
            {"highlights": ["a"] * 4},
            {"highlights": ("a",)},
            {"highlights": [" \n\t"]},
            {"highlights": [None]},
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "card.png"
            for case in cases:
                with self.subTest(case=case):
                    options = {
                        "version": "1.6.100",
                        "channel": "stable",
                        "highlights": ["Synthetic release highlight"],
                    }
                    options.update(case)
                    with self.assertRaises(ValueError):
                        release_card.render_card(output, **options)
                    self.assertFalse(output.exists())

    def test_pillow_is_optional_and_missing_dependency_has_fixed_error(self):
        original_import = builtins.__import__

        def without_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("synthetic unavailable dependency")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "card.png"
            with patch("builtins.__import__", side_effect=without_pillow):
                with self.assertRaisesRegex(RuntimeError, "optional Pillow"):
                    release_card.render_card(
                        output,
                        version="1.6.100",
                        channel="stable",
                        highlights=["Synthetic highlight"],
                    )
            self.assertFalse(output.exists())


@unittest.skipIf(Image is None, "optional Pillow dependency is not installed")
class ReleaseCardRenderingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="release-card-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def render(self, name, **options):
        output = self.root / name
        arguments = {
            "version": "1.6.100",
            "channel": "stable",
            "highlights": [
                "Synthetic workspace improvements",
                "A clearer release pipeline",
                "Predictable local tooling",
            ],
        }
        arguments.update(options)
        self.assertIsNone(release_card.render_card(output, **arguments))
        with Image.open(output) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1200, 630))
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.info, {})
            return output.read_bytes(), image.copy()

    def test_branded_png_channel_distinction_and_highlight_counts(self):
        stable_bytes, stable = self.render("stable.png")
        devel_bytes, devel = self.render("devel.png", channel="devel")
        self.assertNotEqual(stable_bytes, devel_bytes)
        self.assertIsNotNone(ImageChops.difference(stable, devel).getbbox())
        self.assertGreater(len(stable.getcolors(1_000_000)), 100)
        self.assertEqual(stable.getpixel((0, 0)), (71, 223, 194))
        for count in (1, 2):
            with self.subTest(highlights=count):
                data, _ = self.render(
                    f"count-{count}.png", highlights=["Synthetic feature"] * count
                )
                self.assertNotEqual(data, stable_bytes)

    def test_identical_inputs_have_deterministic_png_bytes(self):
        first, _ = self.render("first.png")
        second, _ = self.render("second.png")
        self.assertEqual(first, second)

    def test_long_unicode_is_clipped_to_highlight_regions(self):
        _, short = self.render("short.png", highlights=["x"] * 3)
        _, long = self.render(
            "unicode.png",
            highlights=[
                "Workspaces — café Ελληνικά 中文 🚀 " * 200,
                "W" * 10_000,
                ("A\u0301\u0308" * 1000) + "\u202e\x00\ud800",
            ],
        )
        left, top, right, bottom = ImageChops.difference(short, long).getbbox()
        self.assertGreaterEqual(left, 94)
        self.assertGreaterEqual(top, 293)
        self.assertLessEqual(right, 94 + 637)
        self.assertLessEqual(bottom, 293 + 2 * 78 + 64)
        _, long_version = self.render(
            "version.png",
            version="版本🚀" + "W" * 10_000,
            highlights=["x"] * 3,
        )
        left, top, right, bottom = ImageChops.difference(short, long_version).getbbox()
        self.assertGreaterEqual(left, 63)
        self.assertGreaterEqual(top, 177)
        self.assertLessEqual(right, 63 + 688)
        self.assertLessEqual(bottom, 177 + 85)

    def test_plain_text_normalization_and_bounded_prefixes(self):
        original, _ = self.render(
            "plain.png",
            version="1.6.100",
            highlights=["café release", "Build better", "Ship locally"],
        )
        normalized, _ = self.render(
            "normalized.png",
            version=" 1.6.100\n",
            highlights=["café\nrelease", "Build\u202ebetter", "Ship\x00locally"],
        )
        self.assertEqual(original, normalized)
        bounded, _ = self.render(
            "bounded.png", version="V" * 128, highlights=["W" * 2048]
        )
        longer, _ = self.render(
            "longer.png",
            version="V" * 128 + "ignored",
            highlights=["W" * 2048 + "ignored"],
        )
        self.assertEqual(bounded, longer)

    def test_wrapped_lines_are_width_bounded_and_ellipsized(self):
        draw = ImageDraw.Draw(Image.new("RGB", (400, 200)))
        font = release_card._font(ImageFont, 25)
        for value in ("A long synthetic feature " * 100, "W" * 2048, "中文🚀" * 100):
            with self.subTest(text=value[:20]):
                lines = release_card._lines(draw, value, font, 300, 2)
                self.assertEqual(len(lines), 2)
                self.assertTrue(lines[-1].endswith("..."))
                for line in lines:
                    self.assertLessEqual(release_card._width(draw, line, font), 300)

    def test_missing_system_fonts_use_local_default_without_downloads(self):
        with patch.object(release_card, "_FONT_DIRS", (self.root / "no-fonts",)):
            first, _ = self.render(
                "fallback.png", highlights=["Unicode café — 中文 🚀"]
            )
            second, _ = self.render(
                "fallback-again.png", highlights=["Unicode café — 中文 🚀"]
            )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
