"""Plain terminal tables for public CLI reports; no optional dependencies."""

import json
from itertools import islice
import re
import sys
import unicodedata

_LABEL_LIMIT = 160
_VALUE_LIMIT = 1024
_ROW_LIMIT = 512
_DEPTH_LIMIT = 8
_OPTION = re.compile(r"--[a-z][a-z0-9-]{0,62}", re.ASCII)


def cell(value):
    """Keep control characters in report data from affecting the terminal."""
    if value is None:
        return "-"
    if type(value) is bool:
        return "yes" if value else "no"
    text = "".join(
        char if char.isprintable() else json.dumps(char, ensure_ascii=True)[1:-1]
        for char in str(value)
    )
    encoding = getattr(sys.stdout, "encoding", None)
    if isinstance(encoding, str):
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    return text


def redacted_options(values):
    """Keep syntactic option names only; never render saved argument values."""
    if not isinstance(values, (list, tuple)):
        return ["[options omitted]"]
    result = []
    for value in islice(values, _ROW_LIMIT):
        name, separator, _ = (
            value.partition("=") if isinstance(value, str) else ("", "", "")
        )
        result.append(
            name + "=[value omitted]"
            if separator and _OPTION.fullmatch(name)
            else "[option omitted]"
        )
    if len(values) > _ROW_LIMIT:
        result.append("[additional options omitted]")
    return result


def _width(text):
    return sum(
        0
        if unicodedata.category(char) in {"Mn", "Me"}
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


def render(headers, rows, *, widths=None, heading=True):
    """Render a plain table, with optional fixed minimum widths for streaming."""
    headers = tuple(map(cell, headers))
    values = [tuple(map(cell, row)) for row in rows]
    if not headers or any(len(row) != len(headers) for row in values):
        raise ValueError("Invalid CLI table shape")
    if widths is None:
        widths = tuple(
            max(_width(row[index]) for row in (headers, *values))
            for index in range(len(headers) - 1)
        )
    else:
        widths = tuple(widths)[: len(headers) - 1]
        if len(widths) != len(headers) - 1:
            raise ValueError("Invalid CLI table widths")

    def line(row):
        padded = (
            text + " " * max(0, width - _width(text))
            for text, width in zip(row, widths)
        )
        return "  ".join((*padded, row[-1]))

    return "\n".join(line(row) for row in ([headers, *values] if heading else values))


def _short(text, limit):
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _field_rows(label, value, depth):
    # Keep path expansion bounded before padding; do not materialize a list of
    # every child of a wide or deeply nested input.
    if isinstance(value, (dict, list, tuple)):
        if not value:
            yield label, "-"
        elif depth >= _DEPTH_LIMIT:
            yield label, "[nested fields omitted]"
        elif isinstance(value, dict):
            for name, item in value.items():
                child = _short(
                    label + "." + _short(str(name), _LABEL_LIMIT), _LABEL_LIMIT
                )
                yield from _field_rows(child, item, depth + 1)
        else:
            for index, item in enumerate(value, 1):
                child = _short(f"{label}[{index}]", _LABEL_LIMIT)
                yield from _field_rows(child, item, depth + 1)
    else:
        yield (
            label,
            _short(str(value), _VALUE_LIMIT)
            if value is not None and type(value) is not bool
            else value,
        )


def fields(mapping):
    """Display a bounded field/value projection of an already selected report."""
    values = (
        row
        for key, value in mapping.items()
        for row in _field_rows(_short(str(key), _LABEL_LIMIT), value, 0)
    )
    rows = list(islice(values, _ROW_LIMIT))
    if next(values, None) is not None:
        rows.append(("-", "[additional fields omitted]"))
    return render(("FIELD", "VALUE"), rows)
