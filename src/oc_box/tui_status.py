"""Compact, cell-bounded diagnostics and fixed-scale resource meters."""

from __future__ import annotations

import math
from pathlib import Path

from rich.style import Style
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


def boxed_report(content, title, width, border_style):
    """Draw captions as selectable content, not an unselectable CSS border."""
    if width < 4:
        return _lines([title, *content.split("\n")], max(1, width))
    caption = _line(" " + title + " ", width - 4)
    caption.stylize(border_style)
    result = Text(no_wrap=True)
    result.append("╭─", style=border_style)
    result.append_text(caption)
    result.append("─" * (width - 3 - caption.cell_len) + "╮", style=border_style)
    for row in content.split("\n"):
        row = _line(row, width - 2)
        result.append("\n│", style=border_style)
        result.append_text(row)
        result.append(" " * (width - 2 - row.cell_len))
        result.append("│", style=border_style)
    result.append("\n╰" + "─" * (width - 2) + "╯", style=border_style)
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
            row.append("█" * filled, style=f"{color} on {color}")
            row.append("░" * (bar_width - filled), style="grey35 on grey15")
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

    COMPONENT_CLASSES = {"monitor--border"}
    DEFAULT_CSS = """
    MonitorPanel { border: none; }
    MonitorPanel .monitor--border { color: $primary; }
    """

    def __init__(self, kind, **kwargs):
        super().__init__("", markup=False, **kwargs)
        self.kind = kind
        self.snapshot = None
        self.cpu_history = ()
        self.ram_history = ()
        self.caption = (
            "System (OS-reported)" if kind == "system" else "Full workspace diagnostics"
        )

    def update_snapshot(self, snapshot, cpu_history=(), ram_history=()):
        self.snapshot = snapshot
        self.cpu_history, self.ram_history = tuple(cpu_history), tuple(ram_history)
        self._render_snapshot()

    def _render_snapshot(self):
        width = max(1, self.content_size.width)
        if self.kind == "system":
            content = system_report(
                self.snapshot or {},
                self.cpu_history,
                self.ram_history,
                width=max(1, width - 2),
            )
        else:
            content = workspace_report(self.snapshot, width=max(1, width - 2))
        self.update(
            boxed_report(
                content,
                self.caption,
                width,
                self.get_component_rich_style("monitor--border"),
            )
        )

    def on_resize(self):
        self._render_snapshot()


def _card_percentage(used, total):
    """Finite, capped gauge value; zero capacity is not a measured zero."""
    used, total = numeric(used), numeric(total)
    if used is None or total is None or total == 0:
        return None
    return min(1.0, used / total) * 100


def _card_memory(used, total, *, unlimited=False):
    used, total = numeric(used), numeric(total)
    if used is None:
        return "n/a"
    capacity = "unlimited" if total == 0 and unlimited else byte_size(total or None)
    return f"{byte_size(used)} / {capacity}"


def _card_count(value):
    return str(value) if type(value) is int and numeric(value) is not None else "n/a"


def _card_bar(value, width, color, muted, caption=""):
    """A compact fixed-scale bar with an optional, unscaled numeric caption."""
    value = numeric(value)
    reserved = Text(caption).cell_len + 1 if caption else 0
    bar_width = max(0, min(32, width - reserved))
    result = Text(no_wrap=True)
    if bar_width >= 4:
        if value is None:
            result.append("·" * bar_width, style=muted)
        else:
            filled = round(min(value, 100) / 100 * bar_width)
            result.append("█" * filled, style=color)
            result.append("░" * (bar_width - filled), style=muted)
        if caption:
            result.append(" ")
    result.append(caption, style=muted)
    return _line(result, width)


