#!/usr/bin/env python3
"""Rootless project environments for an unmodified OpenCode installation."""

from __future__ import annotations

import argparse
import contextlib
from contextvars import ContextVar
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import uuid

if __package__:
    from . import (
        cli_help,
        credentials,
        dependencies,
        history_reader,
        history_recovery,
        lifecycle_output,
        log_config,
        native_tabs,
        opencode_compat,
        opencode_v2_io,
        paths,
        plugin_compat,
        private_memory,
        startup_output,
        sync_config,
        vault,
        workspace_control,
        workspace_identity,
        workspace_service,
    )
else:
    import cli_help
    import credentials
    import dependencies
    import history_reader
    import history_recovery
    import lifecycle_output
    import log_config
    import native_tabs
    import opencode_compat
    import opencode_v2_io
    import paths
    import plugin_compat
    import private_memory
    import startup_output
    import sync_config
    import vault
    import workspace_control
    import workspace_identity
    import workspace_service

PACKAGE = Path(__file__).resolve().parent
APP = paths.source_root(PACKAGE)
LABEL = "io.oc-box.workspace"
MARKER = ".oc-box-id"
CONTAINER_OPENCODE = "/opt/oc-box/runtime/bin/opencode"
DISTROS = {
    "tumbleweed": (
        "registry.opensuse.org/opensuse/tumbleweed:latest",
        dependencies.baseline_install("tumbleweed"),
    ),
    "leap": (
        "registry.opensuse.org/opensuse/leap:latest",
        dependencies.baseline_install("leap"),
    ),
    "ubuntu": (
        "docker.io/library/ubuntu:24.04",
        dependencies.baseline_install("ubuntu"),
    ),
    "debian": (
        "docker.io/library/debian:stable",
        dependencies.baseline_install("debian"),
    ),
    "fedora": (
        "registry.fedoraproject.org/fedora:latest",
        dependencies.baseline_install("fedora"),
    ),
    "arch": (
        "docker.io/library/archlinux:base",
        dependencies.baseline_install("arch"),
    ),
    "alpine": (
        "docker.io/library/alpine:latest",
        dependencies.baseline_install("alpine"),
    ),
}
ALIASES = {
    "opensuse": "tumbleweed",
    "opensuse-tumbleweed": "tumbleweed",
    "opensuse-leap": "leap",
    "archlinux": "arch",
}


class BoxError(Exception):
    pass


class HostImportUnavailable(BoxError):
    """Fixed preflight guidance from the host terminal or clipboard helper."""


class HistoryPending(BoxError):
    """A session's history is not settled right now; others may still sync."""


class HistoryWaitTimeout(BoxError):
    """Keep the fixed refusal separate from aggregate readiness diagnostics."""

    def __init__(
        self,
        diagnostic,
        *,
        recovery_eligible=False,
        recovery_reason="readiness-unverified",
        timing_diagnostic=None,
        interrupted_foreground=False,
    ):
        super().__init__("OpenCode 2.x history wait failed (activity-timeout).")
        self.diagnostic = diagnostic
        self.recovery_eligible = recovery_eligible
        self.recovery_reason = recovery_reason
        self.timing_diagnostic = timing_diagnostic
        self.interrupted_foreground = interrupted_foreground


_TTY_PROGRESS = ContextVar("oc_box_tty_progress", default=())
_DEBUG_ONLY_STAGES = frozenset(
    {"history-wait", "history-sync", "history-recovery", "shutdown"}
)


@contextlib.contextmanager
def loading_stage(label, *, stage=None, interval=0.1, stream=None):
    """Foreground CLI feedback; Python stream proxying must nest lexically.

    Only explicitly tagged stages opt in to concise presentation and journaling.
    The label is for display, never diagnostic metadata.
    """
    started = time.monotonic()
    outcome = "failure"
    try:
        with _loading_progress(
            label,
            started,
            interval,
            stream,
            stage is not None and lifecycle_output.concise(),
        ):
            yield
        outcome = "success"
    except KeyboardInterrupt:
        outcome = "cancelled"
        raise
    finally:
        if stage is not None:
            startup_failed = outcome == "failure" and stage not in _DEBUG_ONLY_STAGES
            lifecycle_output.record(
                "lifecycle.stage",
                level="error" if startup_failed else "debug",
                stage=stage,
                reason="startup-failed" if startup_failed else None,
                outcome=outcome,
                duration_ms=int((time.monotonic() - started) * 1000),
            )


@contextlib.contextmanager
def _loading_progress(label, started, interval, stream, concise):
    stream = sys.stderr if stream is None else stream
    try:
        fd = stream.fileno()
        interactive = (
            stream.isatty()
            and os.environ.get("TERM") != "dumb"
            and os.get_terminal_size(fd).columns > 1
        )
    except (AttributeError, OSError, ValueError):
        interactive = False
    if concise and not interactive:
        yield
        return
    progress = _TTY_PROGRESS.get()
    if concise and progress:
        coalesces, matches, show_stage, _ = progress[-1]
        if coalesces and matches(stream):
            with show_stage(label, started):
                yield
            return

    badge = startup_output.badge_for(stream, temporary=True) if interactive else None
    final_badge = startup_output.badge_for(stream) if interactive else None
    stopped = threading.Event()
    lock = threading.RLock()
    drawn = False
    badge_erase = ""
    partial_output = False
    retired = False
    writable = True
    worker = None
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    try:
        frames.encode(getattr(stream, "encoding", None) or "ascii")
    except (LookupError, UnicodeError):
        frames = "|/-\\"
    frame = 0

    def emit(text):
        nonlocal writable
        if writable:
            try:
                stream.write(text)
                stream.flush()
            except (OSError, ValueError):
                # Progress is best-effort; do not replace the operation's error.
                writable = False

    def clear():
        nonlocal drawn, badge_erase
        if drawn:
            emit(badge_erase + "\r\x1b[K")
            drawn = False
            badge_erase = ""

    def clip(text, width):
        result = []
        for character in text:
            cells = (
                0
                if unicodedata.combining(character)
                else 2
                if unicodedata.east_asian_width(character) in ("W", "F")
                else 1
            )
            if cells > width:
                break
            result.append(character)
            width -= cells
        return "".join(result)

    def render(suffix, *, final=False):
        nonlocal drawn, frame, badge_erase
        try:
            # Query the actual output fd on every draw, including after resize;
            # leave the last cell unused to avoid terminal autowrap.
            width = max(os.get_terminal_size(fd).columns - 1, 0)
        except (OSError, ValueError):
            width = 0
        mark = (
            startup_output.Badge()
            if retired
            else (final_badge if final else badge).fit(width + 1)
        )
        width -= mark.cells
        spinner = "" if final else frames[frame % len(frames)] + " "
        frame += 1
        prefix = "".join(
            character if character.isprintable() else " "
            for character in f"oc-box: {spinner}{label}"
        )
        text = (
            clip(prefix, width - len(suffix)) + suffix
            if len(suffix) < width
            else suffix.lstrip()[:width]
        )
        output = (
            text + "\n"
            if retired
            else badge_erase + "\r\x1b[K" + mark.wire + text + ("\n" if final else "")
        )
        # A flush can be interrupted after the terminal already got the badge.
        # Keep its deletion armed before attempting the output.
        drawn = not final
        badge_erase = mark.erase
        emit(output)

    class StageOutput:
        """Separate Python output without saving it or capturing child fds."""

        def __init__(self, target):
            self.target = target

        def __getattr__(self, name):
            return getattr(self.target, name)

        def write(self, text):
            nonlocal partial_output
            with lock:
                if text:
                    clear()
                count = self.target.write(text)
                if text:
                    partial_output = not text.endswith("\n")
                return count

        def writelines(self, lines):
            for line in lines:
                self.write(line)

    def same_terminal(other):
        while isinstance(other, StageOutput):
            other = other.target
        if other is stream:
            return True
        try:
            here, there = os.fstat(fd), os.fstat(other.fileno())
            return other.isatty() and (here.st_dev, here.st_ino) == (
                there.st_dev,
                there.st_ino,
            )
        except (AttributeError, OSError, ValueError):
            return False

    def redraw():
        if not retired and not partial_output:
            render(f"... ({int(time.monotonic() - started)}s elapsed)")

    @contextlib.contextmanager
    def show_stage(message, since):
        nonlocal label, started
        previous = label, started
        try:
            with lock:
                label, started = message, since
                redraw()
            yield
        finally:
            with lock:
                try:
                    clear()
                finally:
                    label, started = previous

    def retire():
        nonlocal retired
        with lock:
            retired = True
            stopped.set()
            clear()

        def finish_live_row():
            with lock:
                # The child may have left a partial line. Start later launcher
                # output on a separate row without a carriage return or erase.
                emit("\n")

        return stream, same_terminal, finish_live_row

    def heartbeat():
        while not stopped.wait(interval):
            with lock:
                if not writable:
                    return
                redraw()

    with contextlib.ExitStack() as redirects:
        entered = False
        failed = True
        try:
            if interactive:
                # Python warnings/status lines must not be overwritten by a redraw.
                # Inherited subprocess fds and previously saved stream references
                # remain direct output; this is not a terminal recorder or relay.
                for name, redirect in (
                    ("stdout", contextlib.redirect_stdout),
                    ("stderr", contextlib.redirect_stderr),
                ):
                    target = getattr(sys, name)
                    if same_terminal(target):
                        redirects.enter_context(redirect(StageOutput(target)))
                token = _TTY_PROGRESS.set(
                    (*progress, (concise, same_terminal, show_stage, retire))
                )
                redirects.callback(_TTY_PROGRESS.reset, token)
                render("... (0s elapsed)")
                worker = threading.Thread(target=heartbeat, daemon=True)
                worker.start()
            else:
                emit(f"oc-box: {label}...\n")
            entered = True
            yield
            failed = False
        finally:
            stopped.set()
            try:
                if worker is not None:
                    # start() may be interrupted before the thread is joinable.
                    # The stop event also prevents a late starter from painting.
                    with contextlib.suppress(RuntimeError):
                        worker.join()
            finally:
                with lock:
                    if concise or not entered:
                        clear()
                    else:
                        elapsed = time.monotonic() - started
                        suffix = (
                            f" failed after {elapsed:.1f}s"
                            if failed
                            else f" done ({elapsed:.1f}s)"
                        )
                        if interactive:
                            if partial_output:
                                emit("\n")
                            render(suffix, final=True)
                        else:
                            emit(f"oc-box: {label}{suffix}\n")


@contextlib.contextmanager
def _startup_tool_output():
    """Keep live Podman/installer output on inherited FDs without a redraw."""
    terminals = []
    # Diagnostics permit independent nested stages. Retire every active
    # painter before handing over the terminal, not just the innermost one.
    for _, _, _, retire in reversed(_TTY_PROGRESS.get()):
        stream, matches, separate = retire()
        if not any(matches(other) for other, _ in terminals):
            terminals.append((stream, separate))
    try:
        yield
    finally:
        for _, separate in terminals:
            separate()


def run_tui(command, *, file_drops=None):
    # Lazy import also keeps noninteractive CLI/library consumers lightweight.
    if __package__:
        from .terminal_ui import run_tui as relay
    else:
        from terminal_ui import run_tui as relay

    preview_options = {}
    preview_open = getattr(command, "preview_host_open", None)
    if callable(preview_open):
        if __package__:
            from . import file_preview_host
        else:
            import file_preview_host
        preview_options = {
            "preview_relay": file_preview_host.Relay(),
            "preview_open": preview_open,
        }
    return relay(
        command,
        env=getattr(command, "environment", None),
        **({"file_drops": file_drops} if file_drops is not None else {}),
        **preview_options,
    )


def validate_host_drop(version=None):
    if __package__:
        from . import host_drop
    else:
        import host_drop
    try:
        return host_drop.preflight(version)
    except host_drop.DropError as error:
        raise HostImportUnavailable(str(error)) from None


def report_host_import_refusal(feature, error):
    reason = (
        str(error)
        if isinstance(error, HostImportUnavailable)
        else "Native OpenCode or terminal compatibility could not be verified."
    )
    startup_output.line(
        f"oc-box: {feature} is off for this client. {reason} "
        "The workspace preference is unchanged.",
        flush=True,
    )


def workspace_ui_preferences(path, data):
    if __package__:
        from . import workspace_preferences
    else:
        import workspace_preferences
    try:
        return workspace_preferences.read(path, data["id"])
    except workspace_preferences.PreferenceError as error:
        raise BoxError(str(error)) from None


def _startup_logo_choice(args):
    """Best-effort display lookup; never use it to authorize a launch."""
    if __package__:
        from . import workspace_preferences
    else:
        import workspace_preferences
    default = workspace_preferences.DEFAULT_STARTUP_LOGO
    try:
        path, data = find_workspace(args.project, args.workspace)
        if data is not None:
            return workspace_ui_preferences(path, data).get("startup_logo", default)
    except (BoxError, OSError, ValueError, KeyError, TypeError):
        # Leave the actual refusal and its ordering to the regular admission.
        return "ascii"
    return default


def configure_host_drop(path, data, args, version, *, saved=None):
    """Resolve the separate host preference before unlock or lifecycle changes."""
    if getattr(args, "_host_drop_resolved", False):
        return
    if __package__:
        from . import workspace_preferences
    else:
        import workspace_preferences
    requested = getattr(args, "host_drop", None)
    if requested is not None and type(requested) is not bool:
        raise BoxError("Invalid host file-drop preference.")
    try:
        # Validate even when overriding: corrupt policy is not permission to
        # replace an unverified workspace's preference file.
        if saved is None:
            saved = workspace_ui_preferences(path, data)
        # A start/reset or attach never writes this independent host choice.
        args._saved_host_file_open = saved.get("host_file_open")
        wanted = saved["host_drop"] if requested is None else requested
        enabled = False
        if wanted:
            try:
                # Automatic noninteractive/unsupported terminals must not add
                # a version probe ahead of ordinary credential validation.
                if version is None:
                    validate_host_drop()
                    version = detect_opencode_release()
                    args._opencode_release = version
                validate_host_drop(version)
                enabled = True
            except BoxError as error:
                if requested is True:
                    raise
                if sys.stdin.isatty() and sys.stdout.isatty():
                    report_host_import_refusal("Host file importing", error)
        if requested is not None:
            workspace_preferences.set_host_drop(path, data["id"], requested)
            startup_output.line(
                "oc-box: Saved workspace host importing: "
                + ("enabled." if requested else "disabled.")
                + " Existing clients keep their current setting.",
                flush=True,
            )
    except workspace_preferences.PreferenceError as error:
        raise BoxError(str(error)) from None
    args._host_drop_enabled = enabled
    args._host_drop_resolved = True


def effective_host_drop(args):
    return bool(getattr(args, "_host_drop_enabled", getattr(args, "host_drop", False)))


def effective_host_file_open(args, data):
    """The owning launcher and attached native clients resolve this separately."""
    requested = getattr(args, "host_file_open", None)
    if requested is not None:
        return requested is True
    saved = getattr(args, "_saved_host_file_open", None)
    if saved is not None:
        return saved is True
    if "restored_from" in data:
        return False
    if __package__:
        from . import host_file_open_policy
    else:
        import host_file_open_policy
    return host_file_open_policy.enabled(
        PACKAGE, home=Path.home(), registry=state_root()
    )


def effective_image_preview(args):
    """Only a caller or a prevalidated owning-launch snapshot requests an override."""
    requested = getattr(args, "image_preview", None)
    if requested is not None:
        return requested is True
    return getattr(args, "_image_preview_enabled", False) is True


def validate_host_file_open(args, version=None, data=None):
    if getattr(args, "host_file_open", None) is not True:
        return
    if __package__:
        from . import file_preview
    else:
        import file_preview
    reason = file_preview.client_incompatibility(version)
    if reason is not None:
        raise BoxError(
            "--host-file-open requires an interactive stable native OpenCode 2.x "
            f"TUI in direct Konsole or Yakuake ({reason})."
        )
    if getattr(args, "podman_arg", None) or (
        data
        and data.get("podman_args")
        and not getattr(args, "clear_podman_args", False)
    ):
        raise BoxError("--host-file-open cannot be used with custom Podman arguments.")


def configure_host_clipboard(args, version=None):
    """Discover the host helper before unlock/reset, never read its clipboard."""
    if not effective_host_clipboard(args):
        if (
            getattr(args, "host_clipboard", None) is False
            and getattr(args, "host_clipboard_symlinks", None) is not None
        ):
            raise BoxError(
                "Clipboard symlink options require --host-clipboard or a saved enabled preference."
            )
        return None
    validate_host_drop(version)
    if not hasattr(args, "_host_clipboard_reader"):
        if __package__:
            from . import host_clipboard
        else:
            import host_clipboard
        try:
            args._host_clipboard_reader = host_clipboard.preflight()
        except host_clipboard.ClipboardError as error:
            raise HostImportUnavailable(str(error)) from None
    return args._host_clipboard_reader


def effective_host_clipboard(args):
    return bool(
        getattr(args, "_host_clipboard_enabled", getattr(args, "host_clipboard", False))
    )


def effective_host_clipboard_symlinks(args):
    value = getattr(
        args, "_host_clipboard_symlinks", getattr(args, "host_clipboard_symlinks", None)
    )
    return True if value is None else value


def configure_host_clipboard_preferences(path, data, args, version):
    """Resolve saved future-client policy without ever reading clipboard contents."""
    if getattr(args, "_host_clipboard_resolved", False):
        return
    if __package__:
        from . import workspace_preferences
    else:
        import workspace_preferences
    requested = getattr(args, "host_clipboard", None)
    symlinks = getattr(args, "host_clipboard_symlinks", None)
    if any(
        type(value) is not bool for value in (requested, symlinks) if value is not None
    ):
        raise BoxError("Invalid host clipboard preference.")
    try:
        saved = workspace_preferences.read(path, data["id"])
        wanted = (
            saved.get("host_clipboard", workspace_preferences.DEFAULT_HOST_CLIPBOARD)
            if requested is None
            else requested
        )
        if symlinks is not None and not wanted:
            raise BoxError(
                "Clipboard symlink options require --host-clipboard or a saved enabled preference."
            )
        args._host_clipboard_symlinks = (
            saved.get(
                "host_clipboard_symlinks",
                workspace_preferences.DEFAULT_HOST_CLIPBOARD_SYMLINKS,
            )
            if symlinks is None
            else symlinks
        )
        args._host_clipboard_enabled = wanted
        try:
            if wanted and version is None:
                # Automatic unsupported clients must not add version/helper
                # probes ahead of ordinary credential validation.
                validate_host_drop()
                version = (
                    getattr(args, "_opencode_release", None)
                    or detect_opencode_release()
                )
                args._opencode_release = version
            configure_host_clipboard(args, version)
        except BoxError as error:
            args._host_clipboard_enabled = False
            if requested is True or symlinks is not None:
                raise
            report_host_import_refusal("Host clipboard importing", error)
        if requested is not None or symlinks is not None:
            workspace_preferences.set_preferences(
                path,
                data["id"],
                host_clipboard=requested,
                host_clipboard_symlinks=symlinks,
            )
            startup_output.line(
                "oc-box: Saved workspace host clipboard preferences. "
                "Existing clients keep their current setting.",
                flush=True,
            )
    except workspace_preferences.PreferenceError as error:
        args._host_clipboard_enabled = False
        raise BoxError(str(error)) from None
    args._host_clipboard_resolved = True


def run_native_tui(
    data,
    command,
    connection,
    *,
    host_drop_enabled=False,
    host_drop_required=True,
    host_clipboard_reader=None,
    host_clipboard_symlinks=True,
):
    if not host_drop_enabled and host_clipboard_reader is None:
        return run_tui(command)
    required = host_drop_required or host_clipboard_reader is not None
    try:
        profile = validate_host_drop(str(data.get("opencode_version", "")))
    except BoxError as error:
        if required:
            raise
        report_host_import_refusal("Host file importing", error)
        return run_tui(command)
    if connection is None:
        raise BoxError("Host file drops require an existing private V2 connection.")
    if __package__:
        from . import host_drop
    else:
        import host_drop
    client_started = False
    try:
        with host_drop.Relay(
            sys.modules[__name__],
            data,
            connection,
            profile=profile,
            **(
                {
                    "paths_enabled": host_drop_enabled,
                    "clipboard": host_clipboard_reader,
                    "clipboard_symlinks": host_clipboard_symlinks,
                }
                if host_clipboard_reader is not None
                else {}
            ),
        ) as drops:
            client_started = True
            details = lifecycle_output.feature_details()
            if host_drop_enabled:
                startup_output.line("oc-box: " + host_drop.DROP_ENABLED, flush=True)
                if details:
                    startup_output.line("oc-box: " + host_drop.DROP_DETAILS, flush=True)
            if host_clipboard_reader is not None:
                startup_output.line(
                    "oc-box: " + host_drop.CLIPBOARD_ENABLED, flush=True
                )
                if details:
                    policy = (
                        host_drop.CLIPBOARD_SYMLINKS_WARNING
                        if host_clipboard_symlinks
                        else host_drop.CLIPBOARD_STRICT_WARNING
                    )
                    startup_output.line(
                        "oc-box: " + host_drop.CLIPBOARD_DETAILS, flush=True
                    )
                    startup_output.line("oc-box: " + policy, flush=True)
            return run_tui(command, file_drops=drops)
    except host_drop.DropError:
        if not client_started and not required:
            startup_output.line(
                "oc-box: Host importing could not start; opening the ordinary native TUI. "
                "The workspace preference is unchanged.",
                flush=True,
            )
            return run_tui(command)
        raise BoxError(
            "Host file-drop transfer failed; no automatic retry or workspace restart was attempted."
        ) from None


