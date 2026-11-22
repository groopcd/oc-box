"""Shape bounded untrusted bytes before a consented host file open."""

import re
import unicodedata
from html import escape
from html.parser import HTMLParser
from types import MappingProxyType

if __package__:
    from . import file_preview_png
else:
    import file_preview_png


MIME_TYPES = MappingProxyType(
    {
        "png": frozenset({"image/png"}),
        "txt": frozenset({"text/plain"}),
        # shared-mime-info lists text/x-markdown as a text/markdown alias.
        "md": frozenset({"text/markdown", "text/x-markdown", "text/plain"}),
        "html": frozenset({"text/html"}),
    }
)
MAX_PNG_BYTES = file_preview_png.MAX_CANONICAL
MAX_TEXT_BYTES = 256 * 1024
# Escaping an ampersand expands it fivefold. Leave room for the fixed shell.
MAX_HTML_OUTPUT_BYTES = 6 * MAX_TEXT_BYTES
_MAX_HTML_DEPTH = 64
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_DESKTOP_ENTRY = re.compile(r"\[\s*(?:kde\s+)?desktop\s+entry\s*\]", re.IGNORECASE)

_HTML_BEGIN = (
    '<!doctype html>\n<html><head><meta charset="utf-8">\n'
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; base-uri 'none'; form-action 'none'\">\n"
    "<title>Workspace file</title></head><body>\n"
)
_HTML_END = "\n</body></html>\n"
_STRUCTURAL_TAGS = frozenset(
    """
    article section main header footer aside nav div span
    h1 h2 h3 h4 h5 h6 p blockquote pre code kbd samp
    strong em b i u s small sub sup mark
    ol ul li dl dt dd figure figcaption
    table caption thead tbody tfoot tr th td
    """.split()
)
_VOID_TAGS = frozenset({"br", "hr"})
_DISCARD_CONTENT = frozenset(
    """
    head title script style iframe object template svg math textarea
    xmp plaintext noembed noframes noscript
    """.split()
)


class InvalidFormat(ValueError):
    """Fixed refusal messages that never contain source bytes or markup."""


def _check_controls(text):
    if any(
        char not in "\t\r\n" and unicodedata.category(char) in _CONTROL_CATEGORIES
        for char in text
    ):
        raise InvalidFormat("File contains disallowed Unicode controls")


def _check_text_header(text):
    # Blank lines and comments must not conceal a desktop-entry group header.
    for line in text.splitlines():
        first = line.lstrip()
        if first.startswith(("#!", "%!PS")) or _DESKTOP_ENTRY.match(first):
            raise InvalidFormat("File has an executable or desktop-entry header")
        if first and not first.startswith("#"):
            break


class _StaticHTML(HTMLParser):
    """Project parser events onto a fixed vocabulary, never copying source tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._output = bytearray(_HTML_BEGIN, "utf-8")
        self._open_tags = []
        self._discard_tag = None
        self._discard_depth = 0

    def _emit(self, markup):
        data = markup.encode("utf-8")
        if len(self._output) + len(data) > MAX_HTML_OUTPUT_BYTES:
            raise InvalidFormat("Static HTML exceeds the output byte limit")
        self._output.extend(data)

    def handle_starttag(self, tag, attrs):
        if self._discard_tag is not None:
            if tag == self._discard_tag:
                self._discard_depth += 1
        elif tag in _DISCARD_CONTENT:
            self._discard_tag = tag
            self._discard_depth = 1
        elif tag in _STRUCTURAL_TAGS:
            if len(self._open_tags) >= _MAX_HTML_DEPTH:
                raise InvalidFormat("Static HTML exceeds the nesting limit")
            self._open_tags.append(tag)
            self._emit(f"<{tag}>")
        elif tag in _VOID_TAGS:
            self._emit(f"<{tag}>")
        # All other tags are unwrapped: a link can leave text, but not a link.

    def handle_endtag(self, tag):
        if self._discard_tag is not None:
            if tag == self._discard_tag:
                self._discard_depth -= 1
                if not self._discard_depth:
                    self._discard_tag = None
        elif tag in self._open_tags:
            while self._open_tags:
                closing = self._open_tags.pop()
                self._emit(f"</{closing}>")
                if closing == tag:
                    break

    def handle_startendtag(self, tag, attrs):
        if self._discard_tag is None and (tag in _STRUCTURAL_TAGS or tag in _VOID_TAGS):
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._discard_tag is None:
            # Character references can introduce controls absent in the source.
            _check_controls(data)
            self._emit(escape(data, quote=False))

    def result(self):
        while self._open_tags:
            self._emit(f"</{self._open_tags.pop()}>")
        self._emit(_HTML_END)
        return bytes(self._output)


def prepare(kind: str, data: bytes | bytearray) -> bytes:
    """Validate a PNG, preserve safe text/Markdown, or rebuild static HTML.

    The caller must still verify the prepared file's desktop MIME against
    MIME_TYPES[kind]. This function performs no I/O or desktop detection.
    """
    if not isinstance(kind, str) or kind not in MIME_TYPES:
        raise InvalidFormat("Unsupported file kind")
    limit = MAX_PNG_BYTES if kind == "png" else MAX_TEXT_BYTES
    if not isinstance(data, (bytes, bytearray)) or len(data) > limit:
        raise InvalidFormat("Invalid file bytes or input byte limit exceeded")
    data = bytes(data)
    if kind == "png":
        try:
            file_preview_png.validate_canonical(data)
        except file_preview_png.InvalidPNG:
            raise InvalidFormat("Invalid canonical PNG") from None
        return data

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidFormat("File is not valid UTF-8") from None
    _check_controls(text)
    if kind in ("txt", "md"):
        _check_text_header(text)
        return data

    parser = _StaticHTML()
    try:
        parser.feed(text)
        parser.close()
    except InvalidFormat:
        raise
    except (AssertionError, ValueError, OverflowError):
        # Older HTMLParser versions may raise on malformed declarations or
        # oversized numeric references. Do not surface parser/source details.
        raise InvalidFormat("Malformed HTML file") from None
    return parser.result()
