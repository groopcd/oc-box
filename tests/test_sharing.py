import base64
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from oc_box import oc_box as box, sharing, workspace_service


class TemporaryShareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)

    def export(self, path=None, **options):
        return self.context.enter_context(
            sharing.Export(self.source if path is None else path, **options)
        )

    def write(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return path


@unittest.skipUnless(sys.platform == "linux", "Live shares require Linux openat2")
class ExportTests(TemporaryShareTests):
    def test_live_sorted_listings_hidden_files_updates_and_new_descendants(self):
        self.write("z.txt", "before")
        self.write(".hidden", "hidden")
        self.write("nested/item.txt", "nested")
        export = self.export()
        expected = [
            {"name": ".hidden", "type": "file"},
            {"name": "nested", "type": "directory"},
            {"name": "z.txt", "type": "file"},
        ]
        self.assertEqual(export.listing("."), expected)
        self.assertEqual(export.listing("."), expected)
        self.assertEqual(export.read("z.txt"), {"type": "text", "text": "before"})
        self.write("z.txt", "after 中🙂")
        self.assertEqual(export.read("z.txt")["text"], "after 中🙂")
        self.write("nested/new.txt", "newly created")
        self.assertEqual(
            export.listing("nested"),
            [
                {"name": "item.txt", "type": "file"},
                {"name": "new.txt", "type": "file"},
            ],
        )
        self.assertEqual(export.read("nested/new.txt")["text"], "newly created")
        self.assertEqual(export.read(".hidden")["text"], "hidden")

    def test_single_file_repeated_reads_have_fresh_offsets_and_observe_writes(self):
        source = self.write("one.txt", "shared 中🙂\n" * 10000)
        export = self.export(source)
        expected = source.read_text(encoding="utf-8")
        for _ in range(3):
            self.assertEqual(export.read("."), {"type": "text", "text": expected})
        source.write_text("updated in place", encoding="utf-8")
        self.assertEqual(export.read(".")["text"], "updated in place")
        for path in ("one.txt", "child"):
            with self.subTest(path=path), self.assertRaises(sharing.ShareError):
                export.read(path)
        with self.assertRaises(sharing.ShareError):
            export.listing(".")

    def test_utf8_text_limit_is_one_mib_of_bytes_not_characters(self):
        limit = 1024 * 1024
        text = "é" * (limit // 2)
        path = self.write("text", text)
        export = self.export()
        self.assertEqual(export.read("text")["text"], text)
        with path.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaises(sharing.ShareError):
            export.read("text")

    def test_text_rejects_non_utf8_binary_and_directories(self):
        self.write("invalid", b"\xff\xfe")
        self.write("nul", b"text\0binary")
        export = self.export()
        for path in ("invalid", "nul", "."):
            with self.subTest(path=path), self.assertRaises(sharing.ShareError):
                export.read(path)

    def test_images_are_identified_by_magic_not_filename(self):
        export = self.export()
        for number, (content, mime) in enumerate(
            (
                (b"\x89PNG\r\n\x1a\nsynthetic", "image/png"),
                (b"\xff\xd8\xffsynthetic", "image/jpeg"),
                (b"GIF87asynthetic", "image/gif"),
                (b"GIF89asynthetic", "image/gif"),
                (b"RIFF\x04\x00\x00\x00WEBPsynthetic", "image/webp"),
            )
        ):
            with self.subTest(mime=mime, number=number):
                name = f"image-{number}.txt"
                self.write(name, content)
                result = export.read(name, image=True)
                self.assertEqual(result["type"], "image")
                self.assertEqual(result["mimeType"], mime)
                self.assertEqual(
                    base64.b64decode(result["data"], validate=True), content
                )
        for content in (b"plain text", b"<svg></svg>", b"RIFF", b"RIFF1234WAVE"):
            with self.subTest(content=content):
                self.write("misleading.png", content)
                with self.assertRaises(sharing.ShareError):
                    export.read("misleading.png", image=True)

    def test_image_limit_is_eight_mib_and_independent_of_text_limit(self):
        limit = 8 * 1024 * 1024
        content = b"\x89PNG\r\n\x1a\n" + b"x" * (limit - 8)
        path = self.write("large-image", content)
        export = self.export()
        result = export.read("large-image", image=True)
        self.assertEqual(base64.b64decode(result["data"], validate=True), content)
        with self.assertRaises(sharing.ShareError):
            export.read("large-image")
        with path.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaises(sharing.ShareError):
            export.read("large-image", image=True)

    def test_paths_reject_traversal_absolute_dot_empty_nul_and_overlong_input(self):
        self.write("nested/readable", "inside")
        (self.source / "nested/child").mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private").write_text("not shared", encoding="utf-8")
        export = self.export()
        for path in (
            "",
            "..",
            "../outside",
            "nested/../../outside",
            str(outside),
            "/",
            "./nested",
            "nested/.",
            "nested/../nested",
            "nested//child",
            "nested/",
            "nested/\0readable",
            "x" * 4097,
        ):
            with self.subTest(path=repr(path)), self.assertRaises(sharing.ShareError):
                export.listing(path)
        self.assertEqual(export.read("nested/readable")["text"], "inside")

    def test_depth_limit_accepts_64_components_but_refuses_a_real_65th_directory(self):
        deepest = self.source
        for _ in range(65):
            deepest = deepest / "d"
            deepest.mkdir()
        export = self.export()
        self.assertEqual(
            export.listing("/".join(["d"] * 64)),
            [{"name": "d", "type": "directory"}],
        )
        with self.assertRaises(sharing.ShareError):
            export.listing("/".join(["d"] * 65))

    def test_symlink_source_roots_and_ancestors_are_refused(self):
        source = self.write("file", "inside")
        for target in (self.source, source):
            link = self.root / ("directory-link" if target.is_dir() else "file-link")
            link.symlink_to(target, target_is_directory=target.is_dir())
            with (
                self.subTest(target=target),
                self.assertRaises((sharing.ShareError, OSError)),
            ):
                sharing.Export(link)
        ancestor = self.root / "ancestor-link"
        ancestor.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises((sharing.ShareError, OSError)):
            sharing.Export(ancestor / "file")

    def test_descendant_symlinks_are_unavailable_even_when_the_target_is_inside(self):
        self.write("real/file", "inside")
        outside = self.root / "outside"
        outside.write_text("outside", encoding="utf-8")
        (self.source / "inside-link").symlink_to(self.source / "real/file")
        (self.source / "outside-link").symlink_to(outside)
        (self.source / "directory-link").symlink_to(
            self.source / "real", target_is_directory=True
        )
        export = self.export()
        kinds = {item["name"]: item["type"] for item in export.listing(".")}
        for name in ("inside-link", "outside-link", "directory-link"):
            self.assertEqual(kinds[name], "unavailable")
        for name in (
            "inside-link",
            "outside-link",
            "directory-link/file",
        ):
            with self.subTest(name=name), self.assertRaises(sharing.ShareError):
                export.read(name)

    def test_hardlinked_files_are_refused_at_selection_and_after_selection(self):
        source = self.write("file", "inside")
        export = self.export(source)
        os.link(source, self.source / "alias")
        with self.assertRaises(sharing.ShareError):
            sharing.Export(source)
        with self.assertRaises(sharing.ShareError):
            export.read(".")
        directory = self.export()
        self.assertEqual(
            directory.listing("."),
            [
                {"name": "alias", "type": "unavailable"},
                {"name": "file", "type": "unavailable"},
            ],
        )
        for name in ("file", "alias"):
            with self.subTest(name=name), self.assertRaises(sharing.ShareError):
                directory.read(name)

    def test_fifo_refusal_does_not_wait_for_a_writer(self):
        fifo = self.source / "fifo"
        os.mkfifo(fifo)
        export = self.export()

        def timed_out(*_):
            raise AssertionError("Refusing a FIFO must not block opening it")

        previous_handler = signal.signal(signal.SIGALRM, timed_out)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, 2)
        try:
            with self.assertRaises(sharing.ShareError):
                sharing.Export(fifo)
            with self.assertRaises(sharing.ShareError):
                export.read("fifo")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        self.assertEqual(export.listing("."), [{"name": "fifo", "type": "unavailable"}])

    def test_protected_direct_ancestor_and_descendant_selections_are_refused(self):
        protected = self.source / "private"
        protected.mkdir()
        child = protected / "secret"
        child.write_text("synthetic secret", encoding="utf-8")
        for source in (protected, self.source, child):
            with self.subTest(source=source), self.assertRaises(sharing.ShareError):
                sharing.Export(source, protected=[protected])
        allowed = self.write("public", "allowed sibling")
        self.assertEqual(
            self.export(allowed, protected=[protected]).read(".")["text"],
            "allowed sibling",
        )

    def test_broad_home_system_and_runtime_roots_are_refused_before_opening(self):
        paths = (
            "/",
            "/home",
            "/tmp",
            "/var",
            "/etc",
            "/usr",
            "/opt",
            Path.home(),
            "/proc",
            "/proc/self",
            "/sys",
            "/sys/kernel",
            "/dev",
            "/dev/null",
            "/run",
            "/run/user",
        )
        with patch.object(sharing.os, "open") as opened:
            for path in paths:
                with self.subTest(path=path), self.assertRaises(sharing.ShareError):
                    sharing.Export(path)
        opened.assert_not_called()

    def test_double_leading_slash_cannot_bypass_source_policy(self):
        protected = self.source / "private"
        protected.mkdir()
        secret = protected / "key"
        secret.write_text("synthetic protected data")
        for path in ("/" + str(secret), "/" + str(Path.home()), "//proc/self/environ"):
            with self.subTest(path=path), patch.object(sharing.os, "open") as opened:
                with self.assertRaises(sharing.ShareError):
                    sharing.Export(path, protected=[protected])
                opened.assert_not_called()

    def test_atomic_descendant_replacement_is_live_but_file_root_replacement_refuses(
        self,
    ):
        source = self.write("file", "old")
        directory = self.export()
        single = self.export(source)
        replacement = self.root / "replacement"
        replacement.write_text("new", encoding="utf-8")
        replacement.replace(source)
        self.assertEqual(directory.read("file")["text"], "new")
        with self.assertRaises(sharing.ShareError):
            single.read(".")

    def test_directory_root_replacement_refuses_and_context_exit_closes_descriptor(
        self,
    ):
        self.write("file", "old")
        with sharing.Export(self.source) as export:
            descriptor = export.fd
            self.source.rename(self.root / "original")
            self.source.mkdir()
            self.write("file", "replacement")
            with self.assertRaises(sharing.ShareError):
                export.listing(".")
            with self.assertRaises(sharing.ShareError):
                export.read("file")
        self.assertIsNone(export.fd)
        with self.assertRaises(OSError) as raised:
            os.fstat(descriptor)
        self.assertEqual(raised.exception.errno, errno.EBADF)
        export.close()
        with self.assertRaises(sharing.ShareError):
            export.read("file")

    def test_removed_and_symlink_replaced_roots_do_not_reveal_replacements(self):
        source = self.write("file", "old")
        export = self.export(source)
        source.unlink()
        with self.assertRaises((sharing.ShareError, OSError)):
            export.read(".")
        target = self.root / "target"
        target.write_text("not authorized", encoding="utf-8")
        source.symlink_to(target)
        with self.assertRaises((sharing.ShareError, OSError)):
            export.read(".")

    def test_observed_root_replacement_is_a_sticky_revocation(self):
        self.write("file", "original")
        export = self.export()
        saved = self.root / "saved"
        self.source.rename(saved)
        with self.assertRaises(sharing.ShareError):
            export.read("file")
        saved.rename(self.source)
        self.assertIsNone(export.fd)
        with self.assertRaises(sharing.ShareError):
            export.read("file")

    def test_directory_entry_limit_allows_1000_and_refuses_1001_without_partial_list(
        self,
    ):
        for number in range(1000):
            (self.source / f"entry-{number:04}").touch()
        export = self.export()
        self.assertEqual(len(export.listing(".")), 1000)
        (self.source / "one-too-many").touch()
        with self.assertRaises(sharing.ShareError):
            export.listing(".")

    def test_reads_and_listings_do_not_mutate_source_contents_or_metadata(self):
        self.write("nested/text", "read-only 中")
        self.write(".hidden", "hidden")
        self.write("image", b"GIF89asynthetic")

        def snapshot():
            result = {}
            for path in [self.source, *self.source.rglob("*")]:
                info = path.stat(follow_symlinks=False)
                result[str(path.relative_to(self.source))] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
                )
            return result

        before = snapshot()
        with sharing.Export(self.source) as export:
            for _ in range(2):
                export.listing(".")
                export.listing("nested")
                export.read("nested/text")
                export.read(".hidden")
                export.read("image", image=True)
        self.assertEqual(snapshot(), before)

    def test_kernel_resolution_refuses_an_actual_nested_mount(self):
        # Metadata-only lookup: /proc is a real mounted filesystem here. No
        # mount operations, native service or private host file reads are used.
        root = os.open("/", os.O_PATH | os.O_CLOEXEC)
        try:
            with self.assertRaises(sharing.ShareError):
                sharing._open_beneath(root, "proc/self/status")
        finally:
            os.close(root)

    def test_unsupported_kernel_resolution_has_no_fallback(self):
        with patch.object(sharing.platform, "machine", return_value="unsupported"):
            with self.assertRaisesRegex(sharing.ShareError, "openat2"):
                sharing.Export(self.source)

    def test_file_changing_during_read_is_not_returned(self):
        path = self.write("changing", "before")
        export = self.export()
        pread = os.pread

        def mutate(fd, size, offset):
            content = pread(fd, size, offset)
            if offset == 0:
                path.write_text("after mutation")
            return content

        with patch.object(sharing.os, "pread", side_effect=mutate):
            with self.assertRaisesRegex(sharing.ShareError, "changed during"):
                export.read("changing")


