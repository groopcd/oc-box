"""Offline two-pass backup diagnostics; no host acceptance or workspace calls.

Mocked process boundaries use anonymous memory only. Optional native-age tests
write private ciphertext in fresh temporary storage; all tar/OCI source bytes,
identities and decrypted streams stay in memory. The original scanner is used
without changed filters. No test invokes a container engine or OpenCode.
"""

import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oc_box import backup_crypto as crypto
from oc_box import backups
from tools import diagnose_sharing_backup as diagnostic
from tools import sharing_backup_probe as probe


SCANNER = Path(__file__).resolve().parents[1] / "tools/sharing_archive_scan.py"
ORIGINAL_SCANNER = SCANNER.with_name("sharing_archive_scan_original.py")
PRIVATE = b"AGE-SECRET-KEY-1" + b"A" * 58 + b"\n"
SENSITIVE = "SYNTHETIC_PRIVATE_PROBE_CONTENT"
RESULT_KEYS = {
    "authenticated",
    "authentication_exit",
    "scanner_producer_exit",
    "scanner_exit",
    "scanner_category",
    "timed_out",
}


class FakeProcess:
    def __init__(self, pid, code, *, producer=False, term_stops=True):
        self.pid = pid
        self.returncode = code
        self.term_stops = term_stops
        self.stdout = io.BytesIO() if producer else None
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fixed-test-child", timeout)
        return self.returncode


class FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        self.now += 0.01
        return self.now

    def sleep(self, duration):
        self.now += duration