def state_root():
    return (
        Path(
            os.environ.get(
                "OC_BOX_STATE_HOME",
                str(
                    Path(
                        os.environ.get(
                            "XDG_STATE_HOME", str(Path.home() / ".local/state")
                        )
                    )
                    / "oc-box"
                ),
            )
        )
        .expanduser()
        .resolve()
    )


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            os.chmod(tmp, 0o600)
            json.dump(value, stream, indent=2)
            stream.write("\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise BoxError(f"Cannot read {path}: {exc}") from exc


def host_default(os_release=Path("/etc/os-release")):
    try:
        values = dict(
            line.split("=", 1)
            for line in os_release.read_text().splitlines()
            if "=" in line
        )
        distro = values.get("ID", "").strip('"')
        version = values.get("VERSION_ID", "").strip('"')
    except OSError:
        return "tumbleweed"
    if distro in ("opensuse-tumbleweed", "opensuse-slowroll"):
        return "tumbleweed"
    if distro.startswith("opensuse"):
        return "leap"
    if distro == "ubuntu" and version != "24.04":
        return "ubuntu"
    return (
        ALIASES.get(distro, distro)
        if ALIASES.get(distro, distro) in DISTROS
        else "tumbleweed"
    )


def distro_name(name):
    value = ALIASES.get(name, name)
    if value not in DISTROS:
        raise BoxError(
            f"Unknown distro {name!r}. Run 'oc-box distros' for supported choices."
        )
    return value


@contextlib.contextmanager
def captured_output(*, private=False):
    if private or credentials.has_unlocked_values():
        # Captured subprocess output may contain a delivered credential. An
        # anonymous in-memory regular file preserves mainline's large-stdout
        # behavior without spilling that output to a plaintext temp file.
        try:
            fd = private_memory.create("oc-box-private-output")
        except OSError as exc:
            raise BoxError(private_memory.error_message()) from exc
        with os.fdopen(fd, "w+b") as stream:
            yield stream
    else:
        with tempfile.TemporaryFile() as stream:
            yield stream


def check_private_output():
    """Probe private capture before an owning launch changes its environment."""
    try:
        private_memory.check()
    except OSError as exc:
        raise BoxError(private_memory.error_message()) from exc


def run(argv, *, capture=False, check=True, private=False, preparation=None, **kwargs):
    remaining = history_recovery.remaining()
    if remaining is not None:
        requested = kwargs.get("timeout")
        kwargs["timeout"] = min(requested, remaining) if requested else remaining
    if getattr(argv, "environment", None) is not None:
        kwargs.setdefault("env", argv.environment)
    if kwargs.get("cwd"):
        kwargs["env"] = {
            **kwargs.get("env", os.environ),
            "PWD": str(Path(kwargs["cwd"]).resolve()),
        }
    tracked_preparation = (
        preparation is not None
        and not capture
        and not private
        and lifecycle_output.preparing_environment()
        and history_recovery.FENCE.get() is None
    )
    details = {}
    try:
        command = [str(x) for x in argv]
        invoke = (
            history_recovery.run_fenced
            if history_recovery.FENCE.get() is not None
            else subprocess.run
        )
        if capture:
            # Mainline OpenCode can truncate large exports when stdout is a
            # pipe. Use regular files for captured CLI output instead.
            with (
                captured_output(private=private) as output,
                captured_output(private=private) as errors,
            ):
                result = invoke(
                    command, text=True, stdout=output, stderr=errors, **kwargs
                )
                output.seek(0)
                errors.seek(0)
                result.stdout = output.read().decode("utf-8")
                result.stderr = errors.read().decode("utf-8", errors="replace")
        elif tracked_preparation:
            # The selected setup tools already ran noninteractively. Don't
            # route raw output or interactive/native subprocesses to disk.
            live = lifecycle_output.live_preparation_output()
            if not live:
                kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if "input" not in kwargs and "stdin" not in kwargs:
                    kwargs["stdin"] = subprocess.DEVNULL
            with (
                lifecycle_output.preparation(preparation) as details,
                _startup_tool_output() if live else contextlib.nullcontext(),
            ):
                try:
                    result = invoke(command, text=True, **kwargs)
                except subprocess.TimeoutExpired:
                    details["reason"] = "timeout"
                    raise
                except OSError:
                    details["reason"] = "io"
                    raise
                details.update(
                    outcome="failure" if result.returncode else "success",
                    exit_code=result.returncode
                    if -255 <= result.returncode <= 255
                    else None,
                )
        else:
            result = invoke(command, text=True, **kwargs)
    except FileNotFoundError as exc:
        raise BoxError(f"Executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        if tracked_preparation:
            task = lifecycle_output.preparation_label(preparation)
            hint = lifecycle_output.preparation_failure_hint(details)
            raise BoxError(f"{task} timed out. {hint}") from exc
        raise BoxError(
            f"{argv[0]} exceeded its {exc.timeout}s timeout; operation stopped."
        ) from exc
    history_recovery.remaining()
    if check and result.returncode:
        if tracked_preparation:
            task = lifecycle_output.preparation_label(preparation)
            hint = lifecycle_output.preparation_failure_hint(details)
            raise BoxError(f"{task} failed (exit {result.returncode}). {hint}")
        detail = (
            (result.stderr or result.stdout or "").strip()
            if capture and not private
            else ""
        )
        detail = credentials.redact(detail)
        raise BoxError(
            f"{argv[0]} failed (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result


def podman(*args, **kwargs):
    return run(["podman", *args], **kwargs)


def check_backend(*, env=None, feature="maintenance"):
    try:
        dependencies.require(feature)
    except dependencies.DependencyError as exc:
        raise BoxError(str(exc)) from exc
    if os.geteuid() == 0:
        raise BoxError(
            "Run oc-box as your regular user, without sudo (rootless Podman is required)."
        )
    result = podman(
        "info",
        "--format",
        "{{.Host.Security.Rootless}}",
        capture=True,
        **({"env": env} if env is not None else {}),
    )
    if result.stdout.strip() != "true":
        raise BoxError("Podman is not operating rootlessly.")


def check_resource_controllers(data, *, cgroup_root=Path("/sys/fs/cgroup"), uid=None):
    """Check the user manager, not kernel-global controller availability.

    On openSUSE the kernel supports memory/cpu but a vendor drop-in clears
    systemd delegation. runc then reports a misleading missing executable when
    it cannot open memory.swap.max in the newly created container cgroup.
    """
    uid = os.getuid() if uid is None else uid
    service = (
        cgroup_root
        / f"user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    )
    try:
        available = set(service.read_text().split())
    except FileNotFoundError:
        # Non-systemd layouts: ask Podman about its effective controllers.
        result = podman(
            "info", "--format", "{{json .Host.CgroupControllers}}", capture=True
        )
        controllers = json.loads(result.stdout)
        if not isinstance(controllers, list) or not all(
            isinstance(item, str) for item in controllers
        ):
            raise BoxError("Cannot determine delegated cgroup controllers from Podman.")
        available = set(controllers)
    required = {"pids"}
    if data.get("cpus") not in (None, "0", 0):
        required.add("cpu")
    if data.get("memory") not in (None, "0", 0):
        required.add("memory")
    missing = required - available
    if missing:
        raise BoxError(
            "Rootless Podman is missing delegated cgroup controllers: "
            + ", ".join(sorted(missing))
            + ". Available: "
            + (", ".join(sorted(available)) or "none")
            + ".\n"
            "On openSUSE, a vendor user@.service drop-in clears delegation. Run in a host terminal:\n"
            f"  sudo systemctl edit user@{uid}.service --drop-in=90-oc-box-delegate.conf\n"
            "Add these two lines to the override:\n"
            "  [Service]\n"
            "  Delegate=cpu cpuset io memory pids\n"
            "Then run 'sudo systemctl daemon-reload' and reboot (or fully end and restart the user manager).\n"
            "Retry the same oc-box command afterward; no reset/purge is needed. Resource limits remain enabled."
        )


def workspace_paths():
    return sorted((state_root() / "workspaces").glob("*/workspace.json"))


def registered_projects(project):
    project = str(Path(project).resolve())
    return [
        (path, data)
        for path in workspace_paths()
        if (data := read_json(path)).get("project") == project
    ]


def missing_marker_error(project, matches):
    choices = "\n".join(
        f"  {data['id']}  session={session_state(path).get('last') or 'none'}"
        for path, data in matches
    )
    commands = "\n".join(
        f"  oc-box repair --workspace {data['id']}" for _, data in matches
    )
    return BoxError(
        f"Project marker is missing, but existing workspace(s) already reference {project}:\n{choices}\n"
        "Refusing to create a duplicate environment. Check 'oc-box --status' and choose the workspace to recover.\n"
        f"Exit/stop its launcher and restore its marker explicitly with:\n{commands}\n"
        "If this is deliberately a different project, preserve any needed history and purge the old workspace by ID first (without --all)."
    )


def validate_project(path):
    project = Path(path).expanduser().resolve(strict=True)
    protected = {Path("/"), Path("/home"), Path.home().resolve(), state_root()}
    if not project.is_dir() or project in protected:
        raise BoxError(
            "Choose a dedicated project directory, not /, /home, your home, or oc-box state."
        )
    if __package__:
        from .paths import web_tls_home
    else:
        from paths import web_tls_home
    web_tls = web_tls_home().resolve()
    if web_tls.is_relative_to(project) or project.is_relative_to(web_tls):
        raise BoxError("Project would expose private host web TLS files.")
    for private in (
        state_root(),
        APP,
        Path.home() / ".config/opencode",
        Path.home() / ".local/share/opencode",
        vault.vault_path().parent,
    ):
        if private.resolve().is_relative_to(project):
            raise BoxError(
                f"Project would expose trusted application/state files: {private}"
            )
    # Podman volume syntax uses ':' and ',' as separators.
    if any(char in str(project) for char in (":", ",", "\n", "\r")):
        raise BoxError("Project path must not contain colons, commas, or line breaks.")
    return project


def find_workspace(project=None, workspace_id=None, *, allow_identity_change=False):
    if workspace_id:
        if not re.fullmatch(r"[0-9a-f]{32}", workspace_id):
            raise BoxError("Invalid workspace ID.")
        path = state_root() / "workspaces" / workspace_id / "workspace.json"
        if not path.exists():
            raise BoxError(f"Workspace not found: {workspace_id}")
        return path, read_json(path)
    project = Path(project or Path.cwd()).resolve()
    marker = project / MARKER
    if marker.is_symlink():
        raise BoxError(
            f"Workspace marker must be a regular file, not a symlink: {marker}"
        )
    if not marker.exists():
        matches = registered_projects(project)
        if matches:
            if allow_identity_change and len(matches) == 1:
                return matches[0]
            raise missing_marker_error(project, matches)
        return None, None
    if not marker.is_file():
        raise BoxError(f"Workspace marker must be a regular file: {marker}")
    ident = marker.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise BoxError(f"Invalid workspace marker: {marker}")
    path = state_root() / "workspaces" / ident / "workspace.json"
    if not path.exists():
        raise BoxError(
            f"Workspace marker has no matching host registry at {path}. "
            "Restore the registry or check OC_BOX_STATE_HOME/XDG_STATE_HOME before creating a new workspace."
        )
    data = read_json(path)
    if data["project"] != str(project):
        raise BoxError(
            "Workspace marker belongs to another directory; copied markers cannot authorize mounts."
        )
    identity, stable = workspace_identity.snapshot(project)
    if not allow_identity_change and not workspace_identity.matches(
        data, identity, stable
    ):
        raise BoxError(
            "Project directory identity changed; it may have been replaced or its filesystem identity changed.\n"
            f"Recorded device/inode: {data['project_identity']}; current: {identity}.\n"
            + (
                "This older registration has no stable filesystem baseline; one explicit repair will record it when supported.\n"
                if workspace_identity.STABLE_IDENTITY_KEY not in data
                and identity[1] == data["project_identity"][1]
                else ""
            )
            + "To reconnect this directory to its existing environment and sessions, run in a host terminal:\n"
            f"  oc-box repair --project {shlex.quote(str(project))}\n"
            "Then retry the original command."
        )
    return path, data


def create_workspace(project, distro):
    ident = uuid.uuid4().hex
    folder = state_root() / "workspaces" / ident
    folder.mkdir(parents=True, mode=0o700)
    (folder / "controller").mkdir(mode=0o700)
    data = {
        "version": 2,
        "id": ident,
        "project": str(project),
        "distro": distro,
        "image": DISTROS[distro][0],
        "container": "oc-box-" + ident,
        "home_volume": "oc-box-home-" + ident,
        "home": str(Path.home()),
        "created": int(time.time()),
        "cpus": "4",
        "memory": "8g",
        "pids": 2048,
    }
    workspace_identity.record(data, *workspace_identity.snapshot(project))
    path = folder / "workspace.json"
    atomic_json(path, data)
    try:
        with (project / MARKER).open("x") as stream:
            stream.write(ident + "\n")
    except OSError:
        path.unlink()
        raise
    return path, data


@contextlib.contextmanager
def workspace_lock(path):
    with path.with_name("lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BoxError(
                "This workspace already has an active oc-box process. Exit it before changing its environment."
            ) from exc
        yield


def repair_workspace(path, data):
    project = validate_project(data["project"])
    marker = project / MARKER
    missing = not marker.exists() and not marker.is_symlink()
    if not missing:
        marker_path, _ = find_workspace(project, allow_identity_change=True)
        if marker_path != path:
            raise BoxError(
                "Project marker does not match the selected workspace; refusing to repair."
            )
    identity, stable = workspace_identity.snapshot(project)
    if (
        not missing
        and data["project_identity"] == identity
        and data.get(workspace_identity.STABLE_IDENTITY_KEY) == stable
    ):
        print("Project directory identity already matches; no repair needed.")
        return
    info = inspect_owned(data)
    if info and info["State"]["Running"]:
        raise BoxError(
            "Container is running. Exit its launcher or run "
            f"'oc-box stop --workspace {data['id']}' before repairing."
        )
    if missing:
        with marker.open("x") as stream:
            stream.write(data["id"] + "\n")
    workspace_identity.record(data, identity, stable)
    atomic_json(path, data)
    print(
        "Workspace identity repaired"
        + (" and missing marker restored" if missing else "")
        + "; existing environment and sessions retained.\n"
        f"Resume with: oc-box --project {shlex.quote(str(project))} --resume"
    )


def refresh_workspace_identity(path, data):
    """Migrate diagnostics/legacy metadata only after a fully verified lookup.

    Called under the workspace lock; read-only list/status never rewrite state.
    A legacy device mismatch cannot be upgraded automatically without a prior
    stable baseline and is directed to explicit repair instead.
    """
    selected, _ = find_workspace(data["project"])
    if selected != path:
        raise BoxError("Project marker does not match the selected workspace.")
    identity, stable = workspace_identity.snapshot(data["project"])
    if not workspace_identity.matches(data, identity, stable):
        raise BoxError(
            "Project identity changed during verification; use explicit repair after checking the project."
        )
    if (
        data["project_identity"] != identity
        or data.get(workspace_identity.STABLE_IDENTITY_KEY) != stable
    ):
        workspace_identity.record(data, identity, stable)
        atomic_json(path, data)


def inspect_owned(data, *, env=None):
    options = {"env": env} if env is not None else {}
    result = podman(
        "container", "inspect", data["container"], capture=True, check=False, **options
    )
    if result.returncode:
        exists = podman(
            "container",
            "exists",
            data["container"],
            capture=True,
            check=False,
            **options,
        )
        if exists.returncode == 1:
            return None
        raise BoxError(f"Cannot inspect container: {result.stderr.strip()}")
    info = json.loads(result.stdout)[0]
    if info.get("Config", {}).get("Labels", {}).get(LABEL) != data["id"]:
        raise BoxError("Container ownership label mismatch; refusing to manage it.")
    return info


def validate_podman_arg(value):
    """One literal long-option token, never a shell command or image/command."""
    if not re.fullmatch(r"--[a-z][a-z0-9-]*(?:=[^\x00\r\n]*)?", value):
        raise BoxError(
            "Each --podman-arg must be one long option, e.g. --podman-arg=--group-add=keep-groups. Include option values with '='."
        )
    name = value.split("=", 1)[0]
    protected = {
        "--name",
        "--label",
        "--label-file",
        "--cidfile",
        "--replace",
        "--rm",
        "--entrypoint",
        "--workdir",
        "--user",
        "--rootfs",
        "--pod",
        "--pod-id-file",
    }
    dedicated = {
        "--cpus": "--cpus",
        "--memory": "--memory",
        "--pids-limit": "--pids-limit",
    }
    if name in protected:
        raise BoxError(
            f"{name} is managed by oc-box and cannot be passed through --podman-arg."
        )
    if name in dedicated:
        raise BoxError(
            f"Use oc-box's {dedicated[name]} option instead of --podman-arg={value}."
        )


def validate_device(value):
    if not value or any(char in value for char in ("\x00", "\r", "\n")):
        raise BoxError("Invalid --device mapping.")
    if value.startswith("/"):
        parts = value.split(":")
        if len(parts) > 3 or any(not part for part in parts):
            raise BoxError("Use --device /host/device[:/container/device][:rwm].")
        if (
            len(parts) >= 2
            and not parts[1].startswith("/")
            and not re.fullmatch(r"[rwm]+", parts[1])
        ):
            raise BoxError("Invalid --device destination or permissions.")
        if len(parts) == 3 and (
            not parts[1].startswith("/") or not re.fullmatch(r"[rwm]+", parts[2])
        ):
            raise BoxError("Invalid --device destination or permissions.")
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+=[A-Za-z0-9_.-]+", value):
        raise BoxError(
            "--device requires an absolute host device path or CDI name (vendor/class=name)."
        )


def configure_podman_options(data, args):
    """Append explicit settings, or clear them, without erasing saved defaults."""
    devices = [] if args.clear_devices else list(data.get("devices", []))
    options = [] if args.clear_podman_args else list(data.get("podman_args", []))
    for value in args.device or []:
        if value not in devices:
            devices.append(value)
    for value in args.podman_arg or []:
        if value not in options:
            options.append(value)
    changed = devices != data.get("devices", []) or options != data.get(
        "podman_args", []
    )
    if changed and args.command != "reset" and inspect_owned(data) is not None:
        raise BoxError(
            "Changing saved devices or Podman arguments requires 'oc-box reset' with the requested options. Project files and the private home/session volume are retained; OS packages are rebuilt."
        )
    data["devices"] = devices
    data["podman_args"] = options


def selinux_query(command, *args):
    # openSUSE keeps these tools in /usr/sbin, often absent from a user's PATH.
    executable = shutil.which(command) or shutil.which(command, path="/usr/sbin:/sbin")
    if executable is None:
        return None
    try:
        result = run([executable, *args], capture=True, check=False, timeout=5)
    except (BoxError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def warn_device_access(data):
    """Check device prerequisites without changing host policy or permissions."""
    mappings = list(data.get("devices", []))
    options = data.get("podman_args", [])
    mappings.extend(
        option.split("=", 1)[1] for option in options if option.startswith("--device=")
    )
    if not mappings:
        return

    for mapping in dict.fromkeys(mappings):
        if not mapping.startswith("/"):
            continue  # CDI names are resolved by Podman, not host paths.
        source, *rest = mapping.split(":")
        permissions = rest[-1] if rest and re.fullmatch(r"[rwm]+", rest[-1]) else "rw"
        mode = (os.R_OK if "r" in permissions else 0) | (
            os.W_OK if "w" in permissions else 0
        )
        if mode and not os.access(source, mode):
            startup_output.line(
                f"oc-box: warning: Host device {source!r} is missing or your user lacks the requested {permissions} access. "
                f"Check 'ls -l {shlex.quote(source)}', host ACLs, and group membership. "
                "Group-based access may require --podman-arg=--runtime=crun and --podman-arg=--group-add=keep-groups.",
                file=sys.stderr,
                flush=True,
            )

    if any(
        option in ("--security-opt=label=disable", "--privileged", "--privileged=true")
        for option in options
    ):
        return  # The explicitly requested container configuration bypasses labeling.
    if selinux_query("getenforce") != "Enforcing":
        return
    setting = selinux_query("getsebool", "container_use_devices")
    if setting and re.fullmatch(r"container_use_devices\s+-->\s+on", setting):
        return
    disabled = bool(
        setting and re.fullmatch(r"container_use_devices\s+-->\s+off", setting)
    )
    reason = (
        "container_use_devices is off"
        if disabled
        else "the container_use_devices setting could not be verified"
    )
    startup_output.line(
        f"oc-box: warning: SELinux is enforcing and {reason}; mapped devices may report Permission denied.\n"
        "Check from the host: /usr/sbin/getsebool container_use_devices\n"
        "To allow mapped devices: sudo /usr/sbin/setsebool -P container_use_devices on\n"
        "This is a persistent host-wide SELinux policy setting; oc-box does not change it automatically. "
        "It does not expose extra devices and takes effect immediately, without a reset or reboot.",
        file=sys.stderr,
        flush=True,
    )


def container_project(data):
    # Restore keeps old internal paths while moving the host project safely.
    return data.get("container_project", data["project"])


def create_container(path, data, *, environment=None):
    def create_podman(*args, **kwargs):
        return podman(
            *args,
            **kwargs,
            **({"env": environment} if environment is not None else {}),
        )

    project = validate_project(data["project"])
    if not workspace_identity.same_project(data, project):
        raise BoxError(
            "Project directory identity changed; refusing to mount a replacement."
        )
    for device in data.get("devices", []):
        validate_device(device)
    for option in data.get("podman_args", []):
        validate_podman_arg(option)
    warn_device_access(data)
    if not lifecycle_output.concise():
        startup_output.line(
            f"Creating {data['distro']} environment ({data['image']})", flush=True
        )
    # The named home is container-only storage, not a bind of the user's home.
    volume = create_podman(
        "volume", "exists", data["home_volume"], capture=True, check=False
    )
    if volume.returncode == 1:
        create_podman(
            "volume",
            "create",
            "--label",
            f"{LABEL}={data['id']}",
            data["home_volume"],
            capture=True,
        )
    elif volume.returncode != 0:
        raise BoxError("Cannot check the private home volume.")
    volume_info = json.loads(
        create_podman("volume", "inspect", data["home_volume"], capture=True).stdout
    )[0]
    if (volume_info.get("Labels") or {}).get(LABEL) != data["id"]:
        raise BoxError("Private home volume ownership mismatch.")
    create_podman(
        "create",
        "--name",
        data["container"],
        "--label",
        f"{LABEL}={data['id']}",
        "--label",
        "io.oc-box.managed=true",
        "--pull=missing",
        "--init",
        "--user",
        "0:0",
        "--network=pasta:--no-map-gw,--map-host-loopback,169.254.1.2",
        "--add-host",
        "host.containers.internal:169.254.1.2",
        "--cpus",
        data["cpus"],
        "--memory",
        data["memory"],
        "--pids-limit",
        str(data["pids"]),
        "--workdir",
        container_project(data),
        "--volume",
        f"{data['home_volume']}:{data['home']}:rw",
        "--volume",
        f"{project}:{container_project(data)}:rw,z,rprivate,nosuid,nodev",
        *(["--image-volume=ignore"] if data.get("restored_from") else []),
        *(["--unsetenv-all"] if data.get("restored_from") else []),
        *(["--no-healthcheck"] if data.get("restored_from") else []),
        *("--device=" + device for device in data.get("devices", [])),
        *data.get("podman_args", []),
        "--env",
        f"HOME={data['home']}",
        "--env",
        "PATH=/opt/oc-box/runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--entrypoint",
        "/bin/sh",
        data["image"],
        "-c",
        "exec sleep infinity",
        preparation="container-image",
    )
    data["ready"] = False
    atomic_json(path, data)


def start_container(path, data):
    project = validate_project(data["project"])
    current_path, _ = find_workspace(project)
    if current_path != path:
        raise BoxError(
            "Project marker does not match the selected workspace; refusing to start another environment for this directory. Use 'oc-box --status' to select the current workspace."
        )
    if not workspace_identity.same_project(data, project):
        raise BoxError(
            "Project directory identity changed; refusing to reuse the environment."
        )
    check_resource_controllers(data)
    info = inspect_owned(data)
    if info is None:
        if data.get("ready"):
            startup_output.line(
                "Previous container is missing; recreating its OS environment.",
                flush=True,
            )
        create_container(path, data)
        info = inspect_owned(data)
    mounts = info.get("Mounts", [])
    if not any(
        m.get("Destination") == container_project(data)
        and m.get("Source") == data["project"]
        for m in mounts
    ):
        raise BoxError("Container workspace mount does not match the host registry.")
    if not info["State"]["Running"]:
        podman("start", data["container"], capture=True)
    if not data.get("ready"):
        try:
            quiet = lifecycle_output.concise()
            with (
                loading_stage("Installing container baseline tools", stage="bootstrap")
                if quiet
                else contextlib.nullcontext()
            ):
                podman(
                    "exec",
                    data["container"],
                    "/bin/sh",
                    "-c",
                    DISTROS[data["distro"]][1],
                    timeout=1800,
                    preparation="baseline-packages",
                )
                podman(
                    "exec",
                    "-i",
                    data["container"],
                    "python3",
                    "-",
                    *(
                        ["--quiet-ready"]
                        if quiet and not lifecycle_output.live_preparation_output()
                        else []
                    ),
                    input=(PACKAGE / "bootstrap.py").read_text(),
                    timeout=600,
                    preparation="baseline-tools",
                )
        except BaseException:
            podman("stop", "--time", "5", data["container"], capture=True, check=False)
            raise
        data["ready"] = True
        atomic_json(path, data)
    # A bind mount can exist while SELinux denies its contents. Detect that
    # before OpenCode imports a conversation into an unusable workspace.
    access = podman(
        "exec",
        data["container"],
        "/bin/sh",
        "-c",
        'test -r "$1" && test -w "$1" && ls -A "$1" >/dev/null',
        "oc-box",
        container_project(data),
        capture=True,
        check=False,
        timeout=30,
    )
    if access.returncode:
        raise BoxError(
            "Container cannot read/write the mounted project. On SELinux hosts, older oc-box containers may lack the project relabel option.\n"
            f"From a host terminal, relabel this project with: chcon -R -t container_file_t {shlex.quote(data['project'])}\n"
            "Then retry the same oc-box command. New containers use Podman's shared project label (:z)."
        )
    return info


def stop_container(data, *, env=None):
    options = {"env": env} if env is not None else {}
    info = inspect_owned(data, **options)
    if info and info["State"]["Running"]:
        podman("stop", "--time", "10", data["container"], capture=True, **options)


def session_file(path):
    # Private per-major state keeps a V2 resume ID out of the retained V1 DB.
    data = read_json(path) if path.is_file() else {}
    return path.with_name(
        "sessions-v2.json"
        if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2
        else "sessions.json"
    )


def session_state(path):
    return (
        read_json(session_file(path))
        if session_file(path).exists()
        else {"sessions": [], "last": None}
    )


def host_cli_env(path):
    """Use the default host DB, without running global/project integrations."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("OPENCODE_") or key.startswith("OC_BOX_"):
            env.pop(key)
    for name in ("host-config", "host-state", "empty-home"):
        (path.parent / name).mkdir(exist_ok=True, mode=0o700)
    env.update(
        {
            "OPENCODE_PURE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_TEST_HOME": str(path.parent / "empty-home"),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "XDG_CONFIG_HOME": str(path.parent / "host-config"),
            "XDG_STATE_HOME": str(path.parent / "host-state"),
            "XDG_DATA_HOME": os.environ.get(
                "XDG_DATA_HOME", str(Path.home() / ".local/share")
            ),
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "snapshot": False,
                    "formatter": False,
                    "lsp": False,
                }
            ),
        }
    )
    return env


def host_command(data, *arguments):
    return opencode_compat.command(
        data.get("opencode_version", "1.0.0"), "opencode", arguments
    )


def detect_opencode_release():
    return opencode_compat.version(
        run(["opencode", "--version"], capture=True, timeout=15).stdout
    )


def private_host_env(path, data):
    env = host_cli_env(path)
    if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "snapshots": False,
                "formatter": False,
                "update": "disable",
                "policies": [
                    {"action": "plugin.use", "resource": "*", "effect": "deny"}
                ],
            }
        )
    reader = history_reader.current()
    if reader is not None:
        env = history_reader.fence_environment(env)
        history_reader.assert_host_safe(
            sys.modules[__name__], path, data, env, reader.remaining
        )
    return env


def container_build_config(release="1.0.0", *, home=None):
    """Override only Build permissions in the container's final config scope."""
    if opencode_compat.major(release) == 2:
        config = {
            "$schema": "https://opencode.ai/config.json",
            "agents": {
                "build": {
                    "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]
                }
            },
        }
        if home is not None:
            # Project plugin removals are lower priority than this generated
            # private content scope. Re-enable only our Build helper, never
            # unrelated integrations or plugins blocked by managed policy.
            config["plugins"] = [
                {"package": home + "/.config/opencode/plugins/oc-box-build-permissions"}
            ]
        return config
    return {
        "$schema": "https://opencode.ai/config.json",
        "agent": {"build": {"permission": "allow"}},
    }


def container_build_plugin(release="1.0.0"):
    # Only Build is overridden. V2 applies its final configuration after external
    # transforms; the private content scope supplies the final agent wildcard,
    # while this hook handles inherited session asks without changing their rules.
    if opencode_compat.major(release) == 2:
        return """export default {
  id: "oc-box.build-permissions",
  async setup(ctx) {
    await ctx.agent.transform(editor => {
      editor.update("build", agent => {
        agent.permissions = [{ action: "*", resource: "*", effect: "allow" }]
      })
    })
    await ctx.permission.hook("evaluate", async event => {
      if (event.effect !== "ask") return
      let agentID = event.agent
      if (agentID === undefined) {
        const session = await ctx.session.get({ sessionID: event.sessionID })
        agentID = session.agent
        if (agentID === undefined) {
          const { data: agents } = await ctx.agent.list()
          agentID = agents.find(agent => agent.mode !== "subagent" && !agent.hidden)?.id
        }
      }
      if (agentID === "build") event.effect = "allow"
    })
  },
}
"""
    return """export default async () => ({
  config: async (config) => {
    config.agent ??= {}
    config.agent.build = { ...config.agent.build, permission: { "*": "allow" } }
  },
})
"""


def container_env(data):
    home = data["home"]
    env = {
        "HOME": home,
        "XDG_CONFIG_HOME": home + "/.config",
        "XDG_DATA_HOME": home + "/.local/share",
        "XDG_STATE_HOME": home + "/.local/state",
        "XDG_CACHE_HOME": home + "/.cache",
        "SHELL": "/bin/bash",
        "PWD": container_project(data),
        "OPENCODE_DISABLE_SANDBOX": "1",
        "OC_BOX_CONTAINER": "1",
        "OPENCODE_CONFIG": home + "/.config/opencode/oc-box-profile.json",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "LEMONADE_BASE_URL": "http://host.containers.internal:13305/api/v1",
    }
    # Pass provider/MCP credentials and explicit settings, never host PATH,
    # loader settings, Unix agent/engine sockets, or sandbox proxy variables.
    forwarded = {}
    global_vault = credentials.effective_policy(data)["mode"] == "vault"
    for key, value in os.environ.items():
        if global_vault and key not in ("COLORTERM", "LANG", "LEMONADE_BASE_URL"):
            continue
        if (
            key.endswith(("_API_KEY", "_TOKEN"))
            or key
            in (
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "BRAVE_API_KEY",
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "COLORTERM",
                "LANG",
                "LEMONADE_BASE_URL",
            )
        ) and "\0" not in value:
            forwarded[key] = value
    settings = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "oc-box/config.json"
    )
    if not global_vault and settings.is_file():
        extra = read_json(settings).get("pass_env", [])
        for key in extra:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key):
                raise BoxError("Invalid pass_env entry in oc-box/config.json")
            if key in os.environ:
                forwarded[key] = os.environ[key]
    env.update(credentials.forwarded(data, forwarded))
    # Apply after project/agent files, without lifting Plan or subagent rules.
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        container_build_config(data.get("opencode_version", "1.0.0"), home=home)
    )
    if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2:
        env["OPENCODE_DB"] = "oc-box-v2.db"
    result = {key: rewrite_localhost(value) for key, value in env.items()}
    # Do not rewrite encrypted values that happen to look like localhost URLs.
    result.update(credentials.active_values(data))
    return result


class ContainerCommand(list):
    """Keep forwarded values out of argv/process listings and error messages."""

    def __init__(self, arguments, environment):
        super().__init__(arguments)
        self.environment = environment


def maintenance_host_env():
    """Only host runtime/storage discovery settings needed by rootless Podman.

    Do not copy the whole host environment and subtract known token names:
    custom secrets, shell/loader hooks, proxies, and pass_env must be excluded.
    """
    names = (
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "DBUS_SESSION_BUS_ADDRESS",
        "TMPDIR",
    )
    return {key: os.environ[key] for key in names if key in os.environ}


def maintenance_command(data):
    # env -i also clears secrets stored in the container's create-time Env.
    # HOME is the private volume, not host storage. No shell/profile hooks run.
    term = os.environ.get("TERM", "xterm-256color")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,63}", term):
        term = "xterm-256color"
    variables = {
        "HOME": data["home"],
        "PWD": container_project(data),
        "TERM": term,
        "PATH": "/opt/oc-box/runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
        "OC_BOX_CONTAINER": "1",
        "HISTFILE": "/dev/null",
        "HISTSIZE": "0",
        "HISTFILESIZE": "0",
    }
    shell = r"if [ -x /bin/bash ]; then PS1='[oc-box \u@\h \W] \$ '; export PS1; exec /bin/bash --noprofile --norc -i; else PS1='[oc-box] $ '; export PS1; exec /bin/sh -i; fi"
    return ContainerCommand(
        [
            "podman",
            "exec",
            "-i",
            "-t",
            "--workdir",
            container_project(data),
            data["container"],
            "/usr/bin/env",
            "-i",
            *(f"{key}={value}" for key, value in variables.items()),
            "/bin/sh",
            "-c",
            shell,
        ],
        maintenance_host_env(),
    )


def maintenance_terminal(path, data):
    """Hold the workspace lock; start only an existing container, not setup."""
    if __package__:
        from .terminal_ui import run_tui as relay
    else:
        from terminal_ui import run_tui as relay

    project = validate_project(data["project"])
    selected, _ = find_workspace(project)
    if selected != path:
        raise BoxError(
            "Project marker does not match the selected workspace; refusing maintenance access to a stale environment."
        )
    environment = maintenance_host_env()
    info = inspect_owned(data, env=environment)
    if info is None:
        raise BoxError(
            "Workspace container is missing. 'oc-box terminal' never creates a replacement; use a normal oc-box launch to recreate it explicitly."
        )
    if not any(
        mount.get("Destination") == container_project(data)
        and mount.get("Source") == data["project"]
        for mount in info.get("Mounts", [])
    ):
        raise BoxError("Container workspace mount does not match the host registry.")
    if info["State"].get("Paused"):
        raise BoxError(
            "Container is paused; unpause it from the host before opening a terminal."
        )
    was_running = bool(info["State"]["Running"])
    try:
        if not was_running:
            with loading_stage(
                "Starting existing container for maintenance", stage="container"
            ):
                podman(
                    "start",
                    data["container"],
                    capture=True,
                    env=environment,
                    timeout=60,
                )
        print(
            f"oc-box: Maintenance terminal in {data['container']} at {data['project']}.\n"
            "Host credentials are not forwarded; startup files and shell history are disabled. Type 'exit' to leave.",
            flush=True,
        )
        command = maintenance_command(data)
        return relay(command, env=command.environment, show_loading=False)
    finally:
        if not was_running:
            with loading_stage("Stopping maintenance container", stage="shutdown"):
                stop_container(data, env=environment)


def exec_command(data, command, *, interactive=False):
    forwarded = container_env(data)
    live = workspace_service.live_connection()
    if live and live["workspace"] == data["id"]:
        forwarded["OPENCODE_PASSWORD"] = live["password"]
    environment = os.environ.copy()
    result = ContainerCommand(["podman", "exec", "-i"], environment)
    if interactive and sys.stdin.isatty() and sys.stdout.isatty():
        result.append("-t")
    result.extend(["--workdir", container_project(data)])
    # Keep container HOME/XDG/config settings from changing the host Podman
    # process's storage/config discovery. Only these non-secret settings belong
    # in argv; credentials/custom forwarded values use inherited environment.
    container_only = {
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "SHELL",
        "PWD",
        "OPENCODE_DISABLE_SANDBOX",
        "OC_BOX_CONTAINER",
        "OPENCODE_CONFIG",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_DB",
    }
    for key, value in forwarded.items():
        if key in container_only:
            result.extend(["--env", f"{key}={value}"])
        else:
            environment[key] = value
            result.extend(["--env", key])
    if command and command[0] == CONTAINER_OPENCODE:
        command = (
            container_cli(data, command[1:]) if "--version" not in command else command
        )
    if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2:
        # Clear image/create-time environment first. Public variable NAMES are
        # argv; values arrive only via Podman's inherited --env NAME mechanism.
        bridge = "import os,sys; keys=sys.argv[1].split(','); env={k:os.environ[k] for k in keys if k in os.environ}; os.execvpe(sys.argv[2],sys.argv[2:],env)"
        command = ["python3", "-c", bridge, ",".join([*forwarded, "PATH"]), *command]
    result.extend([data["container"], *command])
    return result


def container_cli(data, arguments):
    live = workspace_service.live_connection()
    return opencode_compat.command(
        data.get("opencode_version", "1.0.0"),
        CONTAINER_OPENCODE,
        arguments,
        server=live["url"] if live and live["workspace"] == data["id"] else None,
    )


def rewrite_localhost(value):
    return re.sub(
        r"(https?://)(?:localhost|127\.0\.0\.1|\[::1\])(?=[:/]|$)",
        r"\1host.containers.internal",
        value,
    )


def rewrite_config(value, key=None):
    if isinstance(value, dict):
        return {name: rewrite_config(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [rewrite_config(item) for item in value]
    # Redirect service endpoints, not OAuth redirect URIs, shell snippets, or
    # arbitrary strings in user instructions/configuration.
    return (
        rewrite_localhost(value)
        if isinstance(value, str) and key in ("url", "baseURL", "endpoint", "serverUrl")
        else value
    )


def image_preview_cli_config():
    """Freeze the unchanged CLI base for seeding, never a persistent opt-in."""
    source = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "opencode"
    )
    refusal = (
        "--image-preview requires a regular, user-owned cli.json and config "
        "directory without symlinks or group/world write access."
    )
    try:
        # Do not silently follow a copied-config selector through a symlink.
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise BoxError(refusal)
        try:
            directory = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            directory = None
        text = None
        if directory is not None:
            try:
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise BoxError(refusal)
                try:
                    descriptor = os.open(
                        "cli.json",
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=directory,
                    )
                except FileNotFoundError:
                    descriptor = None
                if descriptor is not None:
                    with os.fdopen(descriptor, encoding="utf-8", newline="") as stream:
                        info = os.fstat(stream.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_uid != os.getuid()
                            or info.st_nlink != 1
                            or info.st_mode & 0o022
                        ):
                            raise BoxError(refusal)
                        text = stream.read()
            finally:
                os.close(directory)
        config = (
            parse_jsonc(text)
            if text is not None
            else {"$schema": "https://opencode.ai/v2/cli.json"}
        )
        for key in ("prompt", "session"):
            section = config.get(key, {})
            if not isinstance(section, dict):
                raise ValueError("CLI section must be an object")
        # Validate finite JSON values without altering image preferences, other
        # settings, comments or formatting. Only the native client gets the
        # separate two-boolean override, and only for this owner's lifetime.
        encoded = (json.dumps(config, indent=2, allow_nan=False) + "\n").encode()
        return text.encode() if text is not None else encoded
    except (OSError, UnicodeError) as exc:
        raise BoxError(refusal) from exc
    except (ValueError, BoxError) as exc:
        # Never include config text or a parser's source excerpt in diagnostics.
        if isinstance(exc, BoxError) and str(exc) == refusal:
            raise
        raise BoxError(
            "--image-preview requires valid cli.json JSON/JSONC with an object "
            "root and object-valued prompt/session settings; no config was changed."
        ) from exc


def validate_image_preview(version):
    """Return the private CLI bytes validated before registration or reset."""
    if opencode_compat.major(version) != 2:
        raise BoxError("--image-preview requires native OpenCode 2.x.")
    return image_preview_cli_config()


def config_archive(
    data, *, image_preview=False, image_preview_snapshot=None, history_only=False
):
    """Snapshot user config as bytes. Extracted only inside the container."""
    source = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "opencode"
    )
    release = data.get("opencode_version", "1.0.0")
    v2 = opencode_compat.major(release) == 2
    # Manual sync uses normal config transport but never needs the broader
    # reporting helper. Retain the pre-existing archive for its legacy releases.
    ui_reporting = native_tabs.supports_web_tabs(release) and (
        not history_only or release in native_tabs.AUTO_RESUME_RELEASES
    )
    preview_config = None
    if image_preview:
        if not v2:
            raise BoxError("--image-preview requires native OpenCode 2.x.")
        preview_config = (
            image_preview_cli_config()
            if image_preview_snapshot is None
            else image_preview_snapshot
        )
    local_plugins = {}
    replacements = {}
    sandbox_version = None
    config_home = data["home"] + "/.config/opencode"
    if ui_reporting:
        if __package__:
            from . import ui_state
        else:
            import ui_state
        if (source / "plugins/oc-box-ui-state").exists() or (
            source / "plugins/oc-box-ui-state"
        ).is_symlink():
            raise BoxError(
                "Generated UI-state helper conflicts with an existing host plugin."
            )
    if v2 and source.is_dir():
        candidates = set()
        configured = {}
        for filename in ("config.json", "opencode.json", "opencode.jsonc"):
            file = source / filename
            if file.is_file():
                config = parse_jsonc(file.read_text())
                for key in ("plugin", "plugins"):
                    for entry in config.get(key, []):
                        target = (
                            entry
                            if isinstance(entry, str)
                            else entry[0]
                            if isinstance(entry, list)
                            else entry.get("package", "")
                        )
                        local = plugin_compat.local_source(target, source)
                        if local:
                            candidates.add(local)
                            configured[target.removeprefix("-")] = local
                        package = plugin_compat.package_target(target)
                        if package:
                            sandbox_version = package[1]
        for folder in ("plugin", "plugins"):
            candidates.update(
                file
                for file in (source / folder).glob("*")
                if file.is_file() and file.suffix in (".ts", ".js")
            )
        for file in sorted(candidates):
            text = file.read_text()
            native = "@opencode/plugin" in text or re.search(
                r"export\s+default\s*\{", text
            )
            if (
                "@opencode-ai/plugin" in text
                or not native
                or file in configured.values()
            ):
                relative = file.relative_to(source)
                original, directory, script = plugin_compat.bridge_file(relative)
                if (source / original).exists() or (source / directory).exists():
                    raise BoxError(
                        "A generated V2 plugin adapter path conflicts with an existing config file; inspect the plugin directory before launching."
                    )
                local_plugins[relative] = (original, directory, script)
                for alias in (
                    str(file),
                    "file://" + str(file),
                    "./" + str(relative),
                    str(relative),
                ):
                    replacements[alias] = config_home + "/" + str(directory)
                replacements[config_home + "/" + str(relative)] = (
                    config_home + "/" + str(directory)
                )
                for target, local in configured.items():
                    if local == file:
                        replacements[target] = config_home + "/" + str(directory)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:

        def add_bytes(name, content, mode=0o600):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))

        for file in sorted(source.rglob("*")) if source.is_dir() else []:
            relative = file.relative_to(source)
            if (
                any(part in ("node_modules", ".git") for part in relative.parts)
                or (image_preview and relative.parts[0] == "cli.json")
                or not file.is_file()
                or relative == Path("AGENTS.md")
                or (file.name.startswith("service") and file.suffix == ".json")
                or (
                    v2
                    and (local_plugins or sandbox_version)
                    and relative == Path("package.json")
                )
            ):
                continue
            content = file.read_bytes()
            if relative in local_plugins:
                original, directory, script = local_plugins[relative]
                patched = plugin_compat.copied_source(relative, content)
                add_bytes(str(original), patched, file.stat().st_mode & 0o777)
                add_bytes(str(directory / "index.js"), script)
                add_bytes(
                    str(directory / "package.json"),
                    b'{"type":"module","main":"index.js"}',
                )
                continue
            if len(relative.parts) == 1 and file.name in (
                "config.json",
                "opencode.json",
                "opencode.jsonc",
            ):
                config = rewrite_config(parse_jsonc(content.decode()))
                if v2:
                    plugin_compat.rewrite_plugins(
                        config,
                        replacements,
                        config_home + "/plugins/oc-box-compat-sandbox",
                    )
                mcp = config.get("mcp", {})
                for server in (mcp.get("servers", mcp)).values():
                    if isinstance(server, dict) and server.get("type") == "local":
                        # A fresh distro must download npx/uvx packages on first
                        # startup; retain explicit per-server overrides.
                        server.setdefault(
                            "timeout",
                            {"catalog": 120000, "execution": 120000}
                            if "servers" in mcp
                            else 120000,
                        )
                # The host config directory is copied, never mounted. Keep
                # absolute local-plugin paths valid with custom XDG roots too.
                content = (
                    json.dumps(config, indent=2)
                    .replace(str(source), data["home"] + "/.config/opencode")
                    .encode()
                )
            add_bytes(str(relative), content, file.stat().st_mode & 0o777)
        if preview_config is not None:
            add_bytes("cli.json", preview_config)
        if v2 and (local_plugins or sandbox_version):
            if any(
                (source / name).exists()
                for name in ("oc-box-v1-bridge.js", "oc-box-v1-provider-config.json")
            ):
                raise BoxError(
                    "Generated V2 plugin bridge resources conflict with existing host config files."
                )
            if sandbox_version and (source / "plugins/oc-box-compat-sandbox").exists():
                raise BoxError(
                    "Generated sandbox adapter conflicts with an existing host plugin."
                )
            add_bytes(
                "oc-box-v1-bridge.js", (PACKAGE / "plugin_bridge.js").read_bytes()
            )
            manifest_file = source / "package.json"
            manifest = (
                parse_jsonc(manifest_file.read_text())
                if manifest_file.is_file()
                else {}
            )
            manifest.setdefault("dependencies", {}).setdefault(
                "@opencode-ai/plugin", "1.19.5"
            )
            if sandbox_version:
                manifest["dependencies"]["opencode-sandbox"] = sandbox_version
                add_bytes(
                    "plugins/oc-box-compat-sandbox/index.js",
                    plugin_compat.sandbox_entry(),
                )
                add_bytes(
                    "plugins/oc-box-compat-sandbox/package.json",
                    b'{"type":"module","main":"index.js"}',
                )
            add_bytes("package.json", json.dumps(manifest).encode())
        note = (
            "\n\n# oc-box environment for this launch\n"
            "OpenCode and all tools, MCP servers, formatters, language servers, and plugins run INSIDE a rootless Podman container. "
            "The writable project is at the same path as on the host. The HOME here is a private container volume, not the host home. "
            "For this oc-box launch the host instructions about Bubblewrap, the host network allowlist, and read-only HOME do not apply: Bash is not wrapped in Bubblewrap here and HOME is private writable container storage. You may install packages and modify the container OS. "
            "Do not edit .oc-box-id. Host services are reachable as host.containers.internal.\n"
        )
        original = (
            (source / "AGENTS.md").read_text()
            if (source / "AGENTS.md").is_file()
            else ""
        )
        add_bytes("AGENTS.md", (original + note).encode())
        # A final local-scope overlay enables native integrations only when
        # omitted, and selects the container shell, without changing user files.
        merged = {}
        for filename in ("config.json", "opencode.json", "opencode.jsonc"):
            file = source / filename
            if file.is_file():
                merged.update(parse_jsonc(file.read_text()))
        if v2 and (local_plugins or sandbox_version):
            provider_config = rewrite_config(
                {key: merged[key] for key in ("provider", "providers") if key in merged}
            )
            add_bytes(
                "oc-box-v1-provider-config.json", json.dumps(provider_config).encode()
            )
        overlay = {**container_build_config(release), "shell": "/bin/bash"}
        if v2:
            overlay["update"] = "disable"
            overlay["plugins"] = [
                data["home"] + "/.config/opencode/plugins/oc-box-build-permissions"
            ]
        for key in ("formatter",) if v2 else ("lsp", "formatter"):
            if key not in merged:
                overlay[key] = True
        add_bytes("oc-box-profile.json", json.dumps(overlay).encode())
        add_bytes(
            "plugins/oc-box-build-permissions/index.js"
            if v2
            else "plugins/oc-box-build-permissions.js",
            container_build_plugin(release).encode(),
        )
        if v2:
            add_bytes(
                "plugins/oc-box-build-permissions/package.json",
                b'{"type":"module","main":"index.js"}',
            )
            if ui_reporting:
                # Only normal configuration preparation installs this.
                # Native/project/managed plugin policy remains authoritative:
                # unlike the Build helper it gets no final mandatory overlay.
                for name, content in ui_state.package_sources(PACKAGE).items():
                    add_bytes("plugins/oc-box-ui-state/" + name, content)
    return buffer.getvalue()


