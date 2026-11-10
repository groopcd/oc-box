"""Optional host control center entrypoint; importing this needs only stdlib."""

import argparse
import importlib.util
import os
from pathlib import Path
import sys


def parser():
    result = argparse.ArgumentParser(
        prog="oc-box tui",
        description="Browse status, settings, sessions, live terminals and searchable logs.",
    )
    selectors = result.add_mutually_exclusive_group()
    selectors.add_argument("--project", type=Path)
    selectors.add_argument("--workspace")
    result.add_argument(
        "--read-only",
        action="store_true",
        help="disable session mutations and terminal attachment",
    )
    result.add_argument(
        "--demo",
        action="store_true",
        help="preview with synthetic data only, including inside oc-box",
    )
    return result


def cli(box, arguments):
    args = parser().parse_args(arguments)
    if not args.demo and os.environ.get("OC_BOX_CONTAINER") == "1":
        raise box.BoxError(
            "Run 'oc-box tui' on the host; --demo previews synthetic data only."
        )
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise box.BoxError("oc-box tui requires an interactive terminal (TTY).")
    if not args.demo and os.geteuid() == 0:
        raise box.BoxError("Run oc-box tui as your regular host user, without sudo.")
    # GitHub releases have an explicit, immutable optional dependency directory.
    bundled = box.PACKAGE / "tui-deps"
    if bundled.is_dir() and not bundled.is_symlink():
        sys.path.insert(0, str(bundled))
    if any(importlib.util.find_spec(name) is None for name in ("textual", "pyte")):
        raise box.BoxError(
            "Optional TUI dependencies are missing. Install 'oc-box[tui]' with uv/pipx, "
            "or reinstall the GitHub channel with --with-tui. No packages were installed."
        )
    try:
        if __package__:
            from .tui_app import DashboardApp
            from .tui_backend import DashboardBackend
        else:
            from tui_app import DashboardApp
            from tui_backend import DashboardBackend
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] in ("textual", "rich", "pyte"):
            raise box.BoxError(
                "Optional TUI dependencies are missing. Install 'oc-box[tui]' with uv/pipx, "
                "or reinstall the GitHub channel with --with-tui. No packages were installed."
            ) from None
        raise
    backend = DemoBackend() if args.demo else DashboardBackend(box)
    selected = None
    if not args.demo and (args.workspace or args.project):
        _path, data = box.find_workspace(args.project, args.workspace)
        if data is None:
            raise box.BoxError("No registered workspace matches the TUI selector.")
        selected = data["id"]
    return (
        DashboardApp(
            backend,
            read_only=args.read_only or args.demo,
            selected=selected,
            demo=args.demo,
        ).run()
        or 0
    )


class DemoBackend:
    """Fixed sample readings; never touches real workspace or host collectors."""

    box = None

    def inventory(self):
        return []

    def collect(self, *, scan_storage=False):
        from datetime import datetime, timezone

        gib = 1024**3
        return {
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "host": {
                "cpu_percent": 24,
                "memory_bytes": 6 * gib,
                "memory_total_bytes": 16 * gib,
                "swap_bytes": 0,
                "swap_total_bytes": 2 * gib,
                "filesystem": {
                    "total_bytes": 256 * gib,
                    "used_bytes": 90 * gib,
                    "free_bytes": 166 * gib,
                },
            },
            "workspaces": [
                {
                    "workspace_id": "a" * 32,
                    "project": "/home/demo/projects/example",
                    "container_project": "/home/demo/projects/example",
                    "distro": "debian",
                    "state": "running",
                    "project_status": "present",
                    "container_id": "b" * 64,
                    "container_name": "oc-box-example",
                    "image": "debian:stable",
                    "session_id": "ses_demo",
                    "recorded_sessions": 12,
                    "limits": {"cpu_cores": 4, "memory_bytes": 8 * gib, "pids": 2048},
                    "configured_limits": {
                        "cpu_cores": "4",
                        "memory_bytes": 8 * gib,
                        "pids": 2048,
                    },
                    "effective_limits": {
                        "cpu_cores": 4,
                        "memory_bytes": 8 * gib,
                        "pids": 2048,
                    },
                    "metrics": {"cpu_percent": 42, "memory_bytes": 2 * gib, "pids": 32},
                    "storage": {
                        "project_bytes": 2 * gib if scan_storage else None,
                        "private_home_bytes": 4 * gib if scan_storage else None,
                        "writable_layer_bytes": gib if scan_storage else None,
                        "project_filesystem": {
                            "free_bytes": 166 * gib,
                            "total_bytes": 256 * gib,
                        },
                    },
                    "mounts": [
                        {
                            "type": "bind",
                            "source": "/home/demo/projects/example",
                            "destination": "/home/demo/projects/example",
                            "writable": True,
                        }
                    ],
                    "notes": [
                        "Synthetic preview. No host resources or sessions were accessed."
                    ],
                }
            ],
        }

    def scan_storage(self, workspace):
        return self.collect(scan_storage=True)["workspaces"][0]

    def logs(self, workspace=None):
        if __package__:
            from .tui_logs import LogEntry
        else:
            from tui_logs import LogEntry
        return [
            LogEntry(
                "2026-01-01T12:00:00Z", "oc-box", "info", "Synthetic dashboard started"
            ),
            LogEntry(
                "2026-01-01T12:00:01Z", "podman", "info", "Synthetic container ready"
            ),
        ]
