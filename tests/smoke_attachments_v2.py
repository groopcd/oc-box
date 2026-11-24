"""Bounded native image-attachment integration, not host/Podman acceptance.

Run from the checkout root, with TMPDIR set if desired:
python3 -B tests/smoke_attachments_v2.py --opencode /path/to/opencode-v2

Only fresh private synthetic files, stdio-owned mainline servers and the existing
canned loopback provider are used. Broker, MCP, Export, sharing.serve,
transport_source and attachment_io remain production implementations. Host
consent is an explicit test callback; it is not interactive host-UI acceptance.
The only read wrapper observes calls before delegating to the real Export.read.
No Podman, real credentials/provider, clipboard, TUI parsing or host files.
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlencode
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from oc_box import attachment_io, attachments, sharing
from oc_box import oc_box as box
from oc_box import opencode_compat as compat
from oc_box import opencode_v2_io as bridge
from smoke_session_sync_v2 import bounded, private_environment, seed
from smoke_sharing_v2 import Failure, PNG, canned_provider, nodes, require, wait


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def source_hashes():
    names = (
        "tests/smoke_attachments_v2.py",
        "tests/smoke_sharing_v2.py",
        "tests/smoke_session_sync_v2.py",
        "src/oc_box/attachments.py",
        "src/oc_box/attachment_io.py",
        "src/oc_box/sharing.py",
        "src/oc_box/session_terminal.py",
        "src/oc_box/opencode_v2_io.py",
        "src/oc_box/opencode_compat.py",
        "src/oc_box/oc_box.py",
    )
    return {name: digest(REPO / name) for name in names}


def source_identity():
    def git(*arguments):
        return subprocess.check_output(
            ["git", *arguments],
            cwd=REPO,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()

    commit, count = git("rev-parse", "HEAD"), git("rev-list", "--count", "HEAD")
    require(
        len(commit) == 40
        and all(character in "0123456789abcdef" for character in commit)
        and count.isdigit(),
        "source-identity",
    )
    dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    return commit, count, dirty


def create_session(native, project, title):
    return native(
        "POST",
        "/api/session",
        {
            "location": {"directory": str(project)},
            "title": title,
            "agent": "build",
            "model": {"providerID": "fixture", "id": "probe"},
            "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        },
    )["data"]["id"]


def invoke(native, session, state, code, *, inbox_id=None):
    """One Code Mode invocation in a fresh, explicitly chosen native session."""
    state["code"], state["results"] = code, []
    if inbox_id is None:
        native(
            "POST",
            "/api/session/" + session + "/prompt",
            {"text": "Run the synthetic attachment check."},
        )
    else:
        native(
            "PATCH",
            "/api/session/" + session + "/inbox/" + inbox_id,
            {"delivery": "steer"},
        )

    def done():
        require(not state["failures"], "provider")
        messages = native(
            "GET", "/api/session/" + session + "/message?limit=100&order=asc"
        )["data"]
        complete = any(
            item.get("type") == "assistant" and item.get("time", {}).get("completed")
            for item in messages
        )
        return (
            messages
            if complete and session not in native("GET", "/api/session/active")["data"]
            else None
        )

    messages = wait(done, "native-session-timeout")
    require(len(state["results"]) == 1, "native-tool-result")
    return state["results"][0], messages


def image_blocks(value):
    return [
        item
        for item in nodes(value)
        if item.get("type") == "file" and item.get("mime") == "image/png"
    ]


@contextlib.contextmanager
def relay(native, project, env, name, protocol):
    """Real native WebSocket and production pipe pump; no container boundary."""
    ready, finished, failures = threading.Event(), threading.Event(), []
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        sharing.transport_source(box),
        native.endpoint["url"],
        str(project),
        name,
    ]
    process = subprocess.Popen(
        command,
        cwd=project,
        env={**env, "OPENCODE_PASSWORD": native.endpoint["password"]},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def serve():
        try:

            def activate():
                require(process.poll() is None, "broker-relay-exited")
                protocol.activate()
                ready.set()

            sharing.serve(process, None, protocol=protocol, ready=activate)
        except Exception:
            failures.append("broker-serve")
        finally:
            finished.set()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    completed = False
    try:
        wait(lambda: ready.is_set() or finished.is_set(), "broker-readiness", 35)
        require(ready.is_set() and not failures, "broker-readiness")
        yield
        completed = True
    finally:
        # A real pipe EOF tears down the native client-MCP connection. No
        # reconnecting SDK/helper is used, and no new grants are offered here.
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            failures.append("relay-stop")
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        worker.join(5)
        process.stdout.close()
        if completed:
            require(
                not worker.is_alive() and not failures and process.returncode == 0,
                "relay-shutdown",
            )


class ObservedProtocol:
    """Observe genuine native metadata; every reply is the production MCP's."""

    def __init__(self, protocol, reads, denied):
        self.protocol, self.reads, self.denied = protocol, reads, denied
        self.calls = []
        self.denied_without_read = False

    @property
    def catalogued(self):
        return self.protocol.catalogued

    def activate(self):
        self.protocol.activate()

    def reply(self, request):
        params = request.get("params", {})
        tool = request.get("method") == "tools/call"
        before = len(self.reads)
        if tool:
            self.calls.append(
                (
                    params.get("name"),
                    params.get("_meta", {}).get("ai.opencode/sessionID"),
                    frozenset(params.get("arguments", {})),
                )
            )
        response = self.protocol.reply(request)
        if (
            tool
            and params.get("name") == "request_image"
            and params.get("arguments") == {"path": str(self.denied)}
            and response.get("result", {}).get("isError") is True
        ):
            require(len(self.reads) == before, "denied-host-read")
            self.denied_without_read = True
        return response


