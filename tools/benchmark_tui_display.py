#!/usr/bin/env python3
"""Synthetic headless dashboard work counts; never inspect host runtime/state.

Run with the optional TUI dependencies installed. Counts are regression oracles;
elapsed times are descriptive only, not CI timing thresholds. No user text,
terminal output, session contents, or real store paths are collected.
"""

import asyncio
import copy
import json
import time
from contextlib import ExitStack
from unittest.mock import Mock, patch

from textual.widgets import DataTable, Input, RichLog

from oc_box.tui import DemoBackend
from oc_box.tui_app import DashboardApp
from oc_box.tui_logs import LogEntry
from oc_box.tui_status import MonitorPanel


class SyntheticBackend(DemoBackend):
    def __init__(self):
        self.samples = self.log_samples = 0

    def collect(self, *, scan_storage=False):
        self.samples += 1
        report = super().collect(scan_storage=scan_storage)
        report["sampled_at"] = "2026-01-01T12:00:00Z"
        template = report["workspaces"][0]
        report["workspaces"] = []
        for index in range(32):
            item = copy.deepcopy(template)
            item["workspace_id"] = f"{index:032x}"
            item["project"] = f"/example/projects/project-{index:02}"
            report["workspaces"].append(item)
        return report

    def logs(self, workspace=None):
        self.log_samples += 1
        return [
            LogEntry(
                f"2026-01-01T12:{index // 60:02}:{index % 60:02}Z",
                "oc-box",
                "info",
                f"Synthetic log {index:04}",
            )
            for index in range(2000)
        ]


def synthetic_sessions():
    return [
        {
            "id": f"ses_{index:04}",
            "title": f"Synthetic {index:04}",
            "location": {"directory": "/example/projects/project-00"},
            "time": {"created": 1, "updated": 1000 - index},
        }
        for index in range(1000)
    ]


async def benchmark():
    backend = SyntheticBackend()
    app = DashboardApp(backend, read_only=True, demo=True)
    async with app.run_test(size=(140, 45)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_timer.pause()
        app.catalog = Mock(origin="host", store="synthetic-store")
        app.sessions = synthetic_sessions()
        # A fake metadata store also counts the old render-time I/O path.
        app.favorites = Mock()
        app.favorites.list.return_value = []
        app._render_sessions()
        app.action_tab("sessions")
        await pilot.pause()
        workspace = app.query_one("#workspaces", DataTable)
        sessions = app.query_one("#session-table", DataTable)
        logs = app.query_one("#log-output", RichLog)
        panels = list(app.query(MonitorPanel))
        observations = {}

        async def measure(name, operation, *, settle=False):
            with ExitStack() as stack:
                counters = {}
                targets = {
                    "workspace_clear": (workspace, "clear"),
                    "workspace_add": (workspace, "add_row"),
                    "workspace_update": (workspace, "update_cell"),
                    "session_clear": (sessions, "clear"),
                    "session_add": (sessions, "add_row"),
                    "session_update": (sessions, "update_cell"),
                    "session_render_line": (sessions, "render_line"),
                    "log_clear": (logs, "clear"),
                    "log_write": (logs, "write"),
                    "log_search": (app.logs, "search"),
                    "query_one": (app, "query_one"),
                }
                for key, (target, method) in targets.items():
                    counters[key] = stack.enter_context(
                        patch.object(target, method, wraps=getattr(target, method))
                    )
                monitor_updates = [
                    stack.enter_context(patch.object(p, "update", wraps=p.update))
                    for p in panels
                ]
                app.favorites.list.reset_mock()
                before_samples = backend.samples, backend.log_samples
                started = time.perf_counter()
                operation()
                dispatch_ms = (time.perf_counter() - started) * 1000
                if settle:
                    await pilot.pause(0.35)
                observations[name] = {
                    **{key: mock.call_count for key, mock in counters.items()},
                    "monitor_update": sum(m.call_count for m in monitor_updates),
                    "favorite_reads": app.favorites.list.call_count,
                    "backend_queries": backend.samples
                    + backend.log_samples
                    - sum(before_samples),
                    "dispatch_ms": round(dispatch_ms, 2),
                    "settled_ms": round((time.perf_counter() - started) * 1000, 2),
                }

        report = backend.collect()
        entries = backend.logs()
        await measure(
            "ten_unchanged_reports",
            lambda: [
                app._apply_report(copy.deepcopy(report), False) for _ in range(10)
            ],
        )
        await measure(
            "ten_unchanged_session_renders",
            lambda: [app._render_sessions() for _ in range(10)],
        )

        def burst(ident, final):
            field = app.query_one(ident, Input)
            for index in range(1, len(final) + 1):
                value = final[:index]
                with field.prevent(Input.Changed):
                    field.value = value
                app.on_input_changed(Input.Changed(field, value))

        await measure(
            "session_typing_burst",
            lambda: burst("#session-search", "Synthetic 099"),
            settle=True,
        )
        app.action_tab("logs")
        await pilot.pause()
        await pilot.click("#log-mode")
        await measure(
            "five_unchanged_log_snapshots",
            lambda: [app._apply_logs(entries) for _ in range(5)],
        )
        await measure(
            "log_typing_burst",
            lambda: burst("#log-search", "Synthetic log 199"),
            settle=True,
        )
        return {"fixture": "32 workspaces / 1000 sessions / 2000 logs", **observations}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(benchmark()), sort_keys=True, indent=2))
