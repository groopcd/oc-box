#!/usr/bin/env python3
"""Opt-in, disposable HOST acceptance for V2's default global encrypted vault.

Uses repository launcher code, unmodified OpenCode 2.2.6 and a private rootless
Podman store. Provider, MCP, vault, host history, config and projects are entirely
synthetic. Only TUI exit and masked prompt input are scripted. There is no paid
inference, host installation/update, shared native service or V1 requirement.
--retry-source retries only a validated retained first source owner, using a
new companion encrypted vault; it does not repeat first-ever import acceptance.
Evidence is retained, including on failure. --cleanup removes only
verified fixture-owned containers/volumes/networks, never files or images.

Run on a regular host, not inside oc-box, with an existing disk-backed parent:
  python3 -B tools/test_global_vault_podman.py --work-dir /path/to/fixtures \
      --opencode-v2 /path/to/unmodified/opencode
"""

import argparse
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import quote

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import (
    backup_crypto as crypto,
    backups,
    credentials,
    oc_box as box,
    opencode_compat as compat,
    opencode_v2_io as bridge,
    vault,
    workspace_service,
)
from tools.test_v2_podman import (
    check_native_version,
    digest_home,
    native_env,
    storage_config,
    temporary_location,
    verify_v2_progress,
)
from oc_box.version_info import get_version_info

PREFIX = "oc-box-gv-host-"
FORMAT = "oc-box-global-vault-host-acceptance"
NATIVE_VERSION = "2.2.6"
PODMAN_CONFIGFILE_OMISSIONS = frozenset({"6.1.4"})
PROJECTS = ("source-a", "source-b", "restored-full")
KEY = "ACCEPTANCE_API_KEY"
SIDE = "ACCEPTANCE_SIDE_SECRET"
ADDED = "ACCEPTANCE_ADDED_SECRET"
AMBIENT = "ACCEPTANCE_AMBIENT_TOKEN"
PASS_ENV = "ACCEPTANCE_CUSTOM_FORWARD"
KNOWN = (KEY, SIDE, ADDED, AMBIENT, PASS_ENV)
ACTION = "ocbox_permission_probe"
TOOL_MARKER = "BUILD_PERMISSION_TOOL_COMPLETED"
SHELL_MARKER = "GLOBAL_VAULT_SHELL_OK"
MCP_MARKER = "GLOBAL_VAULT_MCP_OK"
DONE_MARKER = "GLOBAL_VAULT_NATIVE_COMPLETED"
SEED_TEXT = "GLOBAL_VAULT_SEED_RETAINED"
OS_FILE = "/etc/oc-box-global-vault-fixture"
ASK_RULES = [
    {"action": "*", "resource": "*", "effect": "ask"},
    {"action": ACTION, "resource": "session-denied", "effect": "deny"},
]

# Deliberately omits agent, exercising native current-session/default resolution.
# No secrets, external packages or fixture-driven permission approvals live here.
TOOL_SOURCE = """export default {
  id: "oc-box.fixture-global-vault-permission",
  async setup(ctx) {
    await ctx.tool.transform(editor => {
      editor.add({
        name: "ocbox_permission_probe",
        description: "Assert a synthetic permission without an agent field.",
        options: { codemode: false },
        input: {
          type: "object", properties: { resource: { type: "string" } },
          required: ["resource"], additionalProperties: false,
        },
        execute: async (input, context) => {
          await ctx.permission.assert({
            sessionID: context.sessionID,
            source: { type: "tool", messageID: context.messageID, id: context.id },
            action: "ocbox_permission_probe", resources: [input.resource],
          })
          return { content: "BUILD_PERMISSION_TOOL_COMPLETED" }
        },
      })
    })
  },
}
"""

# Expected hashes are tool input, not plaintext values or an inherited host file.
MCP = """import hashlib,json,os,sys
known=('ACCEPTANCE_API_KEY','ACCEPTANCE_SIDE_SECRET','ACCEPTANCE_ADDED_SECRET','ACCEPTANCE_AMBIENT_TOKEN','ACCEPTANCE_CUSTOM_FORWARD')
for line in sys.stdin:
    message=json.loads(line); method=message.get('method'); result={}
    if method=='initialize':
        result={'protocolVersion':message['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'global-vault-fixture','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':'check','description':'Check synthetic environment hashes and aliases without revealing values','inputSchema':{'type':'object','properties':{'expected':{'type':'object','additionalProperties':{'anyOf':[{'type':'string'},{'type':'null'}]}}},'required':['expected'],'additionalProperties':False}}]}
    elif method=='tools/call':
        expected=message['params']['arguments']['expected']; assert set(expected)==set(known)
        for name in known:
            actual=os.environ.get(name)
            assert (actual is None) if expected[name] is None else (actual is not None and hashlib.sha256(actual.encode()).hexdigest()==expected[name])
        assert os.environ['FIXTURE_MCP_KEY']==os.environ['ACCEPTANCE_API_KEY']
        result={'content':[{'type':'text','text':'GLOBAL_VAULT_MCP_OK'}]}
    if 'id' in message:
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)
"""


def stderr_category(message):
    """Return only static categories, never private error content."""
    if not isinstance(message, (str, bytes)) or not message:
        return "empty"
    text = message[:65536]
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.lower()
    if "current system boot id differs from cached boot id" in text:
        return "runtime-boot-mismatch"
    if re.search(r"\beof\b|unexpected end", text):
        return "unexpected-end"
    for terms, category in (
        (
            (
                "database configuration mismatch",
                "database is locked",
                "sqlite",
                "boltdb",
            ),
            "engine-database",
        ),
        (
            ("error parsing", "error decoding", "invalid character", "toml"),
            "invalid-data",
        ),
        (
            ("cannot re-exec", "cannot clone", "invalid internal status", "unshare"),
            "engine-namespace",
        ),
        (("conmon", "crun", "runc"), "engine-helper"),
        (
            ("unknown flag", "unknown command", "unrecognized argument"),
            "invalid-command",
        ),
        (("panic:", "fatal error:"), "engine-panic"),
        (("cgroup", "systemd"), "cgroup-runtime"),
        (("quota", "no space left", "storage"), "storage"),
        (("mount", "selinux"), "mount-or-label"),
        (("permission denied", "operation not permitted"), "permission"),
        (
            (
                "unauthorized",
                "forbidden",
                "authentication",
                "invalid api key",
                "http error 401",
                "http error 403",
            ),
            "authentication",
        ),
        (("gateway", "http error 502", "http error 503", "http error 504"), "gateway"),
        (
            (
                "connection refused",
                "name resolution",
                "getaddrinfo",
                "network is unreachable",
            ),
            "network",
        ),
        (("timed out", "timeout"), "timeout"),
        (("plugin", "npm err", "module not found", "cannot find package"), "plugin"),
        (("provider", "api key", "chat/completions"), "provider"),
        (
            ("permission.asked", "permission assertion", "permission rejected"),
            "permission",
        ),
        (("http error", "http status", "status code"), "http"),
        (("no such file", "executable", "not found"), "missing-runtime-file"),
    ):
        if any(term in text for term in terms):
            return category
    return "unclassified"


# Include the exact declared classifier in the separate container probe.
STDERR_CLASSIFIER = inspect.getsource(stderr_category)
STDERR_CATEGORIES = frozenset(
    {
        "empty",
        "unclassified",
        "runtime-boot-mismatch",
        "unexpected-end",
        "engine-database",
        "invalid-data",
        "engine-namespace",
        "engine-helper",
        "invalid-command",
        "engine-panic",
        "cgroup-runtime",
        "storage",
        "mount-or-label",
        "permission",
        "authentication",
        "gateway",
        "network",
        "timeout",
        "plugin",
        "provider",
        "http",
        "missing-runtime-file",
        "event-stream",
        "native-events",
    }
)
PROBE_STAGES = frozenset(
    {
        "native-event-barrier",
        "native-tool-client",
        "native-event-stream",
        "native-results",
    }
)
ERROR_CLASSES = frozenset(
    {
        "fixture-error",
        "box-error",
        "assertion",
        "timeout",
        "missing-file",
        "os-error",
        "invalid-data",
        "subprocess-exit",
        "interrupted",
        "exception",
    }
)


# Observe the authenticated event stream INSIDE the fixture container. Host
# /proc cannot safely interpret namespace PIDs. This runs the real native client
# with stdin EOF, no --auto, and retains private output in anonymous memory only.
NATIVE_PROBE = (
    """import base64,json,os,re,subprocess,sys,threading
from urllib.request import Request,build_opener,ProxyHandler
"""
    + STDERR_CLASSIFIER
    + """
def main():
    stage='native-event-barrier'; native_exit_code=None; category='native-events'
    try:
        url,ident=sys.argv[1:3]; command=sys.argv[3:]
        assert '--server' in command and '--auto' not in command
        assert command[command.index('--server')+1]==url
        ready=threading.Event(); done=threading.Event(); events=[]; failures=[]
        def observe():
            try:
                auth=base64.b64encode(('opencode:'+os.environ['OPENCODE_PASSWORD']).encode()).decode()
                request=Request(url+'/api/event',headers={'Authorization':'Basic '+auth,'Accept':'text/event-stream'})
                with build_opener(ProxyHandler({})).open(request,timeout=200) as response:
                    data=[]
                    for raw in response:
                        line=raw.decode().rstrip('\\r\\n')
                        if line.startswith('data:'): data.append(line[5:].lstrip())
                        elif not line and data:
                            event=json.loads('\\n'.join(data)); data=[]
                            if event['type']=='server.connected': ready.set()
                            if event.get('data',{}).get('sessionID')==ident:
                                events.append(event['type'])
                                if event['type'] in ('session.execution.succeeded','session.execution.failed','session.execution.interrupted'): break
            except Exception as error:
                failures.append(stderr_category(str(error)))
            finally:
                done.set()
        worker=threading.Thread(target=observe,daemon=True); worker.start()
        if not ready.wait(10):
            category=failures[0] if failures else 'timeout'
            raise AssertionError()
        stage='native-tool-client'
        with os.fdopen(os.memfd_create('fixture-native-output',os.MFD_CLOEXEC),'w+b') as output, os.fdopen(os.memfd_create('fixture-native-errors',os.MFD_CLOEXEC),'w+b') as errors:
            result=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=output,stderr=errors,timeout=180)
            native_exit_code=result.returncode
            if native_exit_code:
                category=stderr_category(os.pread(errors.fileno(),65536,0))
                raise subprocess.CalledProcessError(native_exit_code, [])
        stage='native-event-stream'
        category='timeout'
        assert done.wait(10); worker.join(timeout=2)
        category=failures[0] if failures else 'native-events'
        assert not failures and 'session.execution.succeeded' in events and 'permission.asked' not in events
        print(json.dumps({'completed':True,'permission_asked':0}))
        return 0
    except BaseException as error:
        error_class='exception'
        if isinstance(error, subprocess.TimeoutExpired): error_class='timeout'; category='timeout'
        elif isinstance(error, subprocess.CalledProcessError): error_class='subprocess-exit'
        elif isinstance(error, AssertionError): error_class='assertion'
        elif isinstance(error, FileNotFoundError): error_class='missing-file'; category='missing-runtime-file'
        elif isinstance(error, OSError): error_class='os-error'; category=stderr_category(str(error))
        elif isinstance(error, (ValueError, KeyError, TypeError)): error_class='invalid-data'
        elif isinstance(error, (KeyboardInterrupt, SystemExit)): error_class='interrupted'
        print(json.dumps({'stage':stage,'error_class':error_class,'native_exit_code':native_exit_code,'stderr_category':category}),file=sys.stderr)
        return 1
sys.exit(main())
"""
)


class FixtureError(Exception):
    """Only fixed, public categories may reach the terminal."""


def require(condition, category):
    if not condition:
        raise FixtureError(category)


def host_guard():
    """Must precede placement, binary probing, configuration and Podman access."""
    require(
        os.geteuid() != 0
        and os.environ.get("OC_BOX_CONTAINER") != "1"
        and not Path("/.dockerenv").exists()
        and not Path("/run/.containerenv").exists(),
        "host-only",
    )


def no_symlinks(path):
    """Reject redirects in every existing ancestor, not just the final leaf."""
    path = Path(path).absolute()
    require(".." not in path.parts, "unsafe-path")
    require(not any(char in str(path) for char in ("\0", "\r", "\n")), "unsafe-path")
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode), "unsafe-path")
        if component != path:
            require(stat.S_ISDIR(info.st_mode), "unsafe-path")
    return path


def existing_directory(path):
    path = no_symlinks(Path(path).expanduser())
    require(path.is_dir(), "placement")
    return path.resolve(strict=True)


def safe_path(root, relative, *, directory=False, optional=False):
    path = no_symlinks(root / relative)
    require(path.is_relative_to(root) and path != root, "unsafe-path")
    if optional and not path.exists():
        return path
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "unsafe-path",
    )
    return path


def private_json(root, relative):
    path = safe_path(root, relative)
    info = path.stat()
    require(
        info.st_uid == os.getuid()
        and not info.st_mode & 0o077
        and info.st_nlink == 1
        and info.st_size <= crypto.MAX_SMALL,
        "unsafe-metadata",
    )
    return json.loads(path.read_text())