def ensure_opencode_version(data, version):
    """Use exactly the host release, including downgrades; never fall back."""
    current = run(
        exec_command(data, [CONTAINER_OPENCODE, "--version"]),
        capture=True,
        check=False,
        timeout=30,
    )
    if current.returncode or opencode_compat.version(current.stdout) != version:
        run(
            exec_command(
                data,
                [
                    "npm",
                    "install",
                    "--global",
                    "--force",  # V1 and V2 packages own the same CLI symlink.
                    "--prefix",
                    "/opt/oc-box/runtime",
                    "--no-audit",
                    "--no-fund",
                    f"{'@opencode/cli' if opencode_compat.major(version) == 2 else 'opencode-ai'}@{version}",
                ],
            ),
            timeout=600,
            preparation="native-cli-install",
        )
        installed = run(
            exec_command(data, [CONTAINER_OPENCODE, "--version"]),
            capture=True,
            timeout=30,
        ).stdout.strip()
        if opencode_compat.version(installed) != version:
            raise BoxError(
                f"OpenCode version mismatch: host is {version}, container reports {installed!r}. Refusing to launch a different version."
            )
    if not lifecycle_output.concise():
        startup_output.line(
            f"oc-box: OpenCode {version} matches the host version.", flush=True
        )


def prepare_runtime(
    path,
    data,
    version,
    *,
    image_preview=False,
    image_preview_snapshot=None,
    history_only=False,
):
    # Owning launches carry the preflight bytes past credential prompts/reset;
    # never reopen that CLI input after resource removal.
    if image_preview and opencode_compat.major(version) != 2:
        raise BoxError("--image-preview requires native OpenCode 2.x.")
    archive_options = {"history_only": True} if history_only else {}
    archive = (
        config_archive(
            data,
            image_preview=True,
            image_preview_snapshot=image_preview_snapshot,
            **archive_options,
        )
        if image_preview
        else None
    )
    # Install the unmodified upstream version currently installed on the host.
    # Node/npm/uv are also used by the user's ordinary MCP/LSP setup.
    ensure_opencode_version(data, version)
    v2 = opencode_compat.major(version) == 2
    if v2:
        # Seed the V2-only database from a consistent SQLite snapshot once.
        # Mainline performs its own supported migration; V1 bytes stay intact.
        bridge = (PACKAGE / "opencode_v2_io.py").read_text()
        run(
            exec_command(data, ["python3", "-c", bridge, "seed", CONTAINER_OPENCODE]),
            capture=True,
            timeout=120,
        )
    if archive is None:
        archive = config_archive(data, **archive_options)
    unpack = (PACKAGE / "sync_config.py").read_text()
    copy_command = exec_command(data, ["python3", "-c", unpack, version])
    # binary input requires the binary subprocess mode here.
    try:
        proc = subprocess.run(
            copy_command,
            env=copy_command.environment,
            input=archive,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise BoxError("Copying configuration into the container timed out.") from exc
    if proc.returncode:
        raise BoxError(
            f"Cannot copy OpenCode config into container (exit {proc.returncode})."
        )
    message = proc.stdout.decode(errors="replace").strip()
    reused = message == "Reusing container plugin dependencies."
    if message:
        if (
            reused
            or message == "Plugin dependencies will be resolved by OpenCode if needed."
        ):
            if not lifecycle_output.concise():
                startup_output.line("oc-box: " + message, flush=True)
        else:
            startup_output.line(
                "oc-box: Configuration copy returned an unrecognized status; checking plugin dependencies.",
                file=sys.stderr,
                flush=True,
            )
    if v2 and not reused:
        # V2 can supply its SDK to local plugins, but unlike V1 it does not
        # automatically install a copied config directory's custom manifest.
        with loading_stage(
            "Installing OpenCode 2.x plugin dependencies", stage="dependencies"
        ):
            npm_options = [
                "--prefix",
                data["home"] + "/.config/opencode",
                "--no-audit",
                "--no-fund",
            ]
            managed = bool(sync_config.sdk_security_overrides(version))
            if managed:
                # npm 10 can hoist the SDK's scoped overrides into a lock
                # that its own `ci` rejects. Give both operations the same
                # shallow layout; unrelated, unmanaged installs keep defaults.
                npm_options.append("--install-strategy=shallow")
                # Resolve without executing package scripts. No private package
                # inventory is uploaded to an advisory service.
                run(
                    exec_command(
                        data,
                        [
                            "npm",
                            "install",
                            *npm_options,
                            "--package-lock-only",
                            "--ignore-scripts",
                        ],
                    ),
                    timeout=600,
                    preparation="plugin-dependencies",
                )
                checked = run(
                    exec_command(
                        data, ["python3", "-c", unpack, "check-sdk-lock", version]
                    ),
                    check=False,
                    capture=True,
                    private=True,
                    timeout=30,
                )
                if checked.returncode:
                    raise BoxError(
                        "Plugin dependencies contain a known unsafe glob/minimatch "
                        "version or their resolution could not be verified. "
                        "Review your OpenCode plugin dependency overrides."
                    )
            run(
                exec_command(
                    data,
                    ["npm", "ci" if managed else "install", *npm_options],
                ),
                timeout=600,
                preparation="plugin-dependencies",
            )
            if managed:
                cached = run(
                    exec_command(
                        data, ["python3", "-c", unpack, "mark-sdk-installed", version]
                    ),
                    check=False,
                    capture=True,
                    private=True,
                    timeout=30,
                )
                if cached.returncode:
                    raise BoxError(
                        "Installed OpenCode plugin dependencies did not match "
                        "their verified npm lock."
                    )
    auth = (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / "opencode/auth.json"
    )
    auth_content = None
    if not v2:
        auth_content = os.environ.get("OPENCODE_AUTH_CONTENT") or (
            auth.read_text() if auth.is_file() else None
        )
    if auth_content:
        json.loads(auth_content)
        target = "import os,pathlib,sys; p=pathlib.Path(os.environ['XDG_DATA_HOME'])/'opencode/auth.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.stdin.read()); p.chmod(0o600)"
        run(
            exec_command(data, ["python3", "-c", target]),
            input=auth_content,
            capture=True,
            timeout=30,
        )
    mcp_auth = auth.with_name("mcp-auth.json")
    if not v2 and mcp_auth.is_file():
        target = "import os,pathlib,sys; p=pathlib.Path(os.environ['XDG_DATA_HOME'])/'opencode/mcp-auth.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.stdin.read()); p.chmod(0o600)"
        run(
            exec_command(data, ["python3", "-c", target]),
            input=json.dumps(rewrite_config(read_json(mcp_auth))),
            capture=True,
            timeout=30,
        )
    if v2:
        # Credential values move through supported export/import commands and
        # private in-memory capture/stdin, never argv or a host plaintext file.
        exported = run(
            host_command(data, "auth", "export"),
            env=private_host_env(path, data),
            cwd=path.parent / "controller",
            capture=True,
            private=True,
            timeout=120,
        )
        entries = json.loads(exported.stdout)
        if not isinstance(entries, list):
            raise BoxError("Invalid OpenCode 2.x credential export.")
        # Native saved/OAuth credentials remain independent of env forwarding.
        # A vault-first launch must not reintroduce an ambient legacy env blob.
        if credentials.effective_policy(data)["mode"] != "vault" and os.environ.get(
            "OPENCODE_AUTH_CONTENT"
        ):
            overrides = opencode_compat.legacy_credentials(
                os.environ["OPENCODE_AUTH_CONTENT"]
            )
            replaced = {entry["integrationID"] for entry in overrides}
            entries = [
                entry for entry in entries if entry.get("integrationID") not in replaced
            ] + overrides
        if entries:
            run(
                exec_command(
                    data, ["python3", "-c", bridge, "auth", CONTAINER_OPENCODE]
                ),
                input=json.dumps(entries),
                capture=True,
                private=True,
                timeout=240,
            )
    if not reused and not v2:
        # Native config/plugin initialization resolves the SDK and enabled local
        # plugins. Run before terminal takeover so cold downloads have visible
        # elapsed-time feedback; warm launches skip this extra process.
        with loading_stage(
            "Resolving OpenCode plugin dependencies (first run or changed configuration)",
            stage="dependencies",
        ):
            run(
                exec_command(
                    data,
                    [CONTAINER_OPENCODE, "debug", "agent", "build"],
                ),
                capture=True,
                timeout=180,
            )
    if v2:
        # Resolve the public readiness boundary before inspecting plugin state;
        # early agent/plugin snapshots can legitimately precede activation.
        with loading_stage(
            "Preparing OpenCode 2.x private service and plugins", stage="service"
        ):
            initialized = run(
                exec_command(
                    data, ["python3", "-c", bridge, "plugins", CONTAINER_OPENCODE]
                ),
                capture=True,
                private=True,
                check=False,
                timeout=240,
            )
            if initialized.returncode:
                raise BoxError(
                    opencode_v2_io.fixed_message(initialized.stderr)
                    or f"OpenCode 2.x private service/plugin preparation failed "
                    f"(exit {initialized.returncode})."
                )
            for line in initialized.stdout.splitlines():
                if line.startswith("oc-box: OpenCode 2.x plugin unavailable: "):
                    print(credentials.redact(line), file=sys.stderr, flush=True)
        if (
            not session_file(path).exists()
            and path.with_name("sessions.json").is_file()
        ):
            prior = read_json(path.with_name("sessions.json"))
            atomic_json(
                session_file(path),
                {"sessions": prior.get("sessions", []), "last": prior.get("last")},
            )
    # Also support OpenCode versions that store auth separately from data.
    # Config-directory auth is already part of the config snapshot.


def run_v2_history(data, action, **settings):
    """Use the owning service when present; never elect a host shared service."""

    def failed(result, fallback):
        message = opencode_v2_io.fixed_message(result.stderr)
        if message in opencode_v2_io.PENDING_MESSAGES:
            raise HistoryPending(message)
        error = BoxError(message or fallback)
        if message is not None and message != opencode_v2_io.NOT_READY:
            error.history_contract_refusal = True
        else:
            error.history_retryable = True
        raise error

    stream = history_reader.EXPORTS.get()
    if stream is not None:
        result = stream.run(data, action, settings)
        if result.returncode:
            failed(result, "OpenCode 2.x history export helper failed.")
        return result
    live = workspace_service.live_connection()
    endpoint = (
        {key: live[key] for key in ("url", "password")}
        if live and live["workspace"] == data["id"]
        else None
    )
    reader = history_reader.current()
    arguments = [
        "python3",
        "-c",
        (PACKAGE / "opencode_v2_io.py").read_text(),
        action,
        CONTAINER_OPENCODE,
    ]
    if reader is not None:
        if reader.data["id"] != data["id"] or endpoint is None:
            raise BoxError("History reader does not own this snapshot request.")
        command = reader.command(arguments)
    else:
        command = exec_command(data, arguments)
    result = run(
        command,
        input=json.dumps(
            {"directory": container_project(data), "endpoint": endpoint, **settings}
        ),
        capture=True,
        check=False,
        private=True,
        timeout=180,
    )
    if result.returncode:
        # Only a whitelisted constant explanation is ever shown; native errors
        # and private output stay hidden behind the generic exit status.
        failed(result, f"podman failed (exit {result.returncode})")
    return result


def list_container_sessions(data):
    """V2's CLI lists roots only; use its paginated API for the full inventory."""
    if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2:
        result = run_v2_history(data, "sessions")
        values = json.loads(result.stdout)
        if not isinstance(values, list):
            raise BoxError("Invalid OpenCode 2.x session inventory.")
        for item in values:
            if (
                not isinstance(item, dict)
                or not valid_session_id(item.get("id"))
                or not isinstance(item.get("location"), dict)
                or opencode_compat.directory({"info": item}) != container_project(data)
                or not isinstance(item.get("time"), dict)
                or not isinstance(item["time"].get("updated"), (int, float))
                or (
                    item.get("parentID") is not None
                    and not valid_session_id(item["parentID"])
                )
            ):
                raise BoxError("Invalid OpenCode 2.x session inventory entry.")
        if len({item["id"] for item in values}) != len(values):
            raise BoxError("Duplicate OpenCode 2.x session inventory entry.")
        return values
    result = run(
        exec_command(
            data, [CONTAINER_OPENCODE, "--pure", "session", "list", "--format", "json"]
        ),
        capture=True,
        timeout=60,
    )
    return json.loads(result.stdout or "[]")


def refresh_sessions(path, data):
    values = list_container_sessions(data)
    v2 = opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2
    roots = [
        item
        for item in values
        if opencode_compat.directory({"info": item}) == container_project(data)
        and valid_session_id(item.get("id"))
        and (
            not v2
            or (
                not item.get("parentID")
                and item.get("time", {}).get("archived") is None
            )
        )
    ]
    roots.sort(
        key=lambda item: (
            item.get("time", {}).get("updated", 0) if v2 else item.get("updated", 0)
        ),
        reverse=True,
    )
    state = session_state(path)
    if v2:
        # A complete, successful inventory is authoritative. Retain the last
        # good archives/host associations for deleted or moved-away sources.
        current = [item["id"] for item in values]
        present = set(current)
        previous = state.get("sessions", [])
        state["missing"] = list(
            dict.fromkeys(
                [
                    ident
                    for ident in state.get("missing", []) + previous
                    if ident not in present
                ]
            )
        )
        state["sessions"] = list(
            dict.fromkeys([ident for ident in previous if ident in present] + current)
        )
        state["last"] = roots[0]["id"] if roots else None
    else:
        state["sessions"] = list(
            dict.fromkeys(state.get("sessions", []) + [item["id"] for item in roots])
        )
    if roots:
        state["last"] = roots[0]["id"]
    atomic_json(session_file(path), state)
    return state


def valid_session_id(value):
    return (
        isinstance(value, str) and re.fullmatch(r"ses[A-Za-z0-9_-]+", value) is not None
    )


def restore_archive_directory(exported, project):
    """Verify a host archive's origin before undoing mainline's import rebinding."""
    directory = Path(opencode_compat.directory(exported)).resolve()
    folder = directory.parent
    if (
        directory.name != "controller"
        or folder.parent != state_root() / "workspaces"
        or not re.fullmatch(r"[0-9a-f]{32}", folder.name)
    ):
        return False
    ident = exported["info"]["id"]
    # Plain purge retains minimal archive provenance outside the active registry
    # so a host archive remains recoverable after retiring its old workspace.
    if not (folder / "workspace.json").is_file():
        folder = state_root() / "retired" / folder.name
    registry = folder / "workspace.json"
    saved = folder / "history" / (ident + ".json")
    if not registry.is_file() or not saved.is_file():
        return False
    origin = read_json(registry)
    archive = read_json(saved)
    validate_session_export(archive, ident)
    # A V1 archive may now be exported by V2 after upstream migration. Its
    # saved origin determines the namespace, not the live export's schema.
    namespace = folder.name + ("-v2" if opencode_compat.native(archive) else "")
    prefix = "ses_" + hashlib.sha256(namespace.encode()).hexdigest()[:12] + "_"
    if not ident.startswith(prefix):
        return False
    if origin.get("id") != folder.name or any(
        not isinstance(value, str) or Path(value).resolve() != Path(project).resolve()
        for value in (origin.get("project"), opencode_compat.directory(archive))
    ):
        return False
    # Keep the current host transcript, including any work after its last sync.
    # The private registry/export establish the project, not the archive title.
    opencode_compat.set_directory(exported, project)
    startup_output.line(
        "oc-box: Verified oc-box archive origin; restored its project directory for import.",
        flush=True,
    )
    return True


def export_host_session(path, data, ident):
    """Export only the selected host conversation; never mount the host DB."""
    if not valid_session_id(ident):
        raise BoxError("Invalid host session ID.")
    result = run(
        host_command(data, "--pure", "export", ident),
        env=private_host_env(path, data),
        cwd=path.parent / "controller",
        capture=True,
        timeout=60,
    )
    try:
        exported = json.loads(result.stdout)
    except ValueError as exc:
        raise BoxError(
            "Host session export is invalid/incomplete; nothing was imported."
        ) from exc
    validate_session_export(exported, ident)
    if exported["info"].get("parentID"):
        raise BoxError("Import the parent conversation, not a subagent session.")
    directory = opencode_compat.directory(exported)
    if (
        not isinstance(directory, str)
        or Path(directory).resolve() != Path(data["project"]).resolve()
    ) and not (
        isinstance(directory, str)
        and restore_archive_directory(exported, data["project"])
    ):
        raise BoxError(
            f"Host session belongs to {directory!r}, not {data['project']}. Select its original project with --project. "
            "An oc-box archive also needs its original host registry and history/ export, or the retired archive provenance retained by plain purge, to verify the project."
        )
    return exported


def validate_session_export(exported, ident):
    """Check transcript identities before copying or accepting an import."""
    if (
        not isinstance(exported, dict)
        or not isinstance(exported.get("info"), dict)
        or exported["info"].get("id") != ident
        or not isinstance(exported.get("messages"), list)
    ):
        raise BoxError("Invalid session export.")
    if opencode_compat.native(exported):
        try:
            opencode_compat.validate_native(exported, ident)
        except ValueError as exc:
            raise BoxError(str(exc)) from exc
        return
    messages, parts = set(), set()
    for message in exported["messages"]:
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            raise BoxError("Invalid exported message.")
        info = message.get("info", {})
        mid = info.get("id")
        if (
            not isinstance(mid, str)
            or not re.fullmatch(r"msg[A-Za-z0-9_-]+", mid)
            or mid in messages
            or info.get("sessionID") != ident
            or not isinstance(message.get("parts"), list)
        ):
            raise BoxError("Invalid exported message identity.")
        messages.add(mid)
        for part in message["parts"]:
            if not isinstance(part, dict):
                raise BoxError("Invalid exported part.")
            pid = part.get("id")
            if (
                not isinstance(pid, str)
                or not re.fullmatch(r"prt[A-Za-z0-9_-]+", pid)
                or pid in parts
                or part.get("sessionID") != ident
                or part.get("messageID") != mid
            ):
                raise BoxError("Invalid exported part identity.")
            parts.add(pid)


def export_container_session(data, ident):
    if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2:
        exported = json.loads(run_v2_history(data, "export", session=ident).stdout)
        validate_session_export(exported, ident)
        if opencode_compat.directory(exported) != container_project(data):
            raise BoxError(
                "OpenCode 2.x session moved outside this workspace directory."
            )
        return exported
    # Podman gives the child a pipe even when host output is a regular file.
    # Spool in-container too to avoid mainline's large-stdout truncation.
    spool = """import shutil,subprocess,sys,tempfile
with tempfile.TemporaryFile() as output:
    result=subprocess.run([sys.argv[2],*__import__('json').loads(sys.argv[3])],stdout=output)
    if result.returncode: sys.exit(result.returncode)
    output.seek(0)
    shutil.copyfileobj(output,sys.stdout.buffer)
    sys.stdout.buffer.flush()
"""
    result = run(
        exec_command(
            data,
            [
                "python3",
                "-c",
                spool,
                ident,
                CONTAINER_OPENCODE,
                json.dumps(container_cli(data, ["--pure", "export", ident])[1:]),
            ],
        ),
        capture=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def verify_session_import(data, exported, imported):
    """Verify all transcript content, tolerating one known schema omission.

    Mainline ToolStatePending owns `raw`; the other tool-state schemas strip
    stale raw buffers on import. Permit only its removal from a nonpending
    tool state. Do not ignore changes to raw when it is present in both copies,
    nested user data named raw, or pending raw buffers required for replay.
    """
    ident = exported["info"]["id"]
    validate_session_export(exported, ident)
    validate_session_export(imported, ident)
    if opencode_compat.native(exported):
        if (
            not opencode_compat.native(imported)
            or opencode_compat.directory(imported) != container_project(data)
            or imported["messages"] != exported["messages"]
        ):
            raise BoxError(
                "Imported OpenCode 2.x session verification failed: project or transcript differs."
            )
        return 0
    expected = {message["info"]["id"]: message for message in exported["messages"]}
    actual = {message["info"]["id"]: message for message in imported["messages"]}
    expected = json.loads(json.dumps(expected))
    omitted = 0
    for mid, message in expected.items():
        actual_parts = {
            part["id"]: part for part in actual.get(mid, {}).get("parts", [])
        }
        for part in message["parts"]:
            other = actual_parts.get(part["id"], {})
            state = part.get("state")
            other_state = other.get("state")
            if (
                part.get("type") == other.get("type") == "tool"
                and isinstance(state, dict)
                and isinstance(other_state, dict)
                and state.get("status") in ("running", "completed", "error")
                and state.get("status") == other_state.get("status")
                and "raw" in state
                and "raw" not in other_state
            ):
                del state["raw"]
                omitted += 1
    if (
        imported["info"].get("directory") != container_project(data)
        or actual != expected
    ):
        raise BoxError(
            "Imported session verification failed: project, message, or part content differs "
            f"(source: {len(expected)} messages/{sum(len(m['parts']) for m in expected.values())} parts; "
            f"container: {len(actual)} messages/{sum(len(m['parts']) for m in actual.values())} parts)."
        )
    return omitted


def import_host_session(path, data, exported):
    """Import once, verify the full transcript, then select it for resume."""
    ident = exported["info"]["id"]
    validate_session_export(exported, ident)
    state = session_state(path)
    backup = (
        path.parent
        / ("imports-v2" if opencode_compat.native(exported) else "imports")
        / (ident + ".json")
    )
    existing = any(item.get("id") == ident for item in list_container_sessions(data))
    if existing:
        if not backup.is_file() or ident in state.get("imported_from_host", {}):
            raise BoxError(
                f"Session {ident} already exists in this container; it was not overwritten. Run oc-box normally to resume it."
            )
        # A previous mainline import succeeded but our verification was rejected
        # or interrupted. Recheck its ORIGINAL recovery export without touching
        # the container DB or replacing it with a newer host conversation.
        exported = read_json(backup)
        validate_session_export(exported, ident)
        if opencode_compat.directory(exported) != data["project"]:
            raise BoxError(
                "Recovery export belongs to another project; nothing was overwritten."
            )
        startup_output.line(
            "oc-box: Rechecking the previous import; existing container history will not be overwritten.",
            flush=True,
        )
    else:
        transfer_host_session(path, data, exported, backup)
    imported = export_container_session(data, ident)
    try:
        omitted = verify_session_import(data, exported, imported)
    except BoxError as exc:
        raise BoxError(
            f"{exc} OpenCode was not launched. Original host session and recovery export are intact at {backup}. "
            "No existing container history was overwritten."
        ) from exc
    state["sessions"] = list(dict.fromkeys(state.get("sessions", []) + [ident]))
    state["last"] = ident
    state.setdefault("imported_from_host", {})[ident] = {"backup": str(backup)}
    atomic_json(session_file(path), state)
    if omitted:
        startup_output.line(
            f"oc-box: Verified {omitted} obsolete nonpending tool raw buffer(s) omitted by mainline import.",
            flush=True,
        )
    startup_output.line(
        f"oc-box: {'Recovered' if existing else 'Imported'} host session {ident} ({len(exported['messages'])} messages); original host conversation retained.",
        flush=True,
    )
    return state


def transfer_host_session(path, data, exported, backup):
    # Keep a private host-side source export for recovery. Never place the
    # transcript in the writable project or pass host filenames to the agent.
    atomic_json(backup, exported)
    transfer = """import pathlib,subprocess,sys,tempfile
with tempfile.TemporaryDirectory(prefix='oc-box-import-') as directory:
    target=pathlib.Path(directory)/'session.json'
    with target.open('x') as output:
        target.chmod(0o600)
        output.write(sys.stdin.read())
    args=__import__('json').loads(sys.argv[2]); args=[str(target) if arg=='OC_BOX_IMPORT_FILE' else arg for arg in args]
    result=subprocess.run([sys.argv[1],*args])
    sys.exit(result.returncode)
"""
    payload = json.loads(json.dumps(exported))
    opencode_compat.set_directory(payload, container_project(data))
    # Import is workspace activity: otherwise a newer pre-existing conversation
    # would immediately displace this older imported session on the next launch.
    payload["info"].setdefault("time", {})["updated"] = max(
        int(time.time() * 1000), payload["info"].get("time", {}).get("updated", 0)
    )
    run(
        exec_command(
            data,
            [
                "python3",
                "-c",
                transfer,
                CONTAINER_OPENCODE,
                json.dumps(
                    container_cli(data, ["--pure", "import", "OC_BOX_IMPORT_FILE"])[1:]
                ),
            ],
        ),
        input=json.dumps(payload),
        capture=True,
        timeout=60,
    )


def archive_id(workspace_id, ident, prefix):
    # OpenCode orders messages/parts by ID. Keep the original sortable suffix.
    return (
        prefix
        + "_"
        + hashlib.sha256(workspace_id.encode()).hexdigest()[:12]
        + "_"
        + ident.split("_", 1)[-1]
    )


def archive_session(export, data):
    """Namespace container-controlled IDs before importing to the host DB."""
    if (
        not isinstance(export, dict)
        or not isinstance(export.get("info"), dict)
        or not isinstance(export.get("messages"), list)
    ):
        raise BoxError("Invalid container session export")
    if not valid_session_id(export["info"].get("id")):
        raise BoxError("Invalid container session ID")
    if opencode_compat.native(export):
        return opencode_compat.native_archive(
            export,
            data,
            lambda ident, prefix: archive_id(data["id"] + "-v2", ident, prefix),
        )
    # Only schema identity fields are changed; conversation/tool text is intact.
    result = json.loads(json.dumps(export))
    mapping = {}
    info = result["info"]
    mapping[info["id"]] = archive_id(data["id"], info["id"], "ses")
    for message in result["messages"]:
        ident = message["info"]["id"]
        if not isinstance(ident, str) or not re.fullmatch(r"msg[A-Za-z0-9_-]+", ident):
            raise BoxError("Invalid exported message ID")
        mapping[ident] = archive_id(data["id"], ident, "msg")
        for part in message["parts"]:
            if not isinstance(part.get("id"), str) or not re.fullmatch(
                r"prt[A-Za-z0-9_-]+", part["id"]
            ):
                raise BoxError("Invalid exported part ID")
            mapping[part["id"]] = archive_id(data["id"], part["id"], "prt")

    def remap(value):
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, dict):
            return {
                key: mapping.get(item, item)
                if key in ("id", "sessionID", "messageID", "parentID", "partID")
                and isinstance(item, str)
                else remap(item)
                for key, item in value.items()
            }
        return value

    result = remap(result)
    result["info"]["directory"] = data["project"]
    result["info"].pop("parentID", None)
    result["info"].pop("permission", None)
    result["info"]["title"] = f"[oc-box {Path(data['project']).name}] " + info.get(
        "title", "Session"
    )
    return result


def guard_host_archive_children(path, data, ident):
    """Native delete is recursive, even across children not owned by oc-box."""
    result = run(
        host_command(data, "api", "get", "/api/session?parentID=" + ident + "&limit=1"),
        env=private_host_env(path, data),
        cwd=path.parent / "controller",
        capture=True,
        private=True,
        timeout=60,
    )
    response = json.loads(result.stdout)
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("data"), list)
        or not isinstance(response.get("cursor"), dict)
        or (not response["data"] and response["cursor"].get("next") is not None)
    ):
        raise BoxError("Invalid host archive child inventory; no archive was replaced.")
    if response["data"]:
        raise BoxError(
            "Host archive has linked child sessions; refusing recursive replacement. "
            "Container history and host conversations are retained."
        )


