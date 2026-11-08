"""Offline encrypted-global-vault regression against unmodified native V2.

Usage: uv run --no-project --with cryptography python -B \
    tests/smoke_global_vault.py --opencode /path/to/v2/opencode

Only temporary synthetic host/workspace state and loopback HTTP/MCP are used.
The production credential context, env/argv construction and foreground owner
run unchanged; a local exec transport replaces Podman, not OpenCode or its tools.
This is not a real host/Podman lifecycle acceptance test.
"""

import argparse
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
PROVIDER = "GLOBAL_PROVIDER_API_KEY"
SHELL = "GLOBAL_SHELL_SECRET"
MCP = "GLOBAL_MCP_SECRET"
LOCAL = "GLOBAL_LOCAL_SECRET"
ADDED = "GLOBAL_ADDED_SECRET"
COMPAT = "GLOBAL_COMPAT_TOKEN"
CUSTOM = "GLOBAL_CUSTOM_COMPAT"
NAMES = (PROVIDER, SHELL, MCP, LOCAL, ADDED, COMPAT, CUSTOM)
ALIASES = ("FIXTURE_PROVIDER_VALUE", "FIXTURE_MCP_VALUE")
MARKERS = {"GLOBAL_VAULT_SHELL_OK", "GLOBAL_VAULT_MCP_OK"}
COMPLETE = "GLOBAL_VAULT_NATIVE_OK"


class FixtureFailure(Exception):
    """Only fixed, non-private messages may leave the fixture."""


def require(condition, marker):
    if not condition:
        raise FixtureFailure(marker)


def digest(value):
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()


def private_free(value, sensitive, marker):
    raw = value if isinstance(value, bytes) else str(value).encode()
    require(not any(secret.encode() in raw for secret in sensitive), marker)


