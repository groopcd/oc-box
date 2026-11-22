"""Host-only TLS preparation; never opens a workspace or browser listener."""

import argparse
import json
import os
from pathlib import Path

if __package__:
    from . import web_tls
    from .paths import web_tls_home
else:
    import web_tls
    from paths import web_tls_home

LOCAL_ORIGINS = ("https://localhost", "https://127.0.0.1")
CERT_NAME = "server.pem"
KEY_NAME = "server-key.pem"


def local_directory():
    return web_tls_home() / "localhost"


def _ensure_not_registered_project(box, directory):
    """Refuse generation into a known project, including earlier registrations."""
    try:
        target = directory.resolve()
        for path in box.workspace_paths():
            record = box.read_json(path)
            project = Path(record["project"])
            if not project.is_absolute():
                raise ValueError
            project = project.resolve()
            if target.is_relative_to(project) or project.is_relative_to(target):
                raise box.BoxError(
                    "A registered project overlaps the private web TLS directory."
                )
            if not record.get("podman_args"):
                continue
            # Custom create flags can add arbitrary mounts. Inspect the
            # registered container instead of guessing from Podman syntax.
            container = box.inspect_owned(record)
            if container is None:
                continue
            mounts = container.get("Mounts")
            if not isinstance(mounts, list):
                raise TypeError
            for mount in mounts:
                if not isinstance(mount, dict) or not isinstance(
                    mount.get("Type"), str
                ):
                    raise TypeError
                if mount["Type"] != "bind":
                    continue
                source = Path(mount["Source"])
                if not source.is_absolute():
                    raise ValueError
                source = source.resolve()
                if target.is_relative_to(source) or source.is_relative_to(target):
                    raise box.BoxError(
                        "A registered workspace bind overlaps the private web TLS directory."
                    )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        raise box.BoxError(
            "Cannot verify registered projects for safe TLS setup."
        ) from None


def parser():
    result = argparse.ArgumentParser(
        prog="oc-box web tls",
        description="Prepare or check host HTTPS files without starting OpenCode.",
        allow_abbrev=False,
    )
    commands = result.add_subparsers(dest="action", required=True)
    check = commands.add_parser(
        "check",
        help="read-only certificate validation and optional helper status",
        allow_abbrev=False,
    )
    check.add_argument("profile", choices=("localhost", "supplied"))
    check.add_argument("--origin", metavar="HTTPS_ORIGIN")
    check.add_argument("--cert", type=Path, metavar="PEM")
    check.add_argument("--key", type=Path, metavar="PEM")
    check.add_argument("--json", action="store_true", help="emit fixed result fields")
    local_pair = commands.add_parser(
        "check-local-pair",
        help="read-only TLS preflight for distinct local named sign-in and workspace sites",
        allow_abbrev=False,
    )
    local_pair.add_argument("--account-cert", type=Path, required=True, metavar="PEM")
    local_pair.add_argument("--account-key", type=Path, required=True, metavar="PEM")
    local_pair.add_argument("--workspace-cert", type=Path, required=True, metavar="PEM")
    local_pair.add_argument("--workspace-key", type=Path, required=True, metavar="PEM")
    local_pair.add_argument("--account-port", type=int, default=9443, metavar="PORT")
    local_pair.add_argument("--workspace-port", type=int, default=9444, metavar="PORT")
    local_pair.add_argument(
        "--json", action="store_true", help="emit fixed result fields"
    )
    setup = commands.add_parser(
        "setup",
        help="prepare localhost certificate files with optional mkcert",
        allow_abbrev=False,
    )
    setup.add_argument("profile", choices=("localhost",))
    setup.add_argument(
        "--install-trust",
        action="store_true",
        help="request browser NSS trust after explicit host confirmation",
    )
    return result


