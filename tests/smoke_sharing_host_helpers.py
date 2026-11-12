#!/usr/bin/env python3
"""Bounded native rehearsal of the sharing HOST fixture's provider and PTY helper.

Run from the checkout root:
  python3 -B tests/smoke_sharing_host_helpers.py --opencode /path/to/opencode-2.2.6

This is NOT host acceptance, Podman isolation or owner/client-lease acceptance.
Only ShareChild is imported from the host runner; its entrypoints and host guards
are neither called nor patched. A separately named test-only Box facade replaces
workspace discovery/backend/attachment with synthetic metadata and a nullcontext
around a private native endpoint. Production sharing.cli, Export, serve and the
WebSocket/pipe transport stay real. All data, native state and credentials are
synthetic; child PTY output and provider payloads remain in memory, never reports.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
from oc_box import opencode_compat as compat
from oc_box import opencode_v2_io as bridge
from smoke_session_sync_v2 import bounded, private_environment
from tools.sharing_fixture_provider import Provider
from tools.test_sharing_podman import ShareChild

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
IMAGE = "data:image/png;base64," + PNG
FAILURES = frozenset(
    (
        "fixture-timeout",
        "native-invoke-timeout",
        "native-version",
        "source-changed",
        "native-changed",
        "child-cleanup",
        "consent",
        "native-credentials",
        "native-client-mcp",
        "provider",
        "session-timeout",
        "native-completed-tool",
        "pending-permission",
        "session-permissions",
        "source-mutated",
        "configuration-mutated",
        "live-read",
        "live-list",
        "native-image",
        "provider-image",
        "share-refusal",
        "native-denial",
        "namespace-revocation",
        "native-reload",
        "unexpected",
    )
)

# This subprocess is intentionally NOT the host runner's --child implementation.
# It owns a real controlling PTY, and only its Box-shaped boundary is synthetic.
# No production module globals or host guards are monkeypatched. The private
# environment is built afresh, not derived from real credentials/service settings.
CHILD = r"""
import contextlib, fcntl, os, signal, sys, termios
from pathlib import Path
from types import SimpleNamespace

repo, source, project, address = sys.argv[1:]
sys.path.insert(0, str(Path(repo) / 'src'))
from oc_box import oc_box as box, opencode_compat as compat, sharing

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.alarm(180)
endpoint = {'url': address, 'password': os.environ['OPENCODE_PASSWORD']}
project = Path(project)
private = Path(os.environ['HOME']).parent
metadata = {'id': 'native-rehearsal-not-an-owner-lease',
            'project': str(project), 'opencode_version': '2.2.6'}

def native_rehearsal_local_client(facade, data, connection, arguments):
    if arguments[:2] != ['python3', '-c'] or len(arguments) != 6:
        raise box.BoxError('native-rehearsal-command')
    if arguments[3:5] != [endpoint['url'], str(project)]:
        raise box.BoxError('native-rehearsal-endpoint')
    command = [sys.executable, '-I', '-B', *arguments[1:]]
    environment = {key: value for key, value in os.environ.items()
                   if key in ('HOME', 'PATH', 'LANG', 'TMPDIR')
                   or key.startswith('XDG_')}
    environment['OPENCODE_PASSWORD'] = connection['password']
    if connection['password'] in command or source in command:
        raise box.BoxError('native-rehearsal-forwarding')
    return box.ContainerCommand(command, environment)

native_rehearsal_box_facade = SimpleNamespace(
    BoxError=box.BoxError, PACKAGE=box.PACKAGE,
    APP=private / 'native-rehearsal-app',
    state_root=lambda: private / 'native-rehearsal-state',
    vault=SimpleNamespace(vault_path=lambda: private / 'native-rehearsal-vault/data'),
    opencode_compat=compat,
    find_workspace=lambda project_selector, workspace_selector: (project, metadata),
    check_backend=lambda **kwargs: None,
    maintenance_host_env=lambda: {},
    container_project=lambda data: str(project),
    workspace_service=SimpleNamespace(
        attach=lambda *args: contextlib.nullcontext(endpoint),
        client_command=native_rehearsal_local_client,
    ),
)
try:
    status = sharing.cli(native_rehearsal_box_facade,
                         [source, '--workspace', metadata['id']])
except box.BoxError:
    print('Native rehearsal sharing refused.', file=sys.stderr, flush=True)
    status = 1
except BaseException:
    print('Native rehearsal child failed.', file=sys.stderr, flush=True)
    status = 1
