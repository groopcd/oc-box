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
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

APP = Path(__file__).resolve().parent
LABEL = "io.oc-box.workspace"
MARKER = ".oc-box-id"
CONTAINER_OPENCODE = "/opt/oc-box/runtime/bin/opencode"
DISTROS = {
    "tumbleweed": ("registry.opensuse.org/opensuse/tumbleweed:latest", "zypper -n refresh && zypper -n install bash ca-certificates curl git python3 ripgrep tar gzip nodejs npm poppler-tools"),
    "leap": ("registry.opensuse.org/opensuse/leap:latest", "zypper -n refresh && zypper -n install bash ca-certificates curl git python3 ripgrep tar gzip nodejs npm poppler-tools"),
    "ubuntu": ("docker.io/library/ubuntu:24.04", "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash ca-certificates curl git python3 ripgrep tar gzip nodejs npm poppler-utils"),
    "debian": ("docker.io/library/debian:stable", "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends bash ca-certificates curl git python3 ripgrep tar gzip nodejs npm poppler-utils"),
    "fedora": ("registry.fedoraproject.org/fedora:latest", "dnf install -y bash ca-certificates curl git python3 ripgrep tar gzip nodejs npm poppler-utils"),
    "arch": ("docker.io/library/archlinux:base", "pacman -Syu --noconfirm --needed bash ca-certificates curl git python ripgrep tar gzip nodejs npm poppler"),
    "alpine": ("docker.io/library/alpine:latest", "apk add --no-cache bash ca-certificates curl git python3 ripgrep tar gzip nodejs npm poppler-utils libstdc++ libgcc"),
}
ALIASES = {"opensuse": "tumbleweed", "opensuse-tumbleweed": "tumbleweed", "opensuse-leap": "leap", "archlinux": "arch"}


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
            print(f"oc-box: {label}... ({int(time.monotonic() - started)}s elapsed)", file=stream, flush=True)

    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        yield
    except BaseException:
        stopped.set()
        worker.join()
        print(f"oc-box: {label} failed after {time.monotonic() - started:.1f}s", file=stream, flush=True)
        raise
    else:
        stopped.set()
        worker.join()
        print(f"oc-box: {label} done ({time.monotonic() - started:.1f}s)", file=stream, flush=True)


def run_tui(command):
    # Lazy import also keeps noninteractive CLI/library consumers lightweight.
    from terminal_ui import run_tui as relay
    return relay(command)


def state_root():
    return Path(os.environ.get("OC_BOX_STATE_HOME", str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "oc-box"))).expanduser().resolve()


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
        values = dict(line.split("=", 1) for line in os_release.read_text().splitlines() if "=" in line)
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
    return ALIASES.get(distro, distro) if ALIASES.get(distro, distro) in DISTROS else "tumbleweed"


def distro_name(name):
    value = ALIASES.get(name, name)
    if value not in DISTROS:
        raise BoxError(f"Unknown distro {name!r}. Run 'oc-box distros' for supported choices.")
    return value


