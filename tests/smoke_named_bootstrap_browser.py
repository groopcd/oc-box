"""Provider-free Chromium check of the named bootstrap on two trusted TLS sites.

Run from the checkout with Playwright, OpenSSL and certutil (NSS tools):

  PYTHONPATH=src uv run --no-project --with playwright python -B \
    tests/smoke_named_bootstrap_browser.py --chromium /path/to/chromium

Only a fresh browser home trusts the test CA. A private loopback fixture
uses the bounded named TLS transport and bootstrap handlers; it does not
start OpenCode or Podman, serve a password field, or establish production
listener acceptance.
"""

import argparse
import contextlib
import os
from pathlib import Path
import shutil
import socket
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
    web_named_listeners,
    web_named_transport,
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


def accept_fixture(listener, reader, ingress, stop):
    while not stop.is_set():
        try:
            peer, _ = listener.accept()
        except TimeoutError:
            continue
        try:
            reader.respond_prelogin(
                peer,
                ingress=ingress,
                login_document=b"<!doctype html><h1>Account bootstrap accepted</h1>",
            )
        except web_named_headers.NamedRequestError:
            pass


def stop_fixture(worker, stop):
    stop.set()
    worker.join(timeout=6)
    require(not worker.is_alive(), "The synthetic TLS fixture did not drain.")


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
            cleanup.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            for _ in range(2)
        ]
        for server in servers:
            server.bind(("127.0.0.1", 0))
        origins = web_local_origins.LocalOrigins(
            *(server.getsockname()[1] for server in servers)
        )
        tls = web_local_origins.check_tls(origins, **pairs)
        reader = web_named_transport.NamedHTTPSReader.from_reservation(
            web_named_listeners.ReservedProfile(origins, tls, *servers)
        )
        binding = web_admission.WebBinding("a" * 32, "b" * 32, origins.workspace.value)
        bootstrap = web_bootstrap.BrowserBootstrap(origins=origins, binding=binding)
        cleanup.callback(bootstrap.close)
        ingress = web_named_bootstrap.NamedBootstrapIngress(
            origins=origins,
            binding=binding,
            bootstrap=bootstrap,
            owner_current=lambda actual: actual == binding,
        )
        stop = threading.Event()
        for server in servers:
            server.listen(4)
            server.settimeout(0.1)
            worker = threading.Thread(
                target=accept_fixture,
                args=(server, reader, ingress, stop),
            )
            worker.start()
            cleanup.callback(stop_fixture, worker, stop)

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
