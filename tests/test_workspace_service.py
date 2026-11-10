import contextlib
import ctypes
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse
from urllib.request import urlopen

from oc_box import oc_box as box, workspace_service as service, vault


def waitable_exit(child, timeout=2):
    """Observe an owned test child without consuming its group identity anchor."""
    deadline = time.monotonic() + timeout
    while True:
        if child.returncode is not None:
            return child.returncode
        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is not None:
            return (
                result.si_status
                if result.si_code == os.CLD_EXITED
                else -result.si_status
            )
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("synthetic waitable child", timeout)
        time.sleep(0.01)


def descendant_prefix(ready):
    """Finite synthetic child retaining its leader's inherited descriptors."""
    return (
        """import json,os,signal,sys,time
signal.alarm(10)
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
read,write=os.pipe()
descendant=os.fork()
if descendant==0:
    os.close(read)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    signal.alarm(10)
    os.write(write,b'1'); os.close(write)
    time.sleep(10); os._exit(0)
os.close(write); os.read(read,1); os.close(read)
with open("""
        + repr(str(ready))
        + """,'x') as output:
    os.fchmod(output.fileno(),0o600)
    json.dump({'pid':descendant,'leader':os.getpid(),'group':os.getpgrp(),
               'stdout':os.readlink('/proc/self/fd/1'),
               'stderr':os.readlink('/proc/self/fd/2')},output)
"""
    )


def reap_descendant(pid, timeout=2):
    deadline = time.monotonic() + timeout
    while True:
        result, status = os.waitpid(pid, os.WNOHANG)
        if result == pid:
            return status
        if time.monotonic() >= deadline:
            raise AssertionError("owned synthetic descendant did not exit")
        time.sleep(0.01)


