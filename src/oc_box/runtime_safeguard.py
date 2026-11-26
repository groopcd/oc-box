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
import sys
import tempfile

if __package__:
    from . import (
        entrypoint_migration,
        guarded_devices,
        history_recovery,
        lifecycle_output,
        native_preflight,
        native_runtime_guard,
    )
    from . import opencode_compat, startup_output, workspace_control, workspace_identity
else:
    import entrypoint_migration
    import guarded_devices
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
# System Node distributions may install under /usr/local/bin; neither
# lookup nor npm's interpreter should search the writable runtime prefix.
_INSTALL_SOURCE = """
import os, subprocess, sys, tempfile
with tempfile.TemporaryDirectory(prefix="oc-box-native-install-") as home:
    env = {"HOME": home, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
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


def _official_digest(box, version, expected):
    """Verify the public CLI package before any new protected floor is written."""
    error = (
        f"Cannot verify the host OpenCode {version} against the official package. "
        "The existing protected runtime was not changed. Check registry access "
        "and use the official native executable."
    )
    try:
        with (
            box.loading_stage("Verifying OpenCode package", stage="version")
            if lifecycle_output.concise()
            else contextlib.nullcontext()
        ):
            result = box.run(
                [*SYSTEM_HELPER, "-c", native_preflight.ARTIFACT_SOURCE, version],
                cwd="/",
                env={"PATH": _HOST_PROBE_PATH, "LANG": "C.UTF-8"},
                stdin=subprocess.DEVNULL,
                timeout=120,
                capture=True,
                private=True,
                check=False,
            )
            if result.returncode != 0 or result.stdout != expected + "\n":
                raise ValueError
    except (OSError, ValueError, box.BoxError):
        raise box.BoxError(error) from None
    return expected


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


def _inspect(
    box, path, data, *, running, attach_only=False, engine_env=None, home_review=None
):
    """Keep the guarded operation on the same passive, ordinary resources."""
    check = "project-identity"
    try:
        if attach_only and running is not True:
            raise ValueError
        project = box.validate_project(Path(data["project"]))
        current_path, _ = box.find_workspace(project)
        if current_path != path or not workspace_identity.same_project(data, project):
            raise ValueError
        saved = guarded_devices.paths(data)
        if saved and engine_env is None:
            engine_env = entrypoint_migration.engine_environment(box)
        options = {"env": engine_env} if engine_env is not None else {}
        staged = entrypoint_migration.RETIRE in data
        prefix = (
            ("--remote=false",) if staged or saved or home_review is not None else ()
        )
        check = "container-inspection"
        info = box.inspect_owned(
            data,
            **({"local_only": True} if prefix else {}),
            **({"private": True, "timeout": 12} if home_review is not None else {}),
            **options,
        )
        if not isinstance(info, dict) or not _HASH.fullmatch(info["Id"]):
            raise ValueError
        check = "container-state"
        if (
            info["State"]["Running"] is not running
            or info["State"].get("Paused") is True
            or (
                staged
                and running
                and not entrypoint_migration.running_replacement(info)
            )
            or (
                info["State"].get("Status")
                not in (("running",) if running else ("exited", "stopped"))
                and (running or not entrypoint_migration.fresh_replacement(data, info))
            )
        ):
            raise ValueError
        if home_review is not None and not entrypoint_migration._stopped(
            info, ("exited", "stopped")
        ):
            raise ValueError
        check = "container-mounts"
        if len(info["Mounts"]) != 2:
            raise ValueError
        if entrypoint_migration.RECEIPT in data:
            check = "conversion-receipt"
            if not entrypoint_migration.replacement_matches(
                data, info
            ) or not entrypoint_migration.no_copy_binding(data, info):
                raise ValueError
        devices = (
            guarded_devices.verify(
                box,
                data,
                info,
                engine_env,
                fresh=staged and entrypoint_migration.fresh_replacement(data, info),
            )
            if saved
            else ()
        )
        validate_mounts = (
            history_recovery.live_attachment_mounts
            if attach_only
            else history_recovery.passive_workspace_mounts
        )
        check = "passive-container"
        signature = validate_mounts(box, data, info)
        check = "private-volume"
        volumes = json.loads(
            box.podman(
                *prefix,
                "volume",
                "inspect",
                data["home_volume"],
                capture=True,
                timeout=15,
                **options,
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
        if entrypoint_migration.RECEIPT in data:
            check = "conversion-home-state"
            if not entrypoint_migration.initialized_volume(volumes[0]):
                raise ValueError
            check = "conversion-home-identity"
            if home_review is not None:
                # This observation is for the explicit, non-launching repair.
                # It is never a substitute for admission's recorded identity.
                identity, stable = workspace_identity.snapshot(signature[2])
                if (
                    stable is None
                    or identity[1] != data[entrypoint_migration.RECEIPT]["volume"][1]
                ):
                    raise ValueError
                home_review.append((identity, stable))
            elif not entrypoint_migration.volume_matches(data, signature[2]):
                raise ValueError
        return (info["Id"], signature, devices) if devices else (info["Id"], signature)
    except guarded_devices.DeviceError as exc:
        raise box.BoxError(str(exc)) from None
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
            error = box.BoxError(
                "Guarded startup refused this older container's PATH-dependent entrypoint. "
                "The existing container and data are retained; do not reset or downgrade to bypass the safeguard."
            )
            error.legacy_entrypoint = info["Id"]
            raise error from None
        mode = "running" if running else "stopped"
        if isinstance(exc, history_recovery.RecoveryError) and exc.reason == "mounts":
            check = "container-mounts"
        guidance = (
            " If the original private home is still in place, review "
            "'oc-box repair --workspace WORKSPACE_ID --private-home'."
            if check == "conversion-home-identity"
            and entrypoint_migration.HOME_IDENTITY not in data
            and not entrypoint_migration.in_progress(data)
            and home_review is None
            else ""
        )
        raise box.BoxError(
            f"Cannot verify the same {mode} workspace and private home for the "
            f"runtime safeguard ({check}). No alternate resources were admitted.{guidance}"
        ) from None


def repair_private_home(box, path, data):
    """Review an old converted-home receipt without reopening native state."""
    if __package__:
        from . import uninstall_tty
    else:
        import uninstall_tty

    migration = entrypoint_migration
    record = load(box, path)
    ident = data.get("id")
    if (
        not isinstance(ident, str)
        or not re.fullmatch(r"[a-f0-9]{32}", ident)
        or data.get("container") != "oc-box-" + ident
        or data.get("home_volume") != "oc-box-home-" + ident
        or record is None
        or not record.ready
        or data.get("ready") is not True
        or record.volume != data.get("home_volume")
        or migration.RECEIPT not in data
        or migration.HOME_IDENTITY in data
        or migration.in_progress(data)
        or migration.needs_bootstrap(data)
        or data.get("restore_incomplete")
    ):
        raise box.BoxError(
            "Private-home repair requires a completed guarded conversion with an "
            "older receipt and no existing persistent home identity."
        )
    env = migration.engine_environment(box)
    migration.require_local_engine(box, env)

    def observe():
        home = []
        resources = _inspect(
            box, path, data, running=False, engine_env=env, home_review=home
        )
        if len(home) != 1 or resources[0] != record.container:
            raise box.BoxError(
                "The original converted container could not be verified."
            )
        users = box.podman(
            "--remote=false",
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "volume=" + record.volume,
            "--format",
            "{{.ID}}",
            capture=True,
            private=True,
            timeout=15,
            env=env,
        ).stdout.split()
        if users != [record.container]:
            raise box.BoxError(
                "The original container is not the only named-volume consumer in this store."
            )
        return resources, home[0]

    try:
        token, uncertain = workspace_control.disposal_observation(path)
        if uncertain:
            raise box.BoxError(
                "The previous container transition is uncertain; no repair was attempted."
            )
        with history_recovery.budget(20):
            expected = observe()
        identity, stable = expected[1]
        phrase = "REPAIR HOME " + ident
        if not uninstall_tty.confirm_review(
            f"Record persistent identity for the private home of workspace {ident}.\n"
            f"Stopped container: {record.container}\n"
            f"Private home: {record.volume}\n"
            f"Selected mountpoint: {json.dumps(expected[0][1][2])}\n"
            "The inode number matches, but the old receipt cannot prove past identity. "
            "Confirm only if you independently know this is the original private home, "
            "not a restored or replaced volume.\n"
            "No container will start, and no files or conversations will be moved.\n"
            f"To proceed, type {phrase}",
            phrase,
        ):
            raise box.BoxError("Private-home repair was not confirmed.")
        with workspace_control.admission(path), history_recovery.budget(20):
            if (
                not workspace_control.unchanged(path, token)
                or box.read_json(path) != data
                or load(box, path) != record
                or observe() != expected
            ):
                raise box.BoxError(
                    "The verified private home changed. No repair was recorded."
                )
            repaired = {
                **data,
                migration.RECEIPT: {**data[migration.RECEIPT], "volume": identity},
                migration.HOME_IDENTITY: stable,
            }
            with workspace_control.retirement_gate(path):
                if (
                    not workspace_control.unchanged(path, token)
                    or box.read_json(path) != data
                    or load(box, path) != record
                ):
                    raise box.BoxError(
                        "The workspace or stop intent changed. No repair was recorded."
                    )
                box.atomic_json(path, repaired, durable=True)
    except (OSError, workspace_control.ControlError):
        raise box.BoxError(
            "Could not verify or record private-home repair. No container was started."
        ) from None
    print(
        "Private-home identity recorded. The guarded version and conversations are unchanged."
    )
    return 0


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
    engine_env: dict | None = None

    def _engine_options(self):
        return {"env": self.engine_env} if self.engine_env is not None else {}

    def _unchanged(self, box):
        try:
            if not workspace_control.unchanged(self.path, self.stop_token):
                raise box.BoxError(
                    "Stop was requested during guarded preparation; no new native startup was admitted."
                )
        except workspace_control.ControlError as exc:
            raise box.BoxError(str(exc)) from None

    def require_live(self, box, data, *, running):
        if (
            _inspect(
                box,
                self.path,
                data,
                running=running,
                **(
                    {"engine_env": self.engine_env}
                    if self.engine_env is not None
                    else {}
                ),
            )
            != self.resources
        ):
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
                self._remember_home_identity(box, data)
                if entrypoint_migration.RETIRE in data:
                    entrypoint_migration.verify_retirement(
                        box,
                        self.path,
                        data,
                        self.stop_token,
                        self.record.container,
                        **self._engine_options(),
                    )
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

    def _remember_home_identity(self, box, data):
        migration = entrypoint_migration
        if (
            migration.RECEIPT not in data
            or migration.HOME_IDENTITY in data
            or migration.in_progress(data)
        ):
            return
        try:
            identity, stable = workspace_identity.snapshot(self.resources[1][2])
        except OSError:
            # A filesystem without persistent identity keeps exact old checks.
            return
        if identity != data[migration.RECEIPT]["volume"] or stable is None:
            return
        updated = {**data, migration.HOME_IDENTITY: stable}
        box.atomic_json(self.path, updated, durable=True)
        data.update(updated)

    def start(self, box, data):
        try:
            staged = entrypoint_migration.RETIRE in data
            saved = guarded_devices.paths(data)
            local = staged or bool(saved)
            prefix = ("--remote=false",) if local else ()
            engine_options = self._engine_options()
            mount_env = (
                self.engine_env
                if self.engine_env is not None
                else (entrypoint_migration.engine_environment(box))
            )
            with workspace_control.admission(self.path), history_recovery.budget(20):
                self._unchanged(box)
                self.require_live(box, data, running=False)
                if entrypoint_migration.RETIRE in data:
                    entrypoint_migration.verify_retirement(
                        box,
                        self.path,
                        data,
                        self.stop_token,
                        self.record.container,
                        **engine_options,
                    )
                box.check_resource_controllers(
                    data,
                    **(
                        {"engine_env": self.engine_env}
                        if self.engine_env is not None
                        else {}
                    ),
                )
                self._unchanged(box)
                workspace_control.mark_uncertain(self.path)
                box.podman(
                    *prefix,
                    "start",
                    self.record.container,
                    capture=True,
                    private=True,
                    timeout=15,
                    **engine_options,
                )
                info = box.inspect_owned(
                    {**data, "container": self.record.container},
                    **({"local_only": True} if local else {}),
                    **engine_options,
                )
                self.require_live(box, data, running=True)
                if (
                    entrypoint_migration.RETIRE in data
                    and not entrypoint_migration.running_replacement(info)
                ):
                    raise box.BoxError(
                        "Could not verify the staged replacement running; the original is retained."
                    )
                workspace_control.finish_transition(self.path)
                self._unchanged(box)
            # A restored base image may not have Python before bootstrap.
            # This passive check uses no forwarded credentials or native CLI.
            command = box.ContainerCommand(
                [
                    "podman",
                    *prefix,
                    "exec",
                    "--workdir",
                    "/",
                    "--user",
                    "0:0",
                    self.record.container,
                    "/bin/sh",
                    "-c",
                    'for directory; do test -d "$directory" && test -r "$directory" '
                    '&& test -w "$directory" && /bin/ls -A "$directory" >/dev/null '
                    "|| exit 1; done",
                    "oc-box",
                    box.container_project(data),
                    data["home"],
                ],
                mount_env,
            )
            command.guarded_spawn = True
            box.run(command, capture=True, private=True, timeout=30)
            if saved:
                # Check the passive runtime identity can see and access the
                # nodes, without opening them or asking the driver for I/O.
                device_check = box.ContainerCommand(
                    [
                        "podman",
                        *prefix,
                        "exec",
                        "--workdir",
                        "/",
                        "--user",
                        "0:0",
                        self.record.container,
                        "/bin/sh",
                        "-c",
                        'for node; do test -c "$node" && test -r "$node" '
                        '&& test -w "$node" || exit 1; done',
                        "oc-box",
                        *saved,
                    ],
                    mount_env,
                )
                device_check.guarded_spawn = True
                result = box.run(
                    device_check, capture=True, private=True, timeout=30, check=False
                )
                if result.returncode:
                    if staged:
                        raise box.BoxError(
                            f"{guarded_devices.DeviceError('access')} "
                            "This attempt did not retire the original."
                        )
                    startup_output.line(
                        "oc-box: Could not verify access to a saved device. "
                        "Continuing with the existing container; tools may not be able "
                        "to use that device.",
                        file=sys.stderr,
                        flush=True,
                    )
            if entrypoint_migration.RETIRE in data:
                with workspace_control.admission(self.path):
                    self._unchanged(box)
                    self.require_live(box, data, running=True)
                    entrypoint_migration.verify_retirement(
                        box,
                        self.path,
                        data,
                        self.stop_token,
                        self.record.container,
                        after_start=True,
                        **engine_options,
                    )
                    self._remember_home_identity(box, data)
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


def client_engine(box, path, data):
    """Select the local device-backed store before verifying a live client."""
    if not data.get("devices") or load(box, path) is None:
        return None
    guarded = current()
    if guarded is not None and guarded.path == path and guarded.engine_env is not None:
        return guarded.engine_env.copy()
    return entrypoint_migration.engine_environment(box)


def bind_client(box, path, data, connection, *, engine_env=None):
    if entrypoint_migration.RETIRE in data:
        raise box.BoxError(entrypoint_migration.RETIRE_PENDING)
    # These are host-private execution selectors, never trusted from the
    # socket's payload or the native readiness frame.
    connection.pop("_runtime_container", None)
    connection.pop("_runtime_digest", None)
    record = load(box, path)
    if record is None:
        return None
    if data.get("devices") and engine_env is None:
        engine_env = client_engine(box, path, data)
    if (
        not record.ready
        or record.version != connection.get("version")
        or record.volume != data.get("home_volume")
        or record.container
        != _inspect(
            box,
            path,
            data,
            running=True,
            attach_only=True,
            **({"engine_env": engine_env} if engine_env is not None else {}),
        )[0]
    ):
        raise box.BoxError(
            "The live lease does not match the guarded runtime and private resources."
        )
    connection["_runtime_container"] = record.container
    connection["_runtime_digest"] = record.digest
    return engine_env


@contextlib.contextmanager
def admit(
    box, path, data, args, previous, *, prepare_replacement=None, engine_env=None
):
    """Run before the vault/readiness path; the caller holds the workspace lock."""
    command = args.command
    requested = getattr(args, "convert_legacy_container", False)
    rebuild = getattr(args, "rebuild_legacy_container", False)
    pending = entrypoint_migration.FIELD in data
    retirement_env = None
    if entrypoint_migration.RETIRE in data:
        entrypoint_migration.retirement_target(box, data)
        if command != "start":
            raise box.BoxError(entrypoint_migration.RETIRE_PENDING)
        retirement_env = entrypoint_migration.engine_environment(box)
        entrypoint_migration.require_local_engine(box, retirement_env)
    if pending and (command != "start" or not (requested or rebuild)):
        raise box.BoxError(entrypoint_migration.PENDING)
    if entrypoint_migration.needs_bootstrap(data) and command != "start":
        raise box.BoxError(
            "The replacement OS needs baseline setup. Use owning 'oc-box start'."
        )
    if command not in ("start", "web", "sync", "purge"):
        raise box.BoxError(
            "This operation is unavailable on a runtime-guarded workspace. "
            "Use guarded start or sync with the admitted version, or back up the workspace."
        )
    if data.get("restore_incomplete"):
        raise box.BoxError(
            "This workspace has an incomplete restore. Preserve it and restore the "
            "original backup to a fresh destination before using the guarded runtime."
        )
    if data.get("ready") is not True:
        raise box.BoxError(
            "Guarded startup requires a workspace that completed setup. Inspect "
            "'oc-box status --table' and preserve the existing workspace before retrying."
        )
    if data.get("distro") == "alpine":
        raise box.BoxError(
            "Guarded OpenCode upgrades do not support Alpine. Back up this workspace "
            "and use a separate workspace on a supported distribution for the new runtime."
        )
    if getattr(args, "import_session", None):
        raise box.BoxError(
            "Guarded startup cannot import a host session as part of this launch. "
            "Retry without --import-session."
        )
    if getattr(args, "device", None) or getattr(args, "clear_devices", False):
        raise box.BoxError(
            "Guarded startup cannot change registered device mappings. Retry without "
            "--device or --clear-devices to keep the current mappings."
        )
    if getattr(args, "podman_arg", None) or getattr(args, "clear_podman_args", False):
        raise box.BoxError(
            "Guarded startup cannot change registered Podman options. Retry without "
            "--podman-arg or --clear-podman-args."
        )
    if data.get("podman_args"):
        raise box.BoxError(
            "Guarded startup cannot verify this workspace's saved custom Podman options. "
            "Inspect 'oc-box status --table'; preserve the existing workspace and use "
            "a separate standard workspace if those options are not needed."
        )
    try:
        saved = guarded_devices.paths(data)
        if saved:
            if command == "purge" or getattr(args, "persisted", False):
                raise box.BoxError(
                    "This guarded operation requires a workspace without device mappings. "
                    "Keep the mappings; ordinary guarded startup and live history sync are separate."
                )
            if retirement_env is None:
                retirement_env = entrypoint_migration.engine_environment(box)
    except guarded_devices.DeviceError as exc:
        raise box.BoxError(str(exc)) from None
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
            "Only plain history-preserving purge with the verified runtime is supported "
            "by this path. To discard a stopped workspace without starting it, use "
            "'oc-box purge --workspace WORKSPACE_ID --discard-guarded' and confirm."
        )
    source_id = None
    if pending:
        resources = None
    else:
        try:
            resources = _inspect(
                box,
                path,
                data,
                running=False,
                **(
                    {"engine_env": retirement_env}
                    if retirement_env is not None
                    else {"engine_env": engine_env}
                    if engine_env is not None
                    else {}
                ),
            )
        except box.BoxError as exc:
            if command != "start" or not getattr(exc, "legacy_entrypoint", False):
                raise
            if previous is not None:
                raise box.BoxError(
                    "This legacy container is already bound to a verified runtime floor. "
                    "In-place replacement is unavailable; preserve it with a full backup."
                ) from None
            source_id = exc.legacy_entrypoint
            resources = None
    converting = pending or source_id is not None
    if converting and (
        previous is not None
        or any(
            getattr(args, option, None)
            for option in (
                "distro",
                "cpus",
                "memory",
                "pids_limit",
                "clear_devices",
                "clear_podman_args",
            )
        )
    ):
        raise box.BoxError(
            "Legacy container replacement cannot change resource settings or rebind an enrolled runtime."
        )
    if (requested or rebuild) and not converting:
        if entrypoint_migration.RETIRE in data:
            raise box.BoxError(
                "The replacement is already staged; use owning 'oc-box start'. "
                "Do not bypass any uncertainty refusal."
            )
        raise box.BoxError("No eligible legacy container entrypoint to replace.")
    if rebuild and not pending:
        raise box.BoxError(
            "Container OS rebuild is available only after a verified snapshot failure."
        )
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
                + (
                    " To discard without startup, use 'oc-box purge --workspace "
                    "WORKSPACE_ID --discard-guarded' and confirm."
                    if command == "purge"
                    else ""
                )
            ) from None
        raise box.BoxError(str(exc)) from None
    if converting:
        retirement_env = entrypoint_migration.engine_environment(box)
        if saved and "CONTAINERS_CONF_OVERRIDE" in retirement_env:
            raise box.BoxError(str(guarded_devices.DeviceError("override")))
        entrypoint_migration.consent(box, args, data)
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
        recheck = getattr(args, "runtime_guard", False)
        if not transition and not (recheck and not previous.ready):
            digest = previous.digest
        elif (
            previous is None
            or recheck
            or entrypoint_migration.RETIRE in data
            or entrypoint_migration.needs_bootstrap(data)
        ):
            # Enrollment and migration keep the isolated proof. Advancing an
            # existing floor does not depend on the disposable SDK installer.
            digest = _candidate_digest(box, path, host, version, recheck=recheck)
            if not transition and digest != previous.digest:
                raise box.BoxError(
                    "The host OpenCode is not the exact recorded native artifact; guarded startup refused."
                )
        else:
            # Do not let a repackaged binary strand the workspace at an
            # uninstalled digest. Confirm the official bytes first, then
            # enforce them as usual on the installed runtime and native helpers.
            digest = _official_digest(box, version, host.digest)
            startup_output.line(
                f"oc-box: OpenCode {version} has not been checked in isolation for "
                "this upgrade. Continuing with runtime and workspace verification.",
                file=sys.stderr,
                flush=True,
            )
        if digest != host.digest:
            raise box.BoxError(
                "The host OpenCode is not the exact verified native artifact; guarded startup refused."
            )
        if converting:
            if prepare_replacement is None:
                raise box.BoxError(
                    "Legacy container replacement requires owning credential validation."
                )
            # Keep the isolated preflight ahead of unlock, but never retire
            # the original OS before the owning launch can unlock its vault.
            args._opencode_release = version
            prepare_replacement()
            host.command(box)
            entrypoint_migration.convert(
                box, path, data, token, source_id, rebuild=rebuild, env=retirement_env
            )
            resources = _inspect(
                box, path, data, running=False, engine_env=retirement_env
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
            path,
            host,
            previous,
            record,
            resources,
            token,
            not record.ready,
            engine_env=retirement_env,
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
            "Live container OpenCode did not match the admitted artifact. "
            "The guarded version remains reserved; no older binary was started."
        )


def install_container(box, data):
    """Resolve the package without ever running npm's native postinstall."""
    scope = current()
    scope._unchanged(box)
    command = box.ContainerCommand(
        [
            "podman",
            *(("--remote=false",) if scope.engine_env is not None else ()),
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
        scope.engine_env.copy()
        if scope.engine_env is not None
        else box.maintenance_host_env(),
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
