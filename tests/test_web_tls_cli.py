import contextlib
import errno
import io
import json
import os
import pty
import select
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from oc_box import dependencies, oc_box, web_tls, web_tls_cli


class WebTLSCLITests(unittest.TestCase):
    def test_local_check_reports_helper_and_certificate_status_independently(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"HOME": temporary}, clear=True),
            patch.object(web_tls, "find_helper", return_value=None),
            patch.object(web_tls, "validate") as validate,
            patch.object(oc_box, "detect_opencode_release") as native,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                oc_box.main(["web", "tls", "check", "localhost", "--json"]), 0
            )
            directory = Path(temporary) / ".local/share/oc-box/web-tls/localhost"
            self.assertEqual(
                [call.args for call in validate.call_args_list],
                [
                    (directory / "server.pem", directory / "server-key.pem", origin)
                    for origin in web_tls_cli.LOCAL_ORIGINS
                ],
            )
            self.assertFalse(directory.exists())
            native.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["certificate"], "valid")
        self.assertEqual(result["browser_trust"], "unverified")
        self.assertEqual(result["helpers"], {"mkcert": False, "certutil": False})

    def test_supplied_material_does_not_consult_optional_helpers_or_log_paths(self):
        args = [
            "web",
            "tls",
            "check",
            "supplied",
            "--origin",
            "https://workspace.example",
            "--cert",
            "/private/chain.pem",
            "--key",
            "/private/key.pem",
            "--json",
        ]
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(web_tls, "find_helper") as helper,
            patch.object(
                web_tls, "validate", side_effect=web_tls.WebTLSError("key_file")
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(oc_box.main(args), 1)
        self.assertEqual(json.loads(output.getvalue())["diagnostic"], "key_file")
        self.assertNotIn("/private", output.getvalue())
        helper.assert_not_called()

    def test_network_origin_refused_by_local_profile_and_incomplete_check_errors(self):
        paths = ["--cert", "/unused/chain.pem", "--key", "/unused/key.pem"]
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(web_tls, "validate") as validate,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                oc_box.main(
                    [
                        "web",
                        "tls",
                        "check",
                        "localhost",
                        "--json",
                        *paths,
                        "--origin",
                        "https://workspace.example",
                    ]
                ),
                1,
            )
            for args in (["supplied"], ["localhost", *paths]):
                with self.assertRaisesRegex(oc_box.BoxError, "requires|together"):
                    oc_box.main(["web", "tls", "check", *args])
        self.assertEqual(json.loads(output.getvalue())["diagnostic"], "profile_origin")
        validate.assert_not_called()

    def test_workspace_container_refuses_operations_but_can_get_help(self):
        with (
            patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}, clear=True),
            patch.object(web_tls, "find_helper") as helper,
            patch.object(web_tls, "validate") as validate,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as result:
                oc_box.main(["web", "tls", "--help"])
            self.assertEqual(result.exception.code, 0)
            for action in ("check", "setup"):
                with self.assertRaisesRegex(oc_box.BoxError, "browser host"):
                    oc_box.main(["web", "tls", action, "localhost"])
        helper.assert_not_called()
        validate.assert_not_called()

    def test_tls_entrypoint_never_sets_up_the_launch_journal(self):
        with (
            patch.object(
                oc_box.sys, "argv", ["oc-box", "web", "tls", "check", "localhost"]
            ),
            patch.object(oc_box, "main", return_value=1),
            patch.object(oc_box.lifecycle_output, "invocation") as invocation,
            patch.object(oc_box.lifecycle_output, "console_event") as event,
        ):
            self.assertEqual(oc_box.entrypoint(), 1)
        invocation.assert_not_called()
        event.assert_not_called()

    def test_doctor_optional_helpers_do_not_determine_default_exit(self):
        original = dependencies.probe

        def available(key):
            return (
                original(key) if key in ("mkcert", "certutil") else (True, "available")
            )

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(web_tls, "find_helper", return_value=None),
            patch.object(dependencies, "probe", side_effect=available),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(dependencies.cli([]), 0)
            self.assertEqual(dependencies.cli(["--feature", "web-tls-local"]), 1)
            result = dependencies.report(["web-tls-local"])["features"][0]
        self.assertEqual(result["missing"], ["mkcert", "certutil"])

    def test_doctor_container_does_not_recommend_installing_host_only_helpers(self):
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}, clear=True):
            feature = dependencies.report(["web-tls-local"])["features"][0]
        self.assertFalse(feature["available"])
        self.assertEqual(feature["packages"], [])
        self.assertEqual(feature["install_commands"], [])
        self.assertIn("browser host", feature["guidance"][0])

    def test_xdg_paths_cannot_relocate_the_private_tls_directory(self):
        for value in ("", "relative/example", "/example/project"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"XDG_DATA_HOME": value}, clear=True),
                patch.object(Path, "home", return_value=Path("/example/host")),
            ):
                self.assertEqual(
                    web_tls_cli.local_directory(),
                    Path("/example/host/.local/share/oc-box/web-tls/localhost"),
                )

    def test_setup_refuses_workspace_projects_and_existing_custom_binds(self):
        tls = Path("/example/home/.local/share/oc-box/web-tls")
        for record, container, message in (
            ({"project": str(tls.parent)}, None, "registered project overlaps"),
            (
                {"project": "/example/project", "podman_args": ["--volume=fixture"]},
                {"Mounts": [{"Type": "bind", "Source": str(tls.parent)}]},
                "registered workspace bind overlaps",
            ),
        ):
            with (
                self.subTest(message=message),
                patch.object(
                    oc_box, "workspace_paths", return_value=[Path("/example/registry")]
                ),
                patch.object(oc_box, "read_json", return_value=record),
                patch.object(oc_box, "inspect_owned", return_value=container),
                self.assertRaisesRegex(oc_box.BoxError, message),
            ):
                web_tls_cli._ensure_not_registered_project(oc_box, tls)

    def test_live_workspace_mount_and_export_policy_reserves_tls_files(self):
        from oc_box import sharing

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / ".local/share/oc-box/web-tls"
            nested = root / "example"
            nested.mkdir(parents=True)
            with patch.object(Path, "home", return_value=home):
                for project in (root.parent, root, nested):
                    with (
                        self.subTest(project=project),
                        self.assertRaisesRegex(oc_box.BoxError, "private host web TLS"),
                    ):
                        oc_box.validate_project(project)
                self.assertIn(root, sharing.protected_paths(oc_box))

    def test_nss_confirm_uses_controlling_tty_and_requires_exact_phrase(self):
        snippet = (
            "from oc_box.web_tls_cli import _approve_browser_trust; "
            "print('ACCEPTED' if _approve_browser_trust() else 'REFUSED', flush=True)"
        )
        for answer, expected in (
            (b"TRUST LOCAL CA\n", b"ACCEPTED"),
            (b"no\n", b"REFUSED"),
        ):
            with self.subTest(answer=answer):
                pid, fd = pty.fork()
                if pid == 0:
                    os.execve(
                        sys.executable,
                        [sys.executable, "-B", "-c", snippet],
                        os.environ,
                    )
                deadline = time.monotonic() + 8
                output = bytearray()
                sent = False
                try:
                    while time.monotonic() < deadline and len(output) < 4096:
                        if not select.select(
                            [fd], [], [], max(0, deadline - time.monotonic())
                        )[0]:
                            break
                        try:
                            data = os.read(fd, 1024)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            break
                        if not data:
                            break
                        output.extend(data)
                        if not sent and b"Type TRUST LOCAL CA" in output:
                            os.write(fd, answer)
                            sent = True
                        if expected in output:
                            break
                    self.assertTrue(sent)
                    self.assertIn(expected, output)
                finally:
                    os.close(fd)
                    if os.waitpid(pid, os.WNOHANG) == (0, 0):
                        try:
                            os.kill(pid, 15)
                        except ProcessLookupError:
                            pass
                        os.waitpid(pid, 0)