def verify_host_archive(path, data, archived):
    """A successful native CLI import may be a conflict/no-op, not an update."""
    ident = archived["info"]["id"]
    result = run(
        host_command(data, "--pure", "export", ident),
        env=private_host_env(path, data),
        cwd=path.parent / "controller",
        capture=True,
        private=True,
        timeout=60,
    )
    imported = json.loads(result.stdout)
    validate_session_export(imported, ident)
    source_time = archived["info"].get("time", {})
    idle = source_time.get("idle")
    # Native import preserves creation/archive/idle times, clips busy/viewed to
    # the idle boundary, and assigns a new updated time. Verify those semantics
    # rather than accepting stale metadata or demanding timestamp byte equality.
    expected_time = {
        "created": source_time.get("created"),
        "archived": source_time.get("archived"),
        "idle": idle,
        **{
            key: min(idle, source_time[key])
            if idle is not None and source_time.get(key) is not None
            else None
            for key in ("busy", "viewed")
        },
    }
    if (
        imported["messages"] != archived["messages"]
        or imported["info"].get("metadata", {}) != archived["info"].get("metadata", {})
        or any(
            imported["info"].get(key) != archived["info"].get(key)
            for key in ("title", "agent", "model", "cost", "tokens")
        )
        or opencode_compat.directory(imported) != str(path.parent / "controller")
        or any(
            key in imported["info"]
            for key in ("parentID", "permissions", "fork", "revert")
        )
        or any(
            imported["info"].get("time", {}).get(key) != value
            for key, value in expected_time.items()
        )
        or imported["info"].get("outcome")
        != (archived["info"].get("outcome") if idle is not None else None)
    ):
        raise BoxError(
            "Host archive verification failed; history sync remains pending."
        )


