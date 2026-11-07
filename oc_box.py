#!/usr/bin/env python3
"""Rootless project environments for an unmodified OpenCode installation."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

if __package__:
    from . import credentials, dependencies, opencode_compat, vault, workspace_identity
else:
    import credentials
    import dependencies
    import opencode_compat
    import vault
    import workspace_identity

APP = Path(__file__).resolve().parent
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


@contextlib.contextmanager
def loading_stage(label, *, interval=5.0, stream=None):
    stream = sys.stderr if stream is None else stream
    started = time.monotonic()
    stopped = threading.Event()
    print(f"oc-box: {label}...", file=stream, flush=True)

    def heartbeat():
        while not stopped.wait(interval):
            print(
                f"oc-box: {label}... ({int(time.monotonic() - started)}s elapsed)",
                file=stream,
                flush=True,
            )

    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        yield
    except BaseException:
        stopped.set()
        worker.join()
        print(
            f"oc-box: {label} failed after {time.monotonic() - started:.1f}s",
            file=stream,
            flush=True,
        )
        raise
    else:
        stopped.set()
        worker.join()
        print(
            f"oc-box: {label} done ({time.monotonic() - started:.1f}s)",
            file=stream,
            flush=True,
        )


def run_tui(command):
    # Lazy import also keeps noninteractive CLI/library consumers lightweight.
    if __package__:
        from .terminal_ui import run_tui as relay
    else:
        from terminal_ui import run_tui as relay

    return relay(command, env=getattr(command, "environment", None))


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
        if not hasattr(os, "memfd_create"):
            raise BoxError(
                "Vault launches require Linux memfd support for private subprocess-output capture."
            )
        fd = os.memfd_create("oc-box-private-output", os.MFD_CLOEXEC)
        with os.fdopen(fd, "w+b") as stream:
            yield stream
    else:
        with tempfile.TemporaryFile() as stream:
            yield stream


def run(argv, *, capture=False, check=True, private=False, **kwargs):
    if getattr(argv, "environment", None) is not None:
        kwargs.setdefault("env", argv.environment)
    if kwargs.get("cwd"):
        kwargs["env"] = {
            **kwargs.get("env", os.environ),
            "PWD": str(Path(kwargs["cwd"]).resolve()),
        }
    try:
        command = [str(x) for x in argv]
        if capture:
            # Mainline OpenCode can truncate large exports when stdout is a
            # pipe. Use regular files for captured CLI output instead.
            with (
                captured_output(private=private) as output,
                captured_output(private=private) as errors,
            ):
                result = subprocess.run(
                    command, text=True, stdout=output, stderr=errors, **kwargs
                )
                output.seek(0)
                errors.seek(0)
                result.stdout = output.read().decode("utf-8")
                result.stderr = errors.read().decode("utf-8", errors="replace")
        else:
            result = subprocess.run(command, text=True, **kwargs)
    except FileNotFoundError as exc:
        raise BoxError(f"Executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BoxError(
            f"{argv[0]} exceeded its {exc.timeout}s timeout; operation stopped."
        ) from exc
    if check and result.returncode:
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
            print(
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
    print(
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
            *args, **kwargs, **({"env": environment} if environment is not None else {})
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
    print(f"Creating {data['distro']} environment ({data['image']})", flush=True)
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
            print(
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
            podman(
                "exec",
                data["container"],
                "/bin/sh",
                "-c",
                DISTROS[data["distro"]][1],
                timeout=1800,
            )
            podman(
                "exec",
                "-i",
                data["container"],
                "python3",
                "-",
                input=(APP / "bootstrap.py").read_text(),
                timeout=600,
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
    return env


def container_build_config(release="1.0.0"):
    """Override only Build permissions in the container's final config scope."""
    if opencode_compat.major(release) == 2:
        return {
            "$schema": "https://opencode.ai/config.json",
            "agents": {
                "build": {
                    "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]
                }
            },
        }
    return {
        "$schema": "https://opencode.ai/config.json",
        "agent": {"build": {"permission": "allow"}},
    }


