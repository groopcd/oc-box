#!/usr/bin/env python3
"""HOST-only, file-only diagnosis of an original failed sharing backup scan.

No Podman/native OpenCode, workspace start, vault unlock, restore, retry, purge,
report rewrite or plaintext staging. The recorded scanner and source must match
their original Git identities. Read-only results are diagnostics, not acceptance.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import secrets
import stat
import sys
from unittest.mock import patch

FORMAT = "oc-box-sharing-backup-diagnostic-v1"
HISTORY = "test_session_sync_podman.py"
RECORDED_FILES = (
    "test_sharing_podman.py",
    HISTORY,
    "sharing_fixture_provider.py",
    "sharing_archive_scan.py",
)
COMPONENTS = ("project.age", "home.age", "registry.age", "image.age")
HOST_SCOPE = (
    "Synthetic external-host production sharing CLI/PTY, real rootless "
    "workspace/native tools; no real providers/projects or security certification."
)
CATEGORIES = frozenset(
    (
        "host-only",
        "invalid-options",
        "placement",
        "purpose",
        "source-changed",
        "scanner-changed",
        "metadata",
        "archive-envelope",
        "evidence-changed",
        "runtime-boot-stale",
        "runtime-boot-marker",
        "runtime-boot-current",
        "interrupted",
        "io-error",
        "diagnostic-failed",
    )
)


class Refusal(Exception):
    pass


def require(value, category="purpose"):
    if not value:
        raise Refusal(category)


def category(error):
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return "interrupted"
    if isinstance(error, Refusal) or type(error).__name__ in (
        "Refusal",
        "FixtureError",
    ):
        if str(error) == "runtime-boot-mismatch":
            return "runtime-boot-stale"
        return str(error) if str(error) in CATEGORIES else "metadata"
    if isinstance(error, OSError):
        return "io-error"
    return "diagnostic-failed"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, "purpose")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def private_file(fixture, root, name, maximum):
    path = fixture.safe_path(root, name)
    info = path.stat()
    require(
        info.st_uid == os.getuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
        and 0 < info.st_size <= maximum,
        "metadata",
    )
    return path


def outside_checkouts(parent):
    # Worktrees/submodules use a .git file, ordinary checkouts a directory.
    # No git invocation (and no directory creation) is needed to reject either.
    require(
        not any(
            (ancestor / ".git").exists() or (ancestor / ".git").is_symlink()
            for ancestor in (parent, *parent.parents)
        ),
        "placement",
    )


def parser():
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, "sharing backup diagnostic: category=invalid-options\n")

    result = Parser(description=__doc__)
    result.add_argument("--source", type=Path, required=True)
    result.add_argument("--expect-commit", required=True)
    result.add_argument("--fixture", type=Path, required=True)
    result.add_argument("--recorded-harness-commit", required=True)
    result.add_argument(
        "--temp-dir",
        type=Path,
        required=True,
        help="Existing parent outside all fixtures/checkouts; encrypted temporary components only",
    )
    return result


def validated_evidence(args, history, loaded):
    """File-only validation. Never use validate_store or inspect resources."""
    f = loaded.fixture
    root = f.no_symlinks(args.fixture.expanduser()).resolve(strict=True)
    require(not root.is_relative_to(loaded.source), "placement")
    f.validate_fixture(root, allow_unrecorded=True)  # Files/selector bytes only.
    f.require_runtime_boot(
        root
    )  # Refuse stale retained engine markers without probing it.
    purpose = f.private_json(root, "sharing-purpose.json")
    report = f.private_json(root, "sharing-result.json")
    require(
        isinstance(purpose, dict)
        and purpose.get("format") == "oc-box-sharing-host-v1"
        and purpose.get("root") == str(root)
        and purpose.get("uid") == os.getuid()
        and purpose.get("source_identity") == loaded.identity,
        "purpose",
    )
    require(
        isinstance(report, dict)
        and report.get("format") == "oc-box-sharing-host-v1"
        and report.get("status") == "FAIL"
        and report.get("stage") == "backup-restore"
        and report.get("scope") == HOST_SCOPE
        and report.get("source_identity") == loaded.identity
        and isinstance(report.get("checks"), dict)
        and report["checks"].get("reset_no_regrant") is True
        and "backup_external_tree_excluded" not in report["checks"],
        "purpose",
    )
    native = report.get("native")
    recorded_native = purpose.get("native")
    require(
        isinstance(native, dict)
        and set(native) == {"version", "sha256"}
        and native.get("version") == "2.2.6"
        and isinstance(native.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", native["sha256"])
        and isinstance(recorded_native, dict)
        and all(recorded_native.get(key) == value for key, value in native.items()),
        "purpose",
    )
    records = f.workspace_records(root)
    require(
        set(records) == {"source-a", "source-b"}
        and not (root / "restored-full").exists()
        and not (root / "restored-full").is_symlink(),
        "purpose",
    )
    original = args.recorded_harness_commit
    harness_root = Path(__file__).resolve().parents[1]
    require(
        history.git(harness_root, "rev-parse", "--is-shallow-repository").strip()
        == b"false",
        "purpose",
    )
    expected = {
        name: hashlib.sha256(
            history.git(harness_root, "show", original + ":tools/" + name)
        ).hexdigest()
        for name in RECORDED_FILES
    }
    require(
        purpose.get("harness_sha256") == expected
        and report.get("harness_sha256") == expected,
        "purpose",
    )
    scanner = Path(__file__).resolve().with_name("sharing_archive_scan.py")
    require(
        not scanner.is_symlink() and digest(scanner) == expected[scanner.name],
        "scanner-changed",
    )
    require(digest(scanner.with_name(HISTORY)) == expected[HISTORY], "source-changed")
    archive = private_file(f, root, "sharing-full.ocbox", f.crypto.MAX_BACKUP)
    identity = private_file(f, root, "sharing-age-identity.txt", f.crypto.MAX_SMALL)
    watched = [
        archive,
        identity,
        private_file(f, root, "sharing-purpose.json", f.crypto.MAX_SMALL),
        private_file(f, root, "sharing-result.json", f.crypto.MAX_SMALL),
    ]
    return root, records["source-a"][1], archive, identity, scanner, watched


def inspect_archive(loaded, archive, identity, scanner, source, probe):
    f = loaded.fixture
    results = {}
    try:
        with f.crypto.opened(archive, [identity]) as (manifest, directory, private):
            f.backups.validate_manifest(loaded.box, manifest)
            require(
                manifest.get("kind") == "full"
                and manifest.get("source_workspace_id") == source["id"]
                and manifest.get("project") == source["project"]
                and manifest.get("container_project")
                == loaded.box.container_project(source)
                and manifest.get("oc_box", {}).get("commit")
                == loaded.identity["commit"]
                and manifest["oc_box"].get("dirty") is False
                and set(manifest.get("components", {})) == set(COMPONENTS),
                "purpose",
            )
            for name in COMPONENTS:
                path = directory / name
                measured = f.crypto.digest(path)
                require(measured == manifest["components"][name], "evidence-changed")
                value = probe.probe_component(
                    f.crypto,
                    path,
                    private,
                    scanner,
                    "oci" if name == "image.age" else "tar",
                )
                require(f.crypto.digest(path) == measured, "evidence-changed")
                results[name] = {
                    **value,
                    "ciphertext": measured,
                    "ciphertext_unchanged": True,
                }
        return results
    except f.crypto.BackupError:
        raise Refusal("archive-envelope") from None


def main(argv=None):
    output = os.fdopen(os.dup(1), "w", buffering=1)
    result = {
        "format": FORMAT,
        "status": "diagnostic-refused",
        "category": None,
        "secondary_categories": [],
        "components": {},
        "engine_commands": 0,
        "acceptance": False,
    }
    try:
        directory = Path(__file__).resolve().parent
        original_flags = sys.flags
        # Refuse even a wrongly invoked root/container run without leaving .pyc
        # files. Preserve the immutable launch flags for the separate -I/-B gate.
        sys.dont_write_bytecode = True
        # -B prevents writes, not loading stale caches. Ignore cache files even
        # for the first host-guard helper, before load_source isolates imports.
        sys.pycache_prefix = (
            "/nonexistent-oc-box-diagnostic-bytecode-" + secrets.token_hex(16)
        )
        history = load_file("_sharing_backup_history", directory / HISTORY)
        history.host_guard()  # Must precede source, placement, fixture or key reads.
        require(
            original_flags.isolated and original_flags.dont_write_bytecode,
            "invalid-options",
        )
        args = parser().parse_args(argv)
        require(
            all(
                re.fullmatch(r"[0-9a-f]{40}", value or "")
                for value in (args.expect_commit, args.recorded_harness_commit)
            ),
            "invalid-options",
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        parent = history.fresh_parent(args.temp_dir)
        outside_checkouts(parent)
        loaded = history.load_source(args.source, args.expect_commit)
        loaded.fixture.host_guard()
        require(
            not parent.is_relative_to(loaded.source)
            and not parent.is_relative_to(directory.parent),
            "placement",
        )
        probe = load_file(
            "_sharing_backup_probe", directory / "sharing_backup_probe.py"
        )
        f = loaded.fixture
        with f.private_output():
            root, source, archive, identity, scanner, watched = validated_evidence(
                args, history, loaded
            )
            require(not parent.is_relative_to(root), "placement")
            before = {path: digest(path) for path in watched}
            result["source_identity"] = {
                key: loaded.identity[key]
                for key in ("commit", "commit_count", "version", "dirty")
            }
            result["recorded_harness_commit"] = args.recorded_harness_commit
            result["diagnostic_sha256"] = {
                path.name: digest(path)
                for path in (
                    Path(__file__).resolve(),
                    directory / "sharing_backup_probe.py",
                    directory / HISTORY,
                    scanner,
                )
            }
            try:
                with (
                    patch.dict(os.environ, f.isolated_env(root), clear=True),
                    f.temporary_location(root, parent),
                ):
                    result["components"] = inspect_archive(
                        loaded, archive, identity, scanner, source, probe
                    )
            except BaseException as error:
                result["category"] = category(error)
            finally:
                # Check every original file even when one changed or inspection
                # failed. Never replace the primary failure with a later check.
                unchanged = True
                for path, old in before.items():
                    try:
                        private_file(
                            f,
                            root,
                            path.name,
                            f.crypto.MAX_BACKUP
                            if path == archive
                            else f.crypto.MAX_SMALL,
                        )
                        require(digest(path) == old, "evidence-changed")
                    except BaseException:
                        unchanged = False
                result["original_evidence_unchanged"] = unchanged
                checks = [] if unchanged else ["evidence-changed"]
                try:
                    require(
                        history.source_snapshot(args.source, args.expect_commit)
                        == loaded.identity,
                        "source-changed",
                    )
                except BaseException:
                    checks.append("source-changed")
                for name, old in result["diagnostic_sha256"].items():
                    try:
                        require(digest(directory / name) == old, "scanner-changed")
                    except BaseException:
                        checks.append("scanner-changed")
                for failure in dict.fromkeys(checks):
                    if result["category"] is None:
                        result["category"] = failure
                    elif failure != result["category"]:
                        result["secondary_categories"].append(failure)
            if result["category"] is None:
                result["status"] = "diagnostic-only"
            result["scope"] = (
                "File-only component inspection with independent authentication and the original recorded scanner; no engine, lifecycle, replay, restore or acceptance claim."
            )
    except BaseException as error:
        result["status"] = "diagnostic-refused"
        failure = category(error)
        if result["category"] is None:
            result["category"] = failure
        elif (
            failure != result["category"]
            and failure not in result["secondary_categories"]
        ):
            result["secondary_categories"].append(failure)
    finally:
        print(json.dumps(result, indent=2), file=output, flush=True)
        output.close()
    return 0 if result["status"] == "diagnostic-only" else 1


if __name__ == "__main__":
    sys.exit(main())
