"""Isolated native runtime preflight and durable workspace admission."""

from __future__ import annotations

import argparse
import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

if __package__:
    from . import (
        history_recovery,
        lifecycle_output,
        native_preflight,
        native_runtime_guard,
    )
    from . import opencode_compat, startup_output, workspace_control, workspace_identity
else:
    import history_recovery
    import lifecycle_output
    import native_preflight
    import native_runtime_guard
    import opencode_compat
    import startup_output
    import workspace_control
    import workspace_identity


_ACTIVE = ContextVar("oc_box_runtime_safeguard", default=None)
_RELEASE = re.compile(r"2\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
PREFIX = "/opt/oc-box/runtime"
SYSTEM_HELPER = ("/usr/bin/python3", "-I", "-S", "-B")
_HOST_PROBE_PATH = "/usr/local/bin:/usr/bin:/bin"
_MAX_BINARY = 512 * 1024 * 1024
_ISSUE_URL = "https://github.com/groopcd/oc-box/issues/new"
_INCOMPATIBLE = {
    "build-plugin": "build-plugin-failed",
}

# npm rejects loading the same path for both config roles, even when it is
# /dev/null. Use a fresh private HOME/user config and an independent global
# config path, and never expand project or workspace .npmrc values here.
_INSTALL_SOURCE = """
import os, subprocess, sys, tempfile
with tempfile.TemporaryDirectory(prefix="oc-box-native-install-") as home:
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    options = [
        "npm", "install", "--global", "--force", "--ignore-scripts", "--prefix", sys.argv[1],
        "--no-audit", "--no-fund", "--userconfig=" + home + "/user.npmrc",
        "--globalconfig=/dev/null", "--git=/bin/false", "--registry=https://registry.npmjs.org",
        "@opencode/cli@" + sys.argv[2],
    ]
    result = subprocess.run(options, cwd="/", env=env, stdin=subprocess.DEVNULL)
    raise SystemExit(result.returncode)
"""


def release_key(version):
    match = _RELEASE.fullmatch(version) if isinstance(version, str) else None
    return tuple(map(int, match.groups())) if match else None


def version_upgrade(previous, candidate):
    old, new = release_key(previous), release_key(candidate)
    return old is not None and new is not None and new > old


def cli(box, arguments):
    parser = argparse.ArgumentParser(
        prog="oc-box runtime-check",
        description="Probe an exact OpenCode 2.x release in a disposable container.",
        epilog="Requires local rootless Podman and public package access. No workspace or host OpenCode store is opened.",
    )
    parser.add_argument(
        "version", metavar="VERSION", help="exact stable release, e.g. 2.4.0"
    )
    args = parser.parse_args(arguments)
    if release_key(args.version) is None:
        parser.error("VERSION must be an exact stable 2.x.y release.")
    with startup_output.invocation(), lifecycle_output.invocation("runtime-check"):
        preflight(box, args.version)
    return 0


def preflight(box, version, *, expected=None):
    with lifecycle_output.preparation(
        "native-preflight",
        quiet=True,
        metrics=native_preflight.version_metrics(version),
    ) as report:
        try:
            with (
                box.loading_stage(
                    "Checking OpenCode in a disposable container", stage="version"
                )
                if lifecycle_output.concise()
                else contextlib.nullcontext()
            ):
                digest = native_preflight.check(box, version)
                if (
                    not isinstance(digest, str)
                    or not _HASH.fullmatch(digest)
                    or (expected is not None and digest != expected)
                ):
                    raise native_preflight.Refusal("artifact")
        except native_preflight.Refusal as exc:
            category = exc.category
            if category in _INCOMPATIBLE and (
                not getattr(exc, "proof", None)
                or (expected is not None and exc.proof != expected)
            ):
                category = "artifact"
            report["reason"] = native_preflight.LOG_REASONS.get(
                category, "verification"
            )
            report["exit_code"] = exc.exit_code
            report["outcome"] = "refused" if category in _INCOMPATIBLE else "failure"
            if category != exc.category:
                raise _preflight_error(box, version, category) from None
            raise _preflight_error(box, version, category) from exc
        report["outcome"] = "success"
    startup_output.line(
        f"oc-box: OpenCode {version} passed isolated Build-plugin preflight"
        f" (details: {lifecycle_output.preparation_hint()}).",
        flush=True,
    )
    return digest


def _preflight_error(box, version, category, *, remembered=False):
    if category in _INCOMPATIBLE.values() or category in _INCOMPATIBLE:
        how = "previously refused" if remembered else "refused"
        category = _INCOMPATIBLE.get(category, category)
        return box.BoxError(
            f"OpenCode {version} is not supported by this oc-box startup probe ({how}: {category}). "
            "The previous workspace runtime was left in place; no older host executable was started. "
            f"Details: {lifecycle_output.preparation_hint()}; check for an oc-box update or report at {_ISSUE_URL}."
        )
    reference = (
        native_preflight.STAGE_IMAGES.get(os.uname().machine)
        if category == "stage-image"
        else None
    )
    hint = (
        f" Preload the pinned tool image with 'podman pull {reference}'."
        if reference
        else ""
    )
    return box.BoxError(
        f"OpenCode {version} isolated preflight did not pass ({category}). "
        "No live workspace or host OpenCode state was opened by the check. "
        f"This is not a recorded compatibility failure.{hint} Details: {lifecycle_output.preparation_hint()}."
    )


def _probe_digest(box, version):
    digest = hashlib.sha256()
    for name in ("native_preflight.py", "opencode_v2_io.py", "sync_config.py"):
        digest.update((box.PACKAGE / name).read_bytes())
    digest.update(box.container_build_plugin(version).encode("utf-8"))
    digest.update(
        json.dumps(box.container_build_config(version), sort_keys=True).encode("utf-8")
    )
    return digest.hexdigest()


def _candidate_digest(box, path, host, version, *, recheck):
    try:
        probe = _probe_digest(box, version)
        previous = native_runtime_guard.rejected(path)
        if (
            not recheck
            and previous is not None
            and (previous.version, previous.host_digest, previous.probe_digest)
            == (version, host.digest, probe)
            and previous.category in _INCOMPATIBLE.values()
        ):
            with lifecycle_output.preparation(
                "native-preflight",
                quiet=True,
                metrics=native_preflight.version_metrics(version),
            ) as report:
                report.update(outcome="refused", reason=previous.category)
            raise _preflight_error(box, version, previous.category, remembered=True)
        try:
            return preflight(box, version, expected=host.digest)
        except box.BoxError as failure:
            exc = failure.__cause__
            if (
                isinstance(exc, native_preflight.Refusal)
                and exc.category in _INCOMPATIBLE
            ):
                record = native_runtime_guard.Rejection(
                    version, host.digest, probe, _INCOMPATIBLE[exc.category]
                )
                if record != previous:
                    native_runtime_guard.publish_rejection(path, previous, record)
            raise failure from None
    except (OSError, native_runtime_guard.GuardError):
        raise box.BoxError(
            "Cannot safely read or record the OpenCode compatibility decision."
        ) from None


def _signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@dataclass(frozen=True)
class HostBinary:
    fd: int
    identity: tuple
    digest: str

    def command(self, box):
        if _signature(os.fstat(self.fd)) != self.identity:
            raise box.BoxError(
                "The pinned host OpenCode changed; guarded launch refused."
            )
        return f"/proc/{os.getpid()}/fd/{self.fd}"


@contextlib.contextmanager
def pinned_host(box):
    """Keep the selected real ELF, not a mutable PATH shim, for this invocation."""
    fd = None
    try:
        found = shutil.which("opencode")
        if found is None:
            raise ValueError
        path = Path(found).resolve(strict=True)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        identity = _signature(info)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & (stat.S_ISUID | stat.S_ISGID | 0o022)
            or not info.st_mode & 0o111
            or not 1 <= info.st_size <= _MAX_BINARY
            or os.pread(fd, 4, 0) != b"\x7fELF"
        ):
            raise ValueError
        digest = hashlib.sha256()
        offset = 0
        while offset < info.st_size:
            chunk = os.pread(fd, min(1024 * 1024, info.st_size - offset), offset)
            if not chunk:
                raise ValueError
            offset += len(chunk)
            digest.update(chunk)
        if _signature(os.fstat(fd)) != identity:
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        if fd is not None:
            os.close(fd)
        raise box.BoxError(
            "Cannot pin the host OpenCode native executable for guarded startup. "
            "Use the official executable rather than a shell or package-manager shim."
        ) from None
    try:
        yield HostBinary(fd, identity, digest.hexdigest())
    finally:
        os.close(fd)


