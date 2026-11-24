"""Disposable local Podman compatibility probe for an exact native V2 artifact."""

import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

if __package__:
    from . import lifecycle_output
else:
    import lifecycle_output


VERSION = re.compile(r"2\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})")
SHA256 = re.compile(r"[a-f0-9]{64}")
CONFIRMED_REFUSALS = frozenset({"build-plugin"})
REFUSAL_CATEGORIES = frozenset(
    {
        "unsupported-version",
        "backend",
        "stage-image",
        "scratch-create",
        "stage",
        "sdk",
        "artifact",
        "probe",
        "build-plugin",
        "cleanup",
    }
)
_IMAGE_REPOSITORY = "docker.io/library/node"
# Official Node 22 Bookworm platform manifests, not a mutable tag or local alias.
STAGE_IMAGES = {
    "x86_64": _IMAGE_REPOSITORY
    + "@sha256:69c6e263db01e9946c7743bbbe9f7b1ba0df384d27ec09e08e5832d6acd3daf3",
    "aarch64": _IMAGE_REPOSITORY
    + "@sha256:f796bda8a91392fe7b5c974ff42abaa5cc801eb6987dca967f1db8be493e0ee3",
}
_LABEL = "io.oc-box.native-preflight"
_PHASE = _LABEL + ".phase"
_STAGE_SECONDS = 210
_PROBE_SECONDS = 100
_CONTROL_SECONDS = 12
# Preparing the pinned image for keep-id on a fresh rootless store can be
# materially slower than inspecting an existing engine object.
_CREATE_SECONDS = 90
_EXE_PATH = "/scratch/runtime/bin/opencode"
_HOME = "/tmp/preflight-home"
_PROJECT = "/tmp/preflight-project"
_PATH = "/usr/local/bin:/usr/bin:/bin"
_CONTROL_OUTPUT = 16384
_CATEGORIES_BY_EXIT = {42: "sdk", 43: "artifact", 44: "build-plugin", 45: "cleanup"}
LOG_REASONS = {
    "build-plugin": "build-plugin-failed",
    "unsupported-version": "unsupported-version",
    "backend": "scope-unavailable",
    "stage-image": "stage-image",
    "scratch-create": "scratch-creation",
    "stage": "transport",
    "sdk": "verification",
    "artifact": "verification",
    "probe": "startup-failed",
    "cleanup": "cleanup-unverified",
}


class Refusal(ValueError):
    """A fixed category; never carry engine, npm or native diagnostic text."""

    def __init__(self, category, *, proof=None, exit_code=None):
        if category not in REFUSAL_CATEGORIES or (
            proof is not None
            and (category not in CONFIRMED_REFUSALS or not SHA256.fullmatch(proof))
        ):
            raise ValueError("Invalid native preflight refusal.")
        self.category = category
        self.proof = proof
        self.exit_code = (
            exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None
        )
        super().__init__(category)


def version_metrics(version):
    if version is None:
        return None
    fields = ("opencode_major", "opencode_minor", "opencode_patch")
    return dict(zip(fields, map(int, version.split("."))))


@contextlib.contextmanager
def _logged_phase(phase, version=None):
    with lifecycle_output.preparation(
        "native-preflight-" + phase, quiet=True, metrics=version_metrics(version)
    ) as report:
        try:
            yield report
        except Refusal as exc:
            report["reason"] = LOG_REASONS[exc.category]
            report["exit_code"] = exc.exit_code
            raise
        else:
            report.setdefault("exit_code", 0)
            report["outcome"] = "success"


