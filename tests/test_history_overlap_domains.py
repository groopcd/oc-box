"""Offline metadata tests: never start Podman or native OpenCode."""

from contextlib import ExitStack, contextmanager
from dataclasses import replace
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from oc_box import history_overlap_domains as domains


def _tick(pid):
    fields = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b") ")[2].split()
    return int(fields[19])


class PhysicalDomainsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._close)
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.src = self.layout(self.source, source=True)
        self.dst = self.layout(self.destination)
        self.src_cwd = dict(self.src.roots)["cwd"]
        self.host_cwd = dict(self.dst.roots)["cwd"]

    def _close(self):
        self.stack.close()

    def layout(self, root, *, source=False):
        data = root / ".local/share/opencode" if source else root / "data/opencode"
        db = data / ("oc-box-v2.db" if source else "opencode.db")
        # Synthetic scopes only. This is not a map of native's actual writes.
        config = root / "config/opencode"
        scopes = {
            "data": data,
            "migration": data,
            "blobs": data / "blobs",
            "state": root / "state/opencode",
            "registration": root / "state/opencode/registration",
            "log": data / "log",
            "cache": root / "cache/opencode",
            "config": config,
            "bin": root / "cache/opencode/bin",
            "repos": data / "repos",
            "home-fallback": root / "home/.native",
            "runtime": root / "runtime",
            "temp": root / "temp",
            "cwd": root / "temp/controller",
        }
        for path in scopes.values():
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        db.touch(mode=0o600)
        return domains.WritablePaths(
            str(db),
            tuple((key, str(loc)) for key, loc in scopes.items()),
            (str(root / "home"),),
        )

    @contextmanager
    def child_space(self, *, database=True):
        with subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                "import os, sys\n"
                "if sys.argv[1]: fd = os.open(sys.argv[1], os.O_RDONLY)\n"
                "os.write(1, b'1'); os.read(0, 1)\n",
                self.src.database if database else "",
            ],
            cwd=self.src_cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        ) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.read(1), b"1")
                with domains.pin_process_namespace(
                    child.pid, start_ticks=_tick(child.pid)
                ) as source:
                    yield child, source
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)

    @contextmanager
    def fixture_backing(self, *, database=True):
        with (
            self.child_space(database=database) as (_, source),
            domains.pin_current_namespace() as host,
        ):
            with ExitStack() as stack:
                # Exercise the composition using real descriptors in the same
                # backing tree. The production acquisition below explicitly
                # refuses a source process in the host mount namespace.
                src = domains._pin_path(stack, source, os.fsencode(self.source))
                dst = domains._pin_path(stack, host, os.fsencode(self.source))
                yield domains._Backing(src, dst)

    def open(self, volume, **changes):
        return domains.pin_writable_domains(
            **{
                "release": "2.4.0",
                "volume": volume,
                "additional_source_backings": (),
                "source": self.src,
                "destination": self.dst,
                "host_cwd": self.host_cwd,
                **changes,
            }
        )

    def test_namespace_rechecks_identity_cwd_and_exact_child_exit(self):
        with self.child_space() as (child, space):
            space.require_cwd(self.src_cwd)
            with self.assertRaisesRegex(domains.DomainRefusal, "cwd-unverified"):
                space.require_cwd(self.host_cwd)
            with self.assertRaisesRegex(
                domains.DomainRefusal, "process-binding-changed"
            ):
                with domains.pin_process_namespace(
                    child.pid, start_ticks=space.start + 1
                ):
                    self.fail("accepted a different process generation")
            child.stdin.close()
            child.wait(timeout=5)
            with self.assertRaisesRegex(
                domains.DomainRefusal, "process-binding-changed"
            ):
                space.recheck()

    def test_mountinfo_drift_and_stack_never_adopt_a_fresh_baseline(self):
        with domains.pin_current_namespace() as host:
            row = host.exact_mount(b"/")
            candidate = os.fsencode(self.destination)
            # Even an otherwise unrelated mount change invalidates this view.
            with patch.object(
                host, "get_mountinfo", return_value=host.mountinfo + b"\n"
            ):
                with self.assertRaisesRegex(
                    domains.DomainRefusal, "mount-binding-changed"
                ):
                    host.recheck()
            with patch.dict(host.mounts, {candidate: [row, row]}):
                with ExitStack() as stack:
                    with self.assertRaisesRegex(domains.DomainRefusal, "mount-binding"):
                        domains._pin_path(stack, host, candidate)

    def test_same_namespace_is_not_an_optionless_volume_witness(self):
        with self.child_space() as (_, source), domains.pin_current_namespace() as host:
            with self.assertRaisesRegex(
                domains.DomainRefusal, "source-backing-unverified"
            ):
                with domains.pin_mount_backing(
                    source, str(self.source), host, str(self.source)
                ):
                    self.fail("accepted a host child as the original container process")

    def test_existing_source_database_must_also_be_open_by_the_bound_process(self):
        with self.fixture_backing(database=False) as volume:
            with self.assertRaisesRegex(
                domains.DomainRefusal, "source-database-not-open"
            ):
                with self.open(volume):
                    self.fail("a path alone did not bind the source database")

    def test_prelease_requires_destination_outside_the_entire_source_volume(self):
        shadow = self.layout(self.source / "host-like-tree")
        with self.fixture_backing(database=False) as volume:
            domains.verify_prelease_separation(volume, self.dst)
            with self.assertRaisesRegex(
                domains.DomainRefusal, "overlapping-writable-domains"
            ):
                domains.verify_prelease_separation(volume, shadow)

    def test_worker_cannot_reuse_the_coordinator_namespace(self):
        failures = []
        with domains.pin_current_namespace() as host:

            def recheck():
                try:
                    host.recheck()
                except domains.DomainRefusal as error:
                    failures.append(str(error))

            thread = threading.Thread(target=recheck)
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, ["process-binding-changed"])

    def test_disjoint_scopes_allow_native_sidecar_cycles_between_checks(self):
        with self.fixture_backing() as volume:
            with self.open(volume, release="2.4.1") as guard:
                for database in (self.src.database, self.dst.database):
                    with self.subTest(database=database):
                        wal = Path(database + "-wal")
                        wal.touch(mode=0o600)
                        guard.recheck()
                        wal.unlink()
                guard.recheck()
            with self.assertRaisesRegex(domains.DomainRefusal, "domain-ended"):
                guard.recheck()

    def test_scoped_roots_refuse_unknown_missing_wildcard_and_wrong_selectors(self):
        with self.fixture_backing() as volume:
            for name, changes in (
                ("version", {"release": "2.4.2"}),
                ("prerelease", {"release": "2.4.1-beta.0"}),
                (
                    "missing-roles",
                    {"destination": replace(self.dst, roots=self.dst.roots[1:])},
                ),
                (
                    "unknown-role",
                    {
                        "destination": replace(
                            self.dst,
                            roots=self.dst.roots + (("unknown", self.host_cwd),),
                        )
                    },
                ),
                (
                    "whole-home",
                    {
                        "source": replace(
                            self.src,
                            roots=self.src.roots
                            + (("home-fallback", self.src.homes[0]),),
                        )
                    },
                ),
                (
                    "missing-root",
                    {
                        "source": replace(
                            self.src,
                            roots=self.src.roots
                            + (("temp", str(self.source / "missing")),),
                        )
                    },
                ),
                (
                    "unmapped-source",
                    {
                        "source": replace(
                            self.src, roots=self.src.roots + (("temp", self.host_cwd),)
                        )
                    },
                ),
                ("ambiguous-map", {"additional_source_backings": (volume,)}),
                (
                    "other-db",
                    {
                        "source": replace(
                            self.src, database=self.src.database + ".other"
                        )
                    },
                ),
                ("cwd-selector", {"host_cwd": self.src_cwd}),
                (
                    "cwd-observation",
                    {
                        "source": replace(
                            self.src,
                            roots=tuple(
                                (
                                    key,
                                    str(self.source / "runtime")
                                    if key == "cwd"
                                    else location,
                                )
                                for key, location in self.src.roots
                            ),
                        )
                    },
                ),
            ):
                with self.subTest(case=name), self.assertRaises(domains.DomainRefusal):
                    with self.open(volume, **changes):
                        self.fail("incomplete scope passed")

    def test_equal_and_ancestor_domains_refuse_in_common_backing_view(self):
        with self.fixture_backing() as volume:
            for other in (dict(self.src.roots)["data"], str(self.source)):
                with self.subTest(other=other):
                    dest = replace(self.dst, roots=self.dst.roots + (("cache", other),))
                    with self.assertRaisesRegex(
                        domains.DomainRefusal, "overlapping-writable"
                    ):
                        with self.open(volume, destination=dest):
                            self.fail("accepted a source backing ancestor or root")

    def test_aliases_and_unowned_modes_refuse_anywhere_in_scoped_tree(self):
        data = Path(dict(self.dst.roots)["data"])
        target = data / "blob"
        with self.fixture_backing() as volume:
            for create in (
                lambda: target.symlink_to(self.src.database),
                lambda: os.link(self.src.database, target),
                lambda: os.mkfifo(target, 0o600),
                lambda: (target.touch(mode=0o600), target.chmod(0o666)),
            ):
                create()
                try:
                    with self.assertRaises(domains.DomainRefusal):
                        with self.open(volume):
                            self.fail("accepted unsafe scoped object")
                finally:
                    target.unlink()

    def test_unsafe_sidecar_change_or_replaced_root_latches_failure(self):
        with self.fixture_backing() as volume, self.open(volume) as guard:
            wal = Path(self.dst.database + "-wal")
            wal.symlink_to(self.src.database)
            with self.assertRaises(domains.DomainRefusal):
                guard.recheck()
            wal.unlink()
            with self.assertRaisesRegex(domains.DomainRefusal, "domain-ended"):
                guard.recheck()
        with self.fixture_backing() as volume, self.open(volume) as guard:
            data = Path(dict(self.dst.roots)["cache"])
            data.rename(data.with_name("old"))
            (data / "bin").mkdir(parents=True)
            with self.assertRaisesRegex(domains.DomainRefusal, "writable-path-changed"):
                guard.recheck()

    def test_sidecar_appearance_during_check_is_not_the_next_baseline(self):
        wal = Path(self.dst.database + "-wal")
        original_tree = domains._tree

        def change_during_scan(pin, budget):
            wal.touch(mode=0o600)
            return original_tree(pin, budget)

        with self.fixture_backing() as volume, self.open(volume) as guard:
            with patch.object(domains, "_tree", side_effect=change_during_scan):
                with self.assertRaisesRegex(
                    domains.DomainRefusal, "writable-sidecars-changed"
                ):
                    guard.recheck()
            with self.assertRaisesRegex(domains.DomainRefusal, "domain-ended"):
                guard.recheck()

    def test_submount_tree_bound_and_missing_database_refuse(self):
        with self.fixture_backing() as volume:
            space = volume.host.space
            row = space.exact_mount(b"/")
            hidden = os.fsencode(Path(dict(self.dst.roots)["data"]) / "hidden")
            with patch.dict(space.mounts, {hidden: [row]}):
                with self.assertRaisesRegex(
                    domains.DomainRefusal, "writable-tree-mount"
                ):
                    with self.open(volume):
                        self.fail("accepted a scoped tree containing another mount")
            with patch.object(domains, "_TREE_ENTRIES", 0):
                with self.assertRaisesRegex(
                    domains.DomainRefusal, "writable-tree-limit"
                ):
                    with self.open(volume):
                        self.fail("accepted an incomplete bounded walk")
            Path(self.dst.database).unlink()
            with self.assertRaises(domains.DomainRefusal):
                with self.open(volume):
                    self.fail("accepted a guessed missing destination database")

    def test_caller_errors_are_not_translated_into_preflight_refusals(self):
        error = OSError("the caller owns this error")
        with self.fixture_backing() as volume:
            with self.assertRaises(OSError) as caught:
                with self.open(volume):
                    raise error
        self.assertIs(caught.exception, error)


