import socket
import ssl
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from oc_box import web_local_origins as origins
from oc_box import web_named_listeners as listeners


CERTS = {
    name: Path("/test/fixture") / name
    for name in ("account_cert", "account_key", "workspace_cert", "workspace_key")
}


class LocalReservationTests(unittest.TestCase):
    def setUp(self):
        host = patch.object(listeners, "_host_allowed", return_value=True)
        host.start()
        self.addCleanup(host.stop)
        self.origins = origins.LocalOrigins()
        self.tls = origins.LocalTLS(
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
        )

    def fixture_sockets(self):
        account = Mock()
        app = Mock()
        account.getsockname.return_value = self.origins.account_bind
        app.getsockname.return_value = self.origins.workspace_bind
        return account, app

    def test_exact_roles_are_reserved_without_listening_or_unlocking(self):
        account, app = self.fixture_sockets()
        with (
            patch.object(origins, "check_tls", return_value=self.tls) as check,
            patch.object(
                listeners.socket, "socket", side_effect=[account, app]
            ) as create,
            listeners.reserve_profile(**CERTS) as held,
        ):
            check.assert_called_once_with(self.origins, **CERTS)
            self.assertEqual(
                create.call_args_list, [call(socket.AF_INET, socket.SOCK_STREAM)] * 2
            )
            self.assertEqual(held.origins, self.origins)
            self.assertIs(held.tls, self.tls)
            self.assertIs(held.account, account)
            self.assertIs(held.workspace, app)
            for stream, bind in zip(
                (account, app), (self.origins.account_bind, self.origins.workspace_bind)
            ):
                stream.set_inheritable.assert_called_once_with(False)
                stream.bind.assert_called_once_with(bind)
                stream.listen.assert_not_called()
                stream.setsockopt.assert_not_called()
                stream.close.assert_not_called()
        account.close.assert_called_once_with()
        app.close.assert_called_once_with()

    def test_second_bind_refusal_releases_both_and_sanitizes_error(self):
        account, app = self.fixture_sockets()
        app.bind.side_effect = OSError("private diagnostic")
        with (
            patch.object(origins, "check_tls", return_value=self.tls),
            patch.object(listeners.socket, "socket", side_effect=[account, app]),
            self.assertRaisesRegex(
                listeners.LocalHTTPSUnavailable, "could not be reserved"
            ),
        ):
            with listeners.reserve_profile(**CERTS):
                self.fail("A partial profile was published")
        account.close.assert_called_once_with()
        app.close.assert_called_once_with()

    def test_wrong_reported_bind_is_not_adopted(self):
        account, app = self.fixture_sockets()
        account.getsockname.return_value = ("0.0.0.0", self.origins.account_port)
        with (
            patch.object(origins, "check_tls", return_value=self.tls),
            patch.object(
                listeners.socket, "socket", side_effect=[account, app]
            ) as create,
            self.assertRaises(listeners.LocalHTTPSUnavailable),
        ):
            with listeners.reserve_profile(**CERTS):
                self.fail("A substituted binding was adopted")
        create.assert_called_once()
        account.close.assert_called_once_with()
        app.close.assert_not_called()

    def test_bad_tls_or_nonhost_never_touches_sockets(self):
        with (
            patch.object(
                origins, "check_tls", side_effect=ValueError("private path")
            ) as check,
            patch.object(listeners.socket, "socket") as create,
        ):
            with self.assertRaisesRegex(
                listeners.LocalHTTPSUnavailable, "certificate pairs"
            ):
                with listeners.reserve_profile(**CERTS):
                    self.fail("Bad TLS was accepted")
            check.assert_called_once()
            check.reset_mock()
            with (
                patch.object(listeners, "_host_allowed", return_value=False),
                self.assertRaisesRegex(
                    listeners.LocalHTTPSUnavailable, "regular Linux host"
                ),
            ):
                with listeners.reserve_profile(**CERTS):
                    self.fail("Nonhost context accepted")
            check.assert_not_called()
            create.assert_not_called()

    def test_cancellation_releases_unserved_reservation(self):
        account, app = self.fixture_sockets()
        with (
            patch.object(origins, "check_tls", return_value=self.tls),
            patch.object(listeners.socket, "socket", side_effect=[account, app]),
            self.assertRaises(KeyboardInterrupt),
        ):
            with listeners.reserve_profile(**CERTS):
                raise KeyboardInterrupt
        account.close.assert_called_once_with()
        app.close.assert_called_once_with()