def _announce_history_synced(*, recovered=False):
    with contextlib.suppress(OSError, ValueError):
        startup_output.line(
            "Session history synced to your normal host OpenCode database"
            + (" using the post-stop fallback." if recovered else "."),
            flush=True,
        )


def sync_sessions(path, data, *, announce=True):
    started = time.monotonic()
    source_mode = (
        "post-stop-reader"
        if history_reader.current() is not None
        else "private-endpoint"
        if workspace_service.live_connection() is not None
        else "standalone"
    )
    counts = dict.fromkeys(
        ("sessions", "exported", "unchanged", "updated", "pending", "interrupted"), 0
    )
    report = history_recovery.REPORT.get()
    reason = None
    try:
        with history_reader.export_pass(sys.modules[__name__], data):
            populated = _sync_sessions(path, data, started, source_mode, counts)
    except BaseException as exc:
        reason = _history_failure_reason(exc)
        if report is not None and reason == "cleanup-unverified":
            # Remember this before outer lease/stop cleanup can raise its own
            # exception. Reporting must not change which exception wins.
            report.reason = reason
        raise
    else:
        if report is not None:
            report.populated = populated
            report.omitted_interrupted_assistants = bool(counts["interrupted"])
    finally:
        if report is not None:
            report.verified |= bool(counts["unchanged"] or counts["updated"])
        lifecycle_output.record(
            "history.pass",
            level="debug",
            stage=source_mode,
            reason=reason,
            outcome="success" if reason is None else "failure",
            duration_ms=int(max(0, min(time.monotonic() - started, 9999)) * 1000),
            metrics={key: min(count, 9999) for key, count in counts.items()},
        )
    if report is None and populated and announce and not lifecycle_output.concise():
        _announce_history_synced()


def _sync_sessions(path, data, started, source_mode, counts):
    state = refresh_sessions(path, data)
    counts["sessions"] = len(state.get("sessions", []))
    archives = path.parent / "history"
    archives.mkdir(exist_ok=True, mode=0o700)
    pending = []
    for ident in state.get("sessions", []):
        if not valid_session_id(ident):
            raise BoxError("Invalid stored session ID")
        try:
            exported = export_container_session(data, ident)
        except HistoryPending as exc:
            # Keep this session's last good archives and continue with the rest.
            pending.append(str(exc))
            counts["pending"] += 1
            continue
        counts["exported"] += 1
        counts["interrupted"] += bool(exported.get(opencode_v2_io.INTERRUPTED))
        archived = archive_session(exported, data)
        target = archives / (archived["info"]["id"] + ".json")
        atomic_json(target, archived)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        state.setdefault("synced", {})
        if state["synced"].get(ident) == digest:
            counts["unchanged"] += 1
            continue
        if ident in state.get("host_sessions", {}):
            if opencode_compat.native(archived):
                guard_host_archive_children(path, data, archived["info"]["id"])
            # Mainline import appends new messages but does not update existing
            # parts or metadata. Replace only this namespaced archive copy.
            run(
                host_command(
                    data, "--pure", "session", "delete", archived["info"]["id"]
                ),
                env=private_host_env(path, data),
                cwd=path.parent / "controller",
                capture=True,
                check=False,
                timeout=60,
            )
        run(
            host_command(data, "--pure", "import", str(target)),
            env=private_host_env(path, data),
            cwd=path.parent / "controller",
            capture=True,
            timeout=60,
        )
        if opencode_compat.native(archived):
            verify_host_archive(path, data, archived)
        state["synced"][ident] = digest
        state.setdefault("host_sessions", {})[ident] = archived["info"]["id"]
        atomic_json(session_file(path), state)
        counts["updated"] += 1
    if not lifecycle_output.concise():
        startup_output.line(
            f"oc-box: History sync summary: source={source_mode}, "
            f"sessions={counts['sessions']}, exported={counts['exported']}, "
            f"unchanged={counts['unchanged']}, updated={counts['updated']}, "
            f"pending={counts['pending']}, elapsed={time.monotonic() - started:.1f}s.",
            flush=True,
        )
    if counts["interrupted"] and not lifecycle_output.concise():
        startup_output.line(
            f"oc-box: {counts['interrupted']} session(s) contain interrupted unfinished assistant "
            "messages that native OpenCode omits from its export; their archives mirror "
            "that export and record the omitted count.",
            flush=True,
        )
    if pending:
        raise HistoryPending(
            f"{len(pending)} session(s) not settled yet: "
            + "; ".join(dict.fromkeys(pending))
            + " Their last archives are retained and other sessions were synchronized."
        )
    return bool(state.get("sessions"))


def _history_compatibility_refusal(error):
    reason = getattr(error, "history_compatibility_category", None)
    if history_recovery.compatibility_category(reason) is not None:
        return reason
    if isinstance(error, HistoryWaitTimeout):
        reason = error.recovery_reason
        if history_recovery.compatibility_category(reason) is not None:
            return reason
    return None


def _history_failure_reason(error):
    reader = getattr(error, "history_reader_category", None)
    if getattr(error, "history_source_cleanup_failed", False) or reader in (
        "cleanup-failed",
        "history-cleanup",
    ):
        return "cleanup-unverified"
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "cancelled"
    compatibility = history_recovery.compatibility_category(
        _history_compatibility_refusal(error)
    ) or history_recovery.compatibility_category(reader)
    if compatibility is not None:
        return compatibility
    if isinstance(error, HistoryWaitTimeout):
        return "timeout"
    if isinstance(error, HistoryPending):
        return "pending"
    return "sync-failed"


_HISTORY_FAILURE_DETAILS = {
    "cancelled": "the operation was interrupted",
    "pending": "some sessions were still unsettled",
    "schema": "the OpenCode compatibility contract was not recognized",
    "stop-requested": "a workspace stop was requested",
    "sync-failed": "verification did not finish",
    "timeout": "OpenCode activity did not settle",
    "unsupported-version": "this OpenCode version is not supported for post-stop recovery",
}


def _report_history_result(report, *, recovered, error):
    """Emit the only concise archive outcome, after required cleanup."""
    reason = report.reason
    interruption = " Interrupted work may be incomplete." if report.interrupted else ""
    if reason == "cleanup-unverified":
        level, outcome = "error", "failure"
        message = (
            f"History sync cleanup could not be verified.{interruption} Container data retained. "
            "Inspect the workspace on the host before retrying with 'oc-box sync'."
        )
    elif recovered:
        level, outcome, reason = "warn", "success", "interrupted"
        message = (
            "No history to sync. Interrupted work may be incomplete."
            if report.populated is False
            else "Available history synced. Interrupted work may be incomplete."
        )
    elif reason is not None:
        level = "warn" if report.verified or reason == "cancelled" else "error"
        outcome = (
            "cancelled" if reason in ("cancelled", "stop-requested") else "failure"
        )
        status = "History partially synced" if report.verified else "History not synced"
        detail = _HISTORY_FAILURE_DETAILS.get(reason, "verification did not finish")
        guidance = interruption
        if reason in ("schema", "unsupported-version"):
            guidance += (
                " Check for an oc-box update first; update OpenCode if necessary. "
                "Do not reset or downgrade workspace state."
            )
        message = (
            f"{status}: {detail}.{guidance} "
            "Container data retained; retry with 'oc-box sync'."
        )
    elif report.populated is not None:
        level, outcome = "info", "success"
        reason = None if report.populated else "not-needed"
        message = "History synced." if report.populated else "No history to sync."
        if report.populated and report.omitted_interrupted_assistants:
            message += " Native export omitted interrupted assistant messages."
    else:
        return
    lifecycle_output.record(
        "history.result",
        level=level,
        stage="history-recovery" if report.recovery_attempted else "history-sync",
        reason=reason,
        outcome=outcome,
    )
    if lifecycle_output.concise():
        if error is not None:
            error.history_outcome_reported = True
        try:
            startup_output.line(
                message,
                file=sys.stdout if outcome == "success" else sys.stderr,
                flush=True,
            )
        except (OSError, ValueError):
            # A closed output stream cannot turn a verified sync into failure.
            return


def parse_jsonc(text):
    """Strip comments/trailing commas without altering quoted JSON strings."""
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
    clean = tokens.sub(lambda m: m[0] if m[0].startswith('"') else " ", text)
    strings_and_commas = re.compile(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])')
    clean = strings_and_commas.sub(
        lambda m: m[0] if m[0].startswith('"') else m[1], clean
    )
    value = json.loads(clean)
    if not isinstance(value, dict):
        raise BoxError("OpenCode configuration must be a JSON object.")
    return value


@contextlib.contextmanager
def launch_credentials(data, args):
    """Resolve automatic credentials against this launch's native CLI major.

    Keep the probed version in ephemeral launch arguments, not credential
    metadata. An owning launch can nest this context without another unlock;
    attach and informational paths never enter it.
    """
    active = credentials.is_active(data)
    version = getattr(args, "_opencode_release", None)
    if not active and (
        "credentials" not in data
        or credentials.policy(data)["mode"] == "vault"
        or getattr(args, "no_vault", False)
    ):
        if version is None:
            with loading_stage("Checking OpenCode version", stage="version"):
                version = detect_opencode_release()
            args._opencode_release = version
    try:
        if not active and (
            (version is not None and opencode_compat.major(version) == 2)
            or (
                not getattr(args, "no_vault", False)
                and credentials.policy(data, version=version)["vault"]
            )
        ):
            check_private_output()
        with credentials.unlocked(
            data, version=version, no_vault=getattr(args, "no_vault", False)
        ) as values:
            yield values
    finally:
        if not active and hasattr(args, "_opencode_release"):
            del args._opencode_release