@contextlib.contextmanager
def adopted_descendants():
    # Test-process-only Linux subreaper state; restore it after exact-PID cleanup.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError("synthetic subreaper query failed")
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError("synthetic subreaper setup failed")
    owned = []
    try:
        yield owned
    finally:
        try:
            for leader, pid in owned:
                try:
                    os.waitid(
                        os.P_PID, leader.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                    )
                except ChildProcessError:
                    pass
                else:
                    try:
                        os.killpg(leader.pid, service.signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                leader.wait(timeout=2)
                try:
                    result = os.waitid(
                        os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                    )
                except ChildProcessError:
                    continue  # Already reaped by the test: never reuse its PID.
                if result is None:
                    os.kill(pid, service.signal.SIGKILL)
                reap_descendant(pid)
        finally:
            if libc.prctl(36, previous.value, 0, 0, 0) != 0:
                raise OSError("synthetic subreaper restoration failed")


class GatewayRelayTests(unittest.TestCase):
    """Real loopback sockets and the shipped proxy; no Podman/native service."""

    @contextlib.contextmanager
    def transport(self, handler, *, proxy_prefix="", launch_hook=None):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.1)
        stopping = threading.Event()
        connections, handlers, children, workers, errors = [], [], [], [], []
        real_thread, real_popen = threading.Thread, subprocess.Popen

        def accepted(stream):
            try:
                stream.settimeout(8)
                handler(stream)
            except (OSError, AssertionError) as error:
                if not stopping.is_set():
                    errors.append(type(error).__name__)
            finally:
                stream.close()

        def serve():
            while not stopping.is_set():
                try:
                    stream, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                connections.append(stream)
                thread = real_thread(target=accepted, args=(stream,), daemon=True)
                handlers.append(thread)
                thread.start()

        server = real_thread(target=serve, daemon=True)
        server.start()

        def local_relay(_box, _data, port):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", proxy_prefix + service.PROXY, str(port)],
                box.maintenance_host_env(),
            )

        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            if launch_hook:
                launch_hook(child)
            return child

        def thread_factory(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            target = kwargs.get("target")
            if getattr(target, "__name__", None) == "forward":
                workers.append(thread)
            return thread

        gateway = service.Gateway(
            box,
            {},
            {
                "url": "http://127.0.0.1:" + str(listener.getsockname()[1]),
                "session": "ses_synthetic",
            },
        )
        clients = []

        def connect():
            stream = socket.create_connection(
                ("127.0.0.1", urlparse(gateway.url).port), timeout=4
            )
            clients.append(stream)
            return stream

        try:
            with (
                patch.object(service, "relay_command", side_effect=local_relay),
                patch.object(service.subprocess, "Popen", side_effect=launch),
                patch.object(service.threading, "Thread", side_effect=thread_factory),
                gateway,
            ):
                yield SimpleNamespace(
                    gateway=gateway,
                    connect=connect,
                    children=children,
                    workers=workers,
                    errors=errors,
                )
        finally:
            stopping.set()
            listener.close()
            for stream in [*clients, *connections]:
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                stream.close()
            # Also clean old/broken implementations after a red assertion.
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
                for pipe in (child.stdin, child.stdout):
                    if pipe:
                        pipe.close()
            for thread in [server, *handlers, *workers]:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "synthetic relay thread leaked")

    @staticmethod
    def headers(stream):
        result = bytearray()
        while not result.endswith(b"\r\n\r\n"):
            chunk = stream.recv(1)
            if not chunk:
                raise OSError("incomplete synthetic headers")
            result.extend(chunk)
        return bytes(result)

    def test_real_proxy_half_close_drains_the_complete_delayed_response(self):
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 32768\r\n\r\n"
            + bytes(range(256)) * 128
        )

        def handler(stream):
            self.headers(stream)
            self.assertEqual(stream.recv(1), b"")
            time.sleep(0.05)
            stream.sendall(response)

        with self.transport(handler) as transport:
            client = transport.connect()
            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            client.shutdown(socket.SHUT_WR)
            actual = bytearray()
            while chunk := client.recv(65536):
                actual.extend(chunk)
            self.assertEqual(len(actual), len(response))
            self.assertEqual(actual, response)
            self.assertEqual(transport.errors, [])
            self.assertTrue(transport.children)
            for child in transport.children:
                self.assertEqual(waitable_exit(child), 0)

    def test_real_proxy_eight_mib_bidirectional_and_slow_reader_are_lossless(self):
        upload = bytes(range(256)) * 32768
        download = upload[::-1]
        upgrade = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        for slow in (False, True):
            with self.subTest(slow_reader=slow):
                received, sending_errors = [], []

                def handler(stream):
                    self.headers(stream)
                    stream.sendall(upgrade)
                    stream.sendall(download)
                    content = bytearray()
                    while chunk := stream.recv(65536):
                        content.extend(chunk)
                        if slow:
                            time.sleep(0.0002)
                    received.append(bytes(content))

                with self.transport(handler) as transport:
                    client = transport.connect()
                    client.sendall(b"GET /upgrade HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    self.assertEqual(self.headers(client), upgrade)

                    def send():
                        try:
                            client.sendall(upload)
                            client.shutdown(socket.SHUT_WR)
                        except OSError as error:
                            sending_errors.append(type(error).__name__)

                    sender = threading.Thread(target=send, daemon=True)
                    sender.start()
                    try:
                        actual = bytearray()
                        while chunk := client.recv(4096 if slow else 65536):
                            actual.extend(chunk)
                            if slow:
                                time.sleep(0.0002)
                        sender.join(timeout=3)

                        self.assertFalse(sender.is_alive())
                        self.assertEqual(sending_errors, [])
                        self.assertEqual(len(actual), len(download))
                        self.assertEqual(
                            hashlib.sha256(actual).digest(),
                            hashlib.sha256(download).digest(),
                        )
                        deadline = time.monotonic() + 2
                        while not received and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertEqual(len(received), 1)
                        self.assertEqual(len(received[0]), len(upload))
                        self.assertEqual(
                            hashlib.sha256(received[0]).digest(),
                            hashlib.sha256(upload).digest(),
                        )
                        self.assertEqual(transport.errors, [])
                        for child in transport.children:
                            self.assertEqual(waitable_exit(child), 0)
                    finally:
                        if sender.is_alive():
                            client.shutdown(socket.SHUT_RDWR)
                        sender.join(timeout=3)

    def test_backend_half_close_keeps_the_upload_direction_open(self):
        upload = bytes(range(256)) * 512
        response = b"HTTP/1.1 200 OK\r\n\r\n" + upload[::-1]
        received = []

        def handler(stream):
            self.headers(stream)
            stream.sendall(response)
            stream.shutdown(socket.SHUT_WR)
            content = bytearray()
            while chunk := stream.recv(65536):
                content.extend(chunk)
            received.append(bytes(content))

        with self.transport(handler) as transport:
            client = transport.connect()
            client.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            actual = bytearray()
            while chunk := client.recv(65536):
                actual.extend(chunk)
            self.assertEqual(len(actual), len(response))
            self.assertEqual(actual, response)
            client.sendall(upload)
            client.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 2
            while not received and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(received), 1)
            self.assertEqual(len(received[0]), len(upload))
            self.assertEqual(received, [upload])
            self.assertEqual(transport.errors, [])

    def test_shutdown_interrupts_blocked_downloads_and_idle_headers(self):
        writing = threading.Event()

        def handler(stream):
            self.headers(stream)
            writing.set()
            stream.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (8 * 1024 * 1024))

        prefix = "import signal; signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        with self.transport(handler, proxy_prefix=prefix) as transport:
            for _ in range(4):
                client = transport.connect()
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            idle = transport.connect()
            idle.sendall(b"GET /partial")
            self.assertTrue(writing.wait(timeout=3))
            deadline = time.monotonic() + 3
            while len(transport.children) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(transport.children), 4)
            time.sleep(0.2)
            started = time.monotonic()
            transport.gateway.__exit__(None, None, None)
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertTrue(transport.workers)
            self.assertTrue(all(not thread.is_alive() for thread in transport.workers))
            self.assertTrue(
                all(child.poll() is not None for child in transport.children)
            )

    def test_partial_writes_are_lossless_and_host_queues_stay_bounded(self):
        payload = bytes(range(256)) * 1024
        response = b"HTTP/1.1 200 OK\r\n\r\n" + payload
        received, writes = [], []

        def handler(stream):
            self.headers(stream)
            content = bytearray()
            while chunk := stream.recv(65536):
                content.extend(chunk)
            received.append(bytes(content))
            stream.sendall(response)

        real_write = os.write

        def partial_write(fd, content):
            writes.append(len(content))
            return real_write(fd, content[:257])

        prefix = "import os; _write=os.write; os.write=lambda fd,data:_write(fd,data[:257])\n"
        with (
            self.transport(handler, proxy_prefix=prefix) as transport,
            patch.object(service.os, "write", side_effect=partial_write),
        ):
            client = transport.connect()
            client.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\n\r\n" + payload)
            client.shutdown(socket.SHUT_WR)
            actual = bytearray()
            while chunk := client.recv(65536):
                actual.extend(chunk)
            self.assertEqual(len(actual), len(response))
            self.assertEqual(actual, response)
            self.assertEqual(received, [payload])
            self.assertTrue(writes)
            self.assertLessEqual(max(writes), service.RELAY_BUFFER)

    def test_shutdown_waits_for_child_creation_already_owned_by_a_worker(self):
        creating, release, stopped = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        failures = []

        def launching(_child):
            creating.set()
            if not release.wait(timeout=3):
                raise OSError("synthetic launch gate timed out")

        def handler(stream):
            self.headers(stream)
            stream.recv(1)

        with self.transport(handler, launch_hook=launching) as transport:
            client = transport.connect()
            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertTrue(creating.wait(timeout=3))

            def shutdown():
                try:
                    transport.gateway.__exit__(None, None, None)
                except Exception as error:
                    failures.append(type(error).__name__)
                finally:
                    stopped.set()

            closer = threading.Thread(target=shutdown, daemon=True)
            closer.start()
            try:
                self.assertTrue(transport.gateway.closed.wait(timeout=2))
                transport.gateway.thread.join(timeout=1)
                self.assertFalse(stopped.wait(timeout=0.1))
            finally:
                release.set()
                closer.join(timeout=3)
            self.assertFalse(closer.is_alive())
            self.assertTrue(stopped.is_set())
            self.assertEqual(failures, [])
            self.assertTrue(
                all(child.poll() is not None for child in transport.children)
            )
            self.assertTrue(all(not worker.is_alive() for worker in transport.workers))

    def test_oversized_request_and_pairing_headers_refuse_without_unbounded_staging(
        self,
    ):
        for request in (True, False):
            with self.subTest(request_headers=request):

                def handler(stream):
                    self.headers(stream)
                    stream.sendall(b"HTTP/1.1 200 OK\r\nX-Fill: " + b"x" * 65536)

                with self.transport(handler) as transport:
                    client = transport.connect()
                    client.sendall(
                        b"GET / " + b"x" * (65536 - 6)
                        if request
                        else b"GET /auth/connect/synthetic HTTP/1.1\r\nHost: localhost\r\n\r\n"
                    )
                    self.assertEqual(client.recv(1), b"")
                    if request:
                        self.assertEqual(transport.children, [])

    def test_pairing_rewrite_never_changes_response_body(self):
        body = b"Location: /\r\n\x00\xff"
        response = (
            b"HTTP/1.1 302 Found\r\nLocation: /unchanged\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )

        def handler(stream):
            self.headers(stream)
            for chunk in (response[:20], response[20:]):
                stream.sendall(chunk)

        with self.transport(handler) as transport:
            client = transport.connect()
            client.sendall(
                b"GET /auth/connect/synthetic HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            actual = bytearray()
            while chunk := client.recv(65536):
                actual.extend(chunk)
            self.assertEqual(actual, response)

    def test_concurrent_shutdown_never_signals_after_worker_reaps_its_anchor(self):
        requested, reaped, release, attempted, stopped = [
            threading.Event() for _ in range(5)
        ]
        proposals, failures, children = [], [], []
        real_killpg = os.killpg

        def handler(stream):
            self.headers(stream)
            requested.set()
            stream.sendall(b"HTTP/1.1 200 OK\r\n\r\nhello")
            stream.recv(1)

        def launching(child):
            children.append(child)
            wait = child.wait

            def wait_then_pause(*args, **kwargs):
                result = wait(*args, **kwargs)
                if not reaped.is_set():
                    reaped.set()
                    if not release.wait(timeout=3):
                        raise OSError("synthetic reap gate timed out")
                return result

            child.wait = wait_then_pause

        def guarded_signal(pid, sig):
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                proposals.append((sig, False))
                return  # Never actually signal an unowned/reusable group.
            proposals.append((sig, True))
            real_killpg(pid, sig)

        with (
            patch.object(service.os, "killpg", side_effect=guarded_signal),
            self.transport(handler, launch_hook=launching) as transport,
        ):
            client = transport.connect()
            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertTrue(requested.wait(timeout=2))
            original_signal = transport.gateway.signal_child
            closer = None

            def signal_child(*args, **kwargs):
                if reaped.is_set() and threading.current_thread() is closer:
                    attempted.set()
                return original_signal(*args, **kwargs)

            def shutdown():
                try:
                    transport.gateway.__exit__(None, None, None)
                except Exception as error:
                    failures.append(type(error).__name__)
                finally:
                    stopped.set()

            with patch.object(
                transport.gateway, "signal_child", side_effect=signal_child
            ):
                closer = threading.Thread(target=shutdown, daemon=True)
                closer.start()
                try:
                    self.assertTrue(reaped.wait(timeout=2))
                    self.assertTrue(attempted.wait(timeout=2))
                    self.assertFalse(stopped.is_set())
                finally:
                    release.set()
                    closer.join(timeout=3)
            self.assertFalse(closer.is_alive())
            self.assertEqual(failures, [])
            self.assertTrue(proposals)
            self.assertTrue(all(owned for _, owned in proposals), proposals)
            self.assertTrue(all(child.returncode is not None for child in children))

    def test_lost_gateway_anchor_refuses_group_signals_and_finishes_other_cleanup(self):
        for cached in (True, False):
            with self.subTest(popen_cached_reap=cached):
                stolen = threading.Event()

                def handler(stream):
                    stream.recv(1)

                def steal(child):
                    child.stdin.close()
                    if cached:
                        child.wait(timeout=2)
                    else:
                        waitable_exit(child)
                        os.waitpid(child.pid, 0)  # ECHILD without a Popen cache.
                    stolen.set()

                with patch.object(service.os, "killpg") as kill:
                    with self.assertRaisesRegex(box.BoxError, "cleanup failed"):
                        with self.transport(handler, launch_hook=steal) as transport:
                            client = transport.connect()
                            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                            self.assertTrue(stolen.wait(timeout=2))
                    kill.assert_not_called()
                self.assertTrue(
                    all(child.stdout.closed for child in transport.children)
                )

    def test_gateway_retains_exited_and_graceful_leaders_until_descendants_stop(self):
        for exited in (False, True):
            with (
                self.subTest(already_exited=exited),
                tempfile.TemporaryDirectory(prefix="oc-box-relay-anchor-") as name,
                adopted_descendants() as owned,
            ):
                ready = Path(name) / "descendant.json"
                proposals = []
                real_killpg = os.killpg

                def handler(stream):
                    self.headers(stream)
                    stream.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                    stream.recv(1)

                def guarded_signal(pid, sig):
                    try:
                        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    except ChildProcessError:
                        proposals.append(False)
                        return
                    proposals.append(True)
                    real_killpg(pid, sig)

                with (
                    patch.object(service.os, "killpg", side_effect=guarded_signal),
                    self.transport(
                        handler, proxy_prefix=descendant_prefix(ready)
                    ) as transport,
                ):
                    client = transport.connect()
                    client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    self.headers(client)
                    evidence = json.loads(ready.read_text())
                    leader = transport.children[-1]
                    owned.append((leader, evidence["pid"]))
                    self.assertEqual(evidence["leader"], leader.pid)
                    self.assertEqual(evidence["group"], leader.pid)
                    self.assertTrue(evidence["stdout"].startswith("pipe:"))
                    if exited:
                        client.shutdown(socket.SHUT_WR)
                        self.assertEqual(waitable_exit(leader), 0)
                    transport.gateway.__exit__(None, None, None)
                    status = reap_descendant(evidence["pid"])
                    self.assertTrue(os.WIFSIGNALED(status))
                    self.assertEqual(os.WTERMSIG(status), service.signal.SIGKILL)
                    self.assertTrue(proposals)
                    self.assertTrue(all(proposals))


class OwnerCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-owner-cleanup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(service.CONNECTION.reset, service.CONNECTION.set(None))
        self.children, self.outputs, self.owners = [], [], []
        self.primary = None
        self.output_failure = False
        self.program = (
            "import json,sys; print(json.dumps({'url':'http://127.0.0.1:12345',"
            "'password':'a'*43}),flush=True); sys.stdin.read()"
        )

        @contextlib.contextmanager
        def capture(**kwargs):
            self.assertEqual(kwargs, {"private": True})
            with box.captured_output(**kwargs) as output:
                self.outputs.append(output)
                self.assertIn(
                    "memfd:oc-box-private-output",
                    os.readlink("/proc/self/fd/" + str(output.fileno())),
                )
                yield output
            if self.output_failure:
                raise OSError("private-cleanup-sentinel")

        self.box = SimpleNamespace(
            BoxError=box.BoxError,
            PACKAGE=box.PACKAGE,
            CONTAINER_OPENCODE="synthetic-unused",
            state_root=lambda: self.root / "state",
            captured_output=capture,
            exec_command=lambda *_: box.ContainerCommand(
                [sys.executable, "-B", "-c", self.program], box.maintenance_host_env()
            ),
        )
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            if self.primary is not None:
                raise self.primary
            child = real_popen(*args, **kwargs)
            self.children.append(child)
            return child

        launching = patch.object(service.subprocess, "Popen", side_effect=launch)
        launching.start()
        self.addCleanup(launching.stop)
        self.addCleanup(self.finish_owned_resources)

    def owner(self):
        owner = service.Owner(
            self.box,
            self.root / "workspace.json",
            {"id": "synthetic-" + str(len(self.owners))},
        )
        self.owners.append(owner)
        return owner

    def finish_owned_resources(self):
        # A red implementation must not leave the synthetic child/context behind.
        self.output_failure = False
        for owner in self.owners:
            thread = getattr(owner, "thread", None)
            if thread is not None and thread.ident is None:
                owner.thread = None
            try:
                owner.shutdown()
            except (AttributeError, OSError, RuntimeError, box.BoxError):
                pass
            connection = getattr(owner, "connection", None)
            token = getattr(owner, "token", None)
            if token is not None and service.live_connection() is connection:
                service.CONNECTION.reset(token)
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            for pipe in (child.stdin, child.stdout):
                if pipe:
                    pipe.close()
        for output in self.outputs:
            output.close()

    def assert_released(self, owner, previous=None):
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertTrue(all(output.closed for output in self.outputs))
        self.assertFalse(owner.address.exists())
        self.assertIs(service.live_connection(), previous)
        self.assertIsNone(getattr(owner, "connection", None))

    def test_thread_start_failure_preserves_primary_and_releases_private_resources(
        self,
    ):
        owner = self.owner()
        primary = RuntimeError("synthetic thread-start refusal")
        with (
            patch.object(service.threading.Thread, "start", side_effect=primary),
            self.assertRaises(RuntimeError) as raised,
        ):
            owner.__enter__()
        self.assertIs(raised.exception, primary)
        self.assert_released(owner)

    def test_launch_readiness_and_bind_failure_matrix_releases_acquired_resources(self):
        valid = self.program
        for failure in ("launch", "json", "endpoint", "oversized", "exit", "bind"):
            with self.subTest(failure=failure), contextlib.ExitStack() as stack:
                self.program = valid
                self.primary = (
                    OSError("synthetic launch refusal") if failure == "launch" else None
                )
                if failure in ("json", "endpoint", "oversized"):
                    content = {
                        "json": "not JSON",
                        "endpoint": json.dumps(
                            {"url": "http://example.test:1", "password": "a" * 43}
                        ),
                        "oversized": "x" * (service.MAX_CONTROL + 1),
                    }[failure]
                    self.program = (
                        "import sys; print("
                        + repr(content)
                        + ",flush=True); sys.stdin.read()"
                    )
                elif failure == "exit":
                    self.program = "import sys; sys.exit(1)"
                if failure == "bind":
                    listener = socket.socket(socket.AF_UNIX)
                    refused = Mock(wraps=listener)
                    refused.bind.side_effect = OSError("synthetic bind refusal")
                    stack.enter_context(
                        patch.object(service.socket, "socket", return_value=refused)
                    )
                owner = self.owner()
                with self.assertRaises((OSError, ValueError, box.BoxError)) as raised:
                    owner.__enter__()
                if self.primary is not None:
                    self.assertIs(raised.exception, self.primary)
                self.assert_released(owner)
                self.primary = None

    def test_independent_cleanup_faults_restore_outer_context_and_close_memfd(self):
        for stage in ("listener", "join", "stdin", "wait", "socket", "output-context"):
            with self.subTest(stage=stage), contextlib.ExitStack() as stack:
                previous = {"synthetic": "outer connection"}
                outer = service.CONNECTION.set(previous)
                stack.callback(service.CONNECTION.reset, outer)
                owner = self.owner().__enter__()
                child = self.children[-1]
                if stage == "listener":
                    listener = owner.listener
                    owner.listener = Mock(wraps=listener)

                    def close():
                        listener.close()
                        raise OSError("private-cleanup-sentinel")

                    owner.listener.close.side_effect = close
                    stack.callback(setattr, owner, "listener", listener)
                elif stage == "join":
                    stack.enter_context(
                        patch.object(
                            owner.thread,
                            "join",
                            side_effect=RuntimeError("private-cleanup-sentinel"),
                        )
                    )
                elif stage == "stdin":
                    pipe = child.stdin
                    child.stdin = Mock(wraps=pipe)

                    def close():
                        pipe.close()
                        raise OSError("private-cleanup-sentinel")

                    child.stdin.close.side_effect = close
                    stack.callback(setattr, child, "stdin", pipe)
                elif stage == "wait":
                    wait = child.wait
                    calls = []

                    def faulty_wait(*args, **kwargs):
                        if not calls:
                            calls.append(True)
                            raise OSError("private-cleanup-sentinel")
                        return wait(*args, **kwargs)

                    stack.enter_context(
                        patch.object(child, "wait", side_effect=faulty_wait)
                    )
                elif stage == "socket":
                    unlink = Path.unlink

                    def refuse(path, *args, **kwargs):
                        if path == owner.address:
                            raise OSError("private-cleanup-sentinel")
                        return unlink(path, *args, **kwargs)

                    stack.enter_context(patch.object(Path, "unlink", refuse))
                else:
                    self.output_failure = True
                with self.assertRaisesRegex(box.BoxError, "cleanup failed") as raised:
                    owner.shutdown()
                self.assertNotIn("private-cleanup-sentinel", str(raised.exception))
                self.assertIs(service.live_connection(), previous)
                self.assertIsNone(owner.connection)
                self.assertTrue(child.stdout.closed)
                self.assertTrue(all(output.closed for output in self.outputs))
                self.assertIsNotNone(child.poll())
                self.output_failure = False
                # Socket refusal is intentional; retry only after fault removal.
                if stage == "socket":
                    stack.close()
                    owner.shutdown()

    def test_cleanup_secondary_is_fixed_and_does_not_replace_startup_primary(self):
        owner = self.owner()
        primary = RuntimeError("synthetic primary")
        self.output_failure = True
        errors = io.StringIO()
        with (
            patch.object(service.threading.Thread, "start", side_effect=primary),
            contextlib.redirect_stderr(errors),
            self.assertRaises(RuntimeError) as raised,
        ):
            owner.__enter__()
        self.assertIs(raised.exception, primary)
        self.assertIn("cleanup failed", errors.getvalue())
        self.assertNotIn("private-cleanup-sentinel", errors.getvalue())
        self.assert_released(owner)

    def test_shutdown_is_repeatable_and_does_not_remove_a_replacement_file(self):
        owner = self.owner().__enter__()
        owner.address.unlink()
        owner.address.write_text("unrelated replacement")
        with self.assertRaisesRegex(box.BoxError, "cleanup failed"):
            owner.shutdown()
        self.assertEqual(owner.address.read_text(), "unrelated replacement")
        self.assertIsNone(service.live_connection())
        self.assertTrue(all(output.closed for output in self.outputs))
        self.assertTrue(all(child.poll() is not None for child in self.children))
        owner.address.unlink()
        owner.shutdown()
        owner.shutdown()

    def test_pending_control_reader_is_interrupted_and_joined_before_child_release(
        self,
    ):
        owner = self.owner().__enter__()
        with socket.socket(socket.AF_UNIX) as stream:
            stream.settimeout(2)
            stream.connect(str(owner.address))
            stream.sendall(b'{"action":')
            deadline = time.monotonic() + 2
            while not owner.streams and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(owner.streams)
            started = time.monotonic()
            owner.shutdown()
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(stream.recv(1), b"")
        self.assertTrue(all(not thread.is_alive() for thread in owner.handlers))
        self.assertEqual(owner.streams, set())
        self.assertEqual(owner.clients, 0)
        self.assert_released(owner)

    def test_output_context_enter_failure_has_no_child_or_connection_acquisition(self):
        primary = OSError("synthetic capture refusal")
        refused = Mock()
        refused.__enter__ = Mock(side_effect=primary)
        refused.__exit__ = Mock()
        owner = self.owner()
        with (
            patch.object(self.box, "captured_output", return_value=refused),
            self.assertRaises(OSError) as raised,
        ):
            owner.__enter__()
        self.assertIs(raised.exception, primary)
        refused.__exit__.assert_not_called()
        self.assertEqual(self.children, [])
        self.assert_released(owner)

    def test_memfd_closes_even_if_output_context_refuses_to_exit(self):
        real = box.captured_output(private=True)
        refused = Mock()

        def enter():
            output = real.__enter__()
            self.outputs.append(output)
            return output

        refused.__enter__ = Mock(side_effect=enter)
        refused.__exit__ = Mock(side_effect=OSError("private-cleanup-sentinel"))
        with patch.object(self.box, "captured_output", return_value=refused):
            owner = self.owner().__enter__()
        try:
            with self.assertRaisesRegex(box.BoxError, "output-context"):
                owner.shutdown()
            self.assert_released(owner)
            owner.shutdown()
            refused.__exit__.assert_called_once()
        finally:
            real.__exit__(None, None, None)

    def test_group_disappearing_during_cleanup_is_not_a_private_failure(self):
        owner = self.owner().__enter__()
        child = self.children[-1]

        def disappeared(pid, _sig):
            # ESRCH is tolerable only while the direct child still pins the PGID.
            result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.assertIsNotNone(result)
            raise ProcessLookupError

        with patch.object(service.os, "killpg", side_effect=disappeared) as kill:
            owner.shutdown()
        kill.assert_called_once_with(child.pid, service.signal.SIGKILL)
        self.assert_released(owner)

    def test_lost_owner_anchor_refuses_group_signals_but_releases_private_state(self):
        for cached in (True, False):
            with self.subTest(popen_cached_reap=cached):
                owner = self.owner().__enter__()
                child = self.children[-1]
                child.stdin.close()
                if cached:
                    child.wait(timeout=2)
                else:
                    waitable_exit(child)
                    os.waitpid(child.pid, 0)
                with patch.object(service.os, "killpg") as kill:
                    with self.assertRaisesRegex(
                        box.BoxError, "cleanup failed"
                    ) as raised:
                        owner.shutdown()
                    kill.assert_not_called()
                self.assertNotIn("private-cleanup-sentinel", str(raised.exception))
                self.assert_released(owner)

    def test_readiness_lease_and_shutdown_observations_do_not_reap_before_signals(self):
        events = []
        launching, real_killpg = service.subprocess.Popen, os.killpg

        def instrument(*args, **kwargs):
            child = launching(*args, **kwargs)
            poll, wait = child.poll, child.wait

            def observed_poll():
                events.append("poll")
                return poll()

            def observed_wait(*args, **kwargs):
                events.append("reap")
                return wait(*args, **kwargs)

            child.poll, child.wait = observed_poll, observed_wait
            self.addCleanup(setattr, child, "poll", poll)
            self.addCleanup(setattr, child, "wait", wait)
            return child

        def signal_group(pid, sig):
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                events.append("unowned-signal")
                return
            events.append("signal")
            real_killpg(pid, sig)

        self.box.valid_session_id = box.valid_session_id
        with (
            patch.object(service.subprocess, "Popen", side_effect=instrument),
            patch.object(service.os, "killpg", side_effect=signal_group),
        ):
            owner = self.owner().__enter__()
            owner.data["opencode_version"] = "2.2.6"
            owner.publish("ses_synthetic")
            with socket.socket(socket.AF_UNIX) as stream:
                stream.settimeout(2)
                stream.connect(str(owner.address))
                stream.sendall(
                    (
                        json.dumps({"action": "attach", "workspace": owner.data["id"]})
                        + "\n"
                    ).encode()
                )
                self.assertEqual(
                    service.read_control(stream)["session"], "ses_synthetic"
                )
                self.children[-1].stdin.close()
                with contextlib.redirect_stdout(io.StringIO()):
                    owner.wait_clients()
                owner.shutdown()
        self.assertNotIn("poll", events)
        self.assertNotIn("unowned-signal", events)
        self.assertIn("signal", events)
        self.assertLess(events.index("signal"), events.index("reap"))
        self.assert_released(owner)

    def test_owner_retains_exited_and_graceful_leaders_until_descendants_stop(self):
        plain = self.program
        for exited in (False, True):
            with self.subTest(already_exited=exited), adopted_descendants() as owned:
                ready = self.root / ("descendant-" + str(exited) + ".json")
                self.program = descendant_prefix(ready) + plain
                proposals = []
                real_killpg = os.killpg

                def guarded_signal(pid, sig):
                    try:
                        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    except ChildProcessError:
                        proposals.append(False)
                        return
                    proposals.append(True)
                    real_killpg(pid, sig)

                with patch.object(service.os, "killpg", side_effect=guarded_signal):
                    owner = self.owner().__enter__()
                    evidence = json.loads(ready.read_text())
                    leader = self.children[-1]
                    owned.append((leader, evidence["pid"]))
                    self.assertEqual(evidence["group"], leader.pid)
                    self.assertIn("memfd:oc-box-private-output", evidence["stderr"])
                    if exited:
                        leader.stdin.close()
                        self.assertEqual(waitable_exit(leader), 0)
                    owner.shutdown()
                    status = reap_descendant(evidence["pid"])
                    self.assertTrue(os.WIFSIGNALED(status))
                    self.assertEqual(os.WTERMSIG(status), service.signal.SIGKILL)
                    self.assertTrue(proposals)
                    self.assertTrue(all(proposals))
                    self.assert_released(owner)

    def test_consuming_wait_failure_cannot_reopen_group_signal_authority(self):
        owner = self.owner().__enter__()
        child, owned = self.children[-1], owner.child_owner
        wait, killpg = child.wait, os.killpg

        def partial_wait(*args, **kwargs):
            wait(*args, **kwargs)
            raise OSError("private-cleanup-sentinel")

        with (
            patch.object(child, "wait", side_effect=partial_wait),
            patch.object(service.os, "killpg", wraps=killpg) as kill,
        ):
            with self.assertRaisesRegex(box.BoxError, "child-reap"):
                owner.shutdown()
            count = kill.call_count
            self.assertGreater(count, 0)
            owned.signal(service.signal.SIGTERM)
            owned.signal(service.signal.SIGKILL, final=True)
            self.assertEqual(kill.call_count, count)
        self.assertIsNotNone(child.returncode)
        self.assertTrue(child.stdout.closed)
        self.assertTrue(all(output.closed for output in self.outputs))
        self.assertIsNone(service.live_connection())
        # A retry may reap, but must retain the lost-anchor failure rather than
        # silently reacquire group authority from the cached numeric PID.
        with patch.object(service.os, "killpg") as kill:
            with self.assertRaisesRegex(box.BoxError, "cleanup failed"):
                owner.shutdown()
            kill.assert_not_called()
        self.assert_released(owner)

    def test_body_failure_identity_survives_a_secondary_cleanup_failure(self):
        owner = self.owner()
        primary = OSError("synthetic client failure")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(OSError) as raised:
            with owner:
                self.output_failure = True
                raise primary
        self.assertIs(raised.exception, primary)
        self.assertIn("cleanup failed", errors.getvalue())
        self.assertNotIn("private-cleanup-sentinel", errors.getvalue())
        self.assert_released(owner)


class HistoryWaitTests(unittest.TestCase):
    def setUp(self):
        self.data = {"id": "synthetic-workspace"}
        self.connection = {
            "url": "http://127.0.0.1:12345",
            "password": "synthetic-private-password",
        }
        self.idle = {"data": {}}
        self.busy = {"data": {"ses_child": {"type": "running"}}}
        self.no_jobs = {"data": []}
        self.job = {
            "id": "job_example",
            "sessionID": "ses_child",
            "kind": "shell",
            "status": "completed",
            "time": {"started": 1},
            "shellID": "sh_example",
            "command": "private-sentinel",
        }
        self.now = 100.0
        for name, options in (
            ("monotonic", {"side_effect": lambda: self.now}),
            ("sleep", {"side_effect": self.advance}),
        ):
            clock = patch.object(service.time, name, **options)
            setattr(self, name, clock.start())
            self.addCleanup(clock.stop)
        request = patch.object(service, "api")
        self.api = request.start()
        self.addCleanup(request.stop)

    def advance(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.now += seconds

    def assert_wait_error(self, category, **options):
        with self.assertRaises(box.BoxError) as raised:
            service.wait_history(box, self.data, self.connection, **options)
        self.assertEqual(
            str(raised.exception),
            f"OpenCode 2.x history wait failed ({category}).",
        )
        self.assertTrue(raised.exception.__suppress_context__)

    def test_running_child_then_two_quiet_pairs_uses_only_same_private_inventories(
        self,
    ):
        self.api.side_effect = [
            self.busy,
            self.no_jobs,
            self.idle,
            self.no_jobs,
            self.idle,
            self.no_jobs,
        ]
        service.wait_history(box, self.data, self.connection)
        self.assertEqual(self.api.call_count, 6)
        self.assertEqual(self.sleep.call_count, 2)
        for index, call in enumerate(self.api.call_args_list):
            self.assertIs(call.args[0], box)
            self.assertIs(call.args[1], self.data)
            self.assertIs(call.args[2], self.connection)
            self.assertEqual(
                call.args[3:],
                (
                    "GET",
                    "/api/session/active"
                    if index % 2 == 0
                    else "/api/experimental/job",
                ),
            )
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 30)

    def test_all_known_job_statuses_and_kinds_remain_pending_until_consumed(self):
        for kind in ("shell", "subagent", "image"):
            for status in ("running", "completed", "error", "cancelled"):
                with self.subTest(kind=kind, status=status):
                    self.api.reset_mock()
                    self.api.side_effect = [
                        self.idle,
                        {"data": [{**self.job, "kind": kind, "status": status}]},
                        self.idle,
                        self.no_jobs,
                        self.idle,
                        self.no_jobs,
                    ]
                    service.wait_history(box, self.data, self.connection)
                    self.assertEqual(self.api.call_count, 6)

    def test_completed_job_can_wake_foreground_and_later_activity_resets_quiet_pairs(
        self,
    ):
        self.api.side_effect = [
            self.idle,
            {"data": [self.job]},
            self.busy,
            self.no_jobs,
            self.idle,
            self.no_jobs,
            self.idle,
            {"data": [{**self.job, "status": "error"}]},
            self.idle,
            self.no_jobs,
            self.idle,
            self.no_jobs,
        ]
        service.wait_history(box, self.data, self.connection)
        self.assertEqual(self.api.call_count, 12)
        self.assertEqual(self.sleep.call_count, 5)

    def test_invalid_inventory_matrix_fails_without_exposing_private_payloads(self):
        active_cases = (
            None,
            {},
            {"data": []},
            {"data": {"private-sentinel": {"type": "running"}}},
            {"data": {"ses_child": "private-sentinel"}},
            {"data": {"ses_child": {"type": "idle", "detail": "private-sentinel"}}},
        )
        job_cases = (
            None,
            {},
            {"data": {}},
            {"data": ["private-sentinel"]},
            {"data": [{}]},
            {"data": [{**self.job, "id": ""}]},
            {"data": [{**self.job, "sessionID": "private-sentinel"}]},
            {"data": [{**self.job, "kind": "private-sentinel"}]},
            {"data": [{**self.job, "status": "private-sentinel"}]},
            {"data": [{**self.job, "status": ["running"]}]},
        )
        for category, cases in (
            ("invalid-active-response", active_cases),
            ("invalid-jobs-response", job_cases),
        ):
            for index, response in enumerate(cases):
                with self.subTest(category=category, case=index):
                    self.api.reset_mock()
                    self.api.side_effect = (
                        [response]
                        if category == "invalid-active-response"
                        else [self.idle, response]
                    )
                    self.assert_wait_error(category)
                    self.assertEqual(
                        self.api.call_count,
                        1 if category == "invalid-active-response" else 2,
                    )
        self.sleep.assert_not_called()

    def test_api_failure_matrix_reports_fixed_category_for_either_inventory(self):
        for target in ("active", "jobs"):
            for error in (
                box.BoxError("private-sentinel"),
                OSError("private-sentinel"),
                ValueError("private-sentinel"),
                subprocess.TimeoutExpired("private-sentinel", 0.5),
            ):
                with self.subTest(target=target, error=type(error).__name__):
                    self.api.reset_mock()
                    self.api.side_effect = (
                        [error] if target == "active" else [self.idle, error]
                    )
                    self.assert_wait_error("activity-api-failed")
                    self.assertEqual(
                        self.api.call_count, 1 if target == "active" else 2
                    )
        self.sleep.assert_not_called()

    def test_busy_deadline_caps_final_sleep_and_never_starts_an_expired_request(self):
        self.api.side_effect = lambda *args, **kwargs: (
            self.busy if args[4] == "/api/session/active" else self.no_jobs
        )
        self.assert_wait_error("activity-timeout", timeout=0.5)
        self.assertAlmostEqual(self.now, 100.5)
        self.assertEqual(self.api.call_count, 6)
        self.assertEqual(self.sleep.call_count, 3)
        self.assertAlmostEqual(self.sleep.call_args.args[0], 0.1)

    def test_each_sequential_request_gets_only_remaining_budget(self):
        costs = iter((3, 1, 3))

        def request(*args, **kwargs):
            cost = next(costs)
            self.assertLessEqual(cost, kwargs["timeout"])
            self.advance(cost)
            return self.idle if args[4] == "/api/session/active" else self.no_jobs

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=8, interval=1)
        self.assertEqual(
            [call.kwargs["timeout"] for call in self.api.call_args_list], [8, 5, 3]
        )
        self.assertEqual(self.now, 108)
        self.assertEqual(self.api.call_count, 3)
        self.sleep.assert_called_once_with(1)

    def test_last_quiet_response_must_arrive_before_deadline(self):
        def request(*args, **kwargs):
            if self.api.call_count == 4:
                self.advance(kwargs["timeout"])
            return self.idle if args[4] == "/api/session/active" else self.no_jobs

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=1)
        self.assertEqual(self.api.call_count, 4)
        self.assertEqual(self.now, 101)

    def test_deadline_expiring_in_active_request_skips_jobs_request(self):
        def request(*args, **kwargs):
            self.advance(kwargs["timeout"])
            return self.idle

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=1)
        self.api.assert_called_once()
        self.assertEqual(self.api.call_args.args[4], "/api/session/active")
        self.sleep.assert_not_called()

    def test_long_overall_budget_keeps_each_native_request_at_most_sixty_seconds(self):
        self.api.side_effect = [self.idle, self.no_jobs, self.idle, self.no_jobs]
        service.wait_history(box, self.data, self.connection, timeout=120)
        self.assertEqual(
            [call.kwargs["timeout"] for call in self.api.call_args_list], [60] * 4
        )

    def test_request_failure_at_deadline_is_a_timeout_not_private_api_error(self):
        def request(*args, **kwargs):
            self.advance(kwargs["timeout"])
            raise box.BoxError("private-sentinel")

        self.api.side_effect = request
        self.assert_wait_error("activity-timeout", timeout=1)
        self.api.assert_called_once()
        self.sleep.assert_not_called()

    def test_zero_budget_and_invalid_wait_options_never_call_native_api(self):
        self.assert_wait_error("activity-timeout", timeout=0)
        for options in (
            {"timeout": -1},
            {"timeout": float("inf")},
            {"timeout": float("nan")},
            {"interval": 0},
            {"interval": -1},
            {"interval": float("inf")},
            {"interval": "private-sentinel"},
        ):
            with self.subTest(options=options):
                self.assert_wait_error("invalid-wait-options", **options)
        self.api.assert_not_called()
        self.sleep.assert_not_called()


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-live-service-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "OC_BOX_STATE_HOME": str(self.root / "state"),
                "XDG_RUNTIME_DIR": str(self.root),
                "OC_BOX_CONTAINER": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        project = self.root / "project"
        project.mkdir()
        self.path, self.data = box.create_workspace(project, "tumbleweed")
        self.data["opencode_version"] = "2.2.6"
        box.atomic_json(self.path, self.data)
        self.connection = {
            "url": "http://127.0.0.1:12345",
            "password": "a" * 43,
            "workspace": self.data["id"],
            "session": "ses_shared",
            "version": "2.2.6",
        }
        self.info = {
            "State": {"Running": True},
            "Mounts": [{"Source": str(project), "Destination": str(project)}],
        }

    def test_endpoint_validation_refuses_remote_hosts_embedded_credentials_and_bad_passwords(
        self,
    ):
        self.assertEqual(service.validate_endpoint(self.connection), self.connection)
        for url in (
            "http://localhost:1",
            "http://example.test:1",
            "http://0.0.0.0:1",
            "http://user:password@127.0.0.1:1",
            "http://127.0.0.1:1/path",
            "http://127.0.0.1:1?password=bad",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                service.validate_endpoint({**self.connection, "url": url})
        with self.assertRaises(ValueError):
            service.validate_endpoint({**self.connection, "password": "short"})

    def test_client_environment_and_argv_do_not_inherit_provider_or_vault_values(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "synthetic-key",
                "CUSTOM_SECRET": "synthetic-custom",
                "BASH_ENV": "host-startup",
                "TERM": "xterm\nbad",
            },
        ):
            command = service.client_command(
                box,
                self.data,
                self.connection,
                [
                    "opencode",
                    "mini",
                    "--server",
                    self.connection["url"],
                    "--session",
                    "ses_shared",
                ],
            )
        self.assertNotIn("synthetic-key", " ".join(command))
        self.assertNotIn(self.connection["password"], " ".join(command))
        self.assertNotIn("OPENAI_API_KEY", command.environment)
        self.assertNotIn("CUSTOM_SECRET", command.environment)
        self.assertNotIn("BASH_ENV", command.environment)
        self.assertEqual(
            command.environment["OPENCODE_PASSWORD"], self.connection["password"]
        )
        self.assertIn("TERM", " ".join(command))
        self.assertNotIn("xterm\\nbad", " ".join(command))

    def test_api_response_matrix_preserves_empty_success_json_and_failures(self):
        command = box.ContainerCommand(["synthetic-native-api"], {})
        for output, expected, error in (
            ("", None, None),
            ("\n", None, None),
            (" \t\n", None, None),
            (
                '{"data":{"id":"ses_synthetic"}}\n',
                {"data": {"id": "ses_synthetic"}},
                None,
            ),
            ("null\n", None, None),
            ("false\n", False, None),
            ("[]\n", [], None),
            ("not JSON", None, json.JSONDecodeError),
            (None, None, box.BoxError),
        ):
            with (
                self.subTest(output=output),
                patch.object(service, "client_command", return_value=command),
                patch.object(
                    box,
                    "run",
                    return_value=subprocess.CompletedProcess(command, 0, output, ""),
                    side_effect=box.BoxError("synthetic failed API command")
                    if output is None
                    else None,
                ) as run,
            ):

                def invoke():
                    return service.api(
                        box, self.data, self.connection, "POST", "/api/location/reload"
                    )

                if error is None:
                    self.assertEqual(invoke(), expected)
                else:
                    with self.assertRaises(error):
                        invoke()
                self.assertTrue(run.call_args.kwargs["capture"])
                self.assertTrue(run.call_args.kwargs["private"])
                self.assertNotIn("check", run.call_args.kwargs)
                self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_api_passes_explicit_request_timeout_without_changing_private_transport(
        self,
    ):
        command = box.ContainerCommand(["synthetic-native-api"], {})
        with (
            patch.object(service, "client_command", return_value=command),
            patch.object(
                box,
                "run",
                return_value=subprocess.CompletedProcess(command, 0, '{"data":{}}', ""),
            ) as run,
        ):
            self.assertEqual(
                service.api(
                    box,
                    self.data,
                    self.connection,
                    "GET",
                    "/api/session/active",
                    timeout=0.75,
                ),
                {"data": {}},
            )
        run.assert_called_once_with(command, capture=True, private=True, timeout=0.75)

    def test_live_terminal_joins_without_lifecycle_lock_unlock_refresh_or_stop(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(self.connection)
            ),
            patch("oc_box.terminal_ui.run_tui", return_value=7) as relay,
            patch.object(vault, "hidden") as prompt,
            patch.object(box, "prepare_runtime") as prepare,
            patch.object(box, "stop_container") as stop,
        ):
            self.assertEqual(box.main(["terminal", "--workspace", self.data["id"]]), 7)
        command = relay.call_args.args[0]
        self.assertNotIn("mini", command)
        self.assertNotIn("--server", command)
        self.assertIn("persistent-pty", " ".join(command))
        self.assertIn("ses_shared", command)
        self.assertNotIn("--standalone", command)
        self.assertFalse(relay.call_args.kwargs["show_loading"])
        for operation in (prompt, prepare, stop):
            operation.assert_not_called()

    def test_explicit_maintenance_still_requires_the_exclusive_lifecycle_lock(self):
        with (
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box.sys.stdin, "isatty", return_value=True),
            patch.object(box.sys.stdout, "isatty", return_value=True),
            self.assertRaisesRegex(box.BoxError, "active oc-box process"),
        ):
            box.main(["terminal", "--maintenance", "--workspace", self.data["id"]])

    def test_web_and_full_tui_attach_to_current_session_while_owner_holds_lock(self):
        with (
            box.workspace_lock(self.path),
            patch.object(box, "check_backend"),
            patch.object(box, "detect_opencode_release", return_value="2.2.6"),
            patch.object(service, "controller_running", return_value=True),
            patch.object(
                service, "attach", return_value=contextlib.nullcontext(self.connection)
            ),
            patch.object(service, "web_session", return_value=0) as web,
            patch.object(box, "run_tui", return_value=0) as tui,
            patch.object(box, "prepare_runtime") as prepare,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.main(["web", "--workspace", self.data["id"], "--no-open"]), 0
            )
            self.assertEqual(box.main(["start", "--workspace", self.data["id"]]), 0)
        self.assertEqual(web.call_args.args[2]["session"], "ses_shared")
        self.assertIn("ses_shared", tui.call_args.args[0])
        prepare.assert_not_called()

    def test_invalid_web_options_or_v1_fail_before_startup_and_credentials(self):
        with (
            patch.object(box, "detect_opencode_release", return_value="1.19.5"),
            patch.object(box, "check_backend") as backend,
            patch.object(vault, "hidden") as prompt,
        ):
            with self.assertRaisesRegex(box.BoxError, "requires OpenCode 2.x"):
                box.main(["web", "--project", self.data["project"]])
        backend.assert_not_called()
        prompt.assert_not_called()
        with (
            patch.object(box, "detect_opencode_release", return_value="2.2.6"),
            self.assertRaisesRegex(box.BoxError, "--port"),
        ):
            box.main(["web", "--port", "65536"])
        with self.assertRaisesRegex(box.BoxError, "--maintenance"):
            box.main(["start", "--maintenance"])

    def test_busy_web_port_fails_before_workspace_lookup_or_vault_prompt(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with (
                patch.object(box, "detect_opencode_release", return_value="2.2.6"),
                patch.object(box, "find_workspace") as lookup,
                patch.object(vault, "hidden") as prompt,
                self.assertRaises(OSError),
            ):
                box.main(["web", "--port", str(listener.getsockname()[1])])
            lookup.assert_not_called()
            prompt.assert_not_called()

    def test_browser_launcher_does_not_receive_provider_credentials_or_pairing_link_in_argv(
        self,
    ):
        gateway = SimpleNamespace(url="http://127.0.0.1:5555")
        args = SimpleNamespace(port=0, no_open=False)
        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "synthetic-provider",
                    "CUSTOM_SECRET": "synthetic-secret",
                },
            ),
            patch.object(
                service, "Gateway", return_value=contextlib.nullcontext(gateway)
            ),
            patch.object(service, "api", return_value={"code": "synthetic_pair_code"}),
            patch.object(service.subprocess, "run") as browser,
            patch.object(service.time, "sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                service.web_session(box, self.data, self.connection, args), 0
            )
        call = browser.call_args
        self.assertNotIn("OPENAI_API_KEY", call.kwargs["env"])
        self.assertNotIn("CUSTOM_SECRET", call.kwargs["env"])
        self.assertNotIn("synthetic_pair_code", " ".join(call.args[0]))
        self.assertIn("synthetic_pair_code", call.kwargs["input"])

    def test_v2_history_cli_uses_active_service_without_persisting_control_password(
        self,
    ):
        connection = {**self.connection}
        token = service.CONNECTION.set(connection)
        try:
            command = box.exec_command(
                self.data, [box.CONTAINER_OPENCODE, "export", "ses_shared"]
            )
            self.assertIn("--server", command)
            self.assertNotIn("--standalone", command)
            self.assertNotIn(connection["password"], " ".join(command))
            self.assertEqual(
                command.environment["OPENCODE_PASSWORD"], connection["password"]
            )
            self.assertNotIn(connection["password"], self.path.read_text())
        finally:
            service.CONNECTION.reset(token)

    def test_web_owner_can_start_foreground_and_client_exit_waits_before_sync(self):
        calls = []
        owner = SimpleNamespace(
            connection=self.connection,
            publish=lambda value: calls.append(("publish", value)),
            wait_clients=lambda: calls.append(("wait", None)),
        )
        args = box.parser().parse_args(["start"])
        args.command, args.port, args.no_open = "web", 0, True
        with (
            patch.object(box, "refresh_sessions", return_value={"last": "ses_shared"}),
            patch.object(
                service,
                "web_session",
                side_effect=lambda *_: calls.append(("web", None)) or 0,
            ),
            patch.object(
                service,
                "wait_history",
                side_effect=lambda *_: calls.append(("ready", None)),
            ) as readiness,
            patch.object(
                box,
                "sync_sessions",
                side_effect=lambda *_: calls.append(("sync", None)),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                box.launch_client(self.path, self.data, args, "2.2.6", None, owner), 0
            )
        self.assertEqual(
            calls,
            [
                ("publish", "ses_shared"),
                ("web", None),
                ("wait", None),
                ("ready", None),
                ("sync", None),
            ],
        )
        readiness.assert_called_once_with(box, self.data, owner.connection)

    def test_readiness_failure_keeps_sync_pending_without_export_or_success_message(
        self,
    ):
        args = box.parser().parse_args(["start"])
        for code in (0, 7):
            owner = SimpleNamespace(
                connection=self.connection,
                publish=Mock(),
                wait_clients=Mock(),
            )
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(client_code=code),
                patch.object(
                    box, "refresh_sessions", return_value={"last": "ses_shared"}
                ),
                patch.object(box, "run_tui", return_value=code),
                patch.object(
                    service,
                    "wait_history",
                    side_effect=box.BoxError(
                        "OpenCode 2.x history wait failed (activity-timeout)."
                    ),
                ) as readiness,
                patch.object(box, "sync_sessions") as sync,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                self.assertEqual(
                    box.launch_client(self.path, self.data, args, "2.2.6", None, owner),
                    code or 1,
                )
            owner.wait_clients.assert_called_once()
            readiness.assert_called_once_with(box, self.data, owner.connection)
            sync.assert_not_called()
            self.assertIn("history sync pending", errors.getvalue())
            self.assertIn("activity-timeout", errors.getvalue())
            self.assertIn("Container data retained", errors.getvalue())
            self.assertNotIn("Session history synced", output.getvalue())

    def test_failed_active_client_still_waits_for_attached_clients(self):
        owner = SimpleNamespace(
            connection=self.connection,
            publish=lambda value: None,
            wait_clients=Mock(),
        )
        args = box.parser().parse_args(["start"])
        with (
            patch.object(box, "refresh_sessions", return_value={"last": "ses_shared"}),
            patch.object(box, "run_tui", side_effect=OSError("terminal failed")),
            patch.object(service, "wait_history") as readiness,
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(OSError),
        ):
            box.launch_client(self.path, self.data, args, "2.2.6", None, owner)
        owner.wait_clients.assert_called_once()
        readiness.assert_not_called()

    def test_local_controller_leases_delay_owner_shutdown_and_preserve_lock(self):
        owner = service.Owner(box, self.path, self.data)
        code = (
            "import json,sys; print(json.dumps("
            + repr(
                {"url": self.connection["url"], "password": self.connection["password"]}
            )
            + "),flush=True); sys.stdin.read()"
        )

        def local_exec(_data, _arguments):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", code], dict(os.environ)
            )

        with (
            box.workspace_lock(self.path),
            patch.object(box, "exec_command", side_effect=local_exec),
            patch.object(box, "inspect_owned", return_value=self.info),
            owner,
        ):
            owner.publish("ses_shared")
            self.assertTrue(service.controller_running(box, self.data))
            with service.attach(box, self.path, self.data) as attached:
                self.assertEqual(attached["session"], "ses_shared")
                self.assertEqual(owner.clients, 1)
                exited = threading.Event()
                thread = threading.Thread(
                    target=lambda: (owner.wait_clients(), exited.set())
                )
                thread.start()
                time.sleep(0.05)
                self.assertFalse(exited.is_set())
                with self.assertRaises(box.BoxError):
                    with box.workspace_lock(self.path):
                        pass
            thread.join(timeout=2)
            self.assertTrue(exited.is_set())
        self.assertFalse(owner.address.exists())
        self.assertIsNone(service.live_connection())

    def test_runtime_directory_must_be_private_and_generated_socket_never_overwrites_files(
        self,
    ):
        address = service.socket_path(box, self.data)
        address.write_text("unrelated")
        with self.assertRaisesRegex(box.BoxError, "Unexpected file"):
            service.Owner(box, self.path, self.data).__enter__()
        self.assertEqual(address.read_text(), "unrelated")
        address.unlink()
        address.parent.chmod(0o777)
        with self.assertRaisesRegex(box.BoxError, "unsafe"):
            service.socket_path(box, self.data)

    def test_browser_gateway_streams_http_pairing_redirect_and_binary_upgrades(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/auth/connect/"):
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", "native-cookie=fixture")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/upgrade":
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.end_headers()
                    self.wfile.write(b"binary-frame\x00\xff")
                    self.wfile.flush()
                    self.connection.recv(1)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"hello")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = {
            **self.connection,
            "url": "http://127.0.0.1:" + str(server.server_port),
        }

        def local_relay(_box, _data, port):
            return box.ContainerCommand(
                [sys.executable, "-B", "-c", service.PROXY, str(port)],
                box.maintenance_host_env(),
            )

        with (
            patch.object(service, "relay_command", side_effect=local_relay),
            service.Gateway(box, self.data, connection) as gateway,
        ):
            with urlopen(gateway.url + "/", timeout=5) as response:
                self.assertEqual(response.read(), b"hello")
            with socket.create_connection(
                ("127.0.0.1", urlparse(gateway.url).port), timeout=5
            ) as stream:
                stream.sendall(
                    b"GET /auth/connect/fixture HTTP/1.1\r\nHost: 127.0.0.1:1\r\nAccept: text/html\r\n\r\n"
                )
                data = stream.recv(8192)
                self.assertIn(gateway.session_route().encode(), data)
                self.assertIn(b"native-cookie=fixture", data)
            with socket.create_connection(
                ("127.0.0.1", urlparse(gateway.url).port), timeout=5
            ) as stream:
                stream.sendall(
                    b"GET /upgrade HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
                )
                data = bytearray()
                while b"binary-frame\x00\xff" not in data:
                    data.extend(stream.recv(8192))
                self.assertIn(b"101", data)
                stream.sendall(b"x")
