"""Native Ctrl-click wiring with a stub or private synthetic mounts.

Run with pyte installed and --opencode /path/to/an/unmodified/stable-v2/cli.
No provider, normal config, host desktop, real workspace or Podman is used.
The optional --private-mounts mode re-execs under unshare and also exercises
the real reader against ordinary synthetic Linux bind mounts.
"""

import argparse
import binascii
import codecs
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import zlib
from unittest.mock import patch

import pyte

SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

from oc_box import file_preview, opencode_compat, opencode_v2_io, ui_state
from smoke_session_sync_v2 import bounded, private_environment, seed


@contextmanager
def project_cwd(project):
    previous = Path.cwd()
    os.chdir(project)
    try:
        yield
    finally:
        os.chdir(previous)


def png_for_test():
    def chunk(kind, body):
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", binascii.crc32(kind + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\x00\xff\x00" * 2))
        + chunk(b"IEND", b"")
    )


def exercise(binary, root, *, private_mounts=False, hook_state="ready"):
    project = root / "project"
    project.mkdir()
    native = root / "native"
    native.mkdir()
    env = private_environment(native)
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    version = opencode_compat.version(bounded([binary, "--version"], env, project))
    assert file_preview.supports(version), version
    config = Path(env["XDG_CONFIG_HOME"]) / "opencode"
    config.mkdir(exist_ok=True)
    # Use the same packaged TUI entry as production, but no server-side
    # reporter. Only the reader boundary is replaced: a private temp project
    # is not actually a Podman-verified bind or named volume here.
    plugin = root / "plugin"
    plugin.mkdir()
    for name, content in ui_state.package_sources(SOURCE / "oc_box").items():
        (plugin / name).write_bytes(content)
    if hook_state == "incompatible":
        (plugin / "file_preview_tui.tsx").write_text(
            'export function install() { throw new Error("synthetic incompatible hook") }\n'
        )
    elif hook_state == "dragging":
        entry = plugin / "file_preview_tui.tsx"
        script = entry.read_text()
        guard = "if (event.isDragging)"
        assert script.count(guard) == 1
        # Exercise the stale-drag refusal with a real native assistant hit,
        # without modifying the renderer or softening the host gesture.
        entry.write_text(
            script.replace(guard, 'if (event.isDragging || event.type === "up")')
        )
    manifest = plugin / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "oc-box-ui-state",
                "type": "module",
                "exports": {"./tui": "./ui_state_tui.js"},
            }
        )
    )
    (config / "cli.json").write_text(
        json.dumps(
            {
                "plugins": [] if hook_state == "missing" else [str(plugin)],
                "highlights": False,
                "animations": False,
            }
        )
    )
    roots = [project, Path(env["HOME"])]
    identities = [["1", "2"], ["3", "4"]]
    if private_mounts:
        for index, dest in enumerate(roots):
            source = root / f"mount-source-{index}"
            source.mkdir()
            subprocess.run(
                ["mount", "--bind", str(source), str(dest)], check=True, timeout=5
            )
        identities = [file_preview._mount_identity(str(dest)) for dest in roots]
    env[file_preview.ROOTS] = json.dumps(
        {
            "roots": list(map(str, roots)),
            "identities": identities,
        }
    )
    opener = root / "xdg-open"
    opener.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >" + str(root / "opened") + "\n"
    )
    opener.chmod(0o700)
    env["PATH"] = str(root) + os.pathsep + env["PATH"]
    if not private_mounts:
        (plugin / "file_preview_io.py").write_text(
            """
from pathlib import Path
import json
import sys
import time
request = json.load(sys.stdin)
root = Path(__file__).parent.parent
expected_roots = [str(root / "project"), str(root / "native/home")]
assert set(request) == {"path", "roots", "identities"} and request["roots"] == expected_roots
assert request["identities"] == [["1", "2"], ["3", "4"]]
with (root / "reads").open("a") as log:
    log.write(Path(request["path"]).name + "\\n")
name = Path(request["path"]).name
if name == "binary.txt":
    sys.stdout.buffer.write(b"REFUSED-MUST-NOT-SHOW\\x01")
    sys.exit(1)
if name == "slow.txt":
    sys.stdout.buffer.write(b"TIMED-OUT-MUST-NOT-SHOW")
    sys.stdout.buffer.flush()
    time.sleep(20)
    sys.exit(1)
if name == "overflow.txt":
    sys.stdout.buffer.write(b"OVERFLOW-MUST-NOT-SHOW" + b"A" * (256 * 1024))
    sys.exit(0)
path = Path(request["path"])
if path.parent != root / "project":
    (root / "wrong-directory").write_text(str(path.parent))
assert path.parent == root / "project", path
sys.stdout.buffer.write(path.read_bytes())
"""
        )
    file = project / "proof.md"
    second = project / "other.md"
    file.write_text("PREVIEW_FIRST_SNAPSHOT\n# This stays literal\n")
    second.write_text("PREVIEW_OTHER_SNAPSHOT\n")
    if private_mounts:
        (project / "sample.png").write_bytes(png_for_test())
        (project / "article.html").write_text(
            "<article><h1>Draft</h1><script>ignored()</script></article>"
        )
        (project / "binary.txt").write_bytes(b"REFUSED-MUST-NOT-SHOW\x1b")
        (project / "overflow.txt").write_bytes(
            b"OVERFLOW-MUST-NOT-SHOW" + b"A" * (256 * 1024)
        )
        (project / "slow.txt").symlink_to(file)
        (project / "outside.txt").touch()
        foreign = root / "unexpected-mount-source"
        foreign.write_text("UNEXPECTED-MOUNT-MUST-NOT-SHOW\n")
        subprocess.run(
            ["mount", "--bind", str(foreign), str(project / "outside.txt")],
            check=True,
            timeout=5,
        )
        (roots[1] / "home.txt").write_text("PREVIEW_HOME_SNAPSHOT\n")
        # This file is on the ordinary namespace root, outside both mounts.
        (root / "overlay.txt").write_text("PREVIEW_ORDINARY_ROOT_SNAPSHOT\n")
    mount_links = (
        f"\n\n[HOME_LINK]({roots[1] / 'home.txt'})\n\n[OVERLAY_LINK]({root / 'overlay.txt'})"
        if private_mounts
        else ""
    )
    text = "\n\n".join(
        (
            f"[ABS_LINK]({file})",
            f"[URI_LINK]({file.as_uri()})",
            "[REL_LINK](other.md)",
            "[DOT_LINK](./other.md)",
            "[REFUSED_LINK](binary.txt)",
            "[SLOW_LINK](slow.txt)",
            "[OVERFLOW_LINK](overflow.txt)",
            "[OUTSIDE_LINK](outside.txt)",
            "[TRAVERSAL_LINK](../other.md)",
            "[REMOTE_FILE](file://remote.example/other.md)",
            "[WEB_LINK](https://example.invalid/preview-test)",
            f"BARE_FILE {file}",
            "END_PROBE",
        )
    )
    with (
        patch.dict(os.environ, env, clear=True),
        project_cwd(project),
        opencode_v2_io.server(binary) as request,
    ):
        sid = seed(request, "ses_preview_seed", project, title="File preview smoke")
        doc = request("GET", "/api/experimental/session/" + sid + "/export")["data"]
        sid = "ses_preview_content"
        doc["info"]["id"] = sid
        doc["messages"][0]["id"] = "msg_preview_user"
        doc["messages"].append(
            {
                "id": "msg_preview_assistant",
                "type": "assistant",
                "agent": "build",
                "model": {"providerID": "fixture", "id": "fixture"},
                "time": {"created": 2, "completed": 3},
                "content": [
                    {"type": "text", "text": text},
                    {"type": "text", "text": "[PART_LINK](other.md)" + mount_links},
                    *(
                        [
                            {
                                "type": "text",
                                "text": "[PNG_LINK](sample.png)\n\n[HTML_LINK](article.html)",
                            }
                        ]
                        if private_mounts
                        else []
                    ),
                ],
            }
        )
        request("POST", "/api/experimental/session/import", doc)
        info = request("GET", "/api/session/" + sid)["data"]
        assert info["location"]["directory"] == str(project), (
            "Fixture imported the wrong native location"
        )
        run_client(
            binary,
            root,
            project,
            env,
            request.endpoint,
            sid,
            file,
            private_mounts=private_mounts,
            hook_state=hook_state,
        )
    if hook_state == "dragging":
        print(
            f"PASS: OpenCode {version} native drag refusal warned without transferring."
        )
        return
    if hook_state != "ready":
        print(f"PASS: OpenCode {version} native {hook_state} hook left opening off.")
        return
    scope = (
        "real reader and disposable mounts" if private_mounts else "synthetic reader"
    )
    print(
        f"PASS: OpenCode {version} native Ctrl-click artifact handoff ({scope}; no desktop launcher)."
    )


