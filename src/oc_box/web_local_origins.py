"""Two distinct HTTPS sites for the loopback-only named sign-in rollout."""

from dataclasses import dataclass
from secrets import compare_digest
from ssl import ALERT_DESCRIPTION_UNRECOGNIZED_NAME, SSLContext

if __package__:
    from . import web_tls
else:
    import web_tls

ACCOUNT_HOST = "accounts.ocbox.test"
WORKSPACE_HOST = "workspace.ocbox-app.test"
LOOPBACK = "127.0.0.1"
ACCOUNT_PORT = 9443
WORKSPACE_PORT = 9444


class LocalOriginError(ValueError):
    """Fixed refusal that does not include untrusted host or path input."""


@dataclass(frozen=True, slots=True)
class LocalOrigins:
    """Fixed hostnames deliberately under different reserved .test sites.

    No browser-supplied name, bind address, authority or return URL configures
    these listeners. A future listener must bind the addresses below directly
    and require exact TLS SNI in addition to HTTP admission.
    """

    account_port: int = ACCOUNT_PORT
    workspace_port: int = WORKSPACE_PORT

    def __post_init__(self):
        if (
            any(
                type(port) is not int or not 1024 <= port <= 65535
                for port in (self.account_port, self.workspace_port)
            )
            or self.account_port == self.workspace_port
        ):
            raise LocalOriginError(
                "Use distinct, unprivileged local HTTPS ports (1024 through 65535)."
            )

    @property
    def account(self):
        return web_tls.HTTPSOrigin(ACCOUNT_HOST, self.account_port)

    @property
    def workspace(self):
        return web_tls.HTTPSOrigin(WORKSPACE_HOST, self.workspace_port)

    @property
    def account_bind(self):
        return LOOPBACK, self.account_port

    @property
    def workspace_bind(self):
        return LOOPBACK, self.workspace_port


@dataclass(frozen=True, slots=True, repr=False)
class LocalTLS:
    account: SSLContext
    workspace: SSLContext


def _isolate_sni(context, hostname):
    # HTTP Host and Origin must still be checked separately. Reject absent or
    # wrong SNI rather than allowing the peer to select an account TLS context.
    def admit(_stream, server_name, _context):
        if type(server_name) is not str or not compare_digest(server_name, hostname):
            return ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        return None

    context.sni_callback = admit


def check_tls(origins, *, account_cert, account_key, workspace_cert, workspace_key):
    """Validate both certificate pairs without resolving names or opening sockets.

    Loading the correct keys and checking name coverage is not verification of
    an operating-system/browser trust store, an /etc/hosts mapping, or DNS.
    """
    if type(origins) is not LocalOrigins:
        raise LocalOriginError("Expected the dedicated local HTTPS profile.")
    account = web_tls.validate(account_cert, account_key, origins.account.value)
    workspace = web_tls.validate(workspace_cert, workspace_key, origins.workspace.value)
    _isolate_sni(account, origins.account.hostname)
    _isolate_sni(workspace, origins.workspace.hostname)
    return LocalTLS(account, workspace)
