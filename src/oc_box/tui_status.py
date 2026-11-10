"""Compact, cell-bounded diagnostics and fixed-scale resource meters."""

from __future__ import annotations

import math
from pathlib import Path

from rich.text import Text
from textual.widgets import Static

if __package__:
    from .status_report import byte_size
    from .tui_backend import plain
else:
    from status_report import byte_size
    from tui_backend import plain

SPARKS = "▁▂▃▄▅▆▇█"


def numeric(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value) and value >= 0:
                return float(value)
        except OverflowError:
            pass
    return None


def percentage(used, total):
    used, total = numeric(used), numeric(total)
    return 100 * used / total if used is not None and total else None


def spark(values, width):
    """One cell per sample; unknown is blank and the scale is always 0–100%."""
    result = []
    for value in list(values)[-max(0, width) :] if width > 0 else []:
        value = numeric(value)
        result.append(
            " " if value is None else SPARKS[round(min(value, 100) / 100 * 7)]
        )
    return "".join(result)


def _line(value, width, style=None):
    row = (
        value.copy()
        if isinstance(value, Text)
        else Text(plain(value), style=style or "")
    )
    row.no_wrap = True
    row.overflow = "ellipsis"
    row.truncate(max(1, width), overflow="ellipsis")
    return row


def _lines(rows, width):
    result = Text(no_wrap=True, overflow="ellipsis")
    for index, row in enumerate(rows):
        if index:
            result.append("\n")
        result.append_text(_line(row, width))
    return result


def meters(rows, width):
    """Render equal-width bars, reserving room for each row's numeric caption.

    rows are (label, percentage-or-None, caption, color). A missing percentage
    must not turn into a measured zero or a full-width auto-scaled chart.
    """
    # Keep both diagnostics panels on the same meter columns, not just rows
    # within each panel. Captions reserve at least 30 cells at normal sizes.
    labels = max(7, *(len(label) for label, *_ in rows))
    captions = max(30, *(Text(caption).cell_len for _, _, caption, _ in rows))
    bar_width = max(0, min(40, width - labels - captions - 3))
    if bar_width < 4:
        return [
            _line(f"{label:<{labels}}  {caption}", width)
            for label, _, caption, _ in rows
        ]
    result = []
    for label, value, caption, color in rows:
        row = Text(f"{label:<{labels}} ", no_wrap=True)
        value = numeric(value)
        if value is None:
            row.append("·" * bar_width, style="grey35 on grey15")
        else:
            filled = min(bar_width, round(min(value, 100) / 100 * bar_width))
            row.append(" " * filled, style=f"on {color}")
            row.append(" " * (bar_width - filled), style="on grey15")
        row.append("  " + caption)
        result.append(_line(row, width))
    return result


def _memory_caption(used, total, *, unlimited=False):
    used, total = numeric(used), numeric(total)
    if used is None:
        return "n/a"
    if total == 0:
        return f"{byte_size(used)} / {'unlimited' if unlimited else 'n/a'}"
    if total is None:
        return f"{byte_size(used)} / n/a"
    return f"{byte_size(used)} / {byte_size(total)}  {100 * used / total:.0f}%"


def system_report(host, cpu_history=(), ram_history=(), *, width=80):
    cpu = numeric(host.get("cpu_percent"))
    used, total = host.get("memory_bytes"), host.get("memory_total_bytes")
    filesystem = host.get("filesystem") or {}
    rows = meters(
        [
            (
                "RAM",
                percentage(used, total),
                _memory_caption(used, total),
                "dodger_blue2",
            ),
            (
                "CPU",
                cpu,
                "n/a" if cpu is None else f"{cpu:.1f}% · whole host",
                "green3",
            ),
            (
                "Home FS",
                percentage(filesystem.get("used_bytes"), filesystem.get("total_bytes")),
                _memory_caption(
                    filesystem.get("used_bytes"), filesystem.get("total_bytes")
                ),
                "dark_orange",
            ),
        ],
        width,
    )
    available = None
    if numeric(used) is not None and numeric(total) is not None and total >= used:
        available = total - used
    rows.append(
        Text(
            f"Available RAM {byte_size(available)} · swap "
            f"{byte_size(host.get('swap_bytes'))}/{byte_size(host.get('swap_total_bytes'))}",
            style="grey62",
        )
    )
    history_width = max(4, min(24, (width - 13) // 2))
    history = Text("CPU ", style="grey62")
    history.append(
        spark(cpu_history, history_width).ljust(history_width), style="green3"
    )
    history.append("  RAM ", style="grey62")
    history.append(spark(ram_history, history_width), style="dodger_blue2")
    rows.append(history)
    rows.append(
        Text(
            "Recent samples · fixed 0–100% scale · blank = unavailable", style="grey50"
        )
    )
    return _lines(rows, width)


def workspace_report(item, *, width=80):
    if not item:
        return _lines(
            ["No workspace selected.", "Host monitoring remains available below."],
            width,
        )
    metrics = item.get("metrics") or {}
    limits = item.get("effective_limits") or {}
    cpu = numeric(metrics.get("cpu_percent"))
    quota = numeric(limits.get("cpu_cores"))
    capacity = quota * 100 if quota else None
    cpu_caption = "n/a" if cpu is None else f"{cpu:.1f}%"
    if quota:
        cpu_caption += f" / {quota:g} CPUs"
    elif cpu is not None:
        cpu_caption += " / " + ("unlimited" if quota == 0 else "quota n/a")
    rows = [
        Text(
            f"{plain(Path(item['project']).name)} · {plain(item.get('state', 'n/a'))} · "
            f"{plain(item.get('distro', 'n/a'))}",
            style="bold",
        )
    ]
    rows.extend(
        meters(
            [
                (
                    "RAM",
                    percentage(metrics.get("memory_bytes"), limits.get("memory_bytes")),
                    _memory_caption(
                        metrics.get("memory_bytes"),
                        limits.get("memory_bytes"),
                        unlimited=True,
                    ),
                    "dodger_blue2",
                ),
                ("CPU", percentage(cpu, capacity), cpu_caption, "green3"),
            ],
            width,
        )
    )
    tasks = metrics.get("pids")
    rows.append(
        Text(
            f"Tasks {tasks if tasks is not None else 'n/a'} · "
            f"sessions {item.get('recorded_sessions', 0)} · "
            f"identity {plain(item.get('project_status', 'n/a'))}",
            style="grey62",
        )
    )
    rows.append(Text("CPU: 100% = one core; bar uses observed quota", style="grey50"))
    return _lines(rows, width)


class MonitorPanel(Static):
    """A resize-aware panel; its meters never inherit Sparkline auto-scaling."""

    def __init__(self, kind, **kwargs):
        super().__init__("", markup=False, **kwargs)
        self.kind = kind
        self.snapshot = None
        self.cpu_history = ()
        self.ram_history = ()
        self.border_title = (
            "System (OS-reported)" if kind == "system" else "Full workspace diagnostics"
        )

    def update_snapshot(self, snapshot, cpu_history=(), ram_history=()):
        self.snapshot = snapshot
        self.cpu_history, self.ram_history = tuple(cpu_history), tuple(ram_history)
        self._render_snapshot()

    def _render_snapshot(self):
        width = max(1, self.content_size.width)
        if self.kind == "system":
            self.update(
                system_report(
                    self.snapshot or {}, self.cpu_history, self.ram_history, width=width
                )
            )
        else:
            self.update(workspace_report(self.snapshot, width=width))

    def on_resize(self):
        self._render_snapshot()