# Reused verbatim by the staging container and by the live runtime admission
# check. npm's postinstall *executes* the candidate; use --ignore-scripts and
# the explicit materialization mode instead. Never execute a wrapper to find
# out what it resolves to.
FINGERPRINT_SOURCE = r"""
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import sys

_CLI = "lib/node_modules/@opencode/cli"
_EXE = _CLI + "/bin/opencode.exe"
_DIR = os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
_SHA = re.compile(r"[a-f0-9]{64}")
_VERSION = re.compile(r"2\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})")
_MAX_BINARY = 768 * 1024 * 1024


@contextlib.contextmanager
def directory(parent, path):
    fd = os.dup(parent)
    try:
        for component in path.split("/"):
            if component in ("", ".", ".."):
                raise ValueError()
            next_fd = os.open(component, _DIR, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def prefix_directory(prefix):
    if not os.path.isabs(prefix) or os.path.normpath(prefix) != prefix:
        raise ValueError()
    root = os.open("/", _DIR)
    try:
        with directory(root, prefix[1:]) as fd:
            yield fd
    finally:
        os.close(root)


def snapshot(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_ctime_ns, info.st_mtime_ns)


def native_digest(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o111
        or info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_IWOTH)
        or not 64 <= info.st_size <= _MAX_BINARY
    ):
        raise ValueError()
    elf = os.read(fd, 64)
    machine = {"x86_64": 62, "aarch64": 183}.get(os.uname().machine)
    if (
        len(elf) != 64 or elf[:7] != b"\x7fELF\x02\x01\x01"
        or int.from_bytes(elf[16:18], "little") not in (2, 3)
        or int.from_bytes(elf[18:20], "little") != machine
        or int.from_bytes(elf[20:24], "little") != 1
    ):
        raise ValueError()
    h = hashlib.sha256(elf)
    remaining = info.st_size - len(elf)
    while remaining:
        data = os.read(fd, min(1024 * 1024, remaining))
        if not data:
            raise ValueError()
        h.update(data)
        remaining -= len(data)
    if os.read(fd, 1) or snapshot(os.fstat(fd)) != snapshot(info):
        raise ValueError()
    return h.hexdigest()


@contextlib.contextmanager
def open_native(prefix):
    with prefix_directory(prefix) as base, directory(base, "bin") as bin_dir:
        alias = os.readlink("opencode", dir_fd=bin_dir)
        if alias not in ("../" + _EXE, prefix + "/" + _EXE):
            raise ValueError()
        with directory(base, _CLI + "/bin") as target:
            fd = os.open("opencode.exe", _FILE, dir_fd=target)
            try:
                proof = native_digest(fd)
                if (
                    snapshot(os.stat("opencode.exe", dir_fd=target, follow_symlinks=False))
                    != snapshot(os.fstat(fd))
                    or os.readlink("opencode", dir_fd=bin_dir) != alias
                ):
                    raise ValueError()
                yield fd, proof
            finally:
                os.close(fd)


def fingerprint(prefix):
    with open_native(prefix) as (_, proof):
        return proof


def platform_package():
    machine = os.uname().machine
    platform = {"x86_64": "x64", "aarch64": "arm64"}.get(machine)
    if platform is None:
        raise ValueError()
    variant = ""
    if platform == "x64":
        with open("/proc/cpuinfo", "rb") as stream:
            flags = stream.read(1024 * 1024 + 1)
        if len(flags) > 1024 * 1024:
            raise ValueError()
        if not re.search(rb"(^|\s)avx2(\s|$)", flags, re.IGNORECASE):
            variant = "-baseline"
    if not os.confstr("CS_GNU_LIBC_VERSION"):
        variant += "-musl"
    return "@opencode/cli-linux-" + platform + variant


def package_json(fd):
    with os.fdopen(os.open("package.json", _FILE, dir_fd=fd), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError()
        value = json.loads(stream.read(65537))
    if not isinstance(value, dict):
        raise ValueError()
    return value


@contextlib.contextmanager
def platform_binary(base, package, version):
    # npm's ordinary global install nests the platform package; a global
    # install explicitly naming that package may place it beside the launcher.
    with contextlib.ExitStack() as stack:
        found = []
        for location in (_CLI + "/node_modules/" + package, "lib/node_modules/" + package):
            try:
                found.append(stack.enter_context(directory(base, location)))
            except FileNotFoundError:
                continue
        if len(found) != 1:
            raise ValueError()
        package_dir = found[0]
        metadata = package_json(package_dir)
        if metadata.get("name") != package or metadata.get("version") != version:
            raise ValueError()
        yield stack.enter_context(directory(package_dir, "bin"))


def materialize(prefix, version, expected=None):
    if not _VERSION.fullmatch(version) or (
        expected is not None and not _SHA.fullmatch(expected)
    ):
        raise ValueError()
    package = platform_package()
    with prefix_directory(prefix) as base, directory(base, _CLI) as cli:
        metadata = package_json(cli)
        commands = metadata.get("bin")
        dependencies = metadata.get("optionalDependencies")
        if (
            metadata.get("name") != "@opencode/cli" or metadata.get("version") != version
            or not isinstance(commands, dict)
            or commands.get("opencode") not in ("./bin/opencode.exe", "bin/opencode.exe")
            or set(commands) != {"opencode", "opencode2"}
            or commands.get("opencode2") != commands["opencode"]
            or metadata.get("opencodeSourceBinary", "opencode") != "opencode"
            or not isinstance(dependencies, dict) or dependencies.get(package) != version
        ):
            raise ValueError()
        with platform_binary(base, package, version) as origin:
            fd = os.open("opencode", _FILE, dir_fd=origin)
            try:
                proof = native_digest(fd)
                if expected is not None and expected != proof:
                    raise ValueError()
                for parent in (base, cli):
                    try:
                        os.mkdir("bin", 0o755, dir_fd=parent)
                    except FileExistsError:
                        pass
                with directory(base, "bin") as bin_dir, directory(cli, "bin") as target:
                    for alias in commands:
                        try:
                            os.readlink(alias, dir_fd=bin_dir)
                        except FileNotFoundError:
                            pass
                    tmp = ".oc-box-native-" + secrets.token_hex(12)
                    os.link("opencode", tmp, src_dir_fd=origin, dst_dir_fd=target,
                            follow_symlinks=False)
                    try:
                        opened = os.fstat(fd)
                        linked = os.stat(tmp, dir_fd=target, follow_symlinks=False)
                        if snapshot(opened) != snapshot(linked):
                            raise ValueError()
                        # The link changed ctime. Rehash pinned bytes before
                        # exposing either launch alias.
                        os.lseek(fd, 0, os.SEEK_SET)
                        if native_digest(fd) != proof:
                            raise ValueError()
                        os.replace(tmp, "opencode.exe", src_dir_fd=target, dst_dir_fd=target)
                    finally:
                        try:
                            os.unlink(tmp, dir_fd=target)
                        except FileNotFoundError:
                            pass
                    for alias in commands:
                        tmp = ".oc-box-native-" + secrets.token_hex(12)
                        os.symlink("../" + _EXE, tmp, dir_fd=bin_dir)
                        try:
                            os.replace(tmp, alias, src_dir_fd=bin_dir, dst_dir_fd=bin_dir)
                        finally:
                            try:
                                os.unlink(tmp, dir_fd=bin_dir)
                            except FileNotFoundError:
                                pass
            finally:
                os.close(fd)
    if fingerprint(prefix) != proof:
        raise ValueError()
    return proof


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2:
            print(fingerprint(sys.argv[1]))
        elif len(sys.argv) == 5 and sys.argv[2] == "--materialize":
            print(materialize(sys.argv[1], sys.argv[3], sys.argv[4]))
        else:
            raise ValueError()
    except Exception:
        raise SystemExit(1) from None
"""