class ProbeBoundaryTests(unittest.TestCase):
    def test_pax_reason_requires_a_validated_refusal_and_no_capture_overflow(self):
        authentication = ([0, 0], True, False, b"", b"", False)
        diagnostic = b"archive-scan:malformed-pax\npax-reason:xattr-value-nul\n"
        cases = (
            ([0, 0], True, False, probe.REFUSAL, False, "unknown", "not-observed"),
            ([0, 2], True, False, probe.REFUSAL, False, "unknown", "not-observed"),
            ([0, 1], True, False, probe.SUCCESS, False, "unknown", "not-observed"),
            ([0, 1], True, False, b"malformed", False, "unknown", "not-observed"),
            (
                [0, 1],
                True,
                False,
                probe.REFUSAL,
                True,
                "capture-overflow",
                "not-observed",
            ),
            (
                [-signal.SIGPIPE, 1],
                True,
                False,
                probe.REFUSAL,
                False,
                "malformed-pax",
                "xattr-value-nul",
            ),
            (
                [-signal.SIGTERM, 1],
                False,
                True,
                probe.REFUSAL,
                False,
                "malformed-pax",
                "xattr-value-nul",
            ),
        )
        for codes, complete, expired, stdout, overflow, category, reason in cases:
            with self.subTest(
                codes=codes, stdout=stdout, overflow=overflow, timeout=expired
            ):
                with crypto.memory_file(b"ciphertext") as ciphertext:
                    with (
                        patch.object(
                            probe.os,
                            "open",
                            side_effect=lambda *a: os.dup(ciphertext.fileno()),
                        ),
                        patch.object(
                            probe,
                            "_pipeline",
                            side_effect=[
                                authentication,
                                (
                                    codes,
                                    complete,
                                    expired,
                                    stdout,
                                    diagnostic,
                                    overflow,
                                ),
                            ],
                        ),
                    ):
                        result = probe.probe_component(
                            crypto, "unused", PRIVATE, SCANNER, "oci", pax_reason=True
                        )
                self.assertEqual(result["scanner_category"], category)
                self.assertEqual(result["pax_reason"], reason)
                self.assertEqual(result["timed_out"], expired)
                self.assertTrue(result["authenticated"])

    def test_pax_reason_protocol_is_exact_and_preserves_original_refusal(self):
        authentication = ([0, 0], True, False, b"", b"", False)
        for reason in probe.PAX_REASONS:
            if reason == "not-observed":
                continue
            original = b"archive-scan:malformed-pax\n"
            for suffix, expected_category, expected_reason in (
                (("pax-reason:" + reason + "\n").encode(), "malformed-pax", reason),
                (
                    b"pax-reason:" + SENSITIVE.encode() + b"\n",
                    "unknown",
                    "not-observed",
                ),
                (b"", "unknown", "not-observed"),
                (
                    ("pax-reason:" + reason + "\n").encode() + b"extra",
                    "unknown",
                    "not-observed",
                ),
            ):
                with self.subTest(reason=reason, suffix=expected_reason):
                    with crypto.memory_file(b"ciphertext") as ciphertext:
                        with (
                            patch.object(
                                probe.os,
                                "open",
                                side_effect=lambda *a: os.dup(ciphertext.fileno()),
                            ),
                            patch.object(
                                probe,
                                "_pipeline",
                                side_effect=[
                                    authentication,
                                    (
                                        [0, 1],
                                        True,
                                        False,
                                        probe.REFUSAL,
                                        original + suffix,
                                        False,
                                    ),
                                ],
                            ) as pipeline,
                        ):
                            result = probe.probe_component(
                                crypto,
                                "unused",
                                PRIVATE,
                                SCANNER,
                                "oci",
                                pax_reason=True,
                            )
                    self.assertTrue(result["authenticated"])
                    self.assertEqual(result["scanner_exit"], 1)
                    self.assertEqual(result["scanner_category"], expected_category)
                    self.assertEqual(result["pax_reason"], expected_reason)
                    self.assertNotIn(SENSITIVE, json.dumps(result))
                    self.assertEqual(
                        pipeline.call_args_list[1].args[2],
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            str(SCANNER.with_name("sharing_pax_reason.py")),
                            str(SCANNER),
                            "oci",
                        ],
                    )

    def invoke(self, specifications=None, *, timeout=10, clock=None):
        specifications = (
            [{}, {}, {}, {"output": probe.SUCCESS}]
            if specifications is None
            else specifications
        )
        calls, children, signals, reads = [], [], [], []

        @contextlib.contextmanager
        def memory_file(content=b""):
            with crypto.memory_file(content) as stream:
                # Track bounded protocol reads without replacing the memfd.
                class Capture:
                    def __getattr__(self, name):
                        return getattr(stream, name)

                    def read(self, size=-1):
                        reads.append(size)
                        return stream.read(size)

                yield Capture()

        supplied_crypto = SimpleNamespace(
            memory_file=memory_file,
            environment=crypto.environment,
            BackupError=crypto.BackupError,
        )

        def popen(command, **kwargs):
            index = len(calls)
            calls.append((list(command), kwargs))
            self.assertLess(index, len(specifications), "unexpected-child")
            specification = specifications[index]
            if "spawn_error" in specification:
                raise OSError(SENSITIVE)
            producer = index % 2 == 0
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("shell", kwargs)
            self.assertNotIn(PRIVATE.decode().strip(), repr(command))
            self.assertNotIn(SENSITIVE, repr(command))
            self.assertEqual(
                command[:6],
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    probe.EXEC_LIMIT,
                    str(probe.PROTOCOL_BYTES),
                ],
            )
            # Inspect the actual exec payload, not the trusted resource wrapper.
            command = command[6:]
            self.assertNotIn(SENSITIVE, repr(kwargs["env"]))
            for name in (
                "PROVIDER_API_KEY",
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "PERSONAL_GITHUB_TOKEN",
                "OPENCODE_AUTH_CONTENT",
                "AGE_PASSPHRASE",
            ):
                self.assertNotIn(name, kwargs["env"])
            if producer:
                self.assertEqual(command[:3], ["age", "--decrypt", "--identity"])
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                identity_fd, ciphertext_fd = kwargs["pass_fds"]
                self.assertEqual(command[3], f"/proc/self/fd/{identity_fd}")
                self.assertEqual(command[4], f"/proc/self/fd/{ciphertext_fd}")
                self.assertTrue(os.pread(identity_fd, 16384, 0) == PRIVATE)
                self.assertEqual(os.lseek(identity_fd, 0, os.SEEK_CUR), 0)
                self.assertEqual(os.lseek(ciphertext_fd, 0, os.SEEK_CUR), 0)
                # Make the second pass prove it rewinds the same pinned fds.
                os.lseek(identity_fd, 1, os.SEEK_SET)
                os.lseek(ciphertext_fd, 1, os.SEEK_SET)
                if index == 2:
                    self.assertEqual(kwargs["pass_fds"], calls[0][1]["pass_fds"])
            else:
                self.assertIs(kwargs["stdin"], children[-1].stdout)
                self.assertNotIn("pass_fds", kwargs)
                expected = (
                    [sys.executable, "-I", "-B", "-c", probe.EOF_SINK]
                    if index == 1
                    else [sys.executable, "-I", "-B", str(SCANNER), "tar"]
                )
                self.assertEqual(command, expected)
                self.assertTrue(
                    stat.S_ISREG(os.fstat(kwargs["stdout"].fileno()).st_mode)
                )
                # Child writes reach the memfd directly, not the parent's
                # buffered file object; fstat must observe their actual size.
                os.write(kwargs["stdout"].fileno(), specification.get("output", b""))
            errors = kwargs["stderr"]
            self.assertTrue(stat.S_ISREG(os.fstat(errors.fileno()).st_mode))
            self.assertIn("memfd:", os.readlink(f"/proc/self/fd/{errors.fileno()}"))
            os.write(errors.fileno(), specification.get("errors", b""))
            child = FakeProcess(
                71000 + index,
                specification.get("code", 0),
                producer=producer,
                term_stops=specification.get("term_stops", True),
            )
            children.append(child)
            return child

        def killpg(pid, sig):
            matches = [child for child in children if child.pid == pid]
            self.assertEqual(len(matches), 1, "signal-outside-owned-children")
            child = matches[0]
            self.assertIsNone(child.poll(), "signal-already-exited-child")
            signals.append((pid, sig))
            if sig == signal.SIGKILL or child.term_stops:
                child.returncode = -sig

        output, errors = io.StringIO(), io.StringIO()
        with crypto.memory_file(b"synthetic-ciphertext") as ciphertext:
            with contextlib.ExitStack() as stack:
                opener = stack.enter_context(
                    patch.object(
                        probe.os,
                        "open",
                        side_effect=lambda *a: os.dup(ciphertext.fileno()),
                    )
                )
                stack.enter_context(
                    patch.object(probe.subprocess, "Popen", side_effect=popen)
                )
                stack.enter_context(
                    patch.object(probe.os, "killpg", side_effect=killpg)
                )
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            key: SENSITIVE
                            for key in (
                                "PROVIDER_API_KEY",
                                "GITHUB_TOKEN",
                                "GH_TOKEN",
                                "PERSONAL_GITHUB_TOKEN",
                                "OPENCODE_AUTH_CONTENT",
                                "AGE_PASSPHRASE",
                            )
                        },
                    )
                )
                if clock is not None:
                    stack.enter_context(
                        patch.object(probe.time, "monotonic", clock.monotonic)
                    )
                    stack.enter_context(patch.object(probe.time, "sleep", clock.sleep))
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                ):
                    result = probe.probe_component(
                        supplied_crypto,
                        "pinned-encrypted.age",
                        PRIVATE,
                        SCANNER,
                        "tar",
                        timeout,
                    )
                opener.assert_called_once_with(
                    "pinned-encrypted.age",
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                )
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertNotIn(SENSITIVE, json.dumps(result))
        self.assertTrue(all(size == probe.PROTOCOL_BYTES + 1 for size in reads))
        for child in children:
            if child.stdout is not None:
                self.assertTrue(child.stdout.closed)
            self.assertTrue(
                all(value is not None and 0 <= value <= 3 for value in child.waits)
            )
        return result, calls, children, signals

    def test_independent_authentication_then_pinned_second_stream(self):
        result, calls, _, signals = self.invoke()
        self.assertEqual(
            result,
            {
                "authenticated": True,
                "authentication_exit": 0,
                "scanner_producer_exit": 0,
                "scanner_exit": 0,
                "scanner_category": "ok",
                "timed_out": False,
            },
        )
        self.assertEqual(len(calls), 4)
        self.assertEqual(signals, [])

    def test_either_authentication_stage_failure_skips_scanner(self):
        for producer, sink in ((1, 0), (0, 1), (-signal.SIGPIPE, 0), (1, 1)):
            with self.subTest(producer=producer, sink=sink):
                result, calls, _, signals = self.invoke(
                    [{"code": producer, "errors": SENSITIVE.encode()}, {"code": sink}]
                )
                self.assertFalse(result["authenticated"])
                self.assertEqual(result["authentication_exit"], producer)
                self.assertIsNone(result["scanner_producer_exit"])
                self.assertIsNone(result["scanner_exit"])
                self.assertEqual(result["scanner_category"], "unknown")
                self.assertEqual(len(calls), 2)
                self.assertEqual(signals, [])

    def test_exact_allowlisted_refusals_preserve_both_scan_statuses(self):
        for category in sorted(probe.SCANNER_CATEGORIES):
            for producer in (0, 1, -signal.SIGPIPE):
                with self.subTest(category=category, producer=producer):
                    result, _, _, _ = self.invoke(
                        [
                            {},
                            {},
                            {"code": producer, "errors": SENSITIVE.encode()},
                            {
                                "code": 1,
                                "output": probe.REFUSAL,
                                "errors": ("archive-scan:" + category + "\n").encode(),
                            },
                        ]
                    )
                    self.assertTrue(result["authenticated"])
                    self.assertEqual(result["scanner_producer_exit"], producer)
                    self.assertEqual(result["scanner_exit"], 1)
                    self.assertEqual(result["scanner_category"], category)

    def test_malformed_or_oversized_protocol_is_unknown_never_accepted(self):
        cases = [
            (0, b"", b""),
            (0, b'{"external_tree_absent":1}\n', b""),
            (0, b'{"external_tree_absent":"true"}\n', b""),
            (0, b'{"external_tree_absent":true,"extra":false}\n', b""),
            (0, b'{"external_tree_absent":false,"external_tree_absent":true}\n', b""),
            (0, b'{"external_tree_absent": true}\n', b""),
            (0, probe.SUCCESS.rstrip(), b""),
            (0, probe.SUCCESS + SENSITIVE.encode(), b""),
            (0, probe.SUCCESS, SENSITIVE.encode()),
            (0, probe.SUCCESS, b"archive-scan:forbidden-path\n"),
            (0, probe.REFUSAL, b"archive-scan:forbidden-path\n"),
            (1, probe.SUCCESS, b"archive-scan:forbidden-path\n"),
            (2, probe.REFUSAL, b"archive-scan:forbidden-path\n"),
            (1, probe.REFUSAL, b"archive-scan:" + SENSITIVE.encode() + b"\n"),
            (1, probe.REFUSAL, b"archive-scan:capture-overflow\n"),
            (1, probe.REFUSAL, b"archive-scan:forbidden-path"),
            (1, probe.REFUSAL, b"archive-scan:forbidden-path\n" + SENSITIVE.encode()),
            (1, probe.REFUSAL, b"prefix archive-scan:forbidden-path\n"),
            (
                1,
                probe.REFUSAL,
                b"archive-scan:forbidden-path\narchive-scan:input-error\n",
            ),
            (1, probe.REFUSAL, b"\xff"),
            (0, b"x" * (probe.PROTOCOL_BYTES * 4), b""),
            (1, probe.REFUSAL, b"x" * (probe.PROTOCOL_BYTES * 4)),
        ]
        for index, (code, response, diagnostic) in enumerate(cases):
            with self.subTest(case=index):
                result, _, _, _ = self.invoke(
                    [
                        {},
                        {},
                        {},
                        {"code": code, "output": response, "errors": diagnostic},
                    ]
                )
                self.assertTrue(result["authenticated"])
                self.assertEqual(
                    result["scanner_category"],
                    "capture-overflow"
                    if max(len(response), len(diagnostic)) >= probe.PROTOCOL_BYTES
                    else "unknown",
                )

    def test_authentication_capture_overflow_prevents_scan_even_with_zero_exits(self):
        for specifications in (
            [{"errors": b"x" * probe.PROTOCOL_BYTES}, {}],
            [{}, {"output": b"x" * probe.PROTOCOL_BYTES}],
            [{}, {"errors": b"x" * probe.PROTOCOL_BYTES}],
            [{"code": -signal.SIGXFSZ}, {}],
        ):
            with self.subTest(stage=specifications):
                result, calls, _, _ = self.invoke(specifications)
                self.assertFalse(result["authenticated"])
                self.assertEqual(result["scanner_category"], "capture-overflow")
                self.assertEqual(len(calls), 2)
                self.assertIsNone(result["scanner_producer_exit"])
                self.assertIsNone(result["scanner_exit"])

    def test_scan_capture_overflow_wins_over_original_refusal_category(self):
        result, _, _, _ = self.invoke(
            [
                {},
                {},
                {"errors": b"x" * probe.PROTOCOL_BYTES},
                {
                    "code": 1,
                    "output": probe.REFUSAL,
                    "errors": b"archive-scan:forbidden-path\n",
                },
            ]
        )
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["scanner_producer_exit"], 0)
        self.assertEqual(result["scanner_exit"], 1)
        self.assertEqual(result["scanner_category"], "capture-overflow")

    def test_scan_producer_failure_cannot_convert_authentication_into_acceptance(self):
        result, _, _, _ = self.invoke([{}, {}, {"code": 1}, {"output": probe.SUCCESS}])
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["scanner_producer_exit"], 1)
        self.assertEqual(result["scanner_exit"], 0)
        self.assertEqual(result["scanner_category"], "unknown")

    def test_spawn_failures_redact_output_and_stop_only_started_children(self):
        for index in range(4):
            with self.subTest(stage=index):
                specifications = [{}, {}, {}, {"output": probe.SUCCESS}]
                specifications[index] = {"spawn_error": True}
                if index % 2:
                    specifications[index - 1] = {"code": None}
                result, calls, children, signals = self.invoke(specifications)
                self.assertEqual(len(calls), index + 1)
                self.assertEqual(len(children), index)
                self.assertEqual(result["authenticated"], index >= 2)
                self.assertEqual(result["scanner_category"], "unknown")
                self.assertEqual(
                    signals,
                    [(children[-1].pid, signal.SIGTERM)] if index % 2 else [],
                )

    def test_authentication_timeout_terms_then_kills_only_owned_live_groups(self):
        result, calls, children, signals = self.invoke(
            [{"code": 0}, {"code": None, "term_stops": False}],
            timeout=0.05,
            clock=FakeClock(),
        )
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["authenticated"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["authentication_exit"], 0)
        self.assertEqual(
            signals,
            [(children[1].pid, signal.SIGTERM), (children[1].pid, signal.SIGKILL)],
        )

    def test_scanner_timeout_preserves_independent_auth_and_primary_refusal(self):
        result, _, children, signals = self.invoke(
            [
                {},
                {},
                {"code": None},
                {
                    "code": 1,
                    "output": probe.REFUSAL,
                    "errors": b"archive-scan:resource-limit\n",
                },
            ],
            timeout=0.1,
            clock=FakeClock(),
        )
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["scanner_producer_exit"], -signal.SIGTERM)
        self.assertEqual(result["scanner_exit"], 1)
        self.assertEqual(result["scanner_category"], "resource-limit")
        self.assertEqual(signals, [(children[2].pid, signal.SIGTERM)])

    def test_invalid_identity_mode_and_timeout_never_spawn_or_open(self):
        for private, mode, timeout in (
            (b"AGE-PLUGIN-" + SENSITIVE.encode(), "tar", 1),
            (PRIVATE * 2, "tar", 1),
            (b"#" * 16385, "tar", 1),
            (PRIVATE.decode(), "tar", 1),
            (PRIVATE, "invalid", 1),
            (PRIVATE, "tar", 0),
            (PRIVATE, "tar", -1),
            (PRIVATE, "tar", float("inf")),
            (PRIVATE, "tar", float("nan")),
            (PRIVATE, "tar", True),
        ):
            with self.subTest(mode=mode, timeout=timeout):
                with (
                    patch.object(probe.os, "open") as opener,
                    patch.object(probe.subprocess, "Popen") as popen,
                ):
                    result = probe.probe_component(
                        crypto, "unused", private, SCANNER, mode, timeout
                    )
                opener.assert_not_called()
                popen.assert_not_called()
                self.assertEqual(set(result), RESULT_KEYS)
                self.assertFalse(result["authenticated"])
                self.assertEqual(result["scanner_category"], "unknown")

    def test_input_open_refusal_is_fixed_and_does_not_spawn(self):
        for failure in (OSError(SENSITIVE), ValueError(SENSITIVE)):
            with self.subTest(failure=type(failure).__name__):
                with (
                    patch.object(probe.os, "open", side_effect=failure),
                    patch.object(probe.subprocess, "Popen") as popen,
                ):
                    result = probe.probe_component(
                        crypto, "unused", PRIVATE, SCANNER, "tar"
                    )
                popen.assert_not_called()
                self.assertEqual(result["scanner_category"], "unknown")
                self.assertNotIn(SENSITIVE, json.dumps(result))


