#!/usr/bin/env python3
"""Fresh-only HOST global-vault edge acceptance, without provider inference.

  python3 -B tools/test_global_vault_edges_podman.py \
      --work-dir /path/to/fixtures --opencode-v2 /path/to/opencode-2.2.6

Unprivileged HOST only, clean unchanged checkout, synthetic private state. No
resume/replay, real credentials/history, backup, automatic cleanup or interactive
UI claim. Public upstream catalog/package metadata traffic may occur; this is
not network containment. Native environment/credential selection is unmodified.
"""

import argparse
import ast
import errno
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
import secrets
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import venv
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(APP), str(APP / "src")]
from tools import test_global_vault_podman as fixture
from tools import test_interactive_podman as interactive

box, vault, credentials, service = (
    fixture.box,
    fixture.vault,
    fixture.credentials,
    fixture.workspace_service,
)
FORMAT = "oc-box-global-vault-edges-host-v2"
SNAPSHOT_SCHEMA = 2
HOME_MANIFEST_CONTRACT = "oc-box-home-topology-v2"
# Native 2.2.6 appends this exact host-side file even for --version. Its content
# digest is diagnostic, not protected state; every other native file still is.
NATIVE_LOG = "opencode/log/opencode.log"
OWNERS = (
    "populated-first-owner",
    "removed-new-owner",
    "initialized-empty-owner",
    "absent-first-owner",
)
FAULTS = ("wrong-passphrase", "corrupt-vault", "missing-crypto")
CASES = OWNERS + tuple(
    fault + "-" + action for fault in FAULTS for action in ("start", "reset")
)
PROMPT = b"Vault passphrase: "
CHILD = b"OC_BOX_EDGES_CHILD:"
EMPTY = b"Global vault is empty; no vault credentials will be forwarded."
ABSENT = b"Global vault is not initialized; using env-file forwarding for this launch."
REFUSALS = {
    "wrong-passphrase": b"Unable to unlock vault: incorrect passphrase or modified/damaged vault. No data was changed.",
    "corrupt-vault": b"Unsupported or damaged vault format; no data was changed.",
    "missing-crypto": b"Vault support needs the optional cryptography dependency.",
}
CHECKS = frozenset(
    (
        "launcher_zero",
        "native_shell",
        "live_snapshot_after_removal",
        "global_default",
        "host_environment_exact",
        "vault_cache_empty",
        "crypto_not_loaded",
        "vault_absent",
        "unlock_count",
        "guidance",
        "same_stopped_resources",
        "state_preserved",
        "ciphertext_preserved",
        "expected_refusal",
    )
)
CATEGORIES = frozenset(
    (
        "host-only",
        "invalid-options",
        "clean-source-required",
        "source-changed",
        "fresh-parent-required",
        "placement",
        "unsafe-path",
        "fixture-root",
        "fixture-format",
        "unsafe-metadata",
        "storage-config",
        "native-executable",
        "native-podman",
        "native-selector",
        "native-version",
        "native-changed",
        "podman-selector",
        "fixture-ids",
        "workspace-allowlist",
        "workspace-marker",
        "workspace-policy",
        "unrecorded-journal",
        "private-store",
        "rootless",
        "remote-engine",
        "storage-driver",
        "store-info-shape",
        "temporary-path",
        "image-temporary-path",
        "runtime-boot-current",
        "runtime-boot-marker",
        "runtime-boot-mismatch",
        "workspace-state",
        "resource-allowlist",
        "private-volume",
        "published-ports",
        "resource-mounts",
        "selected-session",
        "private-owner",
        "client-autoaccept",
        "terminal-contract",
        "terminal-output-bound",
        "terminal-oracle",
        "terminal-exit",
        "terminal-state",
        "native-context",
        "native-credentials",
        "native-messages",
        "native-log-secret",
        "oracle-contract",
        "host-environment-changed",
        "vault-cache-retained",
        "vault-created",
        "bundled-crypto",
        "parent-crypto-required",
        "crypto-free-environment",
        "private-venv",
        "child-timeout",
        "child-output-bound",
        "unexpected-prompt",
        "prompt-echo-enabled",
        "prompt-count",
        "passphrase-echoed",
        "child-protocol",
        "owner-refused",
        "unexpected-child-result",
        "refusal-category",
        "state-changed",
        "snapshot-contract",
        "ciphertext-changed",
        "report-schema",
        "fixture-only",
        "workspace-missing",
        "os-sentinel",
        "catalog-request",
        "catalog-shutdown",
        "fixture-refusal",
        "io-error",
        "subprocess-error",
        "interrupted",
        "unexpected-error",
    )
)


class CaseFailure(fixture.FixtureError):
    def __init__(self, primary, secondary=()):
        super().__init__(primary)
        self.secondary = list(secondary)


def require(value, category):
    fixture.require(value, category)


def category(error):
    # Never serialize raw exceptions, errors, argv, request bodies or environment.
    if isinstance(error, fixture.FixtureError):
        return str(error) if str(error) in CATEGORIES else "fixture-refusal"
    if isinstance(error, OSError):
        return "io-error"
    if isinstance(error, subprocess.SubprocessError):
        return "subprocess-error"
    return "interrupted" if isinstance(error, KeyboardInterrupt) else "unexpected-error"


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "global-vault edges: category=invalid-options\n")


def parser():
    value = ArgumentParser(description=__doc__)
    value.add_argument("--work-dir", type=Path, required=True)
    value.add_argument("--temp-dir", type=Path)
    value.add_argument("--opencode-v2", type=Path, required=True)
    value.add_argument("--distro", choices=tuple(box.DISTROS), default="tumbleweed")
    return value


