"""Pinned real native cookie issuance, with only a synthetic owner/exec bridge."""

from contextlib import ExitStack, chdir, contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from oc_box import opencode_v2_io, web_native_pair, workspace_service

NATIVE_SHA = "e6543f6e5c39c40971ba38afd1819ac852c29ac735b5a89065d3d148d47f27ad"
_NATIVE_PINS = {
    "2.3.2": NATIVE_SHA,
    "2.4.1": "ec93043f18f8a6b4d8e71ec9260e292ba6f91b66b142782135d40f0b58b0c152",
}


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


class FixtureBridgeError(ValueError):
    pass


class WorkerCommand(list):
    def __init__(self, args, password):
        super().__init__(args)
        self.environment = {"OPENCODE_PASSWORD": password, "LANG": "C.UTF-8"}


class NativeFixture:
    def __init__(self, request, *, version, project, home):
        self.request = request
        self.endpoint = request.endpoint["url"]
        self.version, self.project, self.home = version, project, home
        self.pairs = 0
        self.identity = request("GET", "/api/info", deadline=monotonic() + 4)
        require(
            self.identity.get("version") == self.version
            and request("GET", "/api/credential", deadline=monotonic() + 4)
            == {"data": []},
            "The native fixture was not the pinned empty service.",
        )

    def bind(self, binding, origin):
        data = {"id": binding.workspace_id, "opencode_version": self.version}
        connection = self.request.endpoint | {
            "workspace": binding.workspace_id,
            "version": self.version,
        }
        authority = origin.value.removeprefix("https://")

        def current(candidate):
            return candidate == binding and self.request.alive()

        def command(candidate, actual_data, actual_connection, argv):
            if (
                candidate is not box
                or actual_data is not data
                or actual_connection is not connection
                or not current(binding)
                or argv[:5] != ["python3", "-I", "-S", "-B", "-c"]
                or argv[5]
                != web_native_pair.named_native_worker_source(
                    Path(web_native_pair.__file__).read_text(encoding="utf-8")
                )
                or argv[6:] != [self.endpoint, authority, self.version]
            ):
                raise FixtureBridgeError(
                    "Unexpected synthetic loopback pairing bridge."
                )
            self.pairs += 1
            return WorkerCommand([sys.executable, *argv[1:]], connection["password"])

        box = SimpleNamespace(
            BoxError=FixtureBridgeError,
            workspace_service=SimpleNamespace(
                validate_endpoint=workspace_service.validate_endpoint,
                client_command=command,
            ),
        )

        def pair(candidate, site):
            if not current(candidate) or site != origin:
                raise web_native_pair.NativePairError()
            return web_native_pair.pair_cookie(
                box,
                data,
                connection,
                origin=origin,
                binding=binding,
                owner_current=current,
            )

        return current, pair


@contextmanager
def native_fixture(root, binary, *, version="2.3.2"):
    expected = _NATIVE_PINS[version]
    with binary.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == expected,
            "Pinned native OpenCode executable mismatch.",
        )
    home, project = root / "native-home", root / "native-project"
    home.mkdir(mode=0o700)
    project.mkdir(mode=0o700)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "OPENCODE_DB": "private-fixture.db",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_TEST_HOME": str(home),
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {
                "snapshots": False,
                "formatter": False,
                "update": "disable",
                "plugins": [],
                "policies": [
                    {"action": "plugin.use", "resource": "*", "effect": "deny"}
                ],
            }
        ),
    }
    exit_info = {}
    with ExitStack() as cleanup:
        with patch.dict(os.environ, env, clear=True), chdir(project):
            observed = subprocess.run(
                [binary, "--version"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=8,
            )
            require(
                observed.returncode == 0
                and observed.stdout.strip() == f"opencode v{version}".encode("ascii"),
                "Pinned native version response mismatch.",
            )
            request = cleanup.enter_context(
                opencode_v2_io.server(
                    str(binary),
                    startup_deadline=monotonic() + 16,
                    exit_info=exit_info,
                )
            )
        yield NativeFixture(request, version=version, project=project, home=home)
    require(
        exit_info == {"code": 0, "forced": False},
        "The private native service did not drain.",
    )
