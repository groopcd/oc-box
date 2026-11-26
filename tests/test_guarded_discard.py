import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import (
    entrypoint_migration as migration,
    guarded_discard as discard,
    native_runtime_guard,
    oc_box as box,
    workspace_control,
)


OLD = "a" * 64
OTHER = "b" * 64
LAYER = "c" * 64
CREATED = "2026-01-01T00:00:00Z"
ENGINE_CLASS = discard.Engine


class FixtureEngine:
    def __init__(self, scope, container, volume):
        self.binding = scope
        self.containers = {OLD: container}
        self.volumes = {volume["Name"]: volume}
        self.removals = []
        self.lost_ack = None
        self.unverified_absence = None
        self.mount_report = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def scope(self):
        return self.binding

    def items(self, kind):
        return self.containers if kind == "container" else self.volumes

    def catalog(self, kind):
        return tuple(sorted(self.items(kind)))

    def inspect(self, kind, selectors):
        return tuple(deepcopy(self.items(kind)[key]) for key in selectors)

    def exists(self, kind, selector, *, present):
        items = self.items(kind)
        actual = (
            selector in items
            or kind == "container"
            and any(row["Name"] == selector for row in items.values())
        )
        if actual != present:
            raise discard.Refused("Exact existence changed.")

    def mounted(self, rows):
        return self.mount_report

    def check_namespace(self, targets):
        pass

    def remove(self, kind, selector):
        self.removals.append((kind, selector))
        if self.unverified_absence != kind:
            row = self.items(kind).pop(selector)
            path = Path(
                row["GraphDriver"]["Data"]["UpperDir"]
                if kind == "container"
                else row["Mountpoint"]
            )
            path.rmdir()
            path.parent.rmdir()
        if self.lost_ack == kind:
            raise discard.Refused("Podman removal was not confirmed.")


class GuardedDiscardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ocbox-guarded-discard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ, {"OC_BOX_STATE_HOME": str(self.root / "state")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.project / "keep-this"
        self.source.write_text("synthetic original user data")
        self.path, self.data = box.create_workspace(self.project, "tumbleweed")
        self.ident = self.data["id"]
        self.name = self.data["home_volume"]
        self.archived = self.path.parent / "history"
        self.archived.mkdir(mode=0o700)
        (self.archived / "fixture.json").write_text("synthetic saved host archive")
        self.guard = native_runtime_guard.Record(
            OLD, self.name, "2.4.1", "d" * 64, False
        )
        native_runtime_guard.publish(self.path, None, self.guard)
        workspace_control.capture(self.path)
        with workspace_control.admission(self.path):
            workspace_control.mark_uncertain(self.path)
        self.control = workspace_control.disposal_observation(self.path)
        graph, run = self.root / "graph", self.root / "run"
        volumes = graph / "volumes"
        home = volumes / self.name / "_data"
        upper = graph / "overlay" / LAYER / "diff"
        for directory in (run, home, upper):
            directory.mkdir(parents=True, mode=0o700)
        self.home = home
        scope = tuple(
            (str(item), discard._identity(item)) for item in (graph, run, volumes)
        )
        self.old = {
            "Id": OLD,
            "Name": self.data["container"],
            "IsInfra": False,
            "IsService": False,
            "State": {
                "Status": "created",
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "Pid": 0,
                "ConmonPid": 0,
                "StartedAt": "0001-01-01T00:00:00Z",
            },
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "Config": {"Labels": {box.LABEL: self.ident, "io.oc-box.managed": "true"}},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(self.project),
                    "Destination": str(self.project),
                },
                {
                    "Type": "volume",
                    "Source": str(home),
                    "Destination": self.data["home"],
                    "Name": self.name,
                },
            ],
            "GraphDriver": {"Name": "overlay", "Data": {"UpperDir": str(upper)}},
        }
        self.volume = {
            "Name": self.name,
            "Driver": "local",
            "Scope": "local",
            "Options": {},
            "CreatedAt": CREATED,
            "Labels": {box.LABEL: self.ident},
            "Mountpoint": str(home),
            "MountCount": 0,
        }
        self.engine = FixtureEngine(scope, self.old, self.volume)
        self.retired = box.state_root() / "retired" / self.ident
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(discard, "_require_host"))
        self.stack.enter_context(
            patch.object(discard, "Engine", return_value=self.engine)
        )
        self.stack.enter_context(
            patch.object(
                discard.workspace_service, "controller_running", return_value=False
            )
        )
        self.confirm = self.stack.enter_context(
            patch.object(discard.uninstall_tty, "confirm_review", return_value=True)
        )
        for method in (
            "run",
            "launch_credentials",
            "sync_sessions",
            "start_container",
            "stop_container",
        ):
            self.stack.enter_context(
                patch.object(box, method, side_effect=AssertionError(method))
            )
        self.output = self.stack.enter_context(
            contextlib.redirect_stdout(io.StringIO())
        )

    def run_discard(self):
        return discard.discard(box, self.ident)

    def assert_original_evidence(self):
        self.assertEqual(self.source.read_text(), "synthetic original user data")
        self.assertEqual((self.project / box.MARKER).read_text(), self.ident + "\n")

    def test_exact_confirmed_cleanup_keeps_full_evidence_and_does_not_reuse_fence(self):
        manifest = self.path.read_bytes()
        self.assertEqual(self.run_discard(), 0)
        review, phrase = self.confirm.call_args.args
        self.assertIn(OLD, review)
        self.assertIn(self.name, review)
        self.assertIn("No final sync", review)
        self.assertEqual(phrase, f"DISCARD {self.ident}")
        self.assertEqual(
            self.engine.removals, [("container", OLD), ("volume", self.name)]
        )
        self.assertEqual(self.path.read_bytes(), manifest)
        self.assertFalse(self.retired.exists())
        self.assertEqual(
            workspace_control.disposal_observation(self.path), self.control
        )
        self.assertEqual(native_runtime_guard.load(self.path), self.guard)
        self.assertEqual(
            (self.archived / "fixture.json").read_text(),
            "synthetic saved host archive",
        )
        self.assert_original_evidence()

    def test_confirmation_is_mandatory(self):
        self.confirm.return_value = False
        with self.assertRaisesRegex(box.BoxError, "not confirmed"):
            self.run_discard()
        self.assertEqual(self.engine.removals, [])
        self.assertTrue(self.path.is_file())
        self.assertFalse(self.retired.exists())
        self.assert_original_evidence()

    def test_empty_store_cannot_authorize_disposal_or_change_registration(self):
        self.engine.containers.clear()
        self.engine.volumes.clear()
        with self.assertRaisesRegex(box.BoxError, "empty store"):
            self.run_discard()
        self.assertEqual(self.engine.removals, [])
        self.assertTrue(self.path.is_file())
        self.confirm.assert_not_called()
        self.assert_original_evidence()

    def test_volume_without_container_needs_the_original_receipt_identity(self):
        del self.engine.containers[OLD]
        with self.assertRaisesRegex(box.BoxError, "filesystem identity"):
            self.run_discard()
        self.assertEqual(self.engine.removals, [])
        self.data[migration.RECEIPT] = {
            "id": OLD,
            "bootstrap": False,
            "volume": list(discard._identity(self.home)[:2]),
        }
        box.atomic_json(self.path, self.data)
        self.assertEqual(self.run_discard(), 0)
        self.assertEqual(self.engine.removals, [("volume", self.name)])

    def test_strong_receipt_never_falls_back_to_matching_device_and_inode(self):
        del self.engine.containers[OLD]
        identity = list(discard._identity(self.home)[:2])
        self.data[migration.RECEIPT] = {
            "id": OLD,
            "bootstrap": False,
            "volume": identity,
        }
        stable = {
            "method": "linux-file-handle-v1",
            "fsid": "1234",
            "handle": {"type": 1, "bytes": "00010203"},
        }
        self.data[migration.HOME_IDENTITY] = stable
        box.atomic_json(self.path, self.data)
        snapshot = discard.workspace_identity.snapshot_fd

        def observe(proof):
            def at(fd):
                current, observed = snapshot(fd)
                return (current, proof) if current == identity else (current, observed)

            return patch.object(
                discard.workspace_identity, "snapshot_fd", side_effect=at
            )

        with observe({**stable, "fsid": "4321"}):
            with self.assertRaisesRegex(box.BoxError, "filesystem identity"):
                self.run_discard()
        self.assertEqual(self.engine.removals, [])
        self.confirm.assert_not_called()
        # A device-only renumber is accepted only with the recorded proof.
        self.data[migration.RECEIPT]["volume"] = [identity[0] + 1, identity[1]]
        box.atomic_json(self.path, self.data)
        with observe(stable):
            self.assertEqual(self.run_discard(), 0)
        self.assertEqual(self.engine.removals, [("volume", self.name)])

    def test_active_or_reused_container_refuses_without_confirmation(self):
        for changed in (
            {**self.old, "State": {**self.old["State"], "Running": True, "Pid": 42}},
            {**self.old, "Id": OTHER},
        ):
            with self.subTest(changed=changed["Id"], state=changed["State"]["Running"]):
                self.engine.containers = {changed["Id"]: changed}
                with self.assertRaisesRegex(
                    box.BoxError, "not verified stopped|ownership or full ID"
                ):
                    self.run_discard()
        self.assertEqual(self.engine.removals, [])
        self.confirm.assert_not_called()

    def test_other_container_bind_alias_to_home_refuses(self):
        alias = self.root / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        upper = self.home.parents[2] / "overlay" / OTHER / "diff"
        upper.mkdir(parents=True)
        self.engine.containers[OTHER] = {
            **self.old,
            "Id": OTHER,
            "Name": "synthetic-other",
            "Config": {"Labels": {}},
            "GraphDriver": {"Name": "overlay", "Data": {"UpperDir": str(upper)}},
            "Mounts": [
                {"Type": "bind", "Source": str(alias), "Destination": "/synthetic"}
            ],
        }
        with self.assertRaisesRegex(box.BoxError, "Another container mounts"):
            self.run_discard()
        self.assertEqual(self.engine.removals, [])
        self.confirm.assert_not_called()

    def test_stopped_but_mounted_or_in_use_refuses_before_prompt(self):
        self.engine.mount_report = {OLD: "/synthetic/mount"}
        with self.assertRaisesRegex(box.BoxError, "still mounted"):
            self.run_discard()
        self.engine.mount_report = {}
        self.volume["MountCount"] = 1
        with self.assertRaisesRegex(box.BoxError, "unused private home"):
            self.run_discard()
        self.confirm.assert_not_called()
        self.assertEqual(self.engine.removals, [])

    def test_mountinfo_refuses_bind_alias_to_a_home_subtree(self):
        device, _, toplevel_mount = discard._identity(self.home)
        roots = {
            toplevel_mount: (device, Path("/"), Path("/")),
            toplevel_mount + 100: (
                device,
                self.home / "nested",
                self.root / "elsewhere",
            ),
        }
        with patch.object(discard, "_mounts", return_value=roots):
            with self.assertRaisesRegex(box.BoxError, "bind alias"):
                self.run_discard()
        self.confirm.assert_not_called()
        self.assertEqual(self.engine.removals, [])

    def test_namespace_covering_mount_projects_the_same_location_not_an_alias(self):
        mounts = {
            1: (1, Path("/"), Path("/")),
            2: (1, Path("/graph"), Path("/graph")),
        }
        discard._check_mount_backing(mounts, "/graph/volumes/selected", 2)
        mounts[3] = (1, Path("/graph"), Path("/other"))
        with self.assertRaisesRegex(discard.Refused, "bind alias"):
            discard._check_mount_backing(mounts, "/graph/volumes/selected", 2)

    def test_lifecycle_and_admission_lock_substitution_refuses_before_removal(self):
        lock = self.path.with_name("lock")
        with box.workspace_lock(self.path):
            with self.assertRaisesRegex(box.BoxError, "active oc-box process"):
                self.run_discard()
        self.confirm.assert_not_called()

        def substitute(*_):
            admission = self.path.parent / "control" / "admission.lock"
            admission.rename(admission.with_name("synthetic-original"))
            admission.touch(mode=0o600)
            return True

        self.confirm.side_effect = substitute
        with self.assertRaisesRegex(
            box.BoxError, "scope or result could not be verified"
        ):
            self.run_discard()
        self.assertEqual(self.engine.removals, [])

    def test_lost_ack_or_unverified_absence_does_not_remove_volume(self):
        for problem in ("lost_ack", "unverified_absence"):
            with self.subTest(problem=problem):
                Path(self.old["GraphDriver"]["Data"]["UpperDir"]).mkdir(
                    parents=True, exist_ok=True
                )
                self.engine.containers[OLD] = self.old
                self.engine.removals.clear()
                setattr(self.engine, problem, "container")
                with self.assertRaisesRegex(
                    box.BoxError, "not confirmed|absence was not verified"
                ):
                    self.run_discard()
                self.assertEqual(self.engine.removals, [("container", OLD)])
                self.assertTrue(self.path.is_file())
                self.assertFalse(self.retired.exists())
                self.assertEqual(
                    workspace_control.disposal_observation(self.path), self.control
                )
                setattr(self.engine, problem, None)

    def test_stop_intent_change_at_confirmation_prevents_any_removal(self):
        def changed(*_):
            workspace_control.request_stop(self.path)
            return True

        self.confirm.side_effect = changed
        with self.assertRaisesRegex(box.BoxError, "confirmed disposal plan changed"):
            self.run_discard()
        self.assertEqual(self.engine.removals, [])
        self.assertTrue(workspace_control.disposal_observation(self.path)[1])

    def test_engine_uses_nonforced_fixed_exact_removal(self):
        engine = object.__new__(ENGINE_CLASS)
        with patch.object(
            engine, "call", return_value=subprocess.CompletedProcess([], 0, OLD + "\n")
        ) as run:
            engine.remove("container", OLD)
            run.assert_called_once_with("container", "rm", "--", OLD)
            run.return_value = subprocess.CompletedProcess([], 0, "another-volume\n")
            with self.assertRaisesRegex(discard.Refused, "not confirmed"):
                engine.remove("volume", self.name)

    def test_existing_volume_prefix_neighbor_blocks_even_when_exact_home_exists(self):
        neighbor = self.name + "-backup"
        self.engine.volumes[neighbor] = {**self.volume, "Name": neighbor}
        with self.assertRaisesRegex(box.BoxError, "as a prefix"):
            self.run_discard()
        self.confirm.assert_not_called()
        self.assertEqual(self.engine.removals, [])

    @unittest.skipUnless(sys.version_info >= (3, 11), "TOML requires Python 3.11")
    def test_store_config_snapshot_cannot_be_redirected_by_original_file_edit(self):
        containers, storage = (
            self.root / name for name in ("containers.conf", "storage.conf")
        )
        original = b'[engine]\nstatic_dir = "/synthetic/original"\n'
        containers.write_bytes(original)
        storage.write_bytes(b'[storage]\ndriver = "overlay"\n')
        env, static, fd, settings = discard._engine_config(
            self.stack,
            {
                "CONTAINERS_CONF": str(containers),
                "CONTAINERS_STORAGE_CONF": str(storage),
            },
        )
        containers.write_text('[engine]\nstatic_dir = "/synthetic/other"\n')
        self.assertEqual(static, "/synthetic/original")
        self.assertEqual(settings[0]["static_dir"], "/synthetic/original")
        self.assertEqual(env["CONTAINERS_CONF"], "/proc/self/fd/0")
        self.assertEqual(env["CONTAINERS_STORAGE_CONF"], "/proc/self/fd/0")
        snapshot = Path(f"/proc/self/fd/{fd}")
        self.assertEqual(snapshot.read_bytes(), original + b"\n" + storage.read_bytes())
        with self.assertRaises(OSError):
            snapshot.write_bytes(b"other")

    @unittest.skipUnless(sys.version_info >= (3, 11), "TOML requires Python 3.11")
    def test_native_config_aliases_and_sqlite_dsn_delimiters_are_rejected(self):
        containers, storage = (
            self.root / name for name in ("containers.conf", "storage.conf")
        )
        for native_config, storage_config in (
            ('[engine]\nSTATIC_DIR = "/synthetic/other"\n', "[storage]\n"),
            ('[engine]\nstatic_dir = "/synthetic/?other"\n', "[storage]\n"),
            ("[engine]\n", '[storage]\nGRAPHROOT = "/synthetic/other"\n'),
            ("[engine]\n", '[storage]\ngraphroot = "/synthetic/?other"\n'),
        ):
            with self.subTest(
                native_config=native_config, storage_config=storage_config
            ):
                containers.write_text(native_config)
                storage.write_text(storage_config)
                with self.assertRaisesRegex(
                    discard.Refused, "field spelling|database location"
                ):
                    discard._engine_config(
                        self.stack,
                        {
                            "CONTAINERS_CONF": str(containers),
                            "CONTAINERS_STORAGE_CONF": str(storage),
                        },
                    )

    def test_pinned_database_rejects_replaced_inode(self):
        engine = object.__new__(ENGINE_CLASS)
        database = self.root / "db.sql"
        database.touch(mode=0o600)
        with database.open("rb") as stream:
            engine.database_path = str(database)
            engine.database = (
                stream.fileno(),
                discard._database_stamp(stream.fileno(), os.fstat(stream.fileno())),
            )
            engine.check_database()
            database.rename(self.root / "old-db.sql")
            database.touch(mode=0o600)
            with self.assertRaisesRegex(discard.Refused, "database identity changed"):
                engine.check_database()

    def test_namespace_refuses_shadowed_database_even_with_unchanged_storage(self):
        engine = object.__new__(ENGINE_CLASS)
        database = self.root / "db.sql"
        database.touch(mode=0o600)
        home = discard._identity(self.home)
        with database.open("rb") as stream:
            stamp = discard._database_stamp(stream.fileno(), os.fstat(stream.fileno()))
        engine.database_path = str(database)
        engine.database = (None, stamp)
        boot = (stamp[0], stamp[1] + 10, stamp[2])
        engine.alive = discard.SimpleNamespace(
            path=str(self.root / "alive"), stamp=boot
        )
        mountinfo = Path("/proc/self/mountinfo").read_text()
        with patch.object(engine, "read") as read:
            read.return_value = json.dumps([stamp, boot, home]) + "\n" + mountinfo
            engine.check_namespace(((str(self.home), home),))
            read.return_value = (
                json.dumps([(stamp[0], stamp[1] + 1, stamp[2]), boot, home])
                + "\n"
                + mountinfo
            )
            with self.assertRaisesRegex(discard.Refused, "reviewed database"):
                engine.check_namespace(((str(self.home), home),))

    def test_dispatch_is_separate_from_guarded_start_and_rejects_bypass_options(self):
        base = ["purge", "--workspace", self.ident, "--discard-guarded"]
        with (
            patch.object(discard, "discard", return_value=0) as execute,
            patch.object(box, "check_backend") as backend,
        ):
            self.assertEqual(box.main(base), 0)
            execute.assert_called_once_with(box, self.ident)
            execute.reset_mock()
            for arguments in (
                [*base, "--yes"],
                [*base, "--all"],
                [*base, "--delete-sessions"],
                ["purge", "--discard-guarded"],
                [*base, "--status"],
                ["start", "--workspace", self.ident, "--discard-guarded"],
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(box.BoxError, "typed confirmation"):
                        box.main(arguments)
            execute.assert_not_called()
            backend.assert_not_called()
