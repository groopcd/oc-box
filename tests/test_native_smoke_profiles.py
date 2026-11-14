import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch

import smoke_history_recovery_v2
import smoke_session_sync_v2
import smoke_v2


class NativeSmokeProfileTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.binary = str(Path("/synthetic/native-v2/opencode").resolve())
        self.probes = Mock()
        for owner, name, label in (
            (smoke_session_sync_v2, "main", "sync"),
            (smoke_history_recovery_v2, "main", "recovery"),
            (smoke_v2, "main", "core"),
            (smoke_v2, "host_fixture_probe", "legacy_host"),
        ):
            probe = self.stack.enter_context(patch.object(owner, name))
            self.probes.attach_mock(probe, label)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))

    def test_default_preserves_all_legacy_stages_and_order(self):
        smoke_v2.run(["--opencode", self.binary])
        self.assertEqual(
            self.probes.mock_calls,
            [
                call.sync(self.binary),
                call.recovery(self.binary),
                call.core(self.binary, None, include_legacy=True),
                call.legacy_host(self.binary),
            ],
        )
        self.assertNotIn("NOT_RUN:", self.output.getvalue())

    def test_v2_only_keeps_native_probes_but_never_selects_legacy_host(self):
        with (
            patch.object(
                smoke_v2.shutil, "which", side_effect=AssertionError("V1 discovery")
            ),
            patch.object(
                smoke_v2,
                "create_interrupted_history",
                side_effect=AssertionError("V1 invocation"),
            ),
        ):
            smoke_v2.run(["--opencode", self.binary, "--v2-only"])
        self.assertEqual(
            self.probes.mock_calls,
            [
                call.sync(self.binary),
                call.recovery(self.binary),
                call.core(self.binary, None, include_legacy=False),
            ],
        )
        self.assertIn(
            "SCOPE: V2-only offline native integration", self.output.getvalue()
        )
        self.assertIn(
            "NOT_RUN: legacy OpenCode 1.19.5/2.2.6 host fixture contract",
            self.output.getvalue(),
        )
        self.assertNotIn("PASS:", self.output.getvalue())

    def test_optional_copied_plugins_remain_available_in_both_profiles(self):
        for flags, include_legacy in (([], True), (["--v2-only"], False)):
            with self.subTest(include_legacy=include_legacy):
                self.probes.reset_mock()
                smoke_v2.run(
                    [
                        "--opencode",
                        self.binary,
                        "--legacy-plugin-dir",
                        "/synthetic/copied-plugins",
                        *flags,
                    ]
                )
                self.probes.core.assert_called_once_with(
                    self.binary,
                    "/synthetic/copied-plugins",
                    include_legacy=include_legacy,
                )
                self.assertEqual(
                    self.probes.legacy_host.call_count, int(include_legacy)
                )

    def test_each_probe_failure_stops_subsequent_stages_in_both_profiles(self):
        for flags, labels in (
            ([], ["sync", "recovery", "core", "legacy_host"]),
            (["--v2-only"], ["sync", "recovery", "core"]),
        ):
            for index, label in enumerate(labels):
                with self.subTest(flags=flags, label=label):
                    self.probes.reset_mock(side_effect=True)
                    failure = RuntimeError("synthetic native probe failure")
                    getattr(self.probes, label).side_effect = failure
                    with self.assertRaises(RuntimeError) as raised:
                        smoke_v2.run(["--opencode", self.binary, *flags])
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(
                        [event[0] for event in self.probes.mock_calls],
                        labels[: index + 1],
                    )


class NativeSmokeVersionTests(unittest.TestCase):
    def test_core_native_major_guard_accepts_old_and_new_v2_but_not_v1_or_v3(self):
        binary = str(Path("/synthetic/native/opencode").resolve())
        for release, expected in (
            ("1.19.5", AssertionError),
            ("2.2.6", RuntimeError),
            ("2.3.0", RuntimeError),
            ("3.0.0", ValueError),
        ):
            for include_legacy in (True, False):
                with self.subTest(release=release, include_legacy=include_legacy):
                    # Stop before config/provider setup: only the real version
                    # parsing/guard runs here, not a simulated native smoke pass.
                    stop = RuntimeError("stop after native version validation")
                    with patch.object(
                        smoke_v2, "bounded", side_effect=[release + "\n", stop]
                    ) as bounded:
                        with self.assertRaises(expected) as raised:
                            smoke_v2.main(binary, include_legacy=include_legacy)
                        self.assertEqual(
                            bounded.call_args_list[0].args[0], [binary, "--version"]
                        )
                        if expected is RuntimeError:
                            self.assertIs(raised.exception, stop)
                            self.assertEqual(
                                bounded.call_args_list[1].args[0],
                                ["git", "init", "--quiet"],
                            )
                        else:
                            self.assertEqual(bounded.call_count, 1)


if __name__ == "__main__":
    unittest.main()
