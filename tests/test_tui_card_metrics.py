import copy
import importlib.util
import math
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("rich", "textual"))
if AVAILABLE:
    from rich.cells import cell_len
    from rich.style import Style
    from textual.app import App, ComposeResult

    from oc_box import tui_status as status

    class CardApp(App):
        CSS = """
        Screen { layout: vertical; overflow: hidden; }
        """

        def compose(self) -> ComposeResult:
            yield status.HostResourceCards()
            yield status.WorkspaceInspector()


GIB = 1024**3
WIDTHS = (1, 16, 32, 60, 80, 104, 140, 220)


def host_snapshot():
    return {
        "cpu_percent": 22,
        "memory_bytes": 6 * GIB,
        "memory_total_bytes": 16 * GIB,
        "filesystem": {"used_bytes": 90 * GIB, "total_bytes": 256 * GIB},
        "workspace_counts": {"running": 2, "registered": 5},
    }


def workspace_snapshot():
    return {
        "project": "/projects/example",
        "state": "running",
        "distro": "debian",
        "metrics": {"cpu_percent": 200, "memory_bytes": 2 * GIB, "pids": 32},
        "effective_limits": {"cpu_cores": 4, "memory_bytes": 8 * GIB},
        "recorded_sessions": 12,
        "storage": {"private_home_bytes": None},
    }


@unittest.skipUnless(AVAILABLE, "optional Textual dependencies are not installed")
class CardValueTests(unittest.TestCase):
    def test_numeric_gauges_reject_invalid_values_and_cap_finite_ratios(self):
        for value in (None, math.nan, math.inf, -1, True, "12", 10**400):
            with self.subTest(value=value):
                self.assertIsNone(status._card_percentage(value, 100))
                self.assertIsNone(status._card_percentage(10, value))
        self.assertIsNone(status._card_percentage(0, 0))
        self.assertEqual(status._card_percentage(0, 100), 0)
        self.assertEqual(status._card_percentage(200, 400), 50)
        self.assertEqual(status._card_percentage(1e308, 1e-308), 100)
        self.assertEqual(status._card_percentage(1e308, 1e308), 100)

    def test_metadata_counts_require_nonnegative_exact_integers(self):
        for value in (None, True, False, -1, 2.0, math.nan, "2", 10**400):
            with self.subTest(value=value):
                self.assertEqual(status._card_count(value), "n/a")
        self.assertEqual(status._card_count(0), "0")
        self.assertEqual(status._card_count(5), "5")

    def test_unknown_zero_and_unlimited_capacity_stay_distinct(self):
        self.assertEqual(status._card_memory(0, 0), "0 B / n/a")
        self.assertEqual(status._card_memory(0, 0, unlimited=True), "0 B / unlimited")
        self.assertEqual(status._card_memory(math.nan, 0, unlimited=True), "n/a")
        for invalid in (math.nan, math.inf, -1, True):
            self.assertEqual(status._card_memory(GIB, invalid), "1.0 GiB / n/a")

    def test_compact_bar_is_cell_bounded_and_preserves_fixed_scale(self):
        for width in WIDTHS:
            for value in (None, math.nan, 0, 22, 100, 200):
                with self.subTest(width=width, value=value):
                    row = status._card_bar(value, width, "green", "dim", "n/a")
                    self.assertLessEqual(row.cell_len, width)
        half = status._card_bar(50, 20, "green", "dim")
        self.assertEqual(half.plain, "█" * 10 + "░" * 10)
        unknown = status._card_bar(None, 20, "green", "dim")
        self.assertEqual(unknown.plain, "·" * 20)
        self.assertEqual(status._card_bar(200, 20, "green", "dim").plain, "█" * 20)


