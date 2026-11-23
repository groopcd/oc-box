#!/usr/bin/env python3
"""Render documentation images from the real dashboard and in-memory fixtures.

Run from the repository root with the optional TUI dependencies and Pillow.
Never connects to OpenCode, Podman, a shell, a clipboard or the host registry.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
from rich.cells import cell_len
from textual.widgets import Input, Select, Static

from oc_box.tui import DemoBackend
from oc_box.tui_app import DashboardApp
from oc_box.tui_logs import LogEntry
from oc_box.tui_sessions import SessionCatalog
from oc_box.tui_storage import StorageInventory, StorageRecord

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-01-01T12:00:00Z"
GIB = 1024**3
CELL = (13, 26)
BACKGROUND = (6, 13, 20)
FOREGROUND = (217, 231, 238)
SECTIONS = (
    ("status", "Status", (144, 44)),
    ("settings", "Settings", (144, 82)),
    ("sessions", "Sessions", (144, 38)),
    ("terminals", "Terminals", (144, 34)),
    ("logs", "Logs", (144, 38)),
    ("storage", "Storage", (144, 42)),
    ("backups", "Backup / restore", (144, 52)),
)
NOTICE = (
    "Synthetic documentation preview · read-only · no host data or native connections"
)


class ScreenshotBackend(DemoBackend):
    """Fixed illustrative reports; box=None keeps native/state paths unavailable."""

    def collect(self, *, scan_storage=False):
        report = super().collect(scan_storage=True)
        report["sampled_at"] = STAMP
        report["host"].update(
            cpu_percent=18,
            memory_bytes=9 * GIB,
            memory_total_bytes=32 * GIB,
            swap_bytes=0,
            swap_total_bytes=8 * GIB,
            filesystem={
                "total_bytes": 512 * GIB,
                "used_bytes": 124 * GIB,
                "free_bytes": 388 * GIB,
            },
        )
        template = report["workspaces"][0]
        report["workspaces"] = []
        for index, (name, distro, running, cpu, ram) in enumerate(
            (
                ("orbit-web", "debian", True, 73.4, 1.8),
                ("atlas-api", "fedora", True, 124.0, 3.1),
                ("docs-site", "alpine", False, 0, 0),
                ("data-pipeline", "ubuntu", True, 38.2, 1.2),
                ("cli-tools", "tumbleweed", False, 0, 0),
                ("research-notes", "debian", False, 0, 0),
            ),
            1,
        ):
            item = deepcopy(template)
            ident, project = f"{index:032x}", "/projects/" + name
            item.update(
                workspace_id=ident,
                project=project,
                container_project=project,
                distro=distro,
                state="running" if running else "stopped",
                container_id=f"{index:064x}",
                container_name="oc-box-" + name,
                image=distro + ":synthetic",
                session_id="ses_demo_" + name.replace("-", "_"),
                recorded_sessions=14 - index,
                storage_sampled_at=STAMP if index != 6 else None,
                metrics=(
                    {
                        "cpu_percent": cpu,
                        "memory_bytes": int(ram * GIB),
                        "pids": 20 + index,
                    }
                    if running
                    else None
                ),
                mounts=[
                    {
                        "type": "bind",
                        "source": project,
                        "destination": project,
                        "writable": True,
                    }
                ],
                notes=[
                    "Synthetic documentation fixture; sizes are illustrative, not live scans."
                ],
            )
            item["storage"].update(
                project_bytes=int((0.3 + index * 0.4) * GIB) if index != 6 else None,
                private_home_bytes=int((1.2 + index * 0.8) * GIB)
                if index != 6
                else None,
                writable_layer_bytes=int(0.7 * GIB) if index != 6 else None,
            )
            report["workspaces"].append(item)
        return report

    def logs(self, workspace=None):
        commands = ("doctor", "start", "status", "sync", "list", "backup-info")
        rows = []
        for index, command in enumerate(commands):
            for step, event in enumerate(("command.started", "command.finished")):
                message = f"{event} · command={command}"
                if step:
                    message += " · outcome=success"
                rows.append(
                    LogEntry(
                        f"2026-01-01T12:00:{index * 2 + step:02}Z",
                        "oc-box",
                        "info",
                        message,
                    )
                )
        return rows

    def session_connection(self, *args, **kwargs):
        raise AssertionError("Documentation capture must not connect native services.")


class ScreenshotApp(DashboardApp):
    def _write_clipboard(self, *args, **kwargs):
        raise AssertionError("Documentation capture must not write the clipboard.")

    def copy_to_clipboard(self, *args, **kwargs):
        raise AssertionError(
            "Documentation capture must not use desktop clipboard I/O."
        )


def session_catalog():
    rows = []
    for index, (title, project, parent, archived) in enumerate(
        (
            ("Plan the workspace navigation", "orbit-web", None, False),
            ("Refine the settings page", "orbit-web", 0, False),
            ("Add API request validation", "atlas-api", None, False),
            ("Review endpoint tests", "atlas-api", 2, False),
            ("Document the release workflow", "docs-site", None, False),
            ("Explore a previous approach", "research-notes", None, True),
        )
    ):
        row = {
            "id": f"ses_demo_{index}",
            "projectID": "project_demo_" + project,
            "title": title,
            "location": {"directory": "/projects/" + project},
            "time": {"created": 1000, "updated": 2000 - index},
        }
        if parent is not None:
            row["parentID"] = f"ses_demo_{parent}"
        if archived:
            row["time"]["archived"] = 1500
        rows.append(row)

    def request(method, route, body=None):
        if method != "GET" or not route.startswith("/api/session?") or body is not None:
            raise AssertionError("Only in-memory session-list queries are available.")
        return {"data": deepcopy(rows), "cursor": {"next": None}}

    return SessionCatalog(request, "host", "synthetic-documentation-store")


def storage_inventory(report):
    state = Path("/synthetic/oc-box-state")
    return StorageInventory(
        scope="all-workspaces",
        rows=tuple(
            StorageRecord(
                workspace_id=item["workspace_id"],
                project=item["project"],
                distro=item["distro"],
                container=item["container_name"],
                home_volume="synthetic-home-" + str(index),
                image=item["image"],
                opencode_version="synthetic",
                state_root=state,
                record_path=state
                / "workspaces"
                / item["workspace_id"]
                / "workspace.json",
            )
            for index, item in enumerate(report["workspaces"])
        ),
        skipped=0,
        state_root=None,
        synthetic=True,
    )


class Renderer:
    """Rasterize actual styled compositor cells; add framing outside the UI."""

    def __init__(self):
        self.font = ImageFont.truetype("DejaVuSansMono.ttf", 20)
        self.bold = ImageFont.truetype("DejaVuSansMono-Bold.ttf", 20)
        self.title = ImageFont.truetype("DejaVuSans-Bold.ttf", 46)
        self.headline = ImageFont.truetype("DejaVuSans-Bold.ttf", 32)
        self.label = ImageFont.truetype("DejaVuSans.ttf", 22)
        self.small = ImageFont.truetype("DejaVuSans.ttf", 18)
        missing = self.font.getmask(chr(0x10FFFF))
        cross = self.font.getmask("❌")
        self.cross_fallback = missing.size == cross.size and bytes(missing) == bytes(
            cross
        )

    @staticmethod
    def color(value, fallback):
        return tuple(value.get_truecolor()) if value is not None else fallback

    def raster(self, strips, columns, rows):
        image = Image.new("RGB", (columns * CELL[0], rows * CELL[1]), BACKGROUND)
        draw = ImageDraw.Draw(image)
        for row, strip in enumerate(strips[:rows]):
            column = 0
            for segment in strip:
                style = segment.style
                fg = self.color(style.color if style else None, FOREGROUND)
                bg = self.color(style.bgcolor if style else None, BACKGROUND)
                if style and style.reverse:
                    fg, bg = bg, fg
                if style and style.dim:
                    fg = tuple(round((a + b) / 2) for a, b in zip(fg, bg))
                for character in segment.text:
                    cells = cell_len(character)
                    if cells and column < columns:
                        x, y = column * CELL[0], row * CELL[1]
                        draw.rectangle(
                            (
                                x,
                                y,
                                min(columns, column + cells) * CELL[0] - 1,
                                y + CELL[1] - 1,
                            ),
                            fill=bg,
                        )
                        if character == "❌" and self.cross_fallback:
                            # A font fallback for this actual two-cell codepoint,
                            # not a new widget or a change to the captured UI.
                            draw.line((x + 5, y + 5, x + 20, y + 20), fill=fg, width=5)
                            draw.line((x + 20, y + 5, x + 5, y + 20), fill=fg, width=5)
                        else:
                            draw.text(
                                (x, y),
                                character,
                                font=self.bold if style and style.bold else self.font,
                                fill=fg,
                            )
                        if style and style.underline:
                            draw.line(
                                (x, y + 23, x + cells * CELL[0] - 1, y + 23), fill=fg
                            )
                    column += cells
        return image

    def frame(self, screenshot, title):
        canvas = Image.new(
            "RGB", (screenshot.width + 48, screenshot.height + 112), "#08111b"
        )
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            (12, 12, canvas.width - 13, canvas.height - 13),
            radius=16,
            fill="#0b1720",
            outline="#244451",
            width=2,
        )
        draw.text((30, 25), "oc-box  /  " + title, font=self.label, fill="#66d5e5")
        draw.text(
            (canvas.width - 385, 28),
            "SYNTHETIC READ-ONLY PREVIEW",
            font=self.small,
            fill="#9db0bf",
        )
        canvas.paste(screenshot, (24, 64))
        draw.text(
            (30, canvas.height - 36),
            "Actual Textual UI · fixture data only · no native connection or host operation",
            font=self.small,
            fill="#9db0bf",
        )
        return canvas

    def hero(self, screenshot):
        canvas = Image.new(
            "RGB", (screenshot.width + 112, screenshot.height + 250), "#07111b"
        )
        draw = ImageDraw.Draw(canvas)
        draw.text((56, 30), "oc-box", font=self.title, fill="#62d7e6")
        draw.text(
            (56, 91), "Give OpenCode room to build.", font=self.headline, fill="#e1edf4"
        )
        draw.text(
            (56, 128),
            "Rootless Podman   /   Unmodified OpenCode   /   Persistent workspaces",
            font=self.small,
            fill="#a0b7c8",
        )
        draw.rounded_rectangle(
            (42, 179, canvas.width - 43, canvas.height - 39),
            radius=14,
            outline="#295565",
            width=2,
            fill="#0b1720",
        )
        canvas.paste(screenshot, (56, 193))
        draw.text(
            (56, canvas.height - 31),
            "Experimental dashboard · synthetic read-only preview · no live host data",
            font=self.small,
            fill="#a0b7c8",
        )
        return canvas


def save(image, path, description):
    info = PngImagePlugin.PngInfo()
    info.add_text("Description", description)
    info.add_text(
        "Source", "Actual oc-box Textual compositor with synthetic in-memory fixtures"
    )
    image.save(path, pnginfo=info, optimize=True)
    return {
        "pixels": list(image.size),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


async def capture(output, force):
    paths = [output / "tui" / (key + ".png") for key, _, _ in SECTIONS]
    paths += [output / "readme-hero.png", output / "tui" / "manifest.json"]
    if not force and any(path.exists() for path in paths):
        raise SystemExit(
            "Capture files already exist; use --force to replace only this known image set."
        )
    (output / "tui").mkdir(parents=True, exist_ok=True)
    renderer = Renderer()
    backend = ScreenshotBackend()
    app = ScreenshotApp(backend, demo=True, read_only=True)
    assert backend.box is None
    records = {}
    async with app.run_test(size=SECTIONS[0][2]) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app._poll_timer.pause()
        app.cpu_history = [14, 18, 16, 24, 21, 29, 25, 32, 28, 34, 27, 24, 20, 26, 22]
        app.ram_history = [22, 23, 24, 23, 25, 26, 27, 28, 27, 28, 29, 28, 27, 28, 28]
        report = backend.collect()
        app._apply_report(report, False)
        app._apply_management(storage_inventory(report))
        overview = None
        for key, title, size in SECTIONS:
            await pilot.resize_terminal(*size)
            app.action_tab(key)
            if key == "sessions":
                catalog = session_catalog()
                app._apply_sessions(catalog, catalog.list())
            if key == "terminals":
                app.query_one("#terminal-info", Static).update(
                    "Synthetic display fixture — disconnected. No native client, shell, or workspace lease is open."
                )
            if key == "logs":
                app.query_one("#log-search", Input).value = "command.finished"
            else:
                app.query_one("#log-search", Input).value = ""
            if key == "storage":
                app.query_one("#storage-scope", Select).value = "all"
            if key == "backups":
                app.query_one("#backup-file", Input).value = "/backups/orbit-web.ocbox"
                app.query_one(
                    "#backup-destination", Input
                ).value = "/projects/orbit-web-restored"
            app.set_focus(None)
            await pilot.pause(0.4)
            if key == "sessions":
                app.query_one("#session-progress-text", Static).update(
                    "Synthetic metadata catalog · native connections are disabled in this preview."
                )
                app.query_one("#session-details", Static).update(
                    "Synthetic metadata preview — no native service connected; no ownership or activity checks performed.\n"
                    "Root, child and archived rows illustrate the actual metadata-only catalog."
                )
            if key == "terminals":
                app._terminal_widget.emulator.feed(
                    (
                        "\x1b[36mSYNTHETIC TERMINAL DISPLAY — DISCONNECTED\x1b[0m\r\n"
                        "\r\nNo command was executed for this documentation image.\r\n"
                        "After explicit attachment, native session-shell output appears here.\r\n"
                        "Ctrl-] detaches its client without ending the native shell.\r\n"
                    ).encode("utf-8")
                )
                app._terminal_widget.refresh()
            if app.catalog:
                app.query_one("#workspace-source-context", Static).update(
                    "Synthetic catalog fixture · no native connection or lease"
                )
            app.query_one("#notice", Static).update(NOTICE)
            await pilot.pause()
            if key == "terminals":
                assert "SYNTHETIC TERMINAL" in "\n".join(
                    app._terminal_widget.emulator.screen.display
                )
            assert app.demo and app.read_only and app.backend.box is None
            assert app._session_connection is None and app._terminal_target is None
            assert app._terminal_widget.transport.fd is None
            assert app._terminal_widget.transport.process is None
            assert app._terminal_widget.state == "disconnected"
            assert app.query_one("#content").active == key
            raster = renderer.raster(app.screen._compositor.render_strips(), *size)
            if key == "status":
                overview = raster.copy()
            path = output / "tui" / (key + ".png")
            records[key] = {
                "file": "tui/" + path.name,
                "cells": list(size),
                **save(renderer.frame(raster, title), path, NOTICE),
            }
    assert overview is not None
    records["hero"] = {
        "file": "readme-hero.png",
        **save(renderer.hero(overview), output / "readme-hero.png", NOTICE),
    }
    manifest = {
        "capture": "Real Textual compositor; synthetic in-memory data only",
        "read_only": True,
        "native_connections": 0,
        "clipboard_access": False,
        "terminal": "Disconnected; explanatory emulator fixture, not process output",
        "fonts": "DejaVu Sans Mono / DejaVu Sans; terminal font rendering can differ",
        "crossmark_font_fallback": renderer.cross_fallback,
        "dependencies": {
            name: version(name) for name in ("textual", "rich", "pyte", "Pillow")
        },
        "renderer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "font_sha256": {
            Path(font.path).name: hashlib.sha256(
                Path(font.path).read_bytes()
            ).hexdigest()
            for font in (renderer.font, renderer.bold, renderer.title, renderer.label)
        },
        "runtime_sha256": {
            name: hashlib.sha256((ROOT / "src/oc_box" / name).read_bytes()).hexdigest()
            for name in (
                "tui.py",
                "tui_app.py",
                "tui_status.py",
                "tui_settings.py",
                "tui_management.py",
                "tui_clipboard.py",
                "tui_terminal.py",
                "tui_sessions.py",
                "tui_logs.py",
            )
        },
        "images": records,
    }
    (output / "tui" / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"output": str(output), "images": records}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/assets")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace only the known documentation image set",
    )
    args = parser.parse_args()
    asyncio.run(capture(args.output_dir.resolve(), args.force))


if __name__ == "__main__":
    main()
