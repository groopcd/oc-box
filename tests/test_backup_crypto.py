import ctypes
import json
import os
from pathlib import Path
import pty
import select
import signal
import termios
import time
import shutil
import sys
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from oc_box import backup_crypto as crypto


@unittest.skipUnless(
    shutil.which("age") and shutil.which("age-keygen"),
    "optional age host tools unavailable",
)
class AgeEnvelopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-age-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = self.root / "identity.txt"
        self.identity.write_bytes(crypto.small(["age-keygen"]))
        self.identity.chmod(0o600)
        self.recipient = (
            crypto.small(["age-keygen", "-y", str(self.identity)]).decode().strip()
        )

    def envelope(self, *, kind="portable"):
        staging = self.root / "ciphertext"
        staging.mkdir()
        _, recipient = crypto.generate_key([self.recipient], staging / "key.age")
        components = {}
        for name in (
            "project.age",
            "home.age",
            "registry.age",
            *(("image.age",) if kind == "full" else ()),
        ):
            crypto.encrypt_bytes(
                b"synthetic-private-backup-data", recipient, staging / name
            )
            components[name] = crypto.digest(staging / name)
        manifest = {
            "format": "oc-box-backup",
            "format_version": 1,
            "kind": kind,
            "components": components,
            "private-marker": "private-manifest-data",
        }
        crypto.encrypt_bytes(
            json.dumps(manifest).encode(), recipient, staging / "manifest.age"
        )
        output = self.root / "backup.ocbox"
        with output.open("wb") as stream:
            crypto.pack(staging, stream)
        return output, staging

    def repack(self, staging, output):
        with output.open("wb") as stream:
            crypto.pack(staging, stream)

    def test_age_recipient_envelope_roundtrip_has_no_plaintext_metadata_or_secret(self):
        output, _ = self.envelope()
        raw = output.read_bytes()
        self.assertNotIn(b"synthetic-private-backup-data", raw)
        self.assertNotIn(b"private-manifest-data", raw)
        self.assertNotIn(b"AGE-SECRET-KEY-1", raw)
        with crypto.opened(output, [self.identity]) as (manifest, staging, key):
            self.assertEqual(manifest["private-marker"], "private-manifest-data")
            self.assertEqual(
                crypto.decrypt_bytes(staging / "project.age", key),
                b"synthetic-private-backup-data",
            )

    def test_ciphertext_tamper_or_component_swap_is_rejected(self):
        output, staging = self.envelope()
        first = staging / "project.age"
        raw = bytearray(first.read_bytes())
        raw[-1] ^= 1
        first.write_bytes(raw)
        self.repack(staging, output)
        with self.assertRaisesRegex(crypto.BackupError, "checksum/size mismatch"):
            with crypto.opened(output, [self.identity]):
                self.fail("tampered component accepted")

    def test_wrong_recipient_identity_cannot_open_backup(self):
        output, _ = self.envelope()
        wrong = self.root / "wrong.txt"
        wrong.write_bytes(crypto.small(["age-keygen"]))
        wrong.chmod(0o600)
        with self.assertRaises(crypto.BackupError):
            with crypto.opened(output, [wrong]):
                self.fail("wrong identity accepted")

    def test_manifest_tamper_is_authenticated(self):
        output, staging = self.envelope()
        manifest = staging / "manifest.age"
        raw = bytearray(manifest.read_bytes())
        raw[-10] ^= 1
        manifest.write_bytes(raw)
        self.repack(staging, output)
        with self.assertRaises(crypto.BackupError):
            with crypto.opened(output, [self.identity]):
                self.fail("tampered manifest accepted")

    def test_duplicate_path_link_and_unknown_outer_entries_are_refused(self):
        for name, kind in (
            ("../outside", tarfile.REGTYPE),
            ("project.age", tarfile.SYMTYPE),
            ("extra", tarfile.REGTYPE),
        ):
            with self.subTest(name=name):
                output = self.root / "malicious.ocbox"
                with tarfile.open(output, "w") as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "/tmp/outside"
                    archive.addfile(member)
                with self.assertRaises(crypto.BackupError):
                    with crypto.opened(output, [self.identity]):
                        self.fail("unsafe outer entry accepted")

    def test_duplicate_valid_outer_name_is_rejected(self):
        output = self.root / "duplicate.ocbox"
        with tarfile.open(output, "w") as archive:
            archive.addfile(tarfile.TarInfo("key.age"))
            archive.addfile(tarfile.TarInfo("key.age"))
        with self.assertRaisesRegex(crypto.BackupError, "duplicates"):
            with crypto.opened(output, [self.identity]):
                self.fail("duplicate accepted")

    def test_age_plugins_or_insecure_keys_are_never_executed(self):
        output, staging = self.envelope()
        plugin = self.root / "plugin.txt"
        plugin.write_text(
            "AGE-PLUGIN-TEST-1MALICIOUS\nAGE-SECRET-KEY-1" + "A" * 58 + "\n"
        )
        plugin.chmod(0o600)
        with patch.object(crypto, "small") as execute:
            with self.assertRaisesRegex(crypto.BackupError, "native unencrypted"):
                crypto.load_key(staging / "key.age", [plugin])
            execute.assert_not_called()
        self.identity.chmod(0o644)
        with self.assertRaisesRegex(crypto.BackupError, "private"):
            with crypto.opened(output, [self.identity]):
                self.fail("insecure identity accepted")

    def test_decrypted_key_envelope_cannot_activate_an_age_plugin(self):
        malicious = b"AGE-PLUGIN-TEST-1MALICIOUS\nAGE-SECRET-KEY-1" + b"A" * 58 + b"\n"
        with patch.object(crypto, "small", return_value=malicious) as execute:
            with self.assertRaisesRegex(
                crypto.BackupError, "Invalid backup key envelope"
            ):
                crypto.load_key(self.root / "untrusted.age", [])
        self.assertEqual(execute.call_count, 1)

    def test_real_streaming_roundtrip_does_not_spool_plaintext(self):
        staging = self.root / "stream.age"
        value = b"large-synthetic-private-value" * 100000
        producer = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'large-synthetic-private-value'*100000)",
        ]
        crypto.encrypt_stream(producer, self.recipient, staging)
        private = self.identity.read_bytes()
        with crypto.memory_file(private) as identity, crypto.memory_file() as output:
            crypto.stream_pipeline(
                [
                    "age",
                    "--decrypt",
                    "--identity",
                    f"/proc/self/fd/{identity.fileno()}",
                    str(staging),
                ],
                [
                    sys.executable,
                    "-c",
                    "import shutil,sys; shutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)",
                ],
                first_fds=(identity.fileno(),),
                destination=output,
            )
            output.seek(0)
            self.assertEqual(output.read(), value)
        self.assertNotIn(b"large-synthetic-private-value", staging.read_bytes())

    def test_pipeline_failure_and_timeout_terminate_both_processes(self):
        with self.assertRaisesRegex(crypto.BackupError, "failed"):
            crypto.stream_pipeline(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                [sys.executable, "-c", "raise SystemExit(1)"],
                timeout=2,
            )
        with self.assertRaisesRegex(crypto.BackupError, "timed out"):
            crypto.stream_pipeline(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                [sys.executable, "-c", "import time; time.sleep(20)"],
                timeout=0.1,
            )

    def test_pipeline_failures_report_stage_category_and_never_private_output(self):
        cases = (
            (
                "consumer",
                "synthetic-secret: no space left on device /private/path",
                125,
                ("space exhausted",),
            ),
            (
                "producer",
                "synthetic-secret failed to decrypt",
                2,
                ("decryption/authentication failed",),
            ),
            (
                "producer",
                "synthetic-secret unrecognized failure",
                2,
                ("private output was not logged",),
            ),
            (
                "consumer",
                "payload does not match any formats: private-file: no space left on device",
                125,
                ("space exhausted",),
            ),
            (
                "consumer",
                "write /private/synthetic-secret: disk quota exceeded",
                125,
                ("quota exceeded", "free space does not establish quota"),
            ),
        )
        for stage, text, code, categories in cases:
            with (
                self.subTest(stage=stage, category=categories),
                self.assertRaises(crypto.BackupError) as error,
            ):
                failed = "import sys; " + (
                    "sys.stdin.buffer.read(); " if stage == "consumer" else ""
                )
                failed += (
                    "sys.stderr.write(" + repr(text) + "); sys.exit(" + str(code) + ")"
                )
                producer = (
                    failed
                    if stage == "producer"
                    else "import sys; sys.stdout.buffer.write(b'fixture-image')"
                )
                consumer = (
                    failed
                    if stage == "consumer"
                    else "import sys; sys.stdin.buffer.read()"
                )
                crypto.stream_pipeline(
                    [sys.executable, "-c", producer], [sys.executable, "-c", consumer]
                )
            message = str(error.exception)
            self.assertIn(stage + " exit " + str(code), message)
            for category in categories:
                self.assertIn(category, message)
            for private in ("synthetic-secret", "/private", "private-file"):
                self.assertNotIn(private, message)

    def test_stream_tools_honor_explicit_temp_location_without_forwarding_secrets(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-pipeline-temp-") as temporary:
            with (
                patch.dict(
                    os.environ,
                    {
                        "TMPDIR": temporary,
                        "OPENAI_API_KEY": "synthetic-private",
                        "AGE_PASSPHRASE": "synthetic-private",
                    },
                ),
                crypto.memory_file() as captured,
            ):
                crypto.stream_pipeline(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "import json,os,sys; sys.stdout.write(json.dumps({'tmp':os.environ.get('TMPDIR'),'provider':os.environ.get('OPENAI_API_KEY'),'password':os.environ.get('AGE_PASSPHRASE')}))",
                    ],
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        "import json,os,sys; item=json.load(sys.stdin); item['consumer_tmp']=os.environ.get('TMPDIR'); print(json.dumps(item))",
                    ],
                    destination=captured,
                )
                captured.seek(0)
                result = json.load(captured)
            self.assertEqual(
                result,
                {
                    "tmp": temporary,
                    "consumer_tmp": temporary,
                    "provider": None,
                    "password": None,
                },
            )

    def test_age_passphrase_envelope_uses_real_tty_never_env_or_argv(self):
        # Exercise only synthetic data through the real age prompt and key
        # envelope, preserving the surrounding terminal on all paths.
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        output = self.root / "passphrase.age"
        code = "import fcntl,os,sys,termios; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); sys.path.insert(0,sys.argv[1]+'/src'); from oc_box import backup_crypto as c; from pathlib import Path; c.generate_key([],Path(sys.argv[2]),minimum=8); p=c.load_key(Path(sys.argv[2]),[]); assert b'AGE-SECRET-KEY-1' in p; print('PASSPHRASE_ROUNDTRIP_OK',flush=True)"
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                str(Path(__file__).resolve().parents[1]),
                str(output),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=crypto.environment(),
        )
        seen = bytearray()
        phrase = b"eight888"
        prompts = [
            b"New backup passphrase",
            b"Confirm backup passphrase",
            b"Enter passphrase",
        ]
        step, offset = 0, 0
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    seen.extend(os.read(master, 65536))
                if step < len(prompts) and prompts[step] in seen[offset:]:
                    os.write(master, phrase + b"\n")
                    offset = len(seen)
                    step += 1
                if process.poll() is not None:
                    break
            self.assertIsNotNone(process.poll(), bytes(seen))
            self.assertEqual(process.returncode, 0, bytes(seen))
            self.assertIn(b"PASSPHRASE_ROUNDTRIP_OK", seen)
            self.assertNotIn(phrase, seen)
            self.assertNotIn(phrase, output.read_bytes())
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            os.close(master)
            os.close(slave)


class PipelineOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="oc-box-pipeline-owned-", dir="/tmp"
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Adopt our orphaned synthetic descendants so the test can reap
        # them and assert disappearance, not merely ignore lingering zombies.
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)

        def restore_subreaper():
            self.assertEqual(libc.prctl(36, previous.value, 0, 0, 0), 0)

        self.addCleanup(restore_subreaper)
        self.unrelated = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(10)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=self.root,
        )

        def stop_unrelated():
            if self.unrelated.poll() is None:
                self.unrelated.kill()
            self.unrelated.wait(timeout=2)

        self.addCleanup(stop_unrelated)

    def owned_descendant_case(self, stage, outcome, resistant, cleanup_error=False):
        ready = self.root / "ready.json"
        ready.unlink(missing_ok=True)
        # Each child has its own finite alarm, even when it ignores TERM. No
        # real services, host engines or data participate in these pipelines.
        leader_code = """
import json, os, pathlib, signal, sys, time
ready = pathlib.Path(sys.argv[1])
child = os.fork()
if child == 0:
    signal.alarm(10)
    if sys.argv[2] == 'resistant':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    links = [os.readlink('/proc/self/fd/' + str(fd))
             for fd in (0, 1, 2)]
    try:
        key_inherited = 'memfd:oc-box-backup-memory' in os.readlink('/proc/self/fd/' + sys.argv[3])
    except OSError:
        key_inherited = False
    ready.write_text(json.dumps({
        'pid': os.getpid(), 'leader': os.getppid(),
        'group': os.getpgrp(), 'session': os.getsid(0),
        'pipe': any(link.startswith('pipe:') for link in links),
        'private_output': any('memfd:oc-box-backup-memory' in link for link in links),
        'key_inherited': key_inherited,
    }))
    time.sleep(10)
    os._exit(0)
while not ready.exists():
    time.sleep(0.005)
os._exit(0)
"""
        other_code = """
import json, pathlib, signal, sys, time
signal.alarm(10)
if sys.argv[2] == 'failure':
    ready = pathlib.Path(sys.argv[1])
    while True:
        try:
            leader = json.loads(ready.read_text())['leader']
            try:
                state = pathlib.Path('/proc/' + str(leader) + '/stat').read_text().split(') ')[1][0]
            except FileNotFoundError:
                state = 'Z'  # The unfixed pipeline already reaped its leader.
            if state == 'Z':
                break
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.005)
    sys.stderr.write('synthetic-private-output: no space left on device /private/path')
    sys.exit(23)
elif sys.argv[2] == 'success':
    sys.exit(0)
time.sleep(10)
"""
        leader_command = [
            sys.executable,
            "-B",
            "-c",
            leader_code,
            str(ready),
            "resistant" if resistant else "cooperative",
        ]
        other_command = [sys.executable, "-B", "-c", other_code, str(ready), outcome]
        first, second = (
            (leader_command, other_command)
            if stage == "producer"
            else (other_command, leader_command)
        )
        children = []
        real_popen, real_sleep, real_killpg = subprocess.Popen, time.sleep, os.killpg
        seen_exited_leader = []
        sent_groups = []
        cancellation = KeyboardInterrupt()
        cancelled = False

        def spawned(*args, **kwargs):
            process = real_popen(*args, cwd=self.root, **kwargs)
            children.append(process)
            return process

        def pause(seconds):
            nonlocal cancelled
            try:
                leader = json.loads(ready.read_text())["leader"]
                try:
                    state = Path(f"/proc/{leader}/stat").read_text().split(") ")[1][0]
                except FileNotFoundError:
                    state = (
                        "Z"
                        if any(
                            child.pid == leader and child.returncode == 0
                            for child in children
                        )
                        else "?"
                    )
                if state == "Z":
                    seen_exited_leader.append(leader)
                    if outcome == "cancel" and not cancelled:
                        cancelled = True
                        raise cancellation
            except (FileNotFoundError, ValueError):
                pass
            real_sleep(seconds)

        def signal_group(group, signum):
            self.assertIn(group, [process.pid for process in children])
            self.assertNotEqual(group, os.getpgrp())
            # A waitable child pins this exact group ID even after its exit.
            os.waitid(os.P_PID, group, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            sent_groups.append((group, signum))
            if cleanup_error and signum == signal.SIGTERM:
                raise PermissionError("synthetic-private-cleanup-error")
            return real_killpg(group, signum)

        descendant = None
        started = time.monotonic()
        try:
            with (
                crypto.memory_file(b"synthetic-private-key") as identity,
                crypto.memory_file() as destination,
                patch.object(crypto.subprocess, "Popen", side_effect=spawned),
                patch.object(crypto.time, "sleep", side_effect=pause),
                patch.object(crypto.os, "killpg", side_effect=signal_group),
            ):
                leader_command.append(str(identity.fileno()))
                arguments = dict(
                    first_fds=(identity.fileno(),), destination=destination, timeout=0.5
                )
                if outcome == "cancel":
                    with self.assertRaises(KeyboardInterrupt) as error:
                        crypto.stream_pipeline(first, second, **arguments)
                    self.assertIs(error.exception, cancellation)
                elif outcome == "success":
                    if cleanup_error:
                        with self.assertRaisesRegex(
                            crypto.BackupError, "cleanup failed"
                        ) as error:
                            crypto.stream_pipeline(first, second, **arguments)
                        self.assertNotIn(
                            "synthetic-private-cleanup-error", str(error.exception)
                        )
                    else:
                        crypto.stream_pipeline(first, second, **arguments)
                else:
                    with self.assertRaises(crypto.BackupError) as error:
                        crypto.stream_pipeline(first, second, **arguments)
                    message = str(error.exception)
                    if outcome == "failure":
                        failed = "consumer" if stage == "producer" else "producer"
                        self.assertIn(f"{failed} exit 23", message)
                        self.assertIn("space exhausted", message)
                    else:
                        self.assertIn("timed out", message)
                    self.assertNotIn("synthetic-private-output", message)
                    self.assertNotIn("/private/path", message)
                    self.assertNotIn("synthetic-private-cleanup-error", message)
            self.assertTrue(identity.closed)
            self.assertTrue(destination.closed)
            self.assertIsNone(self.unrelated.poll(), "unrelated group was signaled")
            self.assertLess(time.monotonic() - started, 5)
            data = json.loads(ready.read_text())
            descendant = data["pid"]
            leader = children[0 if stage == "producer" else 1]
            self.assertEqual(data["leader"], leader.pid)
            self.assertEqual(data["group"], leader.pid)
            self.assertEqual(data["session"], leader.pid)
            self.assertEqual(leader.returncode, 0)
            self.assertTrue(data["pipe"])
            self.assertTrue(data["private_output"])
            self.assertEqual(data["key_inherited"], stage == "producer")
            if outcome in ("timeout", "cancel"):
                self.assertIn(leader.pid, seen_exited_leader)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                reaped, _ = os.waitpid(descendant, os.WNOHANG)
                if reaped:
                    break
                real_sleep(0.01)
            self.assertEqual(
                reaped, descendant, "owned descendant survived pipeline cleanup"
            )
            self.assertFalse(Path(f"/proc/{descendant}").exists())
            descendant = None
            self.assertTrue(sent_groups)
            self.assertEqual(
                set(sent_groups),
                {
                    (process.pid, signum)
                    for process in children
                    for signum in (signal.SIGTERM, signal.SIGKILL)
                },
            )
            self.assertTrue(all(process.returncode is not None for process in children))
            self.assertTrue(children[0].stdout.closed)
            self.assertEqual(list(self.root.iterdir()), [ready])
        finally:
            # Failure of the regression must not leave the demonstrated leak.
            # The unreaped adopted child pins its PID until this exact wait.
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
            if descendant is None and ready.exists():
                descendant = json.loads(ready.read_text())["pid"]
            if descendant is not None:
                try:
                    reaped, _ = os.waitpid(descendant, os.WNOHANG)
                    if not reaped:
                        os.kill(descendant, signal.SIGKILL)
                        os.waitpid(descendant, 0)
                except ChildProcessError:
                    pass

    def test_timeout_cleans_exited_stage_groups_and_term_resistant_descendants(self):
        for stage in ("producer", "consumer"):
            for resistant in (False, True):
                with self.subTest(stage=stage, resistant=resistant):
                    self.owned_descendant_case(stage, "timeout", resistant)

    def test_failed_stage_preserves_category_and_cleans_other_exited_group(self):
        for stage in ("producer", "consumer"):
            with self.subTest(exited_stage=stage):
                self.owned_descendant_case(stage, "failure", True)

    def test_cancellation_cleans_exited_stage_groups_without_replacing_primary(self):
        for stage in ("producer", "consumer"):
            with self.subTest(stage=stage):
                self.owned_descendant_case(stage, "cancel", True)

    def test_completed_stages_do_not_leave_descriptor_holding_descendants(self):
        for stage in ("producer", "consumer"):
            with self.subTest(stage=stage):
                self.owned_descendant_case(stage, "success", True)

    def test_cleanup_signal_error_does_not_replace_primary_or_skip_other_groups(self):
        for stage in ("producer", "consumer"):
            with self.subTest(stage=stage):
                self.owned_descendant_case(stage, "failure", True, cleanup_error=True)

    def test_cleanup_signal_error_prevents_false_success_without_private_output(self):
        self.owned_descendant_case("producer", "success", True, cleanup_error=True)

    def test_cleanup_refuses_to_signal_after_ownership_anchor_was_reaped(self):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=self.root,
        )
        process.wait(timeout=2)
        with (
            patch.object(crypto.os, "killpg") as signal_group,
            self.assertRaisesRegex(crypto.BackupError, "cleanup failed"),
        ):
            crypto.cleanup_pipeline([process])
        signal_group.assert_not_called()


class BackupEnvironmentTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("age"), "optional age host tool unavailable")
    def test_backup_age_bridge_keeps_password_out_of_argv_env_and_files(self):
        phrase = "éight888"
        with tempfile.TemporaryDirectory(prefix="oc-box-password-bridge-") as temporary:
            path = Path(temporary) / "key.age"
            with (
                patch.object(crypto.secret_prompt, "read_secret", return_value=phrase),
                patch.object(
                    crypto.subprocess, "Popen", wraps=subprocess.Popen
                ) as child,
            ):
                crypto.encrypt_passphrase(b"synthetic-private-key", path, 8)
            arguments = child.call_args.args[0]
            self.assertNotIn(phrase, " ".join(arguments))
            self.assertNotIn(phrase, json.dumps(child.call_args.kwargs["env"]))
            self.assertNotIn(phrase.encode(), path.read_bytes())
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_stricter_backup_policy_applies_to_creation_and_not_recipients(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-backup-policy-") as temporary:
            settings = Path(temporary) / "oc-box/config.json"
            settings.parent.mkdir()
            settings.write_text('{"backup_min_passphrase_length":16}')
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary}):
                minimum = crypto.passphrase_minimum([])
                self.assertEqual(minimum, 16)
                self.assertEqual(crypto.passphrase_minimum([], 8), 8)
                with (
                    patch.object(
                        crypto.secret_prompt, "read_secret", return_value="eight888"
                    ),
                    patch.object(crypto.subprocess, "Popen") as child,
                    self.assertRaisesRegex(crypto.BackupError, "16 or more"),
                ):
                    crypto.encrypt_passphrase(
                        b"key", Path(temporary) / "unused.age", minimum
                    )
                child.assert_not_called()
                settings.write_text('{"backup_min_passphrase_length":7}')
                self.assertIsNone(crypto.passphrase_minimum(["public-key"]))
                with self.assertRaisesRegex(
                    crypto.BackupError, "backup_min_passphrase_length"
                ):
                    crypto.passphrase_minimum([])

    def test_backup_creation_policy_rejects_bad_options_before_backend_access(self):
        from oc_box import backups
        from oc_box import oc_box as box

        for options in (
            ["--min-passphrase-length", "7"],
            ["--min-passphrase-length", "1025"],
            ["--recipient", "age1" + "q" * 58, "--min-passphrase-length", "8"],
        ):
            with (
                self.subTest(options=options),
                patch.object(box, "find_workspace") as lookup,
                self.assertRaises(crypto.BackupError),
            ):
                args = backups.parser_for("backup").parse_args(
                    ["--output", "/tmp/example.ocbox", *options]
                )
                backups.make_backup(box, args)
            lookup.assert_not_called()

    def test_short_or_mismatched_backup_password_never_starts_age(self):
        for answers in (["short77"], ["eight888", "different"]):
            with (
                self.subTest(answers=answers),
                patch.object(crypto.secret_prompt, "read_secret", side_effect=answers),
                patch.object(crypto.subprocess, "Popen") as child,
                self.assertRaises(crypto.BackupError),
            ):
                crypto.encrypt_passphrase(b"synthetic-key", Path("unused.age"), 8)
            child.assert_not_called()

    def test_backup_children_never_inherit_tokens_loader_hooks_or_age_settings(self):
        with patch.dict(
            os.environ,
            {
                "PERSONAL_GITHUB_TOKEN": "synthetic",
                "BRAVE_API_KEY": "synthetic-api",
                "AGE_PASSPHRASE": "not-used",
                "AGE_PLUGIN_PATH": "/bad",
                "LD_PRELOAD": "/bad",
                "PYTHONPATH": "/bad",
                "HTTP_PROXY": "http://secret.invalid",
            },
        ):
            env = crypto.environment()
        for key in (
            "PERSONAL_GITHUB_TOKEN",
            "BRAVE_API_KEY",
            "AGE_PASSPHRASE",
            "AGE_PLUGIN_PATH",
            "LD_PRELOAD",
            "PYTHONPATH",
            "HTTP_PROXY",
        ):
            self.assertNotIn(key, env)

    def test_missing_age_yields_actionable_optional_dependency_message(self):
        with patch.object(crypto.shutil, "which", return_value=None):
            with self.assertRaisesRegex(crypto.BackupError, "age, age-keygen"):
                crypto.tools()


if __name__ == "__main__":
    unittest.main()
