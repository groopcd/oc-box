"""Read-only file consistency for one audited frozen-source restore failure.

Does not run Podman/OpenCode, inspect volumes, decrypt archives, repair selectors,
or prove current stop/historical home equality. No automatic fixture discovery.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = SOURCE = None
COMMIT = "9a6e99ad01b3bf02e19edb3ec9d7ab3289d40f22"
ORIGINAL = "ce8613bb75325e750af09db7bf3b8fe3ac1d00dd84a63aeb4cae8c26f1fb30a1"
COMPANION = "164fc630cb1952574082d8c06bc2aa7beecb2e40c91802d24454739d1e64ab00"
IDENTITY = "fc2164c2545a30d52d4bf4eb261591823aff869b603abf0d61293a50bfc75daa"
BOOT = re.compile(rb"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\n?")
HEX = re.compile(r"[0-9a-f]{32}")
KEY = "ACCEPTANCE_API_KEY"


class Refusal(Exception):
    pass


def need(value, category):
    if not value:
        raise Refusal(category)


def directory(path):
    """Pin each ancestor; no symlinks, discovery, recursion or directory listing."""
    path = Path(path)
    need(path.is_absolute() and ".." not in path.parts, "path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in path.parts[1:]:
            nxt = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def read(path, limit, *, private=True, links=1, digest=None, optional=False):
    """Owned, bounded NOFOLLOW/NOATIME file read with stable fstat identity."""
    fd = directory(Path(path).parent)
    try:
        pin_flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            pinned = os.open(Path(path).name, pin_flags, dir_fd=fd)
        except FileNotFoundError:
            if optional:
                return None
            raise Refusal("missing-file")
        try:
            before = os.fstat(pinned)
            need(
                stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid(),
                "file-owner",
            )
            need(before.st_nlink == links and before.st_size <= limit, "file-kind-size")
            need(not before.st_mode & (0o077 if private else 0o022), "file-mode")
            # O_PATH pins without opening a FIFO/device for data. Reopen only
            # the already verified regular inode via the kernel's own fd link.
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOATIME | os.O_NONBLOCK
            file_fd = os.open("/proc/self/fd/" + str(pinned), flags)
            try:
                opened = os.fstat(file_fd)
                need(
                    (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                    "changing-file",
                )
                blocks, remaining = [], limit + 1
                while remaining:
                    block = os.read(file_fd, min(65536, remaining))
                    if not block:
                        break
                    blocks.append(block)
                    remaining -= len(block)
                raw = b"".join(blocks)
                after = os.fstat(file_fd)
            finally:
                os.close(file_fd)
            fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
                "st_mode",
                "st_uid",
                "st_nlink",
            )
            need(
                len(raw) <= limit
                and all(getattr(before, k) == getattr(after, k) for k in fields),
                "changing-file",
            )
            if digest is not None:
                need(hashlib.sha256(raw).hexdigest() == digest, "report-or-source-hash")
            return raw
        finally:
            os.close(pinned)
    finally:
        os.close(fd)


def pairs(items):
    result = {}
    for key, value in items:
        need(key not in result, "duplicate-json-key")
        result[key] = value
    return result


def document(path, *, limit=65536, **options):
    raw = read(path, limit, **options)
    if raw is None:
        return None
    value = json.loads(raw, object_pairs_hook=pairs)
    need(isinstance(value, dict), "json-object")
    return value


def selection(path):
    value = document(path, optional=True)
    if value is None:
        return None
    need(isinstance(value.get("sessions", []), list), "session-state")
    last = value.get("last")
    need(
        last is None
        or isinstance(last, str)
        and re.fullmatch(r"ses_[A-Za-z0-9]+", last),
        "session-state",
    )
    return last


def audit():
    need(os.geteuid() != 0 and os.environ.get("OC_BOX_CONTAINER") != "1", "host-only")
    need(
        not Path("/.dockerenv").exists() and not Path("/run/.containerenv").exists(),
        "host-only",
    )
    fd = directory(ROOT)
    try:
        info = os.fstat(fd)
        need(info.st_uid == os.getuid() and not info.st_mode & 0o077, "private-root")
    finally:
        os.close(fd)
    original = document(ROOT / "result.json", limit=4 * 1024 * 1024, digest=ORIGINAL)
    companion = document(
        ROOT / "external-lifecycle-result.json",
        limit=4 * 1024 * 1024,
        links=2,
        digest=COMPANION,
    )
    build = original["source"]["build"]
    need(
        build["commit"] == COMMIT
        and build["commit_count"] == 123
        and build["dirty"] is False,
        "source-binding",
    )
    need(
        companion["original_report_sha256"] == ORIGINAL
        and companion["primary_failure"]["category"] == "stopped-restore",
        "report-binding",
    )
    need(
        companion["provenance"]["application"]["sha256"]
        == original["source"]["sha256"],
        "source-binding",
    )
    need(
        original["source"]["sha256"]["src/oc_box/workspace_identity.py"] == IDENTITY,
        "source-binding",
    )
    metadata = document(ROOT / "fixture.json")
    need(
        metadata
        == {
            "format": "oc-box-v2-only-lifecycle-host-acceptance",
            "version": 1,
            "projects": ["source-a", "restored-full", "restored-portable"],
            "distro": "tumbleweed",
        },
        "fixture-binding",
    )
    profile = document(ROOT / "native-profile.json")
    need(profile == original["native"], "native-binding")
    need(
        document(ROOT / "executables.json") == {"2": profile["path"]}, "native-binding"
    )
    cached = read(ROOT / "podman-tmp/alive", 128, private=False)
    fd = directory(Path("/proc/sys/kernel/random"))
    try:
        boot_fd = os.open(
            "boot_id", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
        )
        with os.fdopen(boot_fd, "rb") as stream:
            current = stream.read(129)
    finally:
        os.close(fd)
    need(BOOT.fullmatch(cached) and BOOT.fullmatch(current), "boot-marker-invalid")
    need(cached == current, "boot-marker-stale")
    need(
        read(ROOT / "config/containers/storage.conf", 16384, private=False).decode()
        == '[storage]\ndriver = "overlay"\nrunroot = '
        + json.dumps(str(ROOT / "podman-run"))
        + "\ngraphroot = "
        + json.dumps(str(ROOT / "data/containers/storage"))
        + "\n",
        "storage-binding",
    )
    expected_engine = (
        "[engine]\nremote = false\ntmp_dir = "
        + json.dumps(str(ROOT / "podman-tmp"))
        + "\nstatic_dir = "
        + json.dumps(str(ROOT / "data/libpod"))
        + "\nvolume_path = "
        + json.dumps(str(ROOT / "data/containers/storage/volumes"))
        + '\nhooks_dir = []\n[containers]\nnetns = "private"\nenv = []\nmounts = []\ndevices = []\n'
    )
    need(
        read(ROOT / "config/containers/containers.conf", 16384, private=False).decode()
        == expected_engine,
        "storage-binding",
    )
    identity_code = read(
        SOURCE / "src/oc_box/workspace_identity.py",
        65536,
        private=False,
        digest=IDENTITY,
    )
    identity = {"__name__": "verified_readonly_directory_identity"}
    exec(compile(identity_code, "<verified-directory-identity>", "exec"), identity)
    ids = document(ROOT / "fixture-ids.json")
    need(
        set(ids) == {"source-a", "restored-full"}
        and all(isinstance(v, str) and HEX.fullmatch(v) for v in ids.values()),
        "journal-binding",
    )
    records = {}
    lasts = {}
    for name, ident in ids.items():
        folder = ROOT / "registry/workspaces" / ident
        data = document(folder / "workspace.json")
        need(data["id"] == ident and data["version"] == 2, "workspace-binding")
        need(
            data["project"] == str(ROOT / name)
            and data["home"] == str(ROOT / "host-home"),
            "workspace-binding",
        )
        need(
            data["container"] == "oc-box-" + ident
            and data["home_volume"] == "oc-box-home-" + ident,
            "workspace-binding",
        )
        need(
            data["distro"] == "tumbleweed"
            and not data.get("devices")
            and not data.get("podman_args"),
            "workspace-policy",
        )
        need(
            data.get("credentials") == {"mode": "selected", "env": [], "vault": [KEY]},
            "workspace-policy",
        )
        need(
            not data.get("restore_incomplete")
            and not data.get("restore_staging_project"),
            "restore-incomplete",
        )
        need(
            read(ROOT / name / ".oc-box-id", 64, private=False).decode().strip()
            == ident,
            "project-marker",
        )
        project_fd = directory(ROOT / name)
        try:
            project_info = os.fstat(project_fd)
            physical = [project_info.st_dev, project_info.st_ino]
            handle = identity["file_handle"](project_fd)
            stable = None
            if handle is not None:
                try:
                    fsid = os.fstatvfs(project_fd).f_fsid
                except (AttributeError, OSError):
                    fsid = None
                if fsid:
                    stable = {
                        "method": "linux-file-handle-v1",
                        "fsid": format(fsid, "x"),
                        "handle": handle,
                    }
            need(
                identity["matches"](data, physical, stable),
                "physical-project-identity",
            )
        finally:
            os.close(project_fd)
        records[name] = data
        lasts[name] = selection(folder / "sessions-v2.json")
    seed = document(ROOT / "seed-baseline.json", limit=4 * 1024 * 1024)
    seed_id = seed["info"]["id"]
    need(
        isinstance(seed_id, str) and re.fullmatch(r"ses_[A-Za-z0-9]+", seed_id),
        "seed-binding",
    )
    need(
        seed["info"]["location"]["directory"] == str(ROOT / "source-a")
        and not seed["info"].get("parentID"),
        "seed-binding",
    )
    src, dst = records["source-a"], records["restored-full"]
    need(
        dst.get("restored_from", {}).get("workspace_id") == src["id"]
        and dst["restored_from"].get("kind") == "full",
        "restore-origin",
    )
    recovered = ROOT / "registry/workspaces" / dst["id"] / "recovered-registry"
    old = document(recovered / "workspace.json", optional=True)
    recovered_last = None
    if old is not None:
        need(
            old.get("id") == src["id"] and old.get("project") == src["project"],
            "recovered-binding",
        )
        recovered_last = selection(recovered / "sessions-v2.json")
    return {
        "diagnostic": "stopped-restore-file-consistency",
        "scope": "Current bounded file consistency only; not historical predicate proof or lifecycle acceptance",
        "same_boot_marker": True,
        "restored_version_matches": dst.get("opencode_version") == "2.3.0",
        "fresh_workspace_id": dst["id"] != src["id"],
        "fresh_container_name": dst["container"] != src["container"],
        "fresh_home_volume_name": dst["home_volume"] != src["home_volume"],
        "internal_project_path_preserved": dst.get("container_project", dst["project"])
        == str(ROOT / "source-a"),
        "restored_last_matches_seed": lasts["restored-full"] == seed_id,
        "source_last_matches_seed": lasts["source-a"] == seed_id,
        "recovered_registry_present": old is not None,
        "recovered_last_matches_seed": old is not None and recovered_last == seed_id,
        "restored_last_matches_recovered": old is not None
        and lasts["restored-full"] == recovered_last,
        "home_tree_comparison_recomputed": False,
        "engine_or_native_cli_executed": False,
        "volume_or_archive_read": False,
    }


if __name__ == "__main__":
    try:
        need(sys.flags.isolated and sys.dont_write_bytecode, "isolated-python-required")
        parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--root", type=Path, required=True)
        parser.add_argument("--source", type=Path, required=True)
        args = parser.parse_args()
        ROOT, SOURCE = args.root, args.source
        print(json.dumps(audit(), indent=2))
    except BaseException as error:
        category = (
            error.args[0] if isinstance(error, Refusal) else "file-or-schema-refusal"
        )
        print(
            json.dumps(
                {
                    "diagnostic": "stopped-restore-file-consistency",
                    "status": "refused",
                    "category": category,
                }
            )
        )
        raise SystemExit(1) from None