def check_target(native, session, project, release, *, broker=False):
    return attachment_io.deliver(
        attachment_io.connection(native.endpoint["url"], native.endpoint["password"]),
        {
            "operation": "broker-check" if broker else "check",
            "session": session,
            "directory": str(project),
            "version": release,
        },
    )


def broker_cases(native, env, state, project, root, release):
    caller = create_session(native, project, "Synthetic attachment caller")
    outsider = create_session(native, project, "Synthetic unrelated session")
    revoked = create_session(native, project, "Synthetic disconnected broker")
    detached_root = root / "selected-then-deleted"
    detached_root.mkdir()
    detached = detached_root / "Screenshot.png"
    approved, denied = root / "approved.png", root / "denied.png"
    for path in (detached, approved, denied):
        path.write_bytes(base64.b64decode(PNG))

    reads, approvals = [], []
    original_read = sharing.Export.read

    def observed_read(export, *arguments, **options):
        reads.append(export.path)
        return original_read(export, *arguments, **options)

    def consent(path):
        require(path in (approved, denied), "unexpected-host-selection")
        approvals.append(path)
        return path == approved

    name = "oc_box_attachments_test_" + secrets.token_hex(6)
    query = "?" + urlencode({"location[directory]": str(project)})
    with (
        attachments.Broker() as broker,
        patch.object(sharing.Export, "read", new=observed_read),
    ):
        token = broker.offer(
            detached, caller, consent=lambda selected: selected == detached
        )
        require(len(reads) == 1, "host-snapshot-read")
        detached.unlink()
        detached_root.rmdir()
        require(not detached_root.exists(), "snapshot-root-deletion")
        protocol = ObservedProtocol(
            attachments.MCP(
                broker,
                caller,
                version=release,
                consent=consent,
                check=lambda: check_target(
                    native, caller, project, release, broker=True
                ),
            ),
            reads,
            denied,
        )
        with relay(native, project, env, name, protocol):
            args = json.dumps({"id": token})
            fake = json.dumps(
                {
                    "id": token,
                    "_meta": {"ai.opencode/sessionID": caller},
                    "sessionID": caller,
                }
            )
            wrong_code = f"""
const failures = [];
for (const args of [{args}, {fake}]) {{
  try {{
    await tools.{name}.read_attachment(args);
    failures.push("UNEXPECTED_IMAGE");
  }} catch (error) {{
    failures.push(String(error));
  }}
}}
return failures;
"""
            result, messages = invoke(native, outsider, state, wrong_code)
            failures = json.loads(result["text"])
            require(
                len(failures) == 2
                and "This image broker belongs to another session." in failures[0]
                and all("UNEXPECTED_IMAGE" not in item for item in failures)
                and not result["image"]
                and not image_blocks(messages)
                and token in broker.grants
                and len(reads) == 1
                and approvals == [],
                "native-session-isolation",
            )
            # At least the schema-valid request must cross real ClientMCP with
            # its native caller identity. Extra fake context may be rejected by
            # native argument validation before reaching the broker; neither
            # outcome may consume a grant or return an image.
            require(
                protocol.calls and all(item[1] == outsider for item in protocol.calls),
                "native-session-metadata",
            )
            caller_code = f"""
let denied = false;
try {{
  await tools.{name}.request_image({json.dumps({"path": str(denied)})});
}} catch (error) {{
  denied = String(error).includes("Host image attachment was not authorized.");
}}
const grant = await tools.{name}.request_image({json.dumps({"path": str(approved)})});
const granted = typeof grant.id === "string" && grant.expires_in === 120;
await tools.{name}.read_attachment({{id: grant.id}});
let consumed = false;
try {{
  await tools.{name}.read_attachment({{id: grant.id}});
}} catch (error) {{
  consumed = String(error).includes("Attachment grant is unavailable for this session.");
}}
await tools.{name}.read_attachment({args});
return {{denied, granted, consumed, snapshot: true}};
"""
            result, messages = invoke(native, caller, state, caller_code)
            require(
                json.loads(result["text"])
                == {"denied": True, "granted": True, "consumed": True, "snapshot": True}
                and result["image"]
                and image_blocks(messages)
                and not broker.grants
                and len(reads) == 2
                and approvals == [denied, approved]
                and protocol.denied_without_read
                and not detached_root.exists(),
                "native-approved-snapshot",
            )
            require(
                any(
                    item[1] == caller and item[0] == "request_image"
                    for item in protocol.calls
                )
                and any(
                    item[1] == caller and item[0] == "read_attachment"
                    for item in protocol.calls
                )
                and all(item[1] in (caller, outsider) for item in protocol.calls),
                "native-caller-metadata",
            )

        wait(
            lambda: all(
                item["name"] != name
                for item in native("GET", "/api/mcp" + query)["data"]
            ),
            "native-broker-disconnect",
        )
        before = len(reads), list(approvals), len(protocol.calls)
        result, messages = invoke(
            native,
            revoked,
            state,
            f"""
try {{
  await tools.{name}.read_attachment({args});
  return "UNEXPECTED_IMAGE";
}} catch (error) {{
  return String(error);
}}
""",
        )
        require(
            f"Unknown tool '{name}.read_attachment'" in result["text"]
            and not result["image"]
            and not image_blocks(messages)
            and (len(reads), approvals, len(protocol.calls)) == before
            and not broker.grants,
            "native-broker-revocation",
        )
    require(
        approved.read_bytes() == denied.read_bytes() == base64.b64decode(PNG),
        "source-preservation",
    )
    print(
        "PASS: actual ClientMCP caller metadata; wrong session/fake argument context "
        "cannot consume grants; approved one-use snapshots survive source-root "
        "deletion; denied consent performs no read; EOF removes tools without regrant.",
        flush=True,
    )