def _check(box, args):
    local = args.profile == "localhost"
    external = args.cert is not None or args.key is not None or args.origin is not None
    if external and (args.cert is None or args.key is None or args.origin is None):
        raise box.BoxError("Supply --origin, --cert and --key together.")
    if not local and not external:
        raise box.BoxError("A supplied TLS check requires --origin, --cert and --key.")
    if external:
        certificate, key = args.cert, args.key
    else:
        directory = local_directory()
        certificate, key = directory / CERT_NAME, directory / KEY_NAME
    report = {
        "profile": args.profile,
        "certificate": "valid",
        "diagnostic": None,
        "message": "Certificate files passed the local HTTPS check.",
        "browser_trust": "unverified",
        "helpers": (
            {name: bool(web_tls.find_helper(name)) for name in ("mkcert", "certutil")}
            if local
            else {}
        ),
    }
    try:
        origins = (args.origin,) if external else LOCAL_ORIGINS
        for value in origins:
            if local and web_tls.parse_https_origin(value).hostname not in (
                "localhost",
                "127.0.0.1",
            ):
                raise WebTLSProfileError
            web_tls.validate(certificate, key, value)
    except WebTLSProfileError:
        report.update(
            certificate="invalid",
            diagnostic="profile_origin",
            message="Use localhost or 127.0.0.1 for the localhost check.",
        )
    except web_tls.WebTLSError as exc:
        report.update(
            certificate="invalid",
            diagnostic=exc.code,
            message=str(exc),
        )
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(report["message"])
        for name, found in report["helpers"].items():
            print(
                f"{name}: {'available' if found else 'unavailable'} (optional setup only)"
            )
        print("Browser trust: unverified. Check each browser profile you will use.")
    return int(report["certificate"] != "valid")


class WebTLSProfileError(Exception):
    """An HTTPS origin does not fit the selected diagnostic profile."""


def _check_local_pair(args):
    if __package__:
        from . import web_local_origins as local
    else:
        import web_local_origins as local
    report = {
        "profile": "named-local-pair",
        "certificate": "valid",
        "diagnostic": None,
        "browser_trust": "unverified",
        "name_resolution": "unverified",
        "listener_started": False,
    }
    try:
        origins = local.LocalOrigins(args.account_port, args.workspace_port)
        report["account_origin"] = origins.account.value
        report["workspace_origin"] = origins.workspace.value
        local.check_tls(
            origins,
            account_cert=args.account_cert,
            account_key=args.account_key,
            workspace_cert=args.workspace_cert,
            workspace_key=args.workspace_key,
        )
    except local.LocalOriginError as exc:
        report.update(
            certificate="invalid", diagnostic="local_profile", message=str(exc)
        )
    except web_tls.WebTLSError as exc:
        report.update(certificate="invalid", diagnostic=exc.code, message=str(exc))
    else:
        report["message"] = (
            "Account and workspace certificate files passed separate name checks."
        )
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(report["message"])
        if "account_origin" in report:
            print(f"Account site: {report['account_origin']}")
            print(f"Workspace site: {report['workspace_origin']}")
        print(
            "Browser trust and loopback name resolution: unverified. No listener was started."
        )
    return int(report["certificate"] != "valid")


def _approve_browser_trust():
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC)
        try:
            os.write(
                fd,
                b"Trusting a local CA gives it authority in the affected browser stores.\n"
                b"This changes your host browser NSS trust. Never share the CA private key.\n"
                b"Type TRUST LOCAL CA to continue: ",
            )
            return os.read(fd, 128) in (b"TRUST LOCAL CA\n", b"TRUST LOCAL CA\r\n")
        finally:
            os.close(fd)
    except OSError:
        return False


def cli(box, argv):
    args = parser().parse_args(argv)
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise box.BoxError(
            "Run 'oc-box web tls' on the browser host, not in a workspace container."
        )
    if args.action == "check":
        return _check(box, args)
    if args.action == "check-local-pair":
        return _check_local_pair(args)
    if __package__:
        from . import web_tls_setup
    else:
        import web_tls_setup
    directory = local_directory()
    _ensure_not_registered_project(box, directory.parent)
    web_tls_setup.setup(
        directory,
        install_trust=args.install_trust,
        approve=_approve_browser_trust,
    )
    print("Localhost TLS files are ready. Browser trust is not verified.")
    print("Check them with 'oc-box web tls check localhost'.")
    return 0