def write_json_new(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def launcher(arguments):
    require(box.main(arguments) == 0, "launcher-exit")


def isolated_env(root, baseline=None):
    """Positive runtime allowlist; never inspect ambient credential values."""
    baseline = box.maintenance_host_env() if baseline is None else baseline
    env = {
        name: baseline[name] for name in ("PATH", "USER", "LOGNAME") if name in baseline
    }
    runtime = baseline.get("XDG_RUNTIME_DIR")
    require(isinstance(runtime, str) and Path(runtime).is_absolute(), "host-runtime")
    runtime = no_symlinks(runtime)
    info = runtime.stat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077,
        "host-runtime",
    )
    env["XDG_RUNTIME_DIR"] = str(runtime)
    bus = baseline.get("DBUS_SESSION_BUS_ADDRESS")
    if bus is not None:
        require(
            isinstance(bus, str)
            and re.fullmatch(
                re.escape("unix:path=" + str(runtime / "bus"))
                + r"(?:,guid=[0-9a-f]{32})?",
                bus,
            )
            is not None,
            "host-runtime-bus",
        )
        env["DBUS_SESSION_BUS_ADDRESS"] = bus
    env.update(
        HOME=str(root / "host-home"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_DATA_HOME=str(root / "data"),
        XDG_STATE_HOME=str(root / "state"),
        XDG_CACHE_HOME=str(root / "cache"),
        OC_BOX_STATE_HOME=str(root / "registry"),
        CONTAINERS_STORAGE_CONF=str(root / "config/containers/storage.conf"),
        CONTAINERS_CONF=str(root / "config/containers/containers.conf"),
        PATH=str(root / "bin") + ":" + env.get("PATH", "/usr/bin:/bin"),
        TMPDIR=str(root / "temporary"),
        OC_BOX_CONTAINER="0",
        OC_BOX_UPDATE_CHECKED="1",
        LANG="C.UTF-8",
        TERM="xterm-256color",
        PYTHONDONTWRITEBYTECODE="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        OPENCODE_DISABLE_MODELS_FETCH="1",
        OPENCODE_DISABLE_AUTOUPDATE="1",
    )
    return env


@contextlib.contextmanager
def private_output():
    """Suppress Python and inherited subprocess output without plaintext logs."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(number) for number in (1, 2)]
    with os.fdopen(
        os.memfd_create("global-vault-fixture-log", os.MFD_CLOEXEC), "w+"
    ) as stream:
        try:
            for number in (1, 2):
                os.dup2(stream.fileno(), number)
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                yield
        finally:
            stream.flush()
            for number, descriptor in zip((1, 2), saved):
                os.dup2(descriptor, number)
                os.close(descriptor)


def containers_config(root):
    # Rootless systemd/cgroups still use the validated HOST runtime. Engine
    # databases, temporary state and volumes remain inside this fresh fixture.
    return (
        "[engine]\nremote = false\ntmp_dir = "
        + json.dumps(str(root / "podman-tmp"))
        + "\nstatic_dir = "
        + json.dumps(str(root / "data/libpod"))
        + "\nvolume_path = "
        + json.dumps(str(root / "data/containers/storage/volumes"))
        + '\nhooks_dir = []\n[containers]\nnetns = "private"\nenv = []\nmounts = []\ndevices = []\n'
    )


def podman_wrapper_source(interpreter=None):
    return (
        "#!"
        + (str(Path(sys.executable).resolve()) if interpreter is None else interpreter)
        + "\n"
        + """import json,os,pathlib,sys
root=pathlib.Path(__file__).resolve().parents[1]
binary=json.loads((root/'podman-executable.json').read_text())['podman']
assert pathlib.Path(binary).is_absolute() and pathlib.Path(binary).resolve()==pathlib.Path(binary)
assert not pathlib.Path(binary).is_relative_to(root)
env=dict(os.environ)
env.pop('CONTAINERS_CONF_OVERRIDE',None)
env['CONTAINERS_STORAGE_CONF']=str(root/'config/containers/storage.conf')
env['CONTAINERS_CONF']=str(root/'config/containers/containers.conf')
os.execve(binary,[binary,*sys.argv[1:]],env)
"""
    )


def validate_native_podman(binary, root):
    binary = Path(binary)
    require(
        binary.is_absolute()
        and binary.resolve(strict=True) == binary
        and binary.is_file()
        and os.access(binary, os.X_OK)
        and not binary.is_relative_to(root),
        "native-podman",
    )
    with binary.open("rb") as stream:
        require(stream.read(4) == b"\x7fELF", "native-podman")
    return binary


def prepare_podman_cli(root, binary):
    """Restore ONLY private selectors around the real, read-only native engine."""
    binary = validate_native_podman(binary, root)
    # The wrapper needs only stdlib. Record the real base interpreter instead
    # of a disposable uv/venv symlink, and never infer it from cleanup's Python.
    interpreter = str(Path(sys.executable).resolve(strict=True))
    validate_native_podman(interpreter, root)
    write_json_new(
        root / "podman-executable.json",
        {"podman": str(binary), "interpreter": interpreter},
    )
    wrapper = root / "bin/podman"
    with wrapper.open("x") as output:
        output.write(podman_wrapper_source(interpreter))
    wrapper.chmod(0o700)
    return wrapper


def expected_storage(root):
    return (
        '[storage]\ndriver = "overlay"\nrunroot = '
        + json.dumps(str(root / "podman-run"))
        + "\ngraphroot = "
        + json.dumps(str(root / "data/containers/storage"))
        + "\n"
    )


def validate_fixture(root, *, allow_unrecorded=False):
    """Pure fixture metadata/path checks; no Podman, vault or native operations."""
    root = no_symlinks(root)
    require(root.name.startswith(PREFIX) and root.is_dir(), "fixture-root")
    info = root.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "fixture-root")
    root = root.resolve(strict=True)
    require(
        private_json(root, "fixture.json")
        == {"format": FORMAT, "version": 1, "projects": list(PROJECTS)},
        "fixture-format",
    )
    for relative in (
        "host-home",
        "config",
        "config/containers",
        "data",
        "data/containers",
        "data/containers/storage",
        "podman-run",
        "podman-tmp",
        "data/libpod",
        "data/containers/storage/volumes",
        "registry",
        "registry/workspaces",
        "state",
        "cache",
        "bin",
        "temporary",
    ):
        safe_path(root, relative, directory=True)
    require(
        safe_path(root, "config/containers/storage.conf").read_text()
        == expected_storage(root),
        "storage-config",
    )
    require(
        safe_path(root, "config/containers/containers.conf").read_text()
        == containers_config(root),
        "storage-config",
    )
    mapping = private_json(root, "podman-executable.json")
    require(
        isinstance(mapping, dict)
        and set(mapping) == {"podman", "interpreter"}
        and isinstance(mapping["podman"], str)
        and isinstance(mapping["interpreter"], str),
        "native-podman",
    )
    validate_native_podman(mapping["podman"], root)
    validate_native_podman(mapping["interpreter"], root)
    wrapper = safe_path(root, "bin/podman")
    require(
        wrapper.read_text() == podman_wrapper_source(mapping["interpreter"])
        and os.access(wrapper, os.X_OK),
        "podman-selector",
    )
    records = workspace_records(root)
    ids = private_json(root, "fixture-ids.json")
    actual = {name: data["id"] for name, (_, data) in records.items()}
    require(
        isinstance(ids, dict)
        and all(actual.get(name) == ident for name, ident in ids.items()),
        "fixture-ids",
    )
    require(allow_unrecorded or ids == actual, "unrecorded-journal")
    return root


def system_boot_id():
    with Path("/proc/sys/kernel/random/boot_id").open("rb") as stream:
        return stream.read(129)


def runtime_boot_checkpoint(root):
    """File-only proof of the retained engine's boot; never emit either ID.

    Podman 6.1.4 checkBootID compares these exact bytes. A disk-backed TmpDir
    can outlive a reboot, where info refuses with advice to delete directories.
    Never follow that advice here or rewrite the marker to conceal the mismatch.
    https://github.com/containers/podman/blob/v6.1.4/libpod/runtime_linux.go
    """
    marker = safe_path(root, "podman-tmp/alive", optional=True)
    cached = None
    if marker.exists():
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and info.st_size <= 128,
                "runtime-boot-marker",
            )
            cached = stream.read(129)
    current = system_boot_id()
    pattern = rb"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n?"
    valid_cached = isinstance(cached, bytes) and bool(re.fullmatch(pattern, cached))
    valid_current = isinstance(current, bytes) and bool(re.fullmatch(pattern, current))
    matches = (cached == current) if valid_cached and valid_current else None
    return {
        "cached_marker_present": cached is not None,
        "cached_marker_empty": cached == b"",
        "cached_id_valid": valid_cached,
        "current_id_valid": valid_current,
        "boot_ids_match": matches,
    }


def require_runtime_boot(root, *, allow_uninitialized=False):
    checkpoint = runtime_boot_checkpoint(root)
    require(checkpoint["current_id_valid"], "runtime-boot-current")
    if allow_uninitialized and (
        not checkpoint["cached_marker_present"] or checkpoint["cached_marker_empty"]
    ):
        return checkpoint
    require(checkpoint["cached_id_valid"], "runtime-boot-marker")
    require(checkpoint["boot_ids_match"] is True, "runtime-boot-mismatch")
    return checkpoint


def validate_store_info(root, info, temporary=None):
    """Independent native Podman proof; labels alone never authorize cleanup."""
    require(isinstance(info, dict), "private-store")
    require(
        info.get("host", {}).get("security", {}).get("rootless") is True, "rootless"
    )
    require(info.get("host", {}).get("serviceIsRemote") is False, "remote-engine")
    store = info.get("store", {})
    require(store.get("graphDriverName") == "overlay", "storage-driver")
    for field, relative in (
        ("graphRoot", "data/containers/storage"),
        ("runRoot", "podman-run"),
        ("volumePath", "data/containers/storage/volumes"),
    ):
        expected = safe_path(root, relative, directory=True)
        actual = store.get(field)
        require(isinstance(actual, str) and Path(actual) == expected, "private-store")
        require(no_symlinks(actual).resolve(strict=True) == expected, "private-store")
    # Podman 5.6.2 exposes configFile. The separately audited v6.1.4
    # libpod/define/info.go removes it; define/version.go reports Version with
    # that exact capitalization. Only that verified omission is supported:
    # https://github.com/containers/podman/blob/v6.1.4/libpod/define/info.go
    # Unknown omissions fail closed. Exposed fields are ALWAYS validated, even
    # for 6.1.4. This exception also revalidates the exact selector source,
    # native executable mapping and private config; it is not label-only proof.
    if "configFile" in store:
        config = store["configFile"]
        require(
            isinstance(config, str)
            and no_symlinks(config) == root / "config/containers/storage.conf",
            "store-info-shape",
        )
    else:
        version = info.get("version")
        require(
            isinstance(version, dict)
            and version.get("Version") in PODMAN_CONFIGFILE_OMISSIONS,
            "store-info-shape",
        )
        validate_fixture(root, allow_unrecorded=True)
    temporary = no_symlinks(temporary or root / "temporary")
    info = temporary.stat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077,
        "temporary-path",
    )
    image_tmp = store.get("imageCopyTmpDir")
    require(
        isinstance(image_tmp, str)
        and no_symlinks(image_tmp).resolve(strict=True)
        == temporary.resolve(strict=True),
        "image-temporary-path",
    )


def validate_store(root, *, allow_unrecorded=False):
    """Verify actual rootless store selection in normal and sanitized envs."""
    root = validate_fixture(root, allow_unrecorded=allow_unrecorded)
    # New fixtures initialize an empty runtime; retained operations separately
    # require an existing matching marker. A known mismatch NEVER reaches info.
    require_runtime_boot(root, allow_uninitialized=not workspace_records(root))
    normal = isolated_env(root)
    # temporary_location deliberately sets both env and Python's tempfile cache.
    # Only an already-isolated fixture caller may contribute this optional path.
    if os.environ.get("OC_BOX_STATE_HOME") == str(root / "registry"):
        normal["TMPDIR"] = str(
            existing_directory(os.environ.get("TMPDIR", str(root / "temporary")))
        )
    with patch.dict(os.environ, normal, clear=True):
        for env in (normal, box.maintenance_host_env(), crypto.environment()):
            result = box.run(
                ["podman", "info", "--format", "json"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture=True,
                private=True,
                timeout=30,
            )
            validate_store_info(root, json.loads(result.stdout), env["TMPDIR"])


def prepare_root(root, distro):
    for relative in (
        "host-home",
        "config/opencode",
        "config/oc-box",
        "state",
        "cache",
        "registry/workspaces",
        "temporary",
        "podman-run",
        "podman-tmp",
        "data/libpod",
        "data/containers/storage/volumes",
        "data/containers/storage",
        "bin",
        "source-a",
        "source-b",
    ):
        (root / relative).mkdir(parents=True, mode=0o700, exist_ok=True)
    storage_config(root)
    (root / "config/containers/containers.conf").write_text(containers_config(root))
    write_json_new(
        root / "fixture.json",
        {"format": FORMAT, "version": 1, "projects": list(PROJECTS)},
    )
    write_json_new(root / "fixture-ids.json", {})
    write_json_new(
        root / "config/oc-box/config.json",
        {
            "default_distro": distro,
            "pass_env": [PASS_ENV],
            "auto_update": False,
        },
    )
    for name in ("source-a", "source-b"):
        (root / name / "unfinished.txt").write_text("PROJECT_RETAINED\n")


def native_wrapper_source(interpreter=None, *, root=None, binary=None):
    """Legacy bytes, or an attempt-local selector bound to ORIGINAL host history."""
    code = "import json,os,pathlib,sys\n"
    code += (
        "root=pathlib.Path(__file__).resolve().parents[1]\n"
        if root is None
        else "root=pathlib.Path(" + repr(str(root)) + ")\n"
    )
    code += (
        "binary=json.loads((root/'executables.json').read_text())['2']\n"
        if binary is None
        else "binary=" + repr(str(binary)) + "\n"
    )
    code += """env={key:os.environ[key] for key in ('PATH','USER','LOGNAME','LANG','TERM','TMPDIR','XDG_RUNTIME_DIR') if key in os.environ}
env['HOME']=str(root/'host-home')
for name in ('CONFIG','DATA','CACHE','STATE'):
    env['XDG_'+name+'_HOME']=str(root/('host-'+name.lower()+'-2'))
env.update(OPENCODE_TEST_HOME=env['HOME'],OPENCODE_DISABLE_MODELS_FETCH='1',OPENCODE_DISABLE_PROJECT_CONFIG='1',OPENCODE_PURE='1',OPENCODE_DISABLE_AUTOUPDATE='1')
env['OPENCODE_CONFIG_CONTENT']=json.dumps({'snapshots':False,'formatter':False,'update':'disable','policies':[{'action':'plugin.use','resource':'*','effect':'deny'}]})
os.execve(binary,[binary,*sys.argv[1:]],env)
"""
    return (
        "#!"
        + (sys.executable if interpreter is None else str(interpreter))
        + "\n"
        + code
    )


def prepare_native_cli(root, binary):
    """A single-version selector, never a host installation or service client."""
    check_native_version(root, binary, "2")
    for name in ("config", "data", "state", "cache"):
        (root / ("host-" + name + "-2")).mkdir(mode=0o700, exist_ok=True)
    write_json_new(root / "executables.json", {"2": str(binary)})
    wrapper = root / "bin/opencode"
    with wrapper.open("x") as output:
        output.write(native_wrapper_source())
    wrapper.chmod(0o700)
    release = compat.version(
        box.run(
            [str(wrapper), "--version"],
            stdin=subprocess.DEVNULL,
            capture=True,
            private=True,
            timeout=15,
        ).stdout
    )
    require(release == NATIVE_VERSION, "native-version")
    return wrapper


def seed_native_history(root, binary):
    """Seed BEFORE box.main creates a workspace, using native export/import APIs."""
    project = root / "source-a"
    env = native_env(root, "2")
    previous = Path.cwd()
    try:
        os.chdir(project)
        # Direct binary is essential: the selector intentionally clears the
        # bridge's OPENCODE_PASSWORD and cannot authenticate this private lease.
        with (
            patch.dict(os.environ, env, clear=True),
            bridge.server(str(binary)) as request,
        ):
            session = request(
                "POST",
                "/api/session",
                {
                    "location": {"directory": str(project)},
                    "agent": "build",
                    "permissions": ASK_RULES,
                },
            )["data"]
            ident = session["id"]
            skeleton = request("GET", "/api/experimental/session/" + ident + "/export")[
                "data"
            ]
            require(skeleton["messages"] == [], "native-seed")
            skeleton["messages"] = [
                {
                    "id": "msg_global_vault_seed_" + secrets.token_hex(12),
                    "type": "user",
                    "text": SEED_TEXT,
                    "time": {"created": 1},
                }
            ]
            request("DELETE", "/api/session/" + ident)
            imported = request(
                "POST",
                "/api/experimental/session/import",
                {
                    **skeleton,
                    "location": {"directory": str(project)},
                },
            )["data"]
            require(imported["id"] == ident, "native-seed")
            baseline = request("GET", "/api/experimental/session/" + ident + "/export")[
                "data"
            ]
            require(baseline["messages"] == skeleton["messages"], "native-seed")
            require(baseline["info"].get("permissions") == ASK_RULES, "native-seed")
            box.validate_session_export(baseline, ident)
    finally:
        os.chdir(previous)
    write_json_new(root / "seed-baseline.json", baseline)
    return baseline


def fixture_config(port, root=None):
    return {
        "$schema": "https://opencode.ai/config.json",
        "snapshots": False,
        "formatter": False,
        "update": "disable",
        "default_agent": "build",
        "plugins": [
            {
                "package": str(
                    root / "config/opencode/plugins/fixture-global-vault-permission"
                )
                if root is not None
                else "./plugins/fixture-global-vault-permission"
            }
        ],
        "agents": {
            "fixture-child": {
                "mode": "subagent",
                "permissions": [
                    {"action": ACTION, "resource": "*", "effect": "ask"},
                ],
            }
        },
        "policies": [
            {"action": "provider.use", "resource": "*", "effect": "deny"},
            {"action": "provider.use", "resource": "fixture", "effect": "allow"},
            {
                "action": "tool.use",
                "resource": ACTION + ":policy-denied",
                "effect": "deny",
            },
        ],
        "providers": {
            "fixture": {
                "package": "aisdk:@ai-sdk/openai-compatible",
                "settings": {
                    "baseURL": f"http://host.containers.internal:{port}/v1",
                    "apiKey": "{env:" + KEY + "}",
                },
                "models": {
                    "gpt-smoke": {
                        "name": "Synthetic global vault acceptance",
                        "limit": {"context": 32000, "output": 2000},
                    }
                },
            }
        },
        "model": "fixture/gpt-smoke",
        "mcp": {
            "servers": {
                "fixture": {
                    "type": "local",
                    "command": ["python3", "-c", MCP],
                    "environment": {"FIXTURE_MCP_KEY": "{env:" + KEY + "}"},
                }
            }
        },
    }


def write_plugin(root):
    directory = root / "config/opencode/plugins/fixture-global-vault-permission"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "package.json").write_text('{"type":"module","main":"index.js"}\n')
    (directory / "index.js").write_text(TOOL_SOURCE)


def expected_hashes(values):
    require(set(values).issubset(KNOWN) and KEY in values, "synthetic-contract")
    return {
        name: hashlib.sha256(values[name].encode()).hexdigest()
        if name in values
        else None
        for name in KNOWN
    }


def shell_program(expected):
    return (
        "import hashlib,json,os; expected=json.loads("
        + repr(json.dumps(expected))
        + "); actual={name:(hashlib.sha256(os.environ[name].encode()).hexdigest() if name in os.environ else None) for name in expected}; assert actual==expected; print('"
        + SHELL_MARKER
        + "')"
    )


def make_provider(state):
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            if self.path != "/health":
                self.send_error(404, "Fixture endpoint unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"fixture":"oc-box-global-vault"}')

        def do_POST(self):
            try:
                self.connection.settimeout(10)
                require(self.path == "/v1/chat/completions", "provider-endpoint")
                require(
                    self.headers.get("Authorization")
                    == "Bearer " + state["values"][KEY],
                    "provider-auth",
                )
                length = int(self.headers["Content-Length"])
                require(0 < length < 2_000_000, "provider-size")
                body = json.loads(self.rfile.read(length))
                state["requests"] += 1
                require(
                    state["requests"] <= 80
                    and state["requests"] - state["before"] <= 12,
                    "provider-bound",
                )
                messages = body.get("messages", [])
                last = max(
                    (
                        i
                        for i, item in enumerate(messages)
                        if item.get("role") == "user"
                    ),
                    default=-1,
                )
                results = [
                    item for item in messages[last + 1 :] if item.get("role") == "tool"
                ]
                names = {item["function"]["name"] for item in body.get("tools", [])}
                markers = (SHELL_MARKER, MCP_MARKER, TOOL_MARKER)
                require(len(results) <= 3, "provider-tools")
                for index, item in enumerate(results):
                    require(
                        markers[index] in json.dumps(item.get("content")),
                        "provider-tool-result",
                    )
                    state["markers"].add(markers[index])
                delta, finish = {"content": DONE_MARKER}, "stop"
                if names and len(results) < 3:
                    expected = expected_hashes(state["values"])
                    actions = (
                        (
                            "shell",
                            {
                                "command": "python3 -c "
                                + shlex.quote(shell_program(expected))
                            },
                        ),
                        (
                            "execute",
                            {
                                "code": "return await tools.fixture.check({expected: "
                                + json.dumps(expected)
                                + "})"
                            },
                        ),
                        (ACTION, {"resource": "native-tool-ask"}),
                    )
                    name, arguments = actions[len(results)]
                    require(name in names, "provider-tool-unavailable")
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
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for choice in (
                    {"index": 0, "delta": delta, "finish_reason": None},
                    {"index": 0, "delta": {}, "finish_reason": finish},
                ):
                    chunk = {
                        "id": "chatcmpl-global-vault",
                        "object": "chat.completion.chunk",
                        "model": "gpt-smoke",
                        "choices": [choice],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except Exception:
                state["errors"] += 1
                self.send_error(500, "Synthetic fixture contract failed")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker


def workspace_records(root):
    """Validate all registry paths before any resource inspection/removal."""
    directory = safe_path(root, "registry/workspaces", directory=True)
    records = {}
    for folder in sorted(directory.iterdir()):
        require(
            re.fullmatch(r"[0-9a-f]{32}", folder.name) is not None,
            "workspace-allowlist",
        )
        safe_path(root, folder.relative_to(root), directory=True)
        path = folder / "workspace.json"
        data = private_json(root, path.relative_to(root))
        project = data.get("project")
        require(isinstance(project, str), "workspace-allowlist")
        project_path = no_symlinks(project)
        name = project_path.name
        require(
            project_path == root / name and name in PROJECTS and name not in records,
            "workspace-allowlist",
        )
        safe_path(
            root,
            name,
            directory=True,
            optional=name == "restored-full" and data.get("restore_incomplete"),
        )
        require(
            data.get("id") == folder.name
            and data.get("version") == 2
            and data.get("home") == str(root / "host-home")
            and data.get("container") == "oc-box-" + folder.name
            and data.get("home_volume") == "oc-box-home-" + folder.name
            and data.get("distro") in box.DISTROS
            and not data.get("devices")
            and not data.get("podman_args"),
            "workspace-allowlist",
        )
        require(
            data.get("opencode_version", NATIVE_VERSION) == NATIVE_VERSION,
            "workspace-allowlist",
        )
        require(
            data.get("container_project", project)
            == str(root / ("source-a" if name == "restored-full" else name)),
            "workspace-allowlist",
        )
        if "credentials" in data:
            require(
                data["credentials"] == {"mode": "vault", "env": [], "vault": []},
                "workspace-policy",
            )
        if name == "restored-full":
            origin = data.get("restored_from", {})
            require(
                origin.get("kind") == "full"
                and isinstance(origin.get("workspace_id"), str),
                "workspace-allowlist",
            )
        if data.get("restore_staging_project"):
            staging = no_symlinks(data["restore_staging_project"])
            require(
                staging.parent == root and staging.name.startswith(".oc-box-restore-"),
                "workspace-allowlist",
            )
        # Check every metadata descendant, including lock/import/history files.
        for child in folder.rglob("*"):
            no_symlinks(child)
            require(child.resolve().is_relative_to(folder), "unsafe-metadata")
        marker = project_path / box.MARKER
        if project_path.exists():
            safe_path(root, marker.relative_to(root))
            require(marker.read_text().strip() == data["id"], "workspace-marker")
        records[name] = (path, data)
    # Directory IDs have no project ordering; validate provenance only once the
    # complete, bounded registry allowlist is available.
    if "restored-full" in records:
        require("source-a" in records, "workspace-allowlist")
        require(
            records["restored-full"][1]["restored_from"]["workspace_id"]
            == records["source-a"][1]["id"],
            "workspace-allowlist",
        )
    return records


def record_ids(root):
    records = workspace_records(root)
    ids = {name: data["id"] for name, (_, data) in records.items()}
    previous = private_json(root, "fixture-ids.json")
    require(
        isinstance(previous, dict)
        and all(ids.get(name) == ident for name, ident in previous.items()),
        "fixture-ids",
    )
    box.atomic_json(root / "fixture-ids.json", ids)
    return ids


def workspace(root, name):
    records = workspace_records(root)
    require(name in records, "workspace-missing")
    return records[name]


def verify_owned_resources(root, data, *, stopped=False):
    info = backups.owned_container(box, data)
    require(
        info is not None and (not stopped or not info["State"]["Running"]),
        "workspace-state",
    )
    require(info.get("Name", "").lstrip("/") == data["container"], "resource-allowlist")
    volume = backups.owned_volume(box, data)
    require(volume.get("Name") == data["home_volume"], "resource-allowlist")
    mountpoint = no_symlinks(volume["Mountpoint"])
    require(
        mountpoint
        == root / "data/containers/storage/volumes" / data["home_volume"] / "_data",
        "private-volume",
    )
    require(not info.get("HostConfig", {}).get("PortBindings"), "published-ports")
    approved = {
        (data["project"], box.container_project(data)),
        (str(mountpoint), data["home"]),
    }
    mounts = {
        (item.get("Source"), item.get("Destination"))
        for item in info.get("Mounts", [])
        if item.get("Type") in ("bind", "volume")
    }
    require(mounts == approved, "resource-mounts")
    return info


def request_for(data, connection):
    def request(method, target, value=None):
        return workspace_service.api(box, data, connection, method, target, value)

    return request


def selected_session(command, path, expected=None):
    """The live endpoint has no session field; validate the actual client ID."""
    require("--session" in command, "selected-session")
    index = command.index("--session")
    require(index + 1 < len(command), "selected-session")
    ident = command[index + 1]
    require(box.valid_session_id(ident), "selected-session")
    last = box.session_state(path).get("last")
    require(last == ident or (last is None and expected is None), "selected-session")
    require(expected is None or ident == expected, "selected-session")
    return ident


def validate_selected_source_session(info, ident, data):
    """A recorded selector must resolve to a root in this same private project."""
    require(
        isinstance(info, dict)
        and info.get("id") == ident
        and not info.get("parentID")
        and isinstance(info.get("location"), dict)
        and info["location"].get("directory") == box.container_project(data),
        "selected-session",
    )


def assert_plugins(request, project, trace=None):
    query = "?directory=" + quote(str(project), safe="")
    if trace is not None:
        trace("api-model-default")
    request("GET", "/api/model/default" + query)
    trace_complete(trace)
    if trace is not None:
        trace("api-plugin-list")
    plugins = request("GET", "/api/plugin" + query)["data"]
    indexed = {item["id"]: item for item in plugins}
    for ident in ("oc-box.build-permissions", "oc-box.fixture-global-vault-permission"):
        require(
            indexed.get(ident, {}).get("state", {}).get("status") == "active",
            "fixture-plugin-readiness",
        )
    require(
        not any(item.get("state", {}).get("status") == "failed" for item in plugins),
        "fixture-plugin-readiness",
    )
    trace_complete(trace)


def create_session(request, project, *, agent=None, parent=None, permissions=None):
    value = {
        "location": {"directory": str(project)},
        "permissions": ASK_RULES if permissions is None else permissions,
    }
    if agent is not None:
        value["agent"] = agent
    if parent is not None:
        value["parentID"] = parent
    return request("POST", "/api/session", value)["data"]


def evaluate(
    request, ident, expected, *, agent=None, resource="ask-target", trace=None
):
    target = "/api/session/" + ident + "/permission"
    require(request("GET", target)["data"] == [], "pending-permission")
    value = {"action": ACTION, "resources": [resource]}
    if agent is not None:
        value["agent"] = agent
    result = request("POST", target, value)["data"]
    pending = request("GET", target)["data"]
    try:
        require(result.get("effect") == expected, "permission-effect")
        if expected == "ask":
            require(
                len(pending) == 1
                and pending[0]["id"] == result["id"]
                and pending[0]["sessionID"] == ident,
                "pending-permission",
            )
        else:
            require(pending == [], "pending-permission")
    except BaseException as error:
        if trace is not None:
            trace.fail(error)
        raise
    finally:
        # Reject even unexpected asks. Never grant once/always or alter policy.
        try:
            for item in pending:
                request(
                    "POST", target + "/" + item["id"] + "/reply", {"decision": "reject"}
                )
        except BaseException as error:
            if trace is not None:
                trace("permission-cleanup")
                trace.fail(error)
            raise
    require(request("GET", target)["data"] == [], "pending-permission")


def permission_matrix(request, project, primary, trace=None):
    assert_plugins(request, project, trace=trace)
    if trace is not None:
        trace("permission-matrix")
    original = request("GET", "/api/session/" + primary)["data"]
    require(original.get("permissions") == ASK_RULES, "session-asks")
    own = []
    try:
        bare = create_session(request, project)
        own.append(bare)
        require("agent" not in bare, "missing-agent-fixture")
        plan = create_session(request, project, agent="plan", parent=primary)
        own.append(plan)
        child = create_session(request, project, agent="fixture-child", parent=primary)
        own.append(child)
        unknown = create_session(
            request, project, agent="fixture-unknown", permissions=[]
        )
        own.append(unknown)
        for ident, agent, effect in (
            (primary, "build", "allow"),
            (primary, None, "allow"),
            (bare["id"], None, "allow"),
            (primary, "plan", "ask"),
            (primary, "fixture-child", "ask"),
            (plan["id"], None, "ask"),
            (child["id"], None, "ask"),
            (primary, "fixture-unknown", "ask"),
            (unknown["id"], None, "deny"),
            (unknown["id"], "fixture-unknown", "deny"),
        ):
            evaluate(request, ident, effect, agent=agent, trace=trace)
        for agent in (None, "build", "plan", "fixture-child"):
            evaluate(
                request,
                primary,
                "deny",
                agent=agent,
                resource="session-denied",
                trace=trace,
            )
            evaluate(
                request,
                primary,
                "deny",
                agent=agent,
                resource="policy-denied",
                trace=trace,
            )
        for session in [original, *own]:
            current = request("GET", "/api/session/" + session["id"])["data"]
            require(
                current.get("permissions") == session.get("permissions")
                and current.get("agent") == session.get("agent")
                and current.get("parentID") == session.get("parentID"),
                "permission-preservation",
            )
        query = "?directory=" + quote(str(project), safe="")
        require(
            request("GET", "/api/permission/request" + query)["data"] == [],
            "pending-permission",
        )
        require(
            request("GET", "/api/permission/saved" + query)["data"] == [],
            "saved-permission",
        )
    except BaseException as error:
        if trace is not None:
            trace.fail(error)
        raise
    finally:
        try:
            for session in reversed(own):
                request("DELETE", "/api/session/" + session["id"])
        except BaseException as error:
            if trace is not None:
                trace("permission-cleanup")
                trace.fail(error)
            raise


def default_plan_fallback(root, data, request):
    """Probe default Plan at the owner's final step, then remove our new file.

    Native V2 may cache a removed project config for this location's lifetime.
    We do not claim a reverse/default-Build reload: this owner performs no more
    model runs, and later owners start with the fixture-owned file absent.
    """
    project = box.container_project(data)
    file = safe_path(
        root, Path(data["project"]).relative_to(root) / "opencode.json", optional=True
    )
    require(not file.exists(), "default-plan-fixture")
    session = create_session(request, project)
    require("agent" not in session, "missing-agent-fixture")
    evaluate(request, session["id"], "allow")
    created = False
    try:
        write_json_new(file, {"default_agent": "plan"})
        created = True
        request("POST", "/api/location/reload")
        assert_plugins(request, project)
        query = "?directory=" + quote(str(project), safe="")
        selectable = [
            item
            for item in request("GET", "/api/agent" + query)["data"]
            if item["mode"] != "subagent" and not item.get("hidden")
        ]
        require(selectable and selectable[0]["id"] == "plan", "default-plan-fixture")
        evaluate(request, session["id"], "ask")
        evaluate(request, session["id"], "allow", agent="build")
        evaluate(request, session["id"], "deny", resource="session-denied")
        current = request("GET", "/api/session/" + session["id"])["data"]
        require(
            current.get("permissions") == ASK_RULES and "agent" not in current,
            "permission-preservation",
        )
    finally:
        if created:
            safe_path(root, file.relative_to(root)).unlink()
        try:
            request("POST", "/api/location/reload")
        finally:
            request("DELETE", "/api/session/" + session["id"])
    assert_plugins(request, project)


def trace_complete(trace):
    complete = getattr(trace, "completed", None)
    if complete is not None:
        complete()


def run_native(data, connection, ident, state, port, values, trace=None):
    if trace is not None:
        trace("plugin-readiness")
    assert_plugins(
        request_for(data, connection), box.container_project(data), trace=trace
    )
    if trace is not None:
        complete = getattr(trace, "completed", None)
        if complete is not None:
            complete("plugin-readiness")
    if trace is not None:
        trace("host-provider-health")
    health = "import json,sys; from urllib.request import build_opener,ProxyHandler; assert json.load(build_opener(ProxyHandler({})).open(sys.argv[1],timeout=5))=={'fixture':'oc-box-global-vault'}"
    box.run(
        box.exec_command(
            data,
            ["python3", "-c", health, f"http://host.containers.internal:{port}/health"],
        ),
        input="",
        capture=True,
        private=True,
        timeout=15,
    )
    trace_complete(trace)
    if trace is not None:
        trace("native-tool-client")
    state.update(values=dict(values), markers=set(), before=state["requests"])
    args = [
        box.CONTAINER_OPENCODE,
        "run",
        "--server",
        connection["url"],
        "--session",
        ident,
        "--model",
        "fixture/gpt-smoke",
        "--agent",
        "build",
        "Exercise the three synthetic global vault fixture tools.",
    ]
    require("--auto" not in args, "client-autoaccept")
    command = workspace_service.client_command(
        box,
        data,
        connection,
        [
            "python3",
            "-c",
            NATIVE_PROBE,
            connection["url"],
            ident,
            *args,
        ],
    )
    result = box.run(command, input="", capture=True, private=True, timeout=215)
    trace_complete(trace)
    if trace is not None:
        trace("native-results")
    require(
        json.loads(result.stdout) == {"completed": True, "permission_asked": 0},
        "native-events",
    )
    require(
        state["errors"] == 0
        and state["markers"] == {SHELL_MARKER, MCP_MARKER, TOOL_MARKER},
        "native-tools",
    )
    request = request_for(data, connection)
    require(
        request("GET", "/api/session/" + ident + "/permission")["data"] == [],
        "pending-permission",
    )
    trace_complete(trace)
    if trace is not None:
        trace("transcript-validation")
    exported = box.export_container_session(data, ident)
    require(exported["info"].get("permissions") == ASK_RULES, "permission-preservation")
    tools = [
        part
        for message in exported["messages"]
        if message["type"] == "assistant"
        for part in message["content"]
        if part.get("type") == "tool"
    ]
    for name, marker in (
        ("shell", SHELL_MARKER),
        ("execute", MCP_MARKER),
        (ACTION, TOOL_MARKER),
    ):
        require(
            any(
                part.get("name") == name
                and part.get("state", {}).get("status") == "completed"
                and marker in json.dumps(part["state"])
                for part in tools
            ),
            "native-transcript",
        )
    require(DONE_MARKER in json.dumps(exported["messages"]), "native-transcript")
    trace_complete(trace)
    return exported


@contextlib.contextmanager
def no_vault_probe():
    # Spies retain actual presence/read behavior. Only masked prompt input is
    # replaced; selection, decryption and credential logic are never substituted.
    with (
        patch.object(vault, "is_present", wraps=vault.is_present) as present,
        patch.object(vault, "read_envelope", wraps=vault.read_envelope) as read,
        patch.object(vault, "hidden", side_effect=FixtureError("unexpected-unlock")),
    ):
        yield
        require(
            present.call_count == 0 and read.call_count == 0, "unexpected-vault-probe"
        )


def write_vault(values, password):
    with vault.locked(vault.vault_path(), create=True):
        vault.write_encrypted(vault.vault_path(), values, password)


def assert_host_vault_clean():
    require(not credentials.has_unlocked_values(), "host-value-retention")
    require(
        all(name not in os.environ for name in (KEY, SIDE, ADDED)), "host-value-leak"
    )


def assert_host_clean():
    assert_host_vault_clean()
    require(all(name not in os.environ for name in KNOWN), "host-value-leak")


def tree_digest(root):
    result = {}
    for path in sorted(root.rglob("*")):
        no_symlinks(path)
        if path.is_file():
            result[str(path.relative_to(root))] = crypto.digest(path)
    return result


def os_sentinel(data):
    content = crypto.small(
        ["podman", "cp", data["container"] + ":" + OS_FILE, "-"], timeout=30
    )
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
        members = archive.getmembers()
        require(len(members) == 1 and members[0].isfile(), "os-sentinel")
        with archive.extractfile(members[0]) as stream:
            require(stream.read() == b"OS_RETAINED\n", "os-sentinel")


def snapshot_source(root, path, data):
    verify_owned_resources(root, data, stopped=True)
    os_sentinel(data)
    return {
        "project": tree_digest(root / "source-a"),
        "registry": tree_digest(path.parent),
        "home": {
            name: digest_home(data, name)
            for name in (".local/share/opencode/oc-box-v2.db", "acceptance-home.txt")
        },
    }


def full_backup_restore(root, baseline, expected, state, port, password, report):
    path, source = workspace(root, "source-a")
    before = snapshot_source(root, path, source)
    vault_before = crypto.digest(vault.vault_path())
    identity = root / "age-identity.txt"
    descriptor = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(crypto.small(["age-keygen"]))
    recipient = crypto.small(["age-keygen", "-y", str(identity)]).decode().strip()
    crypto.validate_recipients([recipient])
    archive = root / "full.ocbox"
    with no_vault_probe():
        launcher(
            [
                "backup",
                "--workspace",
                source["id"],
                "--output",
                str(archive),
                "--recipient",
                recipient,
            ]
        )
        launcher(["backup-info", str(archive), "--identity", str(identity)])
        with crypto.opened(archive, [identity]) as (manifest, _, _):
            backups.validate_manifest(box, manifest)
            require(
                manifest["kind"] == "full"
                and manifest["source_workspace_id"] == source["id"]
                and manifest["project"] == source["project"]
                and manifest["container_project"] == source["project"]
                and manifest["configuration"]["credentials"]
                == {"mode": "vault", "env": [], "vault": []}
                and manifest["sessions"]["last"] == baseline["info"]["id"]
                and set(manifest["components"])
                == {"project.age", "home.age", "registry.age", "image.age"},
                "backup-manifest",
            )
        archive_before = crypto.digest(archive)
        destination = root / "restored-full"
        safe_path(root, "restored-full", directory=True, optional=True)
        require(not destination.exists(), "restore-destination")
        launcher(
            [
                "restore",
                str(archive),
                "--project",
                str(destination),
                "--identity",
                str(identity),
            ]
        )
    record_ids(root)
    restored_path, restored = workspace(root, "restored-full")
    verify_owned_resources(root, restored, stopped=True)
    require(
        restored["id"] != source["id"]
        and restored["container"] != source["container"]
        and restored["home_volume"] != source["home_volume"]
        and box.container_project(restored) == source["project"]
        and box.session_state(restored_path)["last"] == baseline["info"]["id"],
        "restore-identity",
    )
    # This comparison happens before any restored native service/database open.
    require(
        all(
            digest_home(restored, name) == digest
            for name, digest in before["home"].items()
        ),
        "restore-database-bytes",
    )
    require(
        tree_digest(restored_path.parent / "recovered-registry") == before["registry"],
        "restore-registry-bytes",
    )
    require(
        (destination / "unfinished.txt").read_text() == "PROJECT_RETAINED\n",
        "restore-project",
    )
    os_sentinel(restored)
    require(snapshot_source(root, path, source) == before, "restore-source-changed")
    require(
        crypto.digest(archive) == archive_before
        and crypto.digest(vault.vault_path()) == vault_before,
        "restore-evidence-changed",
    )
    report("restored-stopped-byte-exact")

    def relaunched(command):
        require("--auto" not in command, "client-autoaccept")
        current_path, current = workspace(root, "restored-full")
        connection = workspace_service.live_connection()
        require(connection is not None, "restored-session")
        ident = selected_session(command, current_path, baseline["info"]["id"])
        prior = box.export_container_session(current, ident)
        verify_v2_progress(prior, baseline)
        run_native(current, connection, ident, state, port, expected)
        permission_matrix(
            request_for(current, connection),
            box.container_project(current),
            ident,
        )
        verify_v2_progress(box.export_container_session(current, ident), baseline)
        return 0

    with (
        patch.object(vault, "hidden", return_value=password) as hidden,
        patch.object(box, "run_tui", side_effect=relaunched),
    ):
        launcher(["start", "--workspace", restored["id"]])
        require(hidden.call_count == 1, "unlock-count")
    assert_host_clean()
    restored_path, restored = workspace(root, "restored-full")
    verify_owned_resources(root, restored, stopped=True)
    os_sentinel(restored)
    require(snapshot_source(root, path, source) == before, "restore-source-changed")
    require(
        crypto.digest(archive) == archive_before
        and crypto.digest(vault.vault_path()) == vault_before,
        "restore-evidence-changed",
    )
    report("restored-latest-host-vault-history-os")


def run(root, binary, distro, report=None):
    host_guard()
    address = (
        Path(isolated_env(root)["XDG_RUNTIME_DIR"])
        / ("oc-box-live-" + str(os.getuid()))
        / ("x" * 24 + ".sock")
    )
    require(len(os.fsencode(address)) < 108, "runtime-path-too-long")
    if report is None:

        def report(name):
            if name != "passed":
                report.checks.append(name)
            print("global-vault fixture: " + name, flush=True)

        report.checks = []
    validate_store(root)  # No workspace/resource use is allowed before this proof.
    box.check_backend(feature="launch")
    crypto.tools()
    vault.crypto()
    prepare_native_cli(root, binary)
    report("private-store-native-version")
    report("native-host-seed")
    baseline = seed_native_history(root, binary)
    require(workspace_records(root) == {}, "first-launch-fixture")
    password = secrets.token_urlsafe(32)
    initial = {
        KEY: "fixture-" + secrets.token_urlsafe(24),
        SIDE: "fixture-" + secrets.token_urlsafe(24),
    }
    latest = {
        KEY: "fixture-" + secrets.token_urlsafe(24),
        SIDE: initial[SIDE],
        ADDED: "fixture-" + secrets.token_urlsafe(24),
    }
    ambient = {
        AMBIENT: "synthetic-ambient-control",
        PASS_ENV: "synthetic-pass-env-control",
    }
    compatibility = {KEY: "synthetic-env-compatibility-only", **ambient}
    require(
        all(
            value not in compatibility.values()
            for value in [*initial.values(), *latest.values()]
        ),
        "synthetic-contract",
    )
    # The encrypted vault exists before the FIRST launch/create/import, with no
    # saved name list and no credentials.configure call anywhere in this fixture.
    write_vault(initial, password)
    state = {"values": {}, "requests": 0, "before": 0, "markers": set(), "errors": 0}
    server, worker = make_provider(state)
    try:
        write_json_new(
            root / "config/opencode/opencode.json",
            fixture_config(server.server_port, root),
        )
        write_plugin(root)
        progress = {}

        def first_owner(command):
            with trace.checkpoint("callback"):
                require("--auto" not in command, "client-autoaccept")
                record_ids(root)
                current_path, data = workspace(root, "source-a")
                require("credentials" not in data, "first-launch-default")
                with trace.checkpoint("source-resources"):
                    verify_owned_resources(root, data)
                with trace.checkpoint("private-owner"):
                    connection = workspace_service.live_connection()
                    require(connection is not None, "first-import-session")
                ident = baseline["info"]["id"]
                with trace.checkpoint("selected-session"):
                    selected_session(command, current_path, ident)
                with trace.checkpoint("retained-transcript"):
                    imported = box.export_container_session(data, ident)
                    require(
                        imported["messages"] == baseline["messages"],
                        "first-import-transcript",
                    )
                    verify_v2_progress(imported, baseline)
                request = request_for(data, connection)
                run_native(
                    data,
                    connection,
                    ident,
                    state,
                    server.server_port,
                    initial,
                    trace=trace,
                )
                permission_matrix(
                    request, box.container_project(data), ident, trace=trace
                )
                trace.completed()
                before_prompts = hidden.call_count
                with trace.checkpoint("vault-rotation"):
                    write_vault(latest, password)
                # A running owner's launch snapshot survives rotation AND reload.
                run_native(
                    data,
                    connection,
                    ident,
                    state,
                    server.server_port,
                    initial,
                    trace=trace,
                )
                with trace.checkpoint("config-reload"):
                    request("POST", "/api/location/reload")
                run_native(
                    data,
                    connection,
                    ident,
                    state,
                    server.server_port,
                    initial,
                    trace=trace,
                )
                permission_matrix(
                    request, box.container_project(data), ident, trace=trace
                )
                trace.completed()
                with trace.checkpoint("default-plan"):
                    default_plan_fallback(root, data, request)
                    require(hidden.call_count == before_prompts == 1, "rotation-unlock")
                with trace.checkpoint("attach-session"):
                    with (
                        no_vault_probe(),
                        workspace_service.attach(
                            box, workspace(root, "source-a")[0], data
                        ) as attached,
                    ):
                        require(attached["session"] == ident, "attach-session")
                        workspace_service.api(box, data, attached, "GET", "/api/info")
                with trace.checkpoint("workspace-sentinels"):
                    sentinel = (
                        "import os,pathlib; pathlib.Path(os.environ['HOME'],'acceptance-home.txt').write_text('HOME_RETAINED\\n'); pathlib.Path('"
                        + OS_FILE
                        + "').write_text('OS_RETAINED\\n')"
                    )
                    box.run(
                        box.exec_command(data, ["python3", "-c", sentinel]),
                        input="",
                        capture=True,
                        private=True,
                        timeout=30,
                    )
                with trace.checkpoint("retained-transcript"):
                    progress["source-a"] = box.export_container_session(data, ident)
                    verify_v2_progress(progress["source-a"], baseline)
            return 0

        with first_launch_trace(root) as trace:
            with (
                patch.dict(os.environ, ambient),
                patch.object(vault, "hidden", return_value=password) as hidden,
                patch.object(box, "run_tui", side_effect=first_owner),
            ):
                report("first-global-owning-launch")
                trace("owning-launch")
                launcher(
                    [
                        "start",
                        "--project",
                        str(root / "source-a"),
                        "--distro",
                        distro,
                        "--import-session",
                        baseline["info"]["id"],
                    ]
                )
                with trace.checkpoint("unlock-count"):
                    require(hidden.call_count == 1, "unlock-count")
                # Check real vault/cache retention before patch.dict restores
                # the environment and could hide an accidentally inserted key.
                with trace.checkpoint("host-vault-clean"):
                    assert_host_vault_clean()
            # Remove fixture-owned ambient controls before checking the host.
            # The trace still covers the real cache/environment leak assertions.
            with trace.checkpoint("host-clean"):
                assert_host_clean()
            with trace.checkpoint("source-resources"):
                verify_owned_resources(
                    root, workspace(root, "source-a")[1], stopped=True
                )
        report("first-import-all-entries-build-asks")
        report("running-owner-rotation-reload-attach")
        report("mandatory-denials-unknown-agent-default-plan")

        def second_owner(command):
            require("--auto" not in command, "client-autoaccept")
            record_ids(root)
            current_path, data = workspace(root, "source-b")
            require("credentials" not in data, "new-owner-default")
            verify_owned_resources(root, data)
            connection = workspace_service.live_connection()
            require(connection is not None, "private-owner")
            request = request_for(data, connection)
            selected = selected_session(command, current_path)
            require(
                request("GET", "/api/session/" + selected)["data"]["location"][
                    "directory"
                ]
                == box.container_project(data),
                "selected-session",
            )
            session = create_session(
                request, box.container_project(data), agent="build"
            )
            run_native(
                data, connection, session["id"], state, server.server_port, latest
            )
            permission_matrix(request, box.container_project(data), session["id"])
            return 0

        with (
            patch.dict(os.environ, ambient),
            patch.object(vault, "hidden", return_value=password) as hidden,
            patch.object(box, "run_tui", side_effect=second_owner),
        ):
            report("second-global-owning-launch")
            launcher(["start", "--project", str(root / "source-b"), "--distro", distro])
            require(hidden.call_count == 1, "unlock-count")
        assert_host_clean()
        verify_owned_resources(root, workspace(root, "source-b")[1], stopped=True)
        report("new-owner-rotated-and-added-all-entries")
        before_policy = workspace(root, "source-b")[1].get("credentials")
        vault_before = crypto.digest(vault.vault_path())

        def opt_out(command):
            require("--auto" not in command, "client-autoaccept")
            current_path, data = workspace(root, "source-b")
            connection = workspace_service.live_connection()
            require(connection is not None, "private-owner")
            request = request_for(data, connection)
            selected = selected_session(command, current_path)
            require(
                request("GET", "/api/session/" + selected)["data"]["location"][
                    "directory"
                ]
                == box.container_project(data),
                "selected-session",
            )
            session = create_session(
                request, box.container_project(data), agent="build"
            )
            run_native(
                data,
                connection,
                session["id"],
                state,
                server.server_port,
                compatibility,
            )
            permission_matrix(request, box.container_project(data), session["id"])
            return 0

        with (
            patch.dict(os.environ, compatibility),
            no_vault_probe(),
            patch.object(box, "run_tui", side_effect=opt_out),
        ):
            report("no-vault-owning-launch")
            launcher(
                [
                    "start",
                    "--workspace",
                    workspace(root, "source-b")[1]["id"],
                    "--no-vault",
                ]
            )
        assert_host_clean()
        _, source_b = workspace(root, "source-b")
        verify_owned_resources(root, source_b, stopped=True)
        require(
            source_b.get("credentials") == before_policy
            and "credentials" not in source_b,
            "opt-out-policy-changed",
        )
        require(
            crypto.digest(vault.vault_path()) == vault_before, "opt-out-vault-changed"
        )
        report("no-vault-env-controls-no-stale-entries-no-probe")
        report("full-encrypted-backup-restore")
        full_backup_restore(
            root,
            progress["source-a"],
            latest,
            state,
            server.server_port,
            password,
            report,
        )
        ids = record_ids(root)
        require(
            set(ids) == set(PROJECTS) and len(set(ids.values())) == 3, "fixture-ids"
        )
        write_json_new(
            root / "result.json",
            {
                "status": "passed",
                "checks": report.checks,
                "source_identity": get_version_info(APP),
                "native_version": NATIVE_VERSION,
                "podman_info_schema": {
                    "exposed_configFile": "exact private storage.conf required",
                    "audited_configFile_omissions": sorted(PODMAN_CONFIGFILE_OMISSIONS),
                    "omission_requirement": "exact native Version plus fully validated private selector/config and rootless/nonremote/overlay/store/temp paths in every environment",
                },
                "workspace_ids": ids,
                "scope": "Scripted TUI exit and masked prompt input; real rootless lifecycle, native Build tools and full encrypted backup/restore. Not interactive TUI acceptance or a security certification.",
            },
        )
        report("passed")
    finally:
        primary = sys.exc_info()[1]
        for stage, action in (
            ("provider-shutdown", server.shutdown),
            ("provider-close", server.server_close),
            ("provider-join", lambda: worker.join(timeout=3)),
        ):
            try:
                action()
            except BaseException as error:
                if not isinstance(primary, FirstLaunchFailure):
                    raise
                # The first-launch report is already frozen. Retain secondary
                # provider cleanup separately in the final fixed public summary.
                primary.shutdown_errors.append(
                    {"stage": stage, "error_class": safe_error_class(error)}
                )


def cleanup(root):
    host_guard()
    root = validate_fixture(root, allow_unrecorded=True)
    require_runtime_boot(root)
    with patch.dict(os.environ, isolated_env(root), clear=True):
        validate_store(root, allow_unrecorded=True)
        records = workspace_records(root)
        # Validate the entire resource allowlist BEFORE removing its first item.
        pending = []
        with contextlib.ExitStack() as stack:
            for path, data in records.values():
                stack.enter_context(box.workspace_lock(path))
                for kind, expected in (
                    ("container", data["container"]),
                    ("volume", data["home_volume"]),
                    ("network", "oc-box-" + data["id"]),
                ):
                    found = box.resource_ids(kind, data["id"])
                    require(len(found) <= 1, "cleanup-allowlist")
                    for ident in found:
                        info = json.loads(
                            box.podman(
                                kind,
                                "inspect",
                                ident,
                                capture=True,
                                private=True,
                                timeout=30,
                            ).stdout
                        )[0]
                        labels = (
                            info.get(
                                "Labels",
                                info.get(
                                    "labels", info.get("Config", {}).get("Labels", {})
                                ),
                            )
                            or {}
                        )
                        name = info.get("Name", info.get("name", "")).lstrip("/")
                        require(
                            name == expected and labels.get(box.LABEL) == data["id"],
                            "cleanup-allowlist",
                        )
                        if kind == "volume":
                            require(
                                no_symlinks(info["Mountpoint"])
                                == root
                                / "data/containers/storage/volumes"
                                / expected
                                / "_data",
                                "private-volume",
                            )
                        if kind == "container":
                            expected_home = str(
                                root
                                / "data/containers/storage/volumes"
                                / data["home_volume"]
                                / "_data"
                            )
                            mounts = {
                                (item.get("Source"), item.get("Destination"))
                                for item in info.get("Mounts", [])
                                if item.get("Type") in ("bind", "volume")
                            }
                            require(
                                mounts
                                == {
                                    (data["project"], box.container_project(data)),
                                    (expected_home, data["home"]),
                                },
                                "cleanup-mounts",
                            )
                            require(
                                not info.get("HostConfig", {}).get("PortBindings"),
                                "published-ports",
                            )
                        pending.append((kind, ident))
            validate_store(root, allow_unrecorded=True)
            # SIGKILL can precede the fixture's finally journal. Reconcile only
            # the complete, strictly validated production journals after ALL
            # resource candidates and the actual private store have passed.
            ids = record_ids(root)
            for kind, ident in pending:
                box.podman(
                    kind,
                    "rm",
                    *(["--force"] if kind == "container" else []),
                    ident,
                    capture=True,
                    private=True,
                    timeout=60,
                )
    return ids


def state_error_category(message):
    """Fixed categories only; never return private container error text."""
    if not isinstance(message, str) or not message:
        return None
    text = message.lower()
    for terms, category in (
        (("cgroup", "systemd"), "cgroup-runtime"),
        (("quota", "no space left", "storage"), "storage"),
        (("permission denied", "operation not permitted"), "permission"),
        (("mount", "selinux"), "mount-or-label"),
        (("executable", "not found"), "missing-runtime-file"),
    ):
        if any(term in text for term in terms):
            return category
    return "unclassified"


def diagnostic_store_probes(root):
    """Bounded selector/native comparisons; raw output stays in anonymous memory.

    Only info/--version are allowed. Even the direct native call retains BOTH
    audited private config selectors; never fall back to the operator's store.
    These results explain a refusal, never authorize resource inspection.
    """
    root = validate_fixture(root, allow_unrecorded=True)
    require_runtime_boot(root)
    mapping = private_json(root, "podman-executable.json")
    native = str(validate_native_podman(mapping["podman"], root))
    wrapper = str(safe_path(root, "bin/podman"))
    normal = isolated_env(root)
    with patch.dict(os.environ, normal, clear=True):
        environments = (
            ("normal-store", wrapper, normal),
            ("maintenance-store", wrapper, box.maintenance_host_env()),
            ("backup-store", wrapper, crypto.environment()),
            ("recorded-native-store", native, normal),
        )
    probes = [
        ("recorded-wrapper-version", wrapper, normal, ["--version"]),
        ("recorded-native-version", native, normal, ["--version"]),
        *[
            (label, binary, env, ["info", "--format", "json"])
            for label, binary, env in environments
        ],
    ]
    results = []
    for label, binary, env, arguments in probes:
        item = {"probe": label}
        try:
            completed = box.run(
                [binary, *arguments],
                env=env,
                stdin=subprocess.DEVNULL,
                capture=True,
                private=True,
                check=False,
                timeout=15 if arguments == ["--version"] else 30,
            )
            item.update(
                exit_code=completed.returncode,
                stdout_present=bool(completed.stdout),
                stderr_present=bool(completed.stderr),
                stderr_category=stderr_category(completed.stderr),
            )
            if completed.returncode == 0:
                if arguments == ["--version"]:
                    version = re.fullmatch(
                        r"podman version ([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})\s*",
                        completed.stdout[:128],
                    )
                    item["version"] = version[1] if version else None
                else:
                    validate_store_info(
                        root, json.loads(completed.stdout), env["TMPDIR"]
                    )
                    item["private_store_verified"] = True
        except Exception as error:
            item["error_class"] = safe_error_class(error)
            # A fixture assertion's label is safe only when explicitly listed.
            item["validation_category"] = (
                str(error)
                if isinstance(error, FixtureError)
                and str(error)
                in {
                    "private-store",
                    "rootless",
                    "remote-engine",
                    "storage-driver",
                    "store-info-shape",
                    "temporary-path",
                    "image-temporary-path",
                }
                else "operation-failed"
            )
        results.append(item)
    return results


def diagnose(root):
    """Inspect retained checkpoints; never start/exec, unlock, sync or replay."""
    host_guard()
    root = validate_fixture(root, allow_unrecorded=True)
    result = {
        "status": "diagnostic-only",
        "scope": "Retained fixture inspection with a file-only boot guard and same-boot informational engine queries; no launch/replay or host acceptance, not database-byte immutability.",
        "fixture_root": str(root),
        "source_version": get_version_info(APP)["version"],
        "workspaces": {},
    }
    # info can initialize/refresh Podman state; it is not a byte-read-only
    # filesystem reader. Refuse an old/missing/invalid boot marker before any
    # engine command, rather than repeatedly probing or implicitly refreshing.
    try:
        checkpoint = require_runtime_boot(root)
    except Exception as error:
        result.update(
            status="diagnostic-failed",
            failed_stage="runtime-boot",
            error_class=safe_error_class(error),
            error_category=str(error)
            if isinstance(error, FixtureError)
            and str(error)
            in {
                "runtime-boot-current",
                "runtime-boot-marker",
                "runtime-boot-mismatch",
            }
            else "runtime-boot-inspection",
            engine_probes=[],
        )
        # The checkpoint itself contains only booleans/None. Read it again only
        # for a classified mismatch; unsafe or unreadable files stay refused.
        if result["error_category"] == "runtime-boot-mismatch":
            result["runtime_boot"] = runtime_boot_checkpoint(root)
        return result
    result["runtime_boot"] = checkpoint
    with patch.dict(os.environ, isolated_env(root), clear=True):
        try:
            validate_store(root, allow_unrecorded=True)
        except Exception as error:
            result.update(
                status="diagnostic-failed",
                failed_stage="private-store",
                error_class=safe_error_class(error),
                engine_probes=diagnostic_store_probes(root),
            )
            # NEVER inspect containers/volumes or alter evidence after refusal,
            # even if one of the comparison probes happened to succeed.
            return result
        for name, (path, data) in workspace_records(root).items():
            info = backups.owned_container(box, data)
            state = {} if info is None else info.get("State", {})
            status = state.get("Status")
            allowed = {
                "configured",
                "created",
                "running",
                "stopped",
                "exited",
                "paused",
                "stopping",
            }
            volume = box.podman(
                "volume",
                "exists",
                data["home_volume"],
                capture=True,
                private=True,
                check=False,
                timeout=30,
            )
            require(volume.returncode in (0, 1), "diagnostic-volume")
            if volume.returncode == 0:
                owned = backups.owned_volume(box, data)
                require(
                    owned.get("Name") == data["home_volume"]
                    and no_symlinks(owned["Mountpoint"])
                    == root
                    / "data/containers/storage/volumes"
                    / data["home_volume"]
                    / "_data",
                    "private-volume",
                )
            imports = path.parent / "imports-v2"
            result["workspaces"][name] = {
                "id": data["id"],
                "native_version": data.get("opencode_version"),
                "baseline_ready": bool(data.get("ready")),
                "container_present": info is not None,
                "container_status": status
                if status in allowed
                else ("other" if info is not None else None),
                "container_running": bool(state.get("Running")),
                "container_exit_code": state.get("ExitCode")
                if isinstance(state.get("ExitCode"), int)
                else None,
                "container_error_category": state_error_category(state.get("Error")),
                "private_home_present": volume.returncode == 0,
                "selected_session": box.session_state(path).get("last"),
                "native_import_exports": len(list(imports.glob("*.json")))
                if imports.is_dir()
                else 0,
            }
    return result


SOURCE_LOADING_STAGES = {
    "Checking rootless Podman": "private-store",
    "Checking OpenCode version": "native-version",
    "Preparing OpenCode and syncing configuration": "runtime-prepare",
    "Installing OpenCode 2.x plugin dependencies": "plugin-dependencies",
    "Preparing OpenCode 2.x private service and plugins": "runtime-plugin-readiness",
    "Finding workspace session": "session-resolve",
    "Syncing session history": "history-sync",
    "Stopping container": "container-stop",
    **{f"Starting {name} container": "container-start" for name in box.DISTROS},
}
SOURCE_STAGES = (
    frozenset(SOURCE_LOADING_STAGES.values())
    | PROBE_STAGES
    | {
        "source-validation",
        "source-resources",
        "source-stop-check",
        "retry-preparation",
        "owning-launch",
        "private-owner",
        "callback",
        "vault-rotation",
        "config-reload",
        "default-plan",
        "attach-session",
        "workspace-sentinels",
        "selected-session",
        "retained-transcript",
        "plugin-readiness",
        "host-provider-health",
        "transcript-validation",
        "api-model-default",
        "api-plugin-list",
        "provider-shutdown",
        "private-owner-stop",
        "permission-matrix",
        "permission-cleanup",
        "unlock-count",
        "host-clean",
        "host-vault-clean",
        "evidence-preservation",
        "complete",
        "launcher-stage",
    }
)


def program_label(command):
    """Never include executable paths or any argument in a diagnostic."""
    if not isinstance(command, (list, tuple)) or not command:
        return "other"
    name = Path(str(command[0])).name
    if name in ("podman", "npm", "git", "age"):
        return name
    if re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", name):
        return "python"
    if name == "opencode" or name.startswith("opencode-"):
        return "opencode"
    return "other"


def safe_error_class(error):
    for kind, label in (
        (FixtureError, "fixture-error"),
        (box.BoxError, "box-error"),
        (subprocess.TimeoutExpired, "timeout"),
        (subprocess.CalledProcessError, "subprocess-exit"),
        (AssertionError, "assertion"),
        (FileNotFoundError, "missing-file"),
        (OSError, "os-error"),
        ((ValueError, KeyError, TypeError), "invalid-data"),
        ((KeyboardInterrupt, SystemExit), "interrupted"),
    ):
        if isinstance(error, kind):
            return label
    return "exception"


def private_stderr(stream):
    """Read anonymous stderr without seeking, replaying or altering its owner."""
    if isinstance(stream, (str, bytes)):
        return stream[:65536]
    try:
        descriptor = stream if isinstance(stream, int) else stream.fileno()
        if descriptor < 0 or "memfd:" not in os.readlink(f"/proc/self/fd/{descriptor}"):
            return b""
        return os.pread(descriptor, 65536, 0)
    except (AttributeError, OSError, ValueError):
        return b""


def probe_diagnostic(message):
    """Accept only the probe's exact fixed JSON schema; discard everything else."""
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    if not isinstance(message, str):
        return None
    for line in message[:65536].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(value, dict)
            and set(value)
            == {"stage", "error_class", "native_exit_code", "stderr_category"}
            and isinstance(value["stage"], str)
            and value["stage"] in PROBE_STAGES
            and isinstance(value["error_class"], str)
            and value["error_class"] in ERROR_CLASSES
            and isinstance(value["stderr_category"], str)
            and value["stderr_category"] in STDERR_CATEGORIES
            and (
                value["native_exit_code"] is None
                or type(value["native_exit_code"]) is int
            )
        ):
            return value
    return None