def run_client(
    binary,
    root,
    project,
    env,
    endpoint,
    sid,
    file,
    *,
    private_mounts=False,
    hook_state="ready",
):
    master, slave = pty.openpty()
    rows, columns = 52, 150
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    bootstrap = (
        "import fcntl,termios,os,sys;"
        "fcntl.ioctl(0,termios.TIOCSCTTY,0);"
        "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)"
    )
    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                bootstrap,
                binary,
                str(project),
                "--server",
                endpoint["url"],
                "--session",
                sid,
            ],
            cwd=project,
            env={**env, "OPENCODE_PASSWORD": endpoint["password"]},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
    finally:
        os.close(slave)
    screen = pyte.Screen(columns, rows)
    stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    reads = root / "reads"
    opened = root / "opened"
    wire = bytearray()
    from oc_box import file_preview_host, file_preview_png
    from oc_box.host_drop_input import PasteInput

    preview_wire = file_preview_host.Relay()
    input_wire = PasteInput(
        lambda _: None,
        on_host_mouse=preview_wire.mouse,
        on_mouse_boundary=preview_wire.break_press,
    )
    candidates = []

    def log():
        return reads.read_text().splitlines() if reads.exists() else []

    def pump(duration):
        end = time.monotonic() + duration
        while child.poll() is None and (remaining := end - time.monotonic()) > 0:
            if select.select([master], [], [], min(0.1, remaining))[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                wire.extend(data)
                assert len(wire) < 4 * 1024 * 1024
                data = preview_wire.output_bytes(data)
                candidate = preview_wire.take()
                if candidate is not None:
                    candidates.append(candidate)
                stream.feed(decoder.decode(data))

    def locate(label):
        for y, row in enumerate(screen.display, 1):
            if label in row:
                return row.index(label) + 3, y

    def wait(label, seconds=9):
        end = time.monotonic() + seconds
        while not locate(label) and child.poll() is None and time.monotonic() < end:
            pump(0.1)
        assert locate(label), (
            label,
            log(),
            (root / "wrong-directory").read_text()
            if (root / "wrong-directory").exists()
            else None,
            [line.strip() for line in screen.display if line.strip()],
        )

    def event(point, button, suffix, *, authorized=True):
        x, y = point
        packet = f"\x1b[<{button};{x};{y}{suffix}".encode()
        os.write(master, input_wire.feed(packet) if authorized else packet)
        pump(0.12)

    def click(label, button=16, *, offset=0, authorized=True):
        x, y = locate(label)
        point = (x + offset, y)
        event(point, 35, "M", authorized=authorized)
        event(point, button, "M", authorized=authorized)
        event(point, button, "m", authorized=authorized)

    def expect_transfer(label, kind, content):
        before = len(candidates)
        click(label)
        end = time.monotonic() + 5
        while len(candidates) <= before and time.monotonic() < end:
            pump(0.15)
        assert candidates[before:] == [(kind, content)], (
            label,
            len(candidates),
            log(),
            [line.strip() for line in screen.display if line.strip()],
        )

    try:
        wait("END_PROBE", 16)
        pump(0.7)
        assert not log() and not opened.exists() and not candidates
        if hook_state == "dragging":
            click("ABS_LINK", authorized=False)
            click("BARE_FILE", offset=12)
            assert not locate("handling a drag")
            click("ABS_LINK")
            wait("handling a drag")
            assert not log() and not opened.exists() and not candidates
            return
        if hook_state != "ready":
            if hook_state == "incompatible":
                wait("Host file opening is off")
            x, y = locate("ABS_LINK")
            press = f"\x1b[<16;{x};{y}M".encode()
            release = f"\x1b[<16;{x};{y}m".encode()
            assert input_wire.feed(press + release) == press + release
            os.write(master, press + release)
            pump(0.5)
            assert not log() and not opened.exists() and not candidates
            assert preview_wire.startup_unverified() == (hook_state == "missing")
            return
        click("ABS_LINK", authorized=False)
        assert not log() and not candidates
        for label in ("TRAVERSAL_LINK", "REMOTE_FILE", "WEB_LINK"):
            click(label)
        click("BARE_FILE", offset=12)
        event(locate("ABS_LINK"), 16, "m")  # no matching press
        event(locate("ABS_LINK"), 16, "M")
        event(locate("URI_LINK"), 16, "m")  # mismatch
        event(locate("ABS_LINK"), 16, "m")  # stale release at original coordinates
        os.write(master, input_wire.feed(b"\x1b[I"))  # focus, then blur before release
        pump(0.12)
        event(locate("ABS_LINK"), 16, "M")
        os.write(master, input_wire.feed(b"\x1b[O"))
        pump(0.12)
        event(locate("ABS_LINK"), 16, "m")
        os.write(master, input_wire.feed(b"\x1b[I"))
        pump(0.12)
        assert not log() and not opened.exists()
        expect_transfer("ABS_LINK", "md", file.read_bytes())
        # Repeated Escape can precede the next terminal report. This focus
        # report must be forwarded, not used as authority for a host action.
        for packet in (b"\x1b", b"\x1b", b"\x1b[I"):
            if forwarded := input_wire.feed(packet):
                os.write(master, forwarded)
            pump(0.08)
        assert input_wire.recovered_framing and not input_wire.disabled
        if not private_mounts:
            assert log() == ["proof.md"]
        file.write_text("PREVIEW_SECOND_SNAPSHOT\n")
        pump(0.4)
        assert len(candidates) == 1
        expect_transfer("URI_LINK", "md", file.read_bytes())
        if not private_mounts:
            assert log() == ["proof.md", "proof.md"]
        for label in ("REL_LINK", "DOT_LINK", "PART_LINK"):
            expect_transfer(label, "md", b"PREVIEW_OTHER_SNAPSHOT\n")
        for label, text in (
            ("REFUSED_LINK", "REFUSED-MUST-NOT-SHOW"),
            ("OVERFLOW_LINK", "OVERFLOW-MUST-NOT-SHOW"),
            ("SLOW_LINK", "TIMED-OUT-MUST-NOT-SHOW"),
        ):
            count = len(log())
            transfers = len(candidates)
            click(label)
            end = time.monotonic() + 6
            if not private_mounts:
                while len(log()) == count and time.monotonic() < end:
                    pump(0.1)
                pump(4.6 if label == "SLOW_LINK" else 0.4)
                assert len(log()) == count + 1
            else:
                wait("Host file open refused")
            assert len(candidates) == transfers and not locate(text)
        if private_mounts:
            expect_transfer(
                "PNG_LINK", "png", file_preview_png.normalize(png_for_test())[0]
            )
            expect_transfer(
                "HTML_LINK", "html", (project / "article.html").read_bytes()
            )
            for label, value in (
                ("HOME_LINK", "PREVIEW_HOME_SNAPSHOT"),
                ("OVERLAY_LINK", "PREVIEW_ORDINARY_ROOT_SNAPSHOT"),
            ):
                expect_transfer(label, "txt", (value + "\n").encode())
            transfers = len(candidates)
            click("OUTSIDE_LINK")
            wait("Host file open refused")
            assert len(candidates) == transfers and not locate(
                "UNEXPECTED-MOUNT-MUST-NOT-SHOW"
            )
        assert not opened.exists()
        click("WEB_LINK", 0)
        assert opened.read_text().strip() == "https://example.invalid/preview-test"
    finally:
        if child.poll() is None:
            os.write(master, b"\x03")
            pump(1.5)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        os.close(master)


if __name__ == "__main__":
    import shutil

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True)
    parser.add_argument("--private-mounts", action="store_true")
    parser.add_argument(
        "--hook-state",
        choices=("ready", "missing", "incompatible", "dragging"),
        default="ready",
    )
    args = parser.parse_args()
    binary = str(Path(shutil.which(args.opencode) or args.opencode).absolute())
    if args.private_mounts:
        unshare = shutil.which("unshare")
        if not unshare:
            parser.error("unshare is required for --private-mounts")
        # A closed-over private directory is removed by the outside parent
        # after unshare exits and the child's namespace drops all mounts.
        with tempfile.TemporaryDirectory(prefix="oc-box-preview-native-") as directory:
            python = (
                "import importlib.util,sys; from pathlib import Path;"
                "path=Path(sys.argv[1]); sys.path.insert(0,str(path.parent));"
                "spec=importlib.util.spec_from_file_location('fixture',path);"
                "fixture=importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture);"
                "fixture.exercise(sys.argv[2],Path(sys.argv[3]),private_mounts=True,hook_state=sys.argv[4])"
            )
            subprocess.run(
                [
                    unshare,
                    "--user",
                    "--map-root-user",
                    "--mount",
                    "--propagation",
                    "private",
                    sys.executable,
                    "-B",
                    "-c",
                    python,
                    str(Path(__file__).resolve()),
                    binary,
                    directory,
                    args.hook_state,
                ],
                check=True,
                timeout=90,
            )
        raise SystemExit(0)
    with tempfile.TemporaryDirectory(prefix="oc-box-preview-native-") as directory:
        exercise(binary, Path(directory), hook_state=args.hook_state)
