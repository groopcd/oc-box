"""Framework-independent dashboard queries. No owning launch or resource edits."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

if __package__:
    from . import status_report, workspace_service
else:
    import status_report
    import workspace_service


class DashboardError(ValueError):
    """Fixed, non-private diagnostic suitable for presentation."""


TERMINAL_REFUSALS = {
    "workspace-missing": "The selected workspace is no longer registered. Refresh and select it again.",
    "unsupported-version": "Live session terminals require an existing OpenCode V2 workspace.",
    "workspace-not-running": "The selected workspace container is stopped or paused. Select the running workspace; no start was attempted.",
    "workspace-identity": "Workspace identity or project-marker validation failed. Inspect oc-box status; no repair was attempted.",
    "workspace-mount": "The container project mount does not match the host registration.",
    "workspace-ownership": "Container ownership validation failed; the resource was not used.",
    "owner-unavailable": "The private workspace owner service is unavailable or not ready. An already-running owner is required; no new owner was started.",
    "owner-mismatch": "The private owner does not match the workspace, native version or advertised session.",
    "workspace-validation": "Workspace or private-service validation failed. Compare with 'oc-box terminal' for this same workspace.",
    "invalid-session": "The requested session ID is invalid.",
    "session-unavailable": "The private service could not return the requested session. For an old owner-selected session, choose an existing live workspace source instead.",
    "session-scope": "The requested session does not belong to this workspace's internal project directory.",
    "client-build": "The native terminal helper could not be prepared. Check that the dashboard checkout/installation is complete.",
    "client-launch": "The local native terminal client could not start. No maintenance shell or owning launch was attempted.",
}
_ATTACH_REASONS = {
    "Workspace is not running; start it before attaching a client.": "workspace-not-running",
    "Workspace identity changed; refusing live session access.": "workspace-identity",
    "Container workspace mount does not match the host registry.": "workspace-mount",
    "Container ownership label mismatch; refusing to manage it.": "workspace-ownership",
    "Container ownership label mismatch; resource not measured.": "workspace-ownership",
    "No ready live OpenCode 2.x workspace service. Start oc-box or 'oc-box web' first; no credentials were refreshed.": "owner-unavailable",
    "Workspace service is not ready; retry after startup.": "owner-unavailable",
    "Live service does not match this workspace/version/session.": "owner-mismatch",
}


class TerminalAttachmentError(DashboardError):
    """Never exposes native errors, control responses, env, or command output."""

    def __init__(self, reason):
        self.reason = reason if reason in TERMINAL_REFUSALS else "workspace-validation"
        super().__init__(
            f"Terminal attachment refused ({self.reason}): {TERMINAL_REFUSALS[self.reason]}"
        )


def plain(value, limit=4096):
    """Render metadata literally, without terminal controls or markup execution."""
    return "".join(c if c.isprintable() else " " for c in str(value))[:limit]


def _private_prefix(path, limit):
    """Bounded private metadata read; never stage an archive/transcript copy."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Untrusted archive directory")
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError("Untrusted archive metadata")
            return stream.read(limit)
    finally:
        os.close(directory)


def _archive_info(path):
    # atomic_json writes info first. Decode that one bounded object, not the
    # messages array; unsupported old layouts remain visible but unassociated.
    prefix = _private_prefix(path, 65536).decode("utf-8", errors="replace")
    match = re.match(r'\s*\{\s*"info"\s*:\s*', prefix)
    if not match:
        return None
    info, _ = json.JSONDecoder().raw_decode(prefix, match.end())
    return info if isinstance(info, dict) else None


