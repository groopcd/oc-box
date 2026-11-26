import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from oc_box import native_preflight as preflight
from oc_box import oc_box as box
from oc_box import opencode_v2_io, sync_config


VERSION = "2.4.1"
PROOF = "a" * 64
IMAGE = "b" * 64


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        arch = {"x86_64": 62, "aarch64": 183}.get(os.uname().machine)
        if arch is None:
            self.skipTest("no audited native platform")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.prefix = Path(temp.name).resolve()
        self.executable = (
            self.prefix / "lib/node_modules/@opencode/cli/bin/opencode.exe"
        )
        self.alias = self.prefix / "bin/opencode"
        self.native = bytearray(128)
        self.native[:7] = b"\x7fELF\x02\x01\x01"
        self.native[16] = 3
        self.native[18] = arch
        self.native[20] = 1
        self.digest = hashlib.sha256(self.native).hexdigest()

    def install(self):
        namespace = {"__name__": "fixture"}
        exec(preflight.FINGERPRINT_SOURCE, namespace)
        package = namespace["platform_package"]()
        cli = self.executable.parent.parent
        cli.mkdir(parents=True)
        (cli / "package.json").write_text(
            json.dumps(
                {
                    "name": "@opencode/cli",
                    "version": VERSION,
                    "bin": {
                        "opencode": "bin/opencode.exe",
                        "opencode2": "bin/opencode.exe",
                    },
                    "optionalDependencies": {package: VERSION},
                }
            )
        )
        platform = cli / "node_modules" / package
        (platform / "bin").mkdir(parents=True)
        (platform / "package.json").write_text(
            json.dumps({"name": package, "version": VERSION})
        )
        program = platform / "bin/opencode"
        program.write_bytes(self.native)
        program.chmod(0o755)
        return package, program

    def test_artifact_only_package_proves_bytes_without_extracting_or_running(self):
        context = {"__name__": "fixture"}
        exec(preflight.ARTIFACT_SOURCE, context)
        package = "@opencode/cli-linux-x64"
        path = f"/{package}/-/cli-linux-x64-{VERSION}.tgz"
        for variant in ("exact", "integrity", "extra-member", "wrong-release"):
            with self.subTest(variant=variant):
                metadata = json.dumps(
                    {
                        "name": package,
                        "version": VERSION if variant != "wrong-release" else "2.0.0",
                    }
                ).encode()
                files = {
                    "package/package.json": metadata,
                    "package/bin/opencode": self.native,
                }
                if variant == "extra-member":
                    files["package/extra"] = b"unwanted"
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode="w") as archive:
                    for name, content in files.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                content = gzip.compress(raw.getvalue())
                proof = hashlib.sha512(
                    content if variant != "integrity" else b"other"
                ).digest()
                manifest = {
                    "dist": {
                        "tarball": "https://registry.npmjs.org" + path,
                        "integrity": "sha512-" + base64.b64encode(proof).decode(),
                    },
                }

                @contextlib.contextmanager
                def response(request):
                    self.assertEqual(request, path)
                    yield io.BytesIO(content)

                with patch.dict(
                    context,
                    {
                        "release_manifest": lambda *_: manifest,
                        "public_response": response,
                    },
                ):
                    if variant in ("exact", "extra-member"):
                        self.assertEqual(
                            context["payload_digest"](package, VERSION), self.digest
                        )
                    else:
                        with self.assertRaises(ValueError):
                            context["payload_digest"](package, VERSION)

    def call(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                preflight.FINGERPRINT_SOURCE,
                str(self.prefix),
                *arguments,
            ],
            timeout=10,
            capture_output=True,
            check=False,
        )

    def assert_refused(self, result):
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (1, b"", b"")
        )

    def test_exact_native_target_is_materialized_without_running_it(self):
        _, program = self.install()
        self.assert_refused(self.call())
        self.assert_refused(self.call("--materialize", VERSION, PROOF))
        self.assertFalse(self.alias.exists())
        self.assertFalse(self.executable.exists())
        expected = (self.digest + "\n").encode()
        materialized = self.call("--materialize", VERSION, self.digest)
        self.assertEqual(
            (materialized.returncode, materialized.stdout, materialized.stderr),
            (0, expected, b""),
        )
        self.assertEqual(self.call().stdout, expected)
        self.assertEqual(self.alias.resolve(), self.executable)
        self.assertEqual(program.stat().st_ino, self.executable.stat().st_ino)

    def test_missing_ambiguous_stale_or_linked_platform_package_refuses(self):
        package, program = self.install()
        extra = self.prefix / "lib/node_modules" / package
        extra.mkdir()
        self.assert_refused(self.call("--materialize", VERSION, self.digest))
        extra.rmdir()
        metadata = program.parent.parent / "package.json"
        metadata.write_text(json.dumps({"name": package, "version": "2.4.0"}))
        self.assert_refused(self.call("--materialize", VERSION, self.digest))
        metadata.write_text(json.dumps({"name": package, "version": VERSION}))
        program.rename(program.with_suffix(".elsewhere"))
        program.symlink_to(program.with_suffix(".elsewhere"))
        self.assert_refused(self.call("--materialize", VERSION, self.digest))

    def test_wrapper_external_target_and_unexpected_elf_identity_refuse(self):
        self.install()
        self.assertEqual(self.call("--materialize", VERSION, self.digest).returncode, 0)
        self.alias.unlink()
        self.alias.write_text("#!/bin/sh\nexit 0\n")
        self.alias.chmod(0o755)
        self.assert_refused(self.call())
        self.alias.unlink()
        self.alias.symlink_to("/usr/bin/python3")
        self.assert_refused(self.call())
        self.alias.unlink()
        self.alias.symlink_to("../" + str(self.executable.relative_to(self.prefix)))
        self.native[18] = 255
        self.executable.write_bytes(self.native)
        self.assert_refused(self.call())


