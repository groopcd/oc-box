"""Headless Status interactions for staged, live-only CPU and RAM limits."""

import asyncio
import copy
import importlib.util
import threading
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(name) for name in ("textual", "pyte"))
if AVAILABLE:
    from textual.widgets import Button, Collapsible, Select

    from oc_box.tui import DemoBackend
    from oc_box.tui_app import DashboardApp
    from oc_box.tui_limits import LimitDraft, LimitTarget
    from oc_box.tui_status import WorkspaceInspector


GIB = 1024**3
FIRST, SECOND = "a" * 32, "c" * 32
STARTED_AT = "2026-01-02T03:04:05Z"
SAVED_LIMITS = {"cpu_cores": "3", "memory_bytes": 6 * GIB, "pids": 2048}
STEP_IDS = tuple(
    f"limit-{resource}-{direction}"
    for resource in ("cpu", "memory")
    for direction in ("less", "more")
)


if AVAILABLE:

    class LiveLimitBackend(DemoBackend):
        def __init__(self):
            first = super().collect()["workspaces"][0]
            first.update(started_at=STARTED_AT, configured_limits=dict(SAVED_LIMITS))
            first["metrics"]["cpu_percent"] = 200
            second = copy.deepcopy(first)
            second.update(
                workspace_id=SECOND,
                project="/projects/another-workspace",
                container_id="d" * 64,
            )
            self.items = {FIRST: first, SECOND: second}
            self.calls = []
            self.apply_entered, self.release_apply = (
                threading.Event(),
                threading.Event(),
            )
            self.reject_apply = False

        def collect(self, *, scan_storage=False):
            report = super().collect(scan_storage=scan_storage)
            report["workspaces"] = copy.deepcopy(list(self.items.values()))
            return report

        def apply_live_limits(self, draft, *, read_only, demo):
            self.calls.append((draft, read_only, demo))
            self.apply_entered.set()
            if not self.release_apply.wait(10):
                raise TimeoutError("synthetic Apply gate timed out")
            if self.reject_apply:
                raise RuntimeError("PRIVATE_SYNTHETIC_ENGINE_ERROR")
            item = self.items[draft.target.workspace]
            changes = {}
            if draft.cpu is not None:
                changes["cpu_cores"] = draft.cpu / 100_000
            if draft.memory is not None:
                changes["memory_bytes"] = draft.memory
            for key in ("limits", "effective_limits"):
                item[key].update(changes)


