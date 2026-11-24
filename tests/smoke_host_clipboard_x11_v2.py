"""Real desktop selection + Ctrl+Y + a native V2 composer on private Xvfb.

Requires Xvfb, xclip, xdotool, Kitty or Konsole, and Pillow with XCB support.
The display, input gesture, selection, oc-box helper, and native client are
real. A synthetic loopback provider sees media only after a separate Enter.
The optional Wayland selection backend uses private headless Weston 16 and
wl-clipboard. The terminal and its input still run on the private X11 display.
No live workspace, desktop clipboard, Podman, or screenshots are retained.
"""

import argparse
from contextlib import chdir, contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from smoke_image_preview_x11_v2 import image_bytes, marker_pixels, private_display, reap
from smoke_host_drop_v2 import MARKER, canned_provider
from smoke_session_sync_v2 import bounded, private_environment, require, seed
from oc_box import host_clipboard, host_drop, oc_box as box, opencode_compat
from oc_box import opencode_v2_io as bridge
from oc_box import terminal_ui

TEST = Path(__file__).resolve()
TERMINALS = {
    "kitty": [
        "/usr/bin/kitty",
        "--config",
        "NONE",
        "-o",
        "linux_display_server=x11",
        "-o",
        "confirm_os_window_close=0",
        "--",
    ],
    "konsole": [
        "/usr/bin/konsole",
        "--separate",
        "--nofork",
        "--hide-menubar",
        "--hide-tabbar",
        "-e",
    ],
}


def relay(arguments):
    observed, binary, project, url, session, release, mode = arguments
    observed = Path(observed)
    original_helper = (box.PACKAGE / "host_drop_io.py").read_text()

    def local_exec(_box, _data, connection, command):
        require(
            connection == {"url": url}
            and command[:5] == ["python3", "-I", "-S", "-B", "-c"]
            and len(command) == 7
            and command[5] == original_helper,
            "Private clipboard helper boundary changed.",
        )
        return box.ContainerCommand(
            [sys.executable, *command[1:]], {"PATH": os.defpath}
        )

    class Observed(host_drop.Relay):
        def stage(self, images):
            output = super().stage(images)
            (observed / "stage").touch()
            return output

        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                (observed / "result").write_text(
                    json.dumps(
                        {
                            "refused": self.clipboard_refused,
                            "cleanup": self.transfer.cleanup_verified,
                        }
                    )
                )

    # The real native client never needs the X11 clipboard environment.
    native_keys = {
        "TERM",
        "COLORTERM",
        "HOME",
        "PATH",
        "LANG",
        "OPENCODE_PASSWORD",
        "OPENCODE_CLI_CONFIG_CONTENT",
        "OPENCODE_CONFIG_CONTENT",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    }
    env = {key: value for key, value in os.environ.items() if key in native_keys}
    client = box.ContainerCommand(
        [binary, project, "--server", url, "--session", session], env
    )
    write_all = terminal_ui.write_all
    markers = {"ready": ("SYNTHETIC_" + session).encode()}
    if mode == "strict":
        markers["refusal"] = b"source-guard"
    tail_limit = max(map(len, markers.values())) - 1
    tail = b""

    def observe_output(fd, data):
        nonlocal tail
        write_all(fd, data)
        if fd == 1 and markers:
            window = tail + data
            for name in tuple(markers):
                if markers[name] in window:
                    (observed / name).touch()
                    del markers[name]
            tail = window[-tail_limit:] if markers else b""

    with (
        patch.object(host_drop, "Relay", Observed),
        patch.object(box.workspace_service, "client_command", local_exec),
        patch.object(terminal_ui, "write_all", observe_output),
    ):
        return box.run_native_tui(
            {"opencode_version": release},
            client,
            {"url": url},
            host_drop_enabled=False,
            host_clipboard_reader=host_clipboard.preflight(),
            host_clipboard_symlinks=mode != "strict",
        )


@contextmanager
def publish(env, mime, content):
    if "WAYLAND_DISPLAY" in env:
        command = ["/usr/bin/wl-copy", "--foreground", "--type", mime]
        types = ["/usr/bin/wl-paste", "--list-types"]
        read = ["/usr/bin/wl-paste", "--no-newline", "--type", mime]
    else:
        base = ["/usr/bin/xclip", "-selection", "clipboard"]
        command = [*base, "-target", mime, "-in", "-quiet"]
        types = [*base, "-target", "TARGETS", "-out"]
        read = [*base, "-target", mime, "-out"]
    process = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.stdin.write(content)
        process.stdin.close()
        yield types, read
    finally:
        reap(process)