sys.exit(status)
"""


class Failure(AssertionError):
    """Only a fixed category may reach the fixture terminal."""


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "FAIL: native sharing helpers (invalid-options).\n")


def require(condition, category):
    if not condition:
        raise Failure(category)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def module_hashes():
    names = (
        "tests/smoke_sharing_host_helpers.py",
        "tests/smoke_session_sync_v2.py",
        "tools/test_sharing_podman.py",
        "tools/sharing_fixture_provider.py",
        "src/oc_box/sharing.py",
        "src/oc_box/session_terminal.py",
        "src/oc_box/opencode_v2_io.py",
        "src/oc_box/oc_box.py",
        "src/oc_box/opencode_compat.py",
        "src/oc_box/workspace_service.py",
    )
    return {name: digest(REPO / name) for name in names}


def record_hashes(phase, values):
    for name, value in values.items():
        print(f"SOURCE {phase} sha256 {name}={value}", flush=True)


@contextlib.contextmanager
def time_limit(seconds, category):
    """Per-invocation alarm without extending the enclosing whole-fixture alarm."""
    previous = signal.getsignal(signal.SIGALRM)
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(*_):
        raise Failure(category)

    if not remaining or seconds < remaining:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if remaining:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, remaining - (time.monotonic() - started)),
                interval,
            )


def wait(check, category, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise Failure(category)


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def manifest(directory):
    """No atime claim, following links, opening FIFOs or reading special files."""
    result, pending = {}, [directory]
    while pending:
        path = pending.pop()
        info = path.lstat()
        item = [
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]
        if stat.S_ISREG(info.st_mode):
            item.append(digest(path))
        elif stat.S_ISLNK(info.st_mode):
            item.append(os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        result[str(path.relative_to(directory))] = item
    return result


class Rehearsal:
    def __init__(self, native, provider, env, project, source):
        self.native, self.provider, self.env = native, provider, env
        self.project, self.source, self.children = project, source, []

    def share(self, *, accept):
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            CHILD,
            str(REPO),
            str(self.source),
            str(self.project),
            self.native.endpoint["url"],
        ]
        require(self.native.endpoint["password"] not in command, "consent")
        child = ShareChild(
            command,
            {**self.env, "OPENCODE_PASSWORD": self.native.endpoint["password"]},
            self.project,
        )
        self.children.append(child)
        child.until(b"Type 'share'", timeout=30)
        require(
            child.name is None
            and b"SECURITY WARNING" in child.content
            and b"cannot recall" in child.content,
            "consent",
        )
        child.send(b"share\n" if accept else b"no\n")
        if accept:
            return child, child.until(ready=True, timeout=30)
        child.finish(expected=1, timeout=10)
        require(child.name is None, "consent")
        return child, None

    def invoke(self, code, *, permissions=None):
        """Actual native prompt -> execute -> provider.take; never an API tool mock."""
        with time_limit(30, "native-invoke-timeout"):
            before = manifest(self.source)
            token = self.provider.plan("execute", {"code": code})
            rules = (
                permissions
                if permissions is not None
                else [{"action": "*", "resource": "*", "effect": "allow"}]
            )
            ident = self.native(
                "POST",
                "/api/session",
                {
                    "location": {"directory": str(self.project)},
                    "title": "Synthetic native sharing helper rehearsal",
                    "agent": "build",
                    "model": {"providerID": "fixture", "id": "share-model"},
                    "permissions": rules,
                },
            )["data"]["id"]
            self.native("POST", f"/api/session/{ident}/prompt", {"text": token})
            captured = None

            def settled():
                nonlocal captured
                require(self.provider.errors == 0, "provider")
                require(
                    self.native("GET", f"/api/session/{ident}/permission")["data"]
                    == [],
                    "pending-permission",
                )
                messages = self.native(
                    "GET", f"/api/session/{ident}/message?limit=100&order=asc"
                )["data"]
                active = self.native("GET", "/api/session/active")["data"]
                if captured is None:
                    captured = self.provider.take(token)
                return (
                    messages
                    if (
                        captured is not None
                        and ident not in active
                        and any(
                            item.get("type") == "assistant"
                            and item.get("time", {}).get("completed")
                            for item in messages
                        )
                    )
                    else None
                )

            messages = wait(settled, "session-timeout")
            completed = [
                part
                for message in messages
                if message.get("type") == "assistant"
                for part in message.get("content", [])
                if part.get("type") == "tool"
                and part.get("name") == "execute"
                and part.get("state", {}).get("status") == "completed"
                and part.get("state", {}).get("input") == {"code": code}
            ]
            require(len(completed) == 1, "native-completed-tool")
            require(
                self.native("GET", f"/api/session/{ident}")["data"].get("permissions")
                == rules,
                "session-permissions",
            )
            require(manifest(self.source) == before, "source-mutated")
            require(
                isinstance(captured, dict)
                and isinstance(captured.get("text"), str)
                and len(captured.get("tool_results", [])) == 1,
                "provider",
            )
            require(self.provider.take(token) is None, "provider")
            return captured, messages, ident

    def call(self, name, method="read_file", path="text.txt", **kwargs):
        return self.invoke(
            f"return await tools.{name}.{method}({json.dumps({'path': path})})",
            **kwargs,
        )

    def read(self, name, expected, path="text.txt"):
        result, _, _ = self.call(name, path=path)
        require(result["text"] == expected, "live-read")

    def revoked(self, name):
        # Native namespace disposal follows WebSocket close asynchronously. Poll
        # through actual native prompts, rather than faking a detach or racing
        # the killed child's pipe EOF against the next model request.
        def absent():
            result, _, _ = self.call(name)
            return (
                f"Unknown tool '{name}.read_file'" in result["text"]
                and "synthetic-live" not in result["text"]
            )

        with time_limit(30, "namespace-revocation"):
            wait(absent, "namespace-revocation", timeout=10)

    def close(self):
        failed = False
        for child in reversed(self.children):
            try:
                child.close()
            except BaseException:
                failed = True
        require(not failed, "child-cleanup")

    def exercise(self):
        self.share(accept=False)
        shared, name = self.share(accept=True)
        self.read(name, "synthetic-live-before 中")
        listing, _, _ = self.call(name, "list_directory", ".")
        require(
            {item["name"] for item in json.loads(listing["text"])}
            == {item.name for item in self.source.iterdir()},
            "live-list",
        )
        self.read(name, "synthetic-hidden", ".hidden")
        (self.source / "new.txt").write_text("synthetic-new", encoding="utf-8")
        replacement = self.source.parent / "replacement.txt"
        replacement.write_text("synthetic-live-after 🙂", encoding="utf-8")
        replacement.replace(self.source / "text.txt")
        self.read(name, "synthetic-live-after 🙂")
        self.read(name, "synthetic-new", "new.txt")
        listing, _, _ = self.call(name, "list_directory", ".")
        require(
            "new.txt" in {item["name"] for item in json.loads(listing["text"])},
            "live-list",
        )
        result, messages, _ = self.call(name, "read_image", "pixel.bin")
        require(IMAGE in result.get("images", []), "provider-image")
        require(
            any(
                node.get("type") == "file" and node.get("mime") == "image/png"
                for node in nodes(messages)
            ),
            "native-image",
        )
        refusals = (
            ("../outside.txt", "Only confined relative share paths are accepted."),
            ("/etc/passwd", "Only confined relative share paths are accepted."),
            (
                "linked",
                "Share path is missing, inaccessible, linked or crosses a mount.",
            ),
            ("hard-a", "Special files and hard-linked files cannot be read."),
            ("fifo", "Special files and hard-linked files cannot be read."),
        )
        for path, reason in refusals:
            result, _, _ = self.call(name, path=path)
            require(
                reason in result["text"]
                and "OUTSIDE_NOT_DISCLOSED" not in result["text"],
                "share-refusal",
            )
        action = name + "_read_file"
        denied_rules = [
            {"action": "*", "resource": "*", "effect": "allow"},
            {"action": action, "resource": "*", "effect": "deny"},
        ]
        denied, _, ident = self.call(name, permissions=denied_rules)
        evaluated = self.native(
            "POST",
            f"/api/session/{ident}/permission",
            {"action": action, "resources": ["*"], "agent": "build"},
        )["data"]
        # A text containing "denied" is not evidence: require the actual native
        # evaluator's deny AND Code Mode's filtered/unknown tool result.
        require(
            evaluated.get("effect") == "deny"
            and f"Unknown tool '{name}.read_file'" in denied["text"]
            and "synthetic-live" not in denied["text"],
            "native-denial",
        )
        require(
            self.native("GET", f"/api/session/{ident}/permission")["data"] == [],
            "pending-permission",
        )
        self.read(name, "synthetic-live-after 🙂")
        shared.send(b"\x03")  # The PTY, not process.send_signal, delivers SIGINT.
        shared.finish(timeout=10)
        self.revoked(name)

        killed, killed_name = self.share(accept=True)
        self.read(killed_name, "synthetic-live-after 🙂")
        # Kill ONLY the serving test child. Its separately-sessioned production
        # transport must observe actual pipe EOF and remove its native namespace.
        os.kill(killed.process.pid, signal.SIGKILL)
        killed.finish(expected=-signal.SIGKILL, timeout=10)
        self.revoked(killed_name)

        reloaded, reloaded_name = self.share(accept=True)
        self.read(reloaded_name, "synthetic-live-after 🙂")
        require(self.native("POST", "/api/location/reload") is None, "native-reload")
        reloaded.finish(timeout=10)
        self.revoked(reloaded_name)


def exercise(binary, root):
    project, source = root / "project", root / "export"
    project.mkdir(mode=0o700)
    source.mkdir(mode=0o700)
    (source / "text.txt").write_text("synthetic-live-before 中", encoding="utf-8")
    (source / ".hidden").write_text("synthetic-hidden", encoding="utf-8")
    (source / "pixel.bin").write_bytes(base64.b64decode(PNG))
    (root / "outside.txt").write_text("OUTSIDE_NOT_DISCLOSED", encoding="utf-8")
    (source / "linked").symlink_to(root / "outside.txt")
    (source / "hard-a").write_text("synthetic-hardlink", encoding="utf-8")
    os.link(source / "hard-a", source / "hard-b")
    os.mkfifo(source / "fifo", 0o600)
    env = private_environment(root / "native")
    previous = Path.cwd()
    with Provider() as provider:
        env["LEMONADE_BASE_URL"] = f"http://127.0.0.1:{provider.port}"
        version = compat.version(bounded([binary, "--version"], env, project))
        require(version == "2.2.6", "native-version")
        print(
            "NATIVE version=" + version + "; private offline rehearsal only.",
            flush=True,
        )
        configuration = provider.config()
        # A COPY returned by the real provider, adapted only for this native-only
        # fixture. No host service, Podman gateway or production config is edited.
        configuration["providers"]["fixture"]["settings"]["baseURL"] = (
            f"http://127.0.0.1:{provider.port}/v1"
        )
        config = Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
        config.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        config.write_text(json.dumps(configuration), encoding="utf-8")
        config_before, outside_before = digest(config), digest(root / "outside.txt")
        try:
            os.chdir(project)
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(binary) as native,
            ):
                require(
                    native("GET", "/api/credential")["data"] == [], "native-credentials"
                )
                require(
                    native("GET", "/openapi.json")["paths"]
                    .get("/api/experimental/client-mcp", {})
                    .get("get", {})
                    .get("x-websocket")
                    is True,
                    "native-client-mcp",
                )
                rehearsal = Rehearsal(native, provider, env, project, source)
                try:
                    rehearsal.exercise()
                    require(provider.errors == 0 and provider.requests > 0, "provider")
                    require(digest(config) == config_before, "configuration-mutated")
                    require(
                        digest(root / "outside.txt") == outside_before, "source-mutated"
                    )
                finally:
                    rehearsal.close()
        finally:
            os.chdir(previous)


def main():
    parser = Arguments(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    args = parser.parse_args()
    old_umask = os.umask(0o077)
    previous = signal.getsignal(signal.SIGALRM)

    def expired(*_):
        raise Failure("fixture-timeout")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(240)
    initial, binary_before, binary = None, None, None
    failure = None
    try:
        initial = module_hashes()
        record_hashes("start", initial)
        binary = str(args.opencode.expanduser().resolve(strict=True))
        binary_before = digest(binary)
        print("NATIVE start sha256=" + binary_before, flush=True)
        parent = Path("/tmp/opencode")
        if not parent.is_dir():
            parent = Path(tempfile.gettempdir())
        with tempfile.TemporaryDirectory(
            prefix="oc-box-share-helpers-", dir=parent
        ) as temporary:
            exercise(binary, Path(temporary).resolve())
    except Failure as error:
        failure = str(error) if str(error) in FAILURES else "unexpected"
    except (Exception, KeyboardInterrupt):
        failure = "unexpected"
    finally:
        try:
            final = module_hashes()
            record_hashes("end", final)
            if initial is not None and final != initial:
                failure = failure or "source-changed"
            if binary_before is not None:
                binary_after = digest(binary)
                print("NATIVE end sha256=" + binary_after, flush=True)
                if binary_after != binary_before:
                    failure = failure or "native-changed"
        except (Exception, KeyboardInterrupt):
            failure = failure or "source-changed"
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        os.umask(old_umask)
    if failure:
        print("FAIL: native sharing helpers (" + failure + ").", file=sys.stderr)
        return 1
    print(
        "PASS: real ShareChild controlling PTY, typed consent/cancel and production sharing CLI/transport."
    )
    print(
        "PASS: real provider/native execute, live reads/new files, structured images, refusals and native denial."
    )
    print(
        "PASS: PTY Ctrl-C, killed-child pipe EOF and native reload remove sharing namespaces."
    )
    print(
        "Native rehearsal ONLY: no host/Podman isolation, owner leases or host acceptance established."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
