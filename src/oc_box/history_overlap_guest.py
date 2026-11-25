"""One guarded source export behind a private READY/GO process barrier."""

import base64
from http.client import HTTPConnection
import json
import os
import re
import select
import signal
import stat
import threading
import time
from urllib.parse import quote, urlsplit


_INPUT = 16384
_REPLY = 32 * 1024 * 1024
_TOTAL = 2 * _REPLY
_CHUNK = 65536


class GuestRefusal(ValueError):
    """Fixed private protocol refusal."""


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GuestRefusal("deadline")
    return remaining


def _read(fd, count, deadline):
    if not select.select([fd], (), (), _remaining(deadline))[0]:
        raise GuestRefusal("deadline")
    data = os.read(fd, count)
    if not data:
        raise GuestRefusal("cancelled")
    return data


def _exact(fd, count, deadline):
    result = bytearray()
    while len(result) < count:
        result.extend(_read(fd, count - len(result), deadline))
    return bytes(result)


def _control_is_open(fd):
    if select.select([fd], (), (), 0)[0]:
        # EOF and unadmitted bytes both invalidate any further work. We
        # never accept a second request or a second GO for this wrapper.
        raise GuestRefusal("control")


def _control_identity(pid):
    with open(f"/proc/{pid}/stat", "rb") as stream:
        raw = stream.read(4097)
    before, end, fields = raw.rpartition(b") ")
    rest = fields.split()
    if (
        len(raw) > 4096
        or not end
        or not before.startswith(str(pid).encode() + b" (")
        or len(rest) < 20
        or not rest[19].isdigit()
        or not 1 <= int(rest[19]) < 2**64
    ):
        raise GuestRefusal("process-binding")
    return int(rest[19])