class SourceRetryTrace:
    """Owning-launch diagnostics, frozen before the launcher's finally cleanup."""

    def __init__(self):
        self.stage = "source-validation"
        self.failure = None
        self.subprocess_failure = None

        self.steps = []
        self.cleanup_errors = []
        # Exception identity is private memory only. Re-propagating a callback
        # error after successful shutdown is not a new container-stop failure.
        self._seen_failures = []

    def __call__(self, stage):
        require(stage in SOURCE_STAGES, "retry-stage")
        self.stage = stage
        self.subprocess_failure = None
        self.steps.append({"stage": stage, "state": "started"})

    def completed(self, stage=None):
        for step in reversed(self.steps):
            if (
                step["stage"] == (self.stage if stage is None else stage)
                and step["state"] == "started"
            ):
                step["state"] = "completed"
                break

    @contextlib.contextmanager
    def checkpoint(self, stage):
        self(stage)
        try:
            yield
            self.completed(stage)
        except BaseException as error:
            self.fail(error)
            raise

    def observe(self, command, exit_code, stderr, error_class="subprocess-exit"):
        self.subprocess_failure = {
            "stage": self.stage,
            "program": program_label(command),
            "exit_code": exit_code if type(exit_code) is int else None,
            "stderr_category": stderr_category(stderr),
            "error_class": error_class
            if isinstance(error_class, str) and error_class in ERROR_CLASSES
            else "exception",
        }
        native = probe_diagnostic(stderr)
        if native is not None:
            self.subprocess_failure.update(native)

    def fail(self, error):
        if any(previous is error for previous in self._seen_failures):
            return
        self._seen_failures.append(error)
        diagnostic = {
            "stage": self.stage,
            "error_class": safe_error_class(error),
            "program": "other",
            "exit_code": None,
            "stderr_category": "empty",
        }
        if self.subprocess_failure is not None:
            diagnostic.update(self.subprocess_failure)
        for step in reversed(self.steps):
            if step["stage"] == self.stage and step["state"] == "started":
                step["state"] = "failed"
                break
        if self.failure is None:
            self.failure = diagnostic
        elif self.stage in {
            "container-stop",
            "history-sync",
            "provider-shutdown",
            "evidence-preservation",
            "host-clean",
            "private-owner-stop",
            "permission-cleanup",
            "source-stop-check",
        }:
            if diagnostic not in self.cleanup_errors and diagnostic != self.failure:
                self.cleanup_errors.append(diagnostic)

    @contextlib.contextmanager
    def installed(self):
        actual_run = subprocess.run
        actual_loading = box.loading_stage
        actual_owner = workspace_service.Owner.__enter__
        actual_shutdown = workspace_service.Owner.shutdown

        def observed_run(command, *args, **kwargs):
            try:
                result = actual_run(command, *args, **kwargs)
            except BaseException as error:
                self.observe(
                    command,
                    getattr(error, "returncode", None),
                    private_stderr(
                        getattr(error, "stderr", None) or kwargs.get("stderr")
                    ),
                    safe_error_class(error),
                )
                raise
            if result.returncode:
                self.observe(
                    command,
                    result.returncode,
                    private_stderr(
                        result.stderr
                        if result.stderr is not None
                        else kwargs.get("stderr")
                    ),
                )
            else:
                self.subprocess_failure = None
            return result

        @contextlib.contextmanager
        def loading(label, **kwargs):
            stage = SOURCE_LOADING_STAGES.get(label, "launcher-stage")
            self(stage)
            try:
                with actual_loading(label, **kwargs):
                    yield
                self.completed(stage)
            except BaseException as error:
                self.fail(error)
                raise

        def owner_enter(owner):
            self("private-owner")
            try:
                result = actual_owner(owner)
                self.completed("private-owner")
                return result
            except BaseException as error:
                self.fail(error)
                raise

        def owner_shutdown(owner):
            if not owner.ready and hasattr(owner, "error_file"):
                child = owner.child
                self.observe(
                    ["opencode"],
                    None if child is None else child.poll(),
                    private_stderr(owner.error_file),
                )
                original_error = sys.exc_info()[1]
                if original_error is not None:
                    self.fail(original_error)
            try:
                return actual_shutdown(owner)
            except BaseException as error:
                self("private-owner-stop")
                self.fail(error)
                raise

        with (
            patch.object(subprocess, "run", side_effect=observed_run),
            patch.object(box, "loading_stage", loading),
            patch.object(workspace_service.Owner, "__enter__", owner_enter),
            patch.object(workspace_service.Owner, "shutdown", owner_shutdown),
        ):
            yield