def launch(path, data, args, *, image_preview_snapshot=None):
    # Keep plaintext values out of persisted workspace metadata, os.environ,
    # and informational/maintenance/sync invocations. Launch unlock is local.
    runtime_options = (
        {"image_preview_snapshot": image_preview_snapshot}
        if image_preview_snapshot is not None
        else {}
    )
    if getattr(args, "host_drop", None) is True or effective_host_clipboard(args):
        release = getattr(args, "_opencode_release", None) or detect_opencode_release()
        if getattr(args, "host_drop", None) is True:
            validate_host_drop(release)
        configure_host_clipboard(args, release)
        args._opencode_release = release
    try:
        if args.command == "sync":
            if getattr(args, "no_vault", False):
                raise BoxError("--no-vault is not a history-sync option.")
            return launch_environment(path, data, args, **runtime_options)
        with launch_credentials(data, args):
            return launch_environment(path, data, args, **runtime_options)
    except vault.VaultError as exc:
        raise BoxError(str(exc)) from exc


def launch_environment(path, data, args, *, image_preview_snapshot=None):
    version = getattr(args, "_opencode_release", None)
    if version is None:
        with loading_stage("Checking OpenCode version", stage="version"):
            version = detect_opencode_release()
    validate_host_file_open(args, version, data)
    if getattr(args, "host_drop", None) is True:
        validate_host_drop(version)
    configure_host_clipboard(args, version)
    if effective_image_preview(args):
        if args.command not in ("start", "reset"):
            raise BoxError(
                "--image-preview applies only to an owning start/reset TUI launch."
            )
        if opencode_compat.major(version) != 2:
            raise BoxError("--image-preview requires native OpenCode 2.x.")
        if image_preview_snapshot is None:
            image_preview_snapshot = validate_image_preview(version)
    if opencode_compat.major(version) == 2 or credentials.has_unlocked_values():
        check_private_output()
    if data.get("opencode_version") != version:
        data["opencode_version"] = version
        atomic_json(path, data)
    exported = None
    if args.import_session:
        with loading_stage("Exporting host session", stage="session"):
            exported = export_host_session(path, data, args.import_session)
    code = 1
    recovered = False
    history_started = False
    manual_populated = False
    manual_closing_source = client_returned = owner_closed = False
    error, owner = None, None
    stop_token, source, cleanup_data = None, None, data
    control_refusal = "control-unavailable"
    if (
        opencode_compat.major(version) == 2
        and args.command != "sync"
        and path.is_file()
    ):
        try:
            stop_token = workspace_control.capture(path)
        except workspace_control.ControlError as exc:
            # Optional recovery never repairs unsafe control state or prevents
            # the ordinary owning launch/cleanup from retaining its old behavior.
            control_refusal = (
                "control-uncertain"
                if exc.category == "uncertain-control"
                else "control-state"
            )
        except OSError:
            pass
    concise = lifecycle_output.concise()
    report = history_recovery.Report()
    report_token = history_recovery.REPORT.set(report)
    try:
        with loading_stage(f"Starting {data['distro']} container", stage="container"):
            initial_info = start_container(path, data)
        with loading_stage(
            "Preparing OpenCode and syncing configuration", stage="configuration"
        ):
            prepare_runtime(
                path,
                data,
                version,
                **({"history_only": True} if args.command == "sync" else {}),
                **(
                    {
                        "image_preview": True,
                        "image_preview_snapshot": image_preview_snapshot,
                    }
                    if effective_image_preview(args)
                    else {}
                ),
            )
        if args.command == "sync":
            history_started = True
            report.attempted = True
            with loading_stage("Syncing session history", stage="history-sync"):
                with (
                    workspace_service.history_source(sys.modules[__name__], data)
                    if opencode_compat.major(version) == 2
                    else contextlib.nullcontext()
                ):
                    sync_sessions(path, data, announce=False)
                    manual_closing_source = True
                manual_closing_source = False
                manual_populated = bool(session_state(path).get("sessions"))
            return 0
        with contextlib.ExitStack() as services:
            owner = (
                services.enter_context(
                    workspace_service.Owner(
                        sys.modules[__name__],
                        path,
                        data,
                        ui_bridge=native_tabs.supports_web_tabs(version),
                        **(
                            {"image_preview": True}
                            if effective_image_preview(args)
                            else {}
                        ),
                    )
                )
                if opencode_compat.major(version) == 2
                else None
            )
            if owner:
                refusals = []
                source = history_recovery.capture(
                    sys.modules[__name__],
                    data,
                    initial_info,
                    stop_token,
                    refusal=refusals.append,
                )
                owner._history_source = source
                owner._history_source_refusal = (
                    control_refusal
                    if stop_token is None
                    else refusals[0]
                    if refusals
                    else "source-unavailable"
                )
                if history_reader.snapshot_profile(version) is None:
                    history_recovery.report_compatibility_refusal("unsupported-version")
            history_started = True
            code = launch_client(path, data, args, version, exported, owner)
            client_returned = True
        owner_closed = True
        if owner and report.attempted:
            clients = getattr(owner, "clients", None)
            owner_code = getattr(getattr(owner, "child", None), "returncode", None)
            if (
                type(clients) is not int
                or clients != 0
                or type(owner_code) is not int
                or owner_code != 0
            ):
                report.reason = "cleanup-unverified"
        failure = None
        if owner:
            failure = getattr(owner, "_history_failure", None) or getattr(
                owner, "_history_timeout", None
            )
        if failure is not None:
            code = code or 1
            report.attempted = True
            report.interrupted = getattr(failure, "interrupted_foreground", False)
            if report.reason is None:
                report.reason = _history_failure_reason(failure)
        if (
            source is not None
            and history_recovery.exit_retry_eligible(
                sys.modules[__name__], data, failure
            )
            and type(owner.clients) is int
            and owner.clients == 0
            and type(owner.child.returncode) is int
            and owner.child.returncode == 0
        ):
            cleanup_data = {**data, "container": source.container_id}
            report.recovery_attempted = True
            recovered = (
                history_recovery.recover(sys.modules[__name__], path, data, source)
                is True
            )
            if recovered:
                # Archival can recover without completing interrupted execution.
                # Never clear a real client failure or an observed foreground
                # interruption, or infer a missing client's exit status.
                client_code = getattr(owner, "_client_exit_code", None)
                if type(client_code) is int:
                    code = client_code or int(
                        getattr(failure, "interrupted_foreground", False)
                    )
        elif failure is not None:
            if isinstance(failure, HistoryWaitTimeout):
                history_recovery.report_skipped(
                    history_recovery.skip_reasons(owner, failure, closed=True)
                )
            if not concise:
                startup_output.line(
                    f"oc-box: history sync pending: {failure}\n"
                    "Container data retained. Retry with 'oc-box sync'.",
                    file=sys.stderr,
                )
    except BaseException as exc:
        error = exc
        if report.attempted:
            if manual_closing_source or (
                owner and client_returned and not owner_closed
            ):
                report.reason = "cleanup-unverified"
            elif report.reason != "cleanup-unverified":
                report.reason = _history_failure_reason(exc)
        if history_started and not concise:
            startup_output.line(
                "oc-box: Container data retained. Retry with 'oc-box sync'.",
                file=sys.stderr,
            )
        raise
    finally:
        history_recovery.REPORT.reset(report_token)
        try:
            if cleanup_data is data:
                try:
                    with loading_stage("Stopping container", stage="shutdown"):
                        stop_container(cleanup_data)
                except BaseException as exc:
                    error = exc
                    report.reason = "cleanup-unverified"
                    if not report.attempted:
                        lifecycle_output.record(
                            "lifecycle.notice",
                            level="error",
                            stage="shutdown",
                            reason="cleanup-unverified",
                            outcome="failure",
                        )
                        if concise and isinstance(exc, (BoxError, OSError, ValueError)):
                            exc.history_outcome_reported = True
                        with contextlib.suppress(OSError, ValueError):
                            startup_output.line(
                                "oc-box: Workspace shutdown could not be verified. "
                                "Container data retained; inspect the workspace on the host.",
                                file=sys.stderr,
                            )
                    elif not concise:
                        startup_output.line(
                            "oc-box: Container data retained. Retry with 'oc-box sync'.",
                            file=sys.stderr,
                        )
                    raise
            else:
                try:
                    with (
                        loading_stage("Stopping container", stage="shutdown"),
                        history_recovery.budget(history_recovery.CLEANUP_BUDGET),
                    ):
                        stop_container(cleanup_data)
                except BaseException as exc:
                    # A saved snapshot is not a clean launch outcome unless the
                    # final stop also completes. Do not mask an existing exception.
                    code = code or 1
                    recovered = False
                    report.reason = "cleanup-unverified"
                    if not concise:
                        startup_output.line(
                            "oc-box: Final history-recovery cleanup could not be verified; "
                            "inspect the workspace on the host. Container data retained. "
                            "Retry with 'oc-box sync' after resolving the cleanup failure.",
                            file=sys.stderr,
                        )
                    if not isinstance(
                        exc, (BoxError, OSError, ValueError, subprocess.SubprocessError)
                    ):
                        error = exc
                        raise
        finally:
            if report.attempted:
                _report_history_result(report, recovered=recovered, error=error)
                if (
                    not concise
                    and not recovered
                    and report.reason is None
                    and (
                        manual_populated if args.command == "sync" else report.populated
                    )
                ):
                    _announce_history_synced()
    if recovered and not concise:
        history_recovery.announce_saved()
        _announce_history_synced(recovered=True)
    return code


def native_tab_selection(data, last, connection):
    """Keep history discovery distinct from the native client's open tab set."""
    snapshot = native_tabs.inspect(sys.modules[__name__], data, connection=connection)
    if snapshot.status == "missing":
        return last, True
    if snapshot.status == "invalid":
        startup_output.line(
            "oc-box: Saved native tab state could not be verified; opening native home without reopening history.",
            file=sys.stderr,
        )
        return None, False
    inventory = list_container_sessions(data) if snapshot.tabs else []
    try:
        selected = native_tabs.choose(
            snapshot, inventory, last, directory=container_project(data)
        )
    except native_tabs.TabError as exc:
        raise BoxError(str(exc)) from None
    return selected, False


def launch_client(path, data, args, version, exported, owner=None):
    if exported is not None:
        with loading_stage("Importing and verifying host session", stage="session"):
            sessions = import_host_session(path, data, exported)
    else:
        with loading_stage("Finding workspace session", stage="session"):
            sessions = refresh_sessions(path, data)
    selected_session = sessions.get("last") if not args.new else None
    allow_creation, created_session = True, False
    explicit_web = getattr(args, "web_session", None) if args.command == "web" else None
    if owner and explicit_web is not None:
        if __package__:
            from . import web_sessions
        else:
            import web_sessions
        web_sessions.explicit_session(
            sys.modules[__name__], data, owner.connection, explicit_web
        )
        selected_session, allow_creation = explicit_web, False
    if (
        owner
        and explicit_web is None
        and version in native_tabs.AUTO_RESUME_RELEASES
        and not any(
            (args.new, exported is not None, getattr(args, "resume", False), args.model)
        )
    ):
        selected_session, allow_creation = native_tab_selection(
            data, selected_session, owner.connection
        )
    if owner:
        selected_model = opencode_compat.model(args.model) if args.model else None
        if not selected_session and allow_creation:
            payload = {"location": {"directory": container_project(data)}}
            if selected_model:
                payload["model"] = selected_model
            created = workspace_service.api(
                sys.modules[__name__],
                data,
                owner.connection,
                "POST",
                "/api/session",
                payload,
            )
            selected_session = created["data"]["id"]
            if not valid_session_id(selected_session):
                raise BoxError("Invalid OpenCode 2.x session creation response.")
            created_session = True
        elif selected_model:
            workspace_service.api(
                sys.modules[__name__],
                data,
                owner.connection,
                "POST",
                "/api/session/" + selected_session + "/model",
                {"model": selected_model},
            )
        owner.publish(selected_session)
    command = opencode_compat.tui(
        version,
        CONTAINER_OPENCODE,
        container_project(data),
        selected_session,
        None if owner else args.model,
        server=owner.connection["url"] if owner else None,
    )
    if not lifecycle_output.concise():
        if owner:
            selection = (
                "new session"
                if created_session
                else "resuming " + selected_session
                if selected_session is not None
                else "native home (no open session selected)"
            )
        else:
            selection = (
                "new session"
                if args.new or not sessions.get("last")
                else "resuming " + sessions["last"]
            )
        startup_output.line(
            f"oc-box: {data['distro']} | {data['project']} | {selection}", flush=True
        )
    report = history_recovery.REPORT.get()
    try:
        if args.command == "web":
            assert owner is not None
            code = workspace_service.web_session(
                sys.modules[__name__],
                data,
                {**owner.connection, "session": selected_session, "version": version},
                args,
            )
        else:
            if not lifecycle_output.concise():
                startup_output.line(
                    "oc-box: Opening OpenCode; plugins and MCP servers may need time to initialize.",
                    flush=True,
                )
            file_open = effective_host_file_open(args, data)
            if file_open and owner is None:
                if __package__:
                    from . import file_preview
                else:
                    import file_preview
                file_preview.report_unavailable(
                    "private-native-client-unavailable"
                    if file_preview.supports(version)
                    else "native-version-unsupported"
                )
            code = run_native_tui(
                data,
                workspace_service.client_command(
                    sys.modules[__name__],
                    data,
                    owner.connection,
                    command,
                    interactive=True,
                    ui_owner=True,
                    file_preview=file_open,
                )
                if owner
                else exec_command(data, command, interactive=True),
                owner.connection if owner else None,
                host_drop_enabled=effective_host_drop(args),
                host_drop_required=getattr(args, "host_drop", None) is True,
                **(
                    {
                        "host_clipboard_reader": args._host_clipboard_reader,
                        "host_clipboard_symlinks": effective_host_clipboard_symlinks(
                            args
                        ),
                    }
                    if effective_host_clipboard(args)
                    else {}
                ),
            )
    except (KeyboardInterrupt, SystemExit):
        if report is not None:
            report.attempted = True
        raise
    else:
        if report is not None:
            report.attempted = True
    finally:
        if owner:
            owner.wait_clients()
    if owner:
        owner._client_exit_code = code
    try:
        if owner:
            with loading_stage(
                "Waiting for OpenCode activity to settle", stage="history-wait"
            ):
                workspace_service.wait_history(
                    sys.modules[__name__], data, owner.connection
                )
        with loading_stage("Syncing session history", stage="history-sync"):
            sync_sessions(path, data)
    except (BoxError, ValueError, KeyError) as exc:
        concise = lifecycle_output.concise()
        if report is not None:
            report.reason = _history_failure_reason(exc)
            report.interrupted = getattr(exc, "interrupted_foreground", False)
        if isinstance(exc, HistoryWaitTimeout) and not concise:
            startup_output.line(f"oc-box: {exc.diagnostic}", file=sys.stderr)
            if exc.timing_diagnostic is not None:
                startup_output.line(f"oc-box: {exc.timing_diagnostic}", file=sys.stderr)
        if (
            owner
            and getattr(owner, "_history_source", None) is not None
            and history_recovery.exit_retry_eligible(sys.modules[__name__], data, exc)
        ):
            owner._history_failure = exc
            if isinstance(exc, HistoryWaitTimeout):
                owner._history_timeout = exc
            if not concise:
                startup_output.line(
                    "oc-box: Live history synchronization did not finish. "
                    "Closing the owner before one automatic, bounded "
                    "post-stop persisted-history snapshot attempt.",
                    file=sys.stderr,
                )
                if getattr(exc, "interrupted_foreground", False):
                    startup_output.line(
                        "oc-box: Foreground execution was still active. "
                        "Saving persisted history will not complete interrupted work.",
                        file=sys.stderr,
                    )
            return code or 1
        if isinstance(exc, HistoryWaitTimeout):
            history_recovery.report_skipped(history_recovery.skip_reasons(owner, exc))
            if report is not None:
                report.reason = (
                    history_recovery.compatibility_category(
                        getattr(owner, "_history_source_refusal", None)
                    )
                    or report.reason
                )
        history_recovery.report_compatibility_refusal(
            _history_compatibility_refusal(exc)
        )
        if not concise:
            startup_output.line(
                f"oc-box: history sync pending: {exc}\nContainer data retained. Retry with 'oc-box sync'.",
                file=sys.stderr,
            )
        return code or 1
    return code


def resource_ids(kind, ident):
    command = {
        "container": ("ps", "-aq"),
        "volume": ("volume", "ls", "-q"),
        "network": ("network", "ls", "-q"),
    }[kind]
    return podman(
        *command, "--filter", f"label={LABEL}={ident}", capture=True
    ).stdout.split()


def remove_resources(data, *, keep_home=False):
    # Only resources carrying this exact workspace ownership label are eligible.
    for kind in ("container", "volume", "network"):
        if kind == "volume" and keep_home:
            continue
        for ident in resource_ids(kind, data["id"]):
            info = json.loads(podman(kind, "inspect", ident, capture=True).stdout)[0]
            labels = (
                info.get(
                    "Labels",
                    info.get("labels", info.get("Config", {}).get("Labels", {})),
                )
                or {}
            )
            if labels.get(LABEL) != data["id"]:
                raise BoxError(f"Ownership mismatch for {kind} {ident}")
            podman(
                kind,
                "rm",
                *(["--force"] if kind == "container" else []),
                ident,
                capture=True,
            )
    # Base images are shared cache. Never prune them globally.


def retain_archive_provenance(path, data):
    """Keep private, verified recovery evidence, not an active workspace entry."""
    retired = state_root() / "retired" / data["id"]
    count = 0
    for source in sorted((path.parent / "history").glob("*.json")):
        archive = read_json(source)
        ident = source.stem
        validate_session_export(archive, ident)
        namespace = data["id"] + ("-v2" if opencode_compat.native(archive) else "")
        prefix = "ses_" + hashlib.sha256(namespace.encode()).hexdigest()[:12] + "_"
        if (
            not ident.startswith(prefix)
            or opencode_compat.directory(archive) != data["project"]
        ):
            raise BoxError(
                "Archive provenance does not match the selected workspace; refusing cleanup before history can be retained."
            )
        retired.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(retired / "history" / source.name, archive)
        count += 1
    if count:
        atomic_json(
            retired / "workspace.json", {"id": data["id"], "project": data["project"]}
        )
        print(f"Private archive recovery provenance retained at {retired}.")


def purge(path, data, args):
    if not data.get("restore_incomplete") or args.all or args.delete_sessions:
        try:
            dependencies.require("history")
        except dependencies.DependencyError as exc:
            raise BoxError(str(exc)) from exc
    project = Path(data["project"])
    marker = project / MARKER
    same = (
        project.is_dir()
        and not project.is_symlink()
        and workspace_identity.same_project(data, project)
    )
    marker_matches = (
        same
        and marker.is_file()
        and not marker.is_symlink()
        and marker.read_text().strip() == data["id"]
    )
    if args.all and (project.exists() or project.is_symlink()) and not marker_matches:
        raise BoxError(
            "Project identity or marker belongs to another workspace; refusing --all before deleting any history or resources. For stale duplicate cleanup use plain 'oc-box purge --workspace "
            + data["id"]
            + "' to retain project files and host history."
        )
    if not data.get("restore_incomplete") or args.all or args.delete_sessions:
        release = detect_opencode_release()
        if opencode_compat.major(release) != opencode_compat.major(
            data.get("opencode_version", "1.0.0")
        ):
            raise BoxError(
                "Host/container OpenCode major versions differ. Run a normal launch or 'oc-box sync' with the new host release before purge so history is migrated and verified; no resources were deleted."
            )
    if not args.yes:
        summary = "sandbox resources and oc-box metadata"
        if args.delete_sessions or args.all:
            summary += ", associated OpenCode conversations"
        if args.all:
            summary += f", AND ALL FILES IN {data['project']}"
        answer = input(f"Delete {summary}? Type {data['id'][:8]} to confirm: ")
        if answer != data["id"][:8]:
            raise BoxError("Purge cancelled.")
    # Keep default purge history-preserving even if the previous exit was
    # interrupted. A failed sync leaves the volume intact for manual recovery.
    try:
        if (
            not (args.delete_sessions or args.all)
            and not data.get("restore_incomplete")
            and inspect_owned(data)
            and Path(data["project"]).is_dir()
        ):
            info = inspect_owned(data)
            if not info["State"]["Running"]:
                podman("start", data["container"], capture=True)
            sync_sessions(path, data)
    finally:
        stop_container(data)
    if args.delete_sessions or args.all:
        recorded = session_state(path)
        for ident in recorded.get("host_sessions", {}).values():
            if not re.fullmatch(r"ses[A-Za-z0-9_-]+", ident):
                raise BoxError("Invalid stored session ID.")
            run(
                host_command(data, "--pure", "session", "delete", ident),
                env=private_host_env(path, data),
                cwd=path.parent / "controller",
                timeout=60,
            )
    else:
        retain_archive_provenance(path, data)
    remove_resources(data)
    same = (
        project.is_dir()
        and not project.is_symlink()
        and workspace_identity.same_project(data, project)
    )
    marker_matches = (
        same
        and marker.is_file()
        and not marker.is_symlink()
        and marker.read_text().strip() == data["id"]
    )
    if args.all and project.exists():
        if not marker_matches:
            raise BoxError(
                "Project identity changed; sandbox removed, but refusing to delete the host directory."
            )
        validate_project(project)
        shutil.rmtree(project)
    elif marker_matches:
        marker.unlink()
    shutil.rmtree(path.parent)
    print(
        "Workspace purged."
        + (
            " Project files and OpenCode history retained."
            if not args.all and not args.delete_sessions
            else ""
        )
    )


