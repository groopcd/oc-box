"""Host-only encrypted workspace backup and staged, stopped restore.

No credential/config refresh, vault unlock, model call, or container exec occurs.
Only fixed managed resources are captured; external mounts/devices are recorded
as informational requirements and are never automatically reactivated.
"""

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid

if __package__:
    from . import backup_crypto as crypto, credentials, workspace_identity
    from .version_info import get_version_info
else:
    import backup_crypto as crypto
    import credentials
    import workspace_identity
    from version_info import get_version_info

BackupError = crypto.BackupError


def command(box, *args, check=True, timeout=60):
    with crypto.memory_file() as output, crypto.memory_file() as errors:
        try:
            result = subprocess.run(
                ["podman", *map(str, args)],
                stdout=output,
                stderr=errors,
                env=crypto.environment(),
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupError(
                "Host Podman backup operation failed or timed out; private output was not logged."
            ) from exc
        if check and result.returncode:
            raise BackupError(
                "Podman backup/restore operation failed. Existing workspaces were not overwritten; check host permissions, storage, and Podman support."
            )
        output.seek(0)
        value = output.read(crypto.MAX_SMALL + 1).decode(errors="replace")
        if len(value) > crypto.MAX_SMALL:
            raise BackupError("Oversized Podman backup metadata response.")
        return subprocess.CompletedProcess(result.args, result.returncode, value, "")


def owned_container(box, data):
    result = command(box, "container", "inspect", data["container"], check=False)
    if result.returncode:
        if (
            command(
                box, "container", "exists", data["container"], check=False
            ).returncode
            == 1
        ):
            return None
        raise BackupError("Cannot inspect backup container.")
    info = json.loads(result.stdout)[0]
    if (info.get("Config", {}).get("Labels") or {}).get(box.LABEL) != data["id"]:
        raise BackupError("Container backup ownership mismatch.")
    return info


def archive_command(box, action, root, *, private=False, limit=None):
    result = [sys.executable, str(box.APP / "backup_archive.py"), action, str(root)]
    if private:
        result = ["podman", "unshare", *result]
    if limit is not None:
        result.extend(["--limit", str(limit)])
    if private and action == "extract":
        result.append("--owners")
    return result


def owned_volume(box, data):
    info = json.loads(command(box, "volume", "inspect", data["home_volume"]).stdout)[0]
    if (info.get("Labels") or {}).get(box.LABEL) != data["id"] or not info.get(
        "Mountpoint"
    ):
        raise BackupError("Private home volume ownership/inspection mismatch.")
    return info


def remove_restore_resource(box, kind, data):
    """Never delete a same-named resource unless its fresh owner label matches."""
    name = data["container"] if kind == "container" else data["home_volume"]
    exists = command(box, kind, "exists", name, check=False)
    if exists.returncode == 1:
        return True
    if exists.returncode:
        return False
    if kind == "container":
        owned_container(box, data)
    else:
        owned_volume(box, data)
    return (
        command(
            box,
            kind,
            "rm",
            *(["--force"] if kind == "container" else []),
            name,
            check=False,
        ).returncode
        == 0
    )


def remove_staged_tree(path):
    """Remove only this operation's tree, even if archived modes were 0000.

    Destination roots are private and caller-owned; never follow archived links
    while granting the owner traversal needed for rollback. Volume cleanup is
    Podman's responsibility because its descendants may have mapped owners.
    """
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise BackupError(
            "Rollback tree is not a real directory; inspect staging manually."
        )
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, 0o700)

        def unlock(directory):
            for name in os.listdir(directory):
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                # chmod through /proc's pinned parent, not a path with mutable
                # ancestor symlinks. No root/development-container authority.
                child = Path(f"/proc/self/fd/{directory}") / name
                os.chmod(child, 0o700, follow_symlinks=False)
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    unlock(descriptor)
                finally:
                    os.close(descriptor)

        unlock(fd)
    finally:
        os.close(fd)
    shutil.rmtree(path)


