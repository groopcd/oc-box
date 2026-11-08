import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

APP = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        "oc_box." + name, APP / "src/oc_box" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


box = load("oc_box")
sync = load("sync_config")
ui = load("terminal_ui")


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, value in files.items():
            data = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            tar.addfile(member, io.BytesIO(data))
    return output.getvalue()


class ConfigCacheTests(unittest.TestCase):
    def test_cold_sync_uses_runtime_sdk_and_drops_incompatible_host_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11","custom":"2"}}',
                "package-lock.json": "old host resolution",
            }
            payload = archive(host)
            self.assertFalse(sync.sync_config(root, payload, "1.19.5"))
            config = root / ".config/opencode"
            effective = json.loads((config / "package.json").read_text())
            self.assertEqual(
                effective["dependencies"],
                {"@opencode-ai/plugin": "1.19.5", "custom": "2"},
            )
            self.assertFalse((config / "package-lock.json").exists())
            self.assertIn("1.15.11", host["package.json"])

    def test_cold_sync_keeps_lock_for_matching_sdk(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}',
                "package-lock.json": "matching resolution",
            }
            sync.sync_config(root, archive(host), "1.19.5")
            self.assertEqual(
                (root / ".config/opencode/package-lock.json").read_text(),
                "matching resolution",
            )

    def test_unchanged_dependencies_survive_but_stale_plugins_are_removed(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            modules = config / "node_modules"
            modules.mkdir(parents=True)
            manifest = json.dumps({"dependencies": {"@opencode-ai/plugin": "1.19.5"}})
            (config / "package.json").write_text(manifest)
            (modules / "cached").write_text("already installed")
            (config / "stale-plugin.ts").write_text("do not load again")
            sync.sync_config(
                root,
                archive(
                    {"package.json": manifest, "plugins/current.ts": "current plugin"}
                ),
            )
            self.assertEqual((modules / "cached").read_text(), "already installed")
            self.assertFalse((config / "stale-plugin.ts").exists())
            self.assertTrue((config / "plugins/current.ts").is_file())

    def test_changed_manifest_invalidates_cached_dependencies(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            (config / "node_modules").mkdir(parents=True)
            (config / "package.json").write_text('{"dependencies":{"old":"1"}}')
            (config / "node_modules/cached").write_text("old")
            sync.sync_config(
                root, archive({"package.json": '{"dependencies":{"new":"2"}}'})
            )
            self.assertFalse((config / "node_modules").exists())

    def test_unsafe_archive_does_not_remove_existing_config(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            config.mkdir(parents=True)
            (config / "keep").write_text("original")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                sync.sync_config(root, archive({"../../escaped": "bad"}))
            self.assertEqual((config / "keep").read_text(), "original")

    def test_copied_config_directory_is_private(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            sync.sync_config(root, archive({"opencode.json": "{}"}))
            self.assertEqual((root / ".config/opencode").stat().st_mode & 0o777, 0o700)

    def test_changed_lockfile_invalidates_cached_dependencies(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            (config / "node_modules").mkdir(parents=True)
            manifest = '{"dependencies":{"example":"^1"}}'
            (config / "package.json").write_text(manifest)
            (config / "package-lock.json").write_text("old resolution")
            (config / "node_modules/cached").write_text("old")
            sync.sync_config(
                root,
                archive(
                    {"package.json": manifest, "package-lock.json": "new resolution"}
                ),
            )
            self.assertFalse((config / "node_modules").exists())

    def test_warm_sync_preserves_runtime_generated_sdk_and_lock(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}',
                "package-lock.json": '{"host":"old sdk lock"}',
            }
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.5"))
            # Mainline upgrades the SDK, manifest, and lock after the first sync.
            runtime = '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
            lock = '{"container":"runtime sdk lock"}'
            (config / "package.json").write_text(runtime)
            (config / "package-lock.json").write_text(lock)
            (config / "node_modules").mkdir()
            (config / "node_modules/cached").write_text("already installed")
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertEqual((config / "package.json").read_text(), runtime)
            self.assertEqual((config / "package-lock.json").read_text(), lock)
            self.assertEqual(
                (config / "node_modules/cached").read_text(), "already installed"
            )
            # Warm sync must remain idempotent over many launches.
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))

    def test_sdk_only_existing_workspace_adopts_installed_runtime_cache(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            plugin = config / "node_modules/@opencode-ai/plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text('{"version":"1.19.5"}')
            (config / "package.json").write_text(
                '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
            )
            (config / "package-lock.json").write_text('{"container":"generated"}')
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}'
            }
            self.assertTrue(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertTrue(plugin.is_dir())
            self.assertEqual(
                json.loads((config / "package.json").read_text())["dependencies"][
                    "@opencode-ai/plugin"
                ],
                "1.19.5",
            )

    def test_source_lock_change_invalidates_previously_recorded_cache(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            host = {
                "package.json": '{"dependencies":{"example":"^1"}}',
                "package-lock.json": "old resolution",
            }
            sync.sync_config(root, archive(host), "1.19.5")
            (config / "node_modules").mkdir()
            host["package-lock.json"] = "new resolution"
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertFalse((config / "node_modules").exists())

    def test_runtime_upgrade_invalidates_previously_recorded_cache(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}'
            }
            sync.sync_config(root, archive(host), "1.19.5")
            modules = root / ".config/opencode/node_modules"
            modules.mkdir()
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.6"))
            self.assertFalse(modules.exists())

    def test_arbitrary_custom_dependencies_are_not_adopted_on_sdk_match(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / ".config/opencode"
            plugin = config / "node_modules/@opencode-ai/plugin"
            plugin.mkdir(parents=True)
            (plugin / "package.json").write_text('{"version":"1.19.5"}')
            (config / "package.json").write_text(
                '{"dependencies":{"@opencode-ai/plugin":"1.19.5","custom":"1"}}'
            )
            host = {
                "package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11","custom":"2"}}'
            }
            self.assertFalse(sync.sync_config(root, archive(host), "1.19.5"))
            self.assertFalse(plugin.exists())


class StartupProgressTests(unittest.TestCase):
    def test_heartbeat_stops_after_stage_finishes(self):
        output = io.StringIO()
        with box.loading_stage("Starting test", interval=0.01, stream=output):
            deadline = time.monotonic() + 1
            while "elapsed" not in output.getvalue() and time.monotonic() < deadline:
                time.sleep(0.005)
        snapshot = output.getvalue()
        time.sleep(0.03)
        self.assertIn("elapsed", snapshot)
        self.assertIn("done", snapshot)
        self.assertEqual(snapshot, output.getvalue())

    def test_stage_failure_is_not_reported_as_success(self):
        output = io.StringIO()
        with self.assertRaises(ValueError), box.loading_stage("Failing", stream=output):
            raise ValueError("failure")
        self.assertIn("failed", output.getvalue())
        self.assertNotIn("done", output.getvalue())

    def test_terminal_setup_is_not_mistaken_for_rendered_ui(self):
        detector = ui.VisibleOutput()
        for chunk in (
            b"\x1b[?1049h\x1b[",
            b"2J\x1b[38;2;0;0;0m  \r\n",
            b"\x1b]0;OpenCode",
            b"\x07",
            b"\x1bP+q544e\x1b",
            b"\\",
        ):
            self.assertFalse(detector.feed(chunk))
        self.assertTrue(detector.feed("OpenCode ▀".encode()))

    def test_loading_indicator_never_interrupts_partial_terminal_sequences(self):
        detector = ui.VisibleOutput()
        detector.feed(b"\x1b]0;Title")
        self.assertFalse(detector.safe_to_draw())
        detector.feed(b"\x07\x1b[38;2;")
        self.assertFalse(detector.safe_to_draw())
        detector.feed(b"0;0;0m")
        self.assertTrue(detector.safe_to_draw())
        detector.feed(b"\xe2\x96")
        self.assertFalse(detector.safe_to_draw())
        self.assertTrue(detector.feed(b"\x80"))
        self.assertTrue(detector.safe_to_draw())

    @unittest.skipUnless(sys.platform == "linux", "Linux PTY/termios integration")
    def test_tui_progress_handoff_input_resize_and_terminal_restore(self):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios

        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        fixture = (
            "import os,sys,time,tty; assert os.environ['OC_BOX_RELAY_CHECK']=='present'; tty.setraw(0); "
            "os.write(1,b'\\x1b[?1049h\\x1b[2J\\x1b]0;Booting\\x07'); "
            "time.sleep(0.2); os.write(1,b'UI_READY'); "
            "key=os.read(0,1); size=os.get_terminal_size(0); "
            "os.write(1,b'KEY='+key+(' SIZE=%dx%d'%(size.columns,size.lines)).encode()); "
            "time.sleep(0.15); os.write(1,b'\\x1b[?1049l')"
        )
        relay = "import os,sys; sys.path.insert(0,sys.argv[1]+'/src'); from oc_box.terminal_ui import run_tui; sys.exit(run_tui([sys.executable,'-c',sys.argv[2]],env={**os.environ,'OC_BOX_RELAY_CHECK':'present'},refresh=0.03))"
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", relay, str(APP), fixture],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        output = bytearray()
        sent = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    output.extend(os.read(master, 65536))
                if b"UI_READY" in output and not sent:
                    fcntl.ioctl(
                        slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0)
                    )
                    os.kill(process.pid, signal.SIGWINCH)
                    time.sleep(0.05)
                    os.write(master, b"z")
                    sent = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertIsNotNone(process.poll(), bytes(output))
            self.assertEqual(process.returncode, 0, bytes(output))
            self.assertIn(b"Loading OpenCode, plugins and MCPs", output)
            self.assertIn(b"KEY=z SIZE=120x32", output)
            self.assertNotIn(b"oc-box:", bytes(output).split(b"UI_READY", 1)[1])
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
