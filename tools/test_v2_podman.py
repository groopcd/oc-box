#!/usr/bin/env python3
"""Opt-in native V2 lifecycle acceptance fixture for a regular HOST terminal.

Runs real rootless Podman and OpenCode 1.x/2.x against an offline synthetic
provider/MCP, isolated registry/config/host databases and synthetic encrypted
vault. The TUI exit and masked prompt input are replaced by scripted fixture
actions; selection/decryption, service startup, tools, backup, restore, reset and
downgrade are production paths. Never uses the operator's vault/model/history.
Fixture resources are retained for diagnosis and removed only by --cleanup.
"""

import argparse
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import backup_crypto as crypto
import backups
import credentials
import oc_box as box
import opencode_compat as compat
import vault
import workspace_service
from version_info import get_version_info

PREFIX = "oc-box-live-v2-"
KEY = "ACCEPTANCE_API_KEY"
UNSELECTED = "ACCEPTANCE_UNSELECTED_TOKEN"
LEGACY_ID = "ses_v1_acceptance_fixture"
NATIVE_VERSIONS = {"1": "1.19.5", "2": "2.2.6"}
MCP = """import json,os,sys
for line in sys.stdin:
    message=json.loads(line); method=message.get('method'); result={}
    if method=='initialize':
        result={'protocolVersion':message['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'oc-box-acceptance','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':'check','description':'Check synthetic MCP environment without revealing values','inputSchema':{'type':'object','properties':{}}}]}
    elif method=='tools/call':
        assert os.environ.get('FIXTURE_MCP_KEY') and os.environ['FIXTURE_MCP_KEY']==os.environ.get('ACCEPTANCE_API_KEY')
        result={'content':[{'type':'text','text':'VAULT_MCP_OK'}]}
    if 'id' in message:
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)
"""


def workspace(ident):
    path, data = box.find_workspace(workspace_id=ident)
    if path is None or data is None:
        raise AssertionError("The recorded fixture workspace is missing")
    return path, data


def stopped_container(data):
    info = backups.owned_container(box, data)
    if info is None or info["State"]["Running"]:
        raise AssertionError("Fixture container should be retained and stopped")
    return info


def isolated_env(root):
    """Keep host runtime discovery; isolate Podman storage and OpenCode data."""
    env = box.maintenance_host_env()
    env.update(
        OC_BOX_STATE_HOME=str(root / "registry"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_DATA_HOME=str(root / "data"),
        XDG_STATE_HOME=str(root / "state"),
        XDG_CACHE_HOME=str(root / "cache"),
        PATH=str(root / "bin") + ":" + env.get("PATH", "/usr/bin:/bin"),
        OC_BOX_CONTAINER="0",
        OPENCODE_DISABLE_MODELS_FETCH="1",
        OPENCODE_AUTH_CONTENT="{}",
        PYTHONDONTWRITEBYTECODE="1",
    )
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


def existing_directory(path):
    parent = Path(path).expanduser().resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("Fixture work/temp parent must be an existing directory")
    return parent


@contextlib.contextmanager
def temporary_location(root, parent=None):
    """Place Python encrypted staging and Podman temp files explicitly."""
    with contextlib.ExitStack() as stack:
        if parent is not None:
            directory = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="oc-box-v2-temp-", dir=existing_directory(parent)
                    )
                )
            )
        else:
            directory = root / "temporary"
            if directory.is_symlink():
                raise ValueError("Fixture temporary directory must not be symlinked")
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError(
                    "Fixture temporary directory must be private and owned by this user"
                )
        print(
            "oc-box fixture: Temporary image and encrypted staging directory: "
            + str(directory),
            flush=True,
        )
        # tempfile caches its default directory; the host process may have
        # used it before the fixture env was set, so override both layers.
        stack.enter_context(patch.dict(os.environ, {"TMPDIR": str(directory)}))
        stack.enter_context(patch.object(tempfile, "tempdir", str(directory)))
        yield directory


def native_env(root, major):
    """Version checks share the wrapper's isolated roots, never host config."""
    env = isolated_env(root)
    for key in list(env):
        if key.startswith("OPENCODE_"):
            env.pop(key)
    env["HOME"] = str(root / "host-home")
    for name in ("CONFIG", "DATA", "CACHE", "STATE"):
        env["XDG_" + name + "_HOME"] = str(
            root / ("host-" + name.lower() + "-" + major)
        )
    env.update(
        OPENCODE_TEST_HOME=env["HOME"],
        OPENCODE_DISABLE_MODELS_FETCH="1",
        OPENCODE_DISABLE_PROJECT_CONFIG="1",
        OPENCODE_PURE="1",
        OPENCODE_DISABLE_AUTOUPDATE="1",
    )
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {"snapshot": False, "formatter": False, "lsp": False, "autoupdate": False}
        if major == "1"
        else {
            "snapshots": False,
            "formatter": False,
            "update": "disable",
            "policies": [{"action": "plugin.use", "resource": "*", "effect": "deny"}],
        }
    )
    return env


def check_native_version(root, binary, major):
    result = box.run(
        [str(binary), "--version"],
        env=native_env(root, major),
        stdin=subprocess.DEVNULL,
        capture=True,
        private=True,
        timeout=15,
    )
    try:
        found = compat.version(result.stdout)
    except ValueError as exc:
        raise ValueError(
            f"Native fixture executable {binary} did not report a supported release version; expected {NATIVE_VERSIONS[major]}."
        ) from exc
    if found != NATIVE_VERSIONS[major]:
        raise ValueError(
            f"Native fixture executable mismatch: expected {NATIVE_VERSIONS[major]}, detected {found}, path {binary}. No test container was started; check or reinstall this separate test executable."
        )
    return found


def prepare_native_cli(root, v1, v2, *, resume=False):
    # Probe the recorded absolute executables before rewriting the selector or
    # launching containers; a PATH-resolved opencode cannot stand in for them.
    for major, binary in (("1", v1), ("2", v2)):
        found = check_native_version(root, binary, major)
        print(
            f"oc-box fixture: Verified native {major}.x {found}: {binary}", flush=True
        )
    wrapper = write_wrapper(root, v1, v2, refresh=resume)
    for major in ("1", "2"):
        (root / "host-major").write_text(major)
        result = box.run(
            [str(wrapper), "--version"],
            env=isolated_env(root),
            stdin=subprocess.DEVNULL,
            capture=True,
            private=True,
            timeout=15,
        )
        found = compat.version(result.stdout)
        if found != NATIVE_VERSIONS[major]:
            raise ValueError(
                f"Fixture selector mismatch: expected {NATIVE_VERSIONS[major]}, detected {found}, wrapper {wrapper}."
            )
    (root / "host-major").write_text("1")
    return wrapper


