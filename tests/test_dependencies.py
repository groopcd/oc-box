import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import backup_crypto
import backups
import dependencies as deps
import oc_box as box
import updater
import vault


class DependencyTests(unittest.TestCase):
    def test_all_supported_distros_have_deduplicated_correct_package_commands(self):
        expected = {
            "tumbleweed": "sudo zypper refresh && sudo zypper install age shadow",
            "leap": "sudo zypper refresh && sudo zypper install age shadow",
            "ubuntu": "sudo apt update && sudo apt install age uidmap",
            "debian": "sudo apt update && sudo apt install age uidmap",
            "fedora": "sudo dnf install age shadow-utils",
            "arch": "sudo pacman -Syu --needed age shadow",
            "alpine": "sudo apk add age shadow-subids",
        }
        self.assertEqual(set(deps.DISTROS), set(box.DISTROS))
        for distro, command in expected.items():
            with self.subTest(distro=distro):
                keys = ["age", "age-keygen", "newuidmap", "newgidmap"]
                self.assertEqual(deps.install_commands(distro, keys), [command])
                for feature, (_, required) in deps.FEATURES.items():
                    for key in required:
                        if key not in ("linux", "memfd", "opencode", "cryptography"):
                            self.assertTrue(
                                deps.package_names(distro, [key]), (feature, key)
                            )
                self.assertEqual(
                    len(deps.package_names(distro, deps.BASELINE)), len(deps.BASELINE)
                )
                self.assertEqual(box.DISTROS[distro][1], deps.baseline_install(distro))

    def test_os_release_recognition_never_uses_workspace_default_or_shell_eval(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "os-release"
            for ident, expected in (
                ("opensuse-tumbleweed", "tumbleweed"),
                ("opensuse-slowroll", "tumbleweed"),
                ("opensuse-leap", "leap"),
                ("ubuntu", "ubuntu"),
                ("debian", "debian"),
                ("fedora", "fedora"),
                ("arch", "arch"),
                ("alpine", "alpine"),
                ("unknown", None),
                ("$(touch malicious)", None),
            ):
                with self.subTest(ident=ident):
                    release.write_text(f'ID="{ident}"\nID_LIKE="ubuntu debian"\n')
                    self.assertEqual(deps.host_distro(release), expected)
            release.write_text('ID="unterminated\n')
            self.assertIsNone(deps.host_distro(release))
            release.unlink()
            self.assertIsNone(deps.host_distro(release))

    def test_missing_optional_tools_only_disable_their_features(self):
        def probe(key):
            return key not in ("age", "age-keygen", "cryptography"), "probe: " + key

        with (
            patch.object(deps, "probe", side_effect=probe),
            patch.object(deps, "host_distro", return_value="alpine"),
        ):
            data = deps.report(["launch", "backup", "vault"])
            self.assertTrue(data["features"][0]["available"])
            self.assertFalse(data["features"][1]["available"])
            self.assertEqual(
                data["features"][1]["install_commands"], ["sudo apk add age"]
            )
            self.assertFalse(data["features"][2]["available"])
            self.assertIn(
                "uv tool install --force 'oc-box[vault]'",
                " ".join(data["features"][2]["guidance"]),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(deps.cli([]), 0)
                self.assertEqual(deps.cli(["--feature", "backup"]), 1)

    def test_unknown_os_reports_missing_names_without_guessing_a_package_manager(self):
        with patch.object(deps, "host_distro", return_value=None):
            message = deps.missing_message("Backup", ["age"])
        self.assertIn("Unrecognized host distro", message)
        self.assertNotIn("sudo", message)

    def test_doctor_is_read_only_without_podman_network_workspaces_or_secret_prompt(
        self,
    ):
        with (
            patch.object(deps, "probe", return_value=(False, "missing")),
            patch.object(deps, "host_distro", return_value="ubuntu"),
            patch.object(box, "check_backend") as backend,
            patch.object(box, "workspace_paths") as workspaces,
            patch.object(vault, "hidden") as prompt,
            patch.object(subprocess, "run") as execute,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                box.main(["doctor", "--feature", "backup-info", "--json"]), 1
            )
        data = json.loads(output.getvalue())
        self.assertEqual(data["distro"], "ubuntu")
        self.assertEqual(data["features"][0]["packages"], ["age", "python3"])
        for mocked in (backend, workspaces, prompt, execute):
            mocked.assert_not_called()

    def test_partial_age_install_reports_only_the_missing_command_and_one_package(self):
        with (
            patch.object(
                deps, "probe", side_effect=lambda key: (key != "age-keygen", key)
            ),
            patch.object(deps, "host_distro", return_value="arch"),
        ):
            with self.assertRaisesRegex(deps.DependencyError, "age-keygen") as error:
                deps.require("backup-info")
        self.assertIn("sudo pacman -Syu --needed age", str(error.exception))

    def test_missing_launch_dependencies_abort_before_workspace_creation_or_vault_unlock(
        self,
    ):
        with (
            patch.object(deps, "probe", return_value=(False, "missing")),
            patch.object(box, "create_workspace") as create,
            patch.object(box, "remove_resources") as remove,
            patch.object(vault, "hidden") as prompt,
            self.assertRaisesRegex(box.BoxError, "launch/reset"),
        ):
            box.main(["reset", "--project", "/tmp/synthetic-doctor-project"])
        for mocked in (create, remove, prompt):
            mocked.assert_not_called()

    def test_missing_restore_prerequisites_abort_before_decryption_or_destination_change(
        self,
    ):
        args = backups.parser_for("restore").parse_args(
            ["example.ocbox", "--project", "/tmp/synthetic-new-project"]
        )
        with (
            patch.object(deps, "probe", return_value=(False, "missing")),
            patch.object(backup_crypto, "opened") as opened,
            patch.object(backups, "activate_project") as activate,
            self.assertRaisesRegex(box.BoxError, "restore unavailable"),
        ):
            backups.restore_backup(box, args)
        opened.assert_not_called()
        activate.assert_not_called()

    def test_upgrade_dependency_failure_precedes_fetch_or_install(self):
        with (
            patch.object(updater, "package_managed", return_value=False),
            patch.object(os, "geteuid", return_value=1000),
            patch.object(deps, "probe", side_effect=lambda key: (key != "git", key)),
            patch.object(updater, "git") as fetch,
            self.assertRaisesRegex(updater.UpdateError, "GitHub/source"),
        ):
            updater.upgrade(Path("/tmp/example-installation"))
        fetch.assert_not_called()

    def test_purge_checks_history_tools_before_confirmation_or_stopping(self):
        from types import SimpleNamespace

        with (
            patch.object(
                deps, "probe", side_effect=lambda key: (key != "opencode", key)
            ),
            patch.object(box, "stop_container") as stop,
            patch("builtins.input") as confirmation,
            self.assertRaisesRegex(box.BoxError, "History-preserving purge"),
        ):
            box.purge(
                Path("/tmp/example-registry"),
                {},
                SimpleNamespace(all=False, delete_sessions=False),
            )
        stop.assert_not_called()
        confirmation.assert_not_called()

    def test_gnu_du_is_required_instead_of_accepting_busybox(self):
        with (
            patch.object(deps, "executable", return_value="/usr/bin/du"),
            patch.object(
                subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"BusyBox", b""),
            ),
        ):
            available, detail = deps.probe("du")
        self.assertFalse(available)
        self.assertIn("GNU du", detail)

    def test_hashed_certificate_directory_is_not_mistaken_for_missing_cas(self):
        import ssl
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "0123abcd.0").write_text("synthetic trust fixture")
            with (
                patch.object(
                    ssl,
                    "get_default_verify_paths",
                    return_value=SimpleNamespace(capath=temporary),
                ),
                patch.object(ssl, "create_default_context") as context,
            ):
                context.return_value.cert_store_stats.return_value = {"x509_ca": 0}
                self.assertTrue(deps.probe("ca-certificates")[0])

    def test_private_podman_helpers_and_alternative_oci_runtime_are_accepted(self):
        with (
            patch.object(deps.shutil, "which", return_value=None),
            patch.object(os.path, "isfile", return_value=True),
            patch.object(os, "access", return_value=True),
        ):
            self.assertEqual(
                deps.executable("catatonit"), "/usr/libexec/podman/catatonit"
            )
        with patch.object(
            deps.shutil,
            "which",
            side_effect=lambda name: "/usr/bin/runc" if name == "runc" else None,
        ):
            self.assertEqual(deps.executable("oci-runtime"), "/usr/bin/runc")

    def test_storage_scan_explains_optional_coreutils_fix_without_running_du(self):
        import status_report

        with (
            patch.object(deps, "probe", return_value=(False, "BusyBox")),
            patch.object(deps, "host_distro", return_value="alpine"),
            patch.object(subprocess, "run") as execute,
        ):
            count, reason = status_report.directory_bytes(Path("/tmp/example"))
        self.assertIsNone(count)
        self.assertIn("sudo apk add coreutils", reason)
        execute.assert_not_called()