def read_version(box, executable, *, path=_HOST_PROBE_PATH):
    # Even release discovery uses scratch HOME/config and never consults the
    # native host shared service or the live working tree.
    try:
        with tempfile.TemporaryDirectory(prefix="oc-box-version-") as home:
            result = box.run(
                [executable, "--version"],
                cwd=home,
                capture=True,
                private=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
                env={
                    "HOME": home,
                    "XDG_DATA_HOME": home + "/data",
                    "XDG_CONFIG_HOME": home + "/config",
                    "XDG_STATE_HOME": home + "/state",
                    "XDG_CACHE_HOME": home + "/cache",
                    "XDG_RUNTIME_DIR": home,
                    "PATH": path,
                    "OPENCODE_TEST_HOME": home,
                    "OPENCODE_DISABLE_AUTOUPDATE": "1",
                    "OPENCODE_DISABLE_MODELS_FETCH": "1",
                },
            )
        return opencode_compat.version(result.stdout)
    except (box.BoxError, OSError, UnicodeError, ValueError):
        raise box.BoxError("Cannot verify the pinned host OpenCode version.") from None


def _host_release(box, binary):
    return read_version(box, binary.command(box))


def load(box, path):
    # Do not impose the new sidecar schema on ordinary legacy workspaces.
    # lstat also detects a dangling link; it is not an absent preference.
    try:
        path.with_name("native-runtime-guard.json").lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise box.BoxError("Cannot safely inspect the native runtime guard.") from None
    try:
        return native_runtime_guard.load(path)
    except native_runtime_guard.GuardError as exc:
        raise box.BoxError(str(exc)) from None


