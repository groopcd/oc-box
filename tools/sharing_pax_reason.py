"""Internal stdin-only reason observer for the frozen sharing archive scanner.

Usage: python -I -B tools/sharing_pax_reason.py ORIGINAL_SCANNER_PATH oci
The original scanner remains the sole authority on acceptance and EOF handling.
Only its verified source is executed; no archive bytes are extracted or staged.
The caller must separately authenticate the producer and enforce host guards.
"""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import math
import os
import re
import stat
import sys
from types import ModuleType


ORIGINAL_SCANNER_SHA256 = (
    "455abf6daab5f3777471fc960103350ed316d224dd598c20b9e08b3f4670193c"
)
MAX_SOURCE_BYTES = 128 * 1024
SUCCESS = '{"external_tree_absent":true}\n'
REFUSAL = '{"external_tree_absent":false}\n'
SCANNER_CATEGORIES = (
    "resource-limit",
    "forbidden-path",
    "forbidden-content",
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
PAX_REASONS = (
    "record-length",
    "length-prefix",
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


def _load_scanner(path):
    """Pin a bounded regular source and execute only its exact verified bytes."""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SOURCE_BYTES:
            raise ValueError
        source = bytearray()
        while len(source) < MAX_SOURCE_BYTES:
            block = os.read(descriptor, min(65536, MAX_SOURCE_BYTES - len(source)))
            if not block:
                break
            source.extend(block)
        else:
            # Do not read an extra byte just to detect an over-budget source.
            raise ValueError
        source = bytes(source)
        if hashlib.sha256(source).hexdigest() != ORIGINAL_SCANNER_SHA256:
            raise ValueError
    finally:
        os.close(descriptor)
    module = ModuleType("_sharing_verified_archive_scan")
    exec(compile(source, "<verified-sharing-archive-scan>", "exec"), module.__dict__)
    return module


def _is_xattr_key(field):
    if any(value in field for value in (b"\0", b"\n", b"\r", b"=")):
        return False
    try:
        key = field.decode("utf-8")
    except UnicodeError:
        return False
    return key.startswith("SCHILY.xattr.") and len(key) > len("SCHILY.xattr.")


def _classify_pax(payload):
    """Observe the frozen parser's first malformed-PAX branch, never accept data.

    Called only after the actual parser already refused. The order follows its
    source, including NUL rejection before decoding either the key or value.
    """
    offset = 0
    while offset < len(payload):
        space = payload.find(b" ", offset, offset + 12)
        if space < 0 or not payload[offset:space].isdigit():
            return "length-prefix"
        length = int(payload[offset:space])
        end = offset + length
        if length < 5 or end > len(payload) or payload[end - 1 : end] != b"\n":
            return "record-length"
        field, equals, value = payload[space + 1 : end - 1].partition(b"=")
        if not equals or not field:
            return "delimiter"
        if b"\0" in field + value:
            if b"\0" in field:
                return "key-nul"
            # A value NUL wins even when the key is not valid UTF-8. Decode only
            # to distinguish a complete, clean xattr key from other fields.
            return "xattr-value-nul" if _is_xattr_key(field) else "text-value-nul"
        try:
            key = field.decode("utf-8")
        except UnicodeError:
            return "key-utf8"
        try:
            text = value.decode("utf-8")
        except UnicodeError:
            return "xattr-value-utf8" if _is_xattr_key(field) else "text-value-utf8"
        if key == "size" and not re.fullmatch(r"[0-9]{1,20}", text):
            return "size"
        if key in ("uid", "gid") and not re.fullmatch(r"[0-9]{1,20}", text):
            return "owner"
        if key in ("mtime", "atime", "ctime"):
            try:
                if not math.isfinite(float(text)):
                    raise ValueError
            except ValueError:
                return "timestamp"
        offset = end
    return "outside-pax-fields"


class _PaxObservation:
    def __init__(self):
        self.reason = "not-observed"
        self.failed = False


def _wrap_pax_fields(module):
    original = module.pax_fields
    observation = _PaxObservation()

    def observed(payload):
        try:
            return original(payload)
        except module.ScanError as exc:
            if exc.args == ("malformed-pax",) and observation.reason == "not-observed":
                try:
                    observation.reason = _classify_pax(payload)
                except (Exception, KeyboardInterrupt, SystemExit):
                    # Preserve the original exception even if observation fails;
                    # the worker then refuses with its fixed protocol failure.
                    observation.failed = True
            raise

    module.pax_fields = observed
    return observation


def _run(path, mode, stdin):
    module = _load_scanner(path)
    observation = _wrap_pax_fields(module)
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = module.main([mode], stdin=stdin, stdout=output, stderr=errors)
    if observation.failed or observation.reason not in PAX_REASONS:
        raise ValueError
    if type(code) is not int:
        raise ValueError
    if code == 0 and output.getvalue() == SUCCESS and errors.getvalue() == "":
        if observation.reason != "not-observed":
            raise ValueError
        return 0, SUCCESS, ""
    if code == 1 and output.getvalue() == REFUSAL:
        for category in SCANNER_CATEGORIES:
            diagnostic = "archive-scan:" + category + "\n"
            if errors.getvalue() == diagnostic:
                if category == "malformed-pax":
                    reason = observation.reason
                    if reason == "not-observed":
                        reason = "outside-pax-fields"
                    diagnostic += "pax-reason:" + reason + "\n"
                return 1, REFUSAL, diagnostic
    raise ValueError


def main(argv=None, *, stdin=None, stdout=None, stderr=None):
    argv = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        if len(argv) != 2:
            raise ValueError
        code, output, errors = _run(argv[0], argv[1], stdin)
    except (Exception, KeyboardInterrupt, SystemExit):
        code, output, errors = 1, REFUSAL, "archive-scan:malformed-input\n"
    stdout.write(output)
    stderr.write(errors)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