EXEC_SOURCE = (
    FINGERPRINT_SOURCE.split('\n\nif __name__ == "__main__":', 1)[0]
    + r"""
try:
    prefix, expected = sys.argv[1:3]
    with open_native(prefix) as (fd, proof):
        if proof != expected:
            raise ValueError()
        # A companion Python bridge may spawn the CLI after it has started.
        # Keep this fd open in that bridge and give it the fixed parent-fd name.
        os.set_inheritable(fd, True)
        name = f"/proc/{os.getpid()}/fd/{fd}"
        command = [name if item == prefix + "/bin/opencode" else item for item in sys.argv[3:]]
        os.execvpe(command[0], command, os.environ)
except Exception:
    raise SystemExit(126) from None
"""
)


_STAGE = r"""
import contextlib
import json
import os
from pathlib import Path
import re
import resource
import select
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import tarfile
import threading
import time
from urllib.parse import urlsplit


REGISTRY = "registry.npmjs.org"
TRANSFER = 256 * 1024 * 1024


class RegistryProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 8
    slots = threading.BoundedSemaphore(8)
    count_lock = threading.Lock()
    remaining = TRANSFER

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def admit(self, size):
        with self.count_lock:
            self.remaining -= size
            return self.remaining >= 0

    def handle_error(self, request, client_address):
        pass


class Tunnel(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(8)
        if not re.fullmatch(
            rb"CONNECT registry\.npmjs\.org:443 HTTP/1\.[01]\r\n", self.rfile.readline(512)
        ):
            return
        budget = 8192
        while budget > 0:
            line = self.rfile.readline(min(1024, budget))
            budget -= len(line)
            if line == b"\r\n":
                break
            if not line.endswith(b"\r\n"):
                return
        else:
            return
        with socket.create_connection((REGISTRY, 443), timeout=8) as upstream:
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            upstream.settimeout(8)
            peers = {self.connection: upstream, upstream: self.connection}
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                for source in select.select(list(peers), [], [], 1)[0]:
                    frame = source.recv(65536)
                    if not frame or not self.server.admit(len(frame)):
                        return
                    peers[source].sendall(frame)


def canonical_lock(path, version):
    packages = sdk["sdk_lock_packages"](path, version)
    if packages is None:
        return False
    for location, item in packages.items():
        if not location:
            continue
        url = urlsplit(item.get("resolved", ""))
        if (
            url.scheme != "https" or url.netloc != REGISTRY
            or not url.path.startswith("/") or not url.path.endswith(".tgz")
            or url.query or url.fragment or item.get("link")
            or not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", item.get("integrity", ""))
        ):
            return False
    return True


def publish_sdk(config, destination):
    # The audited 2.3.2 and 2.4.0 SDKs each contain ~20k entries / 132 MiB.
    # Leave bounded headroom; refuse large or ambiguous future layouts.
    remaining = 192 * 1024 * 1024
    entries = 32768
    for parent, directories, files in os.walk(config, followlinks=False):
        for item in directories + files:
            path = Path(parent) / item
            info = path.lstat()
            entries -= 1
            if stat.S_ISREG(info.st_mode):
                remaining -= info.st_size
            elif stat.S_ISLNK(info.st_mode):
                if os.path.isabs(os.readlink(path)) or not path.resolve(strict=True).is_relative_to(config):
                    raise ValueError()
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError()
            if entries < 0 or remaining < 0:
                raise ValueError()
    shutil.copytree(config, destination, symlinks=True)


def stage_cli(npm, runtime, wrapper_dir, package, version):
    wrapper_dir.mkdir()
    # A global install of the wrapper resolves every Linux x64 variant,
    # even with --omit=optional on supported npm. Pack only the tiny wrapper
    # and install exactly one platform artifact. Copy just the wrapper's
    # bounded manifest; never extract or run its scripts or launcher stub.
    npm("pack", "--pack-destination", str(wrapper_dir), f"@opencode/cli@{version}")
    wrapper = wrapper_dir / f"opencode-cli-{version}.tgz"
    info = wrapper.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= 1024 * 1024:
        raise ValueError()
    manifest = None
    with tarfile.open(wrapper, "r:gz") as archive:
        for count, item in enumerate(archive):
            if count >= 64 or not 0 <= item.size <= 1024 * 1024:
                raise ValueError()
            if item.name != "package/package.json":
                continue
            if manifest is not None or not item.isreg() or not 1 <= item.size <= 65536:
                raise ValueError()
            with archive.extractfile(item) as source:
                manifest = source.read(65537)
            if len(manifest) != item.size:
                raise ValueError()
    if manifest is None:
        raise ValueError()
    npm("install", "--global", f"--prefix={runtime}", f"{package}@{version}")
    target = runtime / "lib/node_modules/@opencode/cli"
    target.mkdir(parents=True)
    with (target / "package.json").open("xb") as stream:
        stream.write(manifest)


def fetch(version):
    resource.setrlimit(resource.RLIMIT_FSIZE, (768 * 1024 * 1024,) * 2)
    os.umask(0o077)
    os.environ["HOME"] = "/tmp/npm-home"
    Path(os.environ["HOME"]).mkdir()
    with RegistryProxy(("127.0.0.1", 0), Tunnel) as proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        address = f"http://127.0.0.1:{proxy.server_address[1]}"
        # npm's Node fetch stack uses this allowlisted tunnel. Do not execute
        # lifecycle scripts, run Git, send audit requests or read any .npmrc.
        options = [
            "npm", f"--registry=https://{REGISTRY}/",
            f"--userconfig={os.environ['HOME']}/user.npmrc",
            "--globalconfig=/dev/null", "--cache=/tmp/npm-cache",
            f"--https-proxy={address}", f"--proxy={address}", "--noproxy=",
            "--strict-ssl=true", "--git=/bin/false", "--no-audit", "--no-fund",
            "--ignore-scripts", "--maxsockets=4", "--fetch-retries=0",
            "--fetch-timeout=90000", "--loglevel=error",
        ]

        def npm(*args):
            subprocess.run(
                [*options, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd="/tmp", check=True, timeout=160,
            )

        package = artifact["platform_package"]()
        stage_cli(npm, Path("/tmp/runtime"), Path("/tmp/wrapper"), package, version)
        try:
            proof = artifact["materialize"]("/tmp/runtime", version)
            # npm extracts into bounded tmpfs. Copy only the verified CLI
            # target, not its package tree or registry cache, across phases.
            target = Path("/scratch/runtime")
            executable = artifact["_EXE"]
            (target / executable).parent.mkdir(parents=True)
            shutil.copyfile(Path("/tmp/runtime") / executable, target / executable)
            (target / executable).chmod(0o700)
            (target / "bin").mkdir()
            (target / "bin/opencode").symlink_to("../" + executable)
            if artifact["fingerprint"](str(target)) != proof:
                raise ValueError()
        except Exception:
            raise SystemExit(43) from None
        try:
            config = Path("/tmp/sdk")
            config.mkdir()
            manifest = sdk["runtime_manifest"](
                {}, sdk["V2_PLUGIN_PACKAGE"], version, sdk["sdk_security_overrides"](version)
            )
            (config / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            prefix = f"--prefix={config}"
            strategy = "--install-strategy=shallow"
            npm("install", prefix, strategy, "--package-lock-only")
            if not canonical_lock(config / "package-lock.json", version):
                raise ValueError()
            npm("ci", prefix, strategy)
            if not sdk["sdk_modules_match"](config, version):
                raise ValueError()
            publish_sdk(config, "/scratch/sdk")
        except Exception:
            raise SystemExit(42) from None
        return proof


if __name__ == "__main__":
    try:
        print(fetch(sys.argv[1]), flush=True)
    except Exception:
        raise SystemExit(41) from None
"""


