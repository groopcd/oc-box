import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from tools import check_release
from tools import check_release_artifacts as artifacts


APP = Path(__file__).resolve().parents[1]


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="oc-box-artifact-gate-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = self.work / "source"
        self.root.mkdir()
        self.dist = self.work / "dist"
        self.dist.mkdir()
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "--initial-branch=feature/artifact-build")
        self.source = self.root / check_release.SOURCE
        self.source.parent.mkdir(parents=True)
        self.set_channel("stable")
        for count in range(28):
            self.git("commit", "--allow-empty", "-m", f"History {count}")
        self.identity = check_release.validate(self.root)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    def set_channel(self, channel):
        self.source.write_text(
            f'VERSION_SERIES = "1.{"7" if channel == "devel" else "6"}"\n'
            f'RELEASE_CHANNEL = "{channel}"\n'
            "VERSION_BASE_COMMIT_COUNT = 29\n"
            # This must never run during archive inspection.
            'raise RuntimeError("Archive source must not execute")\n'
        )
        self.git("add", check_release.SOURCE)
        self.git("commit", "-m", f"Track {channel}")

    def select_channel(self, channel):
        if channel != self.identity["release_channel"]:
            self.set_channel(channel)
        self.identity = check_release.validate(self.root)
        self.clean_dist()

    def clean_dist(self):
        for path in self.dist.iterdir():
            path.unlink()

    def publication(self):
        branch = check_release.CHANNEL_BRANCHES[self.identity["release_channel"]]
        self.git("update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        self.git("tag", self.identity["tag"])
        release = {
            "tag_name": self.identity["tag"],
            "draft": False,
            "prerelease": self.identity["release_channel"] == "devel",
            "published_at": "2026-11-13T00:00:00Z",
            "assets": [],
        }
        self.identity = check_release.validate(
            self.root, publish=True, tag=self.identity["tag"], release=release
        )
        return release

    def write_archives(self, *, change=None, extra_wheel=(), extra_sdist=()):
        version = self.identity["distribution_version"]
        build = {
            key: self.identity[key]
            for key in ("version", "commit", "commit_count", "dirty", "release_channel")
        }
        metadata = f"Name: oc-box\nVersion: {version}\n\n".encode()
        wheel = {
            "oc_box/build-info.json": json.dumps(
                {**build, "distribution_version": version}
            ).encode(),
            "oc_box/version_info.py": self.source.read_bytes(),
            "oc_box/distribution.json": json.dumps(
                {"installer": "python-package", "name": "oc-box", "version": version}
            ).encode(),
            f"oc_box-{version}.dist-info/METADATA": metadata,
        }
        root = f"oc_box-{version}"
        sdist = {
            root + "/build-info.json": json.dumps(build).encode(),
            root + "/src/oc_box/version_info.py": self.source.read_bytes(),
            root + "/PKG-INFO": metadata,
        }
        if change is not None:
            change(wheel, sdist)
        wheel_path = self.dist / f"oc_box-{version}-py3-none-any.whl"
        sdist_path = self.dist / f"oc_box-{version}.tar.gz"
        with zipfile.ZipFile(
            wheel_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, value in [*wheel.items(), *extra_wheel]:
                archive.writestr(name, value)
        with tarfile.open(sdist_path, "w:gz") as archive:
            for name, value in [*sdist.items(), *extra_sdist]:
                member = (
                    name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
                )
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
        return wheel_path, sdist_path

    def check(self, **kwargs):
        return artifacts.check(self.root, self.dist, self.identity, **kwargs)

    def mutate_json(self, target, key, value):
        def mutate(wheel, sdist):
            content = wheel if target == "wheel" else sdist
            name = next(name for name in content if name.endswith("/build-info.json"))
            build = json.loads(content[name])
            build[key] = value
            content[name] = json.dumps(build).encode()

        return mutate

    def asset(self, path, **changes):
        return {
            "name": path.name,
            "size": path.stat().st_size,
            "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            # Never follow these even if supplied by the release API.
            "browser_download_url": "https://untrusted.example.invalid/package",
            "url": "https://untrusted.example.invalid/api",
            **changes,
        }

    def test_feature_branch_build_only_checks_both_channels_without_assets_or_network(
        self,
    ):
        for channel in ("stable", "devel"):
            with self.subTest(channel=channel):
                self.select_channel(channel)
                paths = self.write_archives()
                with patch.object(
                    artifacts, "build_opener", side_effect=AssertionError
                ):
                    digests = self.check()
                self.assertEqual(set(digests), {path.name for path in paths})
                self.assertFalse(self.identity["publish_validated"])
                self.assertIs(self.identity["dirty"], False)
                artifacts.write_bundle(self.dist, self.identity, digests)
                self.assertEqual(
                    json.loads((self.dist / artifacts.IDENTITY_NAME).read_text()),
                    self.identity,
                )
                lines = (self.dist / artifacts.CHECKSUM_NAME).read_text().splitlines()
                self.assertEqual(len(lines), 3)
                for line in lines:
                    digest, name = line.split("  ")
                    self.assertEqual(
                        digest,
                        hashlib.sha256((self.dist / name).read_bytes()).hexdigest(),
                    )

    def test_both_channels_initial_releases_allow_no_assets_and_compare_existing_bytes(
        self,
    ):
        for channel in ("stable", "devel"):
            with self.subTest(channel=channel):
                self.select_channel(channel)
                release = self.publication()
                paths = self.write_archives()
                with patch.object(
                    artifacts, "build_opener", side_effect=AssertionError
                ):
                    self.check(release=release)
                    for subset in (paths[:1], paths[1:], paths):
                        release["assets"] = [self.asset(path) for path in subset]
                        self.check(release=release)

    def test_both_channels_wheel_and_sdist_embedded_identity_negative_matrix(self):
        for channel in ("stable", "devel"):
            self.select_channel(channel)
            changes = (
                ("commit", "f" * 40),
                ("commit", None),
                ("commit_count", self.identity["commit_count"] + 1),
                ("commit_count", True),
                ("release_channel", "devel" if channel == "stable" else "stable"),
                ("version", self.identity["version"] + ".dirty"),
                ("version", self.identity["distribution_version"] + "+gfffffff"),
                ("distribution_version", "1.6.999"),
                ("dirty", True),
                ("dirty", 0),
            )
            for target in ("wheel", "sdist"):
                for key, value in changes:
                    with self.subTest(
                        channel=channel, target=target, key=key, value=value
                    ):
                        self.write_archives(change=self.mutate_json(target, key, value))
                        with self.assertRaisesRegex(ValueError, "Embedded"):
                            self.check()

    def test_both_channels_package_public_version_source_and_ownership_negative_matrix(
        self,
    ):
        for channel in ("stable", "devel"):
            self.select_channel(channel)
            for target in ("wheel", "sdist"):
                for kind in ("public", "duplicate-public", "name", "source", "channel"):
                    with self.subTest(channel=channel, target=target, kind=kind):

                        def mutate(
                            wheel, sdist, target=target, kind=kind, channel=channel
                        ):
                            content = wheel if target == "wheel" else sdist
                            if kind in ("public", "duplicate-public", "name"):
                                name = next(
                                    name
                                    for name in content
                                    if name.endswith(("/METADATA", "/PKG-INFO"))
                                )
                                content[name] = {
                                    "public": b"Name: oc-box\nVersion: 1.6.999\n\n",
                                    "duplicate-public": content[name].rstrip(b"\n")
                                    + b"\nVersion: 1.6.999\n\n",
                                    "name": b"Name: different-project\nVersion: 1.6.0\n\n",
                                }[kind]
                            else:
                                name = next(
                                    name
                                    for name in content
                                    if name.endswith("/version_info.py")
                                )
                                content[name] += b"\n# modified source\n"
                                if kind == "channel":
                                    content[name] = content[name].replace(
                                        f'RELEASE_CHANNEL = "{channel}"'.encode(),
                                        b'RELEASE_CHANNEL = "other"',
                                    )

                        self.write_archives(change=mutate)
                        with self.assertRaisesRegex(ValueError, "metadata|source"):
                            self.check()
            self.write_archives(
                change=lambda wheel, _sdist: wheel.update(
                    {"oc_box/distribution.json": b'{"installer":"github"}'}
                )
            )
            with self.assertRaisesRegex(ValueError, "ownership"):
                self.check()

    def test_postvalidation_dirty_source_staged_and_untracked_changes_refused(self):
        self.write_archives()
        original = self.source.read_bytes()
        self.source.write_bytes(original + b"\n# dirty after build\n")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.check()
        self.git("add", check_release.SOURCE)
        with self.assertRaisesRegex(ValueError, "clean"):
            self.check()
        self.git("commit", "-m", "New source")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.check()
        self.identity = check_release.validate(self.root)
        self.write_archives()
        (self.root / "unexpected.txt").write_text("untracked after build")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.check()

    def test_postvalidation_clean_head_and_channel_changes_refused(self):
        self.write_archives()
        self.git("commit", "--allow-empty", "-m", "Different clean HEAD")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.check()
        self.set_channel("devel")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.check()

    def test_assume_unchanged_version_source_is_not_a_clean_source_substitute(self):
        self.git("update-index", "--assume-unchanged", check_release.SOURCE)
        self.source.write_text(self.source.read_text() + "# hidden dirty source\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "committed source"):
            check_release.validate(self.root)

    def test_source_and_archive_mutations_during_asset_checks_are_rechecked(self):
        release = self.publication()
        paths = self.write_archives()
        release["assets"] = [self.asset(paths[0], digest=None)]

        def dirty_source(*_args):
            self.source.write_text(self.source.read_text() + "# late dirty\n")
            return hashlib.sha256(paths[0].read_bytes()).hexdigest()

        with self.assertRaisesRegex(ValueError, "clean"):
            self.check(release=release, downloader=dirty_source)
        # Restore only this fixture's own synthetic change.
        self.source.write_bytes(
            self.git("show", "HEAD:" + check_release.SOURCE).encode() + b"\n"
        )

        def changed_archive(*_args):
            digest = hashlib.sha256(paths[0].read_bytes()).hexdigest()
            paths[1].write_bytes(paths[1].read_bytes() + b"changed")
            return digest

        with self.assertRaisesRegex(ValueError, "archives changed"):
            self.check(release=release, downloader=changed_archive)

    def test_wrong_extra_missing_and_symlink_archive_names_refused(self):
        for target in ("wheel", "sdist"):
            for operation in ("wrong-name", "extra", "missing", "symlink"):
                with self.subTest(target=target, operation=operation):
                    self.clean_dist()
                    paths = self.write_archives()
                    path = paths[0 if target == "wheel" else 1]
                    suffix = ".whl" if target == "wheel" else ".tar.gz"
                    if operation == "wrong-name":
                        path.rename(self.dist / ("oc_box-1.6.999" + suffix))
                    elif operation == "extra":
                        (self.dist / ("unexpected" + suffix)).write_bytes(
                            path.read_bytes()
                        )
                    elif operation == "missing":
                        path.unlink()
                    else:
                        outside = self.work / ("outside" + suffix)
                        outside.write_bytes(path.read_bytes())
                        path.unlink()
                        path.symlink_to(outside)
                    with self.assertRaisesRegex(ValueError, "filenames|regular"):
                        self.check()

    def test_malicious_member_path_duplicate_and_link_matrix(self):
        for target in ("wheel", "sdist"):
            for name in (
                "../escape",
                "/absolute",
                "root/../escape",
                "root\\escape",
                "root//escape",
            ):
                with self.subTest(target=target, name=name):
                    extras = {"extra_" + target: [(name, b"untrusted")]}
                    self.write_archives(**extras)
                    with self.assertRaisesRegex(ValueError, "Unsafe"):
                        self.check()
        version = self.identity["distribution_version"]
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            with self.subTest(tar_type=kind):
                link = tarfile.TarInfo(f"oc_box-{version}/link")
                link.type = kind
                link.linkname = "/outside"
                self.write_archives(extra_sdist=[(link, b"")])
                with self.assertRaisesRegex(ValueError, "special"):
                    self.check()
        link = zipfile.ZipInfo("oc_box/link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        self.write_archives(extra_wheel=[(link, b"/outside")])
        with self.assertRaisesRegex(ValueError, "special"):
            self.check()
        self.write_archives(extra_sdist=[(f"oc_box-{version}/build-info.json", b"{}")])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.check()
        with self.assertWarns(UserWarning):
            self.write_archives(extra_wheel=[("oc_box/build-info.json", b"{}")])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.check()
        self.assertFalse((self.work / "escape").exists())

    def test_metadata_size_duplicate_json_and_archive_inventory_bounds(self):
        for target in ("wheel", "sdist"):
            for content in (
                b"x" * (artifacts.MAX_METADATA + 1),
                b'{"commit":"first","commit":"second"}',
                b'{"dirty":NaN}',
            ):
                with self.subTest(target=target, content_size=len(content)):

                    def mutate(wheel, sdist, target=target, content=content):
                        entries = wheel if target == "wheel" else sdist
                        name = next(
                            name
                            for name in entries
                            if name.endswith("/build-info.json")
                        )
                        entries[name] = content

                    self.write_archives(change=mutate)
                    with self.assertRaises(ValueError):
                        self.check()
        self.write_archives()
        for bound, limit in (
            ("MAX_ARCHIVE", 1),
            ("MAX_MEMBERS", 1),
            ("MAX_EXPANDED", 1),
        ):
            with (
                self.subTest(bound=bound),
                patch.object(artifacts, bound, limit),
                self.assertRaises(ValueError),
            ):
                self.check()
        paths = self.write_archives()
        # Huge declared tar member without allocating/expanding its body.
        member = tarfile.TarInfo(
            "oc_box-" + self.identity["distribution_version"] + "/bomb"
        )
        member.size = artifacts.MAX_EXPANDED + 1
        with gzip.open(paths[1], "wb") as stream:
            stream.write(member.tobuf())
        with self.assertRaisesRegex(ValueError, "size bound"):
            self.check()

    def test_zip64_directory_cannot_override_preflight_bounds(self):
        for comment_size in (0, 65535):
            with self.subTest(comment_size=comment_size):
                paths = self.write_archives()
                raw = paths[0].read_bytes()
                offset = raw.rfind(b"PK\x05\x06")
                locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
                end = bytearray(raw[offset:])
                struct.pack_into("<H", end, 20, comment_size)
                paths[0].write_bytes(raw[:offset] + locator + end + b"x" * comment_size)
                with (
                    patch.object(
                        artifacts.zipfile, "ZipFile", side_effect=AssertionError
                    ),
                    self.assertRaisesRegex(ValueError, "bounds"),
                ):
                    self.check()

    def test_both_channels_existing_asset_wrong_checksum_name_state_size_and_digest_matrix(
        self,
    ):
        for channel in ("stable", "devel"):
            self.select_channel(channel)
            release = self.publication()
            paths = self.write_archives()
            for path in paths:
                changes = (
                    {"digest": "sha256:" + "0" * 64},
                    {"digest": "sha512:" + "0" * 128},
                    {"digest": "sha256:not-a-hash"},
                    {"digest": 1},
                    {"name": path.name.replace("oc_box-", "oc-box-")},
                    {"name": "../" + path.name},
                    {
                        "name": path.name.replace(
                            self.identity["distribution_version"], "1.6.999"
                        )
                    },
                    {"state": "new"},
                    {"size": path.stat().st_size + 1},
                    {"size": True},
                )
                for change in changes:
                    with self.subTest(channel=channel, name=path.name, change=change):
                        release["assets"] = [self.asset(path, **change)]
                        with self.assertRaises(ValueError):
                            self.check(
                                release=release,
                                downloader=lambda *_: self.fail("unexpected download"),
                            )
                release["assets"] = [self.asset(path), self.asset(path)]
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    self.check(release=release)

    def test_missing_digests_download_only_validated_selectors_and_must_match_exact_bytes(
        self,
    ):
        release = self.publication()
        paths = self.write_archives()
        for missing in (None, ""):
            for path in paths:
                with self.subTest(missing=missing, name=path.name):
                    release["assets"] = [self.asset(path, digest=missing)]
                    observed = []

                    def download(tag, name, size, observed=observed, path=path):
                        observed.append((tag, name, size))
                        return hashlib.sha256(path.read_bytes()).hexdigest()

                    self.check(release=release, downloader=download)
                    self.assertEqual(
                        observed,
                        [(self.identity["tag"], path.name, path.stat().st_size)],
                    )
                    with self.assertRaisesRegex(ValueError, "checksum"):
                        self.check(release=release, downloader=lambda *_: "0" * 64)
        for inventory in (None, {}, [None], [{"name": None}]):
            with self.subTest(inventory=inventory):
                release["assets"] = inventory
                with self.assertRaises(ValueError):
                    self.check(release=release)

    def test_build_only_rejects_release_inventory_and_invalid_saved_identity(self):
        self.write_archives()
        with self.assertRaisesRegex(ValueError, "Build-only"):
            self.check(release={"assets": []})
        for identity in (
            None,
            {},
            {**self.identity, "publish_validated": 1},
            {**self.identity, "dirty": True},
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                artifacts.check(self.root, self.dist, identity)

    def test_cli_build_and_manual_publication_save_bundles_and_fail_closed_offline(
        self,
    ):
        identity_path = self.work / "identity.json"
        release_path = self.work / "release.json"
        paths = self.write_archives()
        tool = APP / "tools/check_release_artifacts.py"
        command = [
            sys.executable,
            "-B",
            str(tool),
            str(self.dist),
            "--root",
            str(self.root),
            "--identity",
            str(identity_path),
        ]
        for publishing in (False, True):
            with self.subTest(publishing=publishing):
                extra = []
                if publishing:
                    release = self.publication()
                    release["assets"] = [self.asset(path) for path in paths]
                    release_path.write_text(json.dumps(release))
                    extra = ["--release-json", str(release_path)]
                identity_path.write_text(json.dumps(self.identity))
                result = subprocess.run(
                    [*command, *extra],
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads((self.dist / artifacts.IDENTITY_NAME).read_text()),
                    self.identity,
                )
        self.source.write_text(self.source.read_text() + "# dirty\n")
        result = subprocess.run(
            [*command, *extra],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("validation refused", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(str(self.root), result.stderr)
        # An absent digest never silently passes when anonymous fetching fails.
        self.source.write_bytes(
            self.git("show", "HEAD:" + check_release.SOURCE).encode() + b"\n"
        )
        release["assets"] = [self.asset(paths[0], digest=None)]
        release_path.write_text(json.dumps(release))
        stderr = io.StringIO()
        with (
            patch.object(
                artifacts, "build_opener", side_effect=URLError("private-server-detail")
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_context,
        ):
            artifacts.main(command[3:] + extra)
        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("validation refused", stderr.getvalue())
        self.assertNotIn("private-server-detail", stderr.getvalue())

    def test_bundle_output_refuses_symlinks_and_hardlinks_without_source_writes(self):
        self.write_archives()
        digests = self.check()
        original = self.source.read_bytes()
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                destination = self.dist / artifacts.IDENTITY_NAME
                if kind == "symlink":
                    destination.symlink_to(self.source)
                elif kind == "hardlink":
                    os.link(self.source, destination)
                else:
                    os.mkfifo(destination)
                with self.assertRaises((OSError, ValueError)):
                    artifacts.write_bundle(self.dist, self.identity, digests)
                self.assertEqual(self.source.read_bytes(), original)
                destination.unlink()

    def test_actual_publish_job_transfer_copies_only_checksum_verified_distributions(
        self,
    ):
        release = self.publication()
        self.write_archives()
        artifacts.write_bundle(
            self.dist,
            self.identity,
            self.check(release=release),
        )
        workflow = (APP / ".github/workflows/publish.yml").read_text()
        section = workflow.split("name: Verify transferred identity", 1)[1]
        script = textwrap.dedent(
            section.split("python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        )
        for mutation in (
            "none",
            "corrupt-wheel",
            "corrupt-identity",
            "duplicate-checksum",
            "checksum-traversal",
            "extra-file",
            "missing-sdist",
        ):
            with self.subTest(mutation=mutation):
                destination = self.work / mutation
                destination.mkdir()
                bundle = destination / "release-artifacts"
                shutil.copytree(self.dist, bundle)
                wheel = next(bundle.glob("*.whl"))
                if mutation == "corrupt-wheel":
                    wheel.write_bytes(wheel.read_bytes() + b"corrupted")
                elif mutation == "corrupt-identity":
                    (bundle / artifacts.IDENTITY_NAME).write_text("{}")
                elif mutation == "duplicate-checksum":
                    checksums = bundle / artifacts.CHECKSUM_NAME
                    lines = checksums.read_text().splitlines(keepends=True)
                    checksums.write_text(lines[0] * 3)
                elif mutation == "checksum-traversal":
                    checksums = bundle / artifacts.CHECKSUM_NAME
                    checksums.write_text(
                        checksums.read_text().replace(wheel.name, "../" + wheel.name)
                    )
                elif mutation == "extra-file":
                    (bundle / "unexpected.json").write_text("{}")
                elif mutation == "missing-sdist":
                    next(bundle.glob("*.tar.gz")).unlink()
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script],
                    cwd=destination,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if mutation == "none":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        {path.name for path in (destination / "dist").iterdir()},
                        {path.name for path in self.dist.glob("*.whl")}
                        | {path.name for path in self.dist.glob("*.tar.gz")},
                    )
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((destination / "dist").exists())

    def test_workflow_final_gate_is_after_tests_build_and_package_checks_and_before_upload(
        self,
    ):
        workflow = (APP / ".github/workflows/publish.yml").read_text()
        gate = workflow.index("python tools/check_release_artifacts.py")
        for step in ("unittest discover", 'python" -m build', "tools/check_package.py"):
            self.assertLess(workflow.index(step), gate)
        self.assertLess(gate, workflow.index("echo 'publish_validated=true'"))
        self.assertLess(gate, workflow.index("actions/upload-artifact@"))
        self.assertIn("steps.artifacts.outputs.publish_validated", workflow)
        self.assertEqual(
            workflow.count(
                'gh api --hostname github.com "repos/groopcd/oc-box/releases/tags/$RELEASE_TAG"'
            ),
            2,
        )
        for name in (
            "validated-identity.json",
            "SHA256SUMS",
            "dist/oc_box-*.whl",
            "dist/oc_box-*.tar.gz",
        ):
            self.assertIn(name, workflow)
        self.assertIn("packages-dir: dist/", workflow)
        self.assertIn("for name in sorted(packages):", workflow)
        self.assertNotIn("path: dist/*\n", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("name: pypi", workflow)


class AnonymousAssetDownloadTests(unittest.TestCase):
    def response(self, data, *, url=None, length=None, status=200):
        class Response(io.BytesIO):
            def geturl(self):
                return (
                    url
                    or "https://release-assets.githubusercontent.com/github-production-release-asset/fixture"
                )

        response = Response(data)
        response.status = status
        response.headers = {} if length is None else {"Content-Length": length}
        return response

    def download(self, response):
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                return response

        with patch.object(artifacts, "build_opener", return_value=Opener()):
            result = artifacts.download_digest("v1.6.0", "oc_box-1.6.0.tar.gz", 3)
        return result, requests

    def test_fixed_canonical_anonymous_download_and_bounded_streaming(self):
        digest, requests = self.download(self.response(b"abc"))
        self.assertEqual(digest, hashlib.sha256(b"abc").hexdigest())
        request, timeout = requests[0]
        self.assertEqual(
            request.full_url,
            "https://github.com/groopcd/oc-box/releases/download/v1.6.0/oc_box-1.6.0.tar.gz",
        )
        self.assertEqual(timeout, 20)
        self.assertIsNone(request.get_header("Authorization"))
        for response in (
            self.response(b"ab"),
            self.response(b"abcd"),
            self.response(b"abc", length="4"),
            self.response(b"abc", length="invalid"),
            self.response(b"abc", status=404),
            self.response(b"abc", url="https://attacker.example.invalid/file"),
            self.response(b"abc", url="http://github.com/file"),
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                self.download(response)

    def test_redirect_origin_and_selector_refusal_matrix(self):
        for url in (
            "http://github.com/file",
            "https://attacker.example.invalid/file",
            "https://github.com.attacker.example.invalid/file",
            "https://user:password@github.com/file",
            "https://github.com:443/file",
            "https://github.com/file#fragment",
        ):
            with self.subTest(url=url):
                self.assertFalse(artifacts.allowed_download_url(url))
                with self.assertRaises(ValueError):
                    artifacts.CanonicalRedirect().redirect_request(
                        None, None, 302, "", {}, url
                    )
        with patch.object(artifacts, "build_opener", side_effect=AssertionError):
            for tag, name in (
                ("../v1.6.0", "oc_box-1.6.0.tar.gz"),
                ("v1.6.0", "../oc_box-1.6.0.tar.gz"),
                ("v1.6.0", "https://attacker.example.invalid/archive.tar.gz"),
                ("v1.6.0", "oc_box-1.6.0.tar.gz?token=untrusted"),
                ("v1.6.1", "oc_box-1.6.0.tar.gz"),
            ):
                with self.subTest(tag=tag, name=name), self.assertRaises(ValueError):
                    artifacts.download_digest(tag, name, 3)


if __name__ == "__main__":
    unittest.main()