def _unique(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise GuestRefusal("frame")
    return values


def _request(payload, deadline):
    data = json.loads(payload, object_pairs_hook=_unique)
    if type(data) is not dict or set(data) not in (
        {"session", "endpoint"},
        {"directory", "endpoint"},
    ):
        raise GuestRefusal("frame")
    inventory = "directory" in data
    target = data["directory" if inventory else "session"]
    endpoint = data["endpoint"]
    if (
        type(target) is not str
        or (
            inventory
            and (
                not target.startswith("/")
                or not 1 <= len(target) <= 4096
                or any(char in target for char in "\0\r\n")
            )
        )
        or (not inventory and re.fullmatch(r"ses[A-Za-z0-9_-]{1,253}", target) is None)
        or type(endpoint) is not dict
        or set(endpoint) != {"url", "password"}
        or type(endpoint["url"]) is not str
        or type(endpoint["password"]) is not str
        or not 1 <= len(endpoint["password"]) <= 256
    ):
        raise GuestRefusal("frame")
    parsed = urlsplit(endpoint["url"])
    if (
        parsed.scheme != "http"
        or parsed.netloc != f"127.0.0.1:{parsed.port}"
        or parsed.port is None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise GuestRefusal("endpoint")
    secret = (
        "Basic "
        + base64.b64encode(("opencode:" + endpoint["password"]).encode()).decode()
    )
    remaining = _TOTAL
    pages = 0
    projection = f"/api/session/{target}/message?limit=100"
    export = f"/api/experimental/session/{target}/export"
    listing = (
        "/api/session?directory=" + quote(target, safe="") + "&order=asc&limit=100"
    )

    def approved(path):
        if inventory:
            return path == listing or path.startswith(listing + "&cursor=")
        return path.startswith(projection + "&") or path in (
            export,
            "/api/session/active",
        )

    def request(method, path, *, raw=False):
        nonlocal remaining, pages
        pages += 1
        if method != "GET" or pages > 512 or remaining <= 0 or not approved(path):
            raise GuestRefusal("request-bounds")
        conn = HTTPConnection("127.0.0.1", parsed.port, timeout=_remaining(deadline))
        try:
            conn.connect()
            stream = conn.sock
            stream.settimeout(_remaining(deadline))
            conn.request(
                "GET", path, headers={"Authorization": secret, "Connection": "close"}
            )
            stream.settimeout(_remaining(deadline))
            with conn.getresponse() as response:
                limits = response.headers.get_all("Content-Length", [])
                if (
                    response.status != 200
                    or response.headers.get_all("Content-Type") != ["application/json"]
                    or len(limits) != 1
                    or re.fullmatch(r"[1-9][0-9]{0,8}", limits[0]) is None
                    or not 1 <= int(limits[0]) <= min(remaining, _REPLY)
                    or any(
                        key in response.headers
                        for key in (
                            "Location",
                            "Set-Cookie",
                            "Set-Cookie2",
                            "Content-Encoding",
                            "Transfer-Encoding",
                        )
                    )
                ):
                    raise GuestRefusal("response-bounds")
                length = int(limits[0])
                body = bytearray()
                while len(body) < length:
                    stream.settimeout(_remaining(deadline))
                    part = response.read1(min(_CHUNK, length - len(body)))
                    if not part:
                        raise GuestRefusal("response-bounds")
                    body.extend(part)
                remaining -= length
                _remaining(deadline)
        finally:
            conn.close()
        value = json.loads(body, object_pairs_hook=_unique)
        if raw:
            return (
                value,
                b"",
            )  # use the checked representation, not an unchecked wire shortcut
        return value

    return inventory, target, request


def _wait(pidfd, maximum):
    if not select.select([pidfd], (), (), maximum)[0]:
        return None
    observed = os.waitid(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG)
    if observed is None or observed.si_code not in (
        os.CLD_EXITED,
        os.CLD_KILLED,
        os.CLD_DUMPED,
    ):
        raise GuestRefusal("cleanup-unverified")
    return observed


def _retire(pidfd):
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            signal.pidfd_send_signal(pidfd, signum)
        except ProcessLookupError:
            pass
        if _wait(pidfd, 1) is not None:
            return
    raise GuestRefusal("cleanup-unverified")


def _send(control, output, data, deadline):
    while data:
        readers, writers, _ = select.select(
            [control], [output], [], _remaining(deadline)
        )
        if readers:
            _control_is_open(control)
        if writers:
            try:
                size = os.write(output, data[:_CHUNK])
            except BlockingIOError:
                continue
            if size <= 0:
                raise GuestRefusal("output")
            data = data[size:]


def supervise(token, bridge, *, control=0, output=1, timeout=30):
    """Run one read child; the launcher still needs independent retirement proof."""
    if (
        type(token) is not str
        or re.fullmatch(r"[a-f0-9]{32}", token) is None
        or type(timeout) not in (float, int)
        or not 0 < timeout <= 60
        or threading.active_count() != 1
        or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
        or not hasattr(os, "P_PIDFD")
        or not hasattr(os, "pidfd_open")
        or not hasattr(signal, "pidfd_send_signal")
        or control == output
        or not stat.S_ISFIFO(os.fstat(control).st_mode)
        or not stat.S_ISFIFO(os.fstat(output).st_mode)
    ):
        raise GuestRefusal("unsupported")
    deadline = time.monotonic() + timeout
    size = int.from_bytes(_exact(control, 4, deadline), "big")
    if not 1 <= size <= _INPUT:
        raise GuestRefusal("frame")
    inventory, target, request = _request(_exact(control, size, deadline), deadline)
    _control_is_open(control)
    reply = os.memfd_create("oc-box-overlap-source-reply", os.MFD_CLOEXEC)
    blocked = release = None
    child = None
    pidfd = None
    retired = False
    try:
        blocked, release = os.pipe2(os.O_CLOEXEC)
        child = os.fork()
        if child == 0:
            try:
                os.close(release)
                os.close(control)
                os.close(output)
                if os.read(blocked, 1) != b"\x01":
                    os._exit(3)
                os.close(blocked)
                try:
                    if inventory:
                        body = (
                            b"I"
                            + json.dumps(
                                bridge.session_inventory(request, target),
                                separators=(",", ":"),
                            ).encode()
                        )
                    else:
                        body = b"E" + bridge._quiet_export_wire(request, target)
                except bridge.FixedError as error:
                    if str(error) == bridge.UNSETTLED:
                        body = b"U"
                    elif str(error) == bridge.CHANGED:
                        body = b"C"
                    else:
                        raise
                if not 1 <= len(body) <= _REPLY:
                    raise GuestRefusal("response-bounds")
                with os.fdopen(reply, "wb") as stream:
                    if stream.write(body) != len(body):
                        raise GuestRefusal("output")
                os._exit(0)
            except BaseException:
                os._exit(2)
        os.close(blocked)
        blocked = None
        pidfd = os.pidfd_open(child)
        os.set_blocking(output, False)
        ready = f"OC-BOX-HISTORY-READY/1 {token} {os.getpid()} {_control_identity(os.getpid())} {child} {_control_identity(child)}\n"
        _send(control, output, ready.encode("ascii"), deadline)
        go = f"OC-BOX-HISTORY-GO/1 {token}\n".encode("ascii")
        if _exact(control, len(go), deadline) != go:
            raise GuestRefusal("control")
        _control_is_open(control)
        if os.write(release, b"\x01") != 1:
            raise GuestRefusal("control")
        os.close(release)
        release = None
        readable = select.select([control, pidfd], (), (), _remaining(deadline))[0]
        if control in readable:
            _control_is_open(control)
        if pidfd not in readable:
            raise GuestRefusal("deadline")
        observed = _wait(pidfd, 0)
        retired = observed is not None
        if not retired or observed.si_code != os.CLD_EXITED or observed.si_status != 0:
            raise GuestRefusal("read-failed")
        length = os.fstat(reply).st_size
        if not 1 <= length <= _REPLY:
            raise GuestRefusal("response-bounds")
        _send(control, output, length.to_bytes(4, "big"), deadline)
        offset = 0
        while offset < length:
            part = os.pread(reply, min(_CHUNK, length - offset), offset)
            if not part:
                raise GuestRefusal("output")
            _send(control, output, part, deadline)
            offset += len(part)
        _control_is_open(control)
    finally:
        try:
            if release is not None:
                os.close(release)
            if child is not None and not retired:
                if pidfd is None:
                    # The child may be alive; no unsupported PID signal.
                    raise GuestRefusal("cleanup-unverified")
                _retire(pidfd)
        finally:
            if blocked is not None:
                os.close(blocked)
            if pidfd is not None:
                os.close(pidfd)
            os.close(reply)
