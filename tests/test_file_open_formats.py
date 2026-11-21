"""Offline format shaping tests; no desktop handlers or workspace state."""

import struct
import unittest
import zlib
from html.parser import HTMLParser
from unittest.mock import patch

from oc_box import file_open_formats as formats
from oc_box import file_preview_png as png


def _png(*, pixel_format=6, metadata=b"", filter_byte=b"\0"):
    def chunk(kind, data=b""):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, pixel_format, 0, 0, 0))
        + metadata
        + chunk(b"IDAT", zlib.compress(filter_byte + b"\x01\x02\x03\xff"))
        + chunk(b"IEND")
    )


class _HTMLProbe(HTMLParser):
    def __init__(self, data):
        super().__init__()
        self.events = []
        self.feed(data.decode("utf-8"))
        self.close()

    def handle_starttag(self, tag, attrs):
        self.events.append(("start", tag, attrs))

    def handle_endtag(self, tag):
        self.events.append(("end", tag))

    def handle_data(self, data):
        if data.strip():
            self.events.append(("text", data))

    def handle_decl(self, decl):
        self.events.append(("declaration", decl))

    def handle_comment(self, data):
        self.events.append(("comment", data))

    def handle_pi(self, data):
        self.events.append(("instruction", data))


class FileOpenFormatsTests(unittest.TestCase):
    def assertStaticShell(self, output):
        events = _HTMLProbe(output).events
        self.assertEqual(
            events[:9],
            [
                ("declaration", "doctype html"),
                ("start", "html", []),
                ("start", "head", []),
                ("start", "meta", [("charset", "utf-8")]),
                (
                    "start",
                    "meta",
                    [
                        ("http-equiv", "Content-Security-Policy"),
                        (
                            "content",
                            "default-src 'none'; base-uri 'none'; form-action 'none'",
                        ),
                    ],
                ),
                ("start", "title", []),
                ("text", "Workspace file"),
                ("end", "title"),
                ("end", "head"),
            ],
        )
        self.assertEqual(events[9], ("start", "body", []))
        self.assertEqual(events[-2:], [("end", "body"), ("end", "html")])
        return events[10:-2]

    def test_only_explicit_kinds_and_safe_mime_types(self):
        self.assertEqual(
            formats.MIME_TYPES,
            {
                "png": {"image/png"},
                "txt": {"text/plain"},
                "md": {"text/markdown", "text/plain"},
                "html": {"text/html"},
            },
        )
        for kind in ("", "PNG", "text", "svg", "pdf", "application/pdf", None, []):
            with self.subTest(kind=kind), self.assertRaises(formats.InvalidFormat):
                formats.prepare(kind, b"anything")

    def test_png_validates_once_and_preserves_canonical_bytes(self):
        data = _png()
        with patch.object(
            png, "validate_canonical", wraps=png.validate_canonical
        ) as check:
            self.assertEqual(formats.prepare("png", bytearray(data)), data)
            check.assert_called_once_with(data)

    def test_png_refuses_trailing_data_unsupported_or_noncanonical_chunks(self):
        comment = b"tEXtDescription\0synthetic"
        metadata = (
            struct.pack(">I", len(comment) - 4)
            + comment
            + struct.pack(">I", zlib.crc32(comment))
        )
        data_cases = (
            b"not a PNG",
            _png() + b"trailing",
            _png(pixel_format=2),
            _png(metadata=metadata),
            _png(filter_byte=b"\1"),
        )
        for data in data_cases:
            with self.subTest(data=data), self.assertRaises(formats.InvalidFormat):
                formats.prepare("png", data)
        with patch.object(png, "validate_canonical") as check:
            with self.assertRaises(formats.InvalidFormat):
                formats.prepare("png", b"x" * (5 * 1024 * 1024 + 1))
            check.assert_not_called()

    def test_text_and_markdown_unchanged_including_newlines_and_unicode(self):
        data = "# Café & 日本語\r\n\t*item*\n<ordinary text>\r".encode()
        for kind in ("txt", "md"):
            with self.subTest(kind=kind):
                self.assertEqual(formats.prepare(kind, bytearray(data)), data)
                self.assertEqual(formats.prepare(kind, b""), b"")

    def test_encoding_controls_and_input_bounds(self):
        for kind in ("txt", "md", "html"):
            for data in (
                "\ufeffBOM".encode(),
                "x\u202eRTL".encode(),
                "x\u200bZWSP".encode(),
                "x\u0085NEL".encode(),
                b"x\xed\xa0\x80",
                b"x\xff",
                b"x\x00",
                b"x\x0b",
                b"x\x1b",
                b"x\x7f",
                b"x" * (256 * 1024 + 1),
                "not bytes",
            ):
                with self.subTest(kind=kind, size=len(data), prefix=data[:12]):
                    with self.assertRaises(formats.InvalidFormat):
                        formats.prepare(kind, data)

    def test_text_and_markdown_refuse_leading_executable_spoofs(self):
        data_cases = (
            b"#!/bin/sh\n",
            b"\r\n\t#!/usr/bin/env python\n",
            b"%!PS-Adobe-3.0\n",
            b"[Desktop Entry]\r\nExec=example\n",
            b"# a comment\r\n \t[\tDeSkToP  EnTrY ]\r",
            b"\n[KDE Desktop Entry]\n",
        )
        for kind in ("txt", "md"):
            for data in data_cases:
                with (
                    self.subTest(kind=kind, data=data),
                    self.assertRaises(formats.InvalidFormat),
                ):
                    formats.prepare(kind, data)
            ordinary = b"# Article\n\nA mention of [Desktop Entry] and #!/bin/sh.\n"
            self.assertEqual(formats.prepare(kind, ordinary), ordinary)

    def test_html_rebuilds_article_and_removes_all_untrusted_attributes_and_elements(
        self,
    ):
        source = (
            '<!doctype html SYSTEM "https://example.invalid/doctype"><html lang="xx">'
            '<head><meta http-equiv="refresh" content="0;url=https://example.invalid">'
            '<base href="https://example.invalid"><title>counterfeit title</title>'
            '<style>@import "https://example.invalid/style";</style></head><body>'
            '<article id="unsafe" onclick="example()" style="color:lime">'
            "<h2 class=hidden>Café &amp; 日本語</h2>"
            '<p>Open <a href="file:///example" ping="//example.invalid">link text</a>'
            '<img srcset="//example.invalid/a 1x" onerror=example()>'
            "<svg><foreignObject><p>discard SVG</p></foreignObject></svg>"
            "<math><mtext>discard MathML</mtext></math>"
            '<iframe srcdoc="<form>discard iframe</form>">discard iframe</iframe>'
            '<object data="https://example.invalid/object">discard object</object>'
            "<script>discard script</script><template>discard template</template>"
            '<form action="https://example.invalid"><input autofocus>'
            "<label>label text</label></form>"
            '<unknown-element data-payload="private">unknown text</unknown-element>'
            "<!-- not output --><?not-output?><br dirty=1>"
            "&lt;img src=x onerror=example()&gt; &amp;lt;script&amp;gt;"
            '</p><hr title="removed"></article></body></html>'
        ).encode()
        output = formats.prepare("html", source)
        self.assertEqual(
            self.assertStaticShell(output),
            [
                ("start", "article", []),
                ("start", "h2", []),
                ("text", "Café & 日本語"),
                ("end", "h2"),
                ("start", "p", []),
                ("text", "Open link textlabel textunknown text"),
                ("start", "br", []),
                ("text", "<img src=x onerror=example()> &lt;script&gt;"),
                ("end", "p"),
                ("start", "hr", []),
                ("end", "article"),
            ],
        )
        self.assertNotIn(b"example.invalid", output)
        self.assertNotIn(b"<img", output)
        self.assertNotIn(b"discard", output)

    def test_html_handles_unclosed_and_mismatched_tags_without_source_markup(self):
        output = formats.prepare(
            "html",
            b"""</head><P odd='"><script>example()</script>' repeated=1 repeated=2>"""
            b"<em>first</P></em><span/><br /><strong>last",
        )
        self.assertEqual(
            self.assertStaticShell(output),
            [
                ("start", "p", []),
                ("start", "em", []),
                ("text", "first"),
                ("end", "em"),
                ("end", "p"),
                ("start", "span", []),
                ("end", "span"),
                ("start", "br", []),
                ("start", "strong", []),
                ("text", "last"),
                ("end", "strong"),
            ],
        )
        for source in (
            b'<p title="</body><script>unterminated attribute',
            b"<![broken]>hidden",
            b"<script>unfinished <iframe src='https://example.invalid'>",
        ):
            with self.subTest(source=source):
                try:
                    output = formats.prepare("html", source)
                except formats.InvalidFormat as exc:
                    # Some stdlib versions refuse malformed declarations.
                    self.assertEqual(str(exc), "Malformed HTML file")
                else:
                    for event in self.assertStaticShell(output):
                        self.assertEqual(event[0], "text")

    def test_html_refuses_entity_encoded_control_and_excessive_nesting(self):
        for data in (b"<p>&#x202E;hidden", b"<div>" * 65):
            with self.subTest(data=data), self.assertRaises(formats.InvalidFormat):
                formats.prepare("html", data)

    def test_html_accepts_limit_and_bounds_escape_expansion(self):
        data = b"&" * (256 * 1024)
        output = formats.prepare("html", data)
        self.assertEqual(output.count(b"&amp;"), len(data))
        self.assertLessEqual(len(output), formats.MAX_HTML_OUTPUT_BYTES)
        self.assertEqual(
            self.assertStaticShell(output), [("text", f"\n{data.decode()}\n")]
        )
        for kind in ("txt", "md"):
            self.assertEqual(formats.prepare(kind, data), data)


if __name__ == "__main__":
    unittest.main()