class _CardPanel(MonitorPanel):
    """One selectable Static, using theme components rather than palette literals."""

    COMPONENT_CLASSES = MonitorPanel.COMPONENT_CLASSES | {
        "card--title",
        "card--muted",
        "card--cpu",
        "card--memory",
        "card--storage",
        "card--border",
    }
    DEFAULT_CSS = """
    _CardPanel { height: auto; border: none; }
    _CardPanel .card--title { color: $accent; text-style: bold; }
    _CardPanel .card--muted { color: $text-muted; }
    _CardPanel .card--cpu { color: $success; }
    _CardPanel .card--memory { color: $accent; }
    _CardPanel .card--storage { color: $warning; }
    _CardPanel .card--border { color: $primary; }
    """

    def _card_styles(self):
        return {
            name: self.get_component_rich_style(
                f"card--{name}", default=Style.parse(fallback)
            )
            for name, fallback in (
                ("title", "bold cyan"),
                ("muted", "dim"),
                ("cpu", "green"),
                ("memory", "cyan"),
                ("storage", "yellow"),
                ("border", "blue"),
            )
        }

    def _card_box(self, rows, title, width, styles):
        inner = max(1, width - 4)
        padded = [Text(" ") + _line(row, inner) + Text(" ") for row in rows]
        result = boxed_report(
            _lines(padded, max(1, width - 2)), title, width, styles["border"]
        )
        return _lines(result.split("\n"), width)


