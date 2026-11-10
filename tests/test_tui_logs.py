import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oc_box import activity_log
from oc_box import tui_logs as logs


class LogBufferTests(unittest.TestCase):
    def entry(
        self,
        message,
        *,
        timestamp="2026-11-10T12:00:00Z",
        source="oc-box",
        level="info",
    ):
        return logs.LogEntry(timestamp, source, level, message)

    def test_case_insensitive_literal_search_covers_all_fields_and_source_filter(self):
        buffer = logs.LogBuffer()
        first = self.entry("Start [done].*")
        second = self.entry("other", source="podman", level="warning")
        buffer.append(first)
        buffer.append(second)
        for query in ("sTaRt", "[done].*", "OC-BOX", "2026-11-10", "INFO"):
            with self.subTest(query=query):
                self.assertIn(first, buffer.search(query))
        self.assertEqual(buffer.search("WARNING"), [second])
        self.assertEqual(buffer.search("", source="podman"), [second])
        self.assertEqual(buffer.search("Start", source="podman"), [])
        self.assertEqual(buffer.search("[missing]"), [])
        self.assertEqual(buffer.search(None), [])

    def test_following_does_not_change_or_narrow_retained_search_data(self):
        buffer = logs.LogBuffer(max_entries=3)
        buffer.replace(
            "podman",
            [
                self.entry("first", source="podman"),
                self.entry("match", source="podman"),
            ],
        )
        self.assertEqual([entry.message for entry in buffer.search("match")], ["match"])
        buffer.append(self.entry("new unrelated line", source="podman"))
        self.assertEqual(
            [entry.message for entry in buffer.search("match")],
            ["match"],
        )
        self.assertEqual(
            [entry.message for entry in buffer.search("")],
            ["first", "match", "new unrelated line"],
        )
        buffer.append(self.entry("fourth", source="podman"))
        self.assertEqual(
            [entry.message for entry in buffer.search()],
            ["match", "new unrelated line", "fourth"],
        )

    def test_replace_does_not_duplicate_snapshots_or_erase_other_sources(self):
        buffer = logs.LogBuffer(max_entries=4)
        own = self.entry("local")
        buffer.append(own)
        buffer.replace("podman", [self.entry("old", source="ignored")])
        replacement = [
            self.entry("new one", source="ignored"),
            self.entry("new two", source="ignored"),
        ]
        buffer.replace("podman", replacement)
        buffer.replace("podman", replacement)
        self.assertEqual(
            [entry.message for entry in buffer.search()],
            ["local", "new one", "new two"],
        )
        self.assertEqual(
            [entry.source for entry in buffer.search(source="podman")],
            ["podman", "podman"],
        )
        buffer.replace("podman", [])
        self.assertEqual(buffer.search(), [own])

    def test_global_retention_bound_generator_tail_and_source_count_bound(self):
        buffer = logs.LogBuffer(max_entries=3)
        buffer.replace("podman", (self.entry(str(index)) for index in range(100)))
        self.assertEqual(
            [entry.message for entry in buffer.search()], ["97", "98", "99"]
        )
        buffer.append(self.entry("latest", source="oc-box"))
        self.assertEqual(
            [entry.message for entry in buffer.search()], ["98", "99", "latest"]
        )
        with patch.object(logs, "_MAX_SOURCES", 2):
            buffer.append(self.entry("third source", source="third"))
        self.assertEqual(
            [entry.message for entry in buffer.search()], ["latest", "third source"]
        )
        self.assertLessEqual(len(buffer._sources), 2)

    def test_timestamp_order_is_stable_and_returns_independent_list(self):
        buffer = logs.LogBuffer()
        late = self.entry("late", timestamp="2026-11-10T12:01:00Z")
        early = self.entry("early", timestamp="2026-11-10T12:00:00Z")
        tied = self.entry("tied", timestamp=early.timestamp, source="podman")
        for entry in (late, early, tied):
            buffer.append(entry)
        result = buffer.search()
        self.assertEqual(result, [early, tied, late])
        result.clear()
        self.assertEqual(buffer.search(), [early, tied, late])
        with self.assertRaises(FrozenInstanceError):
            late.message = "changed"

    def test_invalid_buffer_inputs_do_not_replace_existing_tail(self):
        for value in (0, -1, True, 10001, 1.0, "2000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                logs.LogBuffer(value)
        buffer = logs.LogBuffer()
        original = self.entry("retained")
        buffer.append(original)
        with self.assertRaises(TypeError):
            buffer.replace("oc-box", [self.entry("new"), "not an entry"])
        self.assertEqual(buffer.search(), [original])
        with self.assertRaises(TypeError):
            buffer.append("not an entry")

    def test_timestamp_order_normalizes_timezones_and_fraction_precision(self):
        buffer = logs.LogBuffer()
        late = self.entry("late", timestamp="2026-11-10T12:00:00.123000001Z")
        early = self.entry("early", timestamp="2026-11-10T13:00:00.123+01:00")
        equivalent = self.entry(
            "equivalent", timestamp="2026-11-10T12:00:00.123000000Z"
        )
        for entry in (late, early, equivalent):
            buffer.append(entry)
        self.assertEqual(buffer.search(), [early, equivalent, late])

    def test_terminal_controls_and_field_sizes_are_sanitized(self):
        buffer = logs.LogBuffer()
        buffer.append(
            logs.LogEntry(
                "\x1b[31m2026\x1b[0m\r",
                "oc-box\x00",
                "info\u202e",
                "\x1b]0;title\x07\x1b[31mred\x1b[0m\r\b\x00\u202e\x9b32m green\ttext",
            )
        )
        entry = buffer.search()[0]
        self.assertEqual(
            (entry.timestamp, entry.source, entry.level), ("2026", "oc-box", "info")
        )
        self.assertEqual(entry.message, "red green text")
        buffer.replace("podman", [self.entry("x" * 10000)])
        self.assertLessEqual(
            len(buffer.search(source="podman")[0].message), logs._MAX_MESSAGE_CHARS + 1
        )

    def test_common_credentials_redacted_before_display_or_search(self):
        values = [
            ("Authorization: Bearer synthetic-header-value", "synthetic-header-value"),
            ('"Authorization": "Basic synthetic-basic-value"', "synthetic-basic-value"),
            ("Bearer synthetic-bearer-value", "synthetic-bearer-value"),
            ("PERSONAL_GITHUB_TOKEN=synthetic-token-value", "synthetic-token-value"),
            ("api_key='synthetic-key-value'", "synthetic-key-value"),
            ('"apiKey": "synthetic-json-value"', "synthetic-json-value"),
            ("password=synthetic-password-value", "synthetic-password-value"),
            (
                "https://user:synthetic-url-password@example.invalid/",
                "synthetic-url-password",
            ),
            (
                "https://example.invalid/?access_token=synthetic-query-value&ok=true",
                "synthetic-query-value",
            ),
            ("ghp_SYNTHETIC_GITHUB_TOKEN", "ghp_SYNTHETIC_GITHUB_TOKEN"),
            ("github_pat_SYNTHETIC_GITHUB_TOKEN", "github_pat_SYNTHETIC_GITHUB_TOKEN"),
            ("sk-synthetic-openai-token", "sk-synthetic-openai-token"),
            ("hf_SYNTHETICTOKEN", "hf_SYNTHETICTOKEN"),
            ("xoxb-synthetic-slack-token", "xoxb-synthetic-slack-token"),
            ("eyJsynthetic.payload.signature", "eyJsynthetic.payload.signature"),
        ]
        buffer = logs.LogBuffer()
        for message, secret in values:
            with self.subTest(message=message):
                buffer.replace("podman", [self.entry(message)])
                entry = buffer.search()[0]
                self.assertNotIn(secret, entry.message)
                self.assertIn("[REDACTED]", entry.message)
                self.assertEqual(buffer.search(secret), [])
                buffer.replace("podman", [entry])
                self.assertEqual(buffer.search()[0], entry)
        # Arbitrary prose secrets are not detectable. This is intentionally not
        # a promise of general secret removal.
        buffer.replace("podman", [self.entry("unrecognizable synthetic prose")])
        self.assertIn("unrecognizable synthetic prose", buffer.search()[0].message)


class ActivityEntriesTests(unittest.TestCase):
    def test_validated_activity_metadata_and_failure_levels(self):
        with tempfile.TemporaryDirectory(prefix="oc-box-log-render-") as temporary:
            root = Path(temporary) / "state"
            self.assertTrue(
                activity_log.record_event(
                    "command.started", command="start", workspace="a" * 32, root=root
                )
            )
            self.assertTrue(
                activity_log.record_event(
                    "command.failed",
                    command="start",
                    outcome="failure",
                    exit_code=1,
                    duration_ms=25,
                    root=root,
                )
            )
            self.assertTrue(
                activity_log.record_event(
                    "session.archived", workspace="b" * 32, root=root
                )
            )
            entries = logs.activity_entries(root)
            self.assertEqual(len(entries), 3)
            self.assertTrue(all(entry.source == "oc-box" for entry in entries))
            self.assertEqual(
                [entry.level for entry in entries], ["info", "error", "info"]
            )
            self.assertEqual(
                entries[1].message,
                "command.failed command=start outcome=failure exit_code=1 duration_ms=25",
            )
            self.assertEqual(logs.activity_entries(root, limit=1), entries[-1:])
            self.assertEqual(logs.activity_entries(root / "missing"), [])


class PodmanEntriesTests(unittest.TestCase):
    def setUp(self):
        self.data = {"id": "a" * 32, "container": "synthetic-managed-name"}
        self.env = {"PATH": "/synthetic/runtime", "HOME": "/synthetic/home"}
        self.info = {
            "Id": "b" * 64,
            "Config": {"Labels": {"io.oc-box.workspace": self.data["id"]}},
        }
        self.box = SimpleNamespace(
            maintenance_host_env=Mock(return_value=self.env),
            inspect_owned=Mock(return_value=self.info),
        )

    def test_owned_immutable_id_maintenance_environment_and_timestamp_parsing(self):
        text = "2026-11-10T12:00:00.123456789Z first\n2026-11-10T12:00:01+00:00 Authorization: Bearer synthetic-secret\nuntimed\n"
        with patch.object(
            logs, "_podman_output", return_value=(text, False)
        ) as capture:
            entries = logs.podman_entries(self.box, self.data, tail=2)
        self.box.inspect_owned.assert_called_once_with(self.data, env=self.env)
        capture.assert_called_once_with(
            ["logs", "--timestamps", "--tail", "2", "b" * 64], self.env
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].timestamp, "2026-11-10T12:00:01+00:00")
        self.assertEqual(entries[1].timestamp, "")
        self.assertNotIn("synthetic-secret", str(entries))
        self.assertTrue(all(entry.source == "podman" for entry in entries))

    def test_normal_ownership_helper_uses_only_readonly_inspection(self):
        from oc_box import oc_box as box

        result = subprocess.CompletedProcess([], 0, json.dumps([self.info]), "")
        with (
            patch.object(box, "maintenance_host_env", return_value=self.env),
            patch.object(box, "podman", return_value=result) as podman,
            patch.object(
                logs, "_podman_output", return_value=("synthetic line", False)
            ) as capture,
        ):
            entries = logs.podman_entries(box, self.data)
        podman.assert_called_once_with(
            "container",
            "inspect",
            self.data["container"],
            capture=True,
            check=False,
            env=self.env,
        )
        self.assertEqual(capture.call_args.args[0][-1], self.info["Id"])
        self.assertEqual(entries[0].message, "synthetic line")

    def test_invalid_ids_ownership_and_malformed_inspection_never_fetch_logs(self):
        cases = [
            {**self.info, "Id": "container-name"},
            {**self.info, "Id": "b" * 12},
            {**self.info, "Id": "b" * 64 + "\n"},
            {**self.info, "Config": {"Labels": {"io.oc-box.workspace": "c" * 32}}},
            {**self.info, "Config": {}},
            {**self.info, "Config": []},
            [],
        ]
        with patch.object(logs, "_podman_output") as capture:
            for value in cases:
                with self.subTest(value=value):
                    self.box.inspect_owned.return_value = value
                    entries = logs.podman_entries(self.box, self.data)
                    self.assertEqual(entries[0].message, "Podman logs unavailable.")
            capture.assert_not_called()
        self.box.inspect_owned.reset_mock()
        for data in ({"id": "not-an-id"}, {"id": "a" * 32 + "\n"}, {}, None):
            self.assertEqual(
                logs.podman_entries(self.box, data)[0].message,
                "Podman logs unavailable.",
            )
        self.box.inspect_owned.assert_not_called()

    def test_absent_container_and_invalid_tail_have_no_log_or_lifecycle_commands(self):
        self.box.inspect_owned.return_value = None
        with patch.object(logs, "_podman_output") as capture:
            self.assertEqual(logs.podman_entries(self.box, self.data), [])
            for tail in (0, -1, True, "500"):
                self.assertEqual(
                    logs.podman_entries(self.box, self.data, tail=tail), []
                )
            capture.assert_not_called()
        self.assertEqual(self.box.inspect_owned.call_count, 1)

    def test_backend_errors_never_expose_exception_or_error_output(self):
        private = "synthetic-private-backend-error"
        self.box.inspect_owned.side_effect = RuntimeError(private)
        with patch.object(logs, "_podman_output") as capture:
            self.assertEqual(
                logs.podman_entries(self.box, self.data), logs._unavailable()
            )
            capture.assert_not_called()
        self.box.inspect_owned.side_effect = None
        with patch.object(logs, "_podman_output", side_effect=RuntimeError(private)):
            entries = logs.podman_entries(self.box, self.data)
        self.assertEqual(entries, logs._unavailable())
        self.assertNotIn(private, str(entries))

    def test_requested_tail_is_capped_and_byte_truncation_is_explicit(self):
        with patch.object(
            logs, "_podman_output", return_value=("line", True)
        ) as capture:
            entries = logs.podman_entries(self.box, self.data, tail=1000000)
        self.assertEqual(capture.call_args.args[0][-2], "10000")
        self.assertEqual(entries[-1].level, "warning")
        self.assertEqual(
            entries[-1].message, "Podman log output truncated (byte limit)."
        )


