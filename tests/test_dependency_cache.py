"""Dependency resolution success, not a copied input hash, authorizes reuse."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, sync_config as sync


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as target:
        for name, value in files.items():
            content = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o600
            target.addfile(member, io.BytesIO(content))
    return output.getvalue()


def installed_sdk(config, version="1.19.5", package="@opencode-ai/plugin"):
    """A complete local package graph, not version-only SDK metadata."""
    modules = config / "node_modules"
    for name, release, dependencies in (
        (package, version, {"fixture-sdk-runtime": "1.0.0"}),
        ("fixture-sdk-runtime", "1.0.0", {}),
    ):
        folder = modules / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "package.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "version": release,
                    "type": "module",
                    "exports": {".": {"import": "./index.js", "types": "./index.d.ts"}},
                    "dependencies": dependencies,
                }
            )
        )
        (folder / "index.js").write_text("export const fixture = true\n")
        (folder / "index.d.ts").write_text("export declare const fixture: boolean\n")


class DependencyCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-dependency-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.config = self.home / ".config/opencode"
        self.payload = archive({"package.json": '{"dependencies":{"custom":"1"}}'})

    def test_partial_node_modules_never_turns_copied_input_into_success(self):
        for version in ("1.19.5", "2.2.6"):
            with self.subTest(version=version):
                home = self.home / version
                self.assertFalse(sync.sync_config(home, self.payload, version))
                modules = home / ".config/opencode/node_modules"
                modules.mkdir()
                (modules / "partial").write_text("failed install")
                self.assertFalse(sync.sync_config(home, self.payload, version))
                self.assertFalse((modules / "partial").exists())

    def test_legacy_exact_custom_manifest_is_not_an_install_receipt(self):
        for legacy in (False, True):
            with self.subTest(input_only_state=legacy):
                home = self.home / str(legacy)
                config = home / ".config/opencode"
                config.mkdir(parents=True)
                (config / "package.json").write_text('{"dependencies":{"custom":"1"}}')
                (config / "node_modules").mkdir()
                (config / "node_modules/partial").touch()
                if legacy:
                    state = {
                        "source_hash": sync.input_hash(
                            {"package.json": b'{"dependencies":{"custom":"1"}}'},
                            "2.2.6",
                        ),
                        "runtime_version": "2.2.6",
                    }
                    (config.parent / ".oc-box-dependencies.json").write_text(
                        json.dumps(state)
                    )
                self.assertFalse(sync.sync_config(home, self.payload, "2.2.6"))

    def test_legacy_sdk_metadata_without_exported_code_is_not_complete(self):
        plugin = self.config / "node_modules/@opencode-ai/plugin"
        plugin.mkdir(parents=True)
        (plugin / "package.json").write_text(
            '{"name":"@opencode-ai/plugin","version":"1.19.5"}'
        )
        (self.config / "package.json").write_text(
            '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
        )
        payload = archive(
            {"package.json": '{"dependencies":{"@opencode-ai/plugin":"1.15.11"}}'}
        )
        self.assertFalse(sync.sync_config(self.home, payload, "1.19.5"))

    def test_completion_receipt_is_bound_to_exact_input_and_runtime(self):
        self.assertFalse(sync.sync_config(self.home, self.payload, "2.2.6"))
        (self.config / "node_modules").mkdir()
        before = (self.config.parent / ".oc-box-dependencies.json").read_bytes()
        for payload, version in (
            (archive({"package.json": '{"dependencies":{"custom":"2"}}'}), "2.2.6"),
            (self.payload, "2.2.7"),
        ):
            with self.subTest(version=version, different_input=payload != self.payload):
                with self.assertRaises(ValueError):
                    sync.complete_config(self.home, payload, version)
                self.assertEqual(
                    (self.config.parent / ".oc-box-dependencies.json").read_bytes(),
                    before,
                )
        sync.complete_config(self.home, self.payload, "2.2.6")
        self.assertTrue(sync.sync_config(self.home, self.payload, "2.2.6"))
        self.assertFalse(sync.sync_config(self.home, self.payload, "2.2.7"))

    def test_legacy_sdk_adoption_requires_code_and_transitive_files(self):
        for version, package in (
            ("1.19.5", "@opencode-ai/plugin"),
            ("2.2.6", "@opencode/plugin"),
        ):
            for missing in (
                None,
                "sdk code",
                "empty sdk code",
                "transitive code",
                "transitive manifest",
                "transitive module",
            ):
                with self.subTest(version=version, missing=missing):
                    home = self.home / version / str(missing)
                    config = home / ".config/opencode"
                    installed_sdk(config, version, package)
                    (config / "package.json").write_text(
                        json.dumps({"dependencies": {package: version}})
                    )
                    if missing == "sdk code":
                        (config / "node_modules" / package / "index.js").unlink()
                    elif missing == "empty sdk code":
                        (config / "node_modules" / package / "index.js").write_bytes(
                            b""
                        )
                    elif missing == "transitive code":
                        (config / "node_modules/fixture-sdk-runtime/index.js").unlink()
                    elif missing == "transitive manifest":
                        (
                            config / "node_modules/fixture-sdk-runtime/package.json"
                        ).unlink()
                    elif missing == "transitive module":
                        manifest = (
                            config / "node_modules/fixture-sdk-runtime/package.json"
                        )
                        value = json.loads(manifest.read_text())
                        value.pop("exports")
                        value.update(main="./index.js", module="./missing-module.js")
                        manifest.write_text(json.dumps(value))
                    payload = archive(
                        {
                            "package.json": json.dumps(
                                {"dependencies": {package: version}}
                            )
                        }
                    )
                    self.assertEqual(
                        sync.sync_config(home, payload, version), missing is None
                    )

    def test_pending_sdk_only_state_cannot_adopt_even_a_complete_package_tree(self):
        payload = archive(
            {"package.json": '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'}
        )
        self.assertFalse(sync.sync_config(self.home, payload, "1.19.5"))
        installed_sdk(self.config)
        self.assertFalse(sync.sync_config(self.home, payload, "1.19.5"))

    def test_legacy_sdk_proof_accepts_published_type_and_optional_package_shapes(self):
        for shape in (
            "inactive source condition",
            "empty type stub",
            "optional peer",
            "types only",
        ):
            with self.subTest(shape=shape):
                home = self.home / shape
                config = home / ".config/opencode"
                installed_sdk(config)
                sdk = config / "node_modules/@opencode-ai/plugin/package.json"
                dependency = config / "node_modules/fixture-sdk-runtime"
                value = json.loads(sdk.read_text())
                if shape == "inactive source condition":
                    value["exports"]["."]["sdk-source"] = "./unpublished-source.js"
                elif shape == "optional peer":
                    value["peerDependencies"] = {"optional-sdk-peer": "*"}
                    value["peerDependenciesMeta"] = {
                        "optional-sdk-peer": {"optional": True}
                    }
                elif shape == "empty type stub":
                    (dependency / "index.js").write_bytes(b"")
                else:
                    metadata = json.loads((dependency / "package.json").read_text())
                    metadata.pop("exports")
                    (dependency / "package.json").write_text(json.dumps(metadata))
                    (dependency / "index.js").unlink()
                sdk.write_text(json.dumps(value))
                manifest = '{"dependencies":{"@opencode-ai/plugin":"1.19.5"}}'
                (config / "package.json").write_text(manifest)
                self.assertTrue(
                    sync.sync_config(
                        home, archive({"package.json": manifest}), "1.19.5"
                    )
                )

    def test_success_receipt_invalidates_when_installed_dependency_files_change(self):
        for name in ("package.json", "package-lock.json"):
            with self.subTest(file=name):
                self.assertFalse(sync.sync_config(self.home, self.payload, "2.2.6"))
                (self.config / "node_modules").mkdir()
                sync.complete_config(self.home, self.payload, "2.2.6")
                (self.config / name).write_text('{"changed":"after install"}')
                self.assertFalse(sync.sync_config(self.home, self.payload, "2.2.6"))

    def test_failed_install_retries_real_sync_and_completes_only_after_zero(self):
        self.failed_install_retry("2.2.6")

    def test_failed_v1_resolution_retries_and_marks_only_success(self):
        self.failed_install_retry("1.19.5")

    def failed_install_retry(self, version):
        self.home.mkdir()
        project = self.root / "project"
        project.mkdir()
        registry = self.root / "registry/workspace.json"
        registry.parent.mkdir()
        (registry.parent / "controller").mkdir()
        data = {
            "id": "a" * 32,
            "home": str(self.home),
            "project": str(project),
            "opencode_version": version,
            "credentials": {"mode": "env", "env": [], "vault": []},
        }
        box.atomic_json(registry, data)
        installer = self.root / "installer.py"
        attempts = self.root / "attempts"
        installer.write_text("""import pathlib,sys