class FirstLaunchFailure(FixtureError):
    """Only fixed diagnostics cross the fixture's public output boundary."""

    def __init__(self, diagnostic, report_written, report_error):
        super().__init__("first-owning-launch-failed")
        self.diagnostic = diagnostic
        self.report_written = report_written
        self.report_error = report_error
        self.shutdown_errors = []


@contextlib.contextmanager
def first_launch_trace(root):
    """Retain NEW first-launch failures; never overwrite an earlier report."""
    report = safe_path(root, "first-launch-failure.json", optional=True)
    require(not report.exists(), "first-launch-report-exists")
    trace = SourceRetryTrace()
    with trace.installed():
        try:
            yield trace
        except BaseException as error:
            trace.fail(error)
            result = {
                "status": "failed",
                "scope": "new-fixture-first-owning-launch",
                "acceptance": "Failed first owning launch; no full fixture acceptance and no replay of an earlier fixture.",
                "source_identity": None,
                "error": trace.failure,
                "steps": trace.steps,
                "cleanup_errors": trace.cleanup_errors,
            }
            report_written = False
            report_error = None
            try:
                result["source_identity"] = get_version_info(APP)
            except Exception as journal_error:
                report_error = safe_error_class(journal_error)
                result["report_error_class"] = report_error
            try:
                write_json_new(report, result)
                report_written = True
            except Exception as journal_error:
                # Do not mask the primary launch failure with a report-write
                # failure or overwrite an independently created report.
                report_error = safe_error_class(journal_error)
            raise FirstLaunchFailure(
                trace.failure, report_written, report_error
            ) from None


