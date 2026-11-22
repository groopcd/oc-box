"""Plain terminal tables for public CLI reports; no optional dependencies."""

import json
import unicodedata


def cell(value):
    """Keep control characters in report data from affecting the terminal."""
    if value is None:
        return "—"
    if type(value) is bool:
        return "yes" if value else "no"
    return "".join(
        char if char.isprintable() else json.dumps(char, ensure_ascii=True)[1:-1]
        for char in str(value)
    )


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


def fields(mapping):
    """Display an already-projected JSON object as a field/value table."""
    pending = list(reversed(tuple(mapping.items())))
    rows = []
    while pending:
        label, value = pending.pop()
        if isinstance(value, dict) and value:
            pending.extend(
                (f"{label}.{name}", item) for name, item in reversed(value.items())
            )
        elif isinstance(value, (list, tuple)) and value:
            pending.extend(
                (f"{label}[{index + 1}]", value[index])
                for index in reversed(range(len(value)))
            )
        else:
            rows.append((label, "—" if value in ({}, [], ()) else value))
    return render(("FIELD", "VALUE"), rows)