def write_wrapper(root, v1, v2, *, refresh=False):
    binary = root / "bin/opencode"
    binary.parent.mkdir(exist_ok=refresh)
    if refresh:
        if binary.is_symlink():
            raise ValueError(
                "Refusing to replace a symlinked retained fixture selector"
            )
        previous = binary.read_text()
        if (
            "paths=json.loads((root/'executables.json').read_text())" not in previous
            or "os.execve(paths[major]" not in previous
        ):
            raise ValueError(
                "Refusing to replace an unfamiliar retained fixture selector"
            )
    code = """import json,os,pathlib,sys
root=pathlib.Path(__file__).resolve().parents[1]
major=(root/'host-major').read_text().strip()
paths=json.loads((root/'executables.json').read_text())
env=dict(os.environ)
for key in list(env):
    if key.startswith('OPENCODE_'): env.pop(key)
env['HOME']=str(root/'host-home')
for name in ('CONFIG','DATA','CACHE','STATE'):
    env['XDG_'+name+'_HOME']=str(root/('host-'+name.lower()+'-'+major))
env['OPENCODE_TEST_HOME']=env['HOME']
env['OPENCODE_DISABLE_MODELS_FETCH']='1'
env['OPENCODE_DISABLE_PROJECT_CONFIG']='1'
env['OPENCODE_PURE']='1'
env['OPENCODE_DISABLE_AUTOUPDATE']='1'
env['OPENCODE_CONFIG_CONTENT']=json.dumps({'snapshot':False,'formatter':False,'lsp':False,'autoupdate':False} if major=='1' else {'snapshots':False,'formatter':False,'update':'disable','policies':[{'action':'plugin.use','resource':'*','effect':'deny'}]})
os.execve(paths[major],[paths[major],*sys.argv[1:]],env)
"""
    binary.write_text("#!" + sys.executable + "\n" + code)
    binary.chmod(0o700)
    (root / "executables.json").write_text(json.dumps({"1": str(v1), "2": str(v2)}))
    (root / "host-major").write_text("1")
    return binary


def storage_config(root):
    # Separate both persistent graphroot and transient runroot; sharing the
    # host's /run/user/.../containers runroot can mix Libpod database identities.
    target = root / "config/containers/storage.conf"
    target.parent.mkdir(parents=True)
    target.write_text(
        '[storage]\ndriver = "overlay"\nrunroot = '
        + json.dumps(str(root / "podman-run"))
        + "\ngraphroot = "
        + json.dumps(str(root / "data/containers/storage"))
        + "\n"
    )


def legacy_session(project):
    return {
        "info": {
            "id": LEGACY_ID,
            "slug": "acceptance-fixture",
            "title": "V1 acceptance",
            "directory": str(project),
            "projectID": "global",
            "version": "1.19.5",
            "time": {"created": 1, "updated": 2},
        },
        "messages": [
            {
                "info": {
                    "id": "msg_v1_acceptance",
                    "sessionID": LEGACY_ID,
                    "role": "user",
                    "agent": "build",
                    "model": {"providerID": "fixture", "modelID": "gpt-smoke"},
                    "time": {"created": 1},
                },
                "parts": [
                    {
                        "id": "prt_v1_acceptance",
                        "sessionID": LEGACY_ID,
                        "messageID": "msg_v1_acceptance",
                        "type": "text",
                        "text": "V1_HISTORY_RETAINED",
                    }
                ],
            }
        ],
    }


def verify_v1_history(actual, expected):
    """V1 may update project/access metadata when reopened; history is exact."""
    box.validate_session_export(actual, expected["info"]["id"])
    if (
        actual["info"]["id"] != expected["info"]["id"]
        or compat.directory(actual) != compat.directory(expected)
        or actual["messages"] != expected["messages"]
    ):
        raise AssertionError(
            "V1 downgrade changed retained session identity, project or complete message/part history"
        )


def verify_v2_progress(actual, expected):
    box.validate_session_export(actual, expected["info"]["id"])
    current = {message["id"]: message for message in actual["messages"]}
    retained = {message["id"] for message in expected["messages"]}
    if (
        compat.directory(actual) != compat.directory(expected)
        or any(
            current.get(message["id"]) != message for message in expected["messages"]
        )
        or [
            message["id"] for message in actual["messages"] if message["id"] in retained
        ]
        != [message["id"] for message in expected["messages"]]
    ):
        raise AssertionError("Returning to V2 lost or changed retained native history")


def fixture_config(port):
    return {
        "$schema": "https://opencode.ai/config.json",
        "snapshot": False,
        "formatter": False,
        "lsp": False,
        "autoupdate": False,
        "provider": {
            "fixture": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": f"http://host.containers.internal:{port}/v1",
                    "apiKey": "{env:" + KEY + "}",
                },
                "models": {
                    "gpt-smoke": {
                        "name": "Acceptance fixture",
                        "limit": {"context": 32000, "output": 2000},
                    }
                },
            }
        },
        "model": "fixture/gpt-smoke",
        "mcp": {
            "fixture": {
                "type": "local",
                "command": ["python3", "-c", MCP],
                "environment": {"FIXTURE_MCP_KEY": "{env:" + KEY + "}"},
            }
        },
    }