class HostResourceCards(_CardPanel):
    """Host readings plus copied registration counts; never queries a backend.

    ``workspace_counts`` is optional internal presentation metadata on the host
    snapshot, with exact nonnegative integer ``running``/``registered`` values.
    """

    def __init__(self, kind="system", **kwargs):
        kwargs.setdefault("id", "system-report")
        super().__init__(kind, **kwargs)
        self.caption = "Host resources (OS-reported)"

    def _render_snapshot(self):
        width = max(1, self.content_size.width)
        styles = self._card_styles()
        host = self.snapshot or {}
        cpu = numeric(host.get("cpu_percent"))
        memory = (host.get("memory_bytes"), host.get("memory_total_bytes"))
        filesystem = host.get("filesystem") or {}
        storage = (filesystem.get("used_bytes"), filesystem.get("total_bytes"))
        counts = host.get("workspace_counts") or {}
        running = _card_count(counts.get("running"))
        registered = _card_count(counts.get("registered"))
        readings = (
            ("HOST CPU", cpu, "n/a" if cpu is None else f"{cpu:.1f}%", "cpu"),
            ("HOST RAM", _card_percentage(*memory), _card_memory(*memory), "memory"),
            (
                "HOST HOME FS",
                _card_percentage(*storage),
                _card_memory(*storage),
                "storage",
            ),
        )
        if width >= 104:
            card_width, remainder = divmod(width - 3, 4)
            widths = [card_width + (index < remainder) for index in range(4)]
            cards = []
            for index, (title, value, caption, color) in enumerate(readings):
                inner = widths[index] - 4
                history = self.cpu_history if index == 0 else self.ram_history
                history_row = Text(
                    "CPU " if index == 0 else "RAM ", style=styles["muted"]
                )
                history_row.append(
                    spark(history, max(0, inner - 4)), style=styles[color]
                )
                rows = [
                    Text(caption, style=styles["title"]),
                    _card_bar(
                        value,
                        inner,
                        styles[color],
                        styles["muted"],
                        ""
                        if index == 0
                        else ("n/a" if value is None else f"{value:.0f}%"),
                    ),
                    history_row
                    if index < 2
                    else Text("OS-reported capacity", style=styles["muted"]),
                    Text(
                        "whole host · 0–100%"
                        if index == 0
                        else "0–100% · gaps = n/a"
                        if index == 1
                        else "not oc-box allocation",
                        style=styles["muted"],
                    ),
                ]
                cards.append(self._card_box(rows, title, widths[index], styles))
            cards.append(
                self._card_box(
                    [
                        Text(f"{running} / {registered}", style=styles["title"]),
                        Text("running / registered", style=styles["muted"]),
                        Text("registration metadata", style=styles["muted"]),
                        Text(""),
                    ],
                    "WORKSPACES",
                    widths[3],
                    styles,
                )
            )
            rows = []
            columns = [card.split("\n") for card in cards]
            for line_index in range(len(columns[0])):
                row = Text()
                for index, column in enumerate(columns):
                    if index:
                        row.append(" ")
                    row.append_text(column[line_index])
                rows.append(row)
            self.update(_lines(rows, width))
            return

        inner = max(1, width - 4)
        # Compact layouts keep the readings, rather than redundant bar rows.
        # Histories below retain the same fixed scale as the wide cards.
        resources = Text(f"CPU {readings[0][2]} whole host", style=styles["cpu"])
        resources.append(" · RAM " + readings[1][2], style=styles["memory"])
        rows = [resources]
        filesystem_row = Text("Home FS " + readings[2][2], style=styles["storage"])
        workspace_row = Text(
            f"Workspaces {running}/{registered} running/registered",
            style=styles["title"],
        )
        if filesystem_row.cell_len + workspace_row.cell_len + 3 <= inner:
            filesystem_row.append(" · ")
            filesystem_row.append_text(workspace_row)
            rows.append(filesystem_row)
        else:
            rows.extend([filesystem_row, workspace_row])
        history_suffix = " · 0–100%; gaps=n/a"
        history_width = max(
            0, min(24, (inner - 10 - Text(history_suffix).cell_len) // 2)
        )
        history = Text("CPU ", style=styles["muted"])
        history.append(
            spark(self.cpu_history, history_width).ljust(history_width),
            style=styles["cpu"],
        )
        history.append("  RAM ", style=styles["muted"])
        history.append(
            spark(self.ram_history, history_width).ljust(history_width),
            style=styles["memory"],
        )
        history.append(history_suffix, style=styles["muted"])
        rows.append(history)
        self.update(self._card_box(rows, self.caption, width, styles))


class WorkspaceInspector(_CardPanel):
    """Selected metadata and observed resource use, with no readiness inference."""

    def __init__(self, kind="workspace", **kwargs):
        kwargs.setdefault("id", "full-diagnostics")
        super().__init__(kind, **kwargs)
        self.caption = "Selected workspace"

    def _render_snapshot(self):
        width = max(1, self.content_size.width)
        inner = max(1, width - 4)
        styles = self._card_styles()
        item = self.snapshot
        if not item:
            rows = [Text("No workspace selected.", style=styles["muted"])]
        else:
            metrics = item.get("metrics") or {}
            limits = item.get("effective_limits") or {}
            cpu, quota = (
                numeric(metrics.get("cpu_percent")),
                numeric(limits.get("cpu_cores")),
            )
            # Raw CPU 100% is one core; only the gauge uses observed capacity.
            cpu_gauge = _card_percentage(None if cpu is None else cpu / 100, quota)
            quota_caption = (
                f"{quota:g} CPUs quota"
                if quota
                else ("unlimited" if quota == 0 else "quota n/a")
            )
            memory = (metrics.get("memory_bytes"), limits.get("memory_bytes"))
            ram_gauge = _card_percentage(*memory)
            project = Path(plain(item.get("project") or "n/a")).name or "n/a"
            os_caption = "OS " + plain(item.get("distro") or "n/a")
            version = item.get("opencode_version") or metrics.get("opencode_version")
            if version:
                os_caption += " · OpenCode " + plain(version)
            rows = [
                Text(
                    f"{project} · {plain(item.get('state') or 'n/a')}",
                    style=styles["title"],
                ),
                Text(os_caption, style=styles["muted"]),
                Text(
                    f"CPU {'n/a' if cpu is None else f'{cpu:.1f}%'} · {quota_caption}",
                    style=styles["cpu"],
                ),
                _card_bar(
                    cpu_gauge,
                    inner,
                    styles["cpu"],
                    styles["muted"],
                    "quota n/a" if cpu_gauge is None else f"{cpu_gauge:.0f}% of quota",
                ),
                Text(
                    "CPU 100% = 1 core; gauge uses observed quota",
                    style=styles["muted"],
                ),
                Text(
                    "RAM " + _card_memory(*memory, unlimited=True),
                    style=styles["memory"],
                ),
                _card_bar(
                    ram_gauge,
                    inner,
                    styles["memory"],
                    styles["muted"],
                    "limit n/a" if ram_gauge is None else f"{ram_gauge:.0f}% of limit",
                ),
                Text(
                    "Processes " + _card_count(metrics.get("pids")),
                    style=styles["muted"],
                ),
                Text(
                    "Recorded sessions " + _card_count(item.get("recorded_sessions")),
                    style=styles["muted"],
                ),
            ]
            home = numeric((item.get("storage") or {}).get("private_home_bytes"))
            rows.append(
                Text(
                    "Private home "
                    + (
                        "n/a · scan required"
                        if home is None
                        else byte_size(home) + " · scanned"
                    ),
                    style=styles["storage"],
                )
            )
        self.update(self._card_box(rows, self.caption, width, styles))