_PROBE = r"""
import json
import os
from pathlib import Path
import re
import resource
import select
import shutil
import subprocess
import sys
import time


def agrees(binary, version):
    exact = rb"(?:opencode[ \t]+)?v?" + re.escape(version.encode("ascii")) + rb"\r?\n"
    with subprocess.Popen(
        [binary, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True,
    ) as child:
        output = bytearray()
        deadline = time.monotonic() + 10
        try:
            while len(output) < 128 and time.monotonic() < deadline:
                if not select.select([child.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                chunk = os.read(child.stdout.fileno(), 128 - len(output))
                if not chunk:
                    return child.wait(timeout=1) == 0 and re.fullmatch(exact, output) is not None
                output.extend(chunk)
            return False
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)


def probe(version, expected, settings):
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024,) * 2)
    os.umask(0o077)
    home = Path(settings["home"])
    project = Path(settings["project"])
    config = home / ".config/opencode"
    project.mkdir()
    os.chdir(project)
    shutil.copytree("/scratch/sdk", config, symlinks=True)
    if not sdk["sdk_modules_match"](config, version):
        raise SystemExit(42)
    build = config / "plugins/oc-box-build-permissions"
    build.mkdir(parents=True)
    (build / "package.json").write_text('{"type":"module","main":"index.js"}', encoding="utf-8")
    (build / "index.js").write_text(settings["plugin"], encoding="utf-8")
    profile = config / "oc-box-profile.json"
    profile.write_text(json.dumps(settings["profile"]), encoding="utf-8")
    os.environ.update({
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
        "PWD": str(project), "SHELL": "/bin/bash", "OC_BOX_CONTAINER": "1",
        "OPENCODE_CONFIG": str(profile), "OPENCODE_CONFIG_CONTENT": json.dumps(settings["overlay"]),
        "OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_SANDBOX": "1", "OPENCODE_DB": "oc-box-v2.db",
    })
    binary = settings["binary"]
    try:
        if artifact["fingerprint"]("/scratch/runtime") != expected or not agrees(binary, version):
            raise ValueError()
    except Exception:
        raise SystemExit(43) from None
    exit_info, admitted = {}, False
    deadline = time.monotonic() + 66
    try:
        with bridge["server"](binary, exit_info=exit_info, startup_deadline=deadline) as request:
            admitted = True
            try:
                plugins = bridge["ready_plugins"](request, str(project), startup_deadline=deadline)
                build = [p for p in plugins if p.get("id") == "oc-box.build-permissions"]
                if len(build) != 1 or build[0]["state"]["status"] != "active" or not request.alive():
                    raise ValueError()
            except bridge["FixedError"] as error:
                # Only a definitive failed required plugin is cacheable. An
                # absent/malformed response or timeout says nothing permanent.
                if str(error) == bridge["BUILD_PLUGIN_FAILED"]:
                    raise SystemExit(44) from None
                raise
    finally:
        if exit_info.get("forced") or (admitted and exit_info != {"code": 0, "forced": False}):
            raise SystemExit(45)
    return expected


if __name__ == "__main__":
    try:
        result = probe(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
        print(result, flush=True)
    except Exception:
        raise SystemExit(46) from None
"""