def _inspect(box, path, data, *, running):
    """Keep the guarded operation on the same passive, ordinary resources."""
    try:
        project = box.validate_project(Path(data["project"]))
        current_path, _ = box.find_workspace(project)
        if current_path != path or not workspace_identity.same_project(data, project):
            raise ValueError
        info = box.inspect_owned(data)
        if (
            not isinstance(info, dict)
            or not _HASH.fullmatch(info["Id"])
            or info["State"]["Running"] is not running
            or info["State"].get("Paused") is True
            or info["State"].get("Status")
            not in (("running",) if running else ("exited", "stopped"))
            or len(info["Mounts"]) != 2
        ):
            raise ValueError
        signature = history_recovery.passive_workspace_mounts(box, data, info)
        volumes = json.loads(
            box.podman(
                "volume", "inspect", data["home_volume"], capture=True, timeout=15
            ).stdout
        )
        if (
            not isinstance(volumes, list)
            or len(volumes) != 1
            or volumes[0]["Name"] != data["home_volume"]
            or volumes[0]["Driver"] != "local"
            or volumes[0].get("Options")
            or volumes[0]["Labels"].get(box.LABEL) != data["id"]
            or volumes[0]["Mountpoint"] != signature[2]
        ):
            raise ValueError
        return info["Id"], signature
    except (
        OSError,
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        box.BoxError,
    ) as exc:
        if (
            isinstance(exc, history_recovery.RecoveryError)
            and exc.reason == "startup-path-unverified"
        ):
            raise box.BoxError(
                "Guarded startup refused this older container's PATH-dependent entrypoint. "
                "The existing container and data are retained; do not reset or downgrade to bypass the safeguard."
            ) from None
        mode = "running" if running else "stopped"
        raise box.BoxError(
            f"Cannot verify the same {mode} workspace and private home for the runtime safeguard."
        ) from None