def validate_source_retry(root):
    """Read-only checkpoint validation. Never consult/decrypt the old vault."""
    root = validate_fixture(root)

    def later_evidence(name):
        return (
            name
            in {
                "result.json",
                "age-identity.txt",
                "retired",
                "recovered-registry",
                "archive-origin",
            }
            or "backup" in name
            or "restore" in name
            or name.endswith((".ocbox", ".age"))
        )

    for entry in root.iterdir():
        require(not later_evidence(entry.name), "retry-later-evidence")
    # Never traverse/move the graphroot/runroot. These small synthetic project,
    # registry and temporary journals suffice for this fixture's later stages.
    scanned = 0
    for name in ("temporary", "registry", "source-a", "source-b"):
        folder = safe_path(root, name, directory=True, optional=name == "source-b")
        if not folder.exists():
            continue
        for child in folder.rglob("*"):
            scanned += 1
            require(scanned <= 4096, "retry-evidence-bound")
            no_symlinks(child)
            require(not later_evidence(child.name), "retry-later-evidence")
            require(name != "source-b" or child.name != box.MARKER, "retry-source-only")
    records = workspace_records(root)
    require(set(records) == {"source-a"}, "retry-source-only")
    path, data = records["source-a"]
    require(
        data.get("ready") is True
        and data.get("opencode_version") == NATIVE_VERSION
        and not any("restore" in name or "backup" in name for name in data),
        "retry-source-state",
    )
    baseline = private_json(root, "seed-baseline.json")
    ident = baseline.get("info", {}).get("id")
    require(box.valid_session_id(ident), "retry-seed")
    box.validate_session_export(baseline, ident)
    require(
        compat.native(baseline)
        and compat.directory(baseline) == data["project"]
        and baseline["info"].get("permissions") == ASK_RULES
        and len(baseline["messages"]) == 1
        and baseline["messages"][0].get("type") == "user"
        and baseline["messages"][0].get("text") == SEED_TEXT,
        "retry-seed",
    )
    state = private_json(root, box.session_file(path).relative_to(root))
    evidence = path.parent / "imports-v2" / (ident + ".json")
    recorded = state.get("sessions")
    require(
        isinstance(recorded, list)
        and len(recorded) <= 16
        and all(box.valid_session_id(item) for item in recorded)
        and len(recorded) == len(set(recorded))
        and box.valid_session_id(state.get("last"))
        and state["last"] in recorded
        and ident in recorded
        and state.get("imported_from_host") == {ident: {"backup": str(evidence)}},
        "retry-verified-import",
    )
    safe_path(root, evidence.parent.relative_to(root), directory=True)
    require(list(evidence.parent.iterdir()) == [evidence], "retry-verified-import")
    exported = private_json(root, evidence.relative_to(root))
    verify_v2_progress(exported, baseline)
    require(
        exported["messages"] == baseline["messages"]
        and exported["info"].get("permissions") == ASK_RULES,
        "retry-verified-import",
    )
    mapping = private_json(root, "executables.json")
    require(
        isinstance(mapping, dict)
        and set(mapping) == {"2"}
        and isinstance(mapping["2"], str),
        "retry-native-executable",
    )
    binary = validate_native_podman(mapping["2"], root)
    wrapper = safe_path(root, "bin/opencode")
    previous = wrapper.read_text()
    first = previous.splitlines()[0] if previous else ""
    require(
        first.startswith("#!/")
        and os.access(wrapper, os.X_OK)
        and previous == native_wrapper_source(first[2:]),
        "retry-native-selector",
    )
    for name in ("config", "data", "state", "cache"):
        safe_path(root, "host-" + name + "-2", directory=True)
    config = private_json(root, "config/opencode/opencode.json")
    url = (
        config.get("providers", {})
        .get("fixture", {})
        .get("settings", {})
        .get("baseURL")
    )
    match = re.fullmatch(
        r"http://host\.containers\.internal:([0-9]{1,5})/v1", url or ""
    )
    require(
        match is not None
        and 0 < int(match[1]) < 65536
        and config == fixture_config(int(match[1]), root),
        "retry-source-config",
    )
    plugin = "config/opencode/plugins/fixture-global-vault-permission/"
    require(
        safe_path(root, plugin + "index.js").read_text() == TOOL_SOURCE
        and safe_path(root, plugin + "package.json").read_text()
        == '{"type":"module","main":"index.js"}\n',
        "retry-source-plugin",
    )
    require(
        private_json(root, "config/oc-box/config.json")
        == {
            "default_distro": data["distro"],
            "pass_env": [PASS_ENV],
            "auto_update": False,
        },
        "retry-source-config",
    )
    original = safe_path(root, "data/oc-box/credentials/vault.json")
    info = original.stat()
    require(
        info.st_uid == os.getuid()
        and not info.st_mode & 0o077
        and info.st_nlink == 1
        and info.st_size <= vault.MAX_FILE,
        "retry-original-vault",
    )
    checkpoint = runtime_boot_checkpoint(root)
    require(checkpoint["boot_ids_match"] is True, "retry-runtime-boot")
    return root, path, data, baseline, binary