def fresh_parent(path):
    parent = interactive.fresh_parent(path)
    for ancestor in (parent, *parent.parents):
        require(
            not any(
                (ancestor / name).exists() or (ancestor / name).is_symlink()
                for name in ("edges.json", "edges-result.json")
            ),
            "fresh-parent-required",
        )
    return parent


def write_report(root, case, checks, primary=None, secondary=(), *, exit_code=None):
    require(
        case in CASES
        and set(checks) <= CHECKS
        and all(type(value) is bool for value in checks.values()),
        "report-schema",
    )
    require(primary is None or primary in CATEGORIES, "report-schema")
    require(all(value in CATEGORIES for value in secondary), "report-schema")
    require(
        exit_code is None or (type(exit_code) is int and -255 <= exit_code <= 255),
        "report-schema",
    )
    fixture.write_json_new(
        root / ("edges-" + case + "-report.json"),
        {
            "format": FORMAT,
            "snapshot_schema": SNAPSHOT_SCHEMA,
            "home_manifest_contract": HOME_MANIFEST_CONTRACT,
            "case": case,
            "status": "PASS" if primary is None and not secondary else "FAIL",
            "checks": checks,
            "primary_category": primary,
            "child_exit_code": exit_code,
            "secondary_categories": list(secondary),
        },
    )


def finish_case(root, case, checks, primary, secondary=(), *, exit_code=None):
    secondary = list(secondary)
    try:
        write_report(root, case, checks, primary, secondary, exit_code=exit_code)
    except BaseException as error:
        secondary.append(category(error))
    if primary is not None or secondary:
        raise CaseFailure(primary or secondary[0], secondary)


def validate_edges(root, *, allow_unrecorded=False):
    root = fixture.validate_fixture(root, allow_unrecorded=allow_unrecorded)
    meta = fixture.private_json(root, "edges.json")
    require(
        meta.get("format") == FORMAT
        and type(meta.get("snapshot_schema")) is int
        and meta.get("snapshot_schema") == SNAPSHOT_SCHEMA
        and meta.get("home_manifest_contract") == HOME_MANIFEST_CONTRACT
        and meta.get("cases") == list(CASES),
        "fixture-only",
    )
    require(
        not any(
            (root / name).exists()
            for name in ("result.json", "interactive.json", "seed-baseline.json")
        ),
        "fixture-only",
    )
    records = fixture.workspace_records(root)
    require(set(records) <= {"source-a", "source-b"}, "workspace-allowlist")
    require(
        all("credentials" not in data for _, data in records.values()),
        "workspace-policy",
    )
    native = meta["native"]
    binary = interactive.native_binary(Path(native["path"]))
    require(
        native["version"] == fixture.NATIVE_VERSION
        and interactive.file_hash(binary) == native["sha256"],
        "native-changed",
    )
    require(
        fixture.private_json(root, "executables.json") == {"2": str(binary)},
        "native-selector",
    )
    wrapper = fixture.safe_path(root, "bin/opencode")
    require(
        wrapper.read_text() == fixture.native_wrapper_source(native["interpreter"])
        and os.access(wrapper, os.X_OK),
        "native-selector",
    )
    require(
        fixture.private_json(root, "config/oc-box/config.json")
        == {
            "default_distro": meta["distro"],
            "pass_env": [fixture.PASS_ENV],
            "auto_update": False,
        },
        "fixture-only",
    )
    require(
        fixture.private_json(root, "config/opencode/opencode.json")
        == provider_free_config(),
        "fixture-only",
    )
    interactive.require_same_source(meta["source"])
    return root


def require_environment(root):
    expected = {
        "HOME": "host-home",
        "XDG_CONFIG_HOME": "config",
        "XDG_DATA_HOME": "data",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "OC_BOX_STATE_HOME": "registry",
        "CONTAINERS_STORAGE_CONF": "config/containers/storage.conf",
        "CONTAINERS_CONF": "config/containers/containers.conf",
    }
    require(
        all(
            os.environ.get(key) == str(root / relative)
            for key, relative in expected.items()
        )
        and os.environ.get("PATH", "").split(":")[0] == str(root / "bin")
        and not any(
            name in os.environ
            for name in (
                "CONTAINERS_CONF_OVERRIDE",
                "CONTAINER_HOST",
                "CONTAINER_CONNECTION",
                "DOCKER_HOST",
            )
        ),
        "fixture-only",
    )


def checkpoint(root, *, allow_unrecorded=False):
    require_environment(root)
    validate_edges(root, allow_unrecorded=allow_unrecorded)
    # File-only original-boot proof before any later retained engine query.
    fixture.require_runtime_boot(
        root, allow_uninitialized=not fixture.workspace_records(root)
    )
    fixture.validate_store(root, allow_unrecorded=allow_unrecorded)


def provider_free_config():
    return {
        "$schema": "https://opencode.ai/config.json",
        "snapshots": False,
        "formatter": False,
        "update": "disable",
    }


