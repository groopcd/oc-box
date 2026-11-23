"""Provider-free native 2.3.0 UI preference persistence, not host acceptance.

TMPDIR=/tmp/opencode python3 -B tests/smoke_native_preferences_v2.py
--opencode /path/to/opencode-v2

Only fresh private synthetic HOME/XDG directories and stdio-owned servers are
used. Real Ctrl+P / Open settings / arrow-key actions own every preference
change after the initial prompt/notifications-off host seed. No launcher
autoaccept flags, inline CLI overrides, providers, Podman or PTY recordings.
The only refresh implementation under test is production sync_config().
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_session_sync_v2 import bounded, private_environment, require

from oc_box import opencode_compat, opencode_v2_io as bridge, sync_config


class Client:
    """Owned bounded PTY; only fixed-label flags and 128 transient ASCII bytes."""

    def __init__(
        self, binary, env, project, endpoint, preferences, *, image_preview=False
    ):
        require(
            "OPENCODE_CLI_CONFIG_CONTENT" not in env,
            "Native preference fixture refuses an inline CLI override.",
        )
        if image_preview:
            env = {
                **env,
                "OPENCODE_CLI_CONFIG_CONTENT": json.dumps(
                    {
                        "prompt": {"image_preview": True},
                        "session": {"image_preview": True},
                    }
                ),
            }
        self.preferences = preferences
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        terminal = (
            "import fcntl,termios,os,sys;"
            "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
            "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
        )
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    terminal,
                    binary,
                    str(project),
                    "--server",
                    endpoint["url"],
                ],
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
        self.state = "text"
        self.watch("Ask anything", "Connect a provider")

    def watch(self, *labels):
        self.labels = dict.fromkeys(labels, False)
        self.tail = ""

    def feed(self, data):
        # Ignore terminal control strings and ANSI formatting without retaining
        # output. Split sequences are carried only as a finite parser state.
        for value in data:
            if self.state == "escape":
                self.state = (
                    "csi"
                    if value == ord("[")
                    else "string"
                    if value in b"]P_^"
                    else "text"
                )
            elif self.state == "csi":
                if 0x40 <= value <= 0x7E:
                    self.state = "text"
            elif self.state == "string":
                if value == 7:
                    self.state = "text"
                elif value == 27:
                    self.state = "string-escape"
            elif self.state == "string-escape":
                self.state = "text" if value == ord("\\") else "string"
            elif value == 27:
                self.state = "escape"
            elif 0x20 <= value < 0x7F or value in (9, 10, 13):
                self.tail = (self.tail + (chr(value) if value >= 0x20 else " "))[-128:]
                for label in self.labels:
                    self.labels[label] |= label in self.tail

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
            self.feed(data)

    def send(self, value, settle=0.15):
        os.write(self.master, value)
        self.drain(settle)

    def wait(self, condition, label, timeout=15):
        deadline = time.monotonic() + timeout
        while not condition():
            require(
                self.process.poll() is None and time.monotonic() < deadline,
                "Native preferences fixture timed out: " + label + ".",
            )
            self.drain(0.05)

    def ready(self):
        self.wait(lambda: any(self.labels.values()), "native home")
        self.drain(0.5)

    def setting(self, label):
        self.watch("Search commands", "Commands")
        self.send(b"\x10", 0.3)  # Official Ctrl+P command palette.
        self.wait(lambda: any(self.labels.values()), "command palette")
        self.watch("Open settings")
        self.send(b"Open settings", 0.5)
        self.wait(lambda: any(self.labels.values()), "Open settings command")
        self.watch("Settings", "Search settings", "Appearance", "Theme")
        self.send(b"\r", 0.6)
        self.wait(lambda: any(self.labels.values()), "settings dialog")
        self.watch(label)
        self.send(label.encode())
        self.wait(lambda: self.labels[label], "filtered setting")
        self.drain(0.3)

    def change(self, label, path, expected):
        self.setting(label)
        self.send(b"\x1b[C")  # Native Next value binding, not a file write.

        def changed():
            try:
                value = json.loads(self.preferences.read_bytes())
            except (OSError, ValueError):
                return False
            for name in path:
                if not isinstance(value, dict) or name not in value:
                    return False
                value = value[name]
            return value == expected

        self.wait(changed, label + " native file write", timeout=5)
        self.send(b"\x1b", 0.25)

    def close(self):
        result = None
        try:
            if self.process.poll() is None:
                self.send(b"\x1b")
                self.send(b"\x03")
                deadline = time.monotonic() + 8
                while self.process.poll() is None and time.monotonic() < deadline:
                    self.drain(0.1)
            result = self.process.poll()
        finally:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            os.close(self.master)
            self.tail = ""
        require(result == 0, "Native preferences TUI did not exit normally.")


def host_archive(config):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in ("cli.json", "opencode.json"):
            content = (config / name).read_bytes()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def selected(preferences):
    value = json.loads(preferences.read_bytes())
    return (
        value.get("session", {}).get("permissions"),
        value.get("attention", {}).get("notifications"),
    )


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    env = private_environment(root / "native")
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
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(release == "2.3.0", "This native preferences fixture requires 2.3.0.")
    digest = hashlib.sha256()
    with open(binary, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    print("Native identity: OpenCode " + release + ", sha256=" + digest.hexdigest())

    host = root / "host-config/opencode"
    host.mkdir(parents=True)
    defaults = {
        "$schema": "https://opencode.ai/v2/cli.json",
        "session": {"permissions": "prompt"},
        "attention": {"notifications": False, "sound": False},
        "theme": {"name": "opencode"},
        "animations": False,
        "highlights": False,
    }
    (host / "cli.json").write_text(json.dumps(defaults, indent=2) + "\n")
    (host / "opencode.json").write_text('{"update":"disable"}\n')
    original_host = (host / "cli.json").read_bytes()
    sync_config.sync_config(home, host_archive(host), release)
    preferences = home / ".config/opencode/cli.json"
    require(selected(preferences) == ("prompt", False), "Initial seed mismatch.")

    previous = Path.cwd()
    os.chdir(project)
    try:
        with patch.dict(os.environ, env, clear=True):
            first_exit = {}
            with bridge.server(binary, exit_info=first_exit) as native:
                require(
                    native("GET", "/api/credential")["data"] == [],
                    "Synthetic native preferences server inherited credentials.",
                )
                require(
                    bridge.session_inventory(native, str(project)) == [],
                    "Synthetic native preferences fixture was not empty.",
                )
                client = Client(binary, env, project, native.endpoint, preferences)
                try:
                    client.ready()
                    client.change(
                        "Permissions", ("session", "permissions"), "autoaccept"
                    )
                    client.change("Notifications", ("attention", "notifications"), True)
                finally:
                    client.close()
                require(
                    bridge.session_inventory(native, str(project)) == [],
                    "Settings interaction unexpectedly created a conversation.",
                )
            require(
                first_exit == {"code": 0, "forced": False},
                "Initial private native server did not stop cleanly.",
            )
            require(
                selected(preferences) == ("autoaccept", True),
                "Real native settings changes were not persisted.",
            )
            require(
                (host / "cli.json").read_bytes() == original_host,
                "Native UI changed the synthetic host preferences.",
            )
            native_preferences = preferences.read_bytes()
            print(
                "PASS: real Ctrl+P Open settings changes prompt -> autoaccept "
                "and notifications off -> on; native quit persists both.",
                flush=True,
            )

            defaults["theme"] = {"name": "tokyonight"}
            (host / "cli.json").write_text(json.dumps(defaults, indent=2) + "\n")
            (host / "opencode.json").write_text(
                '{"update":"disable","formatter":false}\n'
            )
            changed_host = (host / "cli.json").read_bytes()
            require(changed_host != original_host, "Host defaults did not change.")
            sync_config.sync_config(home, host_archive(host), release)
            require(
                preferences.read_bytes() == native_preferences,
                "Production refresh replaced native CLI settings.",
            )
            require(
                json.loads((preferences.parent / "opencode.json").read_bytes())
                == {"update": "disable", "formatter": False},
                "Production refresh skipped the unrelated server config.",
            )

            second_exit = {}
            with bridge.server(binary, exit_info=second_exit) as native:
                client = Client(binary, env, project, native.endpoint, preferences)
                try:
                    client.ready()
                    require(
                        preferences.read_bytes() == native_preferences
                        and selected(preferences) == ("autoaccept", True),
                        "Restart changed native CLI settings.",
                    )
                    # The inverse transitions prove the restarted native UI
                    # loaded the retained values, not just unchanged disk bytes.
                    client.change("Permissions", ("session", "permissions"), "prompt")
                    client.change(
                        "Permissions", ("session", "permissions"), "autoaccept"
                    )
                    client.change(
                        "Notifications", ("attention", "notifications"), False
                    )
                    client.change("Notifications", ("attention", "notifications"), True)
                finally:
                    client.close()
                require(
                    bridge.session_inventory(native, str(project)) == []
                    and native("GET", "/api/credential")["data"] == [],
                    "Provider-free settings fixture changed sessions/credentials.",
                )
            require(
                second_exit == {"code": 0, "forced": False},
                "Restarted private native server did not stop cleanly.",
            )
            require(
                selected(preferences) == ("autoaccept", True)
                and (host / "cli.json").read_bytes() == changed_host,
                "Native restart did not retain private preferences/host separation.",
            )
            print(
                "PASS: production refresh applies changed server config but "
                "retains exact native cli.json; restarted real UI loads both "
                "preferences, confirmed by inverse/native cycles; host unchanged.",
                flush=True,
            )
    finally:
        os.chdir(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="oc-box-native-preferences-v2-"
    ) as directory:
        exercise(str(arguments.opencode.resolve()), Path(directory))
    print(
        "Native synthetic PTY/config integration only: NOT interactive host/"
        "Podman acceptance, permission enforcement or desktop notification delivery.",
        flush=True,
    )


if __name__ == "__main__":
    main()