class _Engine:
    """Local deterministic Podman protocol fake, not a nested engine."""

    def __init__(self):
        self.calls = []
        self.objects = {}
        self.probe_status = 0
        self.probe_output = (PROOF + "\n").encode()
        self.interrupt = False
        self.refuse_stop = False
        self.refuse_remove = False
        self.bad_label = False
        self.bad_mount = False
        self.ambiguous_create = False
        self.rootless = True
        self.match_image = True

    def find(self, key):
        return next(
            (row for name, row in self.objects.items() if key in (name, row["id"])),
            None,
        )

    def __call__(self, path, env, args, **limits):
        self.calls.append((path, env, args, limits))
        if args[:2] == ("info", "--format"):
            return 0, b"true\n" if self.rootless else b"false\n"
        if args[:2] == ("image", "inspect"):
            ref = args[-1]
            digest = ref.split("@")[1] if self.match_image else "sha256:" + "0" * 64
            return 0, f"{IMAGE}\n{digest}\nlinux\namd64\n".encode()
        if args[:2] == ("container", "exists"):
            return int(self.find(args[-1]) is None), b""
        if args[0] == "create":
            name, source = (
                args[args.index("--name") + 1],
                args[args.index("--volume") + 1],
            )
            phase = name.rsplit("-", 1)[-1]
            cid = ("c" if phase == "stage" else "d") * 64
            labels = dict(
                args[i + 1].split("=", 1)
                for i in range(len(args))
                if args[i] == "--label"
            )
            if self.bad_label:
                labels[preflight._LABEL] = "foreign"
            self.objects[name] = {
                "id": cid,
                "name": name,
                "phase": phase,
                "labels": labels,
                "state": {
                    "Status": "configured",
                    "Running": False,
                    "Pid": 0,
                    "ExitCode": 0,
                },
                "mounts": [
                    {
                        "Type": "bind",
                        "Source": "/unexpected"
                        if self.bad_mount
                        else source.split(":")[0],
                        "Destination": "/scratch",
                        "RW": phase == "stage",
                    }
                ],
                "network": "none" if phase == "probe" else "pasta",
            }
            if self.ambiguous_create:
                raise preflight._CommandError()
            return 0, (cid + "\n").encode()
        if args[:2] == ("container", "inspect"):
            row = self.find(args[-1])
            if row is None:
                return 125, b""
            fields = (
                row["id"],
                row["name"],
                row["labels"],
                row["state"],
                IMAGE,
                row["mounts"],
                {},
                row["network"],
            )
            return 0, "".join(json.dumps(value) + "\n" for value in fields).encode()
        if args[0] == "start":
            row = self.find(args[-1])
            if self.interrupt:
                row["state"].update(Status="running", Running=True, Pid=234)
                raise KeyboardInterrupt
            status, output = (
                (0, (PROOF + "\n").encode())
                if row["phase"] == "stage"
                else (self.probe_status, self.probe_output)
            )
            row["state"].update(Status="exited", ExitCode=status)
            return status, output
        if args[0] == "stop":
            if self.refuse_stop:
                return 125, b""
            self.find(args[-1])["state"].update(
                Status="exited", Running=False, Pid=0, ExitCode=137
            )
            return 0, b""
        if args[0] == "rm":
            if self.refuse_remove:
                return 125, b""
            del self.objects[self.find(args[-1])["name"]]
            return 0, b""
        raise AssertionError(args[:2])


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.scratch = Path(temp.name) / "scratch"
        self.scratch.mkdir()
        self.engine = _Engine()
        exists = Path.exists

        def guarded_exists(path):
            return (
                False
                if str(path) in ("/run/.containerenv", "/.dockerenv")
                else exists(path)
            )

        mocks = (
            patch.object(preflight, "_invoke", self.engine),
            patch.object(preflight.shutil, "which", return_value="/usr/bin/podman"),
            patch.object(preflight.os, "geteuid", return_value=1000),
            patch.object(
                preflight.os,
                "uname",
                return_value=type("Uname", (), {"machine": "x86_64"})(),
            ),
            patch.object(preflight.tempfile, "mkdtemp", return_value=str(self.scratch)),
            patch.object(Path, "exists", guarded_exists),
            patch.dict(
                os.environ,
                {
                    "HOME": "/example/private/home",
                    "XDG_DATA_HOME": "/example/local/data",
                    "XDG_RUNTIME_DIR": "/example/runtime",
                    "GH_TOKEN": "synthetic-secret",
                    "OC_BOX_CONTAINER": "0",
                    "CONTAINER_HOST": "ssh://example.invalid",
                    "CONTAINERS_CONF_OVERRIDE": "/example/foreign",
                    "HTTPS_PROXY": "http://example.invalid",
                },
                clear=True,
            ),
        )
        for mock in mocks:
            mock.start()
            self.addCleanup(mock.stop)

    def refused(self, category):
        with self.assertRaises(preflight.Refusal) as caught:
            preflight.check(box, VERSION)
        self.assertEqual(caught.exception.category, category)
        self.assertEqual(str(caught.exception), category)
        return caught.exception

    def test_ordered_staging_then_offline_probe_and_private_environment(self):
        self.assertEqual(preflight.check(box, VERSION), PROOF)
        commands = [args for _, _, args, _ in self.engine.calls]
        create = [cmd for cmd in commands if cmd[0] == "create"]
        self.assertEqual(len(create), 2)
        self.assertTrue(all("--pull=never" in cmd for cmd in create))
        self.assertTrue(
            all(
                "--http-proxy=false" in cmd and "--env-host=false" in cmd
                for cmd in create
            )
        )
        self.assertTrue(
            all(
                "--read-only" in cmd and "--image-volume=ignore" in cmd
                for cmd in create
            )
        )
        self.assertFalse(any("--publish" in cmd for cmd in create))
        self.assertIn("--network=pasta:--no-map-gw", create[0])
        self.assertIn("--network=none", create[1])
        self.assertFalse(any(arg.startswith("--dns") for arg in create[1]))
        self.assertIn(":/scratch:ro,", create[1][create[1].index("--volume") + 1])
        self.assertIn(("rm", "c" * 64), commands[: commands.index(create[1])])
        self.assertEqual(
            json.loads(create[1][-1])["plugin"], box.container_build_plugin(VERSION)
        )
        for _, env, _, _ in self.engine.calls:
            self.assertEqual(
                set(env),
                {
                    "HOME",
                    "PATH",
                    "LANG",
                    "XDG_CONFIG_HOME",
                    "XDG_DATA_HOME",
                    "XDG_RUNTIME_DIR",
                    "CONTAINERS_CONF",
                },
            )
            self.assertEqual(env["HOME"], str(self.scratch / "home"))
        self.assertFalse(self.scratch.exists())
        self.assertFalse(self.engine.objects)

    def test_noncanonical_and_nested_refuse_before_engine(self):
        for version in ("2.4", "v2.4.1", "2.04.1", "2.4.1-canary", "1.19.5"):
            with (
                self.subTest(version=version),
                self.assertRaises(preflight.Refusal) as caught,
            ):
                preflight.check(box, version)
            self.assertEqual(caught.exception.category, "unsupported-version")
        with patch.dict(os.environ, OC_BOX_CONTAINER="1"):
            self.refused("backend")
        self.assertFalse(self.engine.calls)

    def test_incorrect_image_manifest_refuses_before_scratch_container(self):
        self.engine.match_image = False
        self.refused("stage-image")
        self.assertFalse(
            any(args[0] == "create" for _, _, args, _ in self.engine.calls)
        )
        self.assertFalse(self.scratch.exists())

    def test_confirmed_plugin_refusal_includes_exact_artifact_after_cleanup(self):
        self.engine.probe_status, self.engine.probe_output = 44, b""
        self.assertEqual(self.refused("build-plugin").proof, PROOF)
        self.assertFalse(self.scratch.exists())
        self.assertFalse(self.engine.objects)

    def test_generic_readiness_and_fingerprint_drift_are_never_confirmed(self):
        self.engine.probe_status, self.engine.probe_output = 46, b""
        self.assertIsNone(self.refused("probe").proof)
        self.scratch.mkdir()
        self.engine.probe_status, self.engine.probe_output = 0, b"e" * 64 + b"\n"
        self.assertIsNone(self.refused("artifact").proof)

    def test_interrupt_stops_rechecks_removes_by_exact_id_then_propagates(self):
        self.engine.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            preflight.check(box, VERSION)
        commands = [args for _, _, args, _ in self.engine.calls]
        self.assertIn(("stop", "--time=3", "c" * 64), commands)
        self.assertIn(("rm", "c" * 64), commands)
        self.assertFalse(self.scratch.exists())

    def test_unverified_stop_keeps_scratch_and_never_uses_force(self):
        self.engine.interrupt = self.engine.refuse_stop = True
        self.assertIsNone(self.refused("cleanup").proof)
        self.assertTrue(self.scratch.exists())
        self.assertFalse(any(cmd[0] == "rm" for _, _, cmd, _ in self.engine.calls))

    def test_removal_failure_keeps_scratch_and_never_promotes_stage_result(self):
        self.engine.refuse_remove = True
        self.assertIsNone(self.refused("cleanup").proof)
        self.assertTrue(self.scratch.exists())
        self.assertEqual(
            len([cmd for _, _, cmd, _ in self.engine.calls if cmd[0] == "create"]), 1
        )

    def test_unverified_identity_never_starts_or_deletes(self):
        self.engine.bad_label = True
        self.refused("cleanup")
        self.assertTrue(self.scratch.exists())
        self.assertFalse(
            any(cmd[0] in ("start", "rm", "stop") for _, _, cmd, _ in self.engine.calls)
        )

    def test_unexpected_mount_refuses_before_start_and_cleans_owned_container(self):
        self.engine.bad_mount = True
        self.refused("backend")
        self.assertFalse(self.engine.objects)
        self.assertFalse(any(cmd[0] == "start" for _, _, cmd, _ in self.engine.calls))
        self.assertFalse(self.scratch.exists())

    def test_uncertain_create_recovers_only_a_matching_identity(self):
        self.engine.ambiguous_create = True
        self.refused("scratch-create")
        self.assertFalse(self.engine.objects)
        self.assertIn(("rm", "c" * 64), [cmd for _, _, cmd, _ in self.engine.calls])
        budgets = [
            limits["timeout"]
            for _, _, args, limits in self.engine.calls
            if args[0] == "create"
        ]
        self.assertEqual(budgets, [90])
        self.assertLess(preflight._CONTROL_SECONDS, budgets[0])


