"""Offline config-generation contracts, not APT installation or TLS acceptance."""

import contextlib
import io
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import ci_ubuntu_apt as apt


def config_values(path):
    """Read the generated flat directives as quoted values, not prose matches."""
    values = {}
    for line in path.read_text().splitlines():
        if not line.endswith(";"):
            raise AssertionError("missing directive terminator")
        key, value = shlex.split(line[:-1])
        if key in values:
            raise AssertionError("duplicate directive")
        values[key] = value
    return values


class UbuntuAptConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ci-ubuntu-apt-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.os_release = self.root / "os-release"
        self.os_release.write_text('ID=ubuntu\nVERSION_CODENAME="noble"\n')
        self.ca = self.root / "public-ca.crt"
        self.ca.write_bytes(b"synthetic public CA bytes\r\n\x00\xff\n")
        self.output = self.root / "config"

    def generate(self, output=None, **kwargs):
        return apt.generate(
            self.output if output is None else output,
            os_release=self.os_release,
            ca_bundle=self.ca,
            **kwargs,
        )

    def test_supported_suite_matrix_and_isolated_source_selection(self):
        for codename in ("noble", "resolute"):
            with self.subTest(codename=codename):
                output = self.root / codename
                config = self.generate(output, codename=codename)
                self.assertEqual(config, output / "apt.conf")
                self.assertTrue(config.is_absolute())
                settings = config_values(config)
                sources = Path(settings["Dir::Etc::sourcelist"])
                parts = Path(settings["Dir::Etc::sourceparts"])
                self.assertEqual(sources, output / "ubuntu.sources")
                self.assertEqual(parts, output / "sources.list.d")
                self.assertTrue(parts.is_dir())
                self.assertEqual(list(parts.iterdir()), [])
                fields = dict(
                    line.split(": ", 1) for line in sources.read_text().splitlines()
                )
                self.assertEqual(
                    fields,
                    {
                        "Types": "deb",
                        "URIs": "https://mirrors.edge.kernel.org/ubuntu",
                        "Suites": " ".join(
                            codename + suffix
                            for suffix in ("", "-updates", "-backports", "-security")
                        ),
                        "Components": "main restricted universe multiverse",
                        "Signed-By": "/usr/share/keyrings/ubuntu-archive-keyring.gpg",
                    },
                )
                self.assertEqual(
                    set(output.iterdir()),
                    {config, sources, parts, output / "ca-certificates.crt"},
                )

    def test_allowlisted_mirror_suite_and_ipv4_matrix(self):
        mirrors = {
            "edge": "https://mirrors.edge.kernel.org/ubuntu",
            "azure": "https://azure.archive.ubuntu.com/ubuntu",
            "ubuntu": "https://archive.ubuntu.com/ubuntu",
        }
        self.assertEqual(apt.MIRROR, mirrors["edge"])
        self.assertEqual(apt.MIRRORS, mirrors)
        for mirror, uri in mirrors.items():
            for codename in ("noble", "resolute"):
                for force_ipv4 in (False, True):
                    with self.subTest(
                        mirror=mirror, codename=codename, force_ipv4=force_ipv4
                    ):
                        output = self.root / f"{mirror}-{codename}-{force_ipv4}"
                        settings = config_values(
                            self.generate(
                                output,
                                codename=codename,
                                mirror=mirror,
                                force_ipv4=force_ipv4,
                            )
                        )
                        sources = Path(settings["Dir::Etc::sourcelist"])
                        fields = dict(
                            line.split(": ", 1)
                            for line in sources.read_text().splitlines()
                        )
                        self.assertEqual(
                            fields,
                            {
                                "Types": "deb",
                                "URIs": uri,
                                "Suites": " ".join(
                                    codename + suffix
                                    for suffix in (
                                        "",
                                        "-updates",
                                        "-backports",
                                        "-security",
                                    )
                                ),
                                "Components": "main restricted universe multiverse",
                                "Signed-By": "/usr/share/keyrings/ubuntu-archive-keyring.gpg",
                            },
                        )
                        if force_ipv4:
                            self.assertEqual(settings["Acquire::ForceIPv4"], "true")
                        else:
                            self.assertNotIn("Acquire::ForceIPv4", settings)
                        self.assertNotIn("Acquire::ForceIPv6", settings)

    def test_automatic_os_release_matrix(self):
        cases = {
            "version_codename": ("ID=ubuntu\nVERSION_CODENAME=noble\n", "noble"),
            "ubuntu_codename": ('ID="ubuntu"\nUBUNTU_CODENAME=resolute\n', "resolute"),
            "quoted_comments": (
                "# comment\nID='ubuntu' # Ubuntu only\n"
                'PRETTY_NAME="Ubuntu with spaces"\nVERSION_CODENAME="resolute"\n',
                "resolute",
            ),
            "both_fields": (
                "ID=ubuntu\nVERSION_CODENAME=noble\nUBUNTU_CODENAME=noble\n",
                "noble",
            ),
        }
        for name, (release, expected) in cases.items():
            with self.subTest(case=name):
                self.os_release.write_text(release)
                config = self.generate(self.root / name)
                sources = Path(config_values(config)["Dir::Etc::sourcelist"])
                self.assertIn(
                    f"Suites: {expected} {expected}-updates ", sources.read_text()
                )

    def test_explicit_target_ignores_host_detection_matrix(self):
        for name, release in {
            "non_ubuntu": "ID=debian\nVERSION_CODENAME=bookworm\n",
            "malformed": 'ID="unclosed\n',
            "absent": None,
        }.items():
            with self.subTest(case=name):
                if release is None:
                    self.os_release.unlink()
                else:
                    self.os_release.write_text(release)
                config = self.generate(self.root / name, codename="noble")
                self.assertTrue(config.is_file())

    def test_security_and_bounded_acquisition_settings(self):
        expected = {
            "APT::Update::Error-Mode": "any",
            "Acquire::Retries": "2",
            "Acquire::http::Timeout": "20",
            "Acquire::https::Timeout": "20",
            "Acquire::https::Verify-Peer": "true",
            "Acquire::https::Verify-Host": "true",
            "Acquire::AllowInsecureRepositories": "false",
            "Acquire::AllowWeakRepositories": "false",
            "Acquire::AllowDowngradeToInsecureRepositories": "false",
            "APT::Get::AllowUnauthenticated": "false",
            "Acquire::Check-Valid-Until": "true",
            "Acquire::Check-Date": "true",
        }
        path_keys = {
            "Dir::Etc::sourcelist",
            "Dir::Etc::sourceparts",
            "Acquire::https::CaInfo",
        }
        for mode, options in (
            ("default", {}),
            ("azure_ipv4", {"mirror": "azure", "force_ipv4": True}),
        ):
            with self.subTest(mode=mode):
                settings = config_values(self.generate(self.root / mode, **options))
                actual = {
                    key: value
                    for key, value in settings.items()
                    if key not in path_keys
                }
                wanted = dict(expected)
                if options:
                    wanted["Acquire::ForceIPv4"] = "true"
                self.assertEqual(actual, wanted)
                self.assertEqual(set(settings), set(wanted) | path_keys)
                ca_copy = Path(settings["Acquire::https::CaInfo"])
                self.assertEqual(ca_copy.read_bytes(), self.ca.read_bytes())

    def test_explicit_default_options_preserve_generated_contents(self):
        implicit = self.generate()
        explicit = self.generate(
            self.root / "explicit", mirror="edge", force_ipv4=False
        )
        self.assertEqual(
            (implicit.parent / "ubuntu.sources").read_bytes(),
            (explicit.parent / "ubuntu.sources").read_bytes(),
        )
        self.assertEqual(
            implicit.read_text().replace(str(implicit.parent), "OUTPUT"),
            explicit.read_text().replace(str(explicit.parent), "OUTPUT"),
        )

    def test_invalid_mirror_and_ipv4_options_refuse_before_reads_or_writes(self):
        marker = self.root / "must-not-be-created"
        attacks = (
            "unknown",
            "Azure",
            "",
            None,
            [],
            {},
            "https://archive.ubuntu.com/ubuntu",
            "https://user:secret@archive.ubuntu.com/ubuntu",
            "http://azure.archive.ubuntu.com/ubuntu",
            "azure\nAcquire::https::Verify-Peer false;",
            f"$(touch {marker})",
            f"`touch {marker}`",
            f"azure;touch {marker}",
            "../azure",
        )
        for attack in attacks:
            with self.subTest(mirror=attack):
                with (
                    patch.object(Path, "read_text") as read_text,
                    patch.object(Path, "read_bytes") as read_bytes,
                    self.assertRaisesRegex(
                        apt.ConfigError,
                        "^supported Ubuntu mirrors are edge, azure and ubuntu$",
                    ),
                ):
                    self.generate(mirror=attack)
                read_text.assert_not_called()
                read_bytes.assert_not_called()
                self.assertEqual(set(self.root.iterdir()), {self.ca, self.os_release})
        for value in ("true", "false", "", None, 0, 1, [], {}):
            with self.subTest(force_ipv4=value):
                with (
                    patch.object(Path, "read_text") as read_text,
                    patch.object(Path, "read_bytes") as read_bytes,
                    self.assertRaisesRegex(
                        apt.ConfigError, "^IPv4-only acquisition must be a boolean$"
                    ),
                ):
                    self.generate(force_ipv4=value)
                read_text.assert_not_called()
                read_bytes.assert_not_called()
                self.assertEqual(set(self.root.iterdir()), {self.ca, self.os_release})

    def test_ca_copy_is_byte_exact_and_original_inputs_are_immutable(self):
        originals = {}
        sources = self.root / "original.sources"
        sources.write_text("Types: deb\nURIs: https://example.invalid/ubuntu\n")
        for path in (sources, self.ca, self.os_release):
            path.chmod(0o600)
            originals[path] = (path.read_bytes(), path.stat())
        config = self.generate()
        ca_copy = Path(config_values(config)["Acquire::https::CaInfo"])
        self.assertEqual(ca_copy, self.output / "ca-certificates.crt")
        self.assertEqual(ca_copy.read_bytes(), self.ca.read_bytes())
        self.assertNotEqual(ca_copy.stat().st_ino, self.ca.stat().st_ino)
        for path, (content, before) in originals.items():
            with self.subTest(input=path.name):
                self.assertEqual(path.read_bytes(), content)
                after = path.stat()
                self.assertEqual(
                    (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns),
                    (
                        before.st_ino,
                        before.st_mode,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    ),
                )

    def test_public_modes_under_restrictive_umask_leave_parent_unchanged(self):
        self.root.chmod(0o700)
        old_umask = os.umask(0o077)
        try:
            self.generate()
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        for path in (self.output, *self.output.iterdir()):
            with self.subTest(path=path.name):
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode), 0o755 if path.is_dir() else 0o644
                )

    def test_existing_output_matrix_preserves_entries_and_symlink_targets(self):
        target = self.root / "target"
        target.mkdir()
        marker = target / "original"
        marker.write_bytes(b"untouched")
        missing = self.root / "absent-target"
        outputs = {}
        for name in (
            "directory",
            "file",
            "directory_link",
            "file_link",
            "dangling_link",
        ):
            output = self.root / name
            if name == "directory":
                output.mkdir()
                (output / "original").write_bytes(b"untouched")
            elif name == "file":
                output.write_bytes(b"untouched")
            else:
                output.symlink_to(
                    {
                        "directory_link": target,
                        "file_link": marker,
                        "dangling_link": missing,
                    }[name]
                )
            outputs[name] = output
        for name, output in outputs.items():
            with self.subTest(kind=name):
                before = output.lstat()
                with self.assertRaisesRegex(apt.ConfigError, "output already exists"):
                    self.generate(output)
                after = output.lstat()
                self.assertEqual(
                    (after.st_ino, after.st_mode, after.st_mtime_ns, after.st_ctime_ns),
                    (
                        before.st_ino,
                        before.st_mode,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    ),
                )
        self.assertEqual(marker.read_bytes(), b"untouched")
        self.assertEqual(list(target.iterdir()), [marker])
        self.assertFalse(missing.exists())

    def test_invalid_detected_release_matrix_refuses_before_writes(self):
        cases = {
            "wrong_id": "ID=debian\nVERSION_CODENAME=noble\n",
            "missing_id": "VERSION_CODENAME=noble\n",
            "missing_codename": "ID=ubuntu\n",
            "unsupported": "ID=ubuntu\nVERSION_CODENAME=jammy\n",
            "unclosed_quote": 'ID=ubuntu\nVERSION_CODENAME="noble\n',
            "extra_tokens": "ID=ubuntu\nVERSION_CODENAME=noble extra\n",
            "invalid_assignment": "ID=ubuntu\nnot an assignment\n",
            "duplicate_id": "ID=ubuntu\nID=debian\nVERSION_CODENAME=noble\n",
            "duplicate_codename": "ID=ubuntu\nVERSION_CODENAME=noble\nVERSION_CODENAME=resolute\n",
        }
        for name, release in cases.items():
            with self.subTest(case=name):
                self.os_release.write_text(release)
                with self.assertRaises(apt.ConfigError):
                    self.generate()
                self.assertFalse(self.output.exists())
        for name, data in {"absent": None, "invalid_utf8": b"\xff"}.items():
            with self.subTest(case=name):
                if data is None:
                    self.os_release.unlink()
                else:
                    self.os_release.write_bytes(data)
                with self.assertRaises(apt.ConfigError):
                    self.generate()
                self.assertFalse(self.output.exists())

    def test_code_evaluation_and_codename_injection_matrix(self):
        marker = self.root / "must-not-be-created"
        attacks = (
            f"$(touch {marker})",
            f"`touch {marker}`",
            f"noble;touch {marker}",
            "noble\nAcquire::https::Verify-Peer false;",
            "../noble",
            "Noble",
            "",
        )
        for attack in attacks:
            for mode in ("explicit", "os_release"):
                with self.subTest(payload=attack, mode=mode):
                    if mode == "os_release":
                        self.os_release.write_text(
                            f'ID=ubuntu\nVERSION_CODENAME="{attack}"\n'
                        )
                    with self.assertRaises(apt.ConfigError):
                        self.generate(
                            **({"codename": attack} if mode == "explicit" else {})
                        )
                    self.assertFalse(self.output.exists())
                    self.assertFalse(marker.exists())
        self.os_release.write_text(
            f'ID=ubuntu\nNAME="$(touch {marker})"\nVERSION_CODENAME=noble\n'
        )
        self.generate()
        self.assertFalse(marker.exists())

    def test_unsafe_apt_output_paths_refuse_before_writes(self):
        for character in ('"', "\\", "\n", "\r", "\t", "\x00", "\x7f", "\udcff"):
            with self.subTest(character=repr(character)):
                output = self.root / ("unsafe" + character + "path")
                with self.assertRaisesRegex(apt.ConfigError, "safely quoted"):
                    self.generate(output)
        self.assertEqual(set(self.root.iterdir()), {self.ca, self.os_release})

    def test_relative_output_and_quoted_spaces_remain_absolute(self):
        output = self.root / "config with spaces and 'apostrophe'"
        relative = Path(os.path.relpath(output))
        config = self.generate(relative)
        self.assertEqual(config, output / "apt.conf")
        settings = config_values(config)
        self.assertEqual(
            Path(settings["Dir::Etc::sourcelist"]), output / "ubuntu.sources"
        )

    def test_missing_empty_and_nonfile_ca_refuse_before_output_creation(self):
        for kind in ("missing", "empty", "directory"):
            with self.subTest(kind=kind):
                ca = self.root / (kind + "-ca")
                if kind == "empty":
                    ca.write_bytes(b"")
                elif kind == "directory":
                    ca.mkdir()
                with self.assertRaises(apt.ConfigError):
                    apt.generate(self.output, codename="noble", ca_bundle=ca)
                self.assertFalse(self.output.exists())
        with patch.object(
            Path, "read_bytes", side_effect=PermissionError("private detail")
        ):
            with self.assertRaisesRegex(
                apt.ConfigError, "^cannot read public CA bundle$"
            ):
                self.generate(codename="noble")
        self.assertFalse(self.output.exists())

    def test_cli_prints_only_absolute_config_path_or_fixed_error(self):
        real_generate = apt.generate

        def generate_for_fixture(output, *, codename, mirror, force_ipv4):
            return real_generate(
                output,
                codename=codename,
                mirror=mirror,
                force_ipv4=force_ipv4,
                ca_bundle=self.ca,
            )

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(
            apt, "generate", side_effect=generate_for_fixture
        ) as generated:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = apt.main(["--output", str(self.output), "--codename", "noble"])
        generated.assert_called_once_with(
            self.output, codename="noble", mirror="edge", force_ipv4=False
        )
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), str(self.output / "apt.conf") + "\n")
        self.assertEqual(stderr.getvalue(), "")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = apt.main(["--output", str(self.output), "--codename", "noble"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            stderr.getvalue(),
            "ci_ubuntu_apt: output already exists; choose a fresh directory\n",
        )

    def test_cli_invalid_argument_matrix_has_fixed_stderr(self):
        for argv in (
            [],
            ["--output", str(self.output), "--codename", "payload\nsecret"],
            ["--output", str(self.output), "--mirror", "https://example.invalid"],
            ["--output", str(self.output), "--mirror", "Azure"],
            ["--output", str(self.output), "--mirror", "azure\nsecret"],
            [
                "--output",
                str(self.output),
                "--mirror",
                "https://user:secret@archive.ubuntu.com/ubuntu",
            ],
            ["--output", str(self.output), "--mirror", "azure;touch secret"],
            ["--output", str(self.output), "--force-ipv4", "false"],
            ["--output", str(self.output), "--force-ipv4=secret"],
        ):
            with self.subTest(argv=argv):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    with self.assertRaises(SystemExit) as caught:
                        apt.main(argv)
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(), "ci_ubuntu_apt: invalid arguments (use --help)\n"
                )
                self.assertFalse(self.output.exists())

    def test_cli_allowlisted_mirror_and_ipv4_options_reach_generator(self):
        for mirror in ("edge", "azure", "ubuntu"):
            for force_ipv4 in (False, True):
                with self.subTest(mirror=mirror, force_ipv4=force_ipv4):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    argv = [
                        "--output",
                        str(self.output),
                        "--codename",
                        "noble",
                        "--mirror",
                        mirror,
                    ]
                    if force_ipv4:
                        argv.append("--force-ipv4")
                    with (
                        patch.object(
                            apt, "generate", return_value=self.output / "apt.conf"
                        ) as generated,
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        code = apt.main(argv)
                    self.assertEqual(code, 0)
                    generated.assert_called_once_with(
                        self.output,
                        codename="noble",
                        mirror=mirror,
                        force_ipv4=force_ipv4,
                    )
                    self.assertEqual(
                        stdout.getvalue(), str(self.output / "apt.conf") + "\n"
                    )
                    self.assertEqual(stderr.getvalue(), "")

    @unittest.skipUnless(shutil.which("apt-config"), "optional installed APT parser")
    def test_installed_apt_config_parser_reads_generated_values(self):
        env = dict(os.environ)
        env.pop("APT_CONFIG", None)
        for mode, options in (
            ("default", {}),
            ("azure_ipv4", {"mirror": "azure", "force_ipv4": True}),
        ):
            with self.subTest(mode=mode):
                config = self.generate(
                    self.root / ("APT parser with spaces " + mode), **options
                )
                expected = config_values(config)
                pairs = [
                    ("VALUE" + str(index), key) for index, key in enumerate(expected)
                ]
                result = subprocess.run(
                    [
                        shutil.which("apt-config"),
                        "-c",
                        str(config),
                        "shell",
                        *(part for pair in pairs for part in pair),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                )
                actual = dict(
                    shlex.split(line.rstrip(";"))[0].split("=", 1)
                    for line in result.stdout.splitlines()
                )
                self.assertEqual(
                    actual, {variable: expected[key] for variable, key in pairs}
                )


if __name__ == "__main__":
    unittest.main()
