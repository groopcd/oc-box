"""Native Ctrl-click wiring with a stub or private synthetic mounts.

Run with pyte installed and --opencode /path/to/an/unmodified/2.3.x/cli.
No provider, normal config, host desktop, real workspace or Podman is used.
The optional --private-mounts mode re-execs under unshare and also exercises
the real reader against ordinary synthetic Linux bind mounts.
"""

import argparse
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


def exercise(binary, root, *, private_mounts=False):
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
                "plugins": [str(plugin)],
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
        )
    scope = (
        "real reader and disposable mounts" if private_mounts else "synthetic reader"
    )
    print(f"PASS: OpenCode {version} native file-preview click/panel ({scope}).")


def run_client(
    binary, root, project, env, endpoint, sid, file, *, private_mounts=False
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

    def event(point, button, suffix):
        x, y = point
        os.write(master, f"\x1b[<{button};{x};{y}{suffix}".encode())
        pump(0.12)

    def click(label, button=16, *, offset=0):
        x, y = locate(label)
        point = (x + offset, y)
        event(point, 35, "M")
        event(point, button, "M")
        event(point, button, "m")

    def dismiss():
        click("[Close preview]", 0)
        wait("END_PROBE")
        assert not locate("File preview (read-only)"), [
            line.strip() for line in screen.display if line.strip()
        ]

    try:
        wait("END_PROBE", 16)
        pump(0.7)
        assert (
            not log() and not opened.exists() and not locate("File preview (read-only)")
        )
        for label in ("TRAVERSAL_LINK", "REMOTE_FILE", "WEB_LINK"):
            click(label)
        click("BARE_FILE", offset=12)
        event(locate("ABS_LINK"), 16, "m")  # no matching press
        event(locate("ABS_LINK"), 16, "M")
        event(locate("URI_LINK"), 16, "m")  # mismatch
        event(locate("ABS_LINK"), 16, "m")  # stale release at original coordinates
        os.write(master, b"\x1b[I")  # focus, then blur between press and release
        pump(0.12)
        event(locate("ABS_LINK"), 16, "M")
        os.write(master, b"\x1b[O")
        pump(0.12)
        event(locate("ABS_LINK"), 16, "m")
        os.write(master, b"\x1b[I")
        pump(0.12)
        assert not log() and not opened.exists()
        click("ABS_LINK")
        wait("PREVIEW_FIRST_SNAPSHOT")
        wait("# This stays literal")
        if not private_mounts:
            assert log() == ["proof.md"]
        file.write_text("PREVIEW_SECOND_SNAPSHOT\n")
        pump(0.4)
        assert locate("PREVIEW_FIRST_SNAPSHOT") and not locate(
            "PREVIEW_SECOND_SNAPSHOT"
        )
        dismiss()
        click("URI_LINK")
        wait("PREVIEW_SECOND_SNAPSHOT")
        if not private_mounts:
            assert log() == ["proof.md", "proof.md"]
        dismiss()
        for label in ("REL_LINK", "DOT_LINK", "PART_LINK"):
            click(label)
            wait("PREVIEW_OTHER_SNAPSHOT")
            dismiss()
        for label, text in (
            ("REFUSED_LINK", "REFUSED-MUST-NOT-SHOW"),
            ("OVERFLOW_LINK", "OVERFLOW-MUST-NOT-SHOW"),
            ("SLOW_LINK", "TIMED-OUT-MUST-NOT-SHOW"),
        ):
            count = len(log())
            click(label)
            end = time.monotonic() + 6
            if not private_mounts:
                while len(log()) == count and time.monotonic() < end:
                    pump(0.1)
                pump(4.6 if label == "SLOW_LINK" else 0.4)
                assert len(log()) == count + 1
            else:
                wait("File preview unavailable")
            assert not locate("File preview (read-only)") and not locate(text)
        if private_mounts:
            for label, value in (
                ("HOME_LINK", "PREVIEW_HOME_SNAPSHOT"),
                ("OVERLAY_LINK", "PREVIEW_ORDINARY_ROOT_SNAPSHOT"),
            ):
                click(label)
                wait(value)
                dismiss()
            click("OUTSIDE_LINK")
            wait("File preview unavailable")
            assert not locate("UNEXPECTED-MOUNT-MUST-NOT-SHOW")
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
                "fixture.exercise(sys.argv[2],Path(sys.argv[3]),private_mounts=True)"
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
                ],
                check=True,
                timeout=90,
            )
        raise SystemExit(0)
    with tempfile.TemporaryDirectory(prefix="oc-box-preview-native-") as directory:
        exercise(binary, Path(directory))