def empty_catalog():
    """Only isolate optional Lemonade discovery, never emulate inference."""
    state = {"requests": 0, "unexpected": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state["requests"] += 1
            accepted = (
                self.path
                in (
                    "/api/v1/models",
                    "/api/v1/models/",
                    "/api/v1/health",
                    "/api/v1/version",
                )
                and state["requests"] <= 32
            )
            if not accepted:
                state["unexpected"] += 1
            content = b'{"data":[],"models":[]}'
            self.send_response(200 if accepted else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            state["unexpected"] += 1
            self.send_response(503)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker, state


def hashes(values):
    # Separate zero-entry oracle. Do not weaken the full helper's KEY/provider/MCP contract.
    require(set(values) <= set(fixture.KNOWN), "oracle-contract")
    return {
        name: hashlib.sha256(values[name].encode()).hexdigest()
        if name in values
        else None
        for name in fixture.KNOWN
    }


def terminal_source():
    source = (box.PACKAGE / "session_terminal.py").read_text()
    tree = ast.parse(source)
    choose = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "choose_terminal"
    )
    posts = [
        node
        for node in ast.walk(choose)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "request"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "POST"
    ]
    require(
        len(posts) == 1
        and len(posts[0].args) == 3
        and isinstance(posts[0].args[2], ast.Dict),
        "terminal-contract",
    )
    body = posts[0].args[2]
    fields = {
        key.value: value
        for key, value in zip(body.keys, body.values)
        if isinstance(key, ast.Constant)
    }
    require(
        isinstance(fields.get("env"), ast.Dict) and not fields["env"].keys,
        "terminal-contract",
    )
    return source


def shell_oracle(expected, data, pid, salt):
    require(
        set(expected) == set(fixture.KNOWN) and type(pid) is int and pid > 0,
        "oracle-contract",
    )
    script = "\n".join(
        [
            "import hashlib,json,os,pathlib",
            "names=" + repr(list(fixture.KNOWN)),
            "expected=" + repr(expected),
            "def dig(env): return {n:hashlib.sha256(env[n].encode()).hexdigest() if n in env else None for n in names}",
            "actual=dig(os.environ)",
            "server=dict(x.split('=',1) for x in pathlib.Path('/proc/"
            + str(pid)
            + "/environ').read_bytes().decode().split('\\0') if '=' in x)",
            "checks={'values':actual==expected,'server_values':dig(server)==expected,'cwd':os.getcwd()=="
            + repr(box.container_project(data))
            + ",'home':os.environ.get('HOME')=="
            + repr(data["home"])
            + ",'control_absent':all(n not in os.environ for n in ('OPENCODE_PASSWORD','OPENCODE_SERVER_PASSWORD'))}",
            "assert all(checks.values())",
            "print('EDGE:'+hashlib.sha256(("
            + repr(salt)
            + "+json.dumps({'values':actual,'checks':checks},sort_keys=True,separators=(',',':'))).encode()).hexdigest(),flush=True)",
        ]
    )
    checks = dict.fromkeys(
        ("values", "server_values", "cwd", "home", "control_absent"), True
    )
    content = salt + json.dumps(
        {"values": expected, "checks": checks}, sort_keys=True, separators=(",", ":")
    )
    marker = ("EDGE:" + hashlib.sha256(content.encode()).hexdigest()).encode()
    submitted = ("python3 -I -B -c " + shlex.quote(script) + "\r").encode()
    require(marker not in submitted, "oracle-contract")
    return submitted, marker


def marker_seen(output, marker):
    return (
        re.search(rb"(?:^|\r?\n)" + re.escape(marker) + rb"\r?\n", output) is not None
    )


def stop_child(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def reap_child(process, primary):
    try:
        stop_child(process)
    except BaseException as error:
        if primary is not None:
            raise CaseFailure(
                category(primary), [*getattr(primary, "secondary", ()), category(error)]
            ) from None
        raise


def terminal_oracle(root, data, connection, ident, expected):
    request = fixture.request_for(data, connection)
    first = request("GET", "/api/info")
    require(
        first.get("version") == fixture.NATIVE_VERSION
        and type(first.get("pid")) is int,
        "native-context",
    )
    target = "/api/experimental/session/" + ident + "/terminal"
    require(
        not any(
            item.get("status") == "running" for item in request("GET", target)["data"]
        ),
        "terminal-state",
    )
    # Exact unmodified production client; env:{} source contract is additionally
    # exercised by offline choose_terminal tests, not a substituted host client.
    command = service.client_command(
        box,
        data,
        connection,
        [
            "python3",
            "-B",
            "-c",
            terminal_source(),
            connection["url"],
            ident,
            box.container_project(data),
        ],
        interactive=True,
    )
    require(
        all(name not in command.environment for name in fixture.KNOWN),
        "terminal-contract",
    )
    submitted, marker = shell_oracle(
        expected, data, first["pid"], secrets.token_hex(16)
    )
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 240, 0, 0))
    process = None
    matched = sent = False
    output = bytearray()
    try:
        process = subprocess.Popen(
            command,
            env=command.environment,
            cwd=root,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
            require(len(output) < 1048576, "terminal-output-bound")
            if output and not sent:
                os.write(master, submitted)
                sent = True
            if not matched and marker_seen(output, marker):
                matched = True
                require(
                    marker.decode()
                    in request("GET", target + "/read")["data"]["screen"]["text"],
                    "terminal-oracle",
                )
                os.write(master, b"exit\r")
            if process.poll() is not None:
                break
        require(matched, "terminal-oracle")
        process.wait(timeout=10)
        require(process.returncode == 0, "terminal-exit")
    finally:
        if slave is not None:
            os.close(slave)
        try:
            if process is not None:
                reap_child(process, sys.exc_info()[1])
        finally:
            os.close(master)
    require(
        not any(
            item.get("status") == "running" for item in request("GET", target)["data"]
        ),
        "terminal-state",
    )
    require(request("GET", "/api/info")["pid"] == first["pid"], "native-context")
    require(
        request("GET", "/api/experimental/session/" + ident + "/export")["data"][
            "messages"
        ]
        == [],
        "native-messages",
    )
    return first["pid"]


def ambient_values(*, auth=True):
    values = {
        fixture.KEY: "synthetic-removed-key-decoy",
        fixture.AMBIENT: "synthetic-suffix-fallback-control",
        fixture.PASS_ENV: "synthetic-pass-env-fallback-control",
    }
    if auth:
        values["OPENCODE_AUTH_CONTENT"] = json.dumps(
            {"openai": {"type": "api", "key": "synthetic-never-used-openai-key"}}
        )
    return values


def assert_host_scope(expected):
    # BEFORE ambient restoration, which must not conceal an inserted vault entry.
    require(dict(os.environ) == expected, "host-environment-changed")
    require(not credentials.has_unlocked_values(), "vault-cache-retained")


def install_cipher(root, name):
    require_environment(root)
    require(name in ("populated", "removed", "empty", "corrupt"), "fixture-only")
    content = fixture.safe_path(root, "ciphertexts/" + name + ".json").read_bytes()
    target = vault.vault_path()
    with vault.locked(target, create=True):
        descriptor, temporary = tempfile.mkstemp(
            prefix=".edge-cipher-", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def verify_crypto_absent():
    import importlib.util

    require(
        sys.flags.isolated == 1
        and sys.prefix != sys.base_prefix
        and importlib.util.find_spec("pip") is None,
        "crypto-free-environment",
    )
    require(
        not (Path(vault.__file__).resolve().parent / "vault-deps").exists(),
        "bundled-crypto",
    )
    require(importlib.util.find_spec("cryptography") is None, "crypto-free-environment")
    try:
        __import__("cryptography")
    except ImportError:
        pass
    else:
        raise fixture.FixtureError("crypto-free-environment")
    require(
        not any(
            name == "cryptography" or name.startswith("cryptography.")
            for name in sys.modules
        ),
        "crypto-free-environment",
    )


def create_crypto_free_python(root):
    require(not (box.PACKAGE / "vault-deps").exists(), "bundled-crypto")
    directory = root / "crypto-free-venv"
    require(not directory.exists() and not directory.is_symlink(), "private-venv")
    # A copied uv standalone 3.10 binary cannot locate its base stdlib. Standard
    # Linux venv symlinks retain that trusted base without enabling system site.
    venv.EnvBuilder(with_pip=False, system_site_packages=False, symlinks=True).create(
        directory
    )
    require(
        "include-system-site-packages = false"
        in (directory / "pyvenv.cfg").read_text().lower(),
        "private-venv",
    )
    python = directory / "bin/python"
    bootstrap = "import sys; sys.path[:0]=[sys.argv[1],sys.argv[1]+'/src']; from tools.test_global_vault_edges_podman import verify_crypto_absent; verify_crypto_absent()"
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", bootstrap, str(APP)],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    require(
        result.returncode == 0 and not result.stdout and not result.stderr,
        "crypto-free-environment",
    )
    return python


def owner_worker(root, case):
    fixture.host_guard()
    require(case in OWNERS, "fixture-only")
    checkpoint(root)
    absent = case == "absent-first-owner"
    key = dict(zip(OWNERS, ("populated", "removed", "empty", "absent")))[case]
    expected = fixture.private_json(root, "oracle-hashes.json")[key]
    if absent:
        verify_crypto_absent()
        require(not vault.vault_path().exists(), "vault-created")
    checks, primary, secondary = {}, None, []
    baseline = dict(os.environ)
    scoped = {**baseline, **ambient_values(auth=not absent)}
    name = "source-b" if absent else "source-a"

    def owning_tui(command):
        require("--auto" not in command, "client-autoaccept")
        checkpoint(root, allow_unrecorded=True)
        fixture.record_ids(root)
        path, data = fixture.workspace(root, name)
        require("credentials" not in data, "workspace-policy")
        fixture.verify_owned_resources(root, data)
        connection = service.live_connection()
        require(connection is not None, "private-owner")
        ident = fixture.selected_session(command, path)
        request = fixture.request_for(data, connection)
        fixture.validate_selected_source_session(
            request("GET", "/api/session/" + ident)["data"], ident, data
        )
        if not absent:
            require(
                request("GET", "/api/credential")["data"] == [], "native-credentials"
            )
        owner_pid = terminal_oracle(root, data, connection, ident, expected)
        checks["native_shell"] = checks["global_default"] = True
        if case == "populated-first-owner":
            install_cipher(root, "removed")
            require(
                terminal_oracle(root, data, connection, ident, expected) == owner_pid,
                "native-context",
            )
            checks["live_snapshot_after_removal"] = True
            # Sentinel SETUP only. Never use podman exec to observe credential env.
            script = (
                "import os,pathlib; pathlib.Path(os.environ['HOME'],'acceptance-home.txt').write_text('HOME_RETAINED\\n'); pathlib.Path("
                + repr(fixture.OS_FILE)
                + ").write_text('OS_RETAINED\\n')"
            )
            box.run(
                box.exec_command(data, ["python3", "-c", script]),
                input="",
                capture=True,
                private=True,
                timeout=30,
            )
        if not absent:
            require(
                request("GET", "/api/credential")["data"] == [], "native-credentials"
            )
        return 0

    with patch.dict(os.environ, scoped, clear=True):
        try:
            # The only production function replacement: scripted TUI callback.
            with patch.object(box, "run_tui", side_effect=owning_tui):
                require(
                    box.main(["start", "--project", str(root / name)]) == 0,
                    "owner-refused",
                )
                checks["launcher_zero"] = True
        except BaseException as error:
            primary = category(error)
            if isinstance(error, CaseFailure):
                secondary.extend(error.secondary)
        try:
            assert_host_scope(scoped)
            checks["host_environment_exact"] = checks["vault_cache_empty"] = True
        except BaseException as error:
            secondary.append(category(error))
    try:
        assert_host_scope(baseline)
        if absent:
            verify_crypto_absent()
            require(not vault.vault_path().exists(), "vault-created")
            checks["crypto_not_loaded"] = checks["vault_absent"] = True
    except BaseException as error:
        secondary.append(category(error))
    return {"checks": checks, "primary": primary, "secondary": secondary}


TTY_BOOTSTRAP = "import fcntl,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0)\n"
OWNER_BOOTSTRAP = (
    TTY_BOOTSTRAP
    + """import json,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/src']
from tools import test_global_vault_edges_podman as edge
try:
    value=edge.owner_worker(Path(sys.argv[2]),sys.argv[3])
except BaseException as error:
    value={'checks':{},'primary':edge.category(error),'secondary':[]}
print('OC_BOX_EDGES_CHILD:'+json.dumps(value,sort_keys=True),flush=True)
raise SystemExit(1 if value['primary'] or value['secondary'] else 0)
"""
)
# No fixture import, function replacement or exception substitution in failures.
FAILURE_BOOTSTRAP = (
    TTY_BOOTSTRAP
    + """import importlib.util,json,pathlib,sys
sys.path.insert(0,sys.argv[1]+'/src')
missing=sys.argv[2]=='missing-crypto'
if missing:
    assert sys.flags.isolated==1 and sys.prefix!=sys.base_prefix
    assert importlib.util.find_spec('pip') is None
    assert importlib.util.find_spec('cryptography') is None
    try:
        import cryptography
    except ImportError:
        pass
    else:
        raise SystemExit(96)
from oc_box import oc_box as box,vault
assert not missing or not (pathlib.Path(vault.__file__).resolve().parent/'vault-deps').exists()
sys.argv=sys.argv[3:]
code=box.entrypoint()
print('OC_BOX_EDGES_CHILD:'+json.dumps({'exit_code':code,'genuine_missing_crypto':missing},sort_keys=True),flush=True)
raise SystemExit(code)
"""
)


def prompt_ready(master, output, sent, password):
    # The real editor redraws its label and ends with [accepted]. Count entries,
    # not occurrences of the label. Never send input with kernel echo enabled.
    count = len(re.findall(re.escape(PROMPT) + rb" *\[0 chars, masked\]", output))
    require(count <= 1, "prompt-count")
    require(password is not None or PROMPT not in output, "unexpected-prompt")
    if count and not sent:
        require(password is not None, "unexpected-prompt")
        require(not termios.tcgetattr(master)[3] & termios.ECHO, "prompt-echo-enabled")
        return True
    return False


def drive_child(command, *, environment, password=None, timeout=180):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
    process, sent = None, False
    output = bytearray()
    try:
        process = subprocess.Popen(
            command,
            env=environment,
            cwd=APP,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        slave = None
        deadline = time.monotonic() + timeout
        while True:
            require(time.monotonic() < deadline, "child-timeout")
            ready = select.select([master], [], [], 0.1)[0]
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
            require(len(output) < 4 * 1048576, "child-output-bound")
            if prompt_ready(master, output, sent, password):
                os.write(master, password.encode() + b"\n")
                sent = True
            if process.poll() is not None and not ready:
                break
        process.wait(timeout=10)
        require(
            password is None or password.encode() not in output, "passphrase-echoed"
        )
        require(output.count(PROMPT + b"[accepted]") == int(sent), "prompt-count")
        records = re.findall(rb"(?:^|\r?\n)" + CHILD + rb"([^\r\n]+)\r?\n", output)
        require(len(records) == 1, "child-protocol")
        value = json.loads(records[0])
        require(isinstance(value, dict), "child-protocol")
        return {
            "exit_code": process.returncode,
            "value": value,
            "prompts": int(sent),
            "empty": EMPTY in output,
            "absent": ABSENT in output,
            "refusals": [name for name, text in REFUSALS.items() if text in output],
        }
    finally:
        if slave is not None:
            os.close(slave)
        try:
            if process is not None:
                reap_child(process, sys.exc_info()[1])
        finally:
            os.close(master)


def owner_case(root, case, password, python):
    require(case in OWNERS, "fixture-only")
    checks, primary, secondary = {}, None, []
    exit_code = None
    try:
        checkpoint(root)
        result = drive_child(
            [str(python), "-I", "-B", "-c", OWNER_BOOTSTRAP, str(APP), str(root), case],
            environment=dict(os.environ),
            password=password,
            timeout=1200,
        )
        exit_code = result["exit_code"]
        value = result["value"]
        require(set(value) == {"checks", "primary", "secondary"}, "child-protocol")
        require(
            value["primary"] is None or value["primary"] in CATEGORIES, "child-protocol"
        )
        require(
            isinstance(value["secondary"], list)
            and all(item in CATEGORIES for item in value["secondary"]),
            "child-protocol",
        )
        if value["primary"] is not None or value["secondary"]:
            raise CaseFailure(
                value["primary"] or value["secondary"][0], value["secondary"]
            )
        required = {
            "launcher_zero",
            "native_shell",
            "global_default",
            "host_environment_exact",
            "vault_cache_empty",
        }
        if case == OWNERS[0]:
            required.add("live_snapshot_after_removal")
        if case == OWNERS[3]:
            required.update(("crypto_not_loaded", "vault_absent"))
        require(
            isinstance(value["checks"], dict)
            and set(value["checks"]) == required
            and all(item is True for item in value["checks"].values())
            and result["exit_code"] == 0,
            "child-protocol",
        )
        checks.update(value["checks"])
        require(result["prompts"] == int(case != OWNERS[3]), "prompt-count")
        checks["unlock_count"] = True
        require(
            result["empty"] == (case == OWNERS[2])
            and result["absent"] == (case == OWNERS[3]),
            "unexpected-child-result",
        )
        checks["guidance"] = True
        checkpoint(root, allow_unrecorded=True)
        fixture.record_ids(root)
        fixture.verify_owned_resources(
            root,
            fixture.workspace(root, "source-b" if case == OWNERS[3] else "source-a")[1],
            stopped=True,
        )
    except BaseException as error:
        primary = category(error)
        if isinstance(error, CaseFailure):
            secondary.extend(error.secondary)
    try:
        fixture.record_ids(
            root
        )  # File-only, including production-created IDs on failure.
    except BaseException as error:
        secondary.append(category(error))
    finish_case(root, case, checks, primary, secondary, exit_code=exit_code)


# Exact stopped-home contract, including root '.': lstat kind/mode/UID/GID/nlink
# for every entry, file size + complete SHA-256, symlink target, and canonical
# first relative path for each non-directory hardlink group. Device/inode values
# identify groups only; they are not serialized. Symlinks are never traversed.
# As before, times (including mtime), ACLs and xattrs are NOT proof fields: a
# timestamp-only change does not waive content or any ownership/topology check.
HOME_MANIFEST = """import hashlib,json,os,pathlib,stat,sys
root=pathlib.Path(sys.argv[1]); result={}; heads={}
def scan(path,relative):
    item=path.lstat()
    common=[stat.S_IMODE(item.st_mode),item.st_uid,item.st_gid,item.st_nlink]
    head=heads.setdefault((item.st_dev,item.st_ino),relative)
    if stat.S_ISLNK(item.st_mode):
        result[relative]=['link',*common,os.readlink(path),head]
    elif stat.S_ISREG(item.st_mode):
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for content in iter(lambda:stream.read(1048576),b''): digest.update(content)
        result[relative]=['file',*common,item.st_size,digest.hexdigest(),head]
    elif stat.S_ISDIR(item.st_mode):
        result[relative]=['directory',*common]
        for child in sorted(path.iterdir()):
            scan(child,child.name if relative=='.' else relative+'/'+child.name)
    else:
        result[relative]=['special',stat.S_IFMT(item.st_mode),*common,item.st_rdev,head]
if not stat.S_ISDIR(root.lstat().st_mode): raise ValueError('home-root')
scan(root,'.')
payload={'contract':'oc-box-home-topology-v2','entries':result}
print(hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest())
"""


def protected_snapshot(snapshot, *, legacy_inspection=False):
    """Pure projection: waive ONLY the native 2.2.6 --version log digest.

    Original before/after snapshots retain that digest. Other log files, native
    DB/config/auth/cache, and the complete container-home manifest remain exact.
    Legacy projection is explicit read-only inspection, never acceptance: it
    does not add schema/contract fields or rewrite the retained evidence.
    """
    require(
        isinstance(snapshot, dict)
        and (
            legacy_inspection
            or (
                type(snapshot.get("snapshot_schema")) is int
                and snapshot.get("snapshot_schema") == SNAPSHOT_SCHEMA
                and snapshot.get("home_manifest_contract") == HOME_MANIFEST_CONTRACT
            )
        ),
        "snapshot-contract",
    )
    result = dict(snapshot)
    if "native" in snapshot:
        native = {name: dict(files) for name, files in snapshot["native"].items()}
        if "data" in native:
            native["data"].pop(NATIVE_LOG, None)
        result["native"] = native
    return result


def native_log_digest(snapshot):
    return snapshot.get("native", {}).get("data", {}).get(NATIVE_LOG)


def assert_native_log_clean(root, forbidden):
    """Read-only streaming leak check; never persist log text or search values."""
    needles = tuple(value.encode("utf-8") for value in forbidden)
    require(all(needles), "oracle-contract")
    path = fixture.safe_path(root, "host-data-2/" + NATIVE_LOG, optional=True)
    if not path.exists():
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    overlap = max((len(value) for value in needles), default=1) - 1
    tail = b""
    with os.fdopen(descriptor, "rb") as stream:
        for content in iter(lambda: stream.read(1048576), b""):
            content = tail + content
            require(not any(value in content for value in needles), "native-log-secret")
            tail = content[-overlap:] if overlap else b""


def stopped_snapshot(root, *, forbidden=()):
    checkpoint(root)
    assert_native_log_clean(root, forbidden)
    records = fixture.workspace_records(root)
    path, data = records["source-a"]
    source = fixture.snapshot_source(
        root, path, data
    )  # Unchanged stopped DB/OS/project/registry guard.
    resources, homes = {}, {}
    for name, (_, item) in records.items():
        info = fixture.verify_owned_resources(root, item, stopped=True)
        resources[name] = {
            "container_id": info["Id"],
            "volume": item["home_volume"],
            "started_at": info["State"].get("StartedAt"),
            "finished_at": info["State"].get("FinishedAt"),
        }
        directory = (
            root / "data/containers/storage/volumes" / item["home_volume"] / "_data"
        )
        result = box.run(
            [
                "podman",
                "unshare",
                str(Path(sys.executable).resolve()),
                "-I",
                "-B",
                "-c",
                HOME_MANIFEST,
                str(directory),
            ],
            stdin=subprocess.DEVNULL,
            capture=True,
            private=True,
            timeout=60,
        )
        digest = result.stdout.strip()
        require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "state-changed")
        homes[name] = digest
    return {
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "home_manifest_contract": HOME_MANIFEST_CONTRACT,
        "source": source,
        "resources": resources,
        "homes": homes,
        "registry": fixture.tree_digest(root / "registry"),
        "host_home": fixture.tree_digest(root / "host-home"),
        "native": {
            name: fixture.tree_digest(root / ("host-" + name + "-2"))
            for name in ("config", "data", "state", "cache")
        },
        "host_config": fixture.tree_digest(root / "config/opencode"),
        "ciphertext": interactive.file_hash(vault.vault_path()),
    }


def failure_case(root, fault, action, password, python, *, forbidden=()):
    require(fault in FAULTS and action in ("start", "reset"), "fixture-only")
    case = fault + "-" + action
    checks, primary, secondary, before = {}, None, [], None
    exit_code = None
    try:
        checkpoint(root)
        install_cipher(root, "corrupt" if fault == "corrupt-vault" else "empty")
        # A case owns an immutable copy even when later cases use the same
        # encrypted empty payload. Only ciphertext is ever staged here.
        source = fixture.safe_path(root, vault.vault_path().relative_to(root))
        descriptor = os.open(
            root / ("edges-" + case + "-ciphertext.json"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source.read_bytes())
        # Snapshot AFTER the fault. Never roll back unexpected starts/deletion.
        candidate = stopped_snapshot(root, forbidden=forbidden)
        protected_snapshot(candidate)  # Legacy inspection is never a new baseline.
        before = candidate
        fixture.write_json_new(root / ("edges-" + case + "-baseline.json"), before)
        ident = fixture.workspace(root, "source-a")[1]["id"]
        result = drive_child(
            [
                str(python),
                "-I",
                "-B",
                "-c",
                FAILURE_BOOTSTRAP,
                str(APP),
                fault,
                "oc-box",
                action,
                "--workspace",
                ident,
            ],
            environment=dict(os.environ),
            password=password if fault == "wrong-passphrase" else None,
        )
        exit_code = result["exit_code"]
        require(
            result["value"]
            == {"exit_code": 1, "genuine_missing_crypto": fault == "missing-crypto"}
            and result["exit_code"] == 1,
            "unexpected-child-result",
        )
        require(result["refusals"] == [fault], "refusal-category")
        require(result["prompts"] == int(fault == "wrong-passphrase"), "prompt-count")
        checks["expected_refusal"] = checks["unlock_count"] = True
    except BaseException as error:
        primary = category(error)
        if isinstance(error, CaseFailure):
            secondary.extend(error.secondary)
    if before is not None:
        try:
            after = stopped_snapshot(root, forbidden=forbidden)
            fixture.write_json_new(root / ("edges-" + case + "-after.json"), after)
            fixture.write_json_new(
                root / ("edges-" + case + "-native-log.json"),
                {
                    "before_sha256": native_log_digest(before),
                    "after_sha256": native_log_digest(after),
                },
            )
            require(before["ciphertext"] == after["ciphertext"], "ciphertext-changed")
            checks["ciphertext_preserved"] = True
            require(
                protected_snapshot(before) == protected_snapshot(after), "state-changed"
            )
            checks["same_stopped_resources"] = checks["state_preserved"] = True
        except BaseException as error:
            if primary is None:
                primary = category(error)
            else:
                secondary.append(category(error))
    finish_case(root, case, checks, primary, secondary, exit_code=exit_code)


def prepare_ciphertexts(root, password):
    directory = root / "ciphertexts"
    directory.mkdir(mode=0o700)
    values = {
        fixture.KEY: "synthetic-" + secrets.token_hex(24),
        fixture.SIDE: "synthetic-" + secrets.token_hex(24),
    }
    expected = {}
    for name, entries in (
        ("populated", values),
        ("removed", {fixture.SIDE: values[fixture.SIDE]}),
        ("empty", {}),
    ):
        target = directory / (name + ".json")
        require(not target.exists(), "fixture-only")
        vault.write_encrypted(target, entries, password)
        expected[name] = hashes(entries)
    with (directory / "corrupt.json").open("xb") as stream:
        stream.write(b'{"format":"synthetic-corrupt-edge-envelope"}\n')
    expected["absent"] = hashes(ambient_values(auth=False))
    fixture.write_json_new(root / "oracle-hashes.json", expected)
    return tuple(values.values())


def run(root, binary):
    fixture.host_guard()
    require_environment(root)
    try:
        vault.crypto()  # Parent creates valid ciphertext; only children lack crypto.
    except vault.VaultError:
        raise fixture.FixtureError("parent-crypto-required") from None
    fixture.validate_store(root)  # FIRST engine query; no resources before proof.
    fixture.prepare_native_cli(root, binary)
    checkpoint(root)
    without_crypto = create_crypto_free_python(root)
    password, wrong = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    # Kept only in this parent's memory for the exact diagnostic-log leak guard.
    forbidden = (*prepare_ciphertexts(root, password), password, wrong)
    install_cipher(root, "populated")
    owner_case(root, OWNERS[0], password, sys.executable)
    # First callback encrypted the removed state, without changing its live env.
    owner_case(root, OWNERS[1], password, sys.executable)
    checkpoint(root)
    install_cipher(root, "empty")
    owner_case(root, OWNERS[2], password, sys.executable)
    checkpoint(root)
    vault.vault_path().unlink()  # Original initialized ciphertext is retained separately.
    owner_case(root, OWNERS[3], None, without_crypto)
    require(not vault.vault_path().exists(), "vault-created")
    for fault in FAULTS:
        for action in ("start", "reset"):
            failure_case(
                root,
                fault,
                action,
                wrong,
                without_crypto if fault == "missing-crypto" else sys.executable,
                forbidden=forbidden,
            )
    require(
        not credentials.has_unlocked_values()
        and all(name not in os.environ for name in fixture.KNOWN),
        "vault-cache-retained",
    )


def main(argv=None):
    args = parser().parse_args(argv)
    root = source = catalog = worker = state = None
    catalog_counts = None
    primary, secondary = None, []
    old_umask = os.umask(0o077)
    try:
        fixture.host_guard()  # Before placement, native/source probes or writes.
        parent = fresh_parent(args.work_dir)
        temporary = fresh_parent(args.temp_dir) if args.temp_dir is not None else None
        source = interactive.source_identity()
        binary = interactive.native_binary(args.opencode_v2)
        podman = shutil.which(
            "podman", path=box.maintenance_host_env().get("PATH", os.defpath)
        )
        require(podman is not None, "native-podman")
        root = Path(tempfile.mkdtemp(prefix=fixture.PREFIX + "edges-", dir=parent))
        root.chmod(0o700)
        print("Retained edge root: " + str(root), flush=True)
        with fixture.private_output():
            fixture.prepare_root(root, args.distro)
            fixture.prepare_podman_cli(root, Path(podman).resolve(strict=True))
            fixture.write_json_new(
                root / "edges.json",
                {
                    "format": FORMAT,
                    "snapshot_schema": SNAPSHOT_SCHEMA,
                    "home_manifest_contract": HOME_MANIFEST_CONTRACT,
                    "cases": list(CASES),
                    "source": source,
                    "distro": args.distro,
                    "native": {
                        "path": str(binary),
                        "sha256": interactive.file_hash(binary),
                        "version": fixture.NATIVE_VERSION,
                        "interpreter": sys.executable,
                    },
                },
            )
            fixture.write_json_new(
                root / "config/opencode/opencode.json", provider_free_config()
            )
            catalog, worker, state = empty_catalog()
            environment = fixture.isolated_env(root)
            # Genuine, already-supported nonsecret routing setting; no controls
            # added to pass_env, service environment, vault or Podman selectors.
            environment["LEMONADE_BASE_URL"] = (
                "http://host.containers.internal:"
                + str(catalog.server_port)
                + "/api/v1"
            )
            with (
                patch.dict(os.environ, environment, clear=True),
                fixture.temporary_location(root, temporary),
            ):
                run(root, binary)
    except BaseException as error:
        primary = category(error)
        if isinstance(error, CaseFailure):
            secondary.extend(error.secondary)
    finally:
        if catalog is not None:
            try:
                catalog.shutdown()
                catalog.server_close()
                worker.join(timeout=3)
                require(not worker.is_alive(), "catalog-shutdown")
            except BaseException as error:
                secondary.append(category(error))
        if state is not None:
            # One finite post-teardown observation, used for BOTH verdict and
            # serialization. Daemon request handlers are not proven drained.
            catalog_counts = dict(state)
            if catalog_counts["unexpected"]:
                if primary is None:
                    primary = "catalog-request"
                else:
                    secondary.append("catalog-request")
        if source is not None:
            try:
                interactive.require_same_source(source)
            except BaseException as error:
                if primary is None:
                    primary = category(error)
                else:
                    secondary.append(category(error))
        if root is not None:
            try:
                fixture.write_json_new(
                    root / "edges-result.json",
                    {
                        "format": FORMAT,
                        "snapshot_schema": SNAPSHOT_SCHEMA,
                        "home_manifest_contract": HOME_MANIFEST_CONTRACT,
                        "status": "PASS"
                        if primary is None and not secondary
                        else "FAIL",
                        "source": source,
                        "native_version": fixture.NATIVE_VERSION,
                        "cases": {
                            case: ("edges-" + case + "-report.json")
                            if (root / ("edges-" + case + "-report.json")).exists()
                            else None
                            for case in CASES
                        },
                        "primary_category": primary,
                        "secondary_categories": secondary,
                        "catalog": catalog_counts,
                        "scope": "Fresh synthetic scripted owner/PTY edges and stopped start/reset refusal preservation. No provider inference, interactive UI, backup, OAuth refresh or security certification.",
                        "network_limit": "Public upstream catalog/package metadata traffic may occur; no network containment guarantee. Optional Lemonade discovery selects only the fixture empty-catalog stub.",
                        "storage_limit": "Backend info/locks may touch runtime metadata; not universal byte-level engine immutability.",
                    },
                )
            except BaseException as error:
                secondary.append(category(error))
        os.umask(old_umask)
    if root is not None:
        print("Report: " + str(root / "edges-result.json"))
        print(
            "Evidence retained; no automatic cleanup. After review, the existing global-vault helper's label-validated --cleanup supports this compatible layout."
        )
    print(
        "global-vault edges: "
        + ("PASS" if primary is None and not secondary else "FAIL")
        + " category="
        + (primary or (secondary[0] if secondary else "none"))
    )
    return 0 if primary is None and not secondary else 1


if __name__ == "__main__":
    raise SystemExit(main())
