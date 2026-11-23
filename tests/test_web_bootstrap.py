"""Offline tests for the two-host browser binding table."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from oc_box import web_bootstrap
from oc_box.web_admission import WebBinding
from oc_box.web_local_origins import LocalOrigins


class BrowserBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.origins = LocalOrigins()
        self.binding = WebBinding(
            "a" * 32, "synthetic-owner", self.origins.workspace.value
        )
        self.account = self.origins.account.value

    def table(self, **options):
        return web_bootstrap.BrowserBootstrap(
            origins=self.origins, binding=self.binding, **options
        )

    def accept(self, table, ticket, **kwargs):
        options = {"auth_origin": self.account, "ticket": ticket.value, **kwargs}
        return table.accept(self.binding, **options)

    def test_one_use_ticket_translates_only_a_known_browser_cookie(self):
        table = self.table()
        ticket = table.issue(self.binding)
        self.assertNotIn(ticket.value, repr(ticket))
        self.assertNotIn(ticket.app_nonce, repr(ticket))
        self.assertIsNone(self.accept(table, ticket, auth_origin=self.binding.origin))
        nonce = self.accept(table, ticket)
        self.assertNotIn(nonce, (ticket.value, ticket.app_nonce))
        self.assertIsNone(self.accept(table, ticket))
        lookup = {"auth_origin": self.account, "account_nonce": nonce}
        self.assertEqual(table.app_nonce(self.binding, **lookup), ticket.app_nonce)
        self.assertIsNone(
            table.app_nonce(replace(self.binding, owner_generation="new"), **lookup)
        )
        self.assertIsNone(
            table.app_nonce(
                self.binding, **(lookup | {"account_nonce": ticket.app_nonce})
            )
        )
        self.assertTrue(table.retire(self.binding, **lookup))
        self.assertIsNone(table.app_nonce(self.binding, **lookup))

    def test_capacity_and_deadline_include_issued_and_accepted_items(self):
        table = self.table(ttl=2, capacity=1)
        with patch.object(web_bootstrap, "monotonic", return_value=100.0) as clock:
            ticket = table.issue(self.binding)
            self.assertIsNone(table.issue(self.binding))
            clock.return_value = 101.0
            nonce = self.accept(table, ticket)
            self.assertIsNone(table.issue(self.binding))
            clock.return_value = 102.0
            self.assertIsNone(
                table.app_nonce(
                    self.binding, auth_origin=self.account, account_nonce=nonce
                )
            )
            self.assertIsNotNone(table.issue(self.binding))
        table.close()
        self.assertIsNone(table.issue(self.binding))

    def test_only_one_thread_accepts_a_ticket(self):
        table = self.table()
        ticket = table.issue(self.binding)
        with ThreadPoolExecutor(max_workers=12) as executor:
            answers = list(
                executor.map(lambda _: self.accept(table, ticket), range(24))
            )
        self.assertEqual(sum(answer is not None for answer in answers), 1)

    def test_retired_ticket_and_cookie_cannot_be_reused_within_lifetime(self):
        table = self.table()
        values = ["A" * 43, "B" * 42 + "E", "C" * 42 + "I"]
        with patch.object(web_bootstrap.secrets, "token_urlsafe", side_effect=values):
            first = table.issue(self.binding)
            nonce = self.accept(table, first)
        self.assertTrue(
            table.retire(self.binding, auth_origin=self.account, account_nonce=nonce)
        )
        with patch.object(
            web_bootstrap.secrets, "token_urlsafe", side_effect=values[:2]
        ):
            self.assertIsNone(table.issue(self.binding))
        with patch.object(
            web_bootstrap.secrets,
            "token_urlsafe",
            side_effect=["D" * 42 + "M", "E" * 42 + "Q", nonce],
        ):
            second = table.issue(self.binding)
            self.assertIsNone(self.accept(table, second))
        self.assertIsNone(
            table.app_nonce(self.binding, auth_origin=self.account, account_nonce=nonce)
        )