config=pathlib.Path(sys.argv[1]); count=pathlib.Path(sys.argv[2])
n=int(count.read_text())+1 if count.exists() else 1
count.write_text(str(n)); modules=config/'node_modules'; modules.mkdir(exist_ok=True)
(modules/('partial' if n==1 else 'installed')).write_text('synthetic')
sys.exit(1 if n==1 else 0)
""")
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.root / "host-config"),
            "XDG_DATA_HOME": str(self.root / "host-data"),
            "OPENCODE_TEST_HOME": str(self.home),
        }

        def command(_data, arguments, **_kwargs):
            if arguments[0] == "npm" or arguments[1:] == ["debug", "agent", "build"]:
                arguments = [
                    sys.executable,
                    str(installer),
                    str(self.config),
                    str(attempts),
                ]
            elif arguments[0] == "python3":
                arguments = [sys.executable, *arguments[1:]]
            return box.ContainerCommand(arguments, environment)

        original_run = box.run

        def run(arguments, **kwargs):
            if arguments[:2] == [sys.executable, str(installer)]:
                kwargs.setdefault("stdin", subprocess.DEVNULL)
                kwargs.setdefault("capture", True)
                return original_run(arguments, **kwargs)
            return subprocess.CompletedProcess(
                arguments,
                0,
                "[]" if "auth" in arguments and "export" in arguments else "",
                "",
            )

        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(box, "ensure_opencode_version"),
            patch.object(box, "config_archive", return_value=self.payload),
            patch.object(box, "exec_command", side_effect=command),
            patch.object(box, "run", side_effect=run),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(box.BoxError):
                box.prepare_runtime(registry, data, version)
            self.assertTrue((self.config / "node_modules/partial").is_file())
            box.prepare_runtime(registry, data, version)
            self.assertEqual(attempts.read_text(), "2")
            self.assertFalse((self.config / "node_modules/partial").exists())
            self.assertTrue((self.config / "node_modules/installed").is_file())
            box.prepare_runtime(registry, data, version)
            self.assertEqual(attempts.read_text(), "2")
