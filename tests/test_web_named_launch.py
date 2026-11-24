"""Verify host-side sequencing without a native service or real secrets."""

from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import web_accounts, web_named_account_assets, web_named_launch as launch


_PATH = Path("/synthetic/named")
_WORKSPACE = "a" * 32


class NamedInputProfileTests(unittest.TestCase):
    def setUp(self):
        context = ExitStack()
        self.addCleanup(context.close)
        root = Path(context.enter_context(TemporaryDirectory()))
        self.path = root / "profile.json"
        context.enter_context(
            patch.object(launch.web_named_listeners, "_host_allowed", return_value=True)
        )
        pin = {"path": "/synthetic/browser.wasm", "size": 20, "sha256": "a" * 64}
        self.data = {
            "version": 1,
            "tls": {key: f"/synthetic/{key}.pem" for key in launch._CERT_KEYS},
            "wasm": pin,
            "verifier": {**pin, "path": "/synthetic/verifier", "sha256": "b" * 64},
            "account_store": "/synthetic/accounts",
        }

    def write(self, data=None, *, raw=None, mode=0o600):
        self.path.write_bytes(
            raw
            if raw is not None
            else json.dumps(self.data if data is None else data).encode()
        )
        self.path.chmod(mode)

    def refusal(self):
        with self.assertRaisesRegex(
            launch.NamedLaunchError, "explicit named HTTPS profile"
        ):
            launch.load_named_inputs(self.path)

    def test_exact_owner_profile_selects_pins_without_opening_referenced_artifacts(
        self,
    ):
        self.write(mode=0o400)
        inputs = launch.load_named_inputs(self.path)
        self.assertEqual(inputs.account_store, Path("/synthetic/accounts"))
        self.assertEqual(inputs.wasm_file, Path("/synthetic/browser.wasm"))
        self.assertEqual(inputs.verifier_pin.sha256.hex(), "b" * 64)
        self.assertEqual(inputs.wasm_pin.size, 20)
        self.assertNotIn("/synthetic", repr(inputs))

    def test_duplicate_fields_passwords_and_nonnative_paths_refuse(self):
        for data in (
            self.data | {"password": "do-not-read"},
            self.data | {"version": True},
            self.data | {"account_store": "relative/accounts"},
            self.data | {"account_store": "/synthetic/../outside"},
            self.data | {"account_store": "//synthetic/accounts"},
            self.data | {"wasm": self.data["wasm"] | {"size": True}},
            self.data | {"verifier": self.data["verifier"] | {"sha256": "A" * 64}},
        ):
            self.write(data)
            self.refusal()
        self.write(raw=b'{"version":1,' + json.dumps(self.data).encode()[1:])
        self.refusal()
        self.write(raw=b" " * (launch._PROFILE_LIMIT + 1))
        self.refusal()

    def test_shared_missing_and_substituted_profiles_fail_closed(self):
        self.refusal()
        self.write(mode=0o644)
        self.refusal()
        self.path.chmod(0o600)
        other = self.path.with_name("other")
        os.link(self.path, other)
        self.refusal()
        self.path.unlink()
        self.path.symlink_to(other)
        self.refusal()
        directory = self.path.with_name("redirect")
        directory.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(launch.NamedLaunchError):
            launch.load_named_inputs(directory / "other")

    def test_changed_profile_while_reading_is_not_adopted(self):
        self.write()
        pread = os.pread

        def replaced(fd, length, offset):
            content = pread(fd, length, offset)
            if offset == 0:
                # Atomic replacement is not a newer value to use without
                # another explicit launch/verification.
                moved = self.path.with_name("moved")
                self.path.rename(moved)
                self.write()
            return content

        with patch.object(launch.os, "pread", side_effect=replaced):
            self.refusal()