def make_provider(state):
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"fixture":"oc-box-v2-acceptance"}')

        def do_POST(self):
            try:
                if self.headers.get("Authorization") != "Bearer " + state["key"]:
                    raise AssertionError(
                        "Provider did not receive the current synthetic selected vault value"
                    )
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages = body.get("messages", [])
                last = max(
                    (
                        index
                        for index, message in enumerate(messages)
                        if message.get("role") == "user"
                    ),
                    default=-1,
                )
                results = [
                    message
                    for message in messages[last + 1 :]
                    if message.get("role") == "tool"
                ]
                names = {tool["function"]["name"] for tool in body.get("tools", [])}
                delta, finish = {"content": "HOST_V2_ACCEPTANCE_OK"}, "stop"
                if names and len(results) < 2:
                    if not results:
                        digest = hashlib.sha256(state["key"].encode()).hexdigest()
                        program = (
                            "import os,hashlib; assert hashlib.sha256(os.environ['"
                            + KEY
                            + "'].encode()).hexdigest()=='"
                            + digest
                            + "'; assert '"
                            + UNSELECTED
                            + "' not in os.environ; print('VAULT_SHELL_OK')"
                        )
                        name, arguments = (
                            "shell",
                            {"command": "python3 -c " + shlex.quote(program)},
                        )
                    else:
                        name, arguments = (
                            "execute",
                            {"code": "return await tools.fixture.check({})"},
                        )
                    if name not in names:
                        raise AssertionError(
                            "Expected native tool unavailable: " + name
                        )
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_acceptance_" + str(len(results)),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                    finish = "tool_calls"
                state["markers"].update(
                    marker
                    for marker in ("VAULT_SHELL_OK", "VAULT_MCP_OK")
                    if any(
                        marker in str(message.get("content", "")) for message in results
                    )
                )
                state["requests"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in (
                    {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "model": "gpt-smoke",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    },
                    {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "model": "gpt-smoke",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                ):
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except (AssertionError, ValueError, KeyError, OSError) as exc:
                state["errors"].append(str(exc))
                self.send_error(500, "Synthetic acceptance provider failed")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    # The container reaches this disposable host fixture via pasta's host
    # loopback gateway. No real provider or sensitive response is served by it.
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker


def digest_home(data, name):
    volume = backups.owned_volume(box, data)
    if volume is None:
        raise AssertionError("Fixture home volume missing")
    target = Path(volume["Mountpoint"]) / name
    return (
        crypto.small(
            [
                "podman",
                "unshare",
                sys.executable,
                "-B",
                "-c",
                "import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())",
                str(target),
            ]
        )
        .decode()
        .strip()
    )


def run_fixture_session(data, session_id, state, provider_port):
    """Bounded reachability check, then EOF-terminated native model input."""
    health = "import json,sys; from urllib.request import build_opener,ProxyHandler; opener=build_opener(ProxyHandler({})); response=opener.open(sys.argv[1],timeout=5); assert json.load(response)=={'fixture':'oc-box-v2-acceptance'}"
    try:
        box.run(
            box.exec_command(
                data,
                [
                    "python3",
                    "-c",
                    health,
                    f"http://host.containers.internal:{provider_port}/health",
                ],
            ),
            input="",
            capture=True,
            private=True,
            timeout=15,
        )
    except box.BoxError as exc:
        raise box.BoxError(
            "Fixture provider is unreachable through pasta's host-loopback mapping; check host firewall/networking. Native model request was not started."
        ) from exc
    print(
        "oc-box fixture: Provider reachable; exercising selected-vault shell and MCP delivery.",
        flush=True,
    )
    try:
        return box.run(
            box.exec_command(
                data,
                [
                    box.CONTAINER_OPENCODE,
                    "run",
                    "--session",
                    session_id,
                    "--model",
                    "fixture/gpt-smoke",
                    "--agent",
                    "build",
                    "Exercise synthetic vault shell and MCP delivery.",
                ],
            ),
            capture=True,
            private=True,
            input="",
            timeout=180,
        )
    except box.BoxError:
        print(
            "oc-box fixture: Model request failed; provider requests="
            + str(state["requests"])
            + ", verified tool outputs="
            + ",".join(sorted(state["markers"]))
            + ", fixture errors="
            + str(len(state["errors"]))
            + ". Private request/response data was not logged.",
            file=sys.stderr,
            flush=True,
        )
        raise


def cleanup(root):
    if (
        root.is_symlink()
        or not root.name.startswith(PREFIX)
        or root.stat().st_uid != os.getuid()
    ):
        raise ValueError("Not an owned V2 acceptance fixture directory")
    root = root.resolve(strict=True)
    recorded = json.loads((root / "fixture-ids.json").read_text())
    if not isinstance(recorded, list):
        raise ValueError("Invalid V2 acceptance fixture resource list")
    registry = root / "registry"
    if registry.is_symlink() or (registry / "workspaces").is_symlink():
        raise ValueError(
            "Fixture registry must not redirect cleanup outside its private root"
        )
    with patch.dict(os.environ, isolated_env(root), clear=True):
        paths = list(box.workspace_paths())
        for path in paths:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("Fixture workspace metadata must not be symlinked")
            data = box.read_json(path)
            if (
                data["id"] != path.parent.name
                or Path(data["project"]).resolve().parent != root
                or Path(data["project"]).name
                not in ("source", "restored-full", "restored-portable")
            ):
                raise ValueError(
                    "Unrecognized resource in the isolated fixture registry"
                )
            with box.workspace_lock(path):
                box.remove_resources(data)
    print(
        "Fixture-owned Podman resources removed; test projects/archives remain at "
        + str(root)
    )


def resume_fixture(root, *, full_restore=False, downgrade=False):
    """Validate isolated resources and evidence for the requested bounded retry."""
    if full_restore and downgrade:
        raise ValueError("Cannot combine full-restore and downgrade retry validation")
    if root.is_symlink() or not root.name.startswith(PREFIX):
        raise ValueError("Not a V2 acceptance fixture directory")
    root = root.resolve(strict=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("V2 acceptance fixture must be owned by this user and private")
    for relative in (
        "registry",
        "registry/workspaces",
        "source",
        "config",
        "config/containers",
        "config/opencode",
        "config/oc-box",
        "bin",
        "data",
        "data/containers",
        "data/containers/storage",
        "podman-run",
    ):
        path = root / relative
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Invalid retained fixture directory: " + relative)
    home = root / "host-home"
    if home.is_symlink() or (home.exists() and not home.is_dir()):
        raise ValueError("Invalid retained fixture host home")
    for relative in (
        "fixture-ids.json",
        "executables.json",
        "host-major",
        "bin/opencode",
        "config/opencode/opencode.json",
        "config/oc-box/config.json",
        "source/unfinished.txt",
        "config/containers/storage.conf",
    ):
        path = root / relative
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size > 1024 * 1024
        ):
            raise ValueError("Invalid retained fixture file: " + relative)
    ids = json.loads((root / "fixture-ids.json").read_text())
    if (
        not isinstance(ids, list)
        or len(ids) != (3 if downgrade else 1)
        or any(
            not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident)
            for ident in ids
        )
        or len(set(ids)) != len(ids)
    ):
        raise ValueError(
            "Resume supports only a source fixture before backup/restore; use cleanup and a new fixture after later failures"
        )
    if any(
        (root / name).exists() or (root / name).is_symlink()
        for name in (
            *(("full.ocbox",) if not (full_restore or downgrade) else ()),
            *(
                ("portable.ocbox", "restored-full", "restored-portable")
                if not downgrade
                else ()
            ),
            "result.json",
            "full-restore-result.json",
            "downgrade-result.json",
        )
    ):
        raise ValueError(
            "Resume would overwrite backup/restore evidence; use cleanup and a new fixture"
        )
    if full_restore or downgrade:
        for name in (
            "full.ocbox",
            "age-identity.txt",
            *(("portable.ocbox",) if downgrade else ()),
        ):
            file = root / name
            info = file.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError(
                    "Retained full backup and identity must be private regular fixture files"
                )
    storage = (
        '[storage]\ndriver = "overlay"\nrunroot = '
        + json.dumps(str(root / "podman-run"))
        + "\ngraphroot = "
        + json.dumps(str(root / "data/containers/storage"))
        + "\n"
    )
    if (root / "config/containers/storage.conf").read_text() != storage:
        raise ValueError(
            "Retained fixture Podman storage is not the expected isolated graphroot/runroot"
        )
    paths = json.loads((root / "executables.json").read_text())
    if not isinstance(paths, dict) or set(paths) != {"1", "2"}:
        raise ValueError("Invalid retained fixture executable mapping")
    binaries = tuple(Path(paths[major]).resolve(strict=True) for major in ("1", "2"))
    if not all(os.access(binary, os.X_OK) for binary in binaries):
        raise ValueError(
            "Retained native test executables are missing; reinstall them and start a new fixture"
        )
    with patch.dict(os.environ, isolated_env(root), clear=True):
        paths = list(box.workspace_paths())
        if len(paths) != len(ids) or any(
            path.is_symlink() or path.parent.is_symlink() for path in paths
        ):
            raise ValueError("Unexpected workspaces in retained fixture registry")
        path, data = workspace(ids[0])
        if (
            path not in paths
            or data["project"] != str(root / "source")
            or data["home"] != str(root / "host-home")
            or data["distro"] not in box.DISTROS
            or data.get("restore_incomplete")
            or data.get("devices")
            or data.get("podman_args")
        ):
            raise ValueError(
                "Retained fixture workspace does not match its isolated source/home"
            )
        if (
            data["container"] != "oc-box-" + ids[0]
            or data["home_volume"] != "oc-box-home-" + ids[0]
        ):
            raise ValueError(
                "Retained fixture resource names do not match its recorded identity"
            )
        with box.workspace_lock(path):
            box.refresh_workspace_identity(path, data)
            stopped_container(data)
        if downgrade:
            baseline = path.parent / "imports" / (LEGACY_ID + ".json")
            if baseline.parent.is_symlink():
                raise ValueError("Retained V1 import evidence must not be symlinked")
            info = baseline.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > crypto.MAX_SMALL
            ):
                raise ValueError("Missing or unsafe retained V1 import evidence")
            verify_v1_history(box.read_json(baseline), legacy_session(root / "source"))
            for index, kind in enumerate(("full", "portable"), 1):
                restored_path, restored = workspace(ids[index])
                if (
                    restored_path not in paths
                    or restored["project"] != str(root / ("restored-" + kind))
                    or restored.get("container_project") != data["project"]
                    or restored["home"] != data["home"]
                    or restored["container"] != "oc-box-" + ids[index]
                    or restored["home_volume"] != "oc-box-home-" + ids[index]
                    or restored.get("devices")
                    or restored.get("podman_args")
                    or restored.get("restore_incomplete")
                    or restored.get("restored_from", {}).get("workspace_id")
                    != data["id"]
                    or restored["restored_from"].get("kind") != kind
                ):
                    raise ValueError(
                        "Recorded restored workspace does not match retained downgrade fixture evidence"
                    )
                with box.workspace_lock(restored_path):
                    box.refresh_workspace_identity(restored_path, restored)
                    stopped_container(restored)
    return root, *binaries, data["distro"]