@unittest.skipUnless(AVAILABLE, "optional Textual dependencies are not installed")
class CardWidgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_widgets_resize_without_children_or_snapshot_mutation(self):
        host, workspace = host_snapshot(), workspace_snapshot()
        workspace["project"] = "/projects/" + "中[bold]" * 80
        original = copy.deepcopy((host, workspace))
        app = CardApp()
        async with app.run_test(size=(140, 48)) as pilot:
            cards = app.query_one("#system-report", status.MonitorPanel)
            inspector = app.query_one("#full-diagnostics", status.MonitorPanel)
            cards.update_snapshot(host, cpu_history=[22] * 60, ram_history=[38] * 60)
            inspector.update_snapshot(workspace)
            for width in WIDTHS:
                await pilot.resize_terminal(width, 48)
                await pilot.pause()
                for widget in (cards, inspector):
                    with self.subTest(width=width, widget=type(widget).__name__):
                        content = widget.render()
                        self.assertTrue(
                            all(
                                cell_len(row.plain) <= width
                                for row in content.split("\n")
                            )
                        )
                        self.assertEqual(len(widget.children), 0)
                lines = cards.render().plain.splitlines()
                if width >= 104:
                    self.assertEqual(len(lines), 6)
                    for title in ("HOST CPU", "HOST RAM", "HOST HOME FS", "WORKSPACES"):
                        self.assertIn(title, lines[0])
                else:
                    self.assertLessEqual(len(lines), 6)
                    if width >= 60:
                        self.assertIn("CPU 22.0% whole host", lines[1])
                        self.assertIn("RAM 6.0 GiB / 16.0 GiB", lines[1])
                        self.assertIn("0–100%; gaps=n/a", lines[-2])
                        self.assertIn("Host resources (OS-reported)", lines[0])
                    if width == 80:
                        self.assertEqual(len(lines), 5)
                        self.assertIn("Home FS", lines[2])
                        self.assertIn("Workspaces 2/5 running/registered", lines[2])
            self.assertEqual((host, workspace), original)
            self.assertEqual(cards.cpu_history, (22,) * 60)
            self.assertEqual(cards.caption, "Host resources (OS-reported)")
            self.assertEqual(inspector.caption, "Selected workspace")

    async def test_cpu_uses_observed_quota_and_private_size_requires_scan(self):
        app = CardApp()
        async with app.run_test(size=(104, 48)):
            inspector = app.query_one(status.WorkspaceInspector)
            item = workspace_snapshot()
            # Configured/requested values must not replace observed limits.
            item["configured_limits"] = {"cpu_cores": 1, "memory_bytes": GIB}
            inspector.update_snapshot(item)
            content = inspector.render()
            self.assertIn("CPU 200.0% · 4 CPUs quota", content.plain)
            self.assertIn("CPU 100% = 1 core; gauge uses observed quota", content.plain)
            cpu_bar = next(
                row for row in content.split("\n") if "50% of quota" in row.plain
            )
            self.assertEqual(cpu_bar.plain.count("█"), 16)
            self.assertEqual(cpu_bar.plain.count("░"), 16)
            self.assertIn("25% of limit", content.plain)
            self.assertIn("Processes 32", content.plain)
            self.assertIn("Recorded sessions 12", content.plain)
            self.assertNotIn("active", content.plain.lower())
            self.assertNotIn("jobs", content.plain.lower())
            self.assertIn("Private home n/a · scan required", content.plain)
            item["storage"]["private_home_bytes"] = 0
            inspector.update_snapshot(item)
            self.assertIn("Private home 0 B · scanned", inspector.render().plain)
            item["storage"]["private_home_bytes"] = 4 * GIB
            inspector.update_snapshot(item)
            self.assertIn("Private home 4.0 GiB · scanned", inspector.render().plain)
            item["effective_limits"] = None
            inspector.update_snapshot(item)
            self.assertIn("quota n/a", inspector.render().plain)
            self.assertIn("2.0 GiB / n/a", inspector.render().plain)
            self.assertNotIn("50% of quota", inspector.render().plain)

    async def test_host_counts_unknowns_and_histories_do_not_invent_storage_history(
        self,
    ):
        app = CardApp()
        async with app.run_test(size=(140, 48)) as pilot:
            cards = app.query_one(status.HostResourceCards)
            cards.update_snapshot(host_snapshot(), [22, None, 100], [0, math.nan, 38])
            content = cards.render().plain
            self.assertIn("2 / 5", content)
            self.assertIn("running / registered", content)
            self.assertIn("whole host", content)
            self.assertIn("OS-reported capacity", content)
            self.assertIn("not oc-box allocation", content)
            self.assertIn("CPU ▃ █", content)
            self.assertIn("RAM ▁ ▄", content)
            self.assertIn("0–100%", content)
            self.assertEqual(
                sum(char in status.SPARKS for char in content.replace("█", "")), 3
            )
            for invalid in (None, True, -1, math.nan):
                host = host_snapshot()
                host["cpu_percent"] = host["memory_bytes"] = invalid
                host["filesystem"] = {"used_bytes": 0, "total_bytes": 0}
                host["workspace_counts"] = {"running": invalid}
                cards.update_snapshot(host)
                content = cards.render().plain
                self.assertIn("n/a / n/a", content)
                self.assertIn("0 B / n/a", content)
                self.assertNotIn("unlimited", content)
                self.assertNotIn("0.0%", content)
            await pilot.resize_terminal(80, 48)
            await pilot.pause()
            cards.update_snapshot(host_snapshot(), [22, None, 100], [0, math.nan, 38])
            self.assertIn("Workspaces 2/5 running/registered", cards.render().plain)
            self.assertIn("CPU ▃ █", cards.render().plain)
            self.assertIn("RAM ▁ ▄", cards.render().plain)
            cards.update_snapshot({})
            self.assertIn("Workspaces n/a/n/a running/registered", cards.render().plain)

    async def test_literal_metadata_and_no_unrequested_configuration_details(self):
        app = CardApp()
        async with app.run_test(size=(220, 48)):
            inspector = app.query_one(status.WorkspaceInspector)
            item = workspace_snapshot()
            item.update(
                project="/private/parents/project[bold]中[red]",
                distro="test[italic]\x1bOS",
                opencode_version="2.2.6[link=x]",
                auth="PRIVATE_AUTH_SENTINEL",
                mounts=[{"source": "PRIVATE_MOUNT_SENTINEL"}],
                container_project="/PRIVATE_INTERNAL_SENTINEL",
            )
            inspector.update_snapshot(item)
            content = inspector.render()
            self.assertIn("project[bold]中[red]", content.plain)
            self.assertIn("test[italic] OS", content.plain)
            self.assertIn("OpenCode 2.2.6[link=x]", content.plain)
            self.assertNotIn("/private/parents", content.plain)
            self.assertNotIn("PRIVATE_", content.plain)
            self.assertNotIn("\x1b", content.plain)
            for span in content.spans:
                style = (
                    Style.parse(span.style)
                    if isinstance(span.style, str)
                    else span.style
                )
                self.assertIsNone(style.link)
            inspector.update_snapshot(None)
            self.assertIn("No workspace selected.", inspector.render().plain)
            self.assertNotIn("OpenCode", inspector.render().plain)

    async def test_unlimited_limits_unknown_metrics_and_theme_components(self):
        app = CardApp()
        async with app.run_test(size=(104, 48)) as pilot:
            inspector = app.query_one(status.WorkspaceInspector)
            cards = app.query_one(status.HostResourceCards)
            item = workspace_snapshot()
            item["effective_limits"].update(cpu_cores=0, memory_bytes=0)
            inspector.update_snapshot(item)
            content = inspector.render().plain
            self.assertIn("CPU 200.0% · unlimited", content)
            self.assertIn("RAM 2.0 GiB / unlimited", content)
            self.assertIn("····", content)
            self.assertNotIn("█", content)
            for invalid in (None, math.nan, math.inf, True):
                item["metrics"] = {
                    "cpu_percent": invalid,
                    "memory_bytes": invalid,
                    "pids": invalid,
                }
                item["recorded_sessions"] = invalid
                inspector.update_snapshot(item)
                self.assertIn("CPU n/a", inspector.render().plain)
                self.assertIn("RAM n/a", inspector.render().plain)
                self.assertIn("Processes n/a", inspector.render().plain)
                self.assertIn("Recorded sessions n/a", inspector.render().plain)
            item = workspace_snapshot()
            for theme in ("textual-light", "textual-dark"):
                app.theme = theme
                await pilot.pause()
                with patch.object(
                    cards,
                    "get_component_rich_style",
                    wraps=cards.get_component_rich_style,
                ) as components:
                    cards.update_snapshot(host_snapshot())
                names = {call.args[0] for call in components.call_args_list}
                self.assertTrue(
                    {
                        f"card--{name}"
                        for name in (
                            "title",
                            "muted",
                            "cpu",
                            "memory",
                            "storage",
                            "border",
                        )
                    }
                    <= names
                )
                inspector.update_snapshot(item)
                self.assertIn("50% of quota", inspector.render().plain)


if __name__ == "__main__":
    unittest.main()