def prompt(ident):
    return {
        "id": ident,
        "text": "SYNTHETIC_DIRECT_HOST_IMAGE",
        "files": [
            {
                "uri": "data:image/png;base64," + PNG,
                "source": {"type": "inline"},
                "name": "host-image.png",
            }
        ],
        "delivery": "queue",
        "resume": False,
    }


def stdin_delivery(native, env, project, value):
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(box.PACKAGE / "attachment_io.py"),
        native.endpoint["url"],
    ]
    child_env = {**env, "OPENCODE_PASSWORD": native.endpoint["password"]}
    require(
        PNG not in json.dumps([command, child_env]),
        "inline-image-forwarded-outside-stdin",
    )
    process = subprocess.Popen(
        command,
        cwd=project,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, _errors = process.communicate(json.dumps(value), timeout=40)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        raise Failure("native-image-admission-timeout") from None
    require(process.returncode == 0, "native-image-admission")
    return json.loads(output)


def direct_cases(native, env, state, project, release):
    session = create_session(native, project, "Synthetic direct image attachment")
    ident = "msg_attachment_" + secrets.token_hex(8)
    value = {
        "operation": "send",
        "session": session,
        "directory": str(project),
        "version": release,
        "prompt": prompt(ident),
    }
    before = state["requests"]
    require(
        check_target(native, session, project, release) == {"ok": True},
        "native-direct-preflight",
    )
    require(
        stdin_delivery(native, env, project, value) == {"ok": True, "message": ident},
        "native-direct-acknowledgement",
    )
    inbox = native("GET", "/api/session/" + session + "/inbox")["data"]
    require(
        len(inbox) == 1
        and inbox[0]["id"] == ident
        and inbox[0]["sessionID"] == session
        and inbox[0]["type"] == "user"
        and inbox[0]["delivery"] == "queue",
        "native-direct-inbox",
    )
    image = inbox[0]["payload"]["files"][0]
    require(
        image.get("mime") == "image/png"
        and image.get("name") == "host-image.png"
        and image.get("source") == {"type": "inline"}
        and base64.b64decode(image["data"], validate=True).startswith(
            b"\x89PNG\r\n\x1a\n"
        )
        and session not in native("GET", "/api/session/active")["data"]
        and state["requests"] == before,
        "native-direct-inline-image",
    )
    context = native("GET", "/api/session/" + session + "/context")["data"]
    require(
        all(item["id"] != ident for item in context),
        "queued-image-was-prematurely-projected",
    )
    # resume:false admits an inbox item, not a composer draft or a model stop.
    # Only this explicit native inbox promotion wakes the idle session and
    # moves the already-normalized image into native user context. Do not add a
    # competing prompt that could run a separate turn ahead of queued input.
    invoke(
        native,
        session,
        state,
        'return "DIRECT_ATTACHMENT_DRAINED"',
        inbox_id=ident,
    )
    context = native("GET", "/api/session/" + session + "/context")["data"]
    delivered = [item for item in context if item["id"] == ident]
    require(
        len(delivered) == 1
        and delivered[0]["type"] == "user"
        and delivered[0]["text"] == value["prompt"]["text"]
        and delivered[0]["files"] == [image]
        and native("GET", "/api/session/" + session + "/inbox")["data"] == [],
        "native-direct-context-image",
    )
    exported = native("GET", "/api/experimental/session/" + session + "/export")[
        "data"
    ]["messages"]
    require(
        any(item["id"] == ident and item.get("files") == [image] for item in exported),
        "native-direct-export-image",
    )
    # Compare native admission with native context/export, never promise that
    # an upstream image normalizer retains the original input bytes.
    revert = seed(native, "ses_attachments_revert", project)
    native(
        "POST",
        "/api/session/" + revert + "/revert/stage",
        {"messageID": "msg_attachments_revert", "files": False},
    )
    staged = native("GET", "/api/session/" + revert)["data"]["revert"]
    preserved = native("GET", "/api/experimental/session/" + revert + "/export")[
        "data"
    ]["messages"]
    refused = False
    try:
        attachment_io.deliver(
            attachment_io.connection(
                native.endpoint["url"], native.endpoint["password"]
            ),
            {**value, "session": revert, "prompt": prompt("msg_attachment_refused")},
        )
    except attachment_io.Refused as error:
        refused = str(error) == "session-revert"
    require(
        refused
        and native("GET", "/api/session/" + revert)["data"]["revert"] == staged
        and native("GET", "/api/session/" + revert + "/inbox")["data"] == []
        and native("GET", "/api/experimental/session/" + revert + "/export")["data"][
            "messages"
        ]
        == preserved,
        "native-staged-revert-preservation",
    )
    archived = create_session(native, project, "Synthetic archived attachment target")
    archived_path = "/api/session/" + archived
    native("PATCH", archived_path, {"archived": True})
    for operation in ("check", "broker-check", "send"):
        refused = False
        attempted = {**value, "session": archived, "operation": operation}
        if operation != "send":
            del attempted["prompt"]
        try:
            attachment_io.deliver(
                attachment_io.connection(
                    native.endpoint["url"], native.endpoint["password"]
                ),
                attempted,
            )
        except attachment_io.Refused as error:
            refused = str(error) == "session-archived"
        require(refused, "native-archived-refusal")
    require(
        native("GET", archived_path)["data"]["time"].get("archived") is not None
        and native("GET", archived_path + "/inbox")["data"] == [],
        "native-archived-preservation",
    )
    explicit = create_session(native, project, "Synthetic explicit attachment send")
    explicit_path = "/api/session/" + explicit
    wake_id = "msg_attachment_" + secrets.token_hex(8)
    state["code"], state["results"] = 'return "EXPLICIT_ATTACHMENT_RESUME"', []
    requests = state["requests"]
    wake_prompt = {**prompt(wake_id), "resume": True}
    require(
        stdin_delivery(
            native, env, project, {**value, "session": explicit, "prompt": wake_prompt}
        )
        == {"ok": True, "message": wake_id},
        "native-resume-acknowledgement",
    )

    def completed():
        require(not state["failures"], "native-resume-provider")
        context = native("GET", explicit_path + "/context")["data"]
        matches = [item for item in context if item["id"] == wake_id]
        if (
            state["results"]
            and explicit not in native("GET", "/api/session/active")["data"]
        ):
            return matches
        return None

    projected = wait(completed, "native-resume-timeout")
    require(
        len(projected) == 1
        and projected[0]["text"] == wake_prompt["text"]
        and len(projected[0]["files"]) == 1
        and projected[0]["files"][0]["mime"] == "image/png"
        and native("GET", explicit_path + "/inbox")["data"] == []
        and len(state["results"]) == 1
        and state["results"][0]["text"] == "EXPLICIT_ATTACHMENT_RESUME"
        and state["requests"] > requests,
        "native-explicit-resume",
    )
    print(
        "PASS: production stdin helper/native HTTP image queue admission without "
        "execution; explicit wake preserves native image in context/export; "
        "explicit resume starts one synthetic turn; archived and staged-revert "
        "refusals preserve history and boundary.",
        flush=True,
    )


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    host = root / "synthetic-host-selection"
    host.mkdir()
    env = private_environment(root / "native")
    release = compat.version(bounded([binary, "--version"], env, project))
    require(release in attachment_io.DIRECT_RELEASES, "native-direct-profile")
    print("Native OpenCode " + release + "; private offline attachments.", flush=True)
    with canned_provider() as (port, state):
        config = Path(env["XDG_CONFIG_HOME"]) / "opencode/opencode.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(
            json.dumps(
                {
                    "provider": {
                        "fixture": {
                            "npm": "@ai-sdk/openai-compatible",
                            "models": {
                                "probe": {
                                    "name": "Synthetic attachments",
                                    "limit": {"context": 32000, "output": 2000},
                                    "modalities": {
                                        "input": ["text", "image"],
                                        "output": ["text"],
                                    },
                                }
                            },
                            "options": {
                                "baseURL": f"http://127.0.0.1:{port}/v1",
                                "apiKey": "synthetic-only",
                            },
                        }
                    },
                    "model": "fixture/probe",
                    "lsp": False,
                }
            ),
            encoding="utf-8",
        )
        configuration = config.read_bytes()
        previous = Path.cwd()
        os.chdir(project)
        exit_info = {}
        try:
            with (
                patch.dict(os.environ, env, clear=True),
                bridge.server(binary, exit_info=exit_info) as native,
            ):
                require(
                    native("GET", "/api/credential")["data"] == []
                    and bridge.session_inventory(native, str(project)) == [],
                    "private-native-state",
                )
                broker_cases(native, env, state, project, host, release)
                direct_cases(native, env, state, project, release)
                require(
                    not state["failures"]
                    and native("GET", "/api/credential")["data"] == []
                    and native("GET", "/api/session/active")["data"] == {}
                    and config.read_bytes() == configuration,
                    "native-final-state",
                )
        finally:
            os.chdir(previous)
        require(exit_info == {"code": 0, "forced": False}, "native-owner-clean-exit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    args = parser.parse_args()

    def timed_out(*_):
        raise Failure("fixture-timeout")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(300)
    try:
        binary = args.opencode.resolve()
        identity, sources, native_digest = (
            source_identity(),
            source_hashes(),
            digest(binary),
        )
        print(
            f"SOURCE commit={identity[0]} commit_count={identity[1]} dirty={str(identity[2]).lower()}",
            flush=True,
        )
        print("NATIVE sha256=" + native_digest, flush=True)
        for name, value in sources.items():
            print(f"SOURCE sha256 {name}={value}", flush=True)
        with tempfile.TemporaryDirectory(
            prefix="oc-box-attachments-native-"
        ) as directory:
            exercise(str(binary), Path(directory).resolve())
        require(
            source_identity() == identity
            and source_hashes() == sources
            and digest(binary) == native_digest,
            "tested-source-changed",
        )
        print(
            "Native scripted synthetic integration only: NOT interactive host "
            "approval, Podman isolation/lifecycle, real-provider inference, "
            "transparent drop or screenshot rendering acceptance.",
            flush=True,
        )
        return 0
    except Failure as error:
        print("FAIL: native attachments (" + str(error) + ").", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("FAIL: native attachments (unexpected).", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    sys.exit(main())
