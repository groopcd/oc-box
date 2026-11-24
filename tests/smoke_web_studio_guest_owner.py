"""Run the unserved owning guest proof with pinned native 2.4.1, not Podman."""

import argparse
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from smoke_web_named_native import (
    _CONTAINER,
    build_environment,
    digest,
    fixture_inspect,
)
from smoke_session_sync_v2 import require

from oc_box import oc_box as box
from oc_box import web_studio_guest_binding, workspace_control, workspace_service


_NATIVE_SHA = "ec93043f18f8a6b4d8e71ec9260e292ba6f91b66b142782135d40f0b58b0c152"


def exercise(root, binary):
    env = build_environment(root / "native", "2.4.1")
    env["OC_BOX_STATE_HOME"] = str(root / "state")
    env["XDG_RUNTIME_DIR"] = str(root)
    project = root / "project"
    project.mkdir(mode=0o700)

    def exec_worker(data, argv):
        require(
            argv[:2] == ["python3", "-c"]
            and argv[3:] == ["guest-proof-lease", box.CONTAINER_OPENCODE, "2.4.1"]
            and data["container"] == _CONTAINER,
            "The guest proof worker lost its immutable target.",
        )
        return box.ContainerCommand(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                argv[2],
                "guest-proof-lease",
                str(binary),
                "2.4.1",
            ],
            env,
        )

    previous = Path.cwd()
    os.chdir(project)
    try:
        with patch.dict(os.environ, env, clear=True):
            path, data = box.create_workspace(project, "tumbleweed")
            data.update(ready=True, opencode_version="2.4.1")
            box.atomic_json(path, data)
            with (
                box.workspace_lock(path),
                patch.object(box, "podman", side_effect=fixture_inspect(data)),
                patch.object(box, "exec_command", side_effect=exec_worker),
            ):
                stop = workspace_control.capture(path)
                owner = workspace_service.Owner(box, path, data, guest_stop_token=stop)
                with owner:
                    owner.publish(None)
                    identity = owner._guest_proof.current()
                    require(
                        identity is not None
                        and identity.version == "2.4.1"
                        and identity.container_id == _CONTAINER
                        and identity.workspace_id == data["id"]
                        and owner._guest_proof.current() is identity,
                        "The original native child was not verified.",
                    )
                    gateway = workspace_service.Gateway(
                        box, owner._guest_proof.pinned_data(), owner.connection
                    )
                    with gateway._listener_preflight() as listener:
                        binding = web_studio_guest_binding.bind_unserved(
                            gateway, listener, owner._guest_proof
                        )
                        require(
                            binding.current() is binding.native.binding
                            and binding.native.native_pid == identity.native_pid
                            and binding.native.binding.origin
                            == listener.origin(gateway, listening=False),
                            "The proof did not join the same worker and socket.",
                        )
                        # A real V2 stop intent retires this proof even while
                        # the worker and the workspace lock are still held.
                        workspace_control.request_stop(path)
                        require(
                            binding.current() is None
                            and owner._guest_proof.current() is None
                            and owner.child.poll() is None,
                            "Stop intent did not fence the original gateway and owner.",
                        )
                require(
                    owner.child.returncode == 0 and not owner.address.exists(),
                    "Native worker did not observe EOF and cleanly reap its server.",
                )
    finally:
        os.chdir(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    args = parser.parse_args()
    if os.getuid() == 0 or os.environ.get("OC_BOX_CONTAINER") == "1":
        parser.error("Requires a non-root synthetic native test environment.")

    def expired(*_):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, expired)
    try:
        binary = args.opencode.resolve(strict=True)
        require(digest(binary) == _NATIVE_SHA, "Unexpected native artifact.")
        with TemporaryDirectory(prefix="ocb-guest-owner-native-") as directory:
            signal.alarm(45)
            try:
                exercise(Path(directory), binary)
            finally:
                signal.alarm(0)
    except Exception as error:
        print(f"FAIL original guest owner: {type(error).__name__}.", file=sys.stderr)
        return 1
    print(
        "PASS native 2.4.1 original guest owner and unserved gateway: authenticated child PID, exact stop intent, clean EOF and reap."
    )
    print(
        "Synthetic Podman inspect/exec; no guest routes, gateway drain or host acceptance."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