class EmbeddedCodeTests(unittest.TestCase):
    def test_native_version_banner_requires_exact_patch_without_extra_output(self):
        scope = {"__name__": "fixture"}
        exec(preflight._PROBE, scope)
        with tempfile.TemporaryDirectory() as working:
            program = Path(working) / "native"
            for output, expected in (
                ("2.4.1", True),
                ("opencode v2.4.1", True),
                ("v2.4.1", True),
                ("opencode v2.4.0", False),
                ("opencode v2.4.1-rc.1", False),
                ("extra\n2.4.1", False),
            ):
                with self.subTest(output=output):
                    program.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
                    program.chmod(0o700)
                    self.assertIs(scope["agrees"](str(program), VERSION), expected)

    def test_stage_fetches_only_one_platform_artifact_and_only_copies_wrapper_manifest(
        self,
    ):
        scope = {"__name__": "fixture"}
        exec(preflight._STAGE, scope)
        with tempfile.TemporaryDirectory() as working:
            root = Path(working)
            runtime, wrapper = root / "runtime", root / "wrapper"
            calls = []

            def npm(*arguments):
                calls.append(arguments)
                if arguments[0] == "pack":
                    with tarfile.open(
                        wrapper / f"opencode-cli-{VERSION}.tgz", "w:gz"
                    ) as archive:
                        for name, value in (
                            ("package/package.json", b"{}"),
                            ("package/postinstall.mjs", b"never execute"),
                        ):
                            member = tarfile.TarInfo(name)
                            member.size = len(value)
                            archive.addfile(member, io.BytesIO(value))

            platform = "@opencode/cli-linux-x64"
            scope["stage_cli"](npm, runtime, wrapper, platform, VERSION)
            self.assertEqual(
                calls,
                [
                    (
                        "pack",
                        "--pack-destination",
                        str(wrapper),
                        f"@opencode/cli@{VERSION}",
                    ),
                    (
                        "install",
                        "--global",
                        f"--prefix={runtime}",
                        f"{platform}@{VERSION}",
                    ),
                ],
            )
            target = runtime / "lib/node_modules/@opencode/cli"
            self.assertEqual(
                [entry.name for entry in target.iterdir()], ["package.json"]
            )
            self.assertEqual((target / "package.json").read_bytes(), b"{}")

    def test_registry_tunnel_never_dials_arbitrary_endpoint(self):
        scope = {"__name__": "fixture"}
        exec(preflight._STAGE, scope)
        cls = scope["Tunnel"]
        for request in (
            b"CONNECT evil.invalid:443 HTTP/1.1\r\n\r\n",
            b"CONNECT registry.npmjs.org:80 HTTP/1.1\r\n\r\n",
            b"GET https://registry.npmjs.org/x HTTP/1.1\r\n\r\n",
        ):
            tunnel = object.__new__(cls)
            tunnel.connection = Mock()
            tunnel.rfile = io.BytesIO(request)
            with patch.object(scope["socket"], "create_connection") as dial:
                tunnel.handle()
            dial.assert_not_called()

    def test_only_readiness_explicit_failure_can_be_confirmed_and_cleanup_can_veto(
        self,
    ):
        stage, script, settings = preflight._scripts(box, VERSION)
        compile(stage, "<npm-stage>", "exec")
        scope = {"__name__": "fixture"}
        exec(script, scope)
        original_dir = Path.cwd()
        for failure, teardown, expected in (
            (None, 0, PROOF),
            (opencode_v2_io.BUILD_PLUGIN_FAILED, 0, 44),
            (opencode_v2_io.BUILD_PLUGIN_TIMEOUT, 0, opencode_v2_io.FixedError),
            (opencode_v2_io.BUILD_PLUGIN_FAILED, 137, 45),
            (None, 137, 45),
        ):
            with (
                self.subTest(failure=failure, teardown=teardown),
                tempfile.TemporaryDirectory() as working,
            ):
                root = Path(working)
                values = {
                    **json.loads(settings),
                    "home": str(root / "home"),
                    "project": str(root / "project"),
                }

                @contextlib.contextmanager
                def server(_binary, *, exit_info, startup_deadline):
                    try:

                        def request(method, route, **kwargs):
                            if failure:
                                raise scope["bridge"]["FixedError"](failure)
                            return {
                                "data": [
                                    {
                                        "id": "oc-box.build-permissions",
                                        "state": {"status": "active"},
                                    }
                                ]
                            }

                        request.alive = lambda: True
                        yield request
                    finally:
                        exit_info.update(code=teardown, forced=False)

                def copy(source, destination, **kwargs):
                    destination.mkdir(parents=True)

                try:
                    with (
                        patch.dict(os.environ, os.environ.copy(), clear=True),
                        patch.object(scope["os"], "umask"),
                        patch.object(scope["resource"], "setrlimit"),
                        patch.object(scope["shutil"], "copytree", copy),
                        patch.dict(
                            scope["bridge"],
                            {"server": server, "FixedError": opencode_v2_io.FixedError},
                        ),
                        patch.dict(
                            scope["sdk"], {"sdk_modules_match": lambda *args: True}
                        ),
                        patch.dict(
                            scope["artifact"], {"fingerprint": lambda *args: PROOF}
                        ),
                        patch.dict(scope, {"agrees": lambda *args: True}),
                    ):
                        if isinstance(expected, str):
                            self.assertEqual(
                                scope["probe"](VERSION, PROOF, values), expected
                            )
                        elif isinstance(expected, int):
                            with self.assertRaises(SystemExit) as caught:
                                scope["probe"](VERSION, PROOF, values)
                            self.assertEqual(caught.exception.code, expected)
                        else:
                            with self.assertRaises(expected):
                                scope["probe"](VERSION, PROOF, values)
                finally:
                    os.chdir(original_dir)

    def test_sdk_stage_lock_validation_reuses_runtime_security_policy(self):
        scope = {"__name__": "fixture", "sdk": vars(sync_config)}
        exec(preflight._STAGE, scope)
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "package-lock.json"
            item = {
                "version": VERSION,
                "resolved": "https://registry.npmjs.org/@opencode/plugin/-/plugin.tgz",
                "integrity": "sha512-" + "A" * 86 + "==",
            }
            packages = {"": {}, "node_modules/@opencode/plugin": item}
            lock.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
            self.assertTrue(scope["canonical_lock"](lock, VERSION))
            packages["node_modules/glob"] = {**item, "version": "10.5.0"}
            lock.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
            self.assertFalse(scope["canonical_lock"](lock, VERSION))
            del packages["node_modules/glob"]
            item["resolved"] = "https://example.invalid/plugin.tgz"
            lock.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
            self.assertFalse(scope["canonical_lock"](lock, VERSION))

    def test_sdk_publish_preserves_internal_link_but_rejects_escape(self):
        scope = {"__name__": "fixture"}
        exec(preflight._STAGE, scope)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source"
            source.mkdir()
            (source / "script.js").write_text("export default {}")
            (source / "tool").symlink_to("script.js")
            destination = root / "approved"
            scope["publish_sdk"](source, destination)
            self.assertEqual(os.readlink(destination / "tool"), "script.js")
            (source / "tool").unlink()
            (root / "outside").write_text("outside")
            (source / "tool").symlink_to("../outside")
            with self.assertRaises(ValueError):
                scope["publish_sdk"](source, root / "rejected")
            self.assertFalse((root / "rejected").exists())


class BoundedEngineProcessTests(unittest.TestCase):
    def test_unbounded_output_and_timeouts_become_fixed_refusals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tool = root / "engine"
            env = {"XDG_CONFIG_HOME": temp, "PATH": "/usr/bin:/bin"}
            for body, timeout in (
                ("echo synthetic private diagnostic >&2\nexec yes payload\n", 2),
                ("exec sleep 10\n", 0.05),
            ):
                with self.subTest(timeout=timeout):
                    tool.write_text("#!/bin/sh\n" + body)
                    tool.chmod(0o700)
                    with self.assertRaises(preflight.Refusal) as caught:
                        preflight._checked(
                            str(tool),
                            env,
                            (),
                            category="stage",
                            limit=65,
                            timeout=timeout,
                        )
                    self.assertEqual(str(caught.exception), "stage")
                    self.assertIsNone(caught.exception.proof)


if __name__ == "__main__":
    unittest.main()