@contextmanager
def private_wayland(runtime):
    env = {"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "fixture-wayland"}
    compositor = subprocess.Popen(
        [
            "/usr/bin/weston",
            "--no-config",
            "--backend=headless",
            "--fake-seat",
            "--renderer=pixman",
            "--shell=kiosk",
            "--idle-time=0",
            "--socket=fixture-wayland",
        ],
        env={**env, "PATH": os.defpath, "HOME": str(runtime)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait(
            (runtime / "fixture-wayland").is_socket,
            "Private Weston did not become ready.",
        )
        yield env
    finally:
        reap(compositor)


def wait(check, message, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.1)
    require(False, message)


def run_case(
    binary,
    terminal,
    version,
    root,
    env,
    project,
    native,
    provider,
    mode,
    mime,
    content,
    image_hash,
    *,
    verify_prompt_pixels,
):
    root.mkdir()
    session = seed(native, "ses_clip_" + mode, project)
    export_route = f"/api/experimental/session/{session}/export"
    before = native("GET", export_route)
    request_count = len(provider["requests"])
    env = {**env, "OPENCODE_PASSWORD": native.endpoint["password"]}
    image_on_screen = None
    if verify_prompt_pixels:
        screen = {key: env[key] for key in ("DISPLAY", "XAUTHORITY")}
        env["OPENCODE_CLI_CONFIG_CONTENT"] = json.dumps(
            {"prompt": {"image_preview": True}, "session": {"image_preview": True}}
        )

        def image_on_screen():
            return all(count > 40 for count in marker_pixels(screen))

    process = subprocess.Popen(
        [
            *TERMINALS[terminal],
            sys.executable,
            "-B",
            str(TEST),
            "--relay",
            str(root),
            binary,
            str(project),
            native.endpoint["url"],
            session,
            version,
            mode,
        ],
        env=env,
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def xdo(*args, check=True):
        return subprocess.run(
            ["/usr/bin/xdotool", *args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=check,
        ).stdout

    def visible():
        require(process.poll() is None, "The private terminal stopped before the test.")
        values = xdo(
            "search", "--onlyvisible", "--class", terminal, check=False
        ).splitlines()
        return values[0].decode() if len(values) == 1 else None

    def offered(command):
        value = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        return value.returncode == 0 and mime.encode() in value.stdout.split()

    try:
        window = wait(visible, "The private terminal never became visible.")
        xdo("windowfocus", "--sync", window)
        with publish(env, mime, content) as (types, read):
            wait(
                lambda: offered(types),
                "The private desktop selection was not published.",
            )
            # The shell maps before the inner TUI handles bracketed paste.
            # Observe the synthetic transcript marker without recording output.
            wait(
                (root / "ready").is_file,
                "The native session did not become ready in the terminal.",
            )
            require(not (root / "stage").exists(), "Clipboard staged before Ctrl+Y.")
            if image_on_screen:
                require(not image_on_screen(), "Image appeared before Ctrl+Y.")
            xdo("key", "--clearmodifiers", "ctrl+y")
            if mode != "strict":
                wait(
                    (root / "stage").is_file, "Ctrl+Y did not stage the selected image."
                )
            else:
                wait(
                    (root / "refusal").is_file,
                    "The native composer did not show the fixed clipboard refusal.",
                )
            # The transport acknowledgement precedes composer processing.
            # Keep the draft unsent for a separate, bounded observation period.
            time.sleep(1.0)
            if image_on_screen:
                if mode == "strict":
                    require(
                        not image_on_screen(),
                        "Strict refusal still displayed the image.",
                    )
                else:
                    wait(
                        image_on_screen,
                        "The native composer did not render the staged image.",
                    )
            require(
                native("GET", f"/api/session/{session}/inbox")["data"] == []
                and native("GET", export_route) == before
                and len(provider["requests"]) == request_count,
                "Ctrl+Y submitted native input without a user Send.",
            )
            same = subprocess.run(
                read,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=True,
            )
            require(
                same.stdout == content, "The import modified the desktop selection."
            )
            if mode != "strict":
                xdo("type", "--delay", "10", MARKER)
                xdo("key", "Return")
                wait(
                    lambda: len(provider["requests"]) > request_count,
                    "Manual Send did not reach the provider.",
                )
                require(
                    provider["requests"][request_count:]
                    == [{"marker": True, "media": {"image/png": image_hash}}],
                    "The native draft did not carry the staged clipboard image.",
                )
            else:
                require(
                    not (root / "stage").exists(),
                    "Strict policy staged a symbolic link.",
                )
        xdo("key", "ctrl+c", "Escape", "ctrl+c", "ctrl+d")
        wait((root / "result").is_file, "The clipboard client did not report cleanup.")
        require(
            json.loads((root / "result").read_bytes())
            == {
                "refused": int(mode == "strict"),
                "cleanup": True,
            },
            "The clipboard relay did not report the expected refusal and cleanup.",
        )
    finally:
        reap(process)
    backend = "Wayland" if "WAYLAND_DISPLAY" in env else "X11"
    print(
        f"PASS {terminal} native {version}, {backend} selection: {mode}; "
        "Ctrl+Y, no automatic Send, selection preserved."
    )


def exercise(binary, terminal, root, *, clipboard_backend, verify_prompt_pixels=False):
    project = root / "project"
    project.mkdir()
    env = private_environment(root / "native")
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(opencode_compat.major(release) == 2, "Native OpenCode V2 is required.")
    config = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    config.mkdir(exist_ok=True)
    (config / "cli.json").write_text(
        json.dumps(
            {
                "highlights": False,
                "animations": False,
                "terminal": {"title": True},
                "attention": {"sound": False, "notifications": False},
            }
        )
    )
    selected = root / "fixture.png"
    content = image_bytes()
    selected.write_bytes(content)
    alias = root / "linked.png"
    alias.symlink_to(selected.name)
    uri = (alias.as_uri() + "\r\n").encode()
    image_hash = hashlib.sha256(content).hexdigest()
    runtime = Path(env["HOME"]) / "run"
    runtime.mkdir(mode=0o700)
    clipboard = (
        private_wayland(runtime) if clipboard_backend == "wayland" else nullcontext({})
    )
    with (
        private_display(root) as display,
        clipboard as selection,
        canned_provider() as (port, provider),
    ):
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        config.update(
            {
                "model": "fixture/probe",
                "warming": False,
                "providers": {
                    "fixture": {
                        "package": "@opencode/ai/providers/openai-compatible",
                        "settings": {
                            "baseURL": f"http://127.0.0.1:{port}/v1",
                            "apiKey": "synthetic-only",
                        },
                        "models": {
                            "probe": {
                                "name": "Synthetic desktop clipboard",
                                "limit": {"context": 32000, "output": 2000},
                                "capabilities": {
                                    "tools": False,
                                    "input": ["text", "image"],
                                    "output": ["text"],
                                },
                            }
                        },
                    }
                },
            }
        )
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        gui = {
            **env,
            **display,
            **selection,
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "QT_QPA_PLATFORM": "xcb",
            "XDG_RUNTIME_DIR": str(runtime),
        }
        with (
            chdir(project),
            patch.dict(os.environ, env, clear=True),
            bridge.server(binary) as native,
        ):
            for mode, mime, data in (
                ("png", "image/png", content),
                ("symlink", "text/uri-list", uri),
                ("strict", "text/uri-list", uri),
            ):
                run_case(
                    binary,
                    terminal,
                    release,
                    root / mode,
                    gui,
                    project,
                    native,
                    provider,
                    mode,
                    mime,
                    data,
                    image_hash,
                    verify_prompt_pixels=verify_prompt_pixels,
                )
            require(
                len(provider["requests"]) == 2 and not provider["failures"],
                "Unexpected provider activity.",
            )
            require(
                selected.read_bytes() == content
                and alias.readlink() == Path(selected.name),
                "A copied source changed.",
            )


if __name__ == "__main__":
    if sys.argv[1:2] == ["--relay"]:
        raise SystemExit(relay(sys.argv[2:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    parser.add_argument("--terminal", choices=TERMINALS, default="kitty")
    parser.add_argument(
        "--clipboard-backend", choices=("x11", "wayland"), default="x11"
    )
    parser.add_argument("--verify-prompt-pixels", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oc-box-clipboard-x11-") as directory:
        exercise(
            str(args.opencode.resolve()),
            args.terminal,
            Path(directory),
            clipboard_backend=args.clipboard_backend,
            verify_prompt_pixels=args.verify_prompt_pixels,
        )