class HostSampler:
    def __init__(self, proc=Path("/proc")):
        self.proc = Path(proc)
        self.previous = None

    def sample(self):
        result = {
            "cpu_percent": None,
            "memory_bytes": None,
            "memory_total_bytes": None,
            "swap_bytes": None,
            "swap_total_bytes": None,
            "sampled_at": time.time(),
        }
        try:
            fields = (self.proc / "stat").read_text().splitlines()[0].split()
            if fields[0] != "cpu" or len(fields) < 5:
                raise ValueError("Invalid CPU sample")
            # guest/guest_nice are already included in user/nice.
            values = [int(value) for value in fields[1:9]]
            if any(value < 0 for value in values):
                raise ValueError("Invalid CPU sample")
            total, idle = sum(values), values[3] + (values[4] if len(values) > 4 else 0)
            if self.previous:
                delta, waiting = total - self.previous[0], idle - self.previous[1]
                if delta > 0 and 0 <= waiting <= delta:
                    result["cpu_percent"] = 100 * (delta - waiting) / delta
            self.previous = total, idle
        except (OSError, ValueError, IndexError):
            self.previous = None
        try:
            memory = {}
            for line in (self.proc / "meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                memory[key] = int(value.split()[0]) * 1024
            total, available = memory["MemTotal"], memory["MemAvailable"]
            if 0 <= available <= total:
                result.update(memory_bytes=total - available, memory_total_bytes=total)
            total, free = memory["SwapTotal"], memory["SwapFree"]
            if 0 <= free <= total:
                result.update(swap_bytes=total - free, swap_total_bytes=total)
        except (OSError, ValueError, IndexError, KeyError):
            pass
        return result


@dataclass
class TerminalHandle:
    command: list
    environment: dict = field(repr=False)
    close: object = field(repr=False)
    session_id: str = ""


@dataclass
class SessionConnection:
    origin: str
    store: str
    request: object = field(repr=False)
    ownership: object = field(repr=False)
    close: object = field(repr=False)


class _PrivateQueries:
    """Existing client orchestration with bounded, memory-only inspection."""

    def __init__(self, box):
        self._box = box

    def __getattr__(self, name):
        return getattr(self._box, name)

    def inspect_owned(self, data, *, env=None):
        def query(*args, **kwargs):
            return self._box.podman(*args, env=env, private=True, **kwargs)

        return status_report.inspect_resource(
            query,
            "container",
            data["container"],
            data["id"],
            [],
        )


class DashboardBackend:
    def __init__(self, box):
        self.box = box
        self._queries = _PrivateQueries(box)
        self.sampler = HostSampler()

    def inventory(self):
        result = []
        skipped = 0
        for path in self.box.workspace_paths():
            try:
                data = self.box.read_json(path)
                if (
                    isinstance(data, dict)
                    and re.fullmatch(r"[0-9a-f]{32}", data.get("id", ""))
                    and all(
                        isinstance(data.get(key), str) and data[key]
                        for key in (
                            "project",
                            "distro",
                            "container",
                            "home_volume",
                            "image",
                            "home",
                        )
                    )
                ):
                    result.append((path, data))
                else:
                    skipped += 1
            except (self.box.BoxError, OSError, ValueError, TypeError):
                skipped += 1
                continue
        self.inventory_skipped = skipped
        return result

    def selected(self, workspace):
        for path, data in self.inventory():
            if data["id"] == workspace:
                return path, data
        raise DashboardError("Workspace is no longer registered; refresh the list.")

    def associations(self, host_store):
        """Verified private provenance for display and conservative copy guards."""
        if __package__:
            from .tui_sessions import archive_associations
        else:
            from tui_sessions import archive_associations
        result = []
        for path, data in self.inventory():
            for name, native in (("sessions-v2.json", True), ("sessions.json", False)):
                try:
                    state = json.loads(
                        _private_prefix(path.parent / name, 2 * 1024 * 1024)
                    )
                    if not isinstance(state, dict):
                        continue
                    mapping = state.get("host_sessions", {})
                    if not isinstance(mapping, dict) or len(mapping) > 10000:
                        continue
                    provenance = {}
                    for ident in mapping.values():
                        if not self.box.valid_session_id(ident):
                            continue
                        try:
                            info = _archive_info(
                                path.parent / "history" / (ident + ".json")
                            )
                            if info:
                                provenance[ident] = info
                        except (OSError, ValueError, UnicodeError):
                            continue
                    relationships = archive_associations(
                        data,
                        state,
                        provenance,
                        source_origin="workspace" if native else "workspace-v1",
                        source_store=data["id"],
                        host_store=host_store,
                        native=native,
                    )
                    for relationship in relationships:
                        relationship["native_directory"] = str(
                            path.parent / "controller"
                        )
                    result.extend(relationships)
                except (OSError, ValueError, UnicodeError, RuntimeError, TypeError):
                    continue
        return result

    def _podman(self, *args, **kwargs):
        try:
            return self.box.podman(
                *args, env=self.box.maintenance_host_env(), private=True, **kwargs
            )
        except (self.box.BoxError, OSError, subprocess.SubprocessError):
            raise RuntimeError("Podman query unavailable.") from None

    def scan_storage(self, workspace):
        report = status_report.collect_reports(
            [self.selected(workspace)],
            podman=self._podman,
            session_state=self.box.session_state,
            scan_storage=True,
        )
        item = report["workspaces"][0]
        return {key: item[key] for key in ("workspace_id", "container_id", "storage")}

    def collect(self, *, scan_storage=False):
        workspaces = self.inventory()

        report = status_report.collect_reports(
            workspaces,
            podman=self._podman,
            session_state=self.box.session_state,
            scan_storage=scan_storage,
        )
        # Native status has historical raw-option/error fields. They are never
        # a dashboard data source: create arguments can contain private values.
        for item in report["workspaces"]:
            item.pop("podman_args", None)
            item["notes"] = [plain(note) for note in item.get("notes", [])]
        report["host"] = self.sampler.sample()
        report["inventory_skipped"] = getattr(self, "inventory_skipped", 0)
        report["host"]["filesystem"] = status_report.filesystem_usage(Path.home())
        return report

    def host_request(self, method, target, value=None):
        """Explicit host connection; native CLI may start its normal host service.

        Never use this for a workspace source. Keep authentication in native
        discovery rather than copying service credentials into TUI preferences.
        """
        env = self.box.maintenance_host_env()
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        command = ["opencode", "api", method.lower(), target]
        if value is not None:
            command.extend(["--data", json.dumps(value)])
        try:
            result = self.box.run(
                command, env=env, capture=True, private=True, timeout=30
            )
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (self.box.BoxError, OSError, ValueError, subprocess.SubprocessError):
            raise DashboardError(
                "Host OpenCode API unavailable or unsupported."
            ) from None

    def session_connection(self, workspace=None):
        """Explicit browsing connection; only workspace sources hold a client lease."""
        lease = None
        if workspace:
            path, data = self.selected(workspace)
            if not str(data.get("opencode_version", "")).startswith("2."):
                raise DashboardError("Live workspace browsing requires OpenCode V2.")
            lease = workspace_service.attach(self._queries, path, data)
            connection = lease.__enter__()

            def request(method, target, value=None):
                return workspace_service.api(
                    self.box,
                    data,
                    connection,
                    method,
                    target,
                    value,
                    timeout=30,
                )

            origin, store = "workspace", workspace
        else:
            request = self.host_request
            try:
                result = self.box.run(
                    ["opencode", "debug", "paths", "db"],
                    env=self.box.maintenance_host_env(),
                    capture=True,
                    private=True,
                    timeout=10,
                )
                candidate = result.stdout.strip()
                if (
                    not candidate.startswith("/")
                    or "\n" in candidate
                    or len(candidate) > 4096
                ):
                    raise ValueError("Invalid native store path")
                origin, store = "host", str(Path(candidate).resolve())
            except (self.box.BoxError, OSError, ValueError, subprocess.SubprocessError):
                raise DashboardError(
                    "Cannot identify the selected host OpenCode store."
                ) from None
        lock, closed, inflight, released = threading.RLock(), False, 0, False

        def release_when_ready():
            nonlocal released
            with lock:
                ready = closed and not inflight and not released
                if ready:
                    released = True
            if ready and lease:
                lease.__exit__(None, None, None)

        def close():
            nonlocal closed
            with lock:
                if not closed:
                    closed = True
            # Never block the UI behind a native request. An in-flight request
            # retains its owner lease and releases it in its own finally block.
            release_when_ready()

        def checked(method, target, value=None):
            nonlocal inflight
            with lock:
                if closed:
                    raise DashboardError("Session source has been disconnected.")
                inflight += 1
            try:
                return request(method, target, value)
            finally:
                with lock:
                    inflight -= 1
                release_when_ready()

        try:
            response = checked("GET", "/api/info")
            info = response.get("data", response) if isinstance(response, dict) else {}
            pid = info.get("pid")
            if (
                type(pid) is not int
                or pid <= 0
                or not str(info.get("version", "")).lstrip("v").startswith("2.")
            ):
                raise DashboardError(
                    "Session browsing requires a verified native V2 service."
                )
        except BaseException:
            close()
            raise

        def ownership(candidate_origin, candidate_store, ids):
            if (candidate_origin, candidate_store) != (origin, store):
                return None
            response = checked("GET", "/api/info")
            current = (
                response.get("data", response) if isinstance(response, dict) else {}
            )
            return "process-local" if current.get("pid") == pid else None

        return SessionConnection(origin, store, checked, ownership, close)

    def workspace_request(self, workspace, method, target, value=None):
        """A short explicit client lease; never creates/refreshes a workspace."""
        path, data = self.selected(workspace)
        if not str(data.get("opencode_version", "")).startswith("2."):
            raise DashboardError("Live workspace browsing requires OpenCode V2.")
        try:
            with workspace_service.attach(self._queries, path, data) as connection:
                return workspace_service.api(
                    self.box,
                    data,
                    connection,
                    method,
                    target,
                    value,
                    timeout=30,
                )
        except (self.box.BoxError, OSError, ValueError, subprocess.SubprocessError):
            raise DashboardError(
                "Live workspace API unavailable; no workspace was started."
            ) from None

    def terminal(self, workspace, session=None):
        """Explicit attach/takeover. The caller owns and must close the lease."""
        try:
            path, data = self.selected(workspace)
        except DashboardError:
            raise TerminalAttachmentError("workspace-missing") from None
        if not str(data.get("opencode_version", "")).startswith("2."):
            raise TerminalAttachmentError("unsupported-version")
        lease = workspace_service.attach(self._queries, path, data)
        entered = False
        stage = "attach"
        try:
            connection = lease.__enter__()
            entered = True
            target = session or connection["session"]
            if not self.box.valid_session_id(target):
                raise TerminalAttachmentError("invalid-session")
            stage = "session"
            response = workspace_service.api(
                self.box,
                data,
                connection,
                "GET",
                "/api/session/" + target,
            )
            info = response.get("data", response) if isinstance(response, dict) else {}
            if info.get("id") != target or info.get("location", {}).get(
                "directory"
            ) != self.box.container_project(data):
                raise TerminalAttachmentError("session-scope")
            stage = "client"
            command = workspace_service.client_command(
                self.box,
                data,
                connection,
                [
                    "python3",
                    "-c",
                    (self.box.PACKAGE / "session_terminal.py").read_text(),
                    connection["url"],
                    target,
                    self.box.container_project(data),
                ],
                force_tty=True,
            )
            return TerminalHandle(
                command,
                command.environment,
                lambda: lease.__exit__(None, None, None),
                target,
            )
        except BaseException as error:
            if entered:
                try:
                    lease.__exit__(None, None, None)
                except Exception:
                    pass
            if isinstance(error, TerminalAttachmentError) or not isinstance(
                error, Exception
            ):
                raise
            if stage == "attach":
                # Only exact known static refusals are categorized. Arbitrary
                # exception text/control replies are never presented or logged.
                reason = _ATTACH_REASONS.get(str(error), "workspace-validation")
            else:
                reason = "session-unavailable" if stage == "session" else "client-build"
            raise TerminalAttachmentError(reason) from None

    def logs(self, workspace=None):
        if __package__:
            from .tui_logs import activity_entries, podman_entries
        else:
            from tui_logs import activity_entries, podman_entries
        result = list(activity_entries(root=self.box.state_root()))
        if workspace:
            _, data = self.selected(workspace)
            result.extend(podman_entries(self._queries, data))
        return result
