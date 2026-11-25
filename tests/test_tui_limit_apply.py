"""Synthetic local-engine/cgroup witnesses; never run real Podman or resize a host."""

from contextlib import contextmanager, ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import (
    tui_limit_apply as live,
    tui_limits,
    workspace_control,
    workspace_identity,
)


WORKSPACE, CONTAINER = "a" * 32, "b" * 64
STARTED = "2026-01-02T03:04:05.123456789Z"
GIB = tui_limits.GIB
LABEL = "io.oc-box.workspace"
read_live_cgroup = live._cgroup


class FixtureEngine:
    def __init__(self, data, cpu=4, memory=8 * GIB, swap=8 * GIB):
        self.calls = []
        self.scope = Mock(return_value=("fixture-scope",))
        self.observed = live._Live(
            ("pid-generation",), (cpu * 100_000, 100_000), memory, swap
        )
        name, home = data["container"], data["home_volume"]
        self.name = name
        private_home = "/synthetic/local-volume/_data"
        self.container = {
            "Id": CONTAINER,
            "Name": name,
            "Config": {"Labels": {LABEL: WORKSPACE}},
            "State": {
                "Status": "running",
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "Pid": 1234,
                "StartedAt": STARTED,
                "CgroupPath": "/fixture/libpod-" + CONTAINER + ".scope",
            },
            "Mounts": [
                {"Type": "bind", "Source": data["project"], "Destination": "/project"},
                {
                    "Type": "volume",
                    "Source": private_home,
                    "Name": home,
                    "Destination": data["home"],
                },
            ],
            "HostConfig": {
                "NanoCpus": 0,
                "CpuPeriod": 100_000,
                "CpuQuota": cpu * 100_000,
                "Memory": memory,
                "MemorySwap": memory + swap if swap >= 0 else -1,
            },
        }
        self.volume = {
            "Name": home,
            "Driver": "local",
            "Labels": {LABEL: WORKSPACE},
            "Mountpoint": private_home,
        }

    def inspect(self, kind, selector):
        if kind == "volume" and selector == self.volume["Name"]:
            return copy.deepcopy(self.volume)
        if kind == "container" and selector == self.name:
            return copy.deepcopy(self.container)
        raise AssertionError("Wrong inspect target")

    def update(self, ident, flags):
        self.calls.append((ident, tuple(flags)))
        cpu, memory, swap = self.observed.cpu, self.observed.memory, self.observed.swap
        limits = self.container["HostConfig"]
        for flag, value in (arg.split("=", 1) for arg in flags):
            if flag == "--cpus":
                quota = round(float(value) * 100_000)
                limits["CpuQuota"] = quota
                cpu = (quota, 100_000)
            elif flag == "--memory":
                memory = limits["Memory"] = int(value)
            elif flag == "--memory-swap":
                total = limits["MemorySwap"] = int(value)
                swap = -1 if total == -1 else total - memory
            else:
                raise AssertionError("Unexpected update option")
        self.observed = live._Live(self.observed.identity, cpu, memory, swap)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.work = ExitStack()
        self.addCleanup(self.work.close)
        temp = Path(self.work.enter_context(tempfile.TemporaryDirectory()))
        directory = temp / "state" / "workspaces" / WORKSPACE
        directory.mkdir(parents=True)
        project = temp / "project"
        project.mkdir()
        (project / ".oc-box-id").write_text(WORKSPACE + "\n")
        self.data = {
            "version": 2,
            "id": WORKSPACE,
            "project": str(project),
            "home": "/home/fixture",
            "distro": "debian",
            "image": "fixture",
            "container": "oc-box-" + WORKSPACE,
            "home_volume": "oc-box-home-" + WORKSPACE,
            "cpus": "3",
            "memory": "6g",
            "opencode_version": "2.4.1",
        }
        workspace_identity.record(self.data, *workspace_identity.snapshot(project))
        self.path = directory / "workspace.json"
        self.path.write_text(json.dumps(self.data))
        self.saved = self.path.read_bytes()
        workspace_control.capture(self.path)
        self.box = SimpleNamespace(
            state_root=lambda: temp / "state",
            validate_project=lambda path: path,
            container_project=lambda _: "/project",
        )
        self.engine = FixtureEngine(self.data)
        self.work.enter_context(patch.object(live, "_engine", self._engine))
        self.work.enter_context(
            patch.object(
                live, "_cgroup", side_effect=lambda *args: self.engine.observed
            )
        )
        self.base = tui_limits.LimitTarget(
            WORKSPACE, CONTAINER, STARTED, 400_000, 8 * GIB
        )

    @contextmanager
    def _engine(self, box, deadline):
        yield self.engine, self.engine.scope()

    def apply(self, draft, **mode):
        live.apply(self.box, draft, **{"demo": False, "read_only": False, **mode})

    def assert_refused(self, reason, draft, **mode):
        with self.assertRaises(tui_limits.LimitError) as caught:
            self.apply(draft, **mode)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.engine.calls, [])

    def test_apply_updates_exact_id_once_and_preserves_saved_limits_and_swap_allowance(
        self,
    ):
        draft = tui_limits.LimitDraft(self.base, cpu=600_000, memory=22 * GIB)
        self.apply(draft)
        self.assertEqual(
            self.engine.calls,
            [
                (
                    CONTAINER,
                    ("--cpus=6", f"--memory={22 * GIB}", f"--memory-swap={30 * GIB}"),
                )
            ],
        )
        self.assertEqual(self.path.read_bytes(), self.saved)
        self.assertEqual(self.engine.observed.swap, 8 * GIB)

    def test_single_setting_omits_other_flags_and_retains_unlimited_swap(self):
        self.apply(tui_limits.LimitDraft(self.base, cpu=450_000))
        self.assertEqual(self.engine.calls, [(CONTAINER, ("--cpus=4.5",))])
        self.engine = FixtureEngine(self.data, swap=-1)
        self.apply(tui_limits.LimitDraft(self.base, memory=7 * GIB))
        self.assertEqual(
            self.engine.calls,
            [(CONTAINER, (f"--memory={7 * GIB}", "--memory-swap=-1"))],
        )

    def test_mode_and_forged_target_refuse_before_registry_or_engine(self):
        draft = tui_limits.LimitDraft(self.base, cpu=450_000)
        with patch.object(live, "_registration") as registration:
            for mode in ({"read_only": True}, {"demo": True}):
                self.assert_refused("unsupported", draft, **mode)
            self.assert_refused(
                "unsupported", tui_limits.LimitDraft(self.base, cpu=400_001)
            )
            registration.assert_not_called()
        self.assertFalse(self.path.with_name(live._LOCK).exists())

    def test_busy_or_uncertain_control_and_concurrent_apply_do_not_dispatch(self):
        draft = tui_limits.LimitDraft(self.base, cpu=450_000)
        with workspace_control.admission(self.path, timeout=0):
            self.assert_refused("busy", draft)
        record, _ = live._registration(self.box, self.base)
        with live._serialize(record):
            self.assert_refused("busy", draft)
        workspace_control.mark_uncertain(self.path)
        self.assert_refused("busy", draft)

    def test_stale_resource_matrix_refuses_before_mutation(self):
        draft = tui_limits.LimitDraft(self.base, cpu=450_000)
        for key, value in (
            ("Id", "c" * 64),
            ("Name", "other"),
            ("State", {"Running": False, "Status": "exited"}),
            ("Config", {"Labels": {LABEL: "d" * 32}}),
            ("Mounts", []),
            (
                "HostConfig",
                {"CpuPeriod": 100_000, "CpuQuota": 500_000, "Memory": 8 * GIB},
            ),
        ):
            with self.subTest(key=key):
                self.engine = FixtureEngine(self.data)
                self.engine.container[key] = value
                self.assert_refused("stale", draft)
        self.engine = FixtureEngine(self.data)
        self.engine.container["State"]["StartedAt"] = "2026-01-02T03:04:06Z"
        self.assert_refused("stale", draft)

    def test_lowering_memory_refuses_an_unverifiable_swap_limit(self):
        self.engine.container["HostConfig"]["MemorySwap"] = 0
        self.assert_refused(
            "unsupported", tui_limits.LimitDraft(self.base, memory=7 * GIB)
        )

    def test_post_dispatch_refusals_never_retry_or_claim_no_change(self):
        draft = tui_limits.LimitDraft(self.base, cpu=450_000)
        for mode in ("command-error", "kernel-unchanged", "new-process", "stop-token"):
            with self.subTest(mode=mode):
                self.engine = FixtureEngine(self.data)
                real_update = self.engine.update

                def fail_after_update(ident, flags):
                    prior = self.engine.observed
                    real_update(ident, flags)
                    if mode == "command-error":
                        raise RuntimeError("PRIVATE_DIAGNOSTIC")
                    if mode == "kernel-unchanged":
                        self.engine.observed = prior
                    if mode == "new-process":
                        self.engine.container["State"]["Pid"] += 1
                        new = self.engine.observed
                        self.engine.observed = live._Live(
                            ("new-generation",), new.cpu, new.memory, new.swap
                        )
                    if mode == "stop-token":
                        workspace_control.request_stop(self.path)

                with patch.object(self.engine, "update", side_effect=fail_after_update):
                    with self.assertRaises(tui_limits.LimitError) as caught:
                        self.apply(draft)
                self.assertEqual(caught.exception.reason, "unverified")
                self.assertNotIn("PRIVATE_DIAGNOSTIC", str(caught.exception))
                self.assertEqual(self.engine.calls, [(CONTAINER, ("--cpus=4.5",))])
                self.assertEqual(self.path.read_bytes(), self.saved)

    def test_update_transport_requests_closed_terminal_and_forces_local_target(self):
        binary = Mock()
        with patch.object(
            live.workspace_podman, "_capture_pinned", return_value=(0, b"")
        ) as run:
            engine = live._Engine(binary, {"PATH": "/bin"}, 42)
            engine.update(CONTAINER, ("--cpus=4.5",))
        run.assert_called_once_with(
            binary,
            ("--remote=false", "update", "--cpus=4.5", "--", CONTAINER),
            {"PATH": "/bin"},
            42,
            4096,
            detach_terminal=True,
        )

    def test_cgroup_parser_binds_to_exact_pid_path_and_reads_real_fixture_limits(self):
        target_path = "/slice/libpod-" + CONTAINER + ".scope"
        orig_read = live._read
        path = self.path.parent
        (path / "cpu.max").write_text("450000 100000\n")
        (path / "memory.max").write_text(f"{7 * GIB}\n")
        (path / "memory.swap.max").write_text("max\n")

        @contextmanager
        def fake_path_fd(name, uid, *, directory):
            self.assertEqual(name, live._CGROUP_ROOT + target_path)
            self.assertTrue(directory)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                yield fd, os.fstat(fd)
            finally:
                os.close(fd)

        def read(fd, name, **options):
            return (
                f"0::{target_path}"
                if name == "cgroup"
                else orig_read(fd, name, **options)
            )

        with (
            patch.object(live.workspace_podman, "_path_fd", fake_path_fd),
            patch.object(live, "_read", side_effect=read),
            patch.object(
                live.history_overlap_domains, "observe_process_start", return_value=42
            ),
        ):
            result = read_live_cgroup(os.getpid(), target_path, CONTAINER)
            self.assertEqual(result.identity[:3], (os.getpid(), 42, target_path))
            self.assertEqual(
                (result.cpu_units(), result.memory, result.swap), (450_000, 7 * GIB, -1)
            )
            with self.assertRaises(tui_limits.LimitError):
                read_live_cgroup(os.getpid(), "/other/foreign.scope", CONTAINER)