class _CommandError(Exception):
    pass


class _Unreaped(Exception):
    pass


def _invoke(podman, env, args, *, limit=_CONTROL_OUTPUT, timeout=_CONTROL_SECONDS):
    """Bound the *only* output pipe, and reap our specific local engine client."""
    deadline = time.monotonic() + timeout
    child = subprocess.Popen(
        [
            podman,
            "--remote=false",
            "--default-mounts-file",
            str(Path(env["XDG_CONFIG_HOME"]) / "containers/mounts.conf"),
            *args,
        ],
        env=env,
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    data = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if (
                remaining <= 0
                or not select.select([child.stdout], [], [], remaining)[0]
            ):
                raise _CommandError()
            chunk = os.read(child.stdout.fileno(), min(4096, limit + 1 - len(data)))
            if not chunk:
                code = child.wait(timeout=max(0, deadline - time.monotonic()))
                return code, bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise _CommandError()
    finally:
        child.stdout.close()
        try:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=3)
        except (OSError, KeyboardInterrupt, subprocess.SubprocessError):
            raise _Unreaped() from None


def _checked(podman, env, args, *, category, **kwargs):
    try:
        return _invoke(podman, env, args, **kwargs)
    except (OSError, ValueError, subprocess.SubprocessError, _CommandError):
        raise Refusal(category) from None