class PodmanPipeTests(unittest.TestCase):
    """Use synthetic child processes only, never Podman or real home/log files."""

    def run_script(self, script, *, limit=1024 * 1024, timeout=3):
        real_popen = subprocess.Popen
        calls = []

        def synthetic_popen(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return real_popen([sys.executable, "-c", script], **kwargs)

        with (
            patch.object(logs.subprocess, "Popen", side_effect=synthetic_popen),
            patch.object(logs, "_MAX_OUTPUT_BYTES", limit),
            patch.object(logs, "_PODMAN_TIMEOUT", timeout),
        ):
            result = logs._podman_output(
                ["logs", "--timestamps", "--tail", "5", "b" * 64], {}
            )
        return result, calls

    def test_pipe_only_capture_preserves_stderr_log_content_and_replaces_bad_utf8(self):
        (output, truncated), calls = self.run_script(
            "import os; os.write(1, b'first\\n'); os.write(2, b'second\\xff\\n')"
        )
        self.assertEqual(output, "first\nsecond\ufffd\n")
        self.assertFalse(truncated)
        arguments, options = calls[0]
        self.assertEqual(arguments[:2], ["podman", "logs"])
        self.assertEqual(
            options,
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "env": {},
            },
        )

    def test_output_is_byte_bounded_even_for_one_unterminated_line(self):
        (output, truncated), _ = self.run_script(
            "import os; os.write(1, b'x' * 1000000)", limit=4096
        )
        self.assertEqual(len(output), 4096)
        self.assertTrue(truncated)

    def test_timeout_and_failure_do_not_return_private_partial_output(self):
        started = time.monotonic()
        with self.assertRaises(logs._LogUnavailable):
            self.run_script(
                "import os, time; os.write(2, b'synthetic-private-error'); time.sleep(20)",
                timeout=0.1,
            )
        self.assertLess(time.monotonic() - started, 3)
        with self.assertRaises(logs._LogUnavailable):
            self.run_script(
                "import os; os.write(2, b'synthetic-private-error'); raise SystemExit(4)"
            )


if __name__ == "__main__":
    unittest.main()