def source_evidence_digests(root, path):
    """Immutable ORIGINAL evidence only; live native DB/session state may grow."""
    files = (
        "fixture.json",
        "fixture-ids.json",
        "seed-baseline.json",
        "executables.json",
        "podman-executable.json",
        "bin/opencode",
        "bin/podman",
        "data/oc-box/credentials/vault.json",
    )
    result = {name: crypto.digest(safe_path(root, name)) for name in files}
    for directory in (root / "config", path.parent / "imports-v2", root / "vault"):
        if directory.exists():
            safe_path(root, directory.relative_to(root), directory=True)
            for name, digest in tree_digest(directory).items():
                result[str(directory.relative_to(root) / name)] = digest
    return result


def prepare_source_retry_selector(root, attempt, binary):
    # A lost creation-time uv interpreter is not a reason to edit old evidence.
    interpreter = Path(getattr(sys, "_base_executable", sys.executable)).resolve(
        strict=True
    )
    validate_native_podman(interpreter, root)
    wrapper = attempt / "bin/opencode"
    wrapper.parent.mkdir(mode=0o700)
    with wrapper.open("x") as output:
        output.write(native_wrapper_source(interpreter, root=root, binary=binary))
    wrapper.chmod(0o700)
    return wrapper


def verify_stopped_source(root, data):
    info = verify_owned_resources(root, data, stopped=True)
    state = info.get("State", {})
    require(
        state.get("Running") is False and state.get("Status") in {"stopped", "exited"},
        "retry-source-state",
    )
    return info


