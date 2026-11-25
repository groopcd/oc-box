import contextlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from oc_box import guarded_discard as discard, podman_config_layers as layers


@unittest.skipUnless(sys.version_info >= (3, 11), "TOML requires Python 3.11")
class LayeredPodmanConfigTests(unittest.TestCase):
    def setUp(self):
        import tomllib

        self.parse = tomllib.loads
        private = tempfile.TemporaryDirectory(prefix="ocbox-config-layer-")
        self.addCleanup(private.cleanup)
        self.root = Path(private.name)
        self.user = self.root / "xdg" / "containers"
        self.user.mkdir(parents=True)
        self.vendor = self.root / "vendor"
        self.vendor.mkdir()
        self.site = self.root / "site"
        self.site.mkdir()
        self.env = {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.user.parent)}
        self.collect = layers.candidates

    def write(self, parent, filename, data):
        path = parent / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def candidates(self, environment=None, reader=Path.read_bytes):
        scanner = layers._dropins
        with patch.object(
            layers, "_dropins", side_effect=lambda path, _: scanner(path, os.geteuid())
        ):
            return self.collect(
                environment or self.env, reader, system=(self.vendor, self.site)
            )

    def test_native_order_applies_only_loaded_files_including_shadowing(self):
        main = self.write(
            self.vendor, "containers.conf", '[engine]\nruntime = "crun"\n'
        )
        shadow = self.write(
            self.vendor,
            "containers.conf.d/20-runtime.conf",
            '[engine]\nruntime = "shadowed"\n',
        )
        selected = self.write(
            self.user,
            "containers.conf.d/20-runtime.conf",
            '[engine]\nruntime = "runc"\n',
        )
        storage = self.write(
            self.site, "storage.conf", '[storage]\ndriver = "overlay"\n'
        )
        ignored = self.write(
            self.user, "containers.conf.d/note.txt", "unread by Podman"
        )
        possible = self.candidates()
        self.assertNotIn(shadow, possible)
        self.assertNotIn(ignored, possible)
        self.assertEqual(tuple(possible), (main, selected, storage))
        data = layers.flatten(
            [blob for family, blob in possible.values() if family == "containers"],
            self.parse,
        )
        self.assertEqual(self.parse(data.decode())["engine"]["runtime"], "runc")

    def test_only_the_first_main_is_loaded_and_dropins_sort_globally(self):
        shadow = self.write(
            self.vendor, "containers.conf", '[engine]\nruntime = "old"\n'
        )
        main = self.write(self.site, "containers.conf", '[engine]\nruntime = "site"\n')
        early = self.write(
            self.user,
            "containers.conf.d/10-runtime.conf",
            '[engine]\nruntime = "early"\n',
        )
        late = self.write(
            self.vendor,
            "containers.conf.d/90-runtime.conf",
            '[engine]\nruntime = "runc"\n',
        )
        possible = self.candidates()
        self.assertNotIn(shadow, possible)
        self.assertEqual(tuple(possible), (main, early, late))
        merged = layers.flatten(
            [blob for family, blob in possible.values() if family == "containers"],
            self.parse,
        )
        self.assertEqual(self.parse(merged.decode())["engine"]["runtime"], "runc")

    def test_merge_preserves_distinct_fields_and_ordinary_array_override(self):
        data = layers.flatten(
            [
                b'[engine]\nstatic_dir = "/synthetic/db"\n'
                b'[engine.runtimes]\nrunc = ["/usr/bin/runc"]\ncrun = ["/usr/local/bin/crun"]\n',
                b'[engine]\nruntime = "runc"\n'
                b'[engine.runtimes]\ncrun = ["/usr/bin/crun"]\n',
            ],
            self.parse,
        )
        self.assertEqual(
            self.parse(data.decode()),
            {
                "engine": {
                    "runtime": "runc",
                    "static_dir": "/synthetic/db",
                    "runtimes": {"runc": ["/usr/bin/runc"], "crun": ["/usr/bin/crun"]},
                }
            },
        )

    def test_layered_merge_refuses_attributed_arrays_and_unverified_repeated_tables(
        self,
    ):
        for data in (
            [
                b'[engine]\nhelper_binaries_dir = [{append = true}, "/synthetic"]',
                b'[engine]\nhelper_binaries_dir = ["/usr/bin"]',
            ],
            [b'[engine]\nruntime="runc"', b"[engine]\nruntime=false"],
            [b"[engine.custom]\nfirst = true", b"[engine.custom]\nsecond = true"],
            [
                b"[engine.runtimes.runc]\nfirst = true",
                b"[engine.runtimes.runc]\nsecond = true",
            ],
        ):
            with self.subTest(data=data), self.assertRaises(layers.ConfigError):
                layers.flatten(data, self.parse)

    def test_sealed_snapshot_rechecks_sources_and_preserves_audited_selectors(self):
        main = self.write(
            self.vendor, "containers.conf", '[engine]\nruntime = "crun"\n'
        )
        drop = self.write(
            self.user,
            "containers.conf.d/50-runtime.conf",
            '[engine]\nruntime = "runc"\n',
        )
        with (
            patch.object(layers, "candidates", side_effect=self.candidates),
            contextlib.ExitStack() as stack,
        ):
            env, static_dir, descriptor, settings = discard._engine_config(
                stack, self.env
            )
            self.assertEqual(static_dir, "")
            self.assertEqual(settings[0]["runtime"], "runc")
            self.assertEqual(env["CONTAINERS_CONF"], "/proc/self/fd/0")
            drop.write_text('[engine]\nruntime = "other"\n')
            self.assertEqual(
                self.parse(Path(f"/proc/self/fd/{descriptor}").read_text())["engine"][
                    "runtime"
                ],
                "runc",
            )

        calls = 0

        def drifting(env, reader):
            nonlocal calls
            calls += 1
            if calls == 2:
                drop.write_text('[engine]\nruntime = "changed-again"\n')
            return self.candidates(env, reader)

        with (
            patch.object(layers, "candidates", side_effect=drifting),
            contextlib.ExitStack() as stack,
            self.assertRaisesRegex(discard.Refused, "changed during review"),
        ):
            discard._engine_config(stack, self.env)

    def test_explicit_store_arguments_replay_mount_options_without_weakening_scope(
        self,
    ):
        self.assertEqual(
            discard._storage_options({"overlay.mountopt": "nodev"}),
            ("--storage-opt=overlay.mountopt=nodev",),
        )
        with self.assertRaisesRegex(discard.Refused, "could not be reproduced"):
            discard._storage_options(
                {"overlay.additionalImageStores": ["/synthetic/other"]}
            )

    def test_empty_storage_override_and_xdg_selector_are_not_normalized(self):
        with contextlib.ExitStack() as stack:
            for name in ("STORAGE_OPTS", "XDG_CONFIG_HOME"):
                with self.subTest(name=name), self.assertRaises(discard.Refused):
                    discard._engine_config(
                        stack,
                        {**self.env, name: ""},
                    )

    def test_concrete_runtime_paths_reject_unverified_expansion(self):
        env = {
            **self.env,
            "XDG_RUNTIME_DIR": str(self.root / "run"),
            "TMPDIR": "/synthetic/marked?path",
        }
        graph, run, volume, static, temp, db, options = discard._store_layout(
            env,
            "",
            (
                {},
                {
                    "rootless_storage_path": "$HOME/graph",
                    "options": {"overlay": {"mountopt": "nodev"}},
                },
            ),
        )
        self.assertEqual(graph, str(self.root / "graph"))
        self.assertEqual(
            (run, volume, static, temp, db),
            tuple(
                str(self.root / path)
                for path in (
                    "run/containers",
                    "graph/volumes",
                    "graph/libpod",
                    "run/libpod/tmp",
                    "graph/db.sql",
                )
            ),
        )
        self.assertEqual(options, {"overlay.mountopt": "nodev"})
        for path in (
            "${TMPDIR}/storage",
            "$HOME/graph?redirect",
            "/synthetic/a/../graph",
        ):
            with self.subTest(path=path), self.assertRaises(discard.Refused):
                discard._store_layout(env, "", ({}, {"graphroot": path}))

    def test_read_only_database_metadata_rejects_shifted_runtime(self):
        db = self.root / "db.sql"
        expected = (
            "/synthetic/static",
            "/synthetic/tmp",
            "/synthetic/graph",
            "/synthetic/run",
            "overlay",
            "/synthetic/volume",
        )
        with contextlib.closing(sqlite3.connect(db)) as sql:
            sql.execute(
                "CREATE TABLE DBConfig(ID, SchemaVersion, OS, StaticDir, TmpDir, GraphRoot, RunRoot, GraphDriver, VolumeDir)"
            )
            sql.execute(
                "INSERT INTO DBConfig VALUES (1, 1, 'linux', ?, ?, ?, ?, ?, ?)",
                expected,
            )
            sql.commit()
        original = db.read_bytes()
        self.assertEqual(
            discard._verify_database_metadata(str(db), expected), "overlay"
        )
        with self.assertRaisesRegex(discard.Refused, "does not match"):
            discard._verify_database_metadata(
                str(db), (expected[0], "/synthetic/other", *expected[2:])
            )
        self.assertEqual(db.read_bytes(), original)
        self.assertEqual(tuple(self.root.glob("db.sql*")), (db,))
        with contextlib.closing(sqlite3.connect(db)) as sql:
            sql.execute("UPDATE DBConfig SET GraphDriver = ''")
            sql.commit()
        automatic = db.read_bytes()
        self.assertEqual(discard._verify_database_metadata(str(db), expected), "")
        self.assertEqual(db.read_bytes(), automatic)
        with contextlib.closing(sqlite3.connect(db)) as sql:
            sql.execute("UPDATE DBConfig SET GraphDriver = 'vfs'")
            sql.commit()
        with self.assertRaisesRegex(discard.Refused, "does not match"):
            discard._verify_database_metadata(str(db), expected)

    def test_pre_open_profile_refuses_unbound_options_and_event_paths(self):
        env = {**self.env, "XDG_RUNTIME_DIR": str(self.root / "run")}
        for key in ("skip_mount_home", "mount_program", "mountopt"):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(discard.Refused, "overlay options"),
            ):
                discard._store_layout(env, "", ({}, {"options": {key: "synthetic"}}))
        with self.assertRaisesRegex(discard.Refused, "event-log"):
            discard._store_layout(
                env, "", ({"events_logfile_path": "/synthetic/unreviewed"}, {})
            )
        with patch.object(discard, "_absent", return_value=False):
            with self.assertRaisesRegex(discard.Refused, "pre-exec hooks"):
                discard.Engine._check_preexec()
