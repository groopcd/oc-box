#!/usr/bin/env python3
"""Boot a disposable, unmounted copy of the affected container's local OS image.

Run on the actual rootless Podman host, not inside oc-box. The affected
container is only inspected. The probe never starts it, edits its limits or
opens the oc-box registry, project, volume, vault, or OpenCode.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile


_SOURCE = re.compile(r"oc-box-[0-9a-f]{32}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = "io.oc-box.rlimit-probe"
_CONFIG_MARK = "io.oc-box.rlimit-probe-config"
_WORKSPACE = "io.oc-box.workspace"
_CMD = ["-c", "exec /bin/sleep infinity"]
_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "DBUS_SESSION_BUS_ADDRESS",
        "CONTAINERS_STORAGE_CONF",
        "CONTAINERS_CONF",
        "STORAGE_DRIVER",
        "STORAGE_OPTS",
    }
)


class Refusal(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def source_profile(source, name):
    state, config = source["State"], source["Config"]
    error, argv = state.get("Error"), config.get("CreateCommand")
    image = source["Image"].removeprefix("sha256:")
    runtime = source.get("OCIRuntime")
    pids = source["HostConfig"].get("PidsLimit")
    require(
        source["Name"] == name
        and _ID.fullmatch(source["Id"])
        and config["Labels"].get(_WORKSPACE) == name.removeprefix("oc-box-")
        and state["Status"] == "created"
        and state["Running"] is False
        and type(state["Pid"]) is int
        and state["Pid"] == 0
        and state.get("Paused") is False
        and state.get("Restarting") is False
        and type(error) is str
        and "rlimit type 6:" in error
        and "operation not permitted" in error
        and type(argv) is list
        and any(type(arg) is str and arg.startswith("--ulimit=nproc=") for arg in argv)
        and config["Entrypoint"] in (["/bin/sh"], "/bin/sh")
        and config["Cmd"] == _CMD
        and _ID.fullmatch(image)
        and runtime in ("runc", "crun")
        and type(pids) is int
        and 32 <= pids <= 1_000_000,
        "The selected container is not the expected stopped nproc failure.",
    )
    return image, runtime, pids


class Probe:
    def __init__(self, podman, empty, source):
        self.command = [
            podman,
            "--remote=false",
            f"--default-mounts-file={empty / 'mounts'}",
            f"--hooks-dir={empty / 'hooks'}",
            f"--cdi-spec-dir={empty / 'cdi'}",
        ]
        self.env = {key: value for key, value in os.environ.items() if key in _ENV}
        self.env["LC_ALL"] = "C"
        self.override = empty / "containers-override.conf"
        self.env["CONTAINERS_CONF_OVERRIDE"] = str(self.override)
        self.source = source
        self.token = secrets.token_hex(16)
        self.name = "ocbox-rlimit-probe-" + self.token
        self.id = None
        self.source_id = None
        self.attempted = False

    def write_config(self):
        # Clear inherited lists even if an earlier containers.conf opted in to
        # append semantics. CDI devices can inject mounts/env during start,
        # after an ordinary mount inspection has already reported an empty list.
        with self.override.open("x", encoding="utf-8") as file:
            file.write(
                "[containers]\n"
                "devices = [{append = false}]\n"
                "volumes = [{append = false}]\n"
                f'annotations = ["{_CONFIG_MARK}={self.token}", {{append = false}}]\n'
            )

    def run(self, *args):
        return subprocess.run(
            [*self.command, *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=45,
        )

    def inspect(self, selector, *, allow_missing=False):
        result = self.run("container", "inspect", selector)
        if result.returncode:
            if (
                allow_missing
                and self.run("container", "exists", selector).returncode == 1
            ):
                return None
            raise Refusal("Podman container inspection failed.")
        values = json.loads(result.stdout)
        require(
            type(values) is list and len(values) == 1,
            "Unexpected Podman inspection result.",
        )
        return values[0]

    def owned(self, item):
        config, host = item["Config"], item["HostConfig"]
        labels = config["Labels"]
        env = config.get("Env") or []
        require(
            _ID.fullmatch(item["Id"])
            and item["Name"] == self.name
            and item["Id"] != self.source_id
            and (self.id is None or item["Id"] == self.id)
            and labels.get(_LABEL) == self.token
            and labels.get(_WORKSPACE) in (None, "")
            and labels.get("io.oc-box.managed") == "false"
            and (config.get("Annotations") or {}).get(_CONFIG_MARK) == self.token
            and item["Image"].removeprefix("sha256:") == self.image
            and item.get("OCIRuntime") == self.runtime
            and item["Mounts"] == []
            and not item.get("Pod")
            and not item.get("Dependencies")
            and config["User"] == "0:0"
            and type(env) is list
            and all(
                type(value) is str
                and (
                    value in ("container=podman", "HOME=/root")
                    or value.startswith(("PATH=", "HOSTNAME="))
                )
                for value in env
            )
            and config["Entrypoint"] in (["/bin/sh"], "/bin/sh")
            and config["Cmd"] == _CMD
            and config.get("Healthcheck") in (None, {}, {"Test": ["NONE"]})
            and not config.get("StartupHealthCheck")
            and host["NetworkMode"] == "none"
            and host.get("Privileged") is False
            and host.get("PidMode") in ("", "private")
            and host.get("PidsLimit") == self.pids
            and (host.get("RestartPolicy") or {}).get("Name") == "no"
            and not any(
                host.get(key)
                for key in ("Devices", "DeviceRequests", "PortBindings", "VolumesFrom")
            ),
            "Could not verify the probe's disposable, unmounted container.",
        )
        self.id = item["Id"]
        return item["State"]

    @staticmethod
    def stopped(state):
        return (
            state["Status"] in ("created", "configured", "exited", "stopped")
            and state["Running"] is False
            and state["Pid"] == 0
            and state.get("Paused") is False
            and state.get("Restarting") is False
        )

    def boot(self):
        info = self.run("info", "--format=json")
        require(info.returncode == 0, "Cannot verify the local Podman engine.")
        host = json.loads(info.stdout)["host"]
        require(
            host["serviceIsRemote"] is False and host["security"]["rootless"] is True,
            "The probe requires local rootless Podman.",
        )
        source = self.inspect(self.source)
        self.image, self.runtime, self.pids = source_profile(source, self.source)
        self.source_id = source["Id"]
        require(
            self.run("container", "exists", self.name).returncode == 1,
            "The fresh probe name could not be verified absent.",
        )
        self.attempted = True
        result = self.run(
            f"--runtime={self.runtime}",
            "create",
            "--pull=never",
            "--name",
            self.name,
            "--network=none",
            "--pid=private",
            "--ipc=private",
            "--restart=no",
            "--no-healthcheck",
            "--image-volume=ignore",
            "--unsetenv-all",
            "--env-host=false",
            "--http-proxy=false",
            "--init",
            "--user=0:0",
            f"--pids-limit={self.pids}",
            f"--label={_LABEL}={self.token}",
            f"--label={_WORKSPACE}=",
            "--label=io.oc-box.managed=false",
            "--entrypoint=/bin/sh",
            self.image,
            *_CMD,
        )
        require(
            result.returncode == 0
            and _ID.fullmatch(result.stdout.strip())
            and result.stdout.strip() != self.source_id,
            "Podman could not create the disposable probe.",
        )
        self.id = result.stdout.strip()
        require(
            self.stopped(self.owned(self.inspect(self.id))),
            "The disposable probe was not created stopped.",
        )
        started = self.run("start", self.id)
        if started.returncode:
            # Only the disposable container's diagnostic is shown locally.
            # Never display the original container's inspection or environment.
            detail = " ".join(
                line.strip() for line in started.stderr.splitlines() if line.strip()
            )
            detail = "".join(char for char in detail if char.isprintable())[:1000]
            raise Refusal(
                f"Disposable start failed (exit {started.returncode}): {detail or 'no diagnostic'}"
            )
        state = self.owned(self.inspect(self.id))
        require(
            state["Status"] == "running"
            and state["Running"] is True
            and type(state["Pid"]) is int
            and state["Pid"] > 0,
            "Podman did not verify the disposable container running.",
        )

    def cleanup(self):
        if not self.attempted:
            return True
        item = self.inspect(self.id or self.name, allow_missing=True)
        if item is None:
            return self.run("container", "exists", self.name).returncode == 1
        if not self.stopped(self.owned(item)):
            self.run("stop", "--time", "3", self.id)
            require(
                self.stopped(self.owned(self.inspect(self.id))),
                "Probe stop not verified.",
            )
        self.run("container", "rm", self.id)
        return all(
            self.run("container", "exists", selector).returncode == 1
            for selector in (self.id, self.name)
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--container", required=True, help="canonical oc-box container name to inspect"
    )
    args = parser.parse_args(argv)
    if not _SOURCE.fullmatch(args.container):
        parser.error("--container must be an oc-box workspace container name.")
    if (
        os.getuid() == 0
        or os.getuid() != os.geteuid()
        or os.environ.get("OC_BOX_CONTAINER") == "1"
        or any(Path(path).exists() for path in ("/.dockerenv", "/run/.containerenv"))
    ):
        parser.error(
            "Run as your regular user outside oc-box, on the rootless Podman host."
        )
    podman = shutil.which("podman")
    if not podman:
        parser.error("Podman was not found on PATH.")
    if os.environ.get("CONTAINERS_CONF_OVERRIDE"):
        parser.error(
            "An existing containers.conf override is in use; the probe will not replace it."
        )
    with tempfile.TemporaryDirectory(prefix="ocbox-rlimit-probe-") as directory:
        root = Path(directory)
        (root / "mounts").touch(mode=0o600)
        (root / "hooks").mkdir(mode=0o700)
        (root / "cdi").mkdir(mode=0o700)
        probe = Probe(podman, root, args.container)
        code = 1

        def interrupted(*_):
            raise KeyboardInterrupt

        prior = signal.signal(signal.SIGTERM, interrupted)
        try:
            probe.write_config()
            probe.boot()
            code = 0
        except Refusal as exc:
            print(exc, file=sys.stderr)
        except (
            ValueError,
            KeyError,
            TypeError,
            OSError,
            subprocess.SubprocessError,
            KeyboardInterrupt,
        ):
            print(
                "Probe interrupted or could not verify Podman metadata.",
                file=sys.stderr,
            )
        finally:
            try:
                cleaned = probe.cleanup()
            except (
                ValueError,
                KeyError,
                TypeError,
                OSError,
                subprocess.SubprocessError,
                KeyboardInterrupt,
            ):
                cleaned = False
            signal.signal(signal.SIGTERM, prior)
        if not cleaned:
            print(
                f"Probe cleanup could not be verified; inspect only {probe.name}.",
                file=sys.stderr,
            )
            return 1
    if code == 0:
        print(
            "PASS: the same OS image booted and stopped without the explicit nproc limit."
        )
        print(
            "The fenced original was only inspected; real mounts and native OpenCode were not tested."
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
