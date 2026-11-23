"""Provider-free native 2.3.0 PTY/tab-state integration, not host acceptance.

python3 -B tests/smoke_native_tabs_v2.py --opencode /path/to/opencode-v2

Only fresh private synthetic HOME/XDG directories and a stdio-owned server are
used. No Podman, shared service, real credentials, transcript logging or native
state-file edits. Native opens/closes/picker actions own every tab-state write.
The helper's production client_command environment bridge is retained; only
its outer Podman exec boundary is replaced with local execution.
"""

import argparse
import contextlib
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_session_sync_v2 import bounded, private_environment, require, seed

from oc_box import native_tabs, opencode_compat
from oc_box import oc_box as box
from oc_box import opencode_v2_io as bridge


class Client:
    """Bounded real PTY; output discarded except a transient close-glyph flag."""

    def __init__(self, binary, env, project, endpoint, selected):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))

        command = [binary, str(project), "--server", endpoint["url"]]
        if selected:
            command.extend(["--session", selected])
        terminal = (
            "import fcntl,termios,os,sys;"
            "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
            "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
        )
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-B", "-c", terminal, *command],
                cwd=project,
                env={**env, "OPENCODE_PASSWORD": endpoint["password"]},
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.close_glyph = False
        self.tail = b""

    def drain(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            if not select.select([self.master], [], [], min(0.1, remaining))[0]:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.close_glyph |= "✕".encode() in self.tail + data
            self.tail = data[-3:]

    def send(self, value, settle=0.2):
        os.write(self.master, value)
        self.drain(settle)

    def wait(self, condition, label, timeout=15):
        deadline = time.monotonic() + timeout
        while not condition():
            require(
                self.process.poll() is None and time.monotonic() < deadline,
                "Native tab fixture timed out: " + label + ".",
            )
            self.drain(0.05)

    def x_close(self, index):
        # Source-pinned native horizontal tab width caps at 32 cells. At 140
        # columns two tabs have that width, and their close control is right=1.
        # SGR motion must precede the click: native displays ✕ only on hover.
        column = (index + 1) * 32 - 1
        self.close_glyph = False
        self.send(f"\x1b[<35;{column};1M".encode(), 0.4)
        require(self.close_glyph, "Native X close control did not render on hover.")
        self.send(f"\x1b[<0;{column};1M".encode(), 0.08)
        self.send(f"\x1b[<0;{column};1m".encode(), 0.3)

    def picker(self, title):
        self.send(b"\x10", 0.3)  # Official Ctrl+P command palette.
        self.send(b"Switch session", 0.4)
        self.send(b"\r", 0.6)
        self.send(title.encode(), 0.5)
        self.send(b"\r", 0.3)

    def close(self):
        try:
            if self.process.poll() is None:
                self.send(b"\x03")
                deadline = time.monotonic() + 8
                while self.process.poll() is None and time.monotonic() < deadline:
                    self.drain(0.1)
            require(self.process.poll() == 0, "Native tab TUI exit failed.")
        finally:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            os.close(self.master)


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    env = private_environment(root / "native")
    # Match the production private-volume layout, rather than host XDG roots.
    home = Path(env["HOME"])
    for name, relative in (
        ("CONFIG", ".config"),
        ("STATE", ".local/state"),
        ("DATA", ".local/share"),
        ("CACHE", ".cache"),
    ):
        env["XDG_" + name + "_HOME"] = str(home / relative)
    env.update(
        {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
    )
    config = home / ".config/opencode"
    config.mkdir(parents=True)
    (config / "cli.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/v2/cli.json",
                "tabs": {"mode": "on", "scope": "cwd", "layout": "horizontal"},
                "animations": False,
                "highlights": False,
            }
        )
    )
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(release == "2.3.0", "This native tab fixture requires OpenCode 2.3.0.")
    data = {
        "id": "a" * 32,
        "project": str(project),
        "home": str(home),
        "container": "synthetic-no-podman",
        "opencode_version": release,
    }
    original_run = box.run

    def local_exec(command, **settings):
        marker = command.index(data["container"])
        require(
            command[:3] == ["podman", "exec", "-i"]
            and command[marker + 1 : marker + 5] == ["python3", "-I", "-S", "-c"],
            "Native tab fixture lost its production client environment bridge.",
        )
        return original_run(
            command[marker + 1 :],
            cwd=project,
            env=command.environment,
            stdin=subprocess.DEVNULL,
            **settings,
        )

    previous = Path.cwd()
    os.chdir(project)
    try:
        with patch.dict(os.environ, env, clear=True), bridge.server(binary) as native:
            connection = {**native.endpoint, "workspace": data["id"]}

            def snapshot():
                with patch.object(box, "run", side_effect=local_exec):
                    return native_tabs.inspect(
                        box, data, connection=connection, timeout=3
                    )

            def inventory():
                return bridge.session_inventory(native, str(project))

            def messages(ident):
                return native("GET", "/api/experimental/session/" + ident + "/export")[
                    "data"
                ]["messages"]

            require(
                snapshot().status == "missing",
                "Fresh synthetic native tab snapshot was not missing.",
            )
            require(
                native("GET", "/api/credential")["data"] == [],
                "Synthetic native tab server inherited credentials.",
            )
            keep = seed(native, "ses_tabs_keep", project, title="SyntheticKeep")
            closed = seed(
                native, "ses_tabs_closed", project, title="SyntheticClosedNativeTab"
            )
            before = {ident: messages(ident) for ident in (keep, closed)}

            @contextlib.contextmanager
            def client(selected, expected):
                process = Client(binary, env, project, native.endpoint, selected)
                try:
                    process.wait(
                        lambda: snapshot().tabs == tuple(expected),
                        "expected membership",
                    )
                    process.drain(1.3)
                    yield process
                finally:
                    process.close()

            with client(keep, [keep]):
                pass
            with client(closed, [keep, closed]) as process:
                process.x_close(1)
                process.wait(lambda: snapshot().tabs == (keep,), "X close")
            require(messages(closed) == before[closed], "X close changed history.")
            selected = native_tabs.choose(
                snapshot(), inventory(), closed, directory=str(project)
            )
            require(selected == keep, "Closed newest root remained a default.")
            with client(selected, [keep]):
                pass
            print(
                "PASS: native 2.3.0 literal X close retains exact history; restart "
                "selects only the remaining open root.",
                flush=True,
            )
            # An additional/default client cannot safely force a stale binding.
            with client(None, [keep]) as process:
                process.drain(0.3)
                process.x_close(0)
                process.wait(lambda: snapshot().tabs == (), "last X close")
            state = snapshot()
            require(
                state.status == "valid"
                and native_tabs.choose(
                    state, inventory(), closed, directory=str(project)
                )
                is None
                and native_tabs.revalidate(
                    state, inventory(), keep, directory=str(project)
                )
                is None,
                "Empty native scope fabricated a default binding.",
            )
            expected_ids = {item["id"] for item in inventory()}
            with client(None, []) as process:
                process.drain(1)
                require(snapshot().tabs == (), "Native home resurrected a tab.")
                process.picker("SyntheticClosedNativeTab")
                process.wait(lambda: snapshot().tabs == (closed,), "picker reopen")
            require(
                {item["id"] for item in inventory()} == expected_ids,
                "Empty home/reopen fabricated a conversation.",
            )
            require(
                {ident: messages(ident) for ident in before} == before,
                "Native close/reopen changed exact retained transcripts.",
            )
            require(
                native_tabs.revalidate(
                    snapshot(), inventory(), keep, directory=str(project)
                )
                == closed,
                "Picker-opened root did not become an eligible default.",
            )
            print(
                "PASS: final literal X stays closed on native-home restart; "
                "Ctrl+P Switch session reopens explicitly, with no created "
                "conversation and exact messages retained.",
                flush=True,
            )
    finally:
        os.chdir(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oc-box-native-tabs-v2-") as directory:
        exercise(str(arguments.opencode.resolve()), Path(directory))
    print(
        "Native synthetic PTY integration only: NOT interactive host/Podman "
        "acceptance or last-active-tab restoration.",
        flush=True,
    )


if __name__ == "__main__":
    main()
