import errno
import gzip
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from oc_box import oc_box as box, plugin_compat


class ConfigArchiveTraversalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-config-traversal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "host-config/opencode"
        self.source.mkdir(parents=True)
        environment = patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.source.parent)}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def archive(self, release):
        return box.config_archive(
            {"home": "/home/example", "opencode_version": release},
            history_only=True,
        )

    def write(self, name, content=b"fixture", *, mode=0o640):
        target = self.source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
        return target

    def test_prunes_before_enumeration_and_preserves_ordered_v1_and_v2_tar_bytes(self):
        self.write("z-clear.md")
        self.write(".hidden/guide.md")
        self.write("a/deep/node_modules-backup/readme.md")
        self.write("a/deep/.gitkeep")
        self.write("a/other/.git", b"git worktree pointer")
        self.write("a/other/node_modules")
        self.write(".git/objects/unread")
        self.write("a/.git/objects/unread")
        self.write("a/deep/node_modules/pkg/ignore")
        linked = self.write("node_modules/pkg/data", b"file link target", mode=0o751)
        (self.source / "linked.md").symlink_to(linked)
        (self.source / "dangling.md").symlink_to(self.root / "missing")
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "never-reached.md").touch()
        (self.source / "linked-dir").symlink_to(outside, target_is_directory=True)
        (self.source / "loop").symlink_to(".", target_is_directory=True)
        self.write("service-private.json", b"not copied")
        self.write("AGENTS.md", b"Host notes")
        self.write("custom/legacy.ts", b"export default async () => ({})\n")
        self.write("plugins/native.js", b'export default { id: "fixture.native" }\n')
        self.write("opencode.jsonc", b'{"plugins":["./custom/legacy.ts"],}\n')
        self.write("cli.json", b'{"theme":"fixture"}\n')
        original, adapter, _ = plugin_compat.bridge_file(Path("custom/legacy.ts"))

        def old_paths(source):
            return (
                path
                for path in source.rglob("*")
                if not {".git", "node_modules"}.intersection(
                    path.relative_to(source).parts
                )
            )

        scandir = os.scandir
        for release in ("1.19.5", "2.4.1"):
            with self.subTest(release=release):
                with patch.object(box, "_config_archive_paths", old_paths):
                    expected = gzip.decompress(self.archive(release))
                scanned = set()

                def observed(path):
                    relative = Path(path).relative_to(self.source)
                    self.assertFalse(
                        {".git", "node_modules", "linked-dir", "loop"}.intersection(
                            relative.parts
                        ),
                        f"Unexpected traversal: {relative}",
                    )
                    scanned.add(relative)
                    return scandir(path)

                with patch.object(os, "scandir", side_effect=observed):
                    actual = self.archive(release)
                self.assertIn(Path("."), scanned)
                self.assertIn(Path("a/deep"), scanned)
                self.assertEqual(gzip.decompress(actual), expected)
                with tarfile.open(fileobj=io.BytesIO(actual), mode="r:gz") as archive:
                    names = archive.getnames()
                    self.assertEqual(
                        names[:3],
                        [
                            ".hidden/guide.md",
                            "a/deep/.gitkeep",
                            "a/deep/node_modules-backup/readme.md",
                        ],
                    )
                    self.assertIn("plugins/native.js", names)
                    self.assertNotIn("linked-dir/never-reached.md", names)
                    self.assertNotIn("dangling.md", names)
                    self.assertNotIn("service-private.json", names)
                    self.assertEqual(archive.getmember("linked.md").mode, 0o751)
                    self.assertEqual(
                        archive.extractfile("linked.md").read(), b"file link target"
                    )
                    config = json.load(archive.extractfile("opencode.jsonc"))
                    if release == "1.19.5":
                        self.assertIn("custom/legacy.ts", names)
                        self.assertNotIn(str(original), names)
                        self.assertEqual(config["plugins"], ["./custom/legacy.ts"])
                    else:
                        self.assertNotIn("custom/legacy.ts", names)
                        self.assertIn(str(original), names)
                        self.assertIn(str(adapter / "index.js"), names)
                        self.assertEqual(
                            config["plugins"],
                            ["/home/example/.config/opencode/" + str(adapter)],
                        )

    def test_retained_scan_errors_keep_python_glob_policy_and_file_read_errors_propagate(
        self,
    ):
        visible = self.write("retained/file.md")
        scandir = os.scandir
        for error in (
            PermissionError(errno.EACCES, "fixture"),
            OSError(errno.EIO, "fixture"),
        ):
            with self.subTest(error=type(error)):

                def observed(path):
                    if Path(path) == visible.parent:
                        raise error
                    return scandir(path)

                with patch.object(os, "scandir", side_effect=observed):
                    if not isinstance(error, PermissionError) and sys.version_info < (
                        3,
                        12,
                    ):
                        with self.assertRaises(OSError) as exc:
                            self.archive("1.19.5")
                        self.assertIs(exc.exception, error)
                    else:
                        payload = self.archive("1.19.5")
                        with tarfile.open(
                            fileobj=io.BytesIO(payload), mode="r:gz"
                        ) as archive:
                            self.assertNotIn("retained/file.md", archive.getnames())

        read_bytes = Path.read_bytes

        def read(path):
            if path == visible:
                raise PermissionError(errno.EACCES, "fixture")
            return read_bytes(path)

        with patch.object(Path, "read_bytes", read), self.assertRaises(PermissionError):
            self.archive("1.19.5")