def parser():
    result = cli_help.LauncherParser(
        prog="oc-box",
        description="Run full OpenCode in rootless Podman; sync session history to the host on exit.",
    )
    result.add_argument(
        "command",
        nargs="?",
        choices=(
            "start",
            "stop",
            "sync",
            "reset",
            "repair",
            "terminal",
            "web",
            "upgrade",
            "secrets",
            "credentials",
            "backup",
            "backup-info",
            "restore",
            "purge",
            "list",
            "distros",
            "status",
            "logs",
            "doctor",
            "completion",
            "tui",
            "share",
            "attach",
            "preferences",
        ),
        default=cli_help.DEFAULT_COMMAND,
        help="operation to run; omitting it starts or resumes OpenCode",
    )
    result.add_argument(
        "--project",
        type=Path,
        metavar="PATH",
        help="select the host project directory",
    )
    info = result.add_mutually_exclusive_group()
    info.add_argument(
        "--status",
        action="store_true",
        help="report workspace CPU, RAM, storage, and session details without starting containers",
    )
    info.add_argument(
        "--about",
        action="store_true",
        help="show version, author, and contributor information",
    )
    info.add_argument(
        "--version", action="store_true", help="show the commit-derived oc-box version"
    )
    formats = result.add_mutually_exclusive_group()
    formats.add_argument(
        "--json",
        action="store_true",
        help="emit the workspace status report as JSON",
    )
    formats.add_argument(
        "--table",
        action="store_true",
        help="show workspace status as human-readable field/value tables",
    )
    result.add_argument(
        "--check",
        action="store_true",
        help="with upgrade, preview the selected published channel without installing",
    )
    channel = result.add_mutually_exclusive_group()
    channel.add_argument(
        "--stable",
        dest="update_channel",
        action="store_const",
        const="stable",
        help="select the stable release channel for upgrade; remember it after a successful selection",
    )
    channel.add_argument(
        "--devel",
        dest="update_channel",
        action="store_const",
        const="devel",
        help="select the development release channel for upgrade; remember it after a successful selection",
    )
    result.add_argument(
        "--vault",
        dest="upgrade_vault",
        action="store_true",
        help="with upgrade, add optional vault dependencies to the selected release",
    )
    result.add_argument(
        "--tui",
        dest="upgrade_tui",
        action="store_true",
        help="with upgrade, add optional dashboard dependencies when the selected release supports them",
    )
    result.add_argument(
        "--maintenance",
        action="store_true",
        help="with terminal, open the restricted maintenance shell instead of a V2 live session client",
    )
    result.add_argument(
        "--auto-update",
        choices=("on", "off"),
        help="with upgrade, toggle automatic updates for a GitHub installation (not a Python package)",
    )
    result.add_argument(
        "--workspace",
        metavar="ID",
        help="select a registered workspace by ID; takes precedence over --project",
    )
    result.add_argument(
        "--distro",
        help="distribution for a new environment; use 'reset --distro NAME' to switch",
    )
    group = result.add_mutually_exclusive_group()
    group.add_argument(
        "--new",
        action="store_true",
        help="start a new conversation in this environment",
    )
    group.add_argument(
        "--resume",
        action="store_true",
        help="require an existing workspace and try its remembered conversation",
    )
    group.add_argument(
        "--import-session",
        metavar="SESSION_ID",
        help="copy a host conversation from this project into the container and resume it",
    )
    result.add_argument(
        "--model",
        metavar="PROVIDER/MODEL",
        help="choose the native OpenCode model for the launch; use provider/model syntax",
    )
    image_preview_choice = result.add_mutually_exclusive_group()
    image_preview_choice.add_argument(
        "--image-preview",
        action="store_const",
        const=True,
        default=None,
        help="V2 owning start/reset TUI only: enable prompt/session image previews for this owner without changing saved CLI preferences",
    )
    image_preview_choice.add_argument(
        "--no-image-preview",
        dest="image_preview",
        action="store_const",
        const=False,
        help="owning start/reset TUI only: skip the launcher preview override for this launch",
    )
    file_open_choice = result.add_mutually_exclusive_group()
    file_open_choice.add_argument(
        "--host-file-open",
        action="store_const",
        const=True,
        default=None,
        help="enable Ctrl-click opening of private host-side file copies for this native TUI client",
    )
    file_open_choice.add_argument(
        "--no-host-file-open",
        dest="host_file_open",
        action="store_const",
        const=False,
        help="disable Ctrl-click host opening for this native TUI client",
    )
    host_drop_choice = result.add_mutually_exclusive_group()
    host_drop_choice.add_argument(
        "--host-drop",
        dest="host_drop",
        action="store_const",
        const=True,
        default=None,
        help="save host file importing enabled for this workspace (default on for supported native TUI clients)",
    )
    host_drop_choice.add_argument(
        "--no-host-drop",
        dest="host_drop",
        action="store_const",
        const=False,
        help="save host file importing disabled for this workspace",
    )
    clipboard_choice = result.add_mutually_exclusive_group()
    clipboard_choice.add_argument(
        "--host-clipboard",
        action="store_const",
        const=True,
        default=None,
        help="save Ctrl+Y host clipboard importing enabled for this workspace (default off)",
    )
    clipboard_choice.add_argument(
        "--no-host-clipboard",
        dest="host_clipboard",
        action="store_const",
        const=False,
        help="save host clipboard importing disabled for this workspace",
    )
    clipboard_symlinks = result.add_mutually_exclusive_group()
    clipboard_symlinks.add_argument(
        "--host-clipboard-symlinks",
        dest="host_clipboard_symlinks",
        action="store_const",
        const=True,
        default=None,
        help="save symlink following for clipboard files (requires clipboard importing; default follows them)",
    )
    clipboard_symlinks.add_argument(
        "--no-host-clipboard-symlinks",
        dest="host_clipboard_symlinks",
        action="store_const",
        const=False,
        help="save strict no-symlink clipboard file importing for this workspace (requires clipboard importing)",
    )
    result.add_argument(
        "--credential-mode",
        choices=("env", "selected", "vault"),
        help="with credentials, select env forwarding, explicit names, or the V2 global vault",
    )
    result.add_argument(
        "--no-vault",
        action="store_true",
        help="V2 owning start/reset/web only: skip vault use for this launch without changing saved policy",
    )
    result.add_argument(
        "--env-secret",
        action="append",
        metavar="NAME",
        help="with credentials, allow a named sourced environment credential; repeatable",
    )
    result.add_argument(
        "--vault-secret",
        action="append",
        metavar="NAME",
        help="with credentials, select a named encrypted vault credential; repeatable",
    )
    result.add_argument(
        "--clear-credentials",
        action="store_true",
        help="with credentials, clear saved credential-name selections before adding new names",
    )
    result.add_argument("--cpus", help="CPU limit (new workspace default: 4)")
    result.add_argument("--memory", help="memory limit (new workspace default: 8g)")
    result.add_argument(
        "--pids-limit",
        type=int,
        metavar="N",
        help="process limit, at least 32 (new workspace default: 2048)",
    )
    result.add_argument(
        "--device",
        action="append",
        metavar="MAPPING",
        help="add a persistent Podman device mapping; repeat for multiple devices",
    )
    result.add_argument(
        "--podman-arg",
        action="append",
        metavar="--OPTION[=VALUE]",
        help="add one persistent Podman create option, e.g. --podman-arg=--group-add=keep-groups",
    )
    result.add_argument(
        "--clear-devices",
        action="store_true",
        help="clear saved device mappings on container creation or reset",
    )
    result.add_argument(
        "--clear-podman-args",
        action="store_true",
        help="clear saved additional Podman options on container creation or reset",
    )
    result.add_argument(
        "--delete-sessions",
        action="store_true",
        help="purge associated conversations too",
    )
    result.add_argument(
        "--all",
        action="store_true",
        help="purge sandbox, conversations, AND project directory",
    )
    result.add_argument(
        "-y", "--yes", action="store_true", help="skip purge confirmation"
    )
    log_config.add_launch_arguments(result)
    return result


def report_status(args):
    if __package__:
        from .status_report import collect_reports, print_report, print_tabular_report
    else:
        from status_report import collect_reports, print_report, print_tabular_report

    if args.workspace or args.project is not None:
        path, data = find_workspace(args.project, args.workspace)
        if data is None:
            raise BoxError(
                "No oc-box workspace registered here. Use 'oc-box list' or '--workspace ID'."
            )
        workspaces = [(path, data)]
    else:
        workspaces = [(path, read_json(path)) for path in workspace_paths()]

    def metrics_podman(*command, **kwargs):
        try:
            return podman(*command, **kwargs)
        except BoxError as exc:
            raise RuntimeError(str(exc)) from exc

    if workspaces:
        check_backend()
    with loading_stage("Collecting workspace resource usage"):
        report = collect_reports(
            workspaces, podman=metrics_podman, session_state=session_state
        )
    if args.json:
        print(json.dumps(report, indent=2))
    elif getattr(args, "table", False):
        print_tabular_report(report)
    else:
        print_report(report)
    return 0


def secrets_parser():
    parser = argparse.ArgumentParser(
        prog="oc-box secrets",
        description="Manage the optional host encrypted vault; values/passphrases use hidden controlling-terminal prompts, never argv/env/stdin.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Actions:\n"
            "  init     Create a new encrypted vault; never replace an existing one.\n"
            "  set      Add or replace the named secret using a masked prompt.\n"
            "  list     Unlock the vault and show credential names only.\n"
            "  remove   Remove the named secret from the vault.\n"
            "  passwd   Unlock the vault and re-encrypt it with a new passphrase."
        ),
    )
    parser.add_argument(
        "action",
        choices=("init", "set", "list", "remove", "passwd"),
        help="vault operation; see action details below",
    )
    parser.add_argument(
        "name", nargs="?", help="credential environment name for set/remove"
    )
    parser.add_argument(
        "--min-passphrase-length",
        type=int,
        help="minimum characters for a new vault passphrase, 8..1024; init/passwd only (default: 8 unless configured)",
    )
    return parser


def secrets_command(argv):
    args = secrets_parser().parse_args(argv)
    if (args.action in ("set", "remove")) != (args.name is not None):
        raise BoxError("Use a credential NAME only with secrets set/remove.")
    if args.min_passphrase_length is not None and args.action not in ("init", "passwd"):
        raise BoxError("--min-passphrase-length applies only to secrets init/passwd.")
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise BoxError(
            "Manage the vault from the host, not from a development container."
        )
    try:
        options = (
            {"min_passphrase_length": args.min_passphrase_length}
            if args.min_passphrase_length is not None
            else {}
        )
        vault.manage(args.action, args.name, **options)
    except vault.VaultError as exc:
        raise BoxError(str(exc)) from exc
    return 0


def credentials_command(args):
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise BoxError(
            "Configure workspace credentials on the host; the private host registry is not exposed here."
        )
    path, data = find_workspace(args.project, args.workspace)
    if data is None:
        raise BoxError(
            "No oc-box workspace registered here. Create it with a normal launch before selecting credentials."
        )
    with workspace_lock(path):
        try:
            if (
                args.credential_mode
                or args.env_secret
                or args.vault_secret
                or args.clear_credentials
            ):
                selected = credentials.configure(
                    data,
                    mode=args.credential_mode,
                    env=args.env_secret or [],
                    secrets=args.vault_secret or [],
                    clear=args.clear_credentials,
                )
                atomic_json(path, data)
            else:
                selected = credentials.policy(data)
        except vault.VaultError as exc:
            raise BoxError(str(exc)) from exc
    print("Credential forwarding mode: " + selected["mode"])
    print("Selected environment names: " + (", ".join(selected["env"]) or "none"))
    print("Selected vault names: " + (", ".join(selected["vault"]) or "none"))
    if selected["mode"] == "env":
        print(
            "Default env-file workflow: automatic token/key and configured pass_env forwarding remains enabled."
        )
    elif selected["mode"] == "vault":
        print(
            "V2 global vault: all current entries are used at the next owning launch; their names and values remain encrypted here. V1 keeps legacy env forwarding."
        )
    print(
        "No secret values were read. Changes apply to the next normal launch, not terminal; copied config/auth and existing container secrets are independent."
    )
    return 0


def completion_parser():
    result = argparse.ArgumentParser(
        prog="oc-box completion",
        description="Write the Bash completion script to stdout without editing shell startup files.",
        epilog="To enable it in the current Bash session: source <(oc-box completion bash)",
    )
    result.add_argument(
        "shell", choices=("bash",), help="shell to generate completion for (Bash)"
    )
    return result


_LOGGING_COMMANDS = frozenset({"start", "reset", "stop", "sync", "terminal", "web"})
_LOGGING_FLAGS = (
    "--log-dir",
    "--journald",
    "--no-journald",
    "--lifecycle-diagnostics",
    "--verbose",
)
_LOGGING_SCOPE_ERROR = "Launcher logging options apply only to start, reset, stop, sync, terminal, and web."
_WEB_ORDER_ERROR = (
    "Use 'oc-box web' first, followed by --project/--workspace/--port/--no-open."
)


def _logging_options_requested(args):
    return (
        args.journald is not None
        or args.log_dir is not None
        or args.lifecycle_diagnostics
        or args.verbose is not None
    )


def _informational_request(args):
    return any(
        getattr(args, name, False)
        for name in ("status", "version", "about", "json", "table")
    )


_STATUS_TABLE_ERROR = (
    "Use --table with status, logs, doctor, preferences, or backup-info."
)
_JOURNALED_COMMANDS = frozenset(
    {
        "start",
        "stop",
        "sync",
        "reset",
        "repair",
        "terminal",
        "web",
        "tui",
        "backup",
        "restore",
        "purge",
        "credentials",
        "secrets",
        "upgrade",
    }
)


def _validate_table_options(args):
    if getattr(args, "table", False) and (
        args.about
        or args.version
        or (args.command != "status" and not (args.command == "start" and args.status))
    ):
        raise BoxError(_STATUS_TABLE_ERROR)


def _validate_logging_options(args):
    if _logging_options_requested(args) and (
        args.command not in _LOGGING_COMMANDS or _informational_request(args)
    ):
        raise BoxError(_LOGGING_SCOPE_ERROR)


def _has_launcher_logging_flag(arguments):
    """Identify otherwise unsupported flags before journaling another command."""
    for argument in arguments:
        if argument == "--":
            break
        flag = argument.partition("=")[0]
        if flag.startswith("--") and any(
            option.startswith(flag) for option in _LOGGING_FLAGS
        ):
            return True
    return False


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "logs":
        if __package__:
            from .logs_cli import cli
        else:
            from logs_cli import cli
        return cli(sys.modules[__name__], arguments[1:])
    if arguments and arguments[0] == "preferences":
        if __package__:
            from .workspace_preferences import cli
        else:
            from workspace_preferences import cli
        return cli(sys.modules[__name__], arguments[1:])
    if arguments and arguments[0] == "attach":
        if __package__:
            from .attachments import cli
        else:
            from attachments import cli
        return cli(sys.modules[__name__], arguments[1:])
    if arguments and arguments[0] == "share":
        if __package__:
            from .sharing import cli
        else:
            from sharing import cli
        return cli(sys.modules[__name__], arguments[1:])
    if arguments and arguments[0] == "tui":
        if __package__:
            from .tui import cli
        else:
            from tui import cli
        return cli(sys.modules[__name__], arguments[1:])
    if arguments and arguments[0] == "completion":
        completion_parser().parse_args(arguments[1:])
        print((APP / "completions/oc-box").read_text(), end="")
        return 0
    if arguments and arguments[0] == "doctor":
        return dependencies.cli(arguments[1:])
    if arguments and arguments[0] in ("backup", "backup-info", "restore"):
        if __package__:
            from .backups import BackupError, cli
        else:
            from backups import BackupError, cli
        try:
            return cli(sys.modules[__name__], arguments)
        except BackupError as exc:
            raise BoxError(str(exc)) from exc
    if arguments and arguments[0] == "secrets":
        return secrets_command(arguments[1:])
    if arguments and arguments[0] == "web":
        web = workspace_service.web_parser().parse_args(arguments[1:])
        if os.environ.get("OC_BOX_CONTAINER") == "1":
            raise BoxError(
                "Run 'oc-box web' from a host terminal; host workspace services are not exposed inside containers."
            )
        if not 0 <= web.port <= 65535:
            raise BoxError("--port must be 0 (automatic) or 1..65535.")
        if web.session is not None and not valid_session_id(web.session):
            raise BoxError("--session must be a valid native session ID.")
        selectors = (
            ["--workspace", web.workspace]
            if web.workspace
            else ["--project", str(web.project or Path.cwd())]
        )
        args = parser().parse_args(["start", *selectors])
        args.command, args.port, args.no_open = "web", web.port, web.no_open
        args.web_session = web.session
        args.no_vault = web.no_vault
        args.attachments = web.attachments
        args.log_dir = web.log_dir
        args.journald = web.journald
        args.lifecycle_diagnostics = web.lifecycle_diagnostics
        args.verbose = web.verbose
        args._opencode_release = detect_opencode_release()
        if opencode_compat.major(args._opencode_release) != 2:
            raise BoxError(
                "'oc-box web' requires OpenCode 2.x; use the native TUI with 1.x."
            )
        if web.attachments:
            if __package__:
                from .web_attachments import SUPPORTED
            else:
                from web_attachments import SUPPORTED
            if args._opencode_release not in SUPPORTED:
                raise BoxError(
                    "The browser attachment adapter requires native OpenCode 2.2.6 or 2.3.0."
                )
        workspace_service.preflight_port(web.port)
    else:
        args = parser().parse_args(argv)
    _validate_logging_options(args)
    with startup_output.invocation():
        if _logging_options_requested(args) and lifecycle_output.command_name() is None:
            # Direct callers may explicitly select CLI behavior. Without options,
            # source-bound harnesses retain the legacy console renderer.
            with (
                log_config.overrides(directory=args.log_dir, journald=args.journald),
                lifecycle_output.invocation(
                    args.command,
                    verbose=args.lifecycle_diagnostics,
                    console_level=args.verbose,
                ),
            ):
                return _main_command(args, argv)
        return _main_command(args, argv)


