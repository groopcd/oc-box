"""Read-only, two-pass age authentication and selected archive-scanner probe.

The caller owns retained-fixture, source/hash, and private-path validation. This
helper never opens a workspace or calls a container engine. It pins a regular
ciphertext file, authenticates it independently to an EOF sink, then streams a
second decryption to the caller's verified scanner. Plaintext is never staged.
Only fixed categories and child exit codes cross this module's boundary.
"""

import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time


PROTOCOL_BYTES = 4096
SUCCESS = b'{"external_tree_absent":true}\n'
REFUSAL = b'{"external_tree_absent":false}\n'
SCANNER_CATEGORIES = frozenset(
    (
        "forbidden-path",
        "forbidden-content",
        "resource-limit",
        "malformed-tar",
        "malformed-pax",
        "unsupported-tar",
        "malformed-compression",
        "malformed-json",
        "malformed-oci",
        "unsupported-oci",
        "unsupported-compression",
        "unsupported-blob",
        "unexpected-oci-entry",
        "invalid-mode",
        "malformed-input",
        "input-error",
        "interrupted",
    )
)
PAX_REASONS = (
    "length-prefix",
    "record-length",
    "delimiter",
    "key-nul",
    "key-utf8",
    "xattr-value-nul",
    "xattr-value-utf8",
    "text-value-nul",
    "text-value-utf8",
    "size",
    "owner",
    "timestamp",
    "outside-pax-fields",
    "not-observed",
)
# Bounded raw reads, no parsing/output, and no early exit before input EOF.
EOF_SINK = "import os\nwhile os.read(0, 65536):\n    pass\n"
# Apply kernel write-time limits before exec, without preexec_fn in a possibly
# threaded caller. RLIMIT_FSIZE caps anonymous regular-file captures, not the
# producer's plaintext pipe. Identity/ciphertext descriptors are read inputs.
EXEC_LIMIT = (
    "import os,resource,sys\n"
    "limit=int(sys.argv[1])\n"
    "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE,(limit,limit))\n"
    "os.execvpe(sys.argv[2],sys.argv[2:],os.environ)\n"
)


def _limited(command):
    return [
        sys.executable,
        "-I",
        "-B",
        "-c",
        EXEC_LIMIT,
        str(PROTOCOL_BYTES),
        *command,
    ]