def retry_source(root, temp_parent=None):
    """Retry one retained owner/native check, NOT the complete acceptance fixture."""
    host_guard()
    root, path, source, baseline, binary = validate_source_retry(root)
    if temp_parent is not None:
        temp_parent = existing_directory(temp_parent)
    before = source_evidence_digests(root, path)
    selected_before = private_json(root, box.session_file(path).relative_to(root))[
        "last"
    ]
    directory = safe_path(root, "source-retries", directory=True, optional=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "retry-directory")
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=directory))
    trace = SourceRetryTrace()
    result = {
        "status": "failed",
        "scope": "retained-source-only",
        "acceptance": "Only retained source owning launch/native checks; no first-ever launch/import, second project, rotation/reload/attach or backup/restore acceptance.",
        "source_identity": None,
        "native_version": NATIVE_VERSION,
        "workspace_ids": {"source-a": source["id"]},
        "selected_session_before": selected_before,
        "diagnostic_session": baseline["info"]["id"],
        "checks": [],
        "evidence_before": before,
        "steps": trace.steps,
        "cleanup_errors": trace.cleanup_errors,
    }
    server = worker = None
    resources_verified = False
    try:
        result["source_identity"] = get_version_info(APP)
        with (
            patch.dict(os.environ, isolated_env(root), clear=True),
            temporary_location(root, temp_parent),
            trace.installed(),
        ):
            trace("private-store")
            validate_store(root)
            trace.completed()
            result["checks"].append("private-store")
            trace("native-version")
            check_native_version(root, binary, "2")
            trace.completed()
            result["checks"].append("recorded-native-version")
            trace("source-resources")
            verify_stopped_source(root, source)
            resources_verified = True
            trace.completed()
            result["checks"].append("retained-stopped-source")
            trace("retry-preparation")
            box.check_backend(feature="launch")
            vault.crypto()
            prepare_source_retry_selector(root, attempt, binary)
            state = {
                "values": {},
                "requests": 0,
                "before": 0,
                "markers": set(),
                "errors": 0,
            }
            server, worker = make_provider(state)
            (attempt / "config/opencode").mkdir(parents=True, mode=0o700)
            (attempt / "config/oc-box").mkdir(mode=0o700)
            write_json_new(
                attempt / "config/opencode/opencode.json",
                fixture_config(server.server_port, attempt),
            )
            write_json_new(
                attempt / "config/oc-box/config.json",
                private_json(root, "config/oc-box/config.json"),
            )
            write_plugin(attempt)
            password = secrets.token_urlsafe(32)
            values = {
                name: "fixture-" + secrets.token_urlsafe(24)
                for name in (KEY, SIDE, ADDED)
            }
            cipher = attempt / "vault/vault.json"
            require(not cipher.exists(), "retry-companion-vault")
            actual_start = box.start_container

            def start_retained(current_path, data):
                # Called under the launcher's lifecycle lock. Refuse recreation
                # if the retained source disappeared/changed since validation.
                _, checked_path, checked, _, _ = validate_source_retry(root)
                require(
                    current_path == checked_path
                    and data == checked
                    and checked["id"] == source["id"],
                    "retry-source-state",
                )
                require(
                    source_evidence_digests(root, checked_path) == before,
                    "retry-evidence-changed",
                )
                verify_stopped_source(root, checked)
                return actual_start(current_path, data)

            def owner(command):
                trace("callback")
                try:
                    require("--auto" not in command, "client-autoaccept")
                    current_path, data = workspace(root, "source-a")
                    trace("source-resources")
                    verify_owned_resources(root, data)
                    trace.completed()
                    trace("private-owner")
                    connection = workspace_service.live_connection()
                    require(connection is not None, "private-owner")
                    trace.completed()
                    trace("selected-session")
                    selected = selected_session(command, current_path, selected_before)
                    request = request_for(data, connection)
                    selected_info = request("GET", "/api/session/" + selected)["data"]
                    validate_selected_source_session(selected_info, selected, data)
                    result["actual_client_selected_session"] = selected
                    ident = baseline["info"]["id"]
                    trace.completed()
                    result["checks"].append("actual-client-selected-session")
                    trace("retained-transcript")
                    other = (
                        box.export_container_session(data, selected)
                        if selected != ident
                        else None
                    )
                    if other is not None:
                        box.validate_session_export(other, selected)
                    verify_v2_progress(
                        box.export_container_session(data, ident), baseline
                    )
                    result["checks"].append("retained-seed-before-native")
                    trace.completed()
                    run_native(
                        data,
                        connection,
                        ident,
                        state,
                        server.server_port,
                        values,
                        trace=trace,
                    )
                    result["checks"].append("native-tools-events-transcript")
                    permission_matrix(
                        request_for(data, connection),
                        box.container_project(data),
                        ident,
                        trace=trace,
                    )
                    result["checks"].append("permission-matrix")
                    trace.completed()
                    trace("retained-transcript")
                    verify_v2_progress(
                        box.export_container_session(data, ident), baseline
                    )
                    if other is not None:
                        unchanged = box.export_container_session(data, selected)
                        require(
                            unchanged["info"]["id"] == selected
                            and unchanged["messages"] == other["messages"],
                            "other-session-changed",
                        )
                        result["checks"].append(
                            "other-recorded-session-retained-unchanged"
                        )
                    result["checks"].append("retained-seed-after-native")
                    trace.completed()
                    trace.completed("callback")
                    return 0
                except BaseException as error:
                    trace.fail(error)
                    raise

            with patch.object(vault, "vault_path", return_value=cipher):
                write_vault(values, password)
                trace.completed("retry-preparation")
                # Only the owner gets the new config. Podman still resolves the
                # UNCHANGED original selector/config/store and original registry.
                with (
                    patch.dict(
                        os.environ,
                        {
                            "XDG_CONFIG_HOME": str(attempt / "config"),
                            "PATH": str(attempt / "bin") + ":" + os.environ["PATH"],
                        },
                    ),
                    patch.object(vault, "hidden", return_value=password) as hidden,
                    patch.object(box, "run_tui", side_effect=owner),
                    patch.object(box, "start_container", side_effect=start_retained),
                ):
                    trace("owning-launch")
                    launcher(["start", "--workspace", source["id"]])
                    trace.completed("owning-launch")
                    trace("unlock-count")
                    require(hidden.call_count == 1, "unlock-count")
                    trace.completed()
            trace("host-clean")
            assert_host_clean()
            trace.completed()
            trace("source-resources")
            verify_stopped_source(root, workspace(root, "source-a")[1])
            trace.completed()
            result["checks"].append("source-stopped-host-clean")
            result["status"] = "passed"
    except BaseException as error:
        trace.fail(error)
    finally:
        if resources_verified:
            trace("source-stop-check")
            try:
                with (
                    patch.dict(os.environ, isolated_env(root), clear=True),
                    trace.installed(),
                ):
                    validate_fixture(root)
                    verify_stopped_source(root, workspace(root, "source-a")[1])
                result["source_container_stopped"] = True
                trace.completed()
            except BaseException as error:
                result["source_container_stopped"] = False
                trace.fail(error)
        try:
            trace("host-clean")
            assert_host_clean()
            trace.completed()
        except BaseException as error:
            trace.fail(error)
        if server is not None:
            trace("provider-shutdown")
            try:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)
                trace.completed()
            except BaseException as error:
                trace.fail(error)
        try:
            trace("evidence-preservation")
            after = source_evidence_digests(root, path)
            result["evidence_after"] = after
            result["evidence_unchanged"] = after == before
            require(after == before, "retry-evidence-changed")
            trace.completed()
            result["checks"].append("original-evidence-byte-preserved")
        except BaseException as error:
            trace.fail(error)
            result["evidence_unchanged"] = False
        if trace.failure is not None:
            result["status"] = "failed"
            result["error"] = trace.failure
        report_path = attempt / "retry-result.json"
        write_json_new(report_path, result)
    return report_path, result


class FixtureArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(
            2, "global-vault fixture: FAILED phase=arguments category=invalid-options\n"
        )


def parser():
    value = FixtureArgumentParser(description=__doc__)
    value.add_argument(
        "--opencode-v2",
        type=Path,
        help="Read-only unmodified OpenCode 2.2.6 executable; defaults to PATH discovery",
    )
    value.add_argument(
        "--work-dir",
        type=Path,
        help="Existing disk-backed parent for a fresh private retained fixture (required for a new run)",
    )
    value.add_argument(
        "--temp-dir",
        type=Path,
        help="Existing parent for private temporary image/encrypted staging; defaults inside the fixture",
    )
    value.add_argument("--distro", choices=tuple(box.DISTROS), default="tumbleweed")
    value.add_argument(
        "--cleanup",
        type=Path,
        help="Remove only owned resources in this fixture's verified private store; preserve all files/images",
    )
    value.add_argument(
        "--diagnose",
        type=Path,
        help="Retained checkpoints with a file-only boot guard and same-boot informational engine queries; no container start, vault unlock or replay",
    )
    value.add_argument(
        "--retry-source",
        type=Path,
        help="Retry only a retained stopped first source owner/native checks with a new companion encrypted vault; preserve original evidence, refuse later-stage evidence",
    )
    return value


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(arguments)  # --help is harmless inside containers.
    root = None
    phase = "host-guard"
    old_umask = os.umask(0o077)
    output = os.fdopen(os.dup(1), "w", buffering=1)

    def report(name):
        nonlocal phase
        phase = name
        if name != "passed":
            report.checks.append(name)
        print("global-vault fixture: " + name, file=output, flush=True)

    report.checks = []

    try:
        host_guard()
        if args.retry_source is not None:
            require(
                args.cleanup is None
                and args.diagnose is None
                and args.work_dir is None
                and args.opencode_v2 is None
                and not any(
                    value == "--distro" or value.startswith("--distro=")
                    for value in arguments
                ),
                "retry-options",
            )
            phase = "retry-source-validation"
            root = validate_fixture(args.retry_source)
            with private_output():
                report_path, result = retry_source(root, args.temp_dir)
            print("Retained root: " + str(root), file=output)
            print("Report: " + str(report_path), file=output)
            if result["status"] == "passed":
                print(
                    "global-vault fixture: retained-source-only PASSED; not a full fixture/import acceptance",
                    file=output,
                )
                return 0
            diagnostic = result["error"]
            print(
                "global-vault fixture: FAILED scope=retained-source-only stage="
                + diagnostic["stage"]
                + " class="
                + diagnostic["error_class"]
                + " program="
                + diagnostic["program"]
                + " exit_code="
                + str(diagnostic["exit_code"])
                + " native_exit_code="
                + str(diagnostic.get("native_exit_code"))
                + " stderr_category="
                + diagnostic["stderr_category"],
                file=output,
            )
            return 1
        if args.diagnose is not None:
            require(
                args.cleanup is None
                and args.work_dir is None
                and args.temp_dir is None
                and args.opencode_v2 is None
                and not any(
                    value == "--distro" or value.startswith("--distro=")
                    for value in arguments
                ),
                "diagnostic-options",
            )
            root = validate_fixture(args.diagnose, allow_unrecorded=True)
            phase = "diagnostic-inspection"
            with private_output():
                result = diagnose(root)
            print(json.dumps(result, indent=2), file=output)
            return 1 if result["status"] == "diagnostic-failed" else 0
        if args.cleanup is not None:
            require(
                args.work_dir is None
                and args.temp_dir is None
                and args.opencode_v2 is None
                and not any(
                    value == "--distro" or value.startswith("--distro=")
                    for value in arguments
                ),
                "cleanup-options",
            )
            root = validate_fixture(args.cleanup, allow_unrecorded=True)
            phase = "cleanup-validation"
            with private_output():
                ids = cleanup(root)
            print(
                "global-vault fixture: owned resources removed; evidence and images retained",
                file=output,
            )
            print("Retained root: " + str(root), file=output)
            for name, ident in sorted(ids.items()):
                print("Workspace " + name + ": " + ident, file=output)
            return 0
        require(args.work_dir is not None, "work-dir-required")
        parent = existing_directory(args.work_dir)
        if args.temp_dir is not None:
            existing_directory(args.temp_dir)
        discovered = args.opencode_v2 or shutil.which(
            "opencode", path=box.maintenance_host_env().get("PATH", "/usr/bin:/bin")
        )
        require(discovered is not None, "native-executable")
        binary = Path(discovered).expanduser().resolve(strict=True)
        require(binary.is_file() and os.access(binary, os.X_OK), "native-executable")
        native_podman = shutil.which(
            "podman", path=box.maintenance_host_env().get("PATH", "/usr/bin:/bin")
        )
        require(native_podman is not None, "native-podman")
        native_podman = Path(native_podman).resolve(strict=True)
        root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=parent))
        root.chmod(0o700)
        print("Retained root: " + str(root), file=output)
        phase = "fixture-preparation"
        with private_output():
            prepare_root(root, args.distro)
            prepare_podman_cli(root, native_podman)
            with (
                patch.dict(os.environ, isolated_env(root), clear=True),
                temporary_location(root, args.temp_dir),
            ):
                try:
                    run(root, binary, args.distro, report)
                finally:
                    # Journal production-created IDs even after a later failure.
                    record_ids(root)
        ids = private_json(root, "fixture-ids.json")
        for name, ident in sorted(ids.items()):
            print("Workspace " + name + ": " + ident, file=output)
        print("Report: " + str(root / "result.json"), file=output)
        return 0
    except BaseException as error:
        if isinstance(error, FirstLaunchFailure):
            diagnostic = error.diagnostic
            print(
                "global-vault fixture: FAILED phase=first-global-owning-launch stage="
                + diagnostic["stage"]
                + " class="
                + diagnostic["error_class"]
                + " program="
                + diagnostic["program"]
                + " exit_code="
                + str(diagnostic["exit_code"])
                + " stderr_category="
                + diagnostic["stderr_category"]
                + " report_error_class="
                + str(error.report_error),
                file=output,
            )
            if error.shutdown_errors:
                print(
                    "First-launch secondary cleanup: "
                    + json.dumps(error.shutdown_errors),
                    file=output,
                )
        elif args.retry_source is not None:
            # Refusals before attempt creation have no report. Do not echo even
            # FixtureError text unless it is one of these fixed public labels.
            refusals = {
                "host-only",
                "retry-options",
                "retry-later-evidence",
                "retry-source-only",
                "retry-source-state",
                "retry-seed",
                "retry-verified-import",
                "retry-evidence-bound",
                "retry-native-executable",
                "retry-native-selector",
                "retry-source-config",
                "retry-source-plugin",
                "retry-original-vault",
                "retry-runtime-boot",
                "retry-directory",
            }
            category = (
                str(error)
                if isinstance(error, FixtureError) and str(error) in refusals
                else "operation-failed"
            )
            print(
                "global-vault fixture: FAILED scope=retained-source-only stage="
                + phase
                + " class="
                + safe_error_class(error)
                + " exit_code=None stderr_category=empty category="
                + category,
                file=output,
            )
        else:
            category = (
                str(error)
                if isinstance(error, FixtureError)
                else (
                    "interrupted"
                    if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else "operation-failed"
                )
            )
            print(
                "global-vault fixture: FAILED phase=" + phase + " category=" + category,
                file=output,
            )
        if root is not None:
            print("Retained root: " + str(root), file=output)
            if (
                args.diagnose is None
                and args.cleanup is None
                and args.retry_source is None
                and (not isinstance(error, FirstLaunchFailure) or error.report_written)
            ):
                try:
                    first_report = safe_path(
                        root, "first-launch-failure.json", optional=True
                    )
                    if first_report.exists():
                        print("Report: " + str(first_report), file=output)
                except Exception:
                    pass
            try:
                ids = private_json(root, "fixture-ids.json")
                if isinstance(ids, dict):
                    for name, ident in sorted(ids.items()):
                        if (
                            name in PROJECTS
                            and isinstance(ident, str)
                            and re.fullmatch(r"[0-9a-f]{32}", ident)
                        ):
                            print("Workspace " + name + ": " + ident, file=output)
            except Exception:
                pass
        return 1
    finally:
        os.umask(old_umask)
        output.close()


if __name__ == "__main__":
    sys.exit(main())