def _main_command(args, argv):
    _validate_table_options(args)
    lifecycle_output.bind_command(args.command)
    if args.command == "logs":
        raise BoxError("Use 'oc-box logs' first, followed by its filters; see --help.")
    if (args.upgrade_vault or args.upgrade_tui) and args.command != "upgrade":
        raise BoxError("--vault and --tui apply only to upgrade.")
    if args.command == "tui":
        raise BoxError(
            "Use 'oc-box tui' first, followed by its workspace selectors/options; see 'oc-box tui --help'."
        )
    if args.command == "share":
        raise BoxError(
            "Use 'oc-box share PATH' first, followed by its workspace selector; see --help."
        )
    if args.command == "attach":
        raise BoxError(
            "Use 'oc-box attach' first, followed by PATH or --broker and --session; see 'oc-box attach --help'."
        )
    if args.command == "preferences":
        raise BoxError(
            "Use 'oc-box preferences' first, followed by its workspace selector."
        )
    if args.command == "web" and not hasattr(args, "port"):
        raise BoxError(_WEB_ORDER_ERROR)
    if args.maintenance and args.command != "terminal":
        raise BoxError("--maintenance applies only to terminal.")
    if args.image_preview is not None and (
        args.command not in ("start", "reset")
        or args.status
        or args.about
        or args.version
        or args.json
    ):
        raise BoxError(
            "--image-preview and --no-image-preview apply only to an owning start/reset TUI launch."
        )
    if getattr(args, "host_file_open", None) is not None and (
        args.command not in ("start", "reset")
        or args.status
        or args.about
        or args.version
        or args.json
    ):
        raise BoxError(
            "--host-file-open/--no-host-file-open apply only to native start/reset TUI clients."
        )
    validate_host_file_open(args)
    if args.host_drop is not None and (
        args.command not in ("start", "reset")
        or args.status
        or args.about
        or args.version
        or args.json
    ):
        raise BoxError(
            "--host-drop/--no-host-drop apply only to native start/reset TUI clients."
        )
    if args.host_drop is True:
        validate_host_drop()
    if (
        args.host_clipboard is not None or args.host_clipboard_symlinks is not None
    ) and (
        args.command not in ("start", "reset")
        or args.status
        or args.about
        or args.version
        or args.json
    ):
        raise BoxError(
            "Host clipboard options apply only to native start/reset TUI clients."
        )
    configure_host_clipboard(args)
    if args.no_vault and (
        args.command not in ("start", "reset", "web")
        or args.status
        or args.about
        or args.version
        or args.json
    ):
        raise BoxError(
            "--no-vault applies only to an owning OpenCode 2.x start/reset/web launch."
        )
    if args.command == "completion":
        raise BoxError("Use 'oc-box completion bash' with no workspace options.")
    if args.command == "doctor":
        raise BoxError(
            "Use 'oc-box doctor' first, followed by --json, --table or --feature; see its --help."
        )
    if args.command in ("backup", "backup-info", "restore"):
        raise BoxError(
            "Use 'oc-box backup|backup-info|restore' first, followed by that command's options; see its --help."
        )
    if args.command == "secrets":
        raise BoxError("Use 'oc-box secrets ACTION [NAME]' with no workspace options.")
    credential_options = bool(
        args.credential_mode
        or args.env_secret
        or args.vault_secret
        or args.clear_credentials
    )
    if credential_options and args.command != "credentials":
        raise BoxError(
            "Credential selection options apply only to 'oc-box credentials'."
        )
    if args.command == "credentials":
        if any(
            (
                args.status,
                args.json,
                args.about,
                args.version,
                args.check,
                args.auto_update is not None,
                args.update_channel is not None,
                args.distro,
                args.new,
                args.resume,
                args.import_session,
                args.model,
                args.cpus,
                args.memory,
                args.pids_limit is not None,
                args.device,
                args.podman_arg,
                args.clear_devices,
                args.clear_podman_args,
                args.all,
                args.delete_sessions,
                args.yes,
            )
        ):
            raise BoxError(
                "Credentials accepts only workspace selectors and credential-selection options."
            )
        return credentials_command(args)
    if (
        args.check or args.auto_update is not None or args.update_channel is not None
    ) and args.command != "upgrade":
        raise BoxError(
            "--check, --auto-update, --stable and --devel apply only to upgrade."
        )
    if args.command == "upgrade":
        if any(
            (
                args.project is not None,
                args.workspace,
                args.distro,
                args.new,
                args.resume,
                args.import_session,
                args.model,
                args.cpus,
                args.memory,
                args.pids_limit is not None,
                args.device,
                args.podman_arg,
                args.clear_devices,
                args.clear_podman_args,
                args.all,
                args.delete_sessions,
                args.yes,
                args.status,
                args.json,
                args.about,
                args.version,
            )
        ):
            raise BoxError(
                "Upgrade accepts only --check, --stable/--devel, --vault, --tui or --auto-update on/off."
            )
        if args.auto_update is not None and (
            args.check
            or args.update_channel is not None
            or args.upgrade_vault
            or args.upgrade_tui
        ):
            raise BoxError(
                "--auto-update cannot be combined with --check, --stable, --devel, --vault or --tui."
            )
        if os.environ.get("OC_BOX_CONTAINER") == "1":
            raise BoxError(
                "Run 'oc-box upgrade' on the host; the container cannot update its host launcher."
            )
        if os.geteuid() == 0:
            raise BoxError(
                "Run oc-box upgrade as your regular host user, without sudo."
            )
        if __package__:
            from .updater import (
                UpdateError,
                package_managed,
                package_upgrade_message,
                update_settings,
                upgrade,
            )
        else:
            from updater import (
                UpdateError,
                package_managed,
                package_upgrade_message,
                update_settings,
                upgrade,
            )

        if args.auto_update is not None and package_managed(APP):
            if args.auto_update == "on":
                raise BoxError(
                    package_upgrade_message(APP)
                    + " GitHub automatic updates cannot manage a Python-package installation."
                )
            print(package_upgrade_message(APP))
            return 0
        try:
            if args.auto_update is not None:
                update_settings(args.auto_update == "on")
                return 0
            upgrade(
                APP,
                check=args.check,
                channel=args.update_channel,
                with_vault=args.upgrade_vault,
                with_tui=args.upgrade_tui,
            )
        except UpdateError as exc:
            raise BoxError(str(exc)) from exc
        return 0
    if args.about or args.version:
        if (
            args.command != "start"
            or args.json
            or args.import_session
            or args.new
            or args.resume
            or args.all
            or args.delete_sessions
            or args.yes
            or args.device
            or args.podman_arg
            or args.clear_devices
            or args.clear_podman_args
            or args.project is not None
            or args.workspace
            or args.distro
            or args.cpus
            or args.memory
            or args.pids_limit is not None
            or args.model
        ):
            raise BoxError(
                "--about and --version cannot be combined with workspace commands."
            )
        if __package__:
            from .version_info import about_text, get_version_info
        else:
            from version_info import about_text, get_version_info

        print(
            about_text(APP)
            if args.about
            else "oc-box " + get_version_info(APP)["version"]
        )
        return 0
    if args.status:
        if args.command not in ("start", "status"):
            raise BoxError("--status cannot be combined with another command.")
        args.command = "status"
    if args.json and args.command != "status":
        raise BoxError("--json applies only to --status/status.")
    if args.command == "status":
        if (
            args.new
            or args.resume
            or args.import_session
            or args.distro
            or args.cpus
            or args.memory
            or args.pids_limit is not None
            or args.all
            or args.delete_sessions
            or args.yes
            or args.model
            or args.device
            or args.podman_arg
            or args.clear_devices
            or args.clear_podman_args
        ):
            raise BoxError(
                "Status accepts only --project, --workspace, --json or --table."
            )
        return report_status(args)
    args.project = args.project or Path.cwd()
    if args.command in ("repair", "terminal") and (
        args.new
        or args.resume
        or args.import_session
        or args.distro
        or args.cpus
        or args.memory
        or args.pids_limit is not None
        or args.model
        or args.device
        or args.podman_arg
        or args.clear_devices
        or args.clear_podman_args
    ):
        raise BoxError(
            f"Unsupported option for '{args.command}'; see 'oc-box {args.command} --help'."
        )
    if args.command == "terminal":
        if args.all or args.delete_sessions or args.yes:
            raise BoxError(
                "Purge options do not apply to terminal; see 'oc-box terminal --help'."
            )
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise BoxError(
                "'oc-box terminal' requires an interactive host terminal (TTY); redirected or piped input/output is not accepted."
            )
    if args.device or args.podman_arg or args.clear_devices or args.clear_podman_args:
        if args.command not in ("start", "reset"):
            raise BoxError("Device and Podman options apply only to start/reset.")
        for value in args.device or []:
            validate_device(value)
        for value in args.podman_arg or []:
            validate_podman_arg(value)
    if args.import_session:
        if args.command != "start":
            raise BoxError("--import-session applies only to start.")
        if not valid_session_id(args.import_session):
            raise BoxError("Invalid host session ID.")
    if args.distro:
        args.distro = distro_name(args.distro)
    if args.pids_limit is not None and args.pids_limit < 32:
        raise BoxError("--pids-limit must be at least 32.")
    if args.command == "distros":
        default = host_default()
        for name, (image, _) in DISTROS.items():
            print(
                f"{name:12} {image}" + ("  [host default]" if name == default else "")
            )
        return 0
    if args.command == "list":
        if __package__:
            from .status_report import project_condition
        else:
            from status_report import project_condition

        headers = ("WORKSPACE ID", "DISTRO", "PROJECT", "STATUS", "SESSION ID")
        rows = []
        for path in workspace_paths():
            data = read_json(path)
            exists, _ = project_condition(data)
            latest = session_state(path).get("last") or "none"
            rows.append((data["id"], data["distro"], data["project"], exists, latest))
        widths = [
            max(len(row[column]) for row in [headers, *rows])
            for column in range(len(headers))
        ]
        for row in [headers, *rows]:
            print(
                "  ".join(
                    value.ljust(width) for value, width in zip(row, widths)
                ).rstrip()
            )
        for row in rows:
            ident, _, project, condition, _ = row
            if condition in ("identity changed", "marker missing"):
                print(
                    f"oc-box: {ident}: {condition}. Exit/stop its launcher, then use 'oc-box repair --workspace {ident}' to retain its environment and history.",
                    file=sys.stderr,
                )
            elif condition == "marker mismatch":
                print(
                    f"oc-box: {ident}: another workspace owns the marker for {project}. Inspect 'oc-box --status' before stale cleanup with plain 'oc-box purge --workspace {ident}' (no --all or --delete-sessions).",
                    file=sys.stderr,
                )
        return 0
    if args.command != "purge" and (args.all or args.delete_sessions or args.yes):
        raise BoxError("--all, --delete-sessions, and --yes apply only to purge.")
    if args.command in ("start", "reset"):
        startup_output.configure(lambda: _startup_logo_choice(args))
    if args.command == "start" and os.environ.get("OC_BOX_CONTAINER") != "1":
        if __package__:
            from .updater import UpdateError, automatic_upgrade
        else:
            from updater import UpdateError, automatic_upgrade

        try:
            release = automatic_upgrade(APP)
        except (UpdateError, OSError, ValueError) as exc:
            startup_output.line(
                f"oc-box: automatic update unavailable: {exc}; continuing with the installed version.",
                file=sys.stderr,
            )
        else:
            if release is not None:
                environment = {**os.environ, "OC_BOX_UPDATE_CHECKED": "1"}
                os.execve(
                    sys.executable,
                    [
                        sys.executable,
                        str(release / "oc_box.py"),
                        *(sys.argv[1:] if argv is None else argv),
                    ],
                    environment,
                )
    with loading_stage("Checking rootless Podman", stage="backend"):
        check_backend(
            **({"env": maintenance_host_env()} if args.command == "terminal" else {}),
            **(
                {"feature": "launch"}
                if args.command in ("start", "reset", "sync", "web")
                else {}
            ),
        )
    image_preview_snapshot = None
    state_root().mkdir(parents=True, exist_ok=True, mode=0o700)
    path, data = find_workspace(
        args.project, args.workspace, allow_identity_change=args.command == "repair"
    )
    if data is None:
        if args.command not in ("start", "web"):
            raise BoxError(
                "No oc-box workspace registered here. Use 'oc-box list' or '--workspace ID'."
            )
        if args.resume:
            raise BoxError("No workspace/session exists here to resume.")
        project = validate_project(args.project)
        version = getattr(args, "_opencode_release", None)
        if version is None:
            with loading_stage("Checking OpenCode version", stage="version"):
                version = detect_opencode_release()
        if args.no_vault and opencode_compat.major(version) != 2:
            raise BoxError("--no-vault requires native OpenCode 2.x.")
        if args.image_preview:
            image_preview_snapshot = validate_image_preview(version)
        validate_host_file_open(args, version)
        if args.host_drop is True:
            validate_host_drop(version)
        configure_host_clipboard(args, version)
        if opencode_compat.major(version) == 2:
            check_private_output()
        args._opencode_release = version
        path, data = create_workspace(
            project, distro_name(args.distro or host_default())
        )
    if data.get("version") != 2:
        raise BoxError(
            "This workspace uses the retired experimental tool-routing design. Purge it with the older launcher before creating a new environment."
        )
    lifecycle_output.bind_workspace(data.get("id"))
    # Explicit stop is usable from a second terminal. Tool calls
    # never auto-restart a stopped container; the running TUI can then be exited.
    if args.command == "stop":
        if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 1:
            stop_container(data)
        else:
            try:
                workspace_control.request_stop(path)
                with workspace_control.admission(path, timeout=45):
                    stop_container(data)
                    workspace_control.require_certain(path)
            except workspace_control.ControlError as exc:
                # Still honor stop as far as possible, but never claim an ordered
                # successful stop if startup/control state could not be fenced.
                stop_container(data)
                raise BoxError(str(exc)) from None
        print("Container stopped; files, packages, and conversations retained.")
        return 0
    if (
        args.command in ("start", "terminal", "web")
        and opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2
    ):
        if args.command == "terminal" and not args.maintenance:
            with workspace_service.attach(
                sys.modules[__name__], path, data
            ) as connection:
                selected_session = workspace_service.require_session(
                    sys.modules[__name__], connection
                )
                command = workspace_service.client_command(
                    sys.modules[__name__],
                    data,
                    connection,
                    [
                        "python3",
                        "-c",
                        (PACKAGE / "session_terminal.py").read_text(),
                        connection["url"],
                        selected_session,
                        container_project(data),
                    ],
                    interactive=True,
                )
                print(
                    "oc-box: Attaching terminal client to session "
                    + selected_session
                    + "; the existing service and credentials are retained.",
                    flush=True,
                )
                print(
                    "oc-box: Session shell terminal; Ctrl-] detaches and keeps it running, 'exit' closes the shell.",
                    flush=True,
                )
                if __package__:
                    from .terminal_ui import run_tui as relay
                else:
                    from terminal_ui import run_tui as relay
                return relay(command, env=command.environment, show_loading=False)
        if args.command == "start" and workspace_service.controller_running(
            sys.modules[__name__], data
        ):
            if any(
                (
                    args.new,
                    args.import_session,
                    args.model,
                    args.distro,
                    args.cpus,
                    args.memory,
                    args.pids_limit is not None,
                    args.device,
                    args.podman_arg,
                    args.clear_devices,
                    args.clear_podman_args,
                    args.no_vault,
                    args.image_preview is not None,
                )
            ):
                raise BoxError(
                    "An active V2 workspace accepts additional TUI clients with selectors/--resume only; change session/configuration after the owner exits."
                )
            validate_host_file_open(args, data.get("opencode_version"), data)
            configure_host_drop(path, data, args, data.get("opencode_version"))
            configure_host_clipboard_preferences(
                path, data, args, data.get("opencode_version")
            )
            configure_host_clipboard(args, data.get("opencode_version"))
            with workspace_service.attach(
                sys.modules[__name__], path, data
            ) as connection:
                command = workspace_service.client_command(
                    sys.modules[__name__],
                    data,
                    connection,
                    [
                        CONTAINER_OPENCODE,
                        container_project(data),
                        "--server",
                        connection["url"],
                        *(
                            ["--session", connection["session"]]
                            if connection["session"] is not None
                            else []
                        ),
                    ],
                    interactive=True,
                    file_preview=effective_host_file_open(args, data),
                )
                return run_native_tui(
                    data,
                    command,
                    connection,
                    host_drop_enabled=effective_host_drop(args),
                    host_drop_required=args.host_drop is True,
                    **(
                        {
                            "host_clipboard_reader": args._host_clipboard_reader,
                            "host_clipboard_symlinks": effective_host_clipboard_symlinks(
                                args
                            ),
                        }
                        if effective_host_clipboard(args)
                        else {}
                    ),
                )
        if args.command == "web" and workspace_service.controller_running(
            sys.modules[__name__], data
        ):
            if args.no_vault:
                raise BoxError(
                    "An attached web client cannot change vault use; exit the owning launcher before a --no-vault launch."
                )
            with workspace_service.attach(
                sys.modules[__name__], path, data
            ) as connection:
                return workspace_service.web_session(
                    sys.modules[__name__], data, connection, args
                )
    if (
        args.image_preview
        or getattr(args, "host_file_open", None) is True
        or args.host_drop is True
        or effective_host_clipboard(args)
    ):
        version = getattr(args, "_opencode_release", None) or detect_opencode_release()
        if args.image_preview and image_preview_snapshot is None:
            image_preview_snapshot = validate_image_preview(version)
        validate_host_file_open(args, version, data)
        args._opencode_release = version
        configure_host_clipboard(args, version)
    if args.command in ("start", "reset"):
        saved = workspace_ui_preferences(path, data)
        if args.image_preview is None and saved.get("image_preview") is True:
            # Saved policy applies only to a new owner. V1 keeps ordinary
            # behavior with a notice; invalid V2 CLI settings fail before any preference
            # writes, credential unlock or reset.
            version = getattr(args, "_opencode_release", None)
            if version is None:
                with loading_stage("Checking OpenCode version", stage="version"):
                    version = detect_opencode_release()
                args._opencode_release = version
            if opencode_compat.major(version) == 2:
                image_preview_snapshot = validate_image_preview(version)
                args._image_preview_enabled = True
            else:
                startup_output.line(
                    "oc-box: Image preview override is off: native OpenCode 2.x "
                    "is required. The saved preference is unchanged.",
                    flush=True,
                )
        configure_host_drop(
            path, data, args, getattr(args, "_opencode_release", None), saved=saved
        )
        configure_host_clipboard_preferences(
            path, data, args, getattr(args, "_opencode_release", None)
        )
    with workspace_lock(path), contextlib.ExitStack() as launch_context:
        if data.get("restore_incomplete") and args.command != "purge":
            raise BoxError(
                "This workspace restore did not finish. Do not start it; inspect backup recovery staging and use plain purge by ID before retrying into a new destination."
            )
        if args.command in ("start", "reset", "web"):
            # Validate/unlock before setup or reset changes the environment.
            # Wrong/missing credentials must never destroy the old OS.
            try:
                launch_context.enter_context(launch_credentials(data, args))
            except vault.VaultError as exc:
                raise BoxError(str(exc)) from exc
        if args.command in ("start", "reset", "sync", "terminal", "web"):
            refresh_workspace_identity(path, data)
        if args.command == "terminal":
            return maintenance_terminal(path, data)
        if args.command == "repair":
            repair_workspace(path, data)
            return 0
        if args.command in ("start", "reset", "sync", "web"):
            if (
                args.command == "start"
                and args.distro
                and distro_name(args.distro) != data["distro"]
            ):
                raise BoxError(
                    f"Existing workspace uses {data['distro']}. Use 'oc-box reset --distro {args.distro}' to switch."
                )
            configure_podman_options(data, args)
            if args.command == "reset":
                current_path, _ = find_workspace(data["project"])
                if current_path != path:
                    raise BoxError(
                        "Project marker does not match the selected workspace; refusing to reset it. Use 'oc-box --status' to select the current workspace."
                    )
                # Identity and credential validation take precedence; validate
                # CLI compatibility before deleting a working container.
                if (
                    getattr(args, "_opencode_release", None) is not None
                    and detect_opencode_release() != args._opencode_release
                ):
                    raise BoxError(
                        "The host OpenCode release changed during credential preparation; reset was not performed. Retry with the intended executable."
                    )
                if getattr(args, "_opencode_release", None) is None:
                    args._opencode_release = detect_opencode_release()
                if (
                    opencode_compat.major(args._opencode_release) == 2
                    or credentials.has_unlocked_values()
                ):
                    check_private_output()
                check_resource_controllers(data)
                remove_resources(data, keep_home=True)
                data["ready"] = False
                # Reset rebuilds the distro, not the captured custom OS image.
                # Keep any restored internal paths for the preserved home DB.
                data["image"] = DISTROS[data["distro"]][0]
                if args.distro:
                    data["distro"] = distro_name(args.distro)
                    data["image"] = DISTROS[data["distro"]][0]
            for name in ("cpus", "memory"):
                if getattr(args, name):
                    if args.command != "reset" and inspect_owned(data):
                        raise BoxError("Change resource limits using 'oc-box reset'.")
                    data[name] = getattr(args, name)
            if args.pids_limit is not None:
                if args.pids_limit < 32:
                    raise BoxError("--pids-limit must be at least 32.")
                if args.command != "reset" and inspect_owned(data):
                    raise BoxError("Change resource limits using 'oc-box reset'.")
                data["pids"] = args.pids_limit
            atomic_json(path, data)
            return launch(
                path,
                data,
                args,
                **(
                    {"image_preview_snapshot": image_preview_snapshot}
                    if effective_image_preview(args)
                    else {}
                ),
            )
        if args.command == "purge":
            purge(path, data, args)
    return 0


def _help_requested(arguments):
    """Keep help and help-like syntax out of the activity journal."""
    for argument in arguments:
        if argument == "--":
            break
        flag = argument.partition("=")[0]
        if flag.startswith("--"):
            if "--help".startswith(flag):
                return True
        elif "h" in argument and re.fullmatch(r"-(?:[hy]+|[hy]=[hy]*)", argument):
            # Only -h and -y are launcher short switches. Older argparse also
            # treats -y=h as -y -h. Leave validity to the parser, and do not
            # mistake a value such as '-draft checkout' for a flag cluster.
            return True
    return False


def entrypoint():
    arguments = sys.argv[1:]
    informational = _help_requested(arguments) or any(
        arg in arguments for arg in ("--version", "--about", "--status", "--demo")
    )
    command = (
        arguments[0] if arguments and not arguments[0].startswith("-") else "start"
    )
    launch_options = None
    try:
        if command == "web":
            launch_options = workspace_service.web_parser().parse_args(arguments[1:])
            launch_options.command = command
        elif command in _LOGGING_COMMANDS:
            # A selector-first command may be read-only. Use the actual parser
            # before any journal append; only valid options can select a sink.
            launch_options = parser().parse_args(arguments)
            command = launch_options.command
            if command == "web":
                raise BoxError(_WEB_ORDER_ERROR)
        elif (
            command != "logs"
            and not informational
            and _has_launcher_logging_flag(arguments[1:])
        ):
            raise BoxError(_LOGGING_SCOPE_ERROR)
        elif command in _JOURNALED_COMMANDS and command != "tui" and not informational:
            for argument in arguments[1:]:
                if argument == "--":
                    break
                flag = argument.partition("=")[0]
                if flag.startswith("--ta") and "--table".startswith(flag):
                    raise BoxError(_STATUS_TABLE_ERROR)
        if command == "tui" and not informational:
            if __package__:
                from .tui import parser as dashboard_parser
            else:
                from tui import parser as dashboard_parser
            # Use argparse's actual mode, including --dem, before opening the
            # activity journal. The dashboard parser uses only the stdlib.
            informational = dashboard_parser().parse_args(arguments[1:]).demo
        if launch_options is not None:
            _validate_logging_options(launch_options)
            _validate_table_options(launch_options)
            # Argparse accepts abbreviations such as --statu. The parsed
            # request, not the spelling in argv, determines read-only behavior.
            informational = informational or _informational_request(launch_options)
    except BoxError as exc:
        print(f"oc-box: {exc}", file=sys.stderr)
        return 1
    journaled = command in _JOURNALED_COMMANDS and not informational
    started = time.monotonic()

    def journal(event, code=None):
        if not journaled:
            return
        try:
            fields = dict(
                command=lifecycle_output.command_name() or command,
                workspace=lifecycle_output.workspace(),
                exit_code=code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            lifecycle_output.console_event(event, **fields)
            if event != "command.started":
                lifecycle_output.flush()
            if __package__:
                from .activity_log import record_event
            else:
                from activity_log import record_event
            record_event(event, **fields)
        except Exception:
            # Diagnostics must never alter lifecycle results or expose private
            # errors. No argv/env/native output is sent to the journal.
            pass
        except KeyboardInterrupt:
            if event == "command.started":
                raise

    reporting = (
        lifecycle_output.invocation(
            command,
            verbose=launch_options.lifecycle_diagnostics,
            console_level=launch_options.verbose,
        )
        if journaled and command in _LOGGING_COMMANDS
        else contextlib.nullcontext()
    )
    policy = (
        log_config.overrides(
            directory=launch_options.log_dir, journald=launch_options.journald
        )
        if launch_options is not None and command in _LOGGING_COMMANDS
        else contextlib.nullcontext()
    )
    with policy, startup_output.invocation(), reporting:
        try:
            journal("command.started")
            result = main()
        except (BoxError, vault.VaultError, OSError, ValueError) as exc:
            if not getattr(exc, "history_outcome_reported", False):
                startup_output.line(f"oc-box: {exc}", file=sys.stderr)
            result = 1
        except KeyboardInterrupt:
            result = 130
        except SystemExit as exc:
            code = exc.code if type(exc.code) is int else 1
            journal("command.finished" if code == 0 else "command.failed", code)
            raise
        except BaseException as exc:
            journal("command.failed", 1)
            if getattr(exc, "history_outcome_reported", False):
                return 1
            raise
        journal("command.finished" if not result else "command.failed", result or 0)
        return result


if __name__ == "__main__":
    sys.exit(entrypoint())
