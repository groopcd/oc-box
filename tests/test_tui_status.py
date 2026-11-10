import importlib.util
import math
import unittest

AVAILABLE = all(importlib.util.find_spec(name) for name in ("rich", "textual"))
if AVAILABLE:
    from oc_box import tui_status as status
    from oc_box.tui import DemoBackend


@unittest.skipUnless(AVAILABLE, "optional Textual dependencies are not installed")
class StatusMeterTests(unittest.TestCase):
    def test_constant_low_history_does_not_autoscale_to_full_height(self):
        values = status.spark([22] * 60, 12)
        self.assertEqual(len(values), 12)
        self.assertNotIn("█", values)
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(status.spark([0, None, 100], 3), "▁ █")
        self.assertEqual(status.spark([25], 0), "")

    def test_meters_share_numeric_and_bar_columns_and_keep_a_fixed_scale(self):
        rows = status.meters(
            [
                ("RAM", 25, "6 / 24 GiB", "blue"),
                ("CPU", 50, "50%", "green"),
                ("Home FS", 0, "0%", "yellow"),
            ],
            80,
        )
        self.assertEqual(
            [
                sum(
                    span.end - span.start
                    for span in row.spans
                    if str(span.style) in ("on blue", "on green", "on yellow")
                )
                for row in rows
            ],
            [10, 20, 0],
        )
        for row in rows:
            self.assertEqual(row.plain[8:48], " " * 40)
            self.assertEqual(row.plain[48:50], "  ")
            self.assertLessEqual(row.cell_len, 80)

    def test_missing_readings_are_not_measured_zero(self):
        row = status.meters([("CPU", None, "n/a", "green")], 60)[0]
        self.assertIn("·", row.plain)
        self.assertNotIn("█", row.plain)
        self.assertNotIn("0%", row.plain)
        self.assertIn("n/a", row.plain)
        self.assertEqual(status.spark([math.nan, math.inf, -1, True], 4), "    ")

    def test_workspace_cpu_bar_normalizes_to_observed_quota_not_one_core(self):
        item = DemoBackend().collect()["workspaces"][0]
        item["metrics"]["cpu_percent"] = 200
        rendered = status.workspace_report(item, width=100)
        cpu = next(row for row in rendered.split("\n") if row.plain.startswith("CPU "))
        self.assertIn("200.0% / 4 CPUs", cpu.plain)
        self.assertEqual(
            sum(
                span.end - span.start
                for span in cpu.spans
                if str(span.style) == "on green3"
            ),
            20,
        )

    def test_unlimited_workspace_limit_does_not_become_a_false_percentage(self):
        item = DemoBackend().collect()["workspaces"][0]
        item["effective_limits"].update(cpu_cores=0, memory_bytes=0)
        rendered = status.workspace_report(item, width=100)
        for row in rendered.split("\n"):
            if row.plain.startswith(("CPU ", "RAM ")):
                self.assertIn("unlimited", row.plain)
                self.assertNotIn("█", row.plain)
                self.assertIn("·", row.plain)

    def test_system_and_workspace_reports_are_cell_bounded_after_resize(self):
        report = DemoBackend().collect()
        item = report["workspaces"][0]
        item["project"] = "/projects/" + "中[bold]" * 40
        for width in (1, 16, 32, 48, 80, 140, 220):
            with self.subTest(width=width):
                for content in (
                    status.workspace_report(item, width=width),
                    status.system_report(
                        report["host"], [22] * 60, [38] * 60, width=width
                    ),
                    status.workspace_report(None, width=width),
                ):
                    self.assertTrue(
                        all(row.cell_len <= width for row in content.split("\n"))
                    )

    def test_zero_filesystem_capacity_is_not_described_as_unlimited_storage(self):
        content = status.system_report(
            {"filesystem": {"used_bytes": 0, "total_bytes": 0}}, width=100
        )
        self.assertNotIn("unlimited", content.plain)
        self.assertIn("n/a", content.plain)


if __name__ == "__main__":
    unittest.main()