@dataclass
class Admission:
    path: Path
    host: HostBinary
    previous: native_runtime_guard.Record | None
    record: native_runtime_guard.Record
    resources: tuple
    stop_token: str
    transition: bool
    launching: bool = True

    def _unchanged(self, box):
        try:
            if not workspace_control.unchanged(self.path, self.stop_token):
                raise box.BoxError(
                    "Stop was requested during guarded preparation; no new native startup was admitted."
                )
        except workspace_control.ControlError as exc:
            raise box.BoxError(str(exc)) from None

    def require_live(self, box, data, *, running):
        if _inspect(box, self.path, data, running=running) != self.resources:
            raise box.BoxError(
                "Workspace resources changed during guarded preparation."
            )

    @contextlib.contextmanager
    def spawn(self, box):
        if not self.launching:
            yield
            return
        try:
            with workspace_control.admission(self.path):
                self._unchanged(box)
                yield
        except workspace_control.ControlError as exc:
            raise box.BoxError(str(exc)) from None

    def _publish(self, box, previous, record):
        try:
            native_runtime_guard.publish(self.path, previous, record)
        except native_runtime_guard.GuardError as exc:
            raise box.BoxError(str(exc)) from None

    def begin(self, box, data):
        """Publish the may-write floor before import or any normal preparation."""
        try:
            with workspace_control.admission(self.path):
                self._unchanged(box)
                self.require_live(box, data, running=False)
                self.host.command(box)
                # Retry a pending publication even when its bytes are visible:
                # the previous writer may have failed the directory fsync.
                if not self.record.ready:
                    self._publish(box, self.previous, self.record)
                elif load(box, self.path) != self.record:
                    raise box.BoxError(
                        "The guarded runtime record changed before admission."
                    )
                self._unchanged(box)
        except workspace_control.ControlError as exc:
            raise box.BoxError(str(exc)) from None

    def start(self, box, data):
        try:
            with workspace_control.admission(self.path), history_recovery.budget(20):
                self._unchanged(box)
                self.require_live(box, data, running=False)
                box.check_resource_controllers(data)
                self._unchanged(box)
                workspace_control.mark_uncertain(self.path)
                box.podman(
                    "start",
                    self.record.container,
                    capture=True,
                    private=True,
                    timeout=15,
                )
                info = box.inspect_owned({**data, "container": self.record.container})
                self.require_live(box, data, running=True)
                workspace_control.finish_transition(self.path)
                self._unchanged(box)
            box.run(
                box.exec_command(
                    data,
                    [
                        "/bin/sh",
                        "-c",
                        'test -r "$1" && test -w "$1" && /bin/ls -A "$1" >/dev/null',
                        "oc-box",
                        box.container_project(data),
                    ],
                ),
                capture=True,
                private=True,
                timeout=30,
            )
            return info
        except workspace_control.ControlError as exc:
            raise box.BoxError(str(exc)) from None
        except history_recovery.RecoveryError:
            raise box.BoxError(
                "Guarded container start could not be verified; existing data is retained."
            ) from None

    def ready(self, box, data):
        """Only the verified foreground owner can acknowledge this transition."""
        try:
            with workspace_control.admission(self.path):
                self._unchanged(box)
                self.require_live(box, data, running=True)
                self.host.command(box)
                if self.record.ready:
                    return
                completed = replace(self.record, ready=True)
                self._publish(box, self.record, completed)
                self.record = completed
        except workspace_control.ControlError as exc:
            raise box.BoxError(str(exc)) from None


def current():
    return _ACTIVE.get()


def private_python(arguments):
    """Pin launcher-authored Python snippets without changing native tool PATH."""
    if not arguments or arguments[0] != "python3":
        return arguments
    index = 1
    while index < len(arguments) and arguments[index] in ("-I", "-S", "-B"):
        index += 1
    if index < len(arguments) and arguments[index] == "-c":
        return [*SYSTEM_HELPER, *arguments[index:]]
    return arguments


def bound_command(arguments, digest):
    arguments = private_python(arguments)
    if PREFIX + "/bin/opencode" not in arguments:
        return arguments
    return [
        *SYSTEM_HELPER,
        "-c",
        native_preflight.EXEC_SOURCE,
        PREFIX,
        digest,
        *arguments,
    ]


def bind_client(box, path, data, connection):
    # These are host-private execution selectors, never trusted from the
    # socket's payload or the native readiness frame.
    connection.pop("_runtime_container", None)
    connection.pop("_runtime_digest", None)
    record = load(box, path)
    if record is None:
        return
    if (
        not record.ready
        or record.version != connection.get("version")
        or record.volume != data.get("home_volume")
        or record.container != _inspect(box, path, data, running=True)[0]
    ):
        raise box.BoxError(
            "The live lease does not match the guarded runtime and private resources."
        )
    connection["_runtime_container"] = record.container
    connection["_runtime_digest"] = record.digest