def container_build_plugin(release="1.0.0"):
    # Config sources deep-merge permission objects, so a later wildcard alone
    # can leave inherited tool-specific denials after it. Normalize Build only
    # after mainline has finished loading project configs and agent files.
    if opencode_compat.major(release) == 2:
        return """export default {
  id: "oc-box.build-permissions",
  async setup(ctx) {
    await ctx.agent.transform(editor => {
      editor.update("build", agent => {
        agent.permissions = [{ action: "*", resource: "*", effect: "allow" }]
      })
    })
    await ctx.permission.hook("evaluate", event => {
      if (event.agent === "build") event.effect = "allow"
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
    for key, value in os.environ.items():
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
    if settings.is_file():
        extra = read_json(settings).get("pass_env", [])
        for key in extra:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key):
                raise BoxError("Invalid pass_env entry in oc-box/config.json")
            if key in os.environ:
                forwarded[key] = os.environ[key]
    env.update(credentials.forwarded(data, forwarded))
    # Apply after project/agent files, without lifting Plan or subagent rules.
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        container_build_config(data.get("opencode_version", "1.0.0"))
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
            with loading_stage("Starting existing container for maintenance"):
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
            with loading_stage("Stopping maintenance container"):
                stop_container(data, env=environment)


def exec_command(data, command, *, interactive=False):
    forwarded = container_env(data)
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
            opencode_compat.command(
                data.get("opencode_version", "1.0.0"), command[0], command[1:]
            )
            if "--version" not in command
            else command
        )
    if opencode_compat.major(data.get("opencode_version", "1.0.0")) == 2:
        # Clear image/create-time environment first. Public variable NAMES are
        # argv; values arrive only via Podman's inherited --env NAME mechanism.
        bridge = "import os,sys; keys=sys.argv[1].split(','); env={k:os.environ[k] for k in keys if k in os.environ}; os.execvpe(sys.argv[2],sys.argv[2:],env)"
        command = ["python3", "-c", bridge, ",".join([*forwarded, "PATH"]), *command]
    result.extend([data["container"], *command])
    return result


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


def config_archive(data):
    """Snapshot user config as bytes. Extracted only inside the container."""
    source = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "opencode"
    )
    release = data.get("opencode_version", "1.0.0")
    v2 = opencode_compat.major(release) == 2
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
                or not file.is_file()
                or relative == Path("AGENTS.md")
                or (file.name.startswith("service") and file.suffix == ".json")
            ):
                continue
            content = file.read_bytes()
            if len(relative.parts) == 1 and file.name in (
                "config.json",
                "opencode.json",
                "opencode.jsonc",
            ):
                config = rewrite_config(parse_jsonc(content.decode()))
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
    print(f"oc-box: OpenCode {version} matches the host version.", flush=True)


def prepare_runtime(path, data, version):
    # Install the unmodified upstream version currently installed on the host.
    # Node/npm/uv are also used by the user's ordinary MCP/LSP setup.
    ensure_opencode_version(data, version)
    v2 = opencode_compat.major(version) == 2
    if v2:
        # Seed the V2-only database from a consistent SQLite snapshot once.
        # Mainline performs its own supported migration; V1 bytes stay intact.
        bridge = (APP / "opencode_v2_io.py").read_text()
        run(
            exec_command(data, ["python3", "-c", bridge, "seed", CONTAINER_OPENCODE]),
            capture=True,
            timeout=120,
        )
    archive = config_archive(data)
    unpack = (APP / "sync_config.py").read_text()
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
            "Cannot copy OpenCode config into container: "
            + credentials.redact(proc.stderr.decode(errors="replace"))
        )
    if proc.stdout:
        print("oc-box: " + proc.stdout.decode(errors="replace").strip(), flush=True)
    reused = (
        proc.stdout.decode(errors="replace").strip()
        == "Reusing container plugin dependencies."
    )
    if v2 and not reused:
        # V2 can supply its SDK to local plugins, but unlike V1 it does not
        # automatically install a copied config directory's custom manifest.
        with loading_stage("Installing OpenCode 2.x plugin dependencies"):
            run(
                exec_command(
                    data,
                    [
                        "npm",
                        "install",
                        "--prefix",
                        data["home"] + "/.config/opencode",
                        "--no-audit",
                        "--no-fund",
                    ],
                ),
                timeout=600,
            )
    auth = (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / "opencode/auth.json"
    )
    auth_content = os.environ.get("OPENCODE_AUTH_CONTENT") or (
        auth.read_text() if auth.is_file() else None
    )
    if auth_content and not v2:
        json.loads(auth_content)
        target = "import os,pathlib,sys; p=pathlib.Path(os.environ['XDG_DATA_HOME'])/'opencode/auth.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.stdin.read()); p.chmod(0o600)"
        run(
            exec_command(data, ["python3", "-c", target]),
            input=auth_content,
            capture=True,
            timeout=30,
        )
    mcp_auth = auth.with_name("mcp-auth.json")
    if mcp_auth.is_file() and not v2:
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
        if os.environ.get("OPENCODE_AUTH_CONTENT"):
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
            "Resolving OpenCode plugin dependencies (first run or changed configuration)"
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
        with loading_stage("Preparing OpenCode 2.x private service and plugins"):
            initialized = run(
                exec_command(
                    data, ["python3", "-c", bridge, "plugins", CONTAINER_OPENCODE]
                ),
                capture=True,
                private=True,
                timeout=240,
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


def refresh_sessions(path, data):
    result = run(
        exec_command(
            data, [CONTAINER_OPENCODE, "--pure", "session", "list", "--format", "json"]
        ),
        capture=True,
        timeout=60,
    )
    values = json.loads(result.stdout or "[]")
    roots = [
        item
        for item in values
        if item.get("directory") == container_project(data)
        and valid_session_id(item.get("id"))
    ]
    roots.sort(key=lambda item: item.get("updated", 0), reverse=True)
    state = session_state(path)
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
    print(
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
                json.dumps(
                    opencode_compat.command(
                        data.get("opencode_version", "1.0.0"),
                        CONTAINER_OPENCODE,
                        ["--pure", "export", ident],
                    )[1:]
                ),
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
    listed = run(
        exec_command(
            data, [CONTAINER_OPENCODE, "--pure", "session", "list", "--format", "json"]
        ),
        capture=True,
        timeout=60,
    )
    existing = any(
        item.get("id") == ident for item in json.loads(listed.stdout or "[]")
    )
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
        print(
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
        print(
            f"oc-box: Verified {omitted} obsolete nonpending tool raw buffer(s) omitted by mainline import.",
            flush=True,
        )
    print(
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
                    opencode_compat.command(
                        data.get("opencode_version", "1.0.0"),
                        CONTAINER_OPENCODE,
                        ["--pure", "import", "OC_BOX_IMPORT_FILE"],
                    )[1:]
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


def sync_sessions(path, data):
    state = refresh_sessions(path, data)
    archives = path.parent / "history"
    archives.mkdir(exist_ok=True, mode=0o700)
    for ident in state.get("sessions", []):
        if not valid_session_id(ident):
            raise BoxError("Invalid stored session ID")
        archived = archive_session(export_container_session(data, ident), data)
        target = archives / (archived["info"]["id"] + ".json")
        atomic_json(target, archived)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        state.setdefault("synced", {})
        if state["synced"].get(ident) == digest:
            continue
        if ident in state.get("host_sessions", {}):
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
        state["synced"][ident] = digest
        state.setdefault("host_sessions", {})[ident] = archived["info"]["id"]
        atomic_json(session_file(path), state)
    if state.get("sessions"):
        print(
            "Session history synced to your normal host OpenCode database.", flush=True
        )


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


def launch(path, data, args):
    # Keep plaintext values out of persisted workspace metadata, os.environ,
    # and informational/maintenance/sync invocations. Launch unlock is local.
    try:
        if args.command == "sync":
            return launch_environment(path, data, args)
        with credentials.unlocked(data):
            return launch_environment(path, data, args)
    except vault.VaultError as exc:
        raise BoxError(str(exc)) from exc


def launch_environment(path, data, args):
    with loading_stage("Checking OpenCode version"):
        version = detect_opencode_release()
    if data.get("opencode_version") != version:
        data["opencode_version"] = version
        atomic_json(path, data)
    exported = None
    if args.import_session:
        with loading_stage("Exporting host session"):
            exported = export_host_session(path, data, args.import_session)
    code = 1
    try:
        with loading_stage(f"Starting {data['distro']} container"):
            start_container(path, data)
        with loading_stage("Preparing OpenCode and syncing configuration"):
            prepare_runtime(path, data, version)
        if args.command == "sync":
            with loading_stage("Syncing session history"):
                sync_sessions(path, data)
            return 0
        if exported is not None:
            with loading_stage("Importing and verifying host session"):
                sessions = import_host_session(path, data, exported)
        else:
            with loading_stage("Finding workspace session"):
                sessions = refresh_sessions(path, data)
        selected_session = sessions.get("last") if not args.new else None
        if opencode_compat.major(version) == 2:
            selected_model = opencode_compat.model(args.model) if args.model else None
            if not selected_session and (args.new or selected_model):
                payload = {"location": {"directory": container_project(data)}}
                if selected_model:
                    payload["model"] = selected_model
                created = run(
                    exec_command(
                        data,
                        [
                            CONTAINER_OPENCODE,
                            "api",
                            "post",
                            "/api/session",
                            "--data",
                            json.dumps(payload),
                        ],
                    ),
                    capture=True,
                    timeout=60,
                )
                selected_session = json.loads(created.stdout)["data"]["id"]
                if not valid_session_id(selected_session):
                    raise BoxError("Invalid OpenCode 2.x session creation response.")
            elif selected_session and selected_model:
                run(
                    exec_command(
                        data,
                        [
                            CONTAINER_OPENCODE,
                            "api",
                            "post",
                            "/api/session/" + selected_session + "/model",
                            "--data",
                            json.dumps({"model": selected_model}),
                        ],
                    ),
                    capture=True,
                    timeout=60,
                )
        command = opencode_compat.tui(
            version,
            CONTAINER_OPENCODE,
            container_project(data),
            selected_session,
            args.model,
        )
        print(
            f"oc-box: {data['distro']} | {data['project']} | {'new session' if args.new or not sessions.get('last') else 'resuming ' + sessions['last']}",
            flush=True,
        )
        print(
            "oc-box: Opening OpenCode; plugins and MCP servers may need time to initialize.",
            flush=True,
        )
        code = run_tui(exec_command(data, command, interactive=True))
        try:
            with loading_stage("Syncing session history"):
                sync_sessions(path, data)
        except (BoxError, ValueError, KeyError) as exc:
            print(
                f"oc-box: history sync pending: {exc}\nContainer data retained. Retry with 'oc-box sync'.",
                file=sys.stderr,
            )
            return code or 1
        return code
    finally:
        with loading_stage("Stopping container"):
            stop_container(data)


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
    result = argparse.ArgumentParser(
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
            "doctor",
            "completion",
        ),
        default="start",
    )
    result.add_argument(
        "--project",
        type=Path,
        help="project directory (default: current directory; --status defaults to all workspaces)",
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
    result.add_argument(
        "--json",
        action="store_true",
        help="emit status or doctor as JSON",
    )
    result.add_argument(
        "--check",
        action="store_true",
        help="with upgrade, check GitHub for a changed version without installing",
    )
    result.add_argument(
        "--auto-update",
        choices=("on", "off"),
        help="with upgrade, enable/disable automatic GitHub updates before normal launches",
    )
    result.add_argument(
        "--workspace",
        help="workspace ID, also works after the project directory was deleted",
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
        help="explicitly resume the recorded conversation",
    )
    group.add_argument(
        "--import-session",
        metavar="SESSION_ID",
        help="copy a host conversation from this project into the container and resume it",
    )
    result.add_argument("--model", help="OpenCode provider/model")
    result.add_argument(
        "--credential-mode",
        choices=("env", "selected"),
        help="with credentials, select legacy env forwarding or explicit per-workspace credentials",
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
    result.add_argument("--cpus", help="CPU limit (default: 4)")
    result.add_argument("--memory", help="memory limit (default: 8g)")
    result.add_argument("--pids-limit", type=int, help="process limit (default: 2048)")
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
        help="clear saved device mappings during reset",
    )
    result.add_argument(
        "--clear-podman-args",
        action="store_true",
        help="clear saved additional Podman options during reset",
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
    return result


def report_status(args):
    if __package__:
        from .status_report import collect_reports, print_report
    else:
        from status_report import collect_reports, print_report

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
    else:
        print_report(report)
    return 0


def secrets_parser():
    parser = argparse.ArgumentParser(
        prog="oc-box secrets",
        description="Manage the optional host encrypted vault; values/passphrases use hidden controlling-terminal prompts, never argv/env/stdin.",
    )
    parser.add_argument("action", choices=("init", "set", "list", "remove", "passwd"))
    parser.add_argument(
        "name", nargs="?", help="credential environment name for set/remove"
    )
    parser.add_argument(
        "--min-passphrase-length",
        type=int,
        help="minimum characters for a new vault passphrase, 8..1024; init/passwd only; overrides host config",
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
    print(
        "No secret values were read. Changes apply to the next normal launch, not terminal; copied config/auth and existing container secrets are independent."
    )
    return 0


def completion_parser():
    result = argparse.ArgumentParser(
        prog="oc-box completion",
        description="Print shell completion for local activation or installation.",
    )
    result.add_argument("shell", choices=("bash",))
    return result


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
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
    args = parser().parse_args(argv)
    if args.command == "completion":
        raise BoxError("Use 'oc-box completion bash' with no workspace options.")
    if args.command == "doctor":
        raise BoxError(
            "Use 'oc-box doctor' first, followed by --json/--feature; see its --help."
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
    if (args.check or args.auto_update is not None) and args.command != "upgrade":
        raise BoxError("--check and --auto-update apply only to upgrade.")
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
            raise BoxError("Upgrade accepts only --check or --auto-update on/off.")
        if args.check and args.auto_update is not None:
            raise BoxError("--check and --auto-update are mutually exclusive.")
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

        if package_managed(APP):
            if args.auto_update == "on":
                raise BoxError(
                    package_upgrade_message()
                    + " GitHub automatic updates cannot manage a Python-package installation."
                )
            print(package_upgrade_message())
            return 0
        if os.environ.get("OC_BOX_CONTAINER") == "1":
            raise BoxError(
                "Run 'oc-box upgrade' on the host; the container cannot update its host launcher."
            )
        try:
            if args.auto_update is not None:
                if os.geteuid() == 0:
                    raise BoxError(
                        "Run oc-box upgrade as your regular host user, without sudo."
                    )
                update_settings(args.auto_update == "on")
                return 0
            upgrade(APP, check=args.check)
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
                "Status accepts only --project, --workspace, and --json selectors."
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
            f"{args.command.capitalize()} accepts only --project or --workspace selectors."
        )
    if args.command == "terminal":
        if args.all or args.delete_sessions or args.yes:
            raise BoxError("Terminal accepts only --project or --workspace selectors.")
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
    if args.command == "start" and os.environ.get("OC_BOX_CONTAINER") != "1":
        if __package__:
            from .updater import UpdateError, automatic_upgrade
        else:
            from updater import UpdateError, automatic_upgrade

        try:
            release = automatic_upgrade(APP)
        except (UpdateError, OSError, ValueError) as exc:
            print(
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
    with loading_stage("Checking rootless Podman"):
        check_backend(
            **({"env": maintenance_host_env()} if args.command == "terminal" else {}),
            **(
                {"feature": "launch"}
                if args.command in ("start", "reset", "sync")
                else {}
            ),
        )
    state_root().mkdir(parents=True, exist_ok=True, mode=0o700)
    path, data = find_workspace(
        args.project, args.workspace, allow_identity_change=args.command == "repair"
    )
    if data is None:
        if args.command != "start":
            raise BoxError(
                "No oc-box workspace registered here. Use 'oc-box list' or '--workspace ID'."
            )
        if args.resume:
            raise BoxError("No workspace/session exists here to resume.")
        project = validate_project(args.project)
        path, data = create_workspace(
            project, distro_name(args.distro or host_default())
        )
    if data.get("version") != 2:
        raise BoxError(
            "This workspace uses the retired experimental tool-routing design. Purge it with the older launcher before creating a new environment."
        )
    # Explicit stop is usable from a second terminal. Tool calls
    # never auto-restart a stopped container; the running TUI can then be exited.
    if args.command == "stop":
        stop_container(data)
        print("Container stopped; files, packages, and conversations retained.")
        return 0
    with workspace_lock(path), contextlib.ExitStack() as launch_context:
        if data.get("restore_incomplete") and args.command != "purge":
            raise BoxError(
                "This workspace restore did not finish. Do not start it; inspect backup recovery staging and use plain purge by ID before retrying into a new destination."
            )
        if args.command == "reset":
            # Validate/unlock before reset removes the old OS. Wrong/missing
            # credentials must never destroy an otherwise working environment.
            try:
                launch_context.enter_context(credentials.unlocked(data))
            except vault.VaultError as exc:
                raise BoxError(str(exc)) from exc
            # Validate CLI compatibility before deleting a working container.
            detect_opencode_release()
        if args.command in ("start", "reset", "sync", "terminal"):
            refresh_workspace_identity(path, data)
        if args.command == "terminal":
            return maintenance_terminal(path, data)
        if args.command == "repair":
            repair_workspace(path, data)
            return 0
        if args.command in ("start", "reset", "sync"):
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
            return launch(path, data, args)
        if args.command == "purge":
            purge(path, data, args)
    return 0


def entrypoint():
    try:
        return main()
    except (BoxError, vault.VaultError, OSError, ValueError) as exc:
        print(f"oc-box: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(entrypoint())
