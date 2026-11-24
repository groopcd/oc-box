"""Unserved preflight for the two fixed local named HTTPS sites.

The reservation validates both certificate pairs and binds both loopback
ports, but does not listen, mint a grant, unlock the vault or open a workspace.
Trusted launcher code must acquire the private owner lease and apply the
named request gates before making either socket a listener.
"""

import os
import socket
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

if __package__:
    from . import web_local_origins
else:
    import web_local_origins


class LocalHTTPSUnavailable(ValueError):
    """Fixed preflight refusal; no certificate, path or OS diagnostics."""


@dataclass(frozen=True, slots=True, repr=False)
class ReservedProfile:
    origins: web_local_origins.LocalOrigins
    tls: web_local_origins.LocalTLS
    account: socket.socket
    workspace: socket.socket


def _host_allowed():
    return (
        sys.platform == "linux"
        and os.getuid() != 0
        and os.geteuid() == os.getuid()
        and os.environ.get("OC_BOX_CONTAINER") != "1"
    )


@contextmanager
def reserve_profile(*, account_cert, account_key, workspace_cert, workspace_key):
    """Bind the fixed profile all-or-nothing before workspace preparation.

    Cookie origins are not isolated by port. Keep one fixed socket pair for
    the reserved sites, without configurable alternate binds or address/port
    reuse. This does not certify DNS or browser trust for those names.
    """
    if not _host_allowed():
        raise LocalHTTPSUnavailable(
            "Named HTTPS setup requires a regular Linux host user."
        )
    origins = web_local_origins.LocalOrigins()
    try:
        tls = web_local_origins.check_tls(
            origins,
            account_cert=account_cert,
            account_key=account_key,
            workspace_cert=workspace_cert,
            workspace_key=workspace_key,
        )
    except (ValueError, OSError):
        raise LocalHTTPSUnavailable(
            "The named HTTPS certificate pairs could not be verified."
        ) from None
    with ExitStack() as stack:
        sockets = []
        try:
            for binding in (origins.account_bind, origins.workspace_bind):
                stream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                stack.callback(stream.close)
                stream.set_inheritable(False)
                stream.bind(binding)
                if stream.getsockname() != binding:
                    raise OSError
                sockets.append(stream)
        except OSError:
            raise LocalHTTPSUnavailable(
                "The two named HTTPS loopback ports could not be reserved."
            ) from None
        yield ReservedProfile(origins, tls, *sockets)