def _stop(processes):
    """Stop only our still-running new-session children, with bounded waits."""
    running = [process for process in processes if process.poll() is None]
    for process in running:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    until = time.monotonic() + 3
    for process in running:
        try:
            process.wait(timeout=max(0, until - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    running = [process for process in running if process.poll() is None]
    for process in running:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    until = time.monotonic() + 3
    for process in running:
        try:
            process.wait(timeout=max(0, until - time.monotonic()))
        except subprocess.TimeoutExpired:
            # Even an uninterruptible child must not make diagnostics unbounded.
            pass


def _pipeline(crypto, first, second, descriptors, deadline):
    processes = []
    timed_out = False
    completed = False
    with (
        crypto.memory_file() as producer_errors,
        crypto.memory_file() as consumer_errors,
        crypto.memory_file() as output,
    ):
        try:
            if time.monotonic() >= deadline:
                timed_out = True
            else:
                producer = subprocess.Popen(
                    _limited(first),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=producer_errors,
                    env=crypto.environment(),
                    pass_fds=descriptors,
                    start_new_session=True,
                )
                processes.append(producer)
                try:
                    consumer = subprocess.Popen(
                        _limited(second),
                        stdin=producer.stdout,
                        stdout=output,
                        stderr=consumer_errors,
                        env=crypto.environment(),
                        start_new_session=True,
                    )
                    processes.append(consumer)
                finally:
                    producer.stdout.close()
                # Do not stop at the first nonzero status: retain both the
                # original scanner refusal and a subsequent age BrokenPipe.
                while True:
                    if all(process.poll() is not None for process in processes):
                        completed = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    time.sleep(min(0.02, remaining))
        except (OSError, ValueError, subprocess.SubprocessError):
            # Missing executables and spawn/wait errors have no text boundary.
            pass
        finally:
            _stop(processes)
        codes = [process.poll() for process in processes]
        codes.extend([None] * (2 - len(codes)))
        capture_overflow = (
            any(
                os.fstat(stream.fileno()).st_size >= PROTOCOL_BYTES
                for stream in (producer_errors, consumer_errors, output)
            )
            or -signal.SIGXFSZ in codes
        )
        # Never read arbitrary tool output in full, even from anonymous memory.
        output.seek(0)
        response = output.read(PROTOCOL_BYTES + 1)
        consumer_errors.seek(0)
        diagnostic = consumer_errors.read(PROTOCOL_BYTES + 1)
    return codes, completed, timed_out, response, diagnostic, capture_overflow


def _category(codes, completed, timed_out, response, diagnostic, capture_overflow):
    if capture_overflow:
        return "capture-overflow"
    if (
        completed
        and not timed_out
        and codes == [0, 0]
        and response == SUCCESS
        and diagnostic == b""
    ):
        return "ok"
    # A completed refusal remains useful even if its producer subsequently
    # breaks its pipe or times out. It can never become an exclusion success.
    if codes[1] == 1 and response == REFUSAL:
        for category in SCANNER_CATEGORIES:
            if diagnostic == ("archive-scan:" + category + "\n").encode("ascii"):
                return category
    return "unknown"


def probe_component(
    crypto, path, private, scanner, mode, timeout=3600, *, pax_reason=False
):
    """Return fixed JSON-safe diagnostics without exposing native tool output.

    ``crypto`` is the source-pinned candidate's backup_crypto module. ``private``
    is its already-decrypted native age identity, never an identity pathname.
    ``scanner`` is the caller's verified original or explicit comparison script.
    PAX reason observation remains restricted to the unchanged original. Success of the
    scanner is represented only by ``scanner_category == 'ok'``; authentication
    alone does not establish archive exclusion. ``timeout`` bounds both passes
    together, with at most two additional three-second TERM/KILL cleanup waits.
    Each capture has a kernel-enforced 4096-byte write limit; reaching it is a
    conservative ``capture-overflow`` refusal, including on authentication.

    The pinned fd prevents pathname substitution between passes, not in-place
    ciphertext mutation: the caller must retain its hash/private-file checks.
    """
    result = {
        "authenticated": False,
        "authentication_exit": None,
        "scanner_producer_exit": None,
        "scanner_exit": None,
        "scanner_category": "unknown",
        "timed_out": False,
    }
    if pax_reason is True:
        result["pax_reason"] = "not-observed"
    if (
        mode not in ("tar", "oci")
        or type(pax_reason) is not bool
        or not isinstance(private, bytes)
        or len(private) > 16384
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        return result
    lines = [
        line.strip()
        for line in private.splitlines()
        if line.strip() and not line.startswith(b"#")
    ]
    if len(lines) != 1 or not re.fullmatch(b"AGE-SECRET-KEY-1[0-9A-Z]{58}", lines[0]):
        return result
    deadline = time.monotonic() + timeout
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return result
            with crypto.memory_file(private) as identity:
                first = [
                    "age",
                    "--decrypt",
                    "--identity",
                    f"/proc/self/fd/{identity.fileno()}",
                    f"/proc/self/fd/{source.fileno()}",
                ]
                descriptors = (identity.fileno(), source.fileno())
                codes, complete, expired, _, _, overflow = _pipeline(
                    crypto,
                    first,
                    [sys.executable, "-I", "-B", "-c", EOF_SINK],
                    descriptors,
                    deadline,
                )
                result["authentication_exit"] = codes[0]
                result["timed_out"] = expired
                result["authenticated"] = (
                    complete and not expired and not overflow and codes == [0, 0]
                )
                if overflow:
                    result["scanner_category"] = "capture-overflow"
                if not result["authenticated"]:
                    return result
                source.seek(0)
                identity.seek(0)
                consumer = [sys.executable, "-I", "-B", os.fspath(scanner), mode]
                if pax_reason:
                    consumer = [
                        sys.executable,
                        "-I",
                        "-B",
                        str(Path(__file__).with_name("sharing_pax_reason.py")),
                        os.fspath(scanner),
                        mode,
                    ]
                codes, complete, expired, response, diagnostic, overflow = _pipeline(
                    crypto,
                    first,
                    consumer,
                    descriptors,
                    deadline,
                )
                result["scanner_producer_exit"], result["scanner_exit"] = codes
                result["timed_out"] = expired
                observed_reason = "not-observed"
                if pax_reason:
                    original = b"archive-scan:malformed-pax\n"
                    for reason in PAX_REASONS:
                        if reason != "not-observed" and diagnostic == (
                            original + ("pax-reason:" + reason + "\n").encode("ascii")
                        ):
                            observed_reason = reason
                            diagnostic = original
                            break
                    else:
                        if diagnostic == original:
                            # A malformed-PAX observation without its required
                            # fixed detail is an incomplete worker protocol.
                            diagnostic = b""
                result["scanner_category"] = _category(
                    codes, complete, expired, response, diagnostic, overflow
                )
                if pax_reason and result["scanner_category"] == "malformed-pax":
                    result["pax_reason"] = observed_reason
    except (OSError, ValueError, subprocess.SubprocessError, crypto.BackupError):
        # Preserve any completed authentication/exit evidence, never exception
        # text (which could contain private paths, plaintext, or credentials).
        pass
    return result