def _private_env(root):
    home = Path.home()
    data = os.environ.get("XDG_DATA_HOME") or str(home / ".local/share")
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not all(Path(path).is_absolute() for path in (data, runtime)):
        raise Refusal("backend")
    config = root / "home/.config"
    defaults = config / "containers"
    defaults.mkdir(parents=True)
    # libpod's subscription-mount lookup can read HOME independently of XDG.
    mounts = defaults / "mounts.conf"
    mounts.touch()
    hooks = root / "hooks"
    hooks.mkdir()
    engine = defaults / "containers.conf"
    engine.write_text(
        '[engine]\nevents_logger = "none"\nhooks_dir = ['
        + json.dumps(str(hooks))
        + "]\n",
        encoding="utf-8",
    )
    return {
        "PATH": _PATH + ":/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": data,
        "XDG_RUNTIME_DIR": runtime,
        "CONTAINERS_CONF": str(engine),
    }


@contextlib.contextmanager
def _termination():
    """An ordinary CLI SIGTERM takes the same cleanup path as Ctrl-C."""
    main = threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGTERM)
    changed = main and previous == signal.SIG_DFL

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    if changed:
        signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        if changed:
            signal.signal(signal.SIGTERM, previous)


def _one_line(output, pattern, category):
    try:
        value = output.decode("ascii")
    except UnicodeError:
        raise Refusal(category) from None
    if not value.endswith("\n") or not pattern.fullmatch(value[:-1]):
        raise Refusal(category)
    return value[:-1]