def retry_downgrade(root):
    """Complete only the V1/V2 transition after the restored fixture stage."""
    root, v1, v2, _ = resume_fixture(root, downgrade=True)
    wrapper = prepare_native_cli(root, v1, v2, resume=True)
    archives = {
        name: crypto.digest(root / name) for name in ("full.ocbox", "portable.ocbox")
    }
    state = {
        "key": "fixture-" + secrets.token_urlsafe(24),
        "markers": set(),
        "requests": 0,
        "errors": [],
    }
    password = secrets.token_urlsafe(24)
    server, worker = make_provider(state)
    history = {}
    try:
        with (
            patch.dict(os.environ, isolated_env(root), clear=True),
            patch.object(vault, "vault_path", return_value=root / "vault/vault.json"),
            patch.object(Path, "home", return_value=root / "host-home"),
            patch.object(
                box,
                "detect_opencode_release",
                side_effect=lambda: compat.version(
                    box.run(
                        [str(wrapper), "--version"],
                        stdin=subprocess.DEVNULL,
                        capture=True,
                        private=True,
                        timeout=15,
                    ).stdout
                ),
            ),
            patch.object(
                box,
                "host_command",
                side_effect=lambda data, *arguments: compat.command(
                    data.get("opencode_version", "1.0.0"), str(wrapper), arguments
                ),
            ),
        ):
            box.check_backend(feature="launch")
            path, source = workspace(
                json.loads((root / "fixture-ids.json").read_text())[0]
            )
            original = box.read_json(path.parent / "imports" / (LEGACY_ID + ".json"))
            with box.workspace_lock(path):
                for kind in ("full", "portable"):
                    with crypto.opened(
                        root / (kind + ".ocbox"), [root / "age-identity.txt"]
                    ) as (manifest, _, _):
                        backups.validate_manifest(box, manifest)
                        if (
                            manifest["kind"] != kind
                            or manifest["source_workspace_id"] != source["id"]
                            or manifest["project"] != source["project"]
                            or manifest["sessions"].get("last") != LEGACY_ID
                        ):
                            raise ValueError(
                                "Retained backup does not match the recorded downgrade fixture"
                            )
                (root / "config/opencode/opencode.json").write_text(
                    json.dumps(fixture_config(server.server_port))
                )
                with vault.locked(vault.vault_path(), create=True):
                    vault.write_encrypted(
                        vault.vault_path(), {KEY: state["key"]}, password
                    )
                credentials.configure(source, mode="selected", secrets=[KEY])
                box.atomic_json(path, source)
            phase = {"value": "baseline"}

            def check_session(_command):
                _, current = workspace(source["id"])
                actual = box.export_container_session(current, LEGACY_ID)
                if current["opencode_version"].startswith("1."):
                    verify_v1_history(actual, original)
                    return 0
                if "V1_HISTORY_RETAINED" not in json.dumps(
                    actual
                ) or "HOST_V2_ACCEPTANCE_OK" not in json.dumps(actual):
                    raise AssertionError(
                        "Expected existing migrated/native V2 history is missing"
                    )
                if phase["value"] == "baseline":
                    history["v2"] = actual
                else:
                    verify_v2_progress(actual, history["v2"])
                    state["markers"].clear()
                    result = run_fixture_session(
                        current, LEGACY_ID, state, server.server_port
                    )
                    if (
                        state["errors"]
                        or state["markers"] != {"VAULT_SHELL_OK", "VAULT_MCP_OK"}
                        or "HOST_V2_ACCEPTANCE_OK" not in result.stdout
                    ):
                        raise AssertionError(
                            "Returning V2 native provider/shell/MCP check failed"
                        )
                    verify_v2_progress(
                        box.export_container_session(current, LEGACY_ID), history["v2"]
                    )
                verify = "import pathlib,os,sys; assert pathlib.Path(sys.argv[1],'unfinished.txt').read_text()=='PROJECT_RETAINED\\n'; assert pathlib.Path(os.environ['HOME'],'acceptance-home.txt').read_text()=='HOME_RETAINED\\n'; assert not pathlib.Path('/etc/oc-box-v2-fixture').exists()"
                box.run(
                    box.exec_command(
                        current,
                        ["python3", "-c", verify, box.container_project(current)],
                    ),
                    input="",
                    capture=True,
                    private=True,
                    timeout=30,
                )
                return 0

            with (
                patch.object(vault, "hidden", return_value=password),
                patch.object(box, "run_tui", side_effect=check_session),
            ):
                (root / "host-major").write_text("2")
                box.main(["start", "--workspace", source["id"]])
                current = workspace(source["id"])[1]
                stopped_container(current)
                v2_before = digest_home(current, ".local/share/opencode/oc-box-v2.db")
                phase["value"] = "downgrade"
                (root / "host-major").write_text("1")
                box.main(["start", "--workspace", source["id"]])
                current = workspace(source["id"])[1]
                stopped_container(current)
                if (
                    digest_home(current, ".local/share/opencode/oc-box-v2.db")
                    != v2_before
                ):
                    raise AssertionError(
                        "V1 downgrade wrote to the retained V2 database"
                    )
                print(
                    "PASS: Native V1 downgrade preserves exact seed message/part history; V2 database bytes unchanged.",
                    flush=True,
                )
                phase["value"] = "return"
                v1_before_return = digest_home(
                    current, ".local/share/opencode/opencode.db"
                )
                (root / "host-major").write_text("2")
                box.main(["start", "--workspace", source["id"]])
                current = workspace(source["id"])[1]
                stopped_container(current)
                if (
                    digest_home(current, ".local/share/opencode/opencode.db")
                    != v1_before_return
                ):
                    raise AssertionError(
                        "Returning V2 wrote to the retained V1 database"
                    )
                print(
                    "PASS: Return to V2 preserves all pre-downgrade native messages and executes synthetic provider/shell/MCP tools.",
                    flush=True,
                )
            if any(
                crypto.digest(root / name) != digest
                for name, digest in archives.items()
            ):
                raise AssertionError("Retained encrypted backup evidence changed")
            result = {
                "status": "passed",
                "scope": "Targeted native V1 downgrade and V2 return on the retained reset/restored fixture; earlier full/portable restore/reset results are separate host evidence, not repeated by this command.",
                "checks": [
                    "V1 identity and full seed transcript exactly retained",
                    "V1 does not write V2 database bytes",
                    "V2 return does not write V1 database bytes",
                    "All existing V2 messages exactly preserved after return",
                    "Native synthetic provider/shell/MCP execution after return",
                    "Retained project/home/reset OS state",
                    "Encrypted full/portable backups unchanged",
                ],
                "fixture_ids": json.loads((root / "fixture-ids.json").read_text()),
                "source_version": get_version_info(APP)["version"],
                "native_versions": dict(NATIVE_VERSIONS),
                "encrypted_backups": archives,
            }
            report = root / "downgrade-result.json"
            with report.open("x") as output:
                json.dump(result, output, indent=2)
                output.write("\n")
            report.chmod(0o600)
            print(
                "Downgrade acceptance retry passed. Report: " + str(report), flush=True
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def retry_full_restore(root):
    """Retry the retained full backup without regenerating/changing its source."""
    root, _, _, _ = resume_fixture(root, full_restore=True)
    archive, identity = root / "full.ocbox", root / "age-identity.txt"
    before = crypto.digest(archive)
    with (
        patch.dict(os.environ, isolated_env(root), clear=True),
        patch.object(vault, "vault_path", return_value=root / "vault/vault.json"),
        patch.object(Path, "home", return_value=root / "host-home"),
    ):
        ident = json.loads((root / "fixture-ids.json").read_text())[0]
        path, data = workspace(ident)
        with box.workspace_lock(path):
            stopped_container(data)
            # Read filesystem capacity only; no predictable temporary file.
            default_image_tmp = "/var/tmp"  # nosec B108
            image_tmp = Path(os.environ.get("TMPDIR", default_image_tmp))
            for label, location in (
                ("fixture image storage", root / "data/containers/storage"),
                ("Podman temporary image storage", image_tmp),
                ("encrypted-component staging", Path(tempfile.gettempdir())),
            ):
                info = os.statvfs(location)
                print(
                    f"oc-box fixture: {label}: available {info.f_bavail * info.f_frsize // (1024 * 1024)} MiB, free inodes {info.f_favail if info.f_files > 0 else 'not reported'} (does not measure quota headroom)",
                    flush=True,
                )
            with crypto.opened(archive, [identity]) as (manifest, encrypted, private):
                backups.validate_manifest(box, manifest)
                if (
                    manifest["kind"] != "full"
                    or manifest["source_workspace_id"] != ident
                    or manifest["project"] != data["project"]
                ):
                    raise ValueError(
                        "Encrypted full backup does not match the retained fixture source"
                    )
                with box.loading_stage(
                    "Authenticating complete encrypted image component"
                ):
                    crypto.decrypt_stream(
                        encrypted / "image.age",
                        private,
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            "import sys; reader=sys.stdin.buffer; data=reader.read(1048576)\nwhile data: data=reader.read(1048576)",
                        ],
                    )
            print(
                "PASS: Complete image.age decrypted/authenticated without plaintext staging; retrying unchanged full backup.",
                flush=True,
            )
            backups.restore_backup(
                box,
                argparse.Namespace(
                    project=root / "restored-full", file=archive, identity=[identity]
                ),
            )
            restored_path, restored = box.find_workspace(root / "restored-full")
            if restored_path is None or restored is None:
                raise AssertionError("Retried full restore was not registered")
            recorded = json.loads((root / "fixture-ids.json").read_text())
            recorded.append(restored["id"])
            (root / "fixture-ids.json").write_text(json.dumps(recorded))
            stopped_container(restored)
            if (
                box.session_state(restored_path)["last"] != LEGACY_ID
                or box.container_project(restored) != data["project"]
            ):
                raise AssertionError(
                    "Retried restore did not preserve selected session/internal path"
                )
    if crypto.digest(archive) != before:
        raise AssertionError("The retained encrypted backup was changed")
    result = {
        "status": "restored",
        "scope": "Full archive authentication and stopped restore; native relaunch, portable restore, reset/downgrade still require the complete host fixture",
        "workspace_id": restored["id"],
        "encrypted_backup": before,
        "version": get_version_info(APP)["version"],
    }
    (root / "full-restore-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        "PASS: Retained full backup restored; source/archive unchanged. Report: "
        + str(root / "full-restore-result.json"),
        flush=True,
    )