class MountCorrespondenceTests(unittest.TestCase):
    def test_mount_superblock_and_descriptor_identity_are_independent(self):
        def row(mount, parent, *, fs="btrfs", device="0:9", mode="rw"):
            return (
                mount,
                parent,
                f"{mount} {parent} {device} / /mount {mode} - {fs} /dev/disk rw".encode(),
            )

        obj = domains._Object(350, 123, 10, 1000, 1000, stat.S_IFDIR | 0o700)
        src = domains._PathPin(
            None, b"/volume", -1, (replace(obj, mount=9), obj), False
        )
        dst = domains._PathPin(
            None, b"/host/volume", -1, (replace(obj, mount=44),), False
        )
        domains._require_direct_mount(src, row(10, 9), dst, row(44, 1))
        for changed_src, changed_dst, host_row in (
            (row(10, 8), dst, row(44, 1)),
            (row(10, 9, device="0:3"), dst, row(44, 1)),
            (row(10, 9, mode="ro"), dst, row(44, 1)),
            (row(10, 9, fs="overlay"), dst, row(44, 1, fs="overlay")),
            (
                row(10, 9),
                replace(dst, chain=(replace(dst.obj, inode=124),)),
                row(44, 1),
            ),
        ):
            with self.subTest(row=changed_src):
                with self.assertRaisesRegex(
                    domains.DomainRefusal, "source-backing-unverified"
                ):
                    domains._require_direct_mount(
                        src, changed_src, changed_dst, host_row
                    )


if __name__ == "__main__":
    unittest.main()