class NativeCaptureLimitTests(unittest.TestCase):
    def test_kernel_caps_each_capture_while_plaintext_pipe_can_exceed_limit(self):
        for target in ("producer-errors", "consumer-output", "consumer-errors"):
            with self.subTest(target=target):
                sizes = []

                @contextlib.contextmanager
                def memory_file():
                    with crypto.memory_file() as stream:
                        try:
                            yield stream
                        finally:
                            sizes.append(os.fstat(stream.fileno()).st_size)

                limits = (
                    "import os,resource\n"
                    "if resource.getrlimit(resource.RLIMIT_CORE)!=(0,0): os._exit(9)\n"
                    "if resource.getrlimit(resource.RLIMIT_FSIZE)!=(4096,4096): os._exit(9)\n"
                )
                source = limits + (
                    "os.write(2,b'x'*(1024*1024))\n"
                    if target == "producer-errors"
                    else "os.write(1,b'p'*(1024*1024))\n"
                )
                sink = limits + (
                    "total=0\n"
                    "while True:\n"
                    "    block=os.read(0,65536)\n"
                    "    if not block: break\n"
                    "    total+=len(block)\n"
                )
                if target != "producer-errors":
                    # The 1 MiB producer pipe is deliberately larger than the
                    # file limit; the bounded sink must receive it completely.
                    sink += "if total!=1024*1024: os._exit(7)\n"
                    descriptor = 1 if target == "consumer-output" else 2
                    sink += f"os.write({descriptor},b'x'*(1024*1024))\n"
                output, errors = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                ):
                    value = probe._pipeline(
                        SimpleNamespace(
                            memory_file=memory_file, environment=crypto.environment
                        ),
                        [sys.executable, "-I", "-B", "-c", source],
                        [sys.executable, "-I", "-B", "-c", sink],
                        (),
                        time.monotonic() + 10,
                    )
                codes, complete, expired, response, diagnostic_text, overflow = value
                self.assertEqual(output.getvalue() + errors.getvalue(), "")
                self.assertTrue(complete)
                self.assertFalse(expired)
                self.assertTrue(overflow)
                self.assertEqual(len(sizes), 3)
                self.assertTrue(all(size <= probe.PROTOCOL_BYTES for size in sizes))
                self.assertIn(probe.PROTOCOL_BYTES, sizes)
                self.assertLessEqual(len(response), probe.PROTOCOL_BYTES)
                self.assertLessEqual(len(diagnostic_text), probe.PROTOCOL_BYTES)
                self.assertEqual(probe._category(*value), "capture-overflow")
                self.assertNotIn(9, codes, "child-resource-limits-were-not-applied")
                if target != "producer-errors":
                    self.assertEqual(codes[0], 0)
                    self.assertNotEqual(codes[1], 7, "plaintext-pipe-was-truncated")