def run(argv, *, capture=False, check=True, **kwargs):
    if kwargs.get("cwd"):
        kwargs["env"] = {**kwargs.get("env", os.environ), "PWD": str(Path(kwargs["cwd"]).resolve())}
    try:
        command = [str(x) for x in argv]
        if capture:
            # Mainline OpenCode can truncate large exports when stdout is a
            # pipe. Use regular files for captured CLI output instead.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                result = subprocess.run(command, text=True, stdout=output, stderr=errors, **kwargs)
                output.seek(0)
                errors.seek(0)
                result.stdout = output.read().decode("utf-8")
                result.stderr = errors.read().decode("utf-8", errors="replace")
        else:
            result = subprocess.run(command, text=True, **kwargs)
    except FileNotFoundError as exc:
        raise BoxError(f"Executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BoxError(f"{argv[0]} exceeded its {exc.timeout}s timeout; operation stopped.") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise BoxError(f"{argv[0]} failed (exit {result.returncode})" + (f": {detail}" if detail else ""))
    return result


def podman(*args, **kwargs):
    return run(["podman", *args], **kwargs)


def check_backend():
    if os.geteuid() == 0:
        raise BoxError("Run oc-box as your regular user, without sudo (rootless Podman is required).")
    result = podman("info", "--format", "{{.Host.Security.Rootless}}", capture=True)
    if result.stdout.strip() != "true":
        raise BoxError("Podman is not operating rootlessly.")


def check_resource_controllers(data, *, cgroup_root=Path("/sys/fs/cgroup"), uid=None):
    """Check the user manager, not kernel-global controller availability.

    On openSUSE the kernel supports memory/cpu but a vendor drop-in clears
    systemd delegation. runc then reports a misleading missing executable when
    it cannot open memory.swap.max in the newly created container cgroup.
    """
    uid = os.getuid() if uid is None else uid
    service = cgroup_root / f"user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers"
    try:
        available = set(service.read_text().split())
    except FileNotFoundError:
        # Non-systemd layouts: ask Podman about its effective controllers.
        result = podman("info", "--format", "{{json .Host.CgroupControllers}}", capture=True)
        controllers = json.loads(result.stdout)
        if not isinstance(controllers, list) or not all(isinstance(item, str) for item in controllers):
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
            "Rootless Podman is missing delegated cgroup controllers: " + ", ".join(sorted(missing))
            + ". Available: " + (", ".join(sorted(available)) or "none") + ".\n"
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


def validate_project(path):
    project = Path(path).expanduser().resolve(strict=True)
    protected = {Path("/"), Path("/home"), Path.home().resolve(), state_root()}
    if not project.is_dir() or project in protected:
        raise BoxError("Choose a dedicated project directory, not /, /home, your home, or oc-box state.")
    for private in (state_root(), APP, Path.home() / ".config/opencode", Path.home() / ".local/share/opencode"):
        if private.resolve().is_relative_to(project):
            raise BoxError(f"Project would expose trusted application/state files: {private}")
    # Podman volume syntax uses ':' and ',' as separators.
    if any(char in str(project) for char in (":", ",", "\n", "\r")):
        raise BoxError("Project path must not contain colons, commas, or line breaks.")
    return project


def find_workspace(project=None, workspace_id=None):
    if workspace_id:
        if not re.fullmatch(r"[0-9a-f]{32}", workspace_id):
            raise BoxError("Invalid workspace ID.")
        path = state_root() / "workspaces" / workspace_id / "workspace.json"
        if not path.exists():
            raise BoxError(f"Workspace not found: {workspace_id}")
        return path, read_json(path)
    project = Path(project or Path.cwd()).resolve()
    marker = project / MARKER
    if not marker.is_file() or marker.is_symlink():
        return None, None
    ident = marker.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{32}", ident):
        raise BoxError(f"Invalid workspace marker: {marker}")
    path = state_root() / "workspaces" / ident / "workspace.json"
    if not path.exists():
        raise BoxError("Workspace marker has no matching host registry. Remove the stale .oc-box-id to create a new workspace.")
    data = read_json(path)
    if data["project"] != str(project):
        raise BoxError("Workspace marker belongs to another directory; copied markers cannot authorize mounts.")
    if data["project_identity"] != [project.stat().st_dev, project.stat().st_ino]:
        raise BoxError("This directory replaced the original workspace. Remove .oc-box-id to start fresh.")
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
        "project_identity": [project.stat().st_dev, project.stat().st_ino],
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
            raise BoxError("This workspace already has an active oc-box process. Exit it before changing its environment.") from exc
        yield


def inspect_owned(data):
    result = podman("container", "inspect", data["container"], capture=True, check=False)
    if result.returncode:
        exists = podman("container", "exists", data["container"], capture=True, check=False)
        if exists.returncode == 1:
            return None
        raise BoxError(f"Cannot inspect container: {result.stderr.strip()}")
    info = json.loads(result.stdout)[0]
    if info.get("Config", {}).get("Labels", {}).get(LABEL) != data["id"]:
        raise BoxError("Container ownership label mismatch; refusing to manage it.")
    return info


def create_container(path, data):
    project = validate_project(data["project"])
    if [project.stat().st_dev, project.stat().st_ino] != data["project_identity"]:
        raise BoxError("Project directory identity changed; refusing to mount a replacement.")
    print(f"Creating {data['distro']} environment ({data['image']})", flush=True)
    # The named home is container-only storage, not a bind of the user's home.
    volume = podman("volume", "exists", data["home_volume"], capture=True, check=False)
    if volume.returncode == 1:
        podman("volume", "create", "--label", f"{LABEL}={data['id']}", data["home_volume"], capture=True)
    elif volume.returncode != 0:
        raise BoxError("Cannot check the private home volume.")
    volume_info = json.loads(podman("volume", "inspect", data["home_volume"], capture=True).stdout)[0]
    if (volume_info.get("Labels") or {}).get(LABEL) != data["id"]:
        raise BoxError("Private home volume ownership mismatch.")
    podman(
        "create", "--name", data["container"], "--label", f"{LABEL}={data['id']}",
        "--label", "io.oc-box.managed=true", "--pull=missing", "--init",
        "--user", "0:0", "--network=pasta:--no-map-gw,--map-host-loopback,169.254.1.2",
        "--add-host", "host.containers.internal:169.254.1.2",
        "--cpus", data["cpus"], "--memory", data["memory"],
        "--pids-limit", str(data["pids"]), "--workdir", str(project),
        "--volume", f"{data['home_volume']}:{data['home']}:rw",
        "--volume", f"{project}:{project}:rw,rprivate,nosuid,nodev",
        "--env", f"HOME={data['home']}",
        "--env", "PATH=/opt/oc-box/runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--entrypoint", "/bin/sh", data["image"], "-c", "exec sleep infinity",
    )
    data["ready"] = False
    atomic_json(path, data)


def start_container(path, data):
    project = validate_project(data["project"])
    if [project.stat().st_dev, project.stat().st_ino] != data["project_identity"]:
        raise BoxError("Project directory identity changed; refusing to reuse the environment.")
    check_resource_controllers(data)
    info = inspect_owned(data)
    if info is None:
        if data.get("ready"):
            print("Previous container is missing; recreating its OS environment.", flush=True)
        create_container(path, data)
        info = inspect_owned(data)
    mounts = info.get("Mounts", [])
    if not any(m.get("Destination") == data["project"] and m.get("Source") == data["project"] for m in mounts):
        raise BoxError("Container workspace mount does not match the host registry.")
    if not info["State"]["Running"]:
        podman("start", data["container"], capture=True)
    if not data.get("ready"):
        try:
            podman("exec", data["container"], "/bin/sh", "-c", DISTROS[data["distro"]][1], timeout=1800)
            podman("exec", "-i", data["container"], "python3", "-", input=(APP / "bootstrap.py").read_text(), timeout=600)
        except BaseException:
            podman("stop", "--time", "5", data["container"], capture=True, check=False)
            raise
        data["ready"] = True
        atomic_json(path, data)


def stop_container(data):
    info = inspect_owned(data)
    if info and info["State"]["Running"]:
        podman("stop", "--time", "10", data["container"], capture=True)


def session_file(path):
    return path.with_name("sessions.json")


def session_state(path):
    return read_json(session_file(path)) if session_file(path).exists() else {"sessions": [], "last": None}


def host_cli_env(path):
    """Use the default host DB, without running global/project integrations."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("OPENCODE_") or key.startswith("OC_BOX_"):
            env.pop(key)
    for name in ("host-config", "host-state", "empty-home"):
        (path.parent / name).mkdir(exist_ok=True, mode=0o700)
    env.update({
        "OPENCODE_PURE": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_TEST_HOME": str(path.parent / "empty-home"),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "XDG_CONFIG_HOME": str(path.parent / "host-config"),
        "XDG_STATE_HOME": str(path.parent / "host-state"),
        "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")),
        "OPENCODE_CONFIG_CONTENT": json.dumps({"$schema": "https://opencode.ai/config.json", "snapshot": False, "formatter": False, "lsp": False}),
    })
    return env


def container_env(data):
    home = data["home"]
    env = {
        "HOME": home, "XDG_CONFIG_HOME": home + "/.config",
        "XDG_DATA_HOME": home + "/.local/share", "XDG_STATE_HOME": home + "/.local/state",
        "XDG_CACHE_HOME": home + "/.cache", "SHELL": "/bin/bash",
        "PWD": data["project"],
        "OPENCODE_DISABLE_SANDBOX": "1", "OC_BOX_CONTAINER": "1",
        "OPENCODE_CONFIG": home + "/.config/opencode/oc-box-profile.json",
        "OPENCODE_DISABLE_AUTOUPDATE": "1", "TERM": os.environ.get("TERM", "xterm-256color"),
        "LEMONADE_BASE_URL": "http://host.containers.internal:13305/api/v1",
    }
    # Pass provider/MCP credentials and explicit settings, never host PATH,
    # loader settings, Unix agent/engine sockets, or sandbox proxy variables.
    for key, value in os.environ.items():
        if (key.endswith(("_API_KEY", "_TOKEN")) or key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BRAVE_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "COLORTERM", "LANG", "LEMONADE_BASE_URL")) and "\0" not in value:
            env[key] = value
    settings = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "oc-box/config.json"
    if settings.is_file():
        extra = read_json(settings).get("pass_env", [])
        for key in extra:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key):
                raise BoxError("Invalid pass_env entry in oc-box/config.json")
            if key in os.environ:
                env[key] = os.environ[key]
    return {key: rewrite_localhost(value) for key, value in env.items()}


def exec_command(data, command, *, interactive=False):
    result = ["podman", "exec", "-i"]
    if interactive and sys.stdin.isatty() and sys.stdout.isatty():
        result.append("-t")
    result.extend(["--workdir", data["project"]])
    for key, value in container_env(data).items():
        result.extend(["--env", f"{key}={value}"])
    result.extend([data["container"], *command])
    return result


def rewrite_localhost(value):
    return re.sub(r"(https?://)(?:localhost|127\.0\.0\.1|\[::1\])(?=[:/]|$)", r"\1host.containers.internal", value)


def rewrite_config(value, key=None):
    if isinstance(value, dict):
        return {name: rewrite_config(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [rewrite_config(item) for item in value]
    # Redirect service endpoints, not OAuth redirect URIs, shell snippets, or
    # arbitrary strings in user instructions/configuration.
    return rewrite_localhost(value) if isinstance(value, str) and key in ("url", "baseURL", "endpoint", "serverUrl") else value


def config_archive(data):
    """Snapshot user config as bytes. Extracted only inside the container."""
    source = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "opencode"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        def add_bytes(name, content, mode=0o600):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))

        for file in sorted(source.rglob("*")) if source.is_dir() else []:
            relative = file.relative_to(source)
            if any(part in ("node_modules", ".git") for part in relative.parts) or not file.is_file() or relative == Path("AGENTS.md"):
                continue
            content = file.read_bytes()
            if len(relative.parts) == 1 and file.name in ("config.json", "opencode.json", "opencode.jsonc"):
                config = rewrite_config(parse_jsonc(content.decode()))
                for server in config.get("mcp", {}).values():
                    if isinstance(server, dict) and server.get("type") == "local":
                        # A fresh distro must download npx/uvx packages on first
                        # startup; retain explicit per-server overrides.
                        server.setdefault("timeout", 120000)
                # The host config directory is copied, never mounted. Keep
                # absolute local-plugin paths valid with custom XDG roots too.
                content = json.dumps(config, indent=2).replace(str(source), data["home"] + "/.config/opencode").encode()
            add_bytes(str(relative), content, file.stat().st_mode & 0o777)
        note = ("\n\n# oc-box environment for this launch\n"
                "OpenCode and all tools, MCP servers, formatters, language servers, and plugins run INSIDE a rootless Podman container. "
                "The writable project is at the same path as on the host. The HOME here is a private container volume, not the host home. "
                "For this oc-box launch the host instructions about Bubblewrap, the host network allowlist, and read-only HOME do not apply: Bash is not wrapped in Bubblewrap here and HOME is private writable container storage. You may install packages and modify the container OS. "
                "Do not edit .oc-box-id. Host services are reachable as host.containers.internal.\n")
        original = (source / "AGENTS.md").read_text() if (source / "AGENTS.md").is_file() else ""
        add_bytes("AGENTS.md", (original + note).encode())
        # A final local-scope overlay enables native integrations only when
        # omitted, and selects the container shell, without changing user files.
        merged = {}
        for filename in ("config.json", "opencode.json", "opencode.jsonc"):
            file = source / filename
            if file.is_file():
                merged.update(parse_jsonc(file.read_text()))
        overlay = {"$schema": "https://opencode.ai/config.json", "shell": "/bin/bash"}
        for key in ("lsp", "formatter"):
            if key not in merged:
                overlay[key] = True
        add_bytes("oc-box-profile.json", json.dumps(overlay).encode())
    return buffer.getvalue()


def ensure_opencode_version(data, version):
    """Use exactly the host release, including downgrades; never fall back."""
    current = run(exec_command(data, [CONTAINER_OPENCODE, "--version"]), capture=True, check=False, timeout=30)
    if current.returncode or current.stdout.strip() != version:
        run(exec_command(data, ["npm", "install", "--global", "--prefix", "/opt/oc-box/runtime", "--no-audit", "--no-fund", f"opencode-ai@{version}"]), timeout=600)
        installed = run(exec_command(data, [CONTAINER_OPENCODE, "--version"]), capture=True, timeout=30).stdout.strip()
        if installed != version:
            raise BoxError(f"OpenCode version mismatch: host is {version}, container reports {installed!r}. Refusing to launch a different version.")
    print(f"oc-box: OpenCode {version} matches the host version.", flush=True)


def prepare_runtime(path, data, version):
    # Install the unmodified upstream version currently installed on the host.
    # Node/npm/uv are also used by the user's ordinary MCP/LSP setup.
    ensure_opencode_version(data, version)
    archive = config_archive(data)
    unpack = (APP / "sync_config.py").read_text()
    # binary input requires the binary subprocess mode here.
    try:
        proc = subprocess.run(exec_command(data, ["python3", "-c", unpack, version]), input=archive, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise BoxError("Copying configuration into the container timed out.") from exc
    if proc.returncode:
        raise BoxError("Cannot copy OpenCode config into container: " + proc.stderr.decode(errors="replace"))
    if proc.stdout:
        print("oc-box: " + proc.stdout.decode(errors="replace").strip(), flush=True)
    auth = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/auth.json"
    auth_content = os.environ.get("OPENCODE_AUTH_CONTENT") or (auth.read_text() if auth.is_file() else None)
    if auth_content:
        json.loads(auth_content)
        target = "import os,pathlib,sys; p=pathlib.Path(os.environ['XDG_DATA_HOME'])/'opencode/auth.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.stdin.read()); p.chmod(0o600)"
        run(exec_command(data, ["python3", "-c", target]), input=auth_content, capture=True, timeout=30)
    mcp_auth = auth.with_name("mcp-auth.json")
    if mcp_auth.is_file():
        target = "import os,pathlib,sys; p=pathlib.Path(os.environ['XDG_DATA_HOME'])/'opencode/mcp-auth.json'; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.stdin.read()); p.chmod(0o600)"
        run(exec_command(data, ["python3", "-c", target]), input=json.dumps(rewrite_config(read_json(mcp_auth))), capture=True, timeout=30)
    # Also support OpenCode versions that store auth separately from data.
    # Config-directory auth is already part of the config snapshot.


def refresh_sessions(path, data):
    result = run(exec_command(data, [CONTAINER_OPENCODE, "--pure", "session", "list", "--format", "json"]), capture=True, timeout=60)
    values = json.loads(result.stdout or "[]")
    roots = [item for item in values if item.get("directory") == data["project"] and valid_session_id(item.get("id"))]
    roots.sort(key=lambda item: item.get("updated", 0), reverse=True)
    state = session_state(path)
    state["sessions"] = list(dict.fromkeys(state.get("sessions", []) + [item["id"] for item in roots]))
    if roots:
        state["last"] = roots[0]["id"]
    atomic_json(session_file(path), state)
    return state


def valid_session_id(value):
    return isinstance(value, str) and re.fullmatch(r"ses[A-Za-z0-9_-]+", value) is not None


def archive_id(workspace_id, ident, prefix):
    # OpenCode orders messages/parts by ID. Keep the original sortable suffix.
    return prefix + "_" + hashlib.sha256(workspace_id.encode()).hexdigest()[:12] + "_" + ident.split("_", 1)[-1]


def archive_session(export, data):
    """Namespace container-controlled IDs before importing to the host DB."""
    if not isinstance(export, dict) or not isinstance(export.get("info"), dict) or not isinstance(export.get("messages"), list):
        raise BoxError("Invalid container session export")
    if not valid_session_id(export["info"].get("id")):
        raise BoxError("Invalid container session ID")
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
            if not isinstance(part.get("id"), str) or not re.fullmatch(r"prt[A-Za-z0-9_-]+", part["id"]):
                raise BoxError("Invalid exported part ID")
            mapping[part["id"]] = archive_id(data["id"], part["id"], "prt")

    def remap(value):
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, dict):
            return {key: mapping.get(item, item) if key in ("id", "sessionID", "messageID", "parentID", "partID") and isinstance(item, str) else remap(item) for key, item in value.items()}
        return value

    result = remap(result)
    result["info"].pop("parentID", None)
    result["info"].pop("permission", None)
    result["info"]["title"] = f"[oc-box {Path(data['project']).name}] " + info.get("title", "Session")
    return result


def sync_sessions(path, data):
    state = refresh_sessions(path, data)
    archives = path.parent / "history"
    archives.mkdir(exist_ok=True, mode=0o700)
    for ident in state.get("sessions", []):
        if not valid_session_id(ident):
            raise BoxError("Invalid stored session ID")
        # A regular host output file is not enough: podman exec still puts the
        # child's stdout on a pipe. Spool inside the container too; Python then
        # streams the completed file and flushes before exiting.
        spool = """import shutil,subprocess,sys,tempfile
with tempfile.TemporaryFile() as output:
    result=subprocess.run([sys.argv[2],'--pure','export',sys.argv[1]],stdout=output)
    if result.returncode: sys.exit(result.returncode)
    output.seek(0)
    shutil.copyfileobj(output,sys.stdout.buffer)
    sys.stdout.buffer.flush()
"""
        result = run(exec_command(data, ["python3", "-c", spool, ident, CONTAINER_OPENCODE]), capture=True, timeout=60)
        archived = archive_session(json.loads(result.stdout), data)
        target = archives / (archived["info"]["id"] + ".json")
        atomic_json(target, archived)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        state.setdefault("synced", {})
        if state["synced"].get(ident) == digest:
            continue
        if ident in state.get("host_sessions", {}):
            # Mainline import appends new messages but does not update existing
            # parts or metadata. Replace only this namespaced archive copy.
            run(["opencode", "--pure", "session", "delete", archived["info"]["id"]], env=host_cli_env(path), cwd=path.parent / "controller", capture=True, check=False, timeout=60)
        run(["opencode", "--pure", "import", str(target)], env=host_cli_env(path), cwd=path.parent / "controller", capture=True, timeout=60)
        state["synced"][ident] = digest
        state.setdefault("host_sessions", {})[ident] = archived["info"]["id"]
        atomic_json(session_file(path), state)
    if state.get("sessions"):
        print("Session history synced to your normal host OpenCode database.", flush=True)


def parse_jsonc(text):
    """Strip comments/trailing commas without altering quoted JSON strings."""
    tokens = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
    clean = tokens.sub(lambda m: m[0] if m[0].startswith('"') else " ", text)
    strings_and_commas = re.compile(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])')
    clean = strings_and_commas.sub(lambda m: m[0] if m[0].startswith('"') else m[1], clean)
    value = json.loads(clean)
    if not isinstance(value, dict):
        raise BoxError("OpenCode configuration must be a JSON object.")
    return value


def launch(path, data, args):
    with loading_stage("Checking OpenCode version"):
        version = run(["opencode", "--version"], capture=True).stdout.strip()
    if not re.fullmatch(r"1\.\d+\.\d+(?:[-+].*)?", version):
        raise BoxError(f"OpenCode {version} session import/export has not been checked (supported: 1.x).")
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
        with loading_stage("Finding workspace session"):
            sessions = refresh_sessions(path, data)
        command = [CONTAINER_OPENCODE, data["project"]]
        if not args.new and sessions.get("last"):
            command.extend(["--session", sessions["last"]])
        if args.model:
            command.extend(["--model", args.model])
        print(f"oc-box: {data['distro']} | {data['project']} | {'new session' if args.new or not sessions.get('last') else 'resuming ' + sessions['last']}", flush=True)
        print("oc-box: Opening OpenCode; plugins and MCP servers may need time to initialize.", flush=True)
        code = run_tui(exec_command(data, command, interactive=True))
        try:
            with loading_stage("Syncing session history"):
                sync_sessions(path, data)
        except (BoxError, ValueError, KeyError) as exc:
            print(f"oc-box: history sync pending: {exc}\nContainer data retained. Retry with 'oc-box sync'.", file=sys.stderr)
            return code or 1
        return code
    finally:
        with loading_stage("Stopping container"):
            stop_container(data)


def resource_ids(kind, ident):
    command = {"container": ("ps", "-aq"), "volume": ("volume", "ls", "-q"), "network": ("network", "ls", "-q")}[kind]
    return podman(*command, "--filter", f"label={LABEL}={ident}", capture=True).stdout.split()


def remove_resources(data, *, keep_home=False):
    # Only resources carrying this exact workspace ownership label are eligible.
    for kind in ("container", "volume", "network"):
        if kind == "volume" and keep_home:
            continue
        for ident in resource_ids(kind, data["id"]):
            info = json.loads(podman(kind, "inspect", ident, capture=True).stdout)[0]
            labels = info.get("Labels", info.get("labels", info.get("Config", {}).get("Labels", {}))) or {}
            if labels.get(LABEL) != data["id"]:
                raise BoxError(f"Ownership mismatch for {kind} {ident}")
            podman(kind, "rm", *(["--force"] if kind == "container" else []), ident, capture=True)
    # Base images are shared cache. Never prune them globally.


def purge(path, data, args):
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
        if not (args.delete_sessions or args.all) and inspect_owned(data) and Path(data["project"]).is_dir():
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
            run(["opencode", "--pure", "session", "delete", ident], env=host_cli_env(path), cwd=path.parent / "controller", timeout=60)
    remove_resources(data)
    project = Path(data["project"])
    marker = project / MARKER
    same = project.is_dir() and not project.is_symlink() and [project.stat().st_dev, project.stat().st_ino] == data["project_identity"]
    marker_matches = same and marker.is_file() and not marker.is_symlink() and marker.read_text().strip() == data["id"]
    if args.all and project.exists():
        if not marker_matches:
            raise BoxError("Project identity changed; sandbox removed, but refusing to delete the host directory.")
        validate_project(project)
        shutil.rmtree(project)
    elif marker_matches:
        marker.unlink()
    shutil.rmtree(path.parent)
    print("Workspace purged." + (" Project files and OpenCode history retained." if not args.all and not args.delete_sessions else ""))


def parser():
    result = argparse.ArgumentParser(prog="oc-box", description="Run full OpenCode in rootless Podman; sync session history to the host on exit.")
    result.add_argument("command", nargs="?", choices=("start", "stop", "sync", "reset", "purge", "list", "distros", "status"), default="start")
    result.add_argument("--project", type=Path, default=Path.cwd(), help="project directory (default: current directory)")
    result.add_argument("--workspace", help="workspace ID, also works after the project directory was deleted")
    result.add_argument("--distro", help="distribution for a new environment; use 'reset --distro NAME' to switch")
    group = result.add_mutually_exclusive_group()
    group.add_argument("--new", action="store_true", help="start a new conversation in this environment")
    group.add_argument("--resume", action="store_true", help="explicitly resume the recorded conversation")
    result.add_argument("--model", help="OpenCode provider/model")
    result.add_argument("--cpus", help="CPU limit (default: 4)")
    result.add_argument("--memory", help="memory limit (default: 8g)")
    result.add_argument("--pids-limit", type=int, help="process limit (default: 2048)")
    result.add_argument("--delete-sessions", action="store_true", help="purge associated conversations too")
    result.add_argument("--all", action="store_true", help="purge sandbox, conversations, AND project directory")
    result.add_argument("-y", "--yes", action="store_true", help="skip purge confirmation")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.distro:
        args.distro = distro_name(args.distro)
    if args.command == "distros":
        default = host_default()
        for name, (image, _) in DISTROS.items():
            print(f"{name:12} {image}" + ("  [host default]" if name == default else ""))
        return 0
    if args.command == "list":
        for path in workspace_paths():
            data = read_json(path)
            exists = "present" if Path(data["project"]).is_dir() else "missing directory"
            latest = session_state(path).get("last") or "none"
            print(f"{data['id']}  {data['distro']:12} {data['project']}  ({exists})  session={latest}")
        return 0
    if args.command != "purge" and (args.all or args.delete_sessions or args.yes):
        raise BoxError("--all, --delete-sessions, and --yes apply only to purge.")
    with loading_stage("Checking rootless Podman"):
        check_backend()
    state_root().mkdir(parents=True, exist_ok=True, mode=0o700)
    path, data = find_workspace(args.project, args.workspace)
    if data is None:
        if args.command != "start":
            raise BoxError("No oc-box workspace registered here. Use 'oc-box list' or '--workspace ID'.")
        if args.resume:
            raise BoxError("No workspace/session exists here to resume.")
        project = validate_project(args.project)
        path, data = create_workspace(project, distro_name(args.distro or host_default()))
    if data.get("version") != 2:
        raise BoxError("This workspace uses the retired experimental tool-routing design. Purge it with the older launcher before creating a new environment.")
    # Status and explicit stop are usable from a second terminal. Tool calls
    # never auto-restart a stopped container; the running TUI can then be exited.
    if args.command == "stop":
        stop_container(data)
        print("Container stopped; files, packages, and conversations retained.")
        return 0
    if args.command == "status":
        info = inspect_owned(data)
        print(json.dumps({**data, "running": bool(info and info["State"]["Running"]), "session": session_state(path).get("last")}, indent=2))
        return 0
    with workspace_lock(path):
        if args.command in ("start", "reset", "sync"):
            if args.command == "start" and args.distro and distro_name(args.distro) != data["distro"]:
                raise BoxError(f"Existing workspace uses {data['distro']}. Use 'oc-box reset --distro {args.distro}' to switch.")
            if args.command == "reset":
                check_resource_controllers(data)
                remove_resources(data, keep_home=True)
                data["ready"] = False
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BoxError, OSError, ValueError) as exc:
        print(f"oc-box: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