def json_request(url, method="GET", value=None, headers=None, timeout=20):
    parsed = urlparse(url)
    require(
        parsed.scheme == "http" and parsed.hostname == "127.0.0.1",
        "non-loopback fixture request",
    )
    request = Request(
        url,
        method=method,
        data=None if value is None else json.dumps(value).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def service_check(oracle, channel):
    """Subprocesses send hashes only; never echo an env value or error body."""
    values = {name: digest(os.environ.get(name)) for name in NAMES}
    aliases = (
        {name: digest(os.environ.get(name)) for name in ALIASES}
        if channel.startswith("mcp")
        else {}
    )
    result = json_request(
        oracle,
        "POST",
        {"channel": channel, "values": values, "aliases": aliases, "pid": os.getpid()},
        timeout=10,
    )
    require(result == {"ok": True}, "synthetic environment check failed")


def fixture_service(mode, oracle):
    if mode == "shell":
        service_check(oracle, "shell")
        print("GLOBAL_VAULT_SHELL_OK", flush=True)
        return
    for line in sys.stdin:
        message = json.loads(line)
        method, result = message.get("method"), {}
        if method == "initialize":
            service_check(oracle, "mcp-init")
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oc-box-global-vault-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "check",
                        "description": "Check synthetic MCP environment without revealing values",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            service_check(oracle, "mcp")
            result = {"content": [{"type": "text", "text": "GLOBAL_VAULT_MCP_OK"}]}
        if "id" in message:
            print(
                json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
                flush=True,
            )


@contextlib.contextmanager
def provider_fixture(sensitive):
    cases = {}
    failures = []

    class Provider(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                require(
                    0 < length <= 8 * 1024 * 1024, "invalid synthetic request length"
                )
                body = json.loads(self.rfile.read(length))
                route = self.path.split("/")
                require(
                    len(route) >= 3 and route[2] in cases, "unknown synthetic scenario"
                )
                case = cases[route[2]]
                if route[1] == "check":
                    channel = body.get("channel")
                    require(
                        channel in ("shell", "mcp", "mcp-init"),
                        "unknown synthetic channel",
                    )
                    expected = {
                        name: digest(case["expected"].get(name)) for name in NAMES
                    }
                    require(
                        body.get("values") == expected,
                        "native credential environment mismatch",
                    )
                    aliases = (
                        {
                            ALIASES[0]: digest(
                                case["expected"].get(case["provider_name"], "")
                            ),
                            ALIASES[1]: digest(case["expected"].get(MCP, "")),
                        }
                        if channel.startswith("mcp")
                        else {}
                    )
                    require(
                        body.get("aliases") == aliases,
                        "native MCP env substitution mismatch",
                    )
                    private_free(
                        Path(f"/proc/{int(body['pid'])}/cmdline").read_bytes(),
                        sensitive,
                        "native tool argv contains plaintext",
                    )
                    case["channels"].add(channel)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                require(
                    route[1] == "v1" and self.path.endswith("/chat/completions"),
                    "unexpected synthetic provider endpoint",
                )
                require(
                    self.headers.get("Authorization")
                    == "Bearer " + case["expected"][case["provider_name"]],
                    "native provider auth did not use startup credentials",
                )
                private_free(
                    json.dumps(body),
                    sensitive,
                    "native provider body contains plaintext",
                )
                messages = body.get("messages", [])
                last = max(
                    (
                        index
                        for index, item in enumerate(messages)
                        if item.get("role") == "user"
                    ),
                    default=-1,
                )
                results = [
                    item for item in messages[last + 1 :] if item.get("role") == "tool"
                ]
                tools = {item["function"]["name"] for item in body.get("tools", [])}
                delta, finish = {"content": COMPLETE}, "stop"
                # Native title/summary requests intentionally have no tools.
                # Only the actual Build request must execute the two checks.
                if tools and len(results) < 2:
                    if not results:
                        name = "shell"
                        command = [
                            sys.executable,
                            "-B",
                            str(Path(__file__).resolve()),
                            "--fixture-service",
                            "shell",
                            "--oracle",
                            case["oracle"],
                        ]
                        arguments = {"command": shlex.join(command)}
                    else:
                        name, arguments = (
                            "execute",
                            {"code": "return await tools.fixture.check({})"},
                        )
                    require(
                        name in tools, "expected native " + name + " tool unavailable"
                    )
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_global_vault_" + str(len(results)),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                    finish = "tool_calls"
                elif tools:
                    require(
                        all(
                            any(
                                marker in str(item.get("content", ""))
                                for item in results
                            )
                            for marker in MARKERS
                        ),
                        "native shell/MCP result marker missing",
                    )
                    case["completed"] += 1
                case["requests"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in (
                    {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                ):
                    event = {
                        "id": "chatcmpl-global-vault",
                        "object": "chat.completion.chunk",
                        "model": "gpt-smoke",
                        **chunk,
                    }
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except Exception as error:
                failures.append(
                    str(error)
                    if isinstance(error, FixtureFailure)
                    else "synthetic provider/oracle rejected a request"
                )
                try:
                    self.send_error(500, "Synthetic global vault fixture failed")
                except OSError:
                    pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, *_args):
            failures.append("synthetic HTTP handler failed")

    server = Server(("127.0.0.1", 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", cases, failures
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        require(not worker.is_alive(), "synthetic HTTP shutdown timed out")


def bounded(command, env, cwd, sensitive, timeout=90, failures=None):
    private_free(
        json.dumps(list(command)), sensitive, "subprocess argv contains plaintext"
    )
    child = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        # Native V2 waits for FIFO stdin EOF even with the prompt already in argv.
        output, errors = child.communicate(input="", timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate(timeout=5)
        raise FixtureFailure("native global vault subprocess timed out") from None
    private_free(output + errors, sensitive, "native output contains plaintext")
    if failures:
        raise FixtureFailure(failures[0])
    require(
        child.returncode == 0,
        "native global vault subprocess failed; private output withheld",
    )
    return output


def scan_storage(root, sensitive):
    for path in root.rglob("*"):
        require(not path.is_symlink(), "fixture unexpectedly created a storage symlink")
        if path.is_file():
            private_free(
                path.read_bytes(), sensitive, "fixture storage contains plaintext"
            )


def main(binary):
    sys.path.insert(0, str(APP))
    import credentials
    import oc_box as box
    import opencode_compat
    import vault
    import workspace_service

    try:
        vault.crypto()
    except vault.VaultError:
        raise FixtureFailure(
            "cryptography is required (no skip): use uv run --no-project --with cryptography python -B tests/smoke_global_vault.py --opencode /path/to/v2/opencode"
        ) from None
    binary = str(Path(binary).resolve(strict=True))
    passphrase = secrets.token_urlsafe(36)
    first = {name: secrets.token_urlsafe(36) for name in (PROVIDER, SHELL, MCP, LOCAL)}
    first[LOCAL] = "http://localhost/" + first[LOCAL]
    latest = {
        **first,
        PROVIDER: secrets.token_urlsafe(36),
        MCP: secrets.token_urlsafe(36),
        ADDED: secrets.token_urlsafe(36),
    }
    compatibility = {
        COMPAT: secrets.token_urlsafe(36),
        CUSTOM: secrets.token_urlsafe(36),
    }
    vault_sensitive = [passphrase, *first.values(), *latest.values()]
    sensitive = [*vault_sensitive, *compatibility.values()]
    temporary_parent = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
    with tempfile.TemporaryDirectory(
        prefix="oc-box-global-", dir=temporary_parent
    ) as temporary:
        root = Path(temporary)
        host = {
            "PATH": os.environ["PATH"],
            "HOME": str(root / "host/home"),
            "LANG": "C.UTF-8",
            "OC_BOX_CONTAINER": "0",
            "OC_BOX_STATE_HOME": str(root / "host/registry"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
        }
        for name in ("CONFIG", "DATA", "STATE", "CACHE"):
            host["XDG_" + name + "_HOME"] = str(root / "host" / name.lower())
        for name in (
            "HOME",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            Path(host[name]).mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = []
        with patch.dict(os.environ, host, clear=True):
            version = opencode_compat.version(
                bounded([binary, "--version"], host, root, sensitive, timeout=30)
            )
            require(version == "2.2.6", "fixture requires unmodified OpenCode 2.2.6")
            settings = Path(host["XDG_CONFIG_HOME"]) / "oc-box/config.json"
            box.atomic_json(settings, {"pass_env": [CUSTOM]})
            target = vault.vault_path()

            def write(values):
                with vault.locked(target, create=True):
                    vault.write_encrypted(target, values, passphrase)

            def host_clean():
                private_free(
                    json.dumps(dict(os.environ)),
                    vault_sensitive,
                    "host os.environ acquired vault plaintext",
                )
                require(
                    all(
                        name not in os.environ
                        for name in (PROVIDER, SHELL, MCP, LOCAL, ADDED)
                    ),
                    "host os.environ acquired vault names",
                )

            def no_load(*_args, **_kwargs):
                raise FixtureFailure("non-unlocking operation accessed the vault")

            write(first)
            original_exec = box.exec_command

            def local_exec(data, arguments, **kwargs):
                # Keep production policy, name-only Podman argv and V2 env -i
                # bridge. Only emulate Podman's env delivery/exec locally.
                command = original_exec(data, arguments, **kwargs)
                private_free(
                    json.dumps(list(command)),
                    sensitive,
                    "production exec argv contains plaintext",
                )
                host_clean()
                private_free(
                    json.dumps(box.maintenance_host_env()),
                    sensitive,
                    "maintenance environment contains credentials",
                )
                start = command.index(data["container"])
                environment = {
                    "PATH": host["PATH"],
                    "OPENCODE_DISABLE_MODELS_FETCH": "1",
                }
                index = 3
                while index < start:
                    option, value = command[index : index + 2]
                    if option == "--env":
                        name, separator, literal = value.partition("=")
                        environment[name] = (
                            literal if separator else command.environment[name]
                        )
                    else:
                        require(
                            option == "--workdir", "unexpected production exec option"
                        )
                    index += 2
                # The production V2 bridge only preserves named variables.
                # Disable external model-catalog fetch in that private process.
                arguments = list(command[start + 1 :])
                require(
                    arguments[:2] == ["python3", "-c"],
                    "production V2 env bridge missing",
                )
                arguments[3] += ",OPENCODE_DISABLE_MODELS_FETCH"
                trampoline = "import os,sys; os.chdir(os.environ['PWD']); os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
                return box.ContainerCommand(
                    [sys.executable, "-B", "-c", trampoline, *arguments], environment
                )

            with (
                provider_fixture(sensitive) as (url, cases, failures),
                patch.object(box, "exec_command", side_effect=local_exec),
                patch.object(box, "CONTAINER_OPENCODE", binary),
            ):

                def case(
                    label, expected, *, data=None, provider_name=PROVIDER, policy=None
                ):
                    if data is None:
                        project = root / label / "project"
                        project.mkdir(parents=True)
                        data = {
                            "id": secrets.token_hex(16),
                            "project": str(project),
                            "home": str(root / label / "home"),
                            "container": "oc-box-synthetic-" + label,
                            "opencode_version": "2.2.6",
                        }
                        if policy is not None:
                            data["credentials"] = policy
                    registry = (
                        box.state_root() / "workspaces" / data["id"] / "workspace.json"
                    )
                    box.atomic_json(registry, data)
                    oracle = url + "/check/" + label
                    config = {
                        **box.container_build_config("2.2.6"),
                        "snapshots": False,
                        "formatter": False,
                        "update": "disable",
                        "model": "fixture/gpt-smoke",
                        "policies": [
                            {
                                "action": "provider.use",
                                "resource": "*",
                                "effect": "deny",
                            },
                            {
                                "action": "provider.use",
                                "resource": "fixture",
                                "effect": "allow",
                            },
                        ],
                        "providers": {
                            "fixture": {
                                "package": "aisdk:@ai-sdk/openai-compatible",
                                "settings": {
                                    "baseURL": url + "/v1/" + label,
                                    "apiKey": "{env:" + provider_name + "}",
                                },
                                "models": {
                                    "gpt-smoke": {
                                        "name": "Global vault fixture",
                                        "capabilities": {
                                            "tools": True,
                                            "input": ["text"],
                                            "output": ["text"],
                                        },
                                        "limit": {"context": 32000, "output": 2000},
                                    }
                                },
                            }
                        },
                        "mcp": {
                            "servers": {
                                "fixture": {
                                    "type": "local",
                                    "command": [
                                        sys.executable,
                                        "-B",
                                        str(Path(__file__).resolve()),
                                        "--fixture-service",
                                        "mcp",
                                        "--oracle",
                                        oracle,
                                    ],
                                    "environment": {
                                        ALIASES[0]: "{env:" + provider_name + "}",
                                        ALIASES[1]: "{env:" + MCP + "}",
                                    },
                                    "timeout": {
                                        "startup": 10000,
                                        "catalog": 10000,
                                        "execution": 10000,
                                    },
                                }
                            }
                        },
                    }
                    config_path = (
                        Path(data["home"]) / ".config/opencode/oc-box-profile.json"
                    )
                    box.atomic_json(config_path, config)
                    value = {
                        "data": data,
                        "registry": registry,
                        "oracle": oracle,
                        "expected": dict(expected),
                        "provider_name": provider_name,
                        "channels": set(),
                        "completed": 0,
                        "requests": 0,
                    }
                    cases[label] = value
                    metadata.append((registry, json.loads(json.dumps(data))))
                    return value

                @contextlib.contextmanager
                def owning(value, *, no_vault=False):
                    data = value["data"]
                    with (
                        box.workspace_lock(value["registry"]),
                        credentials.unlocked(data, no_vault=no_vault),
                        workspace_service.Owner(box, value["registry"], data) as owner,
                    ):
                        host_clean()
                        require(
                            credentials.is_active(data),
                            "owner credential context is missing",
                        )
                        yield owner
                    require(
                        not credentials.is_active(data),
                        "owner credential context leaked",
                    )

                def infer(owner, value, session=None):
                    connection = owner.connection
                    auth = (
                        "Basic "
                        + base64.b64encode(
                            ("opencode:" + connection["password"]).encode()
                        ).decode()
                    )

                    def api(method, path, body=None):
                        return json_request(
                            connection["url"] + path,
                            method,
                            body,
                            {"Authorization": auth},
                        )

                    pid = api("GET", "/api/info")["pid"]
                    require(
                        value.get("pid", pid) == pid, "live native owner was replaced"
                    )
                    value["pid"] = pid
                    private_free(
                        Path(f"/proc/{pid}/cmdline").read_bytes(),
                        sensitive,
                        "native server argv contains plaintext",
                    )
                    if session is None:
                        session = api(
                            "POST",
                            "/api/session",
                            {"location": {"directory": value["data"]["project"]}},
                        )["data"]["id"]
                    owner.publish(session)
                    count = value["completed"]
                    value["channels"].discard("shell")
                    value["channels"].discard("mcp")
                    # Explicit --server clients carry control auth only. The
                    # provider/shell/MCP must use the owner's startup env.
                    environment = {
                        **box.maintenance_host_env(),
                        "OPENCODE_PASSWORD": connection["password"],
                        "OPENCODE_DISABLE_MODELS_FETCH": "1",
                    }
                    private_free(
                        json.dumps(environment),
                        sensitive,
                        "native client environment contains credentials",
                    )
                    output = bounded(
                        [
                            binary,
                            "run",
                            "--server",
                            connection["url"],
                            "--session",
                            session,
                            "--agent",
                            "build",
                            "--model",
                            "fixture/gpt-smoke",
                            "Exercise synthetic global vault shell and MCP delivery.",
                        ],
                        environment,
                        value["data"]["project"],
                        sensitive,
                        failures=failures,
                    )
                    require(
                        not failures,
                        failures[0]
                        if failures
                        else "synthetic provider/oracle rejected a request",
                    )
                    require(
                        COMPLETE in output
                        and value["completed"] == count + 1
                        and value["requests"] >= 3,
                        "native inference did not complete",
                    )
                    require(
                        value["channels"] == {"mcp-init", "shell", "mcp"},
                        "native shell/MCP startup checks did not run",
                    )
                    host_clean()
                    scan_storage(root, sensitive)
                    return session

                initial = case("first", first)
                require(
                    "credentials" not in initial["data"]
                    and all(name not in os.environ for name in NAMES),
                    "first launch is not an implicit, unsourced global vault launch",
                )
                with patch.object(vault, "hidden", return_value=passphrase) as prompt:
                    with owning(initial) as owner:
                        session = infer(owner, initial)
                        require(
                            prompt.call_count == 1,
                            "first owner did not unlock exactly once",
                        )
                        write(latest)
                        infer(owner, initial, session)
                        require(
                            prompt.call_count == 1,
                            "live owner reopened the rotated vault",
                        )
                        with patch.dict(os.environ, compatibility):
                            added = case("second-project", latest)
                            with owning(added) as second_owner:
                                infer(second_owner, added)
                            require(
                                prompt.call_count == 2,
                                "new project did not unlock the current vault",
                            )
                        infer(owner, initial, session)
                    restart = case("restart", latest, data=initial["data"])
                    with owning(restart) as restarted:
                        infer(restarted, restart)
                    require(
                        prompt.call_count == 3,
                        "restarted owner did not reread the current vault",
                    )
                print(
                    "PASS: first unsourced native V2 owner receives every global vault entry through provider auth, shell and MCP.",
                    flush=True,
                )
                print(
                    "PASS: addition/rotation reaches another project and restarted owner; live owner retains startup env and global mode suppresses ambient/pass_env credentials.",
                    flush=True,
                )

                encrypted_before_opt_out = target.read_bytes()
                with (
                    patch.dict(os.environ, compatibility),
                    patch.object(vault, "hidden", side_effect=no_load),
                    patch.object(vault, "read_envelope", side_effect=no_load),
                    patch.object(vault, "crypto", side_effect=no_load),
                ):
                    opted_out = case("no-vault", compatibility, provider_name=COMPAT)
                    with owning(opted_out, no_vault=True) as owner:
                        infer(owner, opted_out)
                    selected_policy = {
                        "mode": "selected",
                        "env": [COMPAT],
                        "vault": [PROVIDER, MCP],
                    }
                    selected_out = case(
                        "selected-no-vault",
                        {COMPAT: compatibility[COMPAT]},
                        provider_name=COMPAT,
                        policy=selected_policy,
                    )
                    with owning(selected_out, no_vault=True) as owner:
                        infer(owner, selected_out)
                    require(
                        selected_out["data"]["credentials"] == selected_policy,
                        "no-vault changed saved selection",
                    )
                    require(
                        "credentials" not in opted_out["data"],
                        "no-vault saved an implicit policy",
                    )
                    explicit_env = {
                        **opted_out["data"],
                        "credentials": {"mode": "env", "env": [], "vault": []},
                    }
                    with credentials.unlocked(explicit_env):
                        require(
                            {
                                name: box.container_env(explicit_env)[name]
                                for name in compatibility
                            }
                            == compatibility,
                            "explicit env policy lost compatibility values",
                        )
                        require(
                            all(
                                name not in box.container_env(explicit_env)
                                for name in latest
                            ),
                            "explicit env policy opened global vault",
                        )
                require(
                    target.read_bytes() == encrypted_before_opt_out,
                    "no-vault/explicit-env launch modified the encrypted vault",
                )
                with (
                    patch.dict(os.environ, compatibility),
                    patch.object(vault, "hidden", return_value=passphrase) as prompt,
                ):
                    selected = case(
                        "selected",
                        {
                            PROVIDER: latest[PROVIDER],
                            MCP: latest[MCP],
                            COMPAT: compatibility[COMPAT],
                        },
                        policy=selected_policy,
                    )
                    with owning(selected) as owner:
                        infer(owner, selected)
                    require(
                        prompt.call_count == 1,
                        "explicit selected policy did not unlock exactly once",
                    )
                print(
                    "PASS: native no-vault skips encrypted values without a read/prompt/crypto load or saved-policy change; selected/env overrides preserve their constraints.",
                    flush=True,
                )

            data = initial["data"]
            with (
                patch.object(vault, "hidden", side_effect=no_load),
                patch.object(vault, "read_envelope", side_effect=no_load),
                patch.object(vault, "crypto", side_effect=no_load),
            ):
                require(
                    credentials.policy(data)
                    == {"mode": "vault", "env": [], "vault": []},
                    "V2 implicit policy is not global",
                )
                require(
                    not credentials.is_active(data),
                    "informational policy access unlocked vault",
                )
                for legacy in (
                    {**data, "opencode_version": "1.19.5"},
                    {
                        **data,
                        "opencode_version": "1.19.5",
                        "credentials": {"mode": "vault", "env": [], "vault": []},
                    },
                ):
                    with (
                        patch.dict(os.environ, compatibility),
                        credentials.unlocked(legacy),
                    ):
                        forwarded = box.container_env(legacy)
                        require(
                            all(
                                forwarded.get(name) == value
                                for name, value in compatibility.items()
                            ),
                            "V1 default/saved-global policy lost legacy forwarding",
                        )
                        require(
                            not credentials.active_values(legacy),
                            "V1 default unlocked a global vault",
                        )
                with (
                    patch.dict(
                        os.environ,
                        {"XDG_DATA_HOME": str(root / "absent/data"), **compatibility},
                    ),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with credentials.unlocked(data):
                        require(
                            credentials.is_active(data)
                            and not credentials.active_values(data),
                            "absent vault context is invalid",
                        )
                        require(
                            all(
                                box.container_env(data).get(name) == value
                                for name, value in compatibility.items()
                            ),
                            "absent vault did not retain compatibility fallback",
                        )
                    require(
                        not vault.vault_path().exists(),
                        "absent vault fallback created a vault",
                    )

            with patch.object(vault, "hidden", return_value=secrets.token_urlsafe(36)):
                try:
                    with credentials.unlocked(data):
                        raise FixtureFailure("incorrect passphrase silently fell back")
                except vault.VaultError:
                    pass
                require(
                    not credentials.is_active(data),
                    "failed unlock left an active credential context",
                )
            write({})
            with (
                patch.dict(os.environ, compatibility),
                patch.object(vault, "hidden", return_value=passphrase) as prompt,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with credentials.unlocked(data):
                    require(
                        credentials.is_active(data)
                        and not credentials.active_values(data),
                        "empty initialized vault context is invalid",
                    )
                    require(
                        all(name not in box.container_env(data) for name in NAMES),
                        "empty initialized vault fell back to ambient credentials",
                    )
                require(
                    prompt.call_count == 1, "empty initialized vault did not unlock"
                )
            host_clean()
            for registry, saved in metadata:
                require(
                    box.read_json(registry) == saved,
                    "credential context modified registry metadata",
                )
                if "credentials" not in saved:
                    require(
                        "credentials" not in box.read_json(registry),
                        "automatic global selection was saved",
                    )
            scan_storage(root, sensitive)
            print(
                "PASS: policy inspection and V1 never load the global vault; absent/empty vault behavior and wrong-passphrase refusal remain distinct.",
                flush=True,
            )
            print(
                "PASS: vault plaintext stays out of host os.environ; no synthetic plaintext in argv, native output, config, registry or fixture storage; clients receive no provider/vault values.",
                flush=True,
            )
    print(
        "LIMIT: offline native V2 regression only; actual global-vault/--no-vault host Podman lifecycle acceptance remains pending.",
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode")
    parser.add_argument(
        "--fixture-service", choices=("shell", "mcp"), help=argparse.SUPPRESS
    )
    parser.add_argument("--oracle", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    try:
        if arguments.fixture_service:
            require(
                arguments.oracle is not None,
                "synthetic service needs a loopback oracle",
            )
            fixture_service(arguments.fixture_service, arguments.oracle)
        else:
            require(arguments.opencode is not None, "--opencode is required")
            main(arguments.opencode)
    except FixtureFailure as error:
        print("FAIL: " + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Native response/error content may contain credentials; never render
        # exception repr, subprocess stdout/stderr or traceback locals.
        print(
            "FAIL: unexpected global vault fixture failure; private output withheld",
            file=sys.stderr,
        )
        sys.exit(1)