def make_tar(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    return output.getvalue()


def binary_xattr_layer(value):
    body = b"SCHILY.xattr.security.capability=" + value + b"\n"
    length = len(body) + 2
    while len(str(length)) + 1 + len(body) != length:
        length = len(str(length)) + 1 + len(body)
    payload = str(length).encode() + b" " + body
    member = tarfile.TarInfo("synthetic-pax")
    member.type, member.size = tarfile.XHDTYPE, len(payload)
    return (
        member.tobuf(format=tarfile.USTAR_FORMAT)
        + payload
        + bytes((-len(payload)) % 512)
        + make_tar([("safe", b"safe")])
    )


def make_oci(layer):
    entries = [("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')]

    def blob(payload, media):
        digest = hashlib.sha256(payload).hexdigest()
        entries.append(("blobs/sha256/" + digest, payload))
        return {"mediaType": media, "digest": "sha256:" + digest, "size": len(payload)}

    config = blob(
        b'{"architecture":"amd64","os":"linux"}',
        "application/vnd.oci.image.config.v1+json",
    )
    content = blob(
        gzip.compress(layer, mtime=0), "application/vnd.oci.image.layer.v1.tar+gzip"
    )
    manifest = json.dumps(
        {"schemaVersion": 2, "config": config, "layers": [content]},
        separators=(",", ":"),
    ).encode()
    root = blob(manifest, "application/vnd.oci.image.manifest.v1+json")
    entries.append(
        ("index.json", json.dumps({"schemaVersion": 2, "manifests": [root]}).encode())
    )
    return make_tar(entries)


@unittest.skipUnless(
    shutil.which("age") and shutil.which("age-keygen"), "native age tools unavailable"
)
class NativeAgeProbeTests(unittest.TestCase):
    def test_reason_observation_repeats_original_refusal_without_accepting_binary_metadata(
        self,
    ):
        layer = binary_xattr_layer(b"\x00\x00\x00\x01")
        path = self.encrypt(make_oci(layer))
        original = path.read_bytes()
        baseline = probe.probe_component(
            crypto, path, self.private, ORIGINAL_SCANNER, "oci", timeout=15
        )
        detail = probe.probe_component(
            crypto,
            path,
            self.private,
            ORIGINAL_SCANNER,
            "oci",
            timeout=15,
            pax_reason=True,
        )
        self.assertEqual(detail["pax_reason"], "xattr-value-nul")
        self.assertEqual({key: detail[key] for key in RESULT_KEYS}, baseline)
        self.assertTrue(detail["authenticated"])
        self.assertEqual(detail["scanner_category"], "malformed-pax")
        corrected = probe.probe_component(
            crypto, path, self.private, SCANNER, "oci", timeout=15
        )
        self.assertTrue(corrected["authenticated"])
        self.assertEqual(corrected["scanner_producer_exit"], 0)
        self.assertEqual(corrected["scanner_exit"], 0)
        self.assertEqual(corrected["scanner_category"], "ok")
        self.assertEqual(path.read_bytes(), original)

    def test_corrected_encrypted_metadata_retains_raw_sentinel_exclusion(self):
        for value, category in (
            (b"\x00\xff\n=\r", "ok"),
            (b"\x00\xffSHARING_NEVER_READ_CONTENT_\x00", "forbidden-content"),
        ):
            path = self.encrypt(make_oci(binary_xattr_layer(value)))
            before = path.read_bytes()
            result = probe.probe_component(
                crypto, path, self.private, SCANNER, "oci", timeout=15
            )
            self.assertTrue(result["authenticated"])
            self.assertEqual(result["scanner_producer_exit"], 0)
            self.assertEqual(result["scanner_exit"], int(category != "ok"))
            self.assertEqual(result["scanner_category"], category)
            self.assertEqual(path.read_bytes(), before)

    @classmethod
    def setUpClass(cls):
        cls.private = crypto.small(["age-keygen"])
        cls.recipient = (
            crypto.small(["age-keygen", "-y"], data=cls.private).decode().strip()
        )
        cls.wrong_private = crypto.small(["age-keygen"])

    def setUp(self):
        preferred = Path("/tmp/opencode")
        temporary = tempfile.TemporaryDirectory(
            prefix="sharing-probe-age-", dir=preferred if preferred.is_dir() else None
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.component_number = 0
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def encrypt(self, source):
        self.component_number += 1
        path = self.root / ("component-" + str(self.component_number) + ".age")
        crypto.encrypt_bytes(source, self.recipient, path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        return path

    def invoke(self, path, mode, *, private=None):
        output, errors = io.StringIO(), io.StringIO()
        before = (path.stat().st_mode, path.read_bytes())
        original_popen = subprocess.Popen
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            patch.object(probe.subprocess, "Popen", wraps=original_popen) as popen,
        ):
            result = probe.probe_component(
                crypto,
                path,
                self.private if private is None else private,
                SCANNER,
                mode,
                timeout=15,
            )
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertEqual((path.stat().st_mode, path.read_bytes()), before)
        self.assertEqual(set(result), RESULT_KEYS)
        self.assertFalse(result["timed_out"])
        return result, popen.call_count

    def test_authenticated_tar_and_compressed_oci_layer_use_original_scanner(self):
        tar = make_tar(
            [("history/native.json", b"intentionally disclosed text/image data")]
        )
        for mode, source in (("tar", tar), ("oci", make_oci(tar))):
            with self.subTest(mode=mode):
                result, count = self.invoke(self.encrypt(source), mode)
                self.assertTrue(result["authenticated"])
                self.assertEqual(result["authentication_exit"], 0)
                self.assertEqual(result["scanner_producer_exit"], 0)
                self.assertEqual(result["scanner_exit"], 0)
                self.assertEqual(result["scanner_category"], "ok")
                self.assertEqual(count, 4)

    def test_authenticated_scanner_refusal_is_not_an_authentication_failure(self):
        for mode, source, category in (
            ("tar", make_tar([("share-input-a/private", b"safe")]), "forbidden-path"),
            (
                "tar",
                make_tar(
                    [
                        (
                            "history/native",
                            b"SHARING_NEVER_READ_CONTENT_" + SENSITIVE.encode(),
                        )
                    ]
                ),
                "forbidden-content",
            ),
            ("tar", b"not an archive", "malformed-tar"),
            (
                "oci",
                make_oci(make_tar([("safe", b"SHARING_NEVER_READ_CONTENT_")])),
                "forbidden-content",
            ),
        ):
            with self.subTest(mode=mode, category=category):
                result, count = self.invoke(self.encrypt(source), mode)
                self.assertTrue(result["authenticated"])
                self.assertEqual(result["authentication_exit"], 0)
                self.assertEqual(result["scanner_producer_exit"], 0)
                self.assertEqual(result["scanner_exit"], 1)
                self.assertEqual(result["scanner_category"], category)
                self.assertEqual(count, 4)

    def test_bad_identity_and_tail_authentication_failure_skip_scanner(self):
        for failure in ("wrong-identity", "corrupt-tail", "truncated-tail"):
            with self.subTest(failure=failure):
                # Multiple age chunks can produce valid plaintext before the
                # final authentication failure. Only the EOF sink may see it.
                path = self.encrypt(make_tar([("safe", b"s" * (128 * 1024 + 57))]))
                if failure != "wrong-identity":
                    encrypted = path.read_bytes()
                    path.write_bytes(
                        encrypted[:-1] + bytes([encrypted[-1] ^ 1])
                        if failure == "corrupt-tail"
                        else encrypted[:-8]
                    )
                result, count = self.invoke(
                    path,
                    "tar",
                    private=self.wrong_private if failure == "wrong-identity" else None,
                )
                self.assertFalse(result["authenticated"])
                self.assertNotEqual(result["authentication_exit"], 0)
                self.assertIsNone(result["scanner_producer_exit"])
                self.assertIsNone(result["scanner_exit"])
                self.assertEqual(result["scanner_category"], "unknown")
                self.assertEqual(count, 2)

    def test_ciphertext_symlink_is_refused_without_starting_children(self):
        path = self.encrypt(make_tar([]))
        alias = self.root / "alias.age"
        alias.symlink_to(path)
        with patch.object(probe.subprocess, "Popen") as popen:
            result = probe.probe_component(crypto, alias, self.private, SCANNER, "tar")
        popen.assert_not_called()
        self.assertFalse(result["authenticated"])
        self.assertIsNone(result["authentication_exit"])
        self.assertEqual(result["scanner_category"], "unknown")

    def inspect_full_envelope(self, components):
        """Use real envelope/manifest/probe paths, not the host-only main.

        Sole adapter patch: load_key decrypts the *real encrypted key envelope*
        with a native identity held in memory. This avoids production load_key's
        requirement for a disk identity file; its identity-path validation is
        explicitly not covered. No opened/manifest/component/probe is mocked.
        """
        self.assertEqual(set(components), set(diagnostic.COMPONENTS))
        directory = self.root / "encrypted-components"
        directory.mkdir(mode=0o700)
        # The current production API is generate_key, not make_key. Its
        # backup-info preflight requires no engine and is left unmodified.
        private, recipient = crypto.generate_key(
            [self.recipient], directory / "key.age"
        )
        for name, plaintext in components.items():
            crypto.encrypt_bytes(plaintext, recipient, directory / name)
        source = {
            "id": "a" * 32,
            "project": "/synthetic/source-project",
            "container_project": "/synthetic/in-container-project",
        }
        source_identity = {"commit": "b" * 40}
        metadata = {name: crypto.digest(directory / name) for name in components}
        manifest = {
            "format": "oc-box-backup",
            "format_version": 1,
            "kind": "full",
            "source_workspace_id": source["id"],
            "project": source["project"],
            "container_project": source["container_project"],
            "configuration": {
                "distro": "synthetic",
                "home": "/private/oc-box-home",
                "devices": [],
                "podman_args": [],
                "ready": True,
                "cpus": "1",
                "memory": "256m",
                "pids": 64,
                "credentials": {"mode": "env", "env": [], "vault": []},
            },
            "architecture": "x86_64",
            "created_at": "2026-01-01T00:00:00Z",
            "sessions": {"sessions": [], "last": None},
            "image_id": "sha256:" + "e" * 64,
            "expanded_byte_limit": 1024 * 1024,
            "oc_box": {"commit": source_identity["commit"], "dirty": False},
            "components": metadata,
        }
        crypto.encrypt_bytes(
            json.dumps(manifest, separators=(",", ":")).encode(),
            recipient,
            directory / "manifest.age",
        )
        archive = self.root / "synthetic-full.ocbox"
        descriptor = os.open(
            archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        with os.fdopen(descriptor, "wb") as destination:
            crypto.pack(directory, destination)
        watched = [archive, *sorted(directory.iterdir())]

        def snapshot():
            return {
                path: (
                    path.read_bytes(),
                    stat.S_IMODE(path.stat().st_mode),
                    crypto.digest(path),
                )
                for path in watched
            }

        before = snapshot()
        self.assertTrue(all(value[1] == 0o600 for value in before.values()))
        self.assertEqual(
            set(path.name for path in directory.iterdir()),
            {*components, "key.age", "manifest.age"},
        )
        # This is only an unused selector, never an identity file on disk.
        identity_selector = self.root / "unused-native-identity-selector"
        self.assertFalse(identity_selector.exists())
        loaded = SimpleNamespace(
            fixture=SimpleNamespace(crypto=crypto, backups=backups),
            identity=source_identity,
            box=SimpleNamespace(
                DISTROS={"synthetic": None},
                container_project=lambda record: record["container_project"],
                valid_session_id=lambda value: value == "ses_synthetic",
            ),
        )
        scanned = []

        def bounded_probe(actual_crypto, path, actual_private, scanner, mode):
            self.assertIs(actual_crypto, crypto)
            self.assertTrue(actual_private == private)
            self.assertEqual(scanner, SCANNER)
            self.assertEqual(mode, "oci" if path.name == "image.age" else "tar")
            measured = crypto.digest(path)
            unchanged = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            value = probe.probe_component(
                actual_crypto, path, actual_private, scanner, mode, timeout=15
            )
            self.assertEqual(crypto.digest(path), measured)
            self.assertEqual(
                (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), unchanged
            )
            scanned.append((path.name, measured))
            return value

        def load_native_key(encrypted_key, identities):
            self.assertEqual(identities, [identity_selector])
            self.assertEqual(encrypted_key.name, "key.age")
            self.assertEqual(
                crypto.digest(encrypted_key), before[directory / "key.age"][2]
            )
            return crypto.decrypt_bytes(encrypted_key, self.private)

        output, errors = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
            patch.object(crypto, "load_key", side_effect=load_native_key) as load_key,
        ):
            # Assert the real opened tuple and full manifest before exercising
            # the diagnostic's independent envelope open and component scans.
            with crypto.opened(archive, [identity_selector]) as opened:
                self.assertIsInstance(opened, tuple)
                self.assertEqual(len(opened), 3)
                actual_manifest, encrypted, actual_private = opened
                self.assertEqual(actual_manifest, manifest)
                self.assertIsInstance(encrypted, Path)
                self.assertTrue(actual_private == private)
                backups.validate_manifest(loaded.box, actual_manifest)
                for name, expected in metadata.items():
                    self.assertEqual(crypto.digest(encrypted / name), expected)
                    self.assertEqual(
                        stat.S_IMODE((encrypted / name).stat().st_mode), 0o600
                    )
            results = diagnostic.inspect_archive(
                loaded,
                archive,
                identity_selector,
                SCANNER,
                source,
                SimpleNamespace(probe_component=bounded_probe),
            )
        self.assertEqual(load_key.call_count, 2)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertEqual(snapshot(), before)
        self.assertFalse(identity_selector.exists())
        self.assertEqual(
            [name for name, measured in scanned], list(diagnostic.COMPONENTS)
        )
        self.assertEqual(dict(scanned), metadata)
        self.assertEqual(set(results), set(components))
        for name, value in results.items():
            self.assertEqual(
                set(value), RESULT_KEYS | {"ciphertext", "ciphertext_unchanged"}
            )
            self.assertTrue(value["authenticated"])
            self.assertEqual(value["authentication_exit"], 0)
            self.assertEqual(value["scanner_producer_exit"], 0)
            self.assertFalse(value["timed_out"])
            self.assertEqual(value["ciphertext"], metadata[name])
            self.assertTrue(value["ciphertext_unchanged"])
        return results

    def test_real_full_envelope_metadata_and_all_component_categories(self):
        safe = make_tar([("history/native.json", b"intentionally disclosed bytes")])
        results = self.inspect_full_envelope(
            {
                "project.age": safe,
                "home.age": safe,
                "registry.age": make_tar([("registry.json", b"{}")]),
                "image.age": make_oci(safe),
            }
        )
        self.assertEqual(
            {name: value["scanner_category"] for name, value in results.items()},
            {name: "ok" for name in diagnostic.COMPONENTS},
        )
        self.assertTrue(all(value["scanner_exit"] == 0 for value in results.values()))

    def test_real_full_envelope_authenticated_refusals_do_not_abort_diagnostic(self):
        results = self.inspect_full_envelope(
            {
                "project.age": make_tar([("safe", b"safe")]),
                "home.age": b"not an archive",
                "registry.age": make_tar(
                    [("registry.json", b"SHARING_NEVER_READ_CONTENT_")]
                ),
                "image.age": make_oci(make_tar([("share-input-b/asset", b"safe")])),
            }
        )
        self.assertEqual(
            {name: value["scanner_category"] for name, value in results.items()},
            {
                "project.age": "ok",
                "home.age": "malformed-tar",
                "registry.age": "forbidden-content",
                "image.age": "forbidden-path",
            },
        )
        self.assertEqual(results["project.age"]["scanner_exit"], 0)
        self.assertTrue(
            all(
                results[name]["scanner_exit"] == 1
                for name in ("home.age", "registry.age", "image.age")
            )
        )


if __name__ == "__main__":
    unittest.main()