@contextlib.contextmanager
def admit(box, path, data, args, previous):
    """Run before the vault/readiness path; the caller holds the workspace lock."""
    command = args.command
    if command not in ("start", "web", "sync", "purge"):
        raise box.BoxError(
            "This operation is unavailable on a runtime-guarded workspace. "
            "Use guarded start or sync with the admitted version, or back up the workspace."
        )
    if (
        data.get("ready") is not True
        or data.get("restore_incomplete")
        or data.get("podman_args")
        or data.get("devices")
        or data.get("distro") == "alpine"
        or getattr(args, "podman_arg", None)
        or getattr(args, "device", None)
        or getattr(args, "import_session", None)
    ):
        raise box.BoxError(
            "The runtime safeguard requires an existing stopped V2 workspace with standard resources."
        )
    baseline = (
        previous.version if previous is not None else data.get("opencode_version")
    )
    if release_key(baseline) is None:
        raise box.BoxError(
            "The runtime safeguard requires an existing stable OpenCode 2.x workspace."
        )
    if previous is None and command != "start":
        raise box.BoxError("Use owning 'oc-box start --runtime-guard' to enroll.")
    if command == "purge" and (not previous.ready or args.all or args.delete_sessions):
        raise box.BoxError(
            "Only plain history-preserving purge with the verified runtime is supported for a guarded workspace."
        )
    resources = _inspect(box, path, data, running=False)
    if previous and (previous.container, previous.volume) != (
        resources[0],
        data["home_volume"],
    ):
        raise box.BoxError(
            "Runtime safeguard resource identity changed; no new startup was admitted."
        )
    try:
        token = workspace_control.capture(path)
    except workspace_control.ControlError as exc:
        if exc.category == "uncertain-control":
            raise box.BoxError(
                "The prior container transition is uncertain. No native launch was admitted. "
                "Inspect the stopped resources and preserve a backup; do not reset or downgrade to bypass the fence."
            ) from None
        raise box.BoxError(str(exc)) from None
    with pinned_host(box) as host:
        version = _host_release(box, host)
        if getattr(args, "_opencode_release", version) != version:
            raise box.BoxError(
                "The host OpenCode changed while selecting the guarded runtime."
            )
        key = release_key(version)
        if key is None or key < release_key(baseline):
            raise box.BoxError(
                f"The guarded workspace will not launch below or outside its admitted OpenCode 2.x runtime ({baseline}). "
                "Changing executables does not roll back state."
            )
        transition = previous is None or version != baseline
        if transition and command != "start":
            raise box.BoxError(
                "Only owning 'oc-box start' can admit a new guarded OpenCode runtime."
            )
        if not transition and not previous.ready and command != "start":
            raise box.BoxError(
                "The guarded runtime has not completed owner readiness; retry owning start."
            )
        digest = (
            _candidate_digest(
                box, path, host, version, recheck=getattr(args, "runtime_guard", False)
            )
            if transition
            else previous.digest
        )
        if digest != host.digest:
            raise box.BoxError(
                "The host OpenCode is not the exact verified native artifact; guarded startup refused."
            )
        record = (
            previous
            if not transition
            else native_runtime_guard.Record(
                container=resources[0],
                volume=data["home_volume"],
                version=version,
                digest=digest,
                ready=False,
            )
        )
        scope = Admission(
            path, host, previous, record, resources, token, not record.ready
        )
        scope._unchanged(box)
        args._opencode_release = version
        marker = _ACTIVE.set(scope)
        try:
            yield scope
        finally:
            _ACTIVE.reset(marker)


def container_digest(box, data, *, materialize=False):
    scope = current()
    scope._unchanged(box)
    options = (
        ("--materialize", scope.record.version, scope.record.digest)
        if materialize
        else ()
    )
    try:
        result = box.podman(
            "exec",
            "--workdir",
            "/",
            scope.record.container,
            *SYSTEM_HELPER,
            "-c",
            native_preflight.FINGERPRINT_SOURCE,
            PREFIX,
            *options,
            capture=True,
            private=True,
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        digest = result.stdout.strip()
        return digest if result.returncode == 0 and _HASH.fullmatch(digest) else None
    except (box.BoxError, OSError, UnicodeError, ValueError):
        return None


def verify_container(box, data):
    if container_digest(box, data) != current().record.digest:
        raise box.BoxError(
            "Live container OpenCode did not match the preflight artifact. "
            "The guarded version remains reserved; no older binary was started."
        )


def install_container(box, data):
    """Resolve the package without ever running npm's native postinstall."""
    scope = current()
    scope._unchanged(box)
    command = box.ContainerCommand(
        [
            "podman",
            "exec",
            "--workdir",
            "/",
            scope.record.container,
            *SYSTEM_HELPER,
            "-c",
            _INSTALL_SOURCE,
            PREFIX,
            scope.record.version,
        ],
        box.maintenance_host_env(),
    )
    command.guarded_spawn = True
    box.run(
        command, stdin=subprocess.DEVNULL, timeout=600, preparation="native-cli-install"
    )
    if container_digest(box, data, materialize=True) != scope.record.digest:
        raise box.BoxError(
            "Live container OpenCode could not be materialized from the verified artifact. "
            "The guarded version remains reserved; no older binary was started."
        )