class PrepareNamedLaunchTests(unittest.TestCase):
    def setUp(self):
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.events = []
        self.profile = SimpleNamespace(
            origins=SimpleNamespace(
                workspace=SimpleNamespace(value="https://workspace.ocbox-app.test:9444")
            )
        )
        self.assets = object()
        self.snapshot = web_accounts.AccountSnapshot(
            web_accounts.AccountStore(
                b"s" * 128,
                (web_accounts.Account("synthetic", (_WORKSPACE,), b"r" * 192),),
            ),
            lambda: True,
            lambda: self.events.append("snapshot-closed"),
        )
        self.owner = SimpleNamespace(data={"id": _WORKSPACE})
        pin = web_named_account_assets.ReviewedArtifactPin(32, b"d" * 32)
        self.inputs = launch.NamedLaunchInputs(
            _PATH / "account.pem",
            _PATH / "account-key.pem",
            _PATH / "workspace.pem",
            _PATH / "workspace-key.pem",
            _PATH / "approved.wasm",
            pin,
            _PATH / "approved-verifier",
            pin,
            _PATH / "accounts",
        )

        def watch(name, value):
            @contextmanager
            def watched(**kwargs):
                self.events.append(name)
                try:
                    yield value
                finally:
                    self.events.append(name + "-closed")

            return watched

        def unlock(*args):
            self.events.append("snapshot")
            return self.snapshot

        def prompt(*args):
            self.events.append("host-prompt")
            return "synthetic-store-passphrase"

        self.host = contexts.enter_context(
            patch.object(launch.web_named_listeners, "_host_allowed", return_value=True)
        )
        self.reserve = contexts.enter_context(
            patch.object(
                launch.web_named_listeners,
                "reserve_profile",
                side_effect=watch("reserve", self.profile),
            )
        )
        self.assets_loader = contexts.enter_context(
            patch.object(
                launch.web_named_account_assets,
                "prepare_account_assets",
                side_effect=watch("assets", self.assets),
            )
        )
        self.prompt = contexts.enter_context(
            patch.object(
                launch.secret_prompt,
                "read_secret",
                side_effect=prompt,
            )
        )
        self.load = contexts.enter_context(
            patch.object(
                launch.web_accounts,
                "load_snapshot",
                side_effect=unlock,
            )
        )
        self.foreground = Mock()
        self.foreground.start.return_value = (
            "https://workspace.ocbox-app.test:9444/auth/oc-box-entry"
        )
        self.compose = contexts.enter_context(
            patch.object(
                launch.web_named_foreground,
                "NamedForeground",
                return_value=self.foreground,
            )
        )

    def test_prepare_pins_tls_and_assets_before_the_distinct_host_account_prompt(self):
        prepared = launch.prepare_named_launch(self.inputs, workspace_label="Synthetic")
        try:
            self.assertEqual(
                self.events, ["reserve", "assets", "host-prompt", "snapshot"]
            )
            self.prompt.assert_called_once_with("Host web account store passphrase: ")
            self.load.assert_called_once_with(
                self.inputs.account_store, "synthetic-store-passphrase"
            )
            self.assertIs(
                self.assets_loader.call_args.kwargs["origins"], self.profile.origins
            )
            self.compose.assert_not_called()
        finally:
            self.assertTrue(prepared.close())
        self.assertEqual(
            self.events[-3:], ["snapshot-closed", "assets-closed", "reserve-closed"]
        )

    def test_certificate_refusal_never_prompts_or_prepares_an_owner(self):
        self.reserve.side_effect = launch.web_named_listeners.LocalHTTPSUnavailable(
            "private path"
        )
        with self.assertRaisesRegex(launch.NamedLaunchError, "reservation refused"):
            launch.prepare_named_launch(self.inputs)
        self.prompt.assert_not_called()
        self.load.assert_not_called()
        self.compose.assert_not_called()

    def test_unverified_artifact_releases_reservation_before_any_prompt(self):
        self.assets_loader.side_effect = web_named_account_assets.AccountAssetError()
        with self.assertRaisesRegex(launch.NamedLaunchError, "artifact refused"):
            launch.prepare_named_launch(self.inputs)
        self.assertEqual(self.events, ["reserve", "reserve-closed"])
        self.prompt.assert_not_called()
        self.compose.assert_not_called()

    def test_no_terminal_releases_unserved_tls_and_artifact_resources(self):
        self.prompt.side_effect = EOFError()
        with self.assertRaisesRegex(launch.NamedLaunchError, "controlling terminal"):
            launch.prepare_named_launch(self.inputs)
        self.assertEqual(
            self.events, ["reserve", "assets", "assets-closed", "reserve-closed"]
        )
        self.load.assert_not_called()
        self.compose.assert_not_called()

    def test_bad_store_refuses_and_releases_unserved_resources(self):
        self.load.side_effect = web_accounts.WebAccountsError("raw synthetic detail")
        with self.assertRaisesRegex(launch.NamedLaunchError, "could not be unlocked"):
            launch.prepare_named_launch(self.inputs)
        self.assertEqual(self.events[-2:], ["assets-closed", "reserve-closed"])
        self.compose.assert_not_called()

    def test_unassigned_workspace_does_not_attach_or_reuse_preparation(self):
        prepared = launch.prepare_named_launch(self.inputs)
        try:
            with self.assertRaisesRegex(
                launch.NamedLaunchError, "No named web account"
            ):
                prepared.start(SimpleNamespace(data={"id": "b" * 32}), expires_at=1)
            self.compose.assert_not_called()
            with self.assertRaisesRegex(launch.NamedLaunchError, "cannot reuse"):
                prepared.start(self.owner, expires_at=1)
        finally:
            self.assertTrue(prepared.close())

    def test_uncertain_drain_never_abandons_any_prepared_host_resource(self):
        prepared = launch.prepare_named_launch(self.inputs)
        try:
            self.assertEqual(
                prepared.start(self.owner, expires_at=10),
                self.foreground.start.return_value,
            )
            self.compose.assert_called_once_with(
                self.owner,
                profile=self.profile,
                account=self.snapshot,
                assets=self.assets,
            )
            self.foreground.close.return_value = False
            self.assertFalse(prepared.close(timeout=0))
            self.assertEqual(
                self.events, ["reserve", "assets", "host-prompt", "snapshot"]
            )
            with self.assertRaises(launch.NamedLaunchError):
                prepared.start(self.owner, expires_at=10)
        finally:
            self.foreground.close.return_value = True
            self.assertTrue(prepared.close())
        self.assertEqual(
            self.events[-3:], ["snapshot-closed", "assets-closed", "reserve-closed"]
        )
        self.assertTrue(prepared.close())

    def test_container_or_root_precondition_precedes_all_path_work(self):
        self.host.return_value = False
        with self.assertRaisesRegex(launch.NamedLaunchError, "regular Linux host user"):
            launch.prepare_named_launch(self.inputs)
        self.assertEqual(self.events, [])
        self.reserve.assert_not_called()

    def test_foreground_ctrl_c_drains_and_no_open_never_activates_a_browser(self):
        prepared = launch.prepare_named_launch(self.inputs)
        self.foreground.wait_stopped.side_effect = KeyboardInterrupt()
        self.foreground.close.return_value = True
        with (
            patch.object(launch.subprocess, "run") as child,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                launch.run_named_client(None, self.owner, prepared, no_open=True), 130
            )
        child.assert_not_called()
        self.assertIn(self.foreground.start.return_value, output.getvalue())
        self.assertNotIn("synthetic-store-passphrase", output.getvalue())
        self.assertTrue(self.foreground.start.call_args.kwargs["studio"])
        self.assertEqual(
            self.events[-3:], ["snapshot-closed", "assets-closed", "reserve-closed"]
        )

    def test_named_client_preserves_all_resources_after_failed_drain(self):
        prepared = launch.prepare_named_launch(self.inputs)
        self.foreground.wait_stopped.side_effect = KeyboardInterrupt()
        self.foreground.close.return_value = False
        try:
            with (
                redirect_stdout(io.StringIO()),
                self.assertRaises(launch.NamedClientUndrained),
            ):
                launch.run_named_client(None, self.owner, prepared, no_open=True)
            self.foreground.close.assert_called_once()
            self.assertEqual(
                self.events, ["reserve", "assets", "host-prompt", "snapshot"]
            )
        finally:
            self.foreground.close.return_value = True
            self.assertTrue(prepared.close())

    def test_browser_activation_passes_only_the_entry_url_over_stdin(self):
        prepared = launch.prepare_named_launch(self.inputs)
        self.foreground.wait_stopped.return_value = True
        self.foreground.stop_reason = (
            launch.web_named_site_lifecycle.StopReason.REQUESTED
        )
        self.foreground.close.return_value = True
        box = SimpleNamespace(maintenance_host_env=lambda: {"PATH": "/usr/bin:/bin"})
        with (
            patch.dict(
                os.environ,
                {"OC_BOX_SYNTHETIC_TOKEN": "fixture secret", "DISPLAY": ":1"},
            ),
            patch.object(launch.subprocess, "run") as child,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                launch.run_named_client(box, self.owner, prepared, no_open=False), 0
            )
        invocation = child.call_args
        self.assertNotIn(self.foreground.start.return_value, invocation.args[0])
        self.assertEqual(invocation.kwargs["input"], self.foreground.start.return_value)
        self.assertNotIn("OC_BOX_SYNTHETIC_TOKEN", invocation.kwargs["env"])
        self.assertEqual(invocation.kwargs["env"]["DISPLAY"], ":1")
