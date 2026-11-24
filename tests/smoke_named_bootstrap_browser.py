"""Provider-free Chromium check of the named bootstrap on two trusted TLS sites.

Run from the checkout with Playwright, OpenSSL and certutil (NSS tools):

  PYTHONPATH=src uv run --no-project --with playwright python -B \
    tests/smoke_named_bootstrap_browser.py --chromium /path/to/chromium

Only a fresh browser home trusts the test CA. A private loopback HTTP fixture
calls the exact unserved bootstrap handlers; it does not start OpenCode or
Podman, serve a password field, or establish production listener acceptance.
"""

import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright

from oc_box import (
    web_admission,
    web_bootstrap,
    web_local_origins,
    web_named_bootstrap,
    web_named_headers,
)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def run(tool, *args, cwd):
    executable = shutil.which(tool)
    require(executable, f"The browser fixture requires {tool}.")
    command = subprocess.run(
        [executable, *args],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(cwd)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    require(command.returncode == 0, f"Synthetic {tool} preparation failed.")


def material(root, hosts):
    ca = root / "ca.pem"
    run(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=oc-box Synthetic Browser Fixture",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        "ca.key",
        "-out",
        ca.name,
        cwd=root,
    )
    choices = {}
    for role, hostname in hosts.items():
        key, request, leaf, bundle = (
            root / f"{role}.{suffix}" for suffix in ("key", "csr", "pem", "chain")
        )
        extensions = root / f"{role}.ext"
        extensions.write_text(
            f"subjectAltName=DNS:{hostname}\n"
            "basicConstraints=critical,CA:FALSE\n"
            "extendedKeyUsage=serverAuth\n",
            encoding="ascii",
        )
        run(
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            f"/CN={hostname}",
            "-keyout",
            key.name,
            "-out",
            request.name,
            cwd=root,
        )
        run(
            "openssl",
            "x509",
            "-req",
            "-days",
            "1",
            "-in",
            request.name,
            "-CA",
            ca.name,
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-extfile",
            extensions.name,
            "-out",
            leaf.name,
            cwd=root,
        )
        key.chmod(0o600)
        bundle.write_bytes(leaf.read_bytes() + ca.read_bytes())
        choices[f"{role}_cert"], choices[f"{role}_key"] = bundle, key
    return ca, choices


def fixture_handler(ingress, role):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _reply(self, status, body, headers=()):
            self.send_response_only(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch(b"")

        def do_POST(self):
            if self.headers.get_all("Content-Length") != ["50"]:
                self._reply(403, b"Refused")
                return
            self.connection.settimeout(3)
            self._dispatch(self.rfile.read(50))

        def _dispatch(self, body):
            args = dict(
                method=self.command,
                target=self.path,
                headers=list(self.headers.raw_items()),
                body=body,
                actual_tls=isinstance(self.connection, ssl.SSLSocket),
            )
            try:
                if role == "workspace":
                    reply = ingress.workspace(**args)
                    self._reply(200, reply.html, reply.headers())
                else:
                    accepted = ingress.account(**args)
                    self._reply(
                        200,
                        b"<!doctype html><h1>Account bootstrap accepted</h1>",
                        (
                            ("Content-Type", "text/html; charset=utf-8"),
                            ("Cache-Control", "no-store"),
                            (
                                "Content-Security-Policy",
                                "default-src 'none'; frame-ancestors 'none'",
                            ),
                            ("Set-Cookie", accepted.cookie),
                        ),
                    )
            except web_named_headers.NamedRequestError:
                self._reply(403, b"Refused")

    return Handler


def exercise(root, browser_exe):
    home = root / "browser-home"
    database = home / ".pki/nssdb"
    database.mkdir(mode=0o700, parents=True)
    hosts = {
        "account": web_local_origins.ACCOUNT_HOST,
        "workspace": web_local_origins.WORKSPACE_HOST,
    }
    ca, pairs = material(root, hosts)
    run("certutil", "-N", "--empty-password", "-d", f"sql:{database}", cwd=root)
    run(
        "certutil",
        "-A",
        "-d",
        f"sql:{database}",
        "-n",
        "Synthetic oc-box test CA",
        "-t",
        "C,,",
        "-i",
        str(ca),
        cwd=root,
    )

    with contextlib.ExitStack() as cleanup:
        servers = [
            cleanup.enter_context(
                ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
            )
            for _ in range(2)
        ]
        origins = web_local_origins.LocalOrigins(
            *(server.server_port for server in servers)
        )
        tls = web_local_origins.check_tls(origins, **pairs)
        binding = web_admission.WebBinding("a" * 32, "b" * 32, origins.workspace.value)
        bootstrap = web_bootstrap.BrowserBootstrap(origins=origins, binding=binding)
        cleanup.callback(bootstrap.close)
        ingress = web_named_bootstrap.NamedBootstrapIngress(
            origins=origins,
            binding=binding,
            bootstrap=bootstrap,
            owner_current=lambda actual: actual == binding,
        )
        for server, role in zip(servers, ("account", "workspace")):
            server.RequestHandlerClass = fixture_handler(ingress, role)
            server.socket = getattr(tls, role).wrap_socket(
                server.socket, server_side=True
            )
            worker = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}
            )
            worker.start()
            cleanup.callback(worker.join, 5)
            cleanup.callback(server.shutdown)

        account = origins.account.value + web_named_bootstrap.ACCOUNT_ENTRY
        workspace = origins.workspace.value + web_named_bootstrap.WORKSPACE_ENTRY
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                executable_path=browser_exe,
                env={
                    "HOME": str(home),
                    "PATH": "/usr/bin:/bin",
                    "LC_ALL": "C.UTF-8",
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                },
                args=[
                    "--disable-background-networking",
                    "--no-proxy-server",
                    "--host-resolver-rules="
                    + ", ".join(f"MAP {host} 127.0.0.1" for host in hosts.values())
                    + ", MAP * ~NOTFOUND",
                ],
            )
            with browser.new_context() as context:
                page = context.new_page()
                page.set_default_timeout(10_000)
                response = page.goto(workspace)
                require(
                    response is not None and response.ok, "Workspace bootstrap failed."
                )
                require(
                    page.evaluate("window.isSecureContext"),
                    "Workspace was not a secure context.",
                )
                original = page.locator('input[name="ticket"]').input_value()
                with page.expect_response(
                    lambda response: response.url == account
                ) as response:
                    page.get_by_role("button", name="Continue to sign in").click()
                require(response.value.ok, "Cross-site account bootstrap failed.")
                expect(
                    page.get_by_role("heading", name="Account bootstrap accepted")
                ).to_be_visible()
                require(
                    page.evaluate("window.isSecureContext"),
                    "Account was not a secure context.",
                )
                cookies = {
                    (item["domain"], item["name"]): item for item in context.cookies()
                }
                require(
                    set(cookies)
                    == {
                        (hosts["account"], "__Host-ocbox-account"),
                        (hosts["workspace"], "__Host-ocbox-bootstrap"),
                    }
                    and all(
                        item["secure"]
                        and item["httpOnly"]
                        and item["sameSite"] == "Strict"
                        for item in cookies.values()
                    ),
                    "Unexpected bootstrap cookie boundaries.",
                )
                require(
                    page.evaluate("document.cookie") == "",
                    "Bootstrap was visible to JavaScript.",
                )

                response = page.goto(workspace)
                require(
                    response is not None and response.ok,
                    "Second workspace bootstrap failed.",
                )
                # Exercise a replay through the browser's real cross-site form.
                page.locator('input[name="ticket"]').evaluate(
                    "(field, token) => field.value = token", original
                )
                with page.expect_response(
                    lambda response: response.url == account
                ) as replay:
                    page.get_by_role("button", name="Continue to sign in").click()
                require(
                    replay.value.status == 403,
                    "The one-use bootstrap replay was not refused.",
                )
            version = browser.version
            browser.close()
        print(
            f"PASS Chromium {version}: trusted two-site bootstrap, separate cookies and replay refusal."
        )
        print(
            "Synthetic local TLS only; no named login, workspace relay, or host acceptance."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="oc-box-named-bootstrap-") as directory:
        exercise(Path(directory), args.chromium)