@unittest.skipUnless(sys.platform == "linux", "Live shares require Linux openat2")
class MCPTests(TemporaryShareTests):
    def setUp(self):
        super().setUp()
        self.write("text", "untrusted contents 中")
        self.write("image", b"GIF89asynthetic")
        self.shared = self.export()
        self.mcp = sharing.MCP(self.shared)

    def request(self, method, params=None, ident=17):
        request = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            request["params"] = params
        response = self.mcp.reply(request)
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], ident)
        return response

    def initialize(self):
        return self.request("initialize", {"protocolVersion": "2024-11-05"})

    def call(self, name, arguments=None):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request("tools/call", params)["result"]

    def test_initialize_advertises_only_readonly_tools_and_calls_return_mcp_content(
        self,
    ):
        result = self.initialize()["result"]
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertIsInstance(result["protocolVersion"], str)
        self.assertTrue(result["instructions"])
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {"list_directory", "read_file", "read_image"},
        )
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        listing = self.call("list_directory")
        self.assertNotIn("isError", listing)
        self.assertEqual(
            json.loads(listing["content"][0]["text"]), self.shared.listing(".")
        )
        self.assertEqual(
            self.call("read_file", {"path": "text"}),
            {"content": [{"type": "text", "text": "untrusted contents 中"}]},
        )
        image = self.call("read_image", {"path": "image"})["content"][0]
        self.assertEqual(image["mimeType"], "image/gif")
        self.assertEqual(base64.b64decode(image["data"]), b"GIF89asynthetic")

    def test_uninitialized_methods_cannot_read_and_ping_remains_available(self):
        with patch.object(self.shared, "read") as read:
            for method, params in (
                ("tools/list", {}),
                ("tools/call", {"name": "read_file", "arguments": {"path": "text"}}),
            ):
                with self.subTest(method=method):
                    self.assertEqual(
                        self.request(method, params)["error"]["code"], -32000
                    )
            self.assertEqual(self.request("ping")["result"], {})
        read.assert_not_called()

    def test_unknown_methods_and_malformed_params_return_protocol_errors(self):
        self.initialize()
        for method in ("resources/list", "prompts/list", "arbitrary/private-sentinel"):
            with self.subTest(method=method):
                response = self.request(method)
                self.assertEqual(response["error"]["code"], -32601)
                self.assertNotIn("private-sentinel", json.dumps(response))
        for params in ([], "private-sentinel", 7, None):
            with self.subTest(params=params):
                response = self.mcp.reply(
                    {
                        "jsonrpc": "2.0",
                        "id": "example",
                        "method": "tools/list",
                        "params": params,
                    }
                )
                self.assertEqual(response["error"]["code"], -32602)
                self.assertNotIn("private-sentinel", json.dumps(response))

    def test_invalid_tool_arguments_and_unknown_tool_names_are_safe_errors(self):
        self.initialize()
        cases = (
            {"name": "private-sentinel", "arguments": {}},
            {"name": "read_file", "arguments": []},
            {"name": "read_file", "arguments": None},
            {"name": "read_file", "arguments": {"private-sentinel": "secret"}},
            *(
                {"name": "read_file", "arguments": {"path": path}}
                for path in (None, 42, False, [], {}, "../private-sentinel", "\ud800")
            ),
        )
        for params in cases:
            with self.subTest(params=repr(params)):
                result = self.request("tools/call", params)["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(result["content"][0]["type"], "text")
                self.assertNotIn("private-sentinel", json.dumps(result))

    def test_private_os_errors_from_listing_and_reads_never_enter_mcp_results(self):
        self.initialize()
        for tool, operation in (
            ("read_file", "read"),
            ("read_image", "read"),
            ("list_directory", "listing"),
        ):
            with (
                self.subTest(tool=tool),
                patch.object(
                    self.shared,
                    operation,
                    side_effect=OSError(errno.EACCES, "private-error-sentinel"),
                ),
            ):
                result = self.call(tool, {"path": "text"})
                self.assertTrue(result["isError"])
                self.assertNotIn("private-error-sentinel", json.dumps(result))
                self.assertNotIn(str(self.source), json.dumps(result))

    def test_notifications_have_no_response_and_invalid_envelopes_are_refused(self):
        self.assertIsNone(
            self.mcp.reply({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        self.assertFalse(self.mcp.initialized)
        for request in (
            None,
            [],
            {},
            {"jsonrpc": "1.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 1, "method": []},
            *(
                {"jsonrpc": "2.0", "id": ident, "method": "ping"}
                for ident in (None, True, [], {})
            ),
        ):
            with self.subTest(request=request), self.assertRaises(sharing.ShareError):
                self.mcp.reply(request)


@unittest.skipUnless(sys.platform == "linux", "Live shares require Linux openat2")
class SharingCLITests(TemporaryShareTests):
    def setUp(self):
        super().setUp()
        self.write("text", "host-only-source-sentinel")
        self.workspace_path = self.root / "workspace.json"
        self.data = {
            "id": "synthetic-workspace",
            "opencode_version": "2.2.6",
            "project": "/synthetic/project",
            "container_project": "/workspace/project",
            "container": "synthetic-container",
            "home": "/home/synthetic",
        }
        self.connection = {
            "url": "http://127.0.0.1:12345",
            "password": "synthetic-control-password",
        }
        self.facade = SimpleNamespace(
            BoxError=box.BoxError,
            APP=self.root / "application",
            PACKAGE=box.PACKAGE,
            state_root=Mock(return_value=self.root / "state"),
            vault=SimpleNamespace(
                vault_path=Mock(return_value=self.root / "vault/vault.age")
            ),
            find_workspace=Mock(return_value=(self.workspace_path, self.data)),
            opencode_compat=box.opencode_compat,
            maintenance_host_env=box.maintenance_host_env,
            container_project=box.container_project,
            ContainerCommand=box.ContainerCommand,
            check_backend=Mock(),
            workspace_service=SimpleNamespace(
                attach=Mock(
                    side_effect=lambda *_: contextlib.nullcontext(self.connection)
                ),
                client_command=Mock(wraps=workspace_service.client_command),
            ),
        )
        self.forbidden = {}
        for name in (
            "start_container",
            "setup",
            "launch_credentials",
            "refresh_sessions",
            "sync_config",
        ):
            operation = Mock(
                side_effect=AssertionError(f"Sharing must not call {name}")
            )
            self.forbidden[name] = operation
            setattr(self.facade, name, operation)
        for group, names in (
            ("credentials", ("unlocked", "forwarded", "active_values")),
            ("sync", ("refresh",)),
        ):
            operations = {}
            for name in names:
                operation = Mock(
                    side_effect=AssertionError(f"Sharing must not call {group}.{name}")
                )
                self.forbidden[f"{group}.{name}"] = operation
                operations[name] = operation
            setattr(self.facade, group, SimpleNamespace(**operations))
        self.context.enter_context(
            patch.dict(
                os.environ,
                {
                    "HOME": str(self.root / "home"),
                    "PATH": "/usr/bin:/bin",
                    "OC_BOX_CONTAINER": "0",
                    "PROVIDER_API_KEY": "provider-secret-sentinel",
                    "CUSTOM_SECRET": "custom-secret-sentinel",
                    "OPENCODE_AUTH_CONTENT": "auth-secret-sentinel",
                },
                clear=True,
            )
        )
        self.stdin = io.StringIO()
        self.stderr = io.StringIO()
        self.stdout = io.StringIO()
        self.context.enter_context(patch.object(sharing.sys, "stdin", self.stdin))
        self.context.enter_context(contextlib.redirect_stderr(self.stderr))
        self.context.enter_context(contextlib.redirect_stdout(self.stdout))
        self.process = Mock()
        self.popen = self.context.enter_context(
            patch.object(sharing.subprocess, "Popen", return_value=self.process)
        )
        self.serve = self.context.enter_context(patch.object(sharing, "serve"))

    def tearDown(self):
        for operation in self.forbidden.values():
            operation.assert_not_called()

    def cli(self, *options):
        return sharing.cli(self.facade, [str(self.source), *options])

    def assert_no_client(self):
        self.facade.check_backend.assert_not_called()
        self.facade.workspace_service.attach.assert_not_called()
        self.facade.workspace_service.client_command.assert_not_called()
        self.popen.assert_not_called()

    def test_container_guard_refuses_before_workspace_discovery(self):
        with patch.dict(os.environ, {"OC_BOX_CONTAINER": "1"}):
            with self.assertRaises(box.BoxError):
                self.cli("--acknowledge-risk")
        self.facade.find_workspace.assert_not_called()
        self.assert_no_client()

    def test_missing_v1_and_other_major_workspaces_never_start_or_attach(self):
        for data in (
            None,
            {},
            {"opencode_version": "1.19.5"},
            {"opencode_version": "3.0.0"},
        ):
            with self.subTest(data=data):
                self.facade.find_workspace.return_value = (self.workspace_path, data)
                with self.assertRaises(box.BoxError):
                    self.cli("--acknowledge-risk")
        self.assert_no_client()

    def test_non_tty_requires_explicit_acknowledgement_without_reading_stdin(self):
        for stdin_tty, stderr_tty in ((False, False), (False, True), (True, False)):
            with (
                self.subTest(stdin=stdin_tty, stderr=stderr_tty),
                patch.object(self.stdin, "isatty", return_value=stdin_tty),
                patch.object(self.stderr, "isatty", return_value=stderr_tty),
                patch(
                    "builtins.input",
                    side_effect=AssertionError("Must not prompt on non-TTY input"),
                ),
            ):
                with self.assertRaises(box.BoxError):
                    self.cli()
        self.assert_no_client()

    def test_declined_interactive_confirmation_closes_export_without_attaching(self):
        selected = []
        real_export = sharing.Export

        def record(*args, **kwargs):
            export = real_export(*args, **kwargs)
            selected.append(export)
            return export

        with (
            patch.object(self.stdin, "isatty", return_value=True),
            patch.object(self.stderr, "isatty", return_value=True),
            patch("builtins.input", return_value="no"),
            patch.object(sharing, "Export", side_effect=record),
        ):
            with self.assertRaises(box.BoxError):
                self.cli()
        self.assertEqual(len(selected), 1)
        self.assertIsNone(selected[0].fd)
        self.assert_no_client()

    def test_stopped_or_unavailable_controller_refuses_without_starting(self):
        self.facade.workspace_service.attach.side_effect = box.BoxError(
            "No live controller"
        )
        with self.assertRaises(box.BoxError):
            self.cli("--acknowledge-risk")
        self.facade.workspace_service.attach.assert_called_once_with(
            self.facade, self.workspace_path, self.data
        )
        self.popen.assert_not_called()

    def test_real_backend_error_boundary_never_exposes_native_stderr(self):
        self.facade.check_backend = box.check_backend

        def failing_native(argv, *, stdout, stderr, **kwargs):
            stderr.write(b"synthetic-private-backend-stderr")
            return subprocess.CompletedProcess(argv, 125)

        with (
            patch.object(box.dependencies, "require"),
            patch.object(box.os, "geteuid", return_value=1000),
            patch.object(box.subprocess, "run", side_effect=failing_native),
        ):
            with self.assertRaises(box.BoxError) as refusal:
                self.cli("--acknowledge-risk")
        self.assertNotIn("synthetic-private-backend-stderr", str(refusal.exception))
        self.assertNotIn("synthetic-private-backend-stderr", self.stderr.getvalue())
        self.facade.workspace_service.attach.assert_not_called()
        self.popen.assert_not_called()

    def test_real_attachment_inspection_error_boundary_is_sanitized(self):
        self.facade.workspace_service.attach = workspace_service.attach
        self.facade.validate_project = Mock(return_value=self.source)
        self.facade.workspace_identity = SimpleNamespace(
            same_project=Mock(return_value=True)
        )
        self.facade.inspect_owned = box.inspect_owned
        result = subprocess.CompletedProcess(
            ["podman"], 125, "", "synthetic-private-inspect-stderr"
        )
        with patch.object(box, "podman", return_value=result):
            with self.assertRaises(box.BoxError) as refusal:
                self.cli("--acknowledge-risk")
        self.assertNotIn("synthetic-private-inspect-stderr", str(refusal.exception))
        self.assertNotIn("synthetic-private-inspect-stderr", self.stderr.getvalue())
        self.popen.assert_not_called()

    def test_acknowledged_share_uses_sanitized_existing_client_and_closes(self):
        observed = []

        def serve(process, export, *, ready):
            self.assertIs(process, self.process)
            self.assertEqual(export.read("text")["text"], "host-only-source-sentinel")
            observed.append(export)
            ready()

        self.serve.side_effect = serve
        self.assertEqual(
            self.cli("--workspace", "synthetic-workspace", "--acknowledge-risk"), 0
        )
        self.facade.find_workspace.assert_called_once_with(None, "synthetic-workspace")
        self.facade.workspace_service.attach.assert_called_once_with(
            self.facade, self.workspace_path, self.data
        )
        command = self.popen.call_args.args[0]
        options = self.popen.call_args.kwargs
        self.assertEqual(command[:3], ["podman", "exec", "-i"])
        self.assertNotIn("-t", command)
        self.assertIs(options["env"], command.environment)
        self.assertEqual(
            command.environment["OPENCODE_PASSWORD"], self.connection["password"]
        )
        for name in ("PROVIDER_API_KEY", "CUSTOM_SECRET", "OPENCODE_AUTH_CONTENT"):
            self.assertNotIn(name, command.environment)
        for private in (
            str(self.source),
            "host-only-source-sentinel",
            "provider-secret-sentinel",
            "custom-secret-sentinel",
            "auth-secret-sentinel",
            self.connection["password"],
        ):
            self.assertNotIn(private, json.dumps(list(command)))
        self.assertEqual(options["stdin"], subprocess.PIPE)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertTrue(options["start_new_session"])
        self.facade.check_backend.assert_called_once_with(
            env=box.maintenance_host_env()
        )
        self.process.stdin.close.assert_called_once_with()
        self.process.stdout.close.assert_called_once_with()
        self.process.wait.assert_called_once_with(timeout=5)
        self.assertIsNone(observed[0].fd)
        self.assertIn("oc_box_share_", self.stdout.getvalue())
        self.assertNotIn("host-only-source-sentinel", self.stderr.getvalue())

    def test_private_subprocess_error_is_sanitized_and_export_is_closed(self):
        observed = []

        def fail(process, export, **_):
            observed.append(export)
            raise OSError(errno.EACCES, "private-os-error-sentinel")

        self.serve.side_effect = fail
        with self.assertRaises(box.BoxError) as raised:
            self.cli("--acknowledge-risk")
        self.assertNotIn("private-os-error-sentinel", str(raised.exception))
        self.assertNotIn("private-os-error-sentinel", self.stderr.getvalue())
        self.assertIsNone(observed[0].fd)
        self.process.stdin.close.assert_called_once_with()
        self.process.stdout.close.assert_called_once_with()

    def test_explicit_project_selector_is_forwarded_without_discovery_side_effects(
        self,
    ):
        project = self.root / "synthetic-project"
        self.assertEqual(self.cli("--project", str(project), "--acknowledge-risk"), 0)
        self.facade.find_workspace.assert_called_once_with(project, None)