class _Scratch:
    def __init__(self, root, podman, env, image):
        self.podman, self.env, self.image = podman, env, image
        self.artifacts = root / "artifacts"
        self.artifacts.mkdir(mode=0o700)
        self.token = secrets.token_hex(16)
        self.retain = False

    def call(self, *args, category, **kwargs):
        return _checked(self.podman, self.env, args, category=category, **kwargs)

    def exists(self, selector):
        code, output = self.call("container", "exists", selector, category="cleanup")
        if output or code not in (0, 1):
            raise Refusal("cleanup")
        return code == 0

    def inspect(self, selector, name, phase, ident=None):
        fields = (
            ".ID",
            ".Name",
            ".Config.Labels",
            ".State",
            ".Image",
            ".Mounts",
            ".HostConfig.PortBindings",
            ".HostConfig.NetworkMode",
        )
        template = "\n".join("{{json " + field + "}}" for field in fields)
        code, output = self.call(
            "container", "inspect", "--format", template, selector, category="cleanup"
        )
        try:
            if code:
                raise ValueError()
            cid, actual_name, labels, state, image, mounts, ports, network = (
                json.loads(line) for line in output.splitlines()
            )
            if (
                not SHA256.fullmatch(cid)
                or ident is not None
                and cid != ident
                or actual_name != name
                or labels.get(_LABEL) != self.token
                or labels.get(_PHASE) != phase
                or image != self.image
                or not isinstance(state, dict)
                or type(state.get("Running")) is not bool
                or type(state.get("Pid")) is not int
            ):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise Refusal("cleanup") from None
        return cid, state, mounts, ports, network

    def clean(self, name, phase, ident):
        if ident is not None:
            if not self.exists(ident):
                # No launch can pass relying on an independently removed
                # container, but verified absence is enough for cleanup.
                if self.exists(name):
                    raise Refusal("cleanup")
                return
        elif not self.exists(name):
            return
        cid, state, *_ = self.inspect(ident or name, name, phase, ident)
        if state["Running"]:
            code, _ = self.call("stop", "--time=3", cid, category="cleanup")
            if code:
                raise Refusal("cleanup")
            _, state, *_ = self.inspect(cid, name, phase, cid)
        if (
            state["Running"]
            or state["Pid"] != 0
            or state.get("Status") not in {"created", "configured", "exited", "stopped"}
        ):
            raise Refusal("cleanup")
        code, _ = self.call("rm", cid, category="cleanup")
        if code or self.exists(cid) or self.exists(name):
            raise Refusal("cleanup")

    def run(self, phase, script, args, seconds):
        name = f"oc-box-native-preflight-{self.token}-{phase}"
        if self.exists(name):
            raise Refusal("cleanup")
        ident = None
        offline = phase == "probe"
        network = "none" if offline else "pasta:--no-map-gw"
        access = "ro" if offline else "rw"
        temporary = 512 if offline else 896
        try:
            code, output = self.call(
                "create",
                "--name",
                name,
                "--label",
                f"{_LABEL}={self.token}",
                "--label",
                f"{_PHASE}={phase}",
                "--pull=never",
                "--network=" + network,
                "--no-hosts",
                *(("--dns=none",) if offline else ("--dns-search=.",)),
                "--env-host=false",
                "--http-proxy=false",
                "--image-volume=ignore",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--userns=keep-id",
                "--read-only",
                "--read-only-tmpfs=false",
                "--pids-limit=96",
                "--memory=1536m",
                "--log-driver=none",
                "--stop-timeout=3",
                f"--timeout={seconds + 5}",
                "--tmpfs",
                f"/tmp:rw,nodev,nosuid,mode=1777,size={temporary}m",
                "--volume",
                f"{self.artifacts}:/scratch:{access},Z,nodev,nosuid",
                "--workdir=/tmp",
                "--entrypoint=/usr/bin/env",
                self.image,
                "-i",
                f"PATH={_PATH}",
                "LANG=C.UTF-8",
                "/usr/bin/python3",
                "-I",
                "-c",
                script,
                *args,
                category="scratch-create",
                timeout=_CREATE_SECONDS,
            )
            if code:
                raise Refusal("scratch-create", exit_code=code)
            ident = _one_line(output, SHA256, "scratch-create")
            _, state, mounts, ports, network_mode = self.inspect(
                ident, name, phase, ident
            )
            self.validate_layout(offline, state, mounts, ports, network_mode)
            code, output = self.call(
                "start", "--attach", ident, category=phase, limit=65, timeout=seconds
            )
            _, state, *_ = self.inspect(ident, name, phase, ident)
            if (
                state.get("Status") not in {"exited", "stopped"}
                or state["Running"]
                or state["Pid"] != 0
                or type(state.get("ExitCode")) is not int
                or state["ExitCode"] != code
                or state.get("OOMKilled")
            ):
                raise Refusal(phase)
            if code:
                category = _CATEGORIES_BY_EXIT.get(code, phase)
                if category in CONFIRMED_REFUSALS and (not offline or output):
                    category = phase
                raise Refusal(category, exit_code=code)
            return _one_line(output, SHA256, phase)
        except _Unreaped:
            self.retain = True
            raise Refusal("cleanup") from None
        finally:
            try:
                self.clean(name, phase, ident)
            except (Refusal, KeyboardInterrupt, _Unreaped):
                # Do not remove a scratch directory a container may still use.
                self.retain = True
                raise Refusal("cleanup") from None

    def validate_layout(self, offline, state, mounts, ports, network):
        if (
            state["Running"]
            or state["Pid"] != 0
            or state.get("Status") not in {"created", "configured"}
            or ports not in (None, {})
            or network not in (("none",) if offline else ("pasta", "pasta:--no-map-gw"))
            or not isinstance(mounts, list)
        ):
            raise Refusal("backend")
        stage_mounts = []
        for mount in mounts:
            if not isinstance(mount, dict):
                raise Refusal("backend")
            if mount.get("Type") == "tmpfs" and mount.get("Destination") == "/tmp":
                continue
            stage_mounts.append(mount)
        if len(stage_mounts) != 1:
            raise Refusal("backend")
        mount = stage_mounts[0]
        if (
            mount.get("Type") != "bind"
            or mount.get("Source") != str(self.artifacts)
            or mount.get("Destination") != "/scratch"
            or mount.get("RW") is not (not offline)
        ):
            raise Refusal("backend")