@unittest.skipUnless(AVAILABLE, "optional dashboard dependencies unavailable")
class LiveLimitStatusTests(unittest.IsolatedAsyncioTestCase):
    async def settle(self, app, pilot):
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=5)
        await pilot.pause()

    @asynccontextmanager
    async def dashboard(self, backend, **mode):
        app = DashboardApp(backend, selected=FIRST, **mode)
        with patch.object(app, "_tick"):
            async with app.run_test(size=(180, 50)) as pilot:
                try:
                    await self.settle(app, pilot)
                    yield app, pilot
                finally:
                    backend.release_apply.set()

    def button(self, app, ident="apply-live-limits"):
        return app.query_one(f"#{ident}", Button)

    async def click(self, app, pilot, ident):
        button = self.button(app, ident)
        self.assertFalse(button.disabled, ident)
        button.scroll_visible(animate=False)
        await pilot.pause()
        self.assertTrue(await pilot.click(button), ident)
        await pilot.pause()

    def resource_rows(self, app):
        rows = app.query_one(WorkspaceInspector).render().split("\n")
        # Skip the separate CPU scale legend.
        return (
            next(row for row in rows if row.plain.startswith("│ CPU 200.0%")),
            next(row for row in rows if "% of quota" in row.plain),
            next(row for row in rows if row.plain.startswith("│ RAM ")),
            next(row for row in rows if "% of limit" in row.plain),
        )

    async def test_staged_values_leave_observed_gauges_and_apply_once_to_exact_target(
        self,
    ):
        backend = LiveLimitBackend()
        async with self.dashboard(backend) as (app, pilot):
            apply = self.button(app)
            scan = self.button(app, "scan-storage")
            self.assertIs(apply.parent, scan.parent)
            self.assertEqual(apply.region.y, scan.region.y)
            self.assertGreaterEqual(apply.region.x, scan.region.right)
            children = list(apply.parent.children)
            self.assertIs(children[children.index(scan) + 1], apply)
            self.assertTrue(apply.disabled)

            _, baseline_cpu, _, baseline_ram = self.resource_rows(app)
            self.assertIn("50% of quota", baseline_cpu.plain)
            self.assertIn("25% of limit", baseline_ram.plain)
            await self.click(app, pilot, "limit-cpu-less")
            self.assertIn("[3.5]", self.resource_rows(app)[0].plain)
            await self.click(app, pilot, "limit-cpu-more")
            self.assertTrue(apply.disabled)
            self.assertNotIn("[", self.resource_rows(app)[0].plain)
            await self.click(app, pilot, "limit-memory-more")
            self.assertIn("[9]", self.resource_rows(app)[2].plain)
            await self.click(app, pilot, "limit-memory-less")
            self.assertTrue(apply.disabled)
            self.assertNotIn("[", self.resource_rows(app)[2].plain)
            await self.click(app, pilot, "limit-cpu-more")
            await self.click(app, pilot, "limit-memory-less")
            await self.click(app, pilot, "limit-cpu-less")
            self.assertNotIn("[", self.resource_rows(app)[0].plain)
            self.assertIn("[7]", self.resource_rows(app)[2].plain)
            self.assertFalse(apply.disabled)
            await self.click(app, pilot, "limit-cpu-more")

            cpu, cpu_bar, ram, ram_bar = self.resource_rows(app)
            self.assertIn("4 CPUs quota [4.5]", cpu.plain)
            self.assertIn("2.0 GiB / 8.0 GiB [7]", ram.plain)
            for staged, baseline in ((cpu_bar, baseline_cpu), (ram_bar, baseline_ram)):
                self.assertEqual(staged.plain, baseline.plain)
                self.assertEqual(
                    [(span.start, span.end) for span in staged.spans],
                    [(span.start, span.end) for span in baseline.spans],
                )
            self.assertEqual(backend.calls, [])

            await self.click(app, pilot, "apply-live-limits")
            self.assertTrue(await asyncio.to_thread(backend.apply_entered.wait, 3))
            expected = LimitDraft(
                LimitTarget(FIRST, "b" * 64, STARTED_AT, 400_000, 8 * GIB),
                cpu=450_000,
                memory=7 * GIB,
            )
            self.assertEqual(backend.calls, [(expected, False, False)])
            for ident in STEP_IDS:
                self.assertTrue(self.button(app, ident).disabled)
            self.assertTrue(apply.disabled)
            self.assertEqual(apply.label.plain, "Applying…")
            self.assertFalse(scan.disabled)
            self.assertEqual(scan.label.plain, "Scan sizes")
            cpu, cpu_bar, ram, ram_bar = self.resource_rows(app)
            self.assertNotIn("[", cpu.plain + ram.plain)
            self.assertEqual(cpu_bar.plain, baseline_cpu.plain)
            self.assertEqual(ram_bar.plain, baseline_ram.plain)
            # A concurrent status observation can see the write before the
            # detached operation's verification has finished.
            early = backend.collect()
            early["workspaces"][0]["effective_limits"]["cpu_cores"] = 4.5
            app._apply_report(early, False, app._limit_revision)
            self.assertEqual(self.resource_rows(app)[1].plain, baseline_cpu.plain)
            self.assertEqual(app.query_one("#content").active, "status")
            await pilot.click(apply)
            backend.release_apply.set()
            await self.settle(app, pilot)

            self.assertEqual(backend.calls, [(expected, False, False)])
            self.assertEqual(apply.label.plain, "Apply")
            self.assertTrue(apply.disabled)
            cpu, cpu_bar, ram, ram_bar = self.resource_rows(app)
            self.assertIn("4.5 CPUs quota", cpu.plain)
            self.assertIn("2.0 GiB / 7.0 GiB", ram.plain)
            self.assertIn("44% of quota", cpu_bar.plain)
            self.assertIn("29% of limit", ram_bar.plain)
            reports = {item["workspace_id"]: item for item in app.report["workspaces"]}
            self.assertEqual(reports[FIRST]["configured_limits"], SAVED_LIMITS)
            self.assertEqual(backend.items[FIRST]["configured_limits"], SAVED_LIMITS)
            self.assertEqual(reports[SECOND]["effective_limits"]["cpu_cores"], 4)
            self.assertEqual(
                reports[SECOND]["effective_limits"]["memory_bytes"], 8 * GIB
            )

    async def test_selection_and_changed_observed_baseline_discard_the_draft(self):
        backend = LiveLimitBackend()
        async with self.dashboard(backend) as (app, pilot):
            picker = app.query_one("#workspace-picker", Select)
            apply = self.button(app)
            await self.click(app, pilot, "limit-cpu-more")
            await self.click(app, pilot, "limit-memory-less")
            for target in (SECOND, FIRST):
                picker.value = target
                await pilot.pause()
                self.assertEqual(app.selected, target)
                self.assertTrue(apply.disabled)
                self.assertNotIn("[", self.resource_rows(app)[0].plain)
                self.assertNotIn("[", self.resource_rows(app)[2].plain)

            await self.click(app, pilot, "limit-cpu-more")
            app.refresh_report()
            await self.settle(app, pilot)
            self.assertFalse(apply.disabled)
            self.assertIn("[4.5]", self.resource_rows(app)[0].plain)
            backend.items[FIRST]["effective_limits"]["cpu_cores"] = 5
            app.refresh_report()
            await self.settle(app, pilot)
            self.assertTrue(apply.disabled)
            self.assertNotIn("[", self.resource_rows(app)[0].plain)
            await self.click(app, pilot, "limit-cpu-more")
            self.assertIn("[5.5]", self.resource_rows(app)[0].plain)
            self.assertEqual(backend.calls, [])

    async def test_compact_inspector_keeps_repeatable_steps_and_apply_reachable(self):
        backend = LiveLimitBackend()
        async with self.dashboard(backend) as (app, pilot):
            await pilot.resize_terminal(80, 24)
            app.query_one("#workspace-inspector", Collapsible).collapsed = False
            await pilot.pause()
            button = self.button(app, "limit-cpu-more")
            button.scroll_visible(animate=False)
            await pilot.pause()
            for _ in range(3):
                self.assertTrue(await pilot.click(button))
            self.assertIn("4 CPUs quota [5.5]", self.resource_rows(app)[0].plain)
            apply = self.button(app)
            apply.scroll_visible(animate=False)
            await pilot.pause()
            placement = app.screen.find_widget(apply)
            self.assertEqual(
                placement.region.intersection(placement.clip), placement.region
            )
            self.assertFalse(apply.disabled)
            self.assertEqual(backend.calls, [])

    async def test_read_only_and_demo_disable_every_live_limit_control(self):
        for mode in ({"read_only": True}, {"demo": True}):
            with self.subTest(mode=mode):
                backend = LiveLimitBackend()
                async with self.dashboard(backend, **mode) as (app, pilot):
                    for ident in (*STEP_IDS, "apply-live-limits"):
                        self.assertTrue(self.button(app, ident).disabled, ident)
                    await pilot.click("#limit-cpu-more")
                    await pilot.click("#apply-live-limits")
                    self.assertEqual(backend.calls, [])

    async def test_stopped_unknown_and_unlimited_observations_disable_correct_steps(
        self,
    ):
        backend = LiveLimitBackend()
        item = backend.items[FIRST]
        async with self.dashboard(backend) as (app, pilot):
            for state, cpu, memory, cpu_disabled, memory_disabled in (
                ("stopped", 4, 8 * GIB, True, True),
                ("running", None, 8 * GIB, True, False),
                ("running", 0, 8 * GIB, True, False),
                ("running", 4, None, False, True),
                ("running", 4, 0, False, True),
                ("running", None, 0, True, True),
            ):
                with self.subTest(state=state, cpu=cpu, memory=memory):
                    item.update(state=state)
                    item["effective_limits"].update(cpu_cores=cpu, memory_bytes=memory)
                    app.refresh_report()
                    await self.settle(app, pilot)
                    for resource, disabled in (
                        ("cpu", cpu_disabled),
                        ("memory", memory_disabled),
                    ):
                        for direction in ("less", "more"):
                            button = self.button(app, f"limit-{resource}-{direction}")
                            self.assertEqual(button.disabled, disabled, button.id)
                    self.assertTrue(self.button(app).disabled)
            self.assertEqual(backend.calls, [])

    async def test_uncertain_result_keeps_prior_observations_and_requires_fresh_status(
        self,
    ):
        backend = LiveLimitBackend()
        backend.reject_apply = True
        async with self.dashboard(backend) as (app, pilot):
            await self.click(app, pilot, "limit-cpu-more")
            await self.click(app, pilot, "apply-live-limits")
            backend.release_apply.set()
            await self.settle(app, pilot)
            self.assertEqual(len(backend.calls), 1)
            cpu, gauge, _, _ = self.resource_rows(app)
            self.assertIn("4 CPUs quota", cpu.plain)
            self.assertIn("50% of quota", gauge.plain)
            self.assertNotIn("[", cpu.plain)
            self.assertNotIn(
                "PRIVATE_SYNTHETIC_ENGINE_ERROR",
                app.query_one("#notice").render().plain,
            )
            for ident in (*STEP_IDS, "apply-live-limits"):
                self.assertTrue(self.button(app, ident).disabled, ident)
            app.refresh_report()
            await self.settle(app, pilot)
            self.assertFalse(self.button(app, "limit-cpu-more").disabled)
            self.assertTrue(self.button(app).disabled)
            self.assertEqual(len(backend.calls), 1)