def run(root, v1, v2, distro, *, resume=False):
    wrapper = prepare_native_cli(root, v1, v2, resume=resume)
    state = {
        "key": "fixture-" + secrets.token_urlsafe(24),
        "markers": set(),
        "requests": 0,
        "errors": [],
    }
    password = secrets.token_urlsafe(24)
    ids = []
    reports = []
    source = root / "source"
    source.mkdir(exist_ok=resume)
    (root / "host-home").mkdir(exist_ok=True)
    config = root / "config/opencode"
    config.mkdir(parents=True, exist_ok=resume)
    settings = root / "config/oc-box"
    settings.mkdir(exist_ok=resume)
    (settings / "config.json").write_text(json.dumps({"default_distro": distro}))
    if not resume:
        storage_config(root)
        (root / "fixture-ids.json").write_text("[]")
    server, worker = make_provider(state)
    try:
        with (
            patch.dict(os.environ, isolated_env(root), clear=True),
            patch.object(vault, "vault_path", return_value=root / "vault/vault.json"),
            patch.object(Path, "home", return_value=root / "host-home"),
            patch.object(
                box,
                "detect_opencode_release",
                side_effect=lambda: compat.version(
                    box.run(
                        [str(wrapper), "--version"],
                        capture=True,
                        private=True,
                        stdin=subprocess.DEVNULL,
                        timeout=15,
                    ).stdout
                ),
            ),
            patch.object(
                box,
                "host_command",
                side_effect=lambda data, *arguments: compat.command(
                    data.get("opencode_version", "1.0.0"), str(wrapper), arguments
                ),
            ),
        ):
            box.check_backend(feature="launch")
            crypto.tools()
            vault.crypto()
            config_value = fixture_config(server.server_port)
            (config / "opencode.json").write_text(json.dumps(config_value))
            if resume:
                ident = json.loads((root / "fixture-ids.json").read_text())[0]
                path, data = workspace(ident)
            else:
                path, data = box.create_workspace(source, distro)
            ids.append(data["id"])
            (root / "fixture-ids.json").write_text(json.dumps(ids))
            (source / "unfinished.txt").write_text("PROJECT_RETAINED\n")

            def snapshot_current(_command):
                current_path, current = workspace(active["id"])
                if current["opencode_version"].startswith("1."):
                    if active["stage"] == "seed-v1":
                        box.import_host_session(
                            current_path, current, legacy_session(source)
                        )
                    transcript = box.export_container_session(current, LEGACY_ID)
                    verify_v1_history(
                        transcript, active.get("v1_history", legacy_session(source))
                    )
                    if active["stage"] == "seed-v1":
                        active["v1_history"] = transcript
                    return 0
                session = workspace_service.live_connection()
                if not session:
                    raise AssertionError("V2 did not create a private foreground owner")
                if active.get("required_v2"):
                    prior = box.export_container_session(current, LEGACY_ID)
                    verify_v2_progress(prior, active["required_v2"])
                session_id = LEGACY_ID
                state["markers"].clear()
                native_run = run_fixture_session(
                    current, session_id, state, server.server_port
                )
                if (
                    state["errors"]
                    or state["markers"] != {"VAULT_SHELL_OK", "VAULT_MCP_OK"}
                    or "HOST_V2_ACCEPTANCE_OK" not in native_run.stdout
                ):
                    raise AssertionError(
                        "Synthetic V2 provider/shell/MCP credential check failed: "
                        + ", ".join(state["errors"])
                    )
                transcript = box.export_container_session(current, LEGACY_ID)
                if "V1_HISTORY_RETAINED" not in json.dumps(
                    transcript
                ) or "HOST_V2_ACCEPTANCE_OK" not in json.dumps(transcript):
                    raise AssertionError("Migrated or native V2 history missing")
                # Real attach validates identity and reuses the owner while the
                # selected values are unlocked; no secondary prompt is allowed.
                with patch.object(
                    vault,
                    "hidden",
                    side_effect=AssertionError("Attach attempted vault unlock"),
                ):
                    with workspace_service.attach(
                        box, current_path, current
                    ) as attached:
                        if attached["session"] != session_id:
                            raise AssertionError(
                                "Attached client selected another session"
                            )
                        workspace_service.api(
                            box, current, attached, "GET", "/api/info"
                        )
                if active["stage"] == "source-v2":
                    sentinel = "import os,pathlib; pathlib.Path(os.environ['HOME'],'acceptance-home.txt').write_text('HOME_RETAINED\\n')"
                    box.run(
                        box.exec_command(current, ["python3", "-c", sentinel]),
                        capture=True,
                        private=True,
                        timeout=30,
                    )
                    box.run(
                        box.exec_command(
                            current,
                            [
                                "python3",
                                "-c",
                                "import pathlib; pathlib.Path('/etc/oc-box-v2-fixture').write_text('OS_RETAINED\\n')",
                            ],
                        ),
                        capture=True,
                        private=True,
                        timeout=30,
                    )
                if active["stage"] in (
                    "restored-full",
                    "restored-portable",
                    "reset-v2",
                    "return-v2",
                ):
                    if box.container_project(current) != str(source):
                        raise AssertionError("Restored internal project path changed")
                    verify = "import pathlib,sys,os; assert pathlib.Path(sys.argv[1],'unfinished.txt').read_text()=='PROJECT_RETAINED\\n'; assert pathlib.Path(os.environ['HOME'],'acceptance-home.txt').read_text()=='HOME_RETAINED\\n'; assert pathlib.Path('/etc/oc-box-v2-fixture').exists()==(sys.argv[2]=='full')"
                    box.run(
                        box.exec_command(
                            current,
                            [
                                "python3",
                                "-c",
                                verify,
                                box.container_project(current),
                                "full"
                                if active["stage"] == "restored-full"
                                else "portable",
                            ],
                        ),
                        capture=True,
                        private=True,
                        timeout=30,
                    )
                if active["stage"] not in ("restored-full", "restored-portable"):
                    active["source_v2"] = transcript
                return 0

            active = {"id": data["id"], "stage": "seed-v1"}
            if not resume:
                with patch.object(box, "run_tui", side_effect=snapshot_current):
                    box.main(["start", "--workspace", data["id"]])
            path, data = workspace(data["id"])
            stopped_container(data)
            original_v1_digest = digest_home(data, ".local/share/opencode/opencode.db")
            reports.append("Native V1 seeded session and stopped lifecycle")
            # Create only the isolated encrypted vault; synthetic values stay
            # in local Python variables, never host environment or argv.
            with vault.locked(vault.vault_path(), create=True):
                vault.write_encrypted(vault.vault_path(), {KEY: state["key"]}, password)
            credentials.configure(data, mode="selected", secrets=[KEY])
            box.atomic_json(path, data)
            (root / "host-major").write_text("2")
            with (
                patch.dict(os.environ, {UNSELECTED: "synthetic-unselected"}),
                patch.object(vault, "hidden", return_value=password) as hidden,
                patch.object(box, "run_tui", side_effect=snapshot_current),
            ):
                for stage in ("source-v2", "rotate-v2"):
                    active["stage"] = stage
                    if stage == "rotate-v2":
                        state["key"] = "fixture-" + secrets.token_urlsafe(24)
                        with vault.locked(vault.vault_path()):
                            vault.write_encrypted(
                                vault.vault_path(), {KEY: state["key"]}, password
                            )
                    box.main(["start", "--workspace", active["id"]])
                    stopped_container(workspace(active["id"])[1])
                    if KEY in os.environ or credentials.has_unlocked_values():
                        raise AssertionError(
                            "Vault launch values leaked into host environment or remained unlocked"
                        )
                if hidden.call_count != 2:
                    raise AssertionError(
                        "Expected exactly one vault unlock per owning V2 launch"
                    )
            reports.append(
                "Selected synthetic vault provider/shell/MCP delivery, rotation and attach without unlock"
            )
            path, data = workspace(active["id"])
            if (
                digest_home(data, ".local/share/opencode/opencode.db")
                != original_v1_digest
            ):
                raise AssertionError("V2 changed retained V1 database bytes")
            database_digests = {
                name: digest_home(data, name)
                for name in (
                    ".local/share/opencode/opencode.db",
                    ".local/share/opencode/oc-box-v2.db",
                    "acceptance-home.txt",
                )
            }
            identity = root / "age-identity.txt"
            identity.write_bytes(crypto.small(["age-keygen"]))
            identity.chmod(0o600)
            recipient = (
                crypto.small(["age-keygen", "-y", str(identity)]).decode().strip()
            )
            for portable in (False, True):
                kind = "portable" if portable else "full"
                output = root / (kind + ".ocbox")
                with patch.object(
                    vault, "hidden", side_effect=AssertionError("Backup unlocked vault")
                ):
                    backups.make_backup(
                        box,
                        argparse.Namespace(
                            project=source,
                            workspace=None,
                            output=output,
                            portable=portable,
                            recipient=[recipient],
                        ),
                    )
                    destination = root / ("restored-" + kind)
                    backups.restore_backup(
                        box,
                        argparse.Namespace(
                            project=destination, file=output, identity=[identity]
                        ),
                    )
                restored_path, restored = box.find_workspace(destination)
                assert restored_path is not None and restored is not None
                ids.append(restored["id"])
                (root / "fixture-ids.json").write_text(json.dumps(ids))
                if restored["opencode_version"] != "2.2.6":
                    raise AssertionError(
                        "Restore did not remain stopped or preserve V2 release"
                    )
                stopped_container(restored)
                if (
                    box.session_state(restored_path)["last"] != LEGACY_ID
                    or restored["id"] == data["id"]
                    or box.container_project(restored) != str(source)
                ):
                    raise AssertionError(
                        "Restore identity/session/internal-path verification failed"
                    )
                if any(
                    digest_home(restored, name) != digest
                    for name, digest in database_digests.items()
                ):
                    raise AssertionError(
                        "Restored private-home/database bytes differ from source"
                    )
                active.update(
                    id=restored["id"],
                    stage="restored-" + kind,
                    required_v2=active["source_v2"],
                )
                with (
                    patch.object(vault, "hidden", return_value=password),
                    patch.object(box, "run_tui", side_effect=snapshot_current),
                ):
                    box.main(["start", "--workspace", active["id"]])
                reports.append(
                    "Native V2 "
                    + kind
                    + " backup/restore: fresh IDs, stopped restore, byte-exact databases, internal paths, sessions and OS-state differences"
                )
            active.update(
                id=data["id"], stage="reset-v2", required_v2=active["source_v2"]
            )
            before_reset = stopped_container(data)["Id"]
            with patch.object(vault, "hidden", return_value=password + "-wrong"):
                try:
                    box.main(["reset", "--workspace", data["id"]])
                except box.BoxError as exc:
                    if "Unable to unlock" not in str(exc):
                        raise
                else:
                    raise AssertionError(
                        "Reset accepted the wrong synthetic vault password"
                    )
            if stopped_container(data)["Id"] != before_reset:
                raise AssertionError("Failed vault unlock deleted the source container")
            with (
                patch.object(vault, "hidden", return_value=password),
                patch.object(box, "run_tui", side_effect=snapshot_current),
            ):
                box.main(["reset", "--workspace", data["id"]])
            stopped_container(workspace(data["id"])[1])
            reports.append(
                "Wrong vault password preserves resources; native V2 reset retains project/home/history and rebuilds OS"
            )
            (root / "host-major").write_text("1")
            v2_before_downgrade = digest_home(
                workspace(data["id"])[1], ".local/share/opencode/oc-box-v2.db"
            )
            active["stage"] = "downgrade-v1"
            with (
                patch.object(vault, "hidden", return_value=password),
                patch.object(box, "run_tui", side_effect=snapshot_current),
            ):
                box.main(["start", "--workspace", data["id"]])
            stopped_container(workspace(data["id"])[1])
            _, current = box.find_workspace(workspace_id=data["id"])
            assert current is not None
            if (
                digest_home(current, ".local/share/opencode/oc-box-v2.db")
                != v2_before_downgrade
            ):
                raise AssertionError("V1 downgrade wrote to the retained V2 database")
            v1_before_return = digest_home(current, ".local/share/opencode/opencode.db")
            (root / "host-major").write_text("2")
            active["stage"] = "return-v2"
            active["required_v2"] = active["source_v2"]
            with (
                patch.object(vault, "hidden", return_value=password),
                patch.object(box, "run_tui", side_effect=snapshot_current),
            ):
                box.main(["start", "--workspace", data["id"]])
            stopped_container(workspace(data["id"])[1])
            if (
                digest_home(
                    workspace(data["id"])[1], ".local/share/opencode/opencode.db"
                )
                != v1_before_return
            ):
                raise AssertionError("Returning V2 wrote to the retained V1 database")
            reports.append(
                "Native V1 downgrade and V2 return preserve both histories without reseeding"
            )
            if (source / box.MARKER).read_text().strip() != data["id"]:
                raise AssertionError("Source workspace was replaced")
            for report in reports:
                print("PASS: " + report, flush=True)
            result = {
                "status": "passed",
                "checks": reports,
                "fixture_ids": ids,
                "source_version": get_version_info(APP)["version"],
                "native_versions": dict(NATIVE_VERSIONS),
                "synthetic_provider_requests": state["requests"],
                "limits": "Synthetic native provider/MCP; TUI and vault prompt input scripted; real rootless lifecycle/storage and credential selection/decryption.",
            }
            (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(
                "Acceptance fixture passed. Report: " + str(root / "result.json"),
                flush=True,
            )
            print(
                "Cleanup fixture resources only: python3 tools/test_v2_podman.py --cleanup "
                + str(root),
                flush=True,
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opencode-v1",
        type=Path,
        help="separate unmodified OpenCode 1.19.5 executable",
    )
    parser.add_argument(
        "--opencode-v2", type=Path, help="separate unmodified OpenCode 2.2.6 executable"
    )
    parser.add_argument("--distro", choices=tuple(box.DISTROS), default="tumbleweed")
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="existing parent for a NEW private fixture, including its isolated Podman image store; use a disk-backed filesystem with adequate quota",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        help="existing parent for private Podman temporary images and Python encrypted staging, usable with new runs or retries; defaults inside fixture",
    )
    parser.add_argument("--cleanup", type=Path)
    parser.add_argument(
        "--retry-downgrade",
        type=Path,
        help="verify exact retained V1 transcript and V2 return on a stopped three-workspace fixture after successful restore/reset stages; preserve existing archives",
    )
    parser.add_argument(
        "--retry-full-restore",
        type=Path,
        help="authenticate and retry a retained full backup into its still-absent restored-full destination without regenerating the backup",
    )
    parser.add_argument(
        "--resume",
        type=Path,
        help="retry a retained stopped source fixture before any backup/restore, reusing its native executables and container cache",
    )
    args = parser.parse_args(argv)
    if os.environ.get("OC_BOX_CONTAINER") == "1" or os.geteuid() == 0:
        parser.exit(
            1,
            "Run this fixture from a regular HOST terminal, not sudo or inside oc-box.\n",
        )
    if args.cleanup:
        if (
            args.resume
            or args.retry_full_restore
            or args.retry_downgrade
            or args.work_dir
            or args.temp_dir
        ):
            parser.error("--cleanup cannot be combined with retry or placement options")
        cleanup(args.cleanup)
        return 0
    if args.retry_downgrade:
        if (
            args.resume
            or args.retry_full_restore
            or args.opencode_v1
            or args.opencode_v2
            or args.work_dir
        ):
            parser.error(
                "--retry-downgrade reuses recorded fixture resources and cannot be combined with another retry/executable/work option"
            )
        try:
            root, _, _, _ = resume_fixture(args.retry_downgrade, downgrade=True)
            with temporary_location(root, args.temp_dir):
                retry_downgrade(root)
        except BaseException:
            print(
                "Fixture and encrypted backups retained for diagnosis: "
                + str(args.retry_downgrade),
                file=sys.stderr,
            )
            raise
        return 0
    if args.retry_full_restore:
        if args.resume or args.opencode_v1 or args.opencode_v2 or args.work_dir:
            parser.error(
                "--retry-full-restore reuses fixture/archive metadata and cannot be combined with other retry/executable options"
            )
        try:
            # Validate the old fixture/evidence before making a temporary dir.
            root, _, _, _ = resume_fixture(args.retry_full_restore, full_restore=True)
            with temporary_location(root, args.temp_dir):
                retry_full_restore(root)
        except BaseException:
            print(
                "Fixture and encrypted backup retained for diagnosis: "
                + str(args.retry_full_restore),
                file=sys.stderr,
            )
            raise
        return 0
    if args.resume:
        if args.opencode_v1 or args.opencode_v2 or args.work_dir:
            parser.error(
                "--resume reuses the recorded native executables and image store; do not supply executable paths or --work-dir"
            )
        root, v1, v2, distro = resume_fixture(args.resume)
        print("Retrying retained V2 source fixture: " + str(root), flush=True)
        try:
            with temporary_location(root, args.temp_dir):
                run(root, v1, v2, distro, resume=True)
        except BaseException:
            print("Fixture retained for diagnosis: " + str(root), file=sys.stderr)
            raise
        return 0
    if not args.opencode_v1 or not args.opencode_v2:
        parser.error("--opencode-v1 and --opencode-v2 are required for a new fixture")
    v1, v2 = (
        args.opencode_v1.resolve(strict=True),
        args.opencode_v2.resolve(strict=True),
    )
    if not all(os.access(binary, os.X_OK) for binary in (v1, v2)):
        parser.error("Both native executables must be executable")
    work_dir = existing_directory(args.work_dir) if args.work_dir is not None else None
    if args.temp_dir is not None:
        existing_directory(args.temp_dir)
    root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=work_dir))
    root.chmod(0o700)
    print("Isolated V2 acceptance fixture: " + str(root), flush=True)
    try:
        with temporary_location(root, args.temp_dir):
            run(root, v1, v2, args.distro)
    except BaseException:
        print("Fixture retained for diagnosis: " + str(root), file=sys.stderr)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