def safe_config(data):
    """Reconstruct via a whitelist; never trust an archived create command."""
    result = {
        key: data[key]
        for key in ("distro", "image", "home", "cpus", "memory", "pids", "created")
    }
    result["ready"] = bool(data.get("ready"))
    result["credentials"] = credentials.policy(data)
    result["devices"] = list(data.get("devices", []))
    result["podman_args"] = list(data.get("podman_args", []))
    if data.get("opencode_version"):
        result["opencode_version"] = data["opencode_version"]
    return result


def activate_project(staging, destination):
    """Atomic Linux no-replace rename, even if an empty destination races us."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise BackupError(
            "Restore requires Linux renameat2 for atomic non-overwrite activation."
        ) from exc
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(destination), 1):
        raise BackupError(
            "Cannot activate restored project without overwrite; destination may have appeared or its filesystem lacks renameat2 support."
        )


def make_backup(box, args):
    crypto.validate_recipients(args.recipient or [])
    minimum = crypto.passphrase_minimum(
        args.recipient or [], getattr(args, "min_passphrase_length", None)
    )
    path, data = box.find_workspace(args.project, args.workspace)
    if data is None:
        raise BackupError(
            "No registered workspace to back up; select an existing project or workspace ID."
        )
    output = Path(args.output).expanduser().absolute()
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise BackupError(
            "Backup output parent must be an existing real directory outside the project."
        )
    output = output.parent.resolve() / output.name
    if output.exists() or output.is_symlink():
        raise BackupError(
            "Backup output already exists; choose a new filename (backups never overwrite)."
        )
    for protected in (Path(data["project"]), box.state_root()):
        if output.is_relative_to(protected):
            raise BackupError(
                "Backup output must be outside the project and workspace registry."
            )
    box.check_backend(env=crypto.environment(), feature="backup")
    crypto.tools()
    with box.workspace_lock(path):
        box.refresh_workspace_identity(path, data)
        info = owned_container(box, data)
        if info is None:
            raise BackupError(
                "Container is missing; full consistency cannot be captured. Recover the environment first."
            )
        if info["State"].get("Paused"):
            raise BackupError("Container is paused; stop it from the host first.")
        volume = owned_volume(box, data)
        if output.is_relative_to(Path(volume["Mountpoint"]).resolve()):
            raise BackupError("Backup output cannot be inside the private home volume.")
        # External mounts and volumes are never silently captured. A full backup
        # records their requirements but is not claimed to preserve their data.
        approved = {
            (data["project"], box.container_project(data)),
            (volume["Mountpoint"], data["home"]),
        }
        additional = [
            m
            for m in info.get("Mounts", [])
            if (m.get("Source"), m.get("Destination")) not in approved
            and m.get("Type") in ("bind", "volume")
        ]
        project_mount = next(
            (
                mount
                for mount in info.get("Mounts", [])
                if (mount.get("Source"), mount.get("Destination"))
                == (data["project"], box.container_project(data))
            ),
            None,
        )
        if project_mount is None:
            raise BackupError(
                "Container project mount does not match the selected registry."
            )
        if additional:
            raise BackupError(
                "This container has additional bind mounts/volumes. Back up those separately and remove them from a dedicated backup fixture first; implicit capture of outside host data is refused."
            )
        if info["State"]["Running"]:
            print(
                "Stopping the existing container for a consistent backup. It will remain stopped; host editors/builds must also stay idle.",
                flush=True,
            )
            command(box, "stop", "--time", "10", data["container"])
        stopped = owned_container(box, data)
        if stopped is None or stopped["State"]["Running"]:
            raise BackupError(
                "Container did not stop; refusing a live database backup."
            )
        image_id = None
        image_tag = None
        temporary_output = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="oc-box-backup-ciphertext-", dir=output.parent
            ) as temp:
                staging = Path(temp)
                _, recipient = crypto.generate_key(
                    args.recipient or [], staging / "key.age", minimum=minimum
                )
                parts = {}
                for name, producer in (
                    ("project.age", archive_command(box, "create", data["project"])),
                    (
                        "home.age",
                        archive_command(
                            box, "create", volume["Mountpoint"], private=True
                        ),
                    ),
                    ("registry.age", archive_command(box, "create", path.parent)),
                ):
                    with box.loading_stage(
                        "Encrypting " + name.split(".")[0] + " backup"
                    ):
                        crypto.encrypt_stream(producer, recipient, staging / name)
                    parts[name] = crypto.digest(staging / name)
                if not args.portable:
                    with box.loading_stage("Capturing and encrypting container OS"):
                        image_tag = "localhost/oc-box-backup-" + uuid.uuid4().hex
                        image_id = command(
                            box,
                            "commit",
                            "--quiet",
                            data["container"],
                            image_tag,
                            timeout=600,
                        ).stdout.strip()
                        if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", image_id):
                            raise BackupError(
                                "Unexpected committed image identity; backup was not finalized."
                            )
                        crypto.encrypt_stream(
                            ["podman", "save", "--format", "oci-archive", image_id],
                            recipient,
                            staging / "image.age",
                        )
                        parts["image.age"] = crypto.digest(staging / "image.age")
                manifest = {
                    "format": "oc-box-backup",
                    "format_version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "kind": "portable" if args.portable else "full",
                    "architecture": platform.machine(),
                    "oc_box": get_version_info(box.APP),
                    "source_workspace_id": data["id"],
                    "project": data["project"],
                    "container_project": box.container_project(data),
                    "configuration": safe_config(data),
                    "sessions": box.session_state(path),
                    "image_id": image_id,
                    "components": parts,
                    "expanded_byte_limit": crypto.MAX_BACKUP,
                    "limitations": [
                        "not a running-process checkpoint",
                        "host vault and env files outside project are excluded",
                        "devices and additional Podman settings require explicit reconfiguration",
                        "host project must remain idle; no recursive nested filesystem backup",
                        "no ACL/xattr/SELinux label restore",
                    ],
                }
                crypto.encrypt_bytes(
                    json.dumps(manifest, sort_keys=True).encode(),
                    recipient,
                    staging / "manifest.age",
                )
                fd, filename = tempfile.mkstemp(
                    prefix=".oc-box-backup-", dir=output.parent
                )
                temporary_output = Path(filename)
                with os.fdopen(fd, "wb") as stream:
                    crypto.pack(staging, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Atomic no-overwrite publication, not replace().
                os.link(temporary_output, output)
                temporary_output.unlink()
                temporary_output = None
                parent = os.open(
                    output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                )
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                print(
                    f"Encrypted {'portable' if args.portable else 'full'} backup saved: {output}"
                )
                print(
                    "The source container remains stopped. Its files, sessions, and host vault were not altered."
                )
        finally:
            if temporary_output is not None:
                temporary_output.unlink(missing_ok=True)
            if image_id is not None and re.fullmatch(
                r"(?:sha256:)?[0-9a-f]{64}", image_id
            ):
                command(box, "image", "rm", image_tag, check=False)


def validate_manifest(box, value):
    if not isinstance(value, dict):
        raise BackupError("Invalid backup manifest.")
    if not isinstance(value.get("source_workspace_id"), str):
        raise BackupError("Invalid source workspace identity in backup.")
    if not re.fullmatch(r"[0-9a-f]{32}", value.get("source_workspace_id", "")):
        raise BackupError("Invalid source workspace identity in backup.")
    for key in ("project", "container_project"):
        path = value.get(key)
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or Path(path) in (Path("/"), Path("/home"))
            or str(Path(path)) != path
            or ".." in Path(path).parts
            or any(char in path for char in ("\0", "\n", ":", ","))
        ):
            raise BackupError("Invalid project path in backup manifest.")
    config = value.get("configuration")
    if (
        not isinstance(config, dict)
        or config.get("distro") not in box.DISTROS
        or not isinstance(config.get("home"), str)
        or not config["home"].startswith("/")
        or Path(config["home"]) in (Path("/"), Path("/home"))
        or str(Path(config["home"])) != config["home"]
        or ".." in Path(config["home"]).parts
        or any(char in config["home"] for char in ("\0", "\r", "\n", ":", ","))
        or not isinstance(config.get("devices"), list)
        or not all(isinstance(item, str) for item in config["devices"])
        or not isinstance(config.get("podman_args"), list)
        or not all(isinstance(item, str) for item in config["podman_args"])
    ):
        raise BackupError("Invalid distro/private-home configuration in backup.")
    if config.get("opencode_version") is not None:
        try:
            box.opencode_compat.version(config["opencode_version"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise BackupError("Invalid archived OpenCode release identity.") from exc
    if (
        len(config["devices"]) > 128
        or len(config["podman_args"]) > 128
        or any(
            "\0" in item or "\r" in item or "\n" in item
            for item in config["devices"] + config["podman_args"]
        )
    ):
        raise BackupError("Invalid saved host requirements in backup.")
    if (
        not isinstance(config.get("ready"), bool)
        or not isinstance(value.get("architecture"), str)
        or not isinstance(value.get("created_at"), str)
    ):
        raise BackupError("Invalid backup platform/setup metadata.")
    for location in (value["container_project"], config["home"]):
        if any(
            Path(location) == Path(base) or Path(location).is_relative_to(Path(base))
            for base in (
                "/dev",
                "/proc",
                "/sys",
                "/etc",
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/run",
                "/opt/oc-box",
            )
        ):
            raise BackupError(
                "Archived container mount paths overlap system/runtime locations."
            )
    if Path(config["home"]).is_relative_to(Path(value["container_project"])):
        raise BackupError("Project mount would expose private home contents.")
    if (
        not isinstance(config.get("cpus"), str)
        or not re.fullmatch(r"\d+(?:\.\d+)?", config["cpus"])
        or not isinstance(config.get("memory"), str)
        or not re.fullmatch(r"\d+(?:\.\d+)?[kKmMgGtT]?[bB]?", config["memory"])
        or type(config.get("pids")) is not int
        or config["pids"] < 32
    ):
        raise BackupError("Invalid resource limits in backup.")
    credentials.policy({"credentials": config.get("credentials")})
    sessions = value.get("sessions")
    if (
        not isinstance(sessions, dict)
        or not isinstance(sessions.get("sessions", []), list)
        or not all(
            box.valid_session_id(ident) for ident in sessions.get("sessions", [])
        )
        or (
            sessions.get("last") is not None
            and not box.valid_session_id(sessions["last"])
        )
    ):
        raise BackupError("Invalid saved session selection in backup.")
    if sessions.get("last") is not None and sessions["last"] not in sessions.get(
        "sessions", []
    ):
        raise BackupError("Selected session is not in the saved session list.")
    if value["kind"] == "full" and not re.fullmatch(
        r"(?:sha256:)?[0-9a-f]{64}", value.get("image_id") or ""
    ):
        raise BackupError("Invalid backup image ID.")
    if (
        type(value.get("expanded_byte_limit")) is not int
        or not 1 <= value["expanded_byte_limit"] <= crypto.MAX_BACKUP
    ):
        raise BackupError("Invalid extraction bound.")


def inspect_backup(box, args):
    with crypto.opened(args.file, args.identity or []) as (manifest, _, _):
        validate_manifest(box, manifest)
        summary = json.loads(json.dumps(manifest))
        summary["configuration"]["podman_args"] = [
            option.split("=", 1)[0] + "=[value omitted]"
            for option in manifest["configuration"]["podman_args"]
        ]
        print(json.dumps(summary, indent=2))


def restore_backup(box, args):
    box.check_backend(env=crypto.environment(), feature="restore")
    if args.project is None:
        raise BackupError(
            "Restore requires --project PATH (a new, nonexistent host destination)."
        )
    destination = Path(args.project).expanduser().absolute()
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise BackupError("Restore parent must exist and not be a symlink.")
    destination = destination.parent.resolve() / destination.name
    protected = (
        box.state_root(),
        box.APP,
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "opencode",
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        / "opencode",
        box.vault.vault_path().parent,
    )
    if any(destination.is_relative_to(location.resolve()) for location in protected):
        raise BackupError(
            "Restore destination must be outside trusted launcher, registry, vault, and host OpenCode config/data directories."
        )
    if (
        destination.exists()
        or destination.is_symlink()
        or box.registered_projects(destination)
    ):
        raise BackupError(
            "Restore never overwrites an existing directory/workspace. Choose a new destination or move the original aside explicitly."
        )
    if not destination.name or destination.name in (".", ".."):
        raise BackupError("Invalid restore destination.")
    with crypto.opened(args.file, args.identity or []) as (
        manifest,
        encrypted,
        private,
    ):
        validate_manifest(box, manifest)
        if manifest["architecture"] != platform.machine():
            raise BackupError(
                "Backup CPU architecture differs from this host. Native home/OS binaries cannot be restored safely here."
            )
        config = manifest["configuration"]
        print(
            "Restore keeps original in-container project/home paths to preserve session and tool references. It creates fresh host workspace IDs and leaves the container stopped."
        )
        if config["devices"] or config["podman_args"]:
            print(
                "Archived device mappings/additional Podman options are NOT activated; reconfigure them explicitly after inspecting backup-info."
            )
        ident = uuid.uuid4().hex
        directory = box.state_root() / "workspaces" / ident
        project_stage = Path(
            tempfile.mkdtemp(prefix=".oc-box-restore-", dir=destination.parent)
        )
        volume_name = "oc-box-home-" + ident
        data = {
            "version": 2,
            "id": ident,
            "project": str(destination),
            "container_project": manifest["container_project"],
            "home": config["home"],
            "distro": config["distro"],
            "image": manifest["image_id"]
            if manifest["kind"] == "full"
            else box.DISTROS[config["distro"]][0],
            "container": "oc-box-" + ident,
            "home_volume": volume_name,
            "cpus": config["cpus"],
            "memory": config["memory"],
            "pids": config["pids"],
            "credentials": config["credentials"],
            "devices": [],
            "podman_args": [],
            "created": int(datetime.now(timezone.utc).timestamp()),
            "ready": manifest["kind"] == "full" and bool(config.get("ready")),
            "restored_from": {
                "workspace_id": manifest["source_workspace_id"],
                "backup_created_at": manifest["created_at"],
                "kind": manifest["kind"],
                "devices": config["devices"],
                "podman_args": config["podman_args"],
            },
            "restore_incomplete": True,
            "restore_staging_project": str(project_stage),
        }
        if config.get("opencode_version"):
            data["opencode_version"] = config["opencode_version"]
        volume_created = False
        registry_created = False
        container_created = False
        project_activated = False
        success = False
        try:
            # Validate the new location without putting an active marker there.
            box.validate_project(project_stage)
            if destination.is_relative_to(
                box.state_root()
            ) or destination.is_relative_to(box.vault.vault_path().parent):
                raise BackupError(
                    "Restore destination cannot be inside private host state/vault storage."
                )
            directory.mkdir(parents=True, mode=0o700, exist_ok=False)
            registry_created = True
            (directory / "controller").mkdir(mode=0o700)
            with box.workspace_lock(directory / "workspace.json"):
                # Journal our fresh resource IDs before any large restoration.
                # A hard host/process crash must leave an identifiable, blocked
                # registry entry rather than an anonymous private-home volume.
                workspace_identity.record(
                    data, *workspace_identity.snapshot(project_stage)
                )
                box.atomic_json(directory / "workspace.json", data)
                with box.loading_stage("Restoring project into private staging"):
                    crypto.decrypt_stream(
                        encrypted / "project.age",
                        private,
                        archive_command(
                            box,
                            "extract",
                            project_stage,
                            limit=manifest["expanded_byte_limit"],
                        ),
                    )
                marker = project_stage / box.MARKER
                if (
                    marker.is_symlink()
                    or not marker.is_file()
                    or marker.stat().st_size > 64
                    or marker.stat().st_nlink != 1
                    or marker.read_text().strip() != manifest["source_workspace_id"]
                ):
                    raise BackupError(
                        "Project backup marker does not match the encrypted manifest."
                    )
                marker.write_text(ident + "\n")
                box.validate_project(project_stage)
                for kind, name in (
                    ("volume", volume_name),
                    ("container", data["container"]),
                ):
                    if command(box, kind, "exists", name, check=False).returncode != 1:
                        raise BackupError(
                            "Fresh restore resource name already exists or could not be checked; refusing to reuse it."
                        )
                command(
                    box,
                    "volume",
                    "create",
                    "--label",
                    f"{box.LABEL}={ident}",
                    volume_name,
                )
                volume_created = True
                volume = owned_volume(box, data)
                with box.loading_stage("Restoring private home and session database"):
                    crypto.decrypt_stream(
                        encrypted / "home.age",
                        private,
                        archive_command(
                            box,
                            "extract",
                            volume["Mountpoint"],
                            private=True,
                            limit=manifest["expanded_byte_limit"],
                        ),
                    )
                # Only registry JSON/control metadata used by oc-box is needed
                # for resume; old absolute imports/host-archive associations are
                # evidence below, not active control data for this new ID.
                registry_stage = directory / "recovered-registry"
                registry_stage.mkdir(mode=0o700)
                crypto.decrypt_stream(
                    encrypted / "registry.age",
                    private,
                    archive_command(
                        box,
                        "extract",
                        registry_stage,
                        limit=manifest["expanded_byte_limit"],
                    ),
                )
                # Retain registry bytes as evidence only, never activate archived
                # paths, permissions, hooks, archive associations or lock files.
                original_record = registry_stage / "workspace.json"
                if (
                    original_record.is_symlink()
                    or not original_record.is_file()
                    or original_record.stat().st_size > crypto.MAX_SMALL
                ):
                    raise BackupError(
                        "Recovered registry metadata must be a bounded regular file."
                    )
                old = box.read_json(original_record)
                if (
                    not isinstance(old, dict)
                    or old.get("id") != manifest["source_workspace_id"]
                    or old.get("project") != manifest["project"]
                ):
                    raise BackupError("Recovered registry does not match manifest.")
                if manifest["kind"] == "full":
                    with box.loading_stage("Loading backed-up container OS image"):
                        crypto.decrypt_stream(
                            encrypted / "image.age",
                            private,
                            ["podman", "load", "--quiet"],
                        )
                    image = json.loads(
                        command(box, "image", "inspect", data["image"]).stdout
                    )[0]
                    if image.get("Id", image.get("ID", "")).removeprefix(
                        "sha256:"
                    ) != data["image"].removeprefix("sha256:"):
                        raise BackupError(
                            "Loaded image does not match manifest identity."
                        )
                # Rename a staged directory only if destination is still absent.
                if (
                    destination.exists()
                    or destination.is_symlink()
                    or any(
                        record != directory / "workspace.json"
                        for record, _ in box.registered_projects(destination)
                    )
                ):
                    raise BackupError(
                        "Restore destination appeared during staging; no overwrite was attempted."
                    )
                activate_project(project_stage, destination)
                project_activated = True
                workspace_identity.record(
                    data, *workspace_identity.snapshot(destination)
                )
                saved = manifest["sessions"]
                box.atomic_json(
                    box.session_file(directory / "workspace.json"),
                    {"sessions": saved.get("sessions", []), "last": saved.get("last")},
                )
                box.atomic_json(directory / "workspace.json", data)
                # create_container adds only standard known limits/mounts and
                # overrides the image entrypoint; it does not start image code.
                container_created = True
                box.create_container(
                    directory / "workspace.json", data, environment=crypto.environment()
                )
                data["ready"] = manifest["kind"] == "full" and bool(config.get("ready"))
                data.pop("restore_incomplete", None)
                data.pop("restore_staging_project", None)
                box.atomic_json(directory / "workspace.json", data)
                success = True
            print(f"Restored stopped workspace {ident} at {destination}.")
            print(f"Resume with: oc-box --workspace {ident}")
            print(
                "The host vault was not restored/unlocked. Recreate required vault/env values before a selected-mode launch. Portable restores reinstall baseline/OS dependencies on their first normal start."
            )
        finally:
            if not success:
                cleanup_ok = True
                if container_created:
                    try:
                        cleanup_ok = remove_restore_resource(box, "container", data)
                    except BackupError:
                        cleanup_ok = False
                if volume_created:
                    try:
                        volume_ok = remove_restore_resource(box, "volume", data)
                        cleanup_ok = cleanup_ok and volume_ok
                    except BackupError:
                        cleanup_ok = False
                if project_activated and cleanup_ok:
                    # Delete only the newly staged directory with our fresh ID;
                    # never touch an unrelated path that appeared at destination.
                    marker = destination / box.MARKER
                    if (
                        destination.is_dir()
                        and not destination.is_symlink()
                        and marker.is_file()
                        and not marker.is_symlink()
                        and marker.read_text().strip() == ident
                        and workspace_identity.same_project(data, destination)
                    ):
                        try:
                            remove_staged_tree(destination)
                        except (OSError, BackupError):
                            cleanup_ok = False
                    else:
                        cleanup_ok = False
                # This directory was created with our random ID. Do not remove
                # caller-supplied paths or a registry that predated this attempt.
                if (
                    cleanup_ok
                    and registry_created
                    and directory.is_dir()
                    and not directory.is_symlink()
                ):
                    remove_staged_tree(directory)
                elif not cleanup_ok and registry_created:
                    data["restore_incomplete"] = True
                    box.atomic_json(directory / "workspace.json", data)
                    print(
                        f"Restore cleanup could not remove all new resources. Incomplete workspace {ident} retained for host inspection/purge; it cannot be started.",
                        file=sys.stderr,
                    )
            if project_stage.exists():
                remove_staged_tree(project_stage)


def parser_for(action):
    parser = argparse.ArgumentParser(prog="oc-box " + action)
    if action == "backup":
        parser.add_argument("--project", type=Path)
        parser.add_argument("--workspace")
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument(
            "--portable",
            action="store_true",
            help="omit container OS; recreate it on first restore launch",
        )
        parser.add_argument(
            "--recipient",
            action="append",
            help="native age public recipient; repeatable; otherwise prompt for a backup passphrase",
        )
        parser.add_argument(
            "--min-passphrase-length",
            type=int,
            help="minimum characters for a new backup passphrase, 8..1024; overrides host config; incompatible with --recipient",
        )
    else:
        parser.add_argument("file", type=Path)
        parser.add_argument(
            "--identity",
            type=Path,
            action="append",
            help="native age identity file; otherwise age prompts for the backup passphrase",
        )
        if action == "restore":
            parser.add_argument(
                "--project",
                type=Path,
                required=True,
                help="new, nonexistent host project destination",
            )
    return parser


def cli(box, arguments):
    action = arguments[0]
    args = parser_for(action).parse_args(arguments[1:])
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        raise BackupError(
            "Run backup/restore on the host; host Podman/registry must not be exposed inside a development container."
        )
    if os.geteuid() == 0:
        raise BackupError("Run backup/restore as your regular host user, without sudo.")
    crypto.tools()
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
    except (OSError, ValueError):
        pass
    handlers = {}

    def cancel(*_):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, cancel)
        try:
            if action == "backup":
                make_backup(box, args)
            elif action == "backup-info":
                inspect_backup(box, args)
            else:
                restore_backup(box, args)
        except (KeyError, TypeError, IndexError, UnicodeError) as exc:
            raise BackupError(
                "Invalid backup metadata or resource response; no existing workspace was overwritten."
            ) from exc
    finally:
        for sig, previous in handlers.items():
            signal.signal(sig, previous)
    return 0