def _source(*modules):
    """Embed shipped stdlib helpers; neither probe imports from a mounted project."""
    return "".join(
        f"{namespace} = {{'__name__': '_oc_box_preflight_{namespace}'}}\n"
        f"exec({source!r}, {namespace})\n"
        for namespace, source in modules
    )


def _scripts(box, version):
    package = Path(__file__).parent
    common = _source(
        ("artifact", FINGERPRINT_SOURCE),
        ("sdk", (package / "sync_config.py").read_text(encoding="utf-8")),
    )
    bridge = _source(
        ("bridge", (package / "opencode_v2_io.py").read_text(encoding="utf-8"))
    )
    overlay = box.container_build_config(version, home=_HOME)
    profile = {
        **box.container_build_config(version),
        "shell": "/bin/bash",
        "update": "disable",
        "plugins": [_HOME + "/.config/opencode/plugins/oc-box-build-permissions"],
        "formatter": True,
    }
    settings = json.dumps(
        {
            "home": _HOME,
            "project": _PROJECT,
            "binary": _EXE_PATH,
            "plugin": box.container_build_plugin(version),
            "overlay": overlay,
            "profile": profile,
        }
    )
    return common + _STAGE, common + bridge + _PROBE, settings


def _backend(root, reference):
    env = _private_env(root)
    podman = shutil.which("podman", path=env["PATH"])
    if podman is None:
        raise Refusal("backend")
    code, output = _checked(
        podman,
        env,
        ("info", "--format", "{{.Host.Security.Rootless}}"),
        category="backend",
    )
    if code or output != b"true\n":
        raise Refusal("backend")
    template = "{{.ID}}\n{{.Digest}}\n{{.Os}}\n{{.Architecture}}"
    code, output = _checked(
        podman,
        env,
        ("image", "inspect", "--format", template, reference),
        category="stage-image",
    )
    lines = output.splitlines()
    arch = b"amd64" if os.uname().machine == "x86_64" else b"arm64"
    digest = reference.split("@", 1)[1].encode("ascii")
    if code or len(lines) != 4 or lines[1:] != [digest, b"linux", arch]:
        raise Refusal("stage-image")
    image = _one_line(lines[0].removeprefix(b"sha256:") + b"\n", SHA256, "stage-image")
    return _Scratch(root, podman, env, image)


def check(box, version):
    """Return one SHA-256 after exact-artifact startup, activation and cleanup."""
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise Refusal("unsupported-version")
    if (
        sys.platform != "linux"
        or os.geteuid() == 0
        or os.environ.get("OC_BOX_CONTAINER") == "1"
        or Path("/run/.containerenv").exists()
        or Path("/.dockerenv").exists()
    ):
        raise Refusal("backend")
    reference = STAGE_IMAGES.get(os.uname().machine)
    if reference is None:
        raise Refusal("stage-image")
    scratch = None
    retain = False
    with _termination():
        try:
            root = Path(
                tempfile.mkdtemp(prefix="oc-box-native-preflight-", dir="/tmp")
            ).resolve()
        except (OSError, ValueError):
            raise Refusal("backend") from None
        try:
            with _logged_phase("stage", version):
                scratch = _backend(root, reference)
                stage, probe, settings = _scripts(box, version)
                proof = scratch.run("stage", stage, (version,), _STAGE_SECONDS)
            try:
                with _logged_phase("probe", version):
                    observed = scratch.run(
                        "probe", probe, (version, proof, settings), _PROBE_SECONDS
                    )
                    if observed != proof:
                        raise Refusal("artifact")
            except Refusal as error:
                if error.category in CONFIRMED_REFUSALS:
                    raise Refusal(
                        error.category, proof=proof, exit_code=error.exit_code
                    ) from None
                raise
            return proof
        except _Unreaped:
            retain = True
            raise Refusal("cleanup") from None
        except Refusal:
            raise
        except (OSError, ValueError):
            raise Refusal("backend") from None
        finally:
            with _logged_phase("cleanup", version):
                if retain or (scratch is not None and scratch.retain):
                    raise Refusal("cleanup") from None
                try:
                    shutil.rmtree(root)
                    if os.path.lexists(root):
                        raise OSError()
                except (OSError, KeyboardInterrupt):
                    raise Refusal("cleanup") from None
