"""Offline migration tests: Podman is a stateful fake, never a subprocess."""

import contextlib
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oc_box import (
    backups,
    entrypoint_migration as migration,
    oc_box as box,
    workspace_control,
)


_SOURCE, _CANDIDATE, _FOREIGN = ("a" * 64, "b" * 64, "c" * 64)
_BASE, _SNAPSHOT = ("d" * 64, "e" * 64)
_CONVERSION_LABEL = "io.oc-box.entrypoint-conversion"
_HOST_CONFIG = {
    "Privileged": False,
    "AutoRemove": False,
    "PidMode": "private",
    "RestartPolicy": {"Name": "no"},
    "PidsLimit": 2048,
    "Init": True,
    "Ulimits": [
        {"Name": "RLIMIT_NOFILE", "Soft": 1024, "Hard": 8192},
        {"Name": "RLIMIT_NPROC", "Soft": 7600, "Hard": 7600},
    ],
}
_PATH = (
    "PATH=/opt/oc-box/runtime/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)


class FixturePodman:
    def __init__(self, path, data, home):
        self.path = path
        self.name = data["container"]
        self.home = data["home_volume"]
        self.mountpoint = str(home)
        self.volume = {
            "Name": self.home,
            "Driver": "local",
            "Mountpoint": self.mountpoint,
            "Labels": {box.LABEL: data["id"]},
            "Options": {},
        }
        source = self.container(
            _SOURCE,
            self.name,
            _BASE,
            {"io.oc-box.managed": "true", box.LABEL: data["id"]},
            data["container_project"],
            [
                f"HOME={data['home']}",
                _PATH,
                "container=podman",
                f"HOSTNAME={_SOURCE[:12]}",
            ],
            "/bin/sh",
            ["-c", "exec sleep infinity"],
            [
                f"{self.home}:{data['home']}:rw",
                f"{data['project']}:{data['container_project']}:rw,z,rprivate,nosuid,nodev",
            ],
            status="exited",
        )
        self.containers = {_SOURCE: source}
        self.images = {_BASE: {"Id": "sha256:" + _BASE, "Config": {"Labels": {}}}}
        self.exists_errors = {}
        self.before = {}
        self.after = {}
        self.calls = []
        self.mutations = []
        self.removal = None

    def container(
        self,
        ident,
        name,
        image,
        labels,
        workdir,
        env,
        entrypoint,
        cmd,
        volumes,
        *,
        status,
    ):
        mounts = []
        for volume in volumes:
            source, destination, options = volume.split(":")
            mount = {
                "Type": "bind",
                "Source": source,
                "Destination": destination,
                "Options": options.split(","),
                "RW": "rw" in options.split(","),
            }
            if source == self.home:
                mount.update(Type="volume", Name=source, Source=self.mountpoint)
            mounts.append(mount)
        return {
            "Id": ident,
            "Name": name,
            "Image": image,
            "Config": {
                "Labels": labels,
                "User": "0:0",
                "Hostname": ident[:12],
                "WorkingDir": workdir,
                "Env": env,
                "Entrypoint": [entrypoint],
                "Cmd": list(cmd),
                "Healthcheck": {"Test": ["NONE"]},
                "CreateCommand": ["podman", "create"],
            },
            "HostConfig": deepcopy(_HOST_CONFIG),
            "State": {
                "Status": status,
                "Running": False,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "Pid": 0,
                "ConmonPid": 0,
            },
            "Mounts": mounts,
        }

    @staticmethod
    def values(args, flag):
        return [args[i + 1] for i, value in enumerate(args) if value == flag]

    def lookup(self, kind, selector):
        if kind == "volume":
            return self.volume if selector == self.home else None
        if kind == "image":
            return self.images.get(selector)
        return self.containers.get(selector) or next(
            (row for row in self.containers.values() if row["Name"] == selector), None
        )

    def __call__(self, *args, capture, private, check, timeout, stdin):
        assert capture and private and stdin == subprocess.DEVNULL
        assert 0 < timeout <= (600 if args[0] == "commit" else 15)
        self.calls.append(args)

        def result(code=0, stdout=""):
            if code and check:
                raise box.BoxError(f"podman failed (exit {code})")
            return subprocess.CompletedProcess(args, code, stdout, "")

        if args == ("info", "--format=json"):
            return result(
                stdout=json.dumps(
                    {"host": {"serviceIsRemote": False, "security": {"rootless": True}}}
                )
            )
        if args[:2] in {
            (kind, verb)
            for kind in ("container", "image", "volume")
            for verb in ("inspect", "exists")
        }:
            kind, verb, selector = args
            found = self.lookup(kind, selector)
            if verb == "exists":
                return result(self.exists_errors.get(kind, 0 if found else 1))
            return result(stdout=json.dumps([found])) if found else result(125)
        if args == (
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"volume={self.home}",
            "--format",
            "{{.ID}}",
        ):
            return result(
                stdout="\n".join(
                    row["Id"]
                    for row in reversed(self.containers.values())
                    if any(mount.get("Name") == self.home for mount in row["Mounts"])
                )
            )
        if args[0] in ("commit", "create"):
            action = args[0]
        elif args[0] == "rename":
            action = "publish" if args[-1] == self.name else "retire"
        elif args[:2] == ("container", "rm"):
            action = "rm"
        else:
            # Includes start, exec, volume creation/removal, and image pulls.
            raise AssertionError(f"Unexpected synthetic engine command: {args!r}")

        self.mutations.append(action)
        if hook := self.before.pop(action, None):
            if (code := hook()) is not None:
                return result(code)
        stdout = ""
        if action == "commit":
            source, tag = args[-2:]
            assert source in self.containers and tag not in self.images
            key, token = (
                args[args.index("--change") + 1].removeprefix("LABEL ").split("=")
            )
            image = {"Id": "sha256:" + _SNAPSHOT, "Config": {"Labels": {key: token}}}
            self.images[tag] = self.images[_SNAPSHOT] = image
            stdout = image["Id"] + "\n"
        elif action == "create":

            def value(flag):
                return self.values(args, flag)[0]

            name, image = value("--name"), args[-3]
            assert image in self.images and not self.lookup("container", name)
            self.containers[_CANDIDATE] = self.container(
                _CANDIDATE,
                name,
                image,
                dict(label.split("=", 1) for label in self.values(args, "--label")),
                value("--workdir"),
                self.values(args, "--env"),
                value("--entrypoint"),
                args[-2:],
                self.values(args, "--volume"),
                status="configured",
            )
            config = self.containers[_CANDIDATE]
            config["Config"]["User"] = value("--user")
            config["Config"]["CreateCommand"] = ["podman", *args]
            config["HostConfig"]["PidsLimit"] = int(value("--pids-limit"))
            config["HostConfig"]["Init"] = "--init" in args
            config["HostConfig"]["Ulimits"] = []
            stdout = _CANDIDATE + "\n"
        elif action in ("retire", "publish"):
            _, ident, name = args
            assert not self.lookup("container", name)
            self.containers[ident]["Name"] = name
        else:
            assert args == ("container", "rm", _SOURCE)  # No --force or --volumes.
            self.removal = (
                {ident: item["Name"] for ident, item in self.containers.items()},
                box.read_json(self.path)[migration.FIELD]["candidate"],
            )
            del self.containers[_SOURCE]
        if hook := self.after.pop(action, None):
            if (code := hook()) is not None:
                return result(code)
        return result(stdout=stdout)


class EntrypointMigrationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with contextlib.ExitStack() as stack:
            root = Path(
                stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="oc-box-entrypoint-migration-")
                )
            )
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "HOME": str(root / "host-home"),
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_DATA_HOME": str(root / "data"),
                        "OC_BOX_STATE_HOME": str(root / "state"),
                    },
                )
            )
            self.project, self.home = root / "project", root / "private-home"
            self.project.mkdir(mode=0o700)
            self.home.mkdir(mode=0o700)
            self.sentinels = {
                self.project / "project-file": b"synthetic project file\n",
                self.home / "opencode.db": b"synthetic private conversation\n",
            }
            for file, payload in self.sentinels.items():
                file.write_bytes(payload)
                file.chmod(0o600)
            self.identities = {
                directory: (directory.stat().st_dev, directory.stat().st_ino)
                for directory in (self.project, self.home)
            }
            self.path, data = box.create_workspace(self.project, "tumbleweed")
            data.update(
                container_project="/private/original-project",
                home="/private/container-home",
                ready=True,
                opencode_version="2.2.7",
            )
            box.atomic_json(self.path, data)
            self.original = deepcopy(data)
            self.token = workspace_control.capture(self.path)
            self.engine = FixturePodman(self.path, data, self.home)
            stack.enter_context(patch.object(box, "podman", self.engine))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            yield

    def convert(self, *, rebuild=False, token=None):
        # A retry starts with on-disk state, never the interrupted invocation's
        # in-memory copy. Hold the same outer lock expected by the conversion.
        with box.workspace_lock(self.path):
            data = box.read_json(self.path)
            migration.convert(
                box,
                self.path,
                data,
                self.token if token is None else token,
                _SOURCE,
                rebuild=rebuild,
            )
        self.assertEqual(data, box.read_json(self.path))
        return data

    def assert_refused(self, message, **kwargs):
        with self.assertRaises(box.BoxError) as error:
            self.convert(**kwargs)
        self.assertEqual(str(error.exception), message)

    def assert_preserved(self):
        for file, payload in self.sentinels.items():
            self.assertEqual(file.read_bytes(), payload)
        for directory, identity in self.identities.items():
            self.assertEqual(
                (directory.stat().st_dev, directory.stat().st_ino), identity
            )
        self.assertEqual(
            (self.project / box.MARKER).read_text().strip(), self.original["id"]
        )

    def assert_completed(self, data, *, image, bootstrap):
        receipt = {
            "id": _CANDIDATE,
            "bootstrap": bootstrap,
            "volume": [self.home.stat().st_dev, self.home.stat().st_ino],
        }
        self.assertEqual(
            data, {**self.original, "image": image, migration.RECEIPT: receipt}
        )
        self.assertEqual(set(self.engine.containers), {_CANDIDATE})
        candidate = self.engine.containers[_CANDIDATE]
        self.assertEqual(candidate["Name"], self.original["container"])
        self.assertEqual(candidate["Image"], image)
        self.assertEqual(candidate["State"]["Status"], "configured")
        # A booted rootless container can report inherited limits that are
        # absent before its replacement boots. Neither route may replay them.
        create = candidate["Config"]["CreateCommand"]
        self.assertFalse(
            any(arg == "--ulimit" or arg.startswith("--ulimit=") for arg in create)
        )
        self.assertEqual(candidate["HostConfig"]["Ulimits"], [])
        self.assertEqual(candidate["HostConfig"]["PidsLimit"], data["pids"])
        self.assertTrue(migration.fresh_replacement(data, candidate))
        self.assertIs(migration.needs_bootstrap(data), bootstrap)
        self.assertEqual(
            [
                (m["Type"], m.get("Name"), m["Source"], m["Destination"])
                for m in candidate["Mounts"]
            ],
            [
                ("volume", data["home_volume"], str(self.home), data["home"]),
                ("bind", None, str(self.project), data["container_project"]),
            ],
        )
        self.assert_preserved()

    def test_snapshot_swap_publishes_stopped_candidate_before_retiring_original(self):
        with self.fixture():
            # Podman can leave both flags set when the image home was absent.
            # The already-populated original remains safe with :nocopy.
            self.engine.volume.update(NeedsCopyUp=True, NeedsChown=True)
            data = self.convert()
            self.assert_completed(data, image=_SNAPSHOT, bootstrap=False)
            self.assertEqual(
                self.engine.mutations, ["commit", "create", "retire", "publish", "rm"]
            )
            commit = next(args for args in self.engine.calls if args[0] == "commit")
            self.assertIn("--pause=false", commit)
            self.assertIn("--include-volumes=false", commit)
            create = next(args for args in self.engine.calls if args[0] == "create")
            self.assertIn("--pull=never", create)
            home_options = next(
                argument.split(":")[2].split(",")
                for argument in self.engine.values(create, "--volume")
                if argument.startswith(f"{data['home_volume']}:{data['home']}:")
            )
            self.assertTrue({"rw", "nocopy"}.issubset(home_options))
            self.assertEqual(
                create[-4:], ("/bin/sh", _SNAPSHOT, "-c", "exec /bin/sleep infinity")
            )
            names, recorded_candidate = self.engine.removal
            self.assertEqual(recorded_candidate, _CANDIDATE)
            self.assertEqual(names[_CANDIDATE], self.original["container"])
            self.assertTrue(names[_SOURCE].endswith("-old"))

    def test_acknowledged_snapshot_failure_only_offers_explicit_base_image_rebuild(
        self,
    ):
        with self.fixture():
            self.assert_refused(
                "Container OS rebuild is available only after a verified snapshot failure.",
                rebuild=True,
            )
            self.assertEqual(self.engine.mutations, [])
            self.engine.before["commit"] = lambda: 125
            self.assert_refused(migration.OFFER_REBUILD)
            pending = box.read_json(self.path)
            self.assertEqual(pending[migration.FIELD]["mode"], "snapshot-failed")
            self.assertIsNone(pending[migration.FIELD]["image"])
            self.assertNotIn(migration.RECEIPT, pending)
            self.assertEqual(set(self.engine.images), {_BASE})
            self.assertEqual(self.engine.mutations, ["commit"])
            self.assertEqual(
                self.engine.containers[_SOURCE]["Name"], self.original["container"]
            )
            with self.assertRaisesRegex(box.BoxError, "replacement is not complete"):
                migration.consent(box, SimpleNamespace(), pending)
            self.assert_preserved()

            data = self.convert(rebuild=True)
            self.assert_completed(data, image=_BASE, bootstrap=True)
            self.assertEqual(set(self.engine.images), {_BASE})
            self.assertEqual(
                self.engine.mutations, ["commit", "create", "retire", "publish", "rm"]
            )
            self.assertIn(("image", "inspect", _BASE), self.engine.calls)

    def test_explicit_or_unverifiable_process_limit_settings_refuse_without_mutation(
        self,
    ):
        for argv in (
            ["podman", "create", "--ulimit=nproc=400:800"],
            ["podman", "create", "--ulimit", "nofile=1024:2048"],
            ["podman", "create", 3],
            [],
            None,
        ):
            with self.subTest(argv=argv), self.fixture():
                self.engine.containers[_SOURCE]["Config"]["CreateCommand"] = argv
                self.assert_refused(migration.REFUSED)
                self.assertEqual(self.engine.mutations, [])
                self.assertNotIn(migration.FIELD, box.read_json(self.path))
                self.assert_preserved()

    def test_previous_rlimit_replay_profile_is_not_adopted(self):
        with self.fixture():
            self.engine.before["commit"] = lambda: 125
            self.assert_refused(migration.OFFER_REBUILD)
            pending = box.read_json(self.path)
            source = self.engine.containers[_SOURCE]
            old_profile = [source["HostConfig"].get(key) for key in migration._OPTIONS]
            old_profile.extend((["nofile=1024:8192", "nproc=7600:7600"], []))
            pending[migration.FIELD]["profile"] = hashlib.sha256(
                json.dumps(old_profile, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            box.atomic_json(self.path, pending)
            self.assert_refused(migration.PENDING)
            self.assert_refused(migration.PENDING, rebuild=True)
            self.assertEqual(self.engine.mutations, ["commit"])
            self.assertEqual(box.read_json(self.path), pending)
            self.assert_preserved()

    def test_new_snapshot_attempt_revokes_previous_rebuild_authority(self):
        with self.fixture():
            self.engine.before["commit"] = lambda: 125
            self.assert_refused(migration.OFFER_REBUILD)

            def timeout():
                self.assertEqual(
                    box.read_json(self.path)[migration.FIELD]["mode"], "snapshot"
                )
                raise box.BoxError("podman exceeded its timeout")

            self.engine.before["commit"] = timeout
            self.assert_refused(migration.PENDING)
            self.assert_refused(migration.PENDING, rebuild=True)
            self.assertEqual(self.engine.mutations, ["commit", "commit"])
            self.assert_preserved()

    def test_unverified_first_mount_chown_refuses(self):
        with self.fixture():
            self.engine.volume.update(NeedsCopyUp=False, NeedsChown=True)
            self.assert_refused(migration.REFUSED)
            self.assertEqual(self.engine.mutations, [])
            self.assert_preserved()

    def test_backup_cannot_capture_an_incomplete_swap_or_unbootstrapped_os(self):
        for field, value in (
            (migration.FIELD, {}),
            (
                migration.RECEIPT,
                {"id": _CANDIDATE, "bootstrap": True, "volume": [1, 2]},
            ),
        ):
            with self.subTest(field=field), self.fixture():
                data = box.read_json(self.path)
                data[field] = value
                box.atomic_json(self.path, data)
                args = SimpleNamespace(
                    project=self.project,
                    workspace=None,
                    recipient=[],
                    output=self.project.parent / "not-created.ocbox",
                )
                with (
                    patch.object(box, "check_backend"),
                    patch.object(backups.crypto, "tools"),
                    patch.object(backups, "owned_container") as inspect,
                    self.assertRaisesRegex(backups.BackupError, "not ready for backup"),
                ):
                    backups.make_backup(box, args)
                inspect.assert_not_called()
                self.assertFalse(args.output.exists())

    def test_ambiguous_snapshot_results_do_not_offer_an_os_rebuild(self):
        for outcome in (
            "timeout",
            "transport-error",
            "signal-exit",
            "tag-present",
            "absence-unverified",
            "source-changed",
        ):
            with self.subTest(outcome=outcome), self.fixture():

                def failure():
                    if outcome == "timeout":
                        # box.run wraps the raw timeout as a BoxError, not an exit.
                        raise box.BoxError("podman exceeded its timeout")
                    if outcome == "transport-error":
                        raise OSError("synthetic transport error")
                    if outcome == "absence-unverified":
                        self.engine.exists_errors["image"] = 125
                    if outcome == "source-changed":
                        self.engine.containers[_SOURCE]["Config"]["Labels"][
                            box.LABEL
                        ] = "f" * 32
                    return -9 if outcome == "signal-exit" else 125

                hooks = (
                    self.engine.after
                    if outcome == "tag-present"
                    else self.engine.before
                )
                hooks["commit"] = failure
                self.assert_refused(migration.PENDING)
                self.assertEqual(
                    box.read_json(self.path)[migration.FIELD]["mode"], "snapshot"
                )
                self.assert_refused(migration.PENDING, rebuild=True)
                self.assertEqual(self.engine.mutations, ["commit"])
                self.assertEqual(
                    self.engine.containers[_SOURCE]["Name"], self.original["container"]
                )
                self.assert_preserved()
                if outcome == "tag-present":
                    self.assert_completed(
                        self.convert(), image=_SNAPSHOT, bootstrap=False
                    )
                    self.assertEqual(self.engine.mutations.count("commit"), 1)

    def test_interrupted_mutations_resume_without_replaying_verified_side_effects(self):
        sequence = ["commit", "create", "retire", "publish", "rm"]
        for action in sequence[1:]:
            for phase in ("before", "after"):
                with self.subTest(action=action, phase=phase), self.fixture():
                    if phase == "before":

                        def interrupted():
                            raise KeyboardInterrupt

                        self.engine.before[action] = interrupted
                        with self.assertRaises(KeyboardInterrupt):
                            self.convert()
                    else:

                        def no_ack():
                            raise box.BoxError("synthetic acknowledgement lost")

                        self.engine.after[action] = no_ack
                        self.assert_refused(migration.PENDING)

                    index = sequence.index(action)
                    self.assertEqual(self.engine.mutations, sequence[: index + 1])
                    pending = box.read_json(self.path)
                    self.assertIn(migration.FIELD, pending)
                    self.assertNotIn(migration.RECEIPT, pending)
                    # Check externally observed names at each interruption.
                    stage = "oc-box-entrypoint-" + pending[migration.FIELD]["token"]
                    public_name = self.original["container"]
                    applied = index if phase == "after" else index - 1
                    expected = {}
                    if applied < 4:
                        expected[_SOURCE] = (
                            stage + "-old" if applied >= 2 else public_name
                        )
                    if applied >= 1:
                        expected[_CANDIDATE] = (
                            public_name if applied >= 3 else stage + "-new"
                        )
                    self.assertEqual(
                        {
                            ident: row["Name"]
                            for ident, row in self.engine.containers.items()
                        },
                        expected,
                    )
                    self.assert_preserved()
                    self.assert_completed(
                        self.convert(), image=_SNAPSHOT, bootstrap=False
                    )
                    repeated = [action] if phase == "before" else []
                    self.assertEqual(
                        self.engine.mutations,
                        sequence[:index] + repeated + sequence[index:],
                    )

    def test_foreign_object_or_third_home_user_fences_pending_swap(self):
        for interference in (
            "wrong-candidate-label",
            "wrong-candidate-home",
            "occupied-retired-name",
            "third-home-user",
        ):
            with self.subTest(interference=interference), self.fixture():

                def no_ack():
                    raise box.BoxError("synthetic create acknowledgement lost")

                self.engine.after["create"] = no_ack
                self.assert_refused(migration.PENDING)
                pending = self.path.read_bytes()
                record = box.read_json(self.path)[migration.FIELD]
                candidate = self.engine.containers[_CANDIDATE]
                if interference == "wrong-candidate-label":
                    candidate["Config"]["Labels"][_CONVERSION_LABEL] = "f" * 32
                elif interference == "wrong-candidate-home":
                    candidate["Mounts"][0]["Source"] = str(self.project)
                else:
                    foreign = deepcopy(candidate)
                    foreign["Id"] = _FOREIGN
                    foreign["Name"] = "foreign-container"
                    foreign["Config"]["Labels"] = {}
                    if interference == "occupied-retired-name":
                        foreign["Name"] = (
                            "oc-box-entrypoint-" + record["token"] + "-old"
                        )
                        foreign["Mounts"] = []
                    self.engine.containers[_FOREIGN] = foreign
                observed = deepcopy(self.engine.containers)
                self.assert_refused(migration.PENDING)
                self.assertEqual(self.engine.mutations, ["commit", "create"])
                self.assertEqual(self.engine.containers, observed)
                self.assertEqual(self.path.read_bytes(), pending)
                self.assert_preserved()

    def test_stop_intent_blocks_first_mutation_and_blocks_removal_after_publication(
        self,
    ):
        with self.fixture():
            workspace_control.request_stop(self.path)
            with self.assertRaisesRegex(box.BoxError, "Stop was requested"):
                self.convert()
            self.assertEqual(self.engine.mutations, [])
            self.assertEqual(box.read_json(self.path), self.original)
            self.assert_preserved()

        with self.fixture():

            def stop():
                workspace_control.request_stop(self.path)

            self.engine.after["publish"] = stop
            with self.assertRaisesRegex(box.BoxError, "Stop was requested"):
                self.convert()
            expected = ["commit", "create", "retire", "publish"]
            self.assertEqual(self.engine.mutations, expected)
            pending = self.path.read_bytes()
            self.assertIn(migration.FIELD, box.read_json(self.path))
            self.assertEqual(set(self.engine.containers), {_SOURCE, _CANDIDATE})
            self.assertEqual(
                self.engine.containers[_CANDIDATE]["Name"], self.original["container"]
            )
            self.assert_preserved()
            with self.assertRaisesRegex(box.BoxError, "Stop was requested"):
                self.convert()
            self.assertEqual(self.engine.mutations, expected)
            self.assertEqual(self.path.read_bytes(), pending)

            # A later explicit invocation can capture a new baseline and
            # verify the incomplete swap, not replay the publication.
            token = workspace_control.capture(self.path)
            self.assertNotEqual(token, self.token)
            self.assert_completed(
                self.convert(token=token), image=_SNAPSHOT, bootstrap=False
            )
            self.assertEqual(self.engine.mutations, [*expected, "rm"])
