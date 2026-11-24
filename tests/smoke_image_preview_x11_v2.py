"""Verify real transcript pixels in native OpenCode V2 and Kitty on private Xvfb.

Requires Linux, Xvfb, Kitty, software OpenGL, and Pillow with XCB support.
No Podman, real desktop, credentials, inference, or retained screenshots.
Run with --opencode /path/to/unmodified-opencode from the repository root.
"""

import argparse
import base64
from collections import Counter
from contextlib import chdir, contextmanager, ExitStack
import io
import json
import os
from pathlib import Path
import secrets
import select
import struct
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oc_box import opencode_compat, opencode_v2_io as bridge
from smoke_session_sync_v2 import bounded, private_environment, require, seed

COLORS = ((219, 21, 182), (10, 241, 53), (18, 136, 239), (252, 108, 6))


def image_bytes():
    image = Image.new("RGB", (192, 128))
    draw = ImageDraw.Draw(image)
    for (x, y), color in zip(((0, 0), (96, 0), (0, 64), (96, 64)), COLORS):
        draw.rectangle((x, y, x + 95, y + 63), fill=color)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def reap(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


@contextmanager
def private_display(root):
    auth = root / "xauthority"
    # FamilyWild, with one random per-fixture MIT cookie. Let the server
    # allocate a free display atomically; do not connect to any existing X11.
    parts = (b"", b"", b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16))
    auth.write_bytes(
        b"\xff\xff" + b"".join(struct.pack("!H", len(x)) + x for x in parts)
    )
    auth.chmod(0o600)
    read, write = os.pipe()
    with ExitStack() as stack:
        stack.callback(os.close, read)
        try:
            process = subprocess.Popen(
                [
                    "/usr/bin/Xvfb",
                    "-displayfd",
                    str(write),
                    "-screen",
                    "0",
                    "1200x850x24",
                    "-nolisten",
                    "tcp",
                    "-auth",
                    str(auth),
                ],
                env={"PATH": os.defpath},
                pass_fds=(write,),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(write)
        stack.callback(reap, process)
        require(
            select.select([read], [], [], 5)[0], "Private Xvfb did not become ready."
        )
        number = os.read(read, 128).strip()
        require(number.isdigit(), "Private Xvfb did not report a display.")
        yield {"DISPLAY": ":" + number.decode(), "XAUTHORITY": str(auth)}


def marker_pixels(display):
    with patch.dict(os.environ, display, clear=True):
        with ImageGrab.grab(xdisplay=display["DISPLAY"]) as snapshot:
            pixels = getattr(snapshot, "get_flattened_data", snapshot.getdata)
            counts = Counter(pixels())
    return tuple(counts[color] for color in COLORS)


def verify_pixels(binary, environment, display, project, native, session, enabled):
    env = {
        **environment,
        **display,
        "LIBGL_ALWAYS_SOFTWARE": "1",
        "XDG_RUNTIME_DIR": str(Path(environment["HOME"]) / "run"),
        "OPENCODE_PASSWORD": native.endpoint["password"],
    }
    if enabled:
        env["OPENCODE_CLI_CONFIG_CONTENT"] = json.dumps(
            {"prompt": {"image_preview": True}, "session": {"image_preview": True}}
        )
    process = subprocess.Popen(
        [
            "/usr/bin/kitty",
            "--config",
            "NONE",
            "-o",
            "linux_display_server=x11",
            "-o",
            "confirm_os_window_close=0",
            "--",
            binary,
            str(project),
            "--server",
            native.endpoint["url"],
            "--session",
            session,
        ],
        env=env,
        cwd=project,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + (12 if enabled else 4)
        while time.monotonic() < deadline:
            require(process.poll() is None, "The private Kitty window stopped.")
            markers = marker_pixels(display)
            if enabled and all(pixels > 40 for pixels in markers):
                return
            require(
                enabled or not any(markers), "Image pixels appeared with previews off."
            )
            time.sleep(0.15)
        require(
            not enabled, "Native transcript preview did not display the synthetic PNG."
        )
    finally:
        reap(process)


def exercise(binary, root):
    project = root / "project"
    project.mkdir()
    env = private_environment(root / "native")
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(opencode_compat.major(release) == 2, "Native OpenCode V2 is required.")
    (Path(env["HOME"]) / "run").mkdir(mode=0o700)
    config = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    config.mkdir(exist_ok=True)
    preferences = config / "cli.json"
    preferences.write_text(
        json.dumps(
            {
                "highlights": False,
                "animations": False,
                "prompt": {"image_preview": False},
                "session": {"image_preview": False},
                "attention": {"sound": False, "notifications": False},
            }
        )
    )
    before = preferences.read_bytes()
    with (
        private_display(root) as display,
        chdir(project),
        patch.dict(os.environ, env, clear=True),
        bridge.server(binary) as native,
    ):
        donor = seed(native, "ses_inline_seed", project)
        pending = native(
            "POST",
            f"/api/session/{donor}/prompt",
            {
                "text": "FIXTURE_ATTACHMENT",
                "files": [
                    {
                        "uri": "data:image/png;base64,"
                        + base64.b64encode(image_bytes()).decode(),
                        "name": "fixture-inline.png",
                    }
                ],
                "resume": False,
            },
        )["data"]
        doc = native("GET", f"/api/experimental/session/{donor}/export")["data"]
        session = "ses_inline_display"
        doc["info"]["id"] = session
        doc["messages"][0]["id"] = "msg_inline_display"
        doc["messages"][0]["files"] = pending["payload"]["files"]
        native("POST", "/api/experimental/session/import", doc)
        messages = native("GET", f"/api/session/{session}/message?order=asc&limit=10")
        require(
            len(messages["data"][0]["files"]) == 1,
            "The synthetic image is not in the transcript.",
        )
        for enabled in (False, True):
            verify_pixels(binary, env, display, project, native, session, enabled)
        require(
            native("GET", "/api/session/active")["data"] == {},
            "Unexpected native execution.",
        )
        require(
            preferences.read_bytes() == before, "The native CLI override was persisted."
        )
    print(
        f"PASS native {release}: no image pixels with preview off; PNG rendered in Kitty with preview on."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="oc-box-preview-x11-") as directory:
        exercise(str(args.opencode.resolve()), Path(directory))
