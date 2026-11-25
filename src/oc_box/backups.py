"""Host-only encrypted workspace backup and staged, stopped restore.

No credential/config refresh, vault unlock, model call, or container exec occurs.
Only fixed managed resources are captured; external mounts/devices are recorded
as informational requirements and are never automatically reactivated.
"""

import argparse
from contextlib import ExitStack, contextmanager, nullcontext
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
    from . import (
        backup_crypto as crypto,
        credentials,
        entrypoint_migration,
        workspace_identity,
    )
    from .version_info import get_version_info
else:
    import backup_crypto as crypto
    import credentials
    import entrypoint_migration
    import workspace_identity
    from version_info import get_version_info

BackupError = crypto.BackupError


def host_payload():
    if __package__:
        from . import host_backup_payload
    else:
        import host_backup_payload
    return host_backup_payload


def host_restore():
    if __package__:
        from . import host_backup_restore
    else:
        import host_backup_restore
    return host_backup_restore


def host_auth():
    if __package__:
        from . import host_backup_auth
    else:
        import host_backup_auth
    return host_backup_auth


def _reviewed_paths(args):
    return getattr(args, "_reviewed_paths", {})


def _check_reviewed_parent(path, expected):
    if expected is not None:
        with crypto.reviewed_directory(path, expected):
            pass


def _opened_archive(args):
    paths = _reviewed_paths(args)
    if not paths:
        return crypto.opened(args.file, args.identity or [])
    return crypto.opened(
        args.file,
        args.identity or [],
        reviewed_parent=paths.get("backup_parent"),
        reviewed_file=paths.get("backup_file"),
    )


def _exclude_captured_export(args, *roots):
    source = getattr(args, "opencode_auth_export", None)
    if source is not None and source != "-":
        try:
            path = Path(source).expanduser().resolve(strict=True)
            if not any(path.is_relative_to(Path(root).resolve()) for root in roots):
                return
        except (OSError, ValueError, RuntimeError):
            raise BackupError(
                "Native credential export location could not be verified."
            ) from None
        raise BackupError(
            "Keep the native credential export outside all archived directories."
        )


def _capture_host(args, output):
    payload = host_payload()
    roots = payload.destinations()
    _exclude_captured_export(args, *roots.values())
    if any(output.is_relative_to(root) for root in roots.values()):
        raise BackupError(
            "Backup output must be outside the host configuration and vault directories."
        )
    native_export = getattr(args, "opencode_auth_export", None)
    args._captured_export_inode = None
    auth = None
    if native_export == "-":
        stream = getattr(sys.stdin, "buffer", None)
        if stream is None:
            raise BackupError(
                "Native credential export requires a binary input stream."
            )
        auth = host_auth().read_native_export(stream=stream)
    elif native_export is not None:
        export = Path(native_export).expanduser()
        reviewed = getattr(args, "_reviewed_auth_export", None)
        if reviewed is None:
            try:
                info = export.lstat()
            except OSError:
                raise BackupError(
                    "Native credential export location could not be verified."
                ) from None
            reviewed = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        auth = host_auth().read_native_export(
            export,
            reviewed_identity=reviewed,
        )
        args._captured_export_inode = tuple(reviewed[:2])
    return payload.encode(
        payload.capture(excluded_file=getattr(args, "_captured_export_inode", None))
    ), auth


def _prepare_host_restore(
    snapshot, include_opencode, project=None, *, auth=None, review=None
):
    payload = host_payload()
    destination = None
    if project is not None:
        project = Path(project).expanduser().absolute()
        destination = project.parent.resolve() / project.name
    plan = host_restore().preflight(
        snapshot,
        include_opencode=include_opencode,
        workspace_destination=destination,
        native_database=include_opencode and auth is not None and auth != b"[]",
    )
    binary = None
    data_home = payload.destinations()[payload.OPENCODE_AUTH].parent
    if include_opencode and auth is not None:
        binary = shutil.which("opencode", path=crypto.environment().get("PATH"))
        if not binary:
            raise BackupError(
                "Install a supported native OpenCode before restoring V2 credentials."
            )
        host_auth().check_native_destination(data_home)
    confirm_native = include_opencode and (
        auth is not None
        or any(
            scope in (payload.OPENCODE_CONFIG, payload.OPENCODE_AUTH)
            for scope, _ in snapshot
        )
    )
    if review is not None:
        if not review(plan, confirm_native):
            raise KeyboardInterrupt
        return plan, binary, data_home
    if not confirm_native:
        return plan, binary, data_home
    # Ordinary CLI calls still require independent controlling-terminal consent.
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY)
        with os.fdopen(fd, "r+b", buffering=0) as tty:
            tty.write(
                b"Native OpenCode restore installs backed-up config, plugins and saved "
                b"credentials when present. The next OpenCode launch can execute restored "
                b"code, use restored credentials and grant restored permissions. "
                b"Continue only if you trust this backup. Type restore-opencode: "
            )
            if tty.readline(64).strip() != b"restore-opencode":
                raise BackupError(
                    "Native OpenCode restore was not confirmed; nothing was restored."
                )
    except OSError:
        raise BackupError(
            "Native OpenCode restore requires a controlling terminal for review."
        ) from None
    return plan, binary, data_home


def _apply_host_restore(plan, include_opencode, *, prepared_auth=None):
    result, database = prepared_auth if prepared_auth is not None else (None, None)
    installed = host_restore().apply(
        plan, include_opencode=include_opencode, native_database=database
    )
    if result is not None:
        print(f"Native V2 credentials restored ({result.count} records).")
    print(f"Host configuration restore complete ({installed} files installed).")
    print("If a vault was restored, unlock it later with its original passphrase.")
    if not include_opencode:
        print("Native OpenCode settings and saved credentials were not restored.")
    return installed


@contextmanager
def _archive_publication(output, reviewed_parent):
    with crypto.reviewed_directory(output.parent, reviewed_parent) as parent:
        # No source pathname to substitute (or inadvertently unlink). Require
        # Linux O_TMPFILE and procfs linkat support on the output filesystem
        # rather than falling back to a racy named temporary file.
        try:
            fd = os.open(
                ".", os.O_WRONLY | os.O_TMPFILE | os.O_CLOEXEC, 0o600, dir_fd=parent
            )
        except OSError:
            raise BackupError(
                "Choose an output directory on a filesystem that supports unnamed temporary files."
            ) from None
        try:
            yield parent, fd
        finally:
            os.close(fd)


def _byte_progress(interaction):
    callback = getattr(interaction, "progress_bytes", None)
    return callback if callable(callback) else None


def _checksum(path, interaction):
    if interaction is not None:
        interaction.stage("checksum")
    return crypto.digest(path, progress=_byte_progress(interaction))


def _publish_archive(
    staging, output, *, interaction=None, reviewed_parent=None, publication=None
):
    destination = (
        _archive_publication(output, reviewed_parent)
        if publication is None
        else nullcontext(publication)
    )
    with destination as (parent, fd):
        expected = _entry_identity(os.fstat(parent))
        with os.fdopen(fd, "wb", closefd=False) as stream:
            if interaction is not None:
                interaction.stage("packing")
            crypto.pack(staging, stream, progress=_byte_progress(interaction))
            if interaction is not None:
                interaction.stage("publishing")
            stream.flush()
            os.fsync(stream.fileno())
        if interaction is not None:
            interaction.dispatched()
        _check_reviewed_parent(output.parent, expected)
        try:
            # dir_fd selects linkat, with AT_SYMLINK_FOLLOW on the procfs
            # descriptor reference. The output name is never replaced.
            os.link(
                f"/proc/self/fd/{fd}",
                output.name,
                dst_dir_fd=parent,
                follow_symlinks=True,
            )
        except FileExistsError:
            raise BackupError(
                "Backup output already exists; choose a new filename (backups never overwrite)."
            ) from None
        except OSError:
            raise BackupError(
                "Cannot publish the encrypted archive from its open file; choose an output directory with Linux procfs hard-link support."
            ) from None
        # Never unlink the public name on failure: it may already have
        # been observed, or since replaced by an unrelated entry.
        try:
            os.fsync(parent)
            _check_reviewed_parent(output.parent, expected)
            named = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
            if _entry_identity(named) != _entry_identity(os.fstat(fd)):
                raise BackupError("Archive output changed after publication.")
        except (OSError, BackupError):
            raise BackupError(
                "Archive publication could not be confirmed; inspect the output directory before retrying."
            ) from None


def _host_components(staging, recipient, host, interaction):
    payload, auth = host
    components = {}
    for name, value in (("host.age", payload), ("host-auth.age", auth)):
        if value is not None:
            if interaction is not None:
                interaction.stage("host-settings")
            destination = staging / name
            crypto.encrypt_bytes(value, recipient, destination)
            components[name] = _checksum(destination, interaction)
    return components


@contextmanager
def _ciphertext_staging(output, reviewed_parent, prefix):
    with crypto.reviewed_directory(output.parent, reviewed_parent) as parent:
        base = (
            Path(f"/proc/{os.getpid()}/fd/{parent}")
            if reviewed_parent is not None
            else output.parent
        )
        with tempfile.TemporaryDirectory(prefix=prefix, dir=base) as temporary:
            yield Path(temporary)


def _make_host_only(box, args, minimum, *, interaction=None):
    output = Path(args.output).expanduser().absolute()
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise BackupError("Host backup requires an existing real output directory.")
    output = output.parent.resolve() / output.name
    reviewed_parent = _reviewed_paths(args).get("backup_parent")
    _check_reviewed_parent(output.parent, reviewed_parent)
    if output.exists() or output.is_symlink():
        raise BackupError("Backup output already exists; choose a new filename.")
    with ExitStack() as stack:
        publication = stack.enter_context(_archive_publication(output, reviewed_parent))
        expected_parent = _entry_identity(os.fstat(publication[0]))
        if interaction is not None:
            interaction.stage("host-settings")
        payload = _capture_host(args, output)
        staging = stack.enter_context(
            _ciphertext_staging(
                output, expected_parent, "oc-box-host-backup-ciphertext-"
            )
        )
        if interaction is not None:
            interaction.stage("backup-key")
        _, recipient = crypto.generate_key(
            args.recipient or [], staging / "key.age", minimum=minimum
        )
        _check_reviewed_parent(output.parent, expected_parent)
        manifest = {
            "format": "oc-box-backup",
            "format_version": 2,
            "kind": "host",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "oc_box": get_version_info(box.APP),
            "components": _host_components(staging, recipient, payload, interaction),
        }
        crypto.encrypt_bytes(
            json.dumps(manifest, sort_keys=True).encode(),
            recipient,
            staging / "manifest.age",
        )
        _publish_archive(
            staging, output, interaction=interaction, publication=publication
        )
    print("Encrypted host configuration backup saved.")


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


def archive_command(
    box, action, root, *, private=False, limit=None, pinned=False, excluded_inode=None
):
    script = str(box.PACKAGE / "backup_archive.py")
    if pinned:
        assert action == "extract" and not private
        result = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import os,runpy,sys; root,script,*args=sys.argv[1:];"
            "os.chdir(root); sys.argv=[script,*args]; runpy.run_path(script,run_name='__main__')",
            str(root),
            script,
            action,
            ".",
        ]
    else:
        result = [sys.executable, script, action, str(root)]
    if private:
        result = ["podman", "unshare", *result]
    if limit is not None:
        result.extend(["--limit", str(limit)])
    if private and action == "extract":
        result.append("--owners")
    if excluded_inode is not None:
        result.extend(["--exclude-inode", *map(str, excluded_inode)])
    return result


def owned_volume(box, data):
    info = json.loads(command(box, "volume", "inspect", data["home_volume"]).stdout)[0]
    if (info.get("Labels") or {}).get(box.LABEL) != data["id"] or not info.get(
        "Mountpoint"
    ):
        raise BackupError("Private home volume ownership/inspection mismatch.")
    return info


def remove_restore_resource(box, kind, data, *, allow_missing=True):
    """Never delete a same-named resource unless its fresh owner label matches."""
    name = data["container"] if kind == "container" else data["home_volume"]
    exists = command(box, kind, "exists", name, check=False)
    if exists.returncode == 1:
        # If create never acknowledged a return, one absence observation
        # cannot rule out a late engine side effect.
        return allow_missing
    if exists.returncode:
        return False
    if kind == "container":
        info = owned_container(box, data)
        if info is None:
            return allow_missing
        # Prefer the inspected immutable container ID over a mutable name.
        resource = info.get("Id") or name
    else:
        owned_volume(box, data)
        resource = name
    return (
        command(
            box,
            kind,
            "rm",
            *(["--force"] if kind == "container" else []),
            resource,
            check=False,
        ).returncode
        == 0
        and command(box, kind, "exists", name, check=False).returncode == 1
    )


def remove_staged_tree(path, *, expected=None):
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
        identity = _entry_identity(os.fstat(fd))
        if expected is not None and identity != expected:
            raise BackupError("Rollback tree changed; inspect staging manually.")
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
        # Delete data by descriptor, never through an attacker-replaced root
        # name. The last pathname operation can only remove an empty directory.
        for name in os.listdir(fd):
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode):
                # Python 3.10 rmtree does not take dir_fd.
                shutil.rmtree(Path(f"/proc/self/fd/{fd}") / name)
            else:
                os.unlink(name, dir_fd=fd)
        if not _stage_entry_matches(path, identity):
            raise BackupError("Rollback tree changed; inspect staging manually.")
        path.rmdir()
    finally:
        os.close(fd)


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


def activate_project(staging, destination, *, parent_fd=None, destination_fd=None):
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
    parent = -100 if parent_fd is None else parent_fd
    target_parent = parent if destination_fd is None else destination_fd
    source_name = staging if parent_fd is None else staging.name
    target_name = (
        destination
        if parent_fd is None and destination_fd is None
        else destination.name
    )
    if rename(
        parent, os.fsencode(source_name), target_parent, os.fsencode(target_name), 1
    ):
        raise BackupError(
            "Cannot activate restored project without overwrite; destination may have appeared or its filesystem lacks renameat2 support."
        )


def _entry_identity(info):
    return (info.st_dev, info.st_ino)


def _stage_entry_matches(path, identity):
    if identity is None:
        return False
    try:
        named = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(named.st_mode) and _entry_identity(named) == identity


def _check_stage_entry(path, identity):
    if not _stage_entry_matches(path, identity):
        raise BackupError("Reviewed project staging changed; it cannot be activated.")


def make_backup(box, args, *, interaction=None, reviewed_workspace=None):
    crypto.validate_recipients(args.recipient or [])
    minimum = crypto.passphrase_minimum(
        args.recipient or [], getattr(args, "min_passphrase_length", None)
    )
    include_host = getattr(args, "host", False)
    if getattr(args, "opencode_auth_export", None) is not None and not include_host:
        raise BackupError("--opencode-auth-export requires --host.")
    if include_host and not (args.project or args.workspace or args.portable):
        return _make_host_only(box, args, minimum, interaction=interaction)
    if reviewed_workspace is None:
        path, data = box.find_workspace(args.project, args.workspace)
    else:
        path, data = reviewed_workspace()
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
    reviewed_parent = _reviewed_paths(args).get("backup_parent")
    _check_reviewed_parent(output.parent, reviewed_parent)
    if output.exists() or output.is_symlink():
        raise BackupError(
            "Backup output already exists; choose a new filename (backups never overwrite)."
        )
    for protected in (Path(data["project"]), box.state_root()):
        if output.is_relative_to(protected):
            raise BackupError(
                "Backup output must be outside the project and workspace registry."
            )
    if include_host:
        _exclude_captured_export(args, data["project"], box.state_root(), path.parent)

    def revalidate_review():
        if reviewed_workspace is not None:
            current_path, current_data = reviewed_workspace()
            if current_path != path or current_data != data:
                raise BackupError("Reviewed workspace changed; open a new review.")

    if reviewed_workspace is None:
        box.check_backend(env=crypto.environment(), feature="backup")
    crypto.tools()
    with box.workspace_lock(path), ExitStack() as stack:
        if reviewed_workspace is None:
            verified_path, current_data = box.find_workspace(None, data["id"])
            if verified_path != path or current_data != data:
                raise BackupError(
                    "Workspace changed before backup; retry with the intended workspace."
                )
            box.refresh_workspace_identity(path, data)
        else:
            revalidate_review()
            # Keep the reviewed fingerprint stable. The ordinary CLI's
            # verified legacy identity migration is not a dashboard write.
            current_path, current_data = box.find_workspace(data["project"])
            if current_path != path or current_data != data:
                raise BackupError("Reviewed workspace changed; open a new review.")
            box.check_backend(env=crypto.environment(), feature="backup")
            revalidate_review()
        if entrypoint_migration.in_progress(
            data
        ) or entrypoint_migration.needs_bootstrap(data):
            raise BackupError(
                "Legacy container replacement is not ready for backup. "
                "Finish the verified replacement and baseline setup first."
            )
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
        # Check filesystem support and keep the exact file and parent pinned
        # before stopping the source or capturing any data.
        publication = stack.enter_context(_archive_publication(output, reviewed_parent))
        expected_parent = _entry_identity(os.fstat(publication[0]))
        if include_host:
            _exclude_captured_export(args, volume["Mountpoint"])
            if interaction is not None:
                interaction.stage("host-settings")
            host = _capture_host(args, output)
        else:
            host = None
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
            revalidate_review()
            _check_reviewed_parent(output.parent, expected_parent)
            if interaction is not None:
                interaction.stage("stopping")
                interaction.dispatched()
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
        try:
            with _ciphertext_staging(
                output, expected_parent, "oc-box-backup-ciphertext-"
            ) as staging:
                if interaction is not None:
                    interaction.stage("backup-key")
                _, recipient = crypto.generate_key(
                    args.recipient or [], staging / "key.age", minimum=minimum
                )
                revalidate_review()
                _check_reviewed_parent(output.parent, expected_parent)
                parts = {}
                for name, root, mapped in (
                    ("project.age", data["project"], False),
                    ("home.age", volume["Mountpoint"], True),
                    ("registry.age", path.parent, False),
                ):
                    if interaction is not None:
                        interaction.stage(name.split(".")[0])
                    with box.loading_stage(
                        "Encrypting " + name.split(".")[0] + " backup"
                    ):
                        crypto.encrypt_stream(
                            archive_command(
                                box,
                                "create",
                                root,
                                private=mapped,
                                excluded_inode=getattr(
                                    args, "_captured_export_inode", None
                                ),
                            ),
                            recipient,
                            staging / name,
                            progress=_byte_progress(interaction),
                        )
                    parts[name] = _checksum(staging / name, interaction)
                if not args.portable:
                    with box.loading_stage("Capturing and encrypting container OS"):
                        image_tag = "localhost/oc-box-backup-" + uuid.uuid4().hex
                        if interaction is not None:
                            interaction.stage("image-commit")
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
                        if interaction is not None:
                            interaction.stage("image")
                        crypto.encrypt_stream(
                            ["podman", "save", "--format", "oci-archive", image_id],
                            recipient,
                            staging / "image.age",
                            progress=_byte_progress(interaction),
                        )
                        parts["image.age"] = _checksum(
                            staging / "image.age", interaction
                        )
                if host is not None:
                    parts.update(
                        _host_components(staging, recipient, host, interaction)
                    )
                manifest = {
                    "format": "oc-box-backup",
                    "format_version": 2 if include_host else 1,
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
                        (
                            "host configuration requires explicit --host restoration; external environment files are excluded"
                            if include_host
                            else "host vault and env files outside project are excluded"
                        ),
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
                _publish_archive(
                    staging,
                    output,
                    interaction=interaction,
                    publication=publication,
                )
                print(
                    f"Encrypted {'portable' if args.portable else 'full'} backup saved: {output}"
                )
                print(
                    "The source container remains stopped. Its files, sessions, and host vault were not altered."
                )
        finally:
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


def dashboard_summary(manifest):
    """Explicit metadata projection; unknown manifest/config fields stay private."""
    host_components = (
        {
            "host_component": True,
            "native_auth_export": "host-auth.age" in manifest["components"],
        }
        if "host.age" in manifest.get("components", {})
        else {}
    )
    if manifest["kind"] == "host":
        return {"format": "host", "kind": "host", **host_components}
    return {
        "format": "workspace",
        **host_components,
        **{
            key: manifest[key]
            for key in (
                "kind",
                "created_at",
                "architecture",
                "source_workspace_id",
                "project",
                "container_project",
            )
        },
        "distro": manifest["configuration"]["distro"],
        "session_count": len(manifest["sessions"].get("sessions", [])),
    }


def _masked_podman_options(options):
    # Keep only short, literal long-option names, including bare flags. A
    # malformed entry can contain a value even before the first "=".
    name = re.compile(r"(--[a-z][a-z0-9-]{0,61})(?:=|\Z)")
    return [
        match[1] + "=[value omitted]" for item in options if (match := name.match(item))
    ]


_INFO_TABLE_TEXT_LIMIT = 160
_INFO_TABLE_LIST_LIMIT = 16


def _info_table_cell(value):
    if type(value) is str and len(value) > _INFO_TABLE_TEXT_LIMIT:
        return value[: _INFO_TABLE_TEXT_LIMIT - 1] + "…"
    if type(value) is int and value.bit_length() > 64:
        return "…"
    return value


def _info_table_scalars(source, fields, *, nullable=()):
    if not isinstance(source, dict):
        return {}
    return {
        key: _info_table_cell(source[key])
        for key, kind in fields.items()
        if key in source
        and (type(source[key]) is kind or (key in nullable and source[key] is None))
    }


def _info_table_list(values):
    entries = [
        _info_table_cell(item)
        for item in values[:_INFO_TABLE_LIST_LIMIT]
        if type(item) is str
    ]
    if len(values) > _INFO_TABLE_LIST_LIMIT:
        entries.append(f"[{len(values) - _INFO_TABLE_LIST_LIMIT} more omitted]")
    return entries


def _workspace_info_table(manifest, masked_options):
    """Project only known workspace metadata, with bounds before table rendering."""
    summary = _info_table_scalars(
        manifest,
        {
            "format": str,
            "format_version": int,
            "kind": str,
            "created_at": str,
            "architecture": str,
            "source_workspace_id": str,
            "project": str,
            "container_project": str,
            "expanded_byte_limit": int,
        },
    )
    if isinstance(manifest.get("oc_box"), dict):
        summary["oc_box"] = _info_table_scalars(
            manifest["oc_box"],
            {
                "version": str,
                "commit": str,
                "commit_count": int,
                "dirty": bool,
                "release_channel": str,
            },
            nullable=("commit", "commit_count"),
        )
    config = manifest["configuration"]
    summary["configuration"] = {
        **_info_table_scalars(
            config,
            {
                "distro": str,
                "image": str,
                "home": str,
                "cpus": str,
                "memory": str,
                "pids": int,
                "created": int,
                "ready": bool,
                "opencode_version": str,
            },
        ),
        "devices": _info_table_list(config["devices"]),
        "podman_args": _info_table_list(masked_options),
        "credentials": {
            "mode": config["credentials"]["mode"],
            "env": _info_table_list(config["credentials"]["env"]),
            "vault": _info_table_list(config["credentials"]["vault"]),
        },
    }
    sessions = manifest["sessions"]
    summary["sessions"] = {
        "sessions": _info_table_list(sessions.get("sessions", [])),
        **_info_table_scalars(sessions, {"last": str}, nullable=("last",)),
    }
    if "image_id" in manifest:
        summary["image_id"] = (
            manifest["image_id"] if manifest["kind"] == "full" else None
        )
    components = manifest["components"]
    summary["components"] = {
        name: _info_table_scalars(components[name], {"sha256": str, "bytes": int})
        for name in (
            "project.age",
            "home.age",
            "registry.age",
            "image.age",
            "host.age",
            "host-auth.age",
        )
        if name in components
    }
    if isinstance(manifest.get("limitations"), list):
        summary["limitations"] = _info_table_list(manifest["limitations"])
    return summary


def inspect_backup(box, args, *, interaction=None):
    if interaction is not None:
        interaction.stage("unlocking")
    with _opened_archive(args) as (manifest, _, _):
        host_only = manifest["kind"] == "host"
        if not host_only:
            validate_manifest(box, manifest)
        if interaction is not None:
            interaction.inspection(dashboard_summary(manifest))
            return
        table = getattr(args, "table", False)
        if host_only:
            summary = {"format": "host", "format_version": 2}
        else:
            config = manifest["configuration"]
            options = _masked_podman_options(config["podman_args"])
            summary = (
                _workspace_info_table(manifest, options)
                if table
                else {**manifest, "configuration": {**config, "podman_args": options}}
            )
        if table:
            if __package__:
                from .cli_table import fields
            else:
                from cli_table import fields
            print(fields(summary))
        else:
            print(json.dumps(summary, indent=2))


def restore_backup(box, args, *, interaction=None):
    host = getattr(args, "host", False)
    native = getattr(args, "include_opencode", False)
    if native and not host:
        raise BackupError("--include-opencode requires --host.")
    if not host and args.project is None:
        raise BackupError("Restore requires --project PATH unless using --host.")
    destination = (
        _workspace_destination(box, args.project) if args.project is not None else None
    )
    if destination is not None:
        box.check_backend(env=crypto.environment(), feature="restore")
        _check_reviewed_parent(
            destination.parent, _reviewed_paths(args).get("destination_parent")
        )
    if interaction is not None:
        interaction.stage("unlocking")
    with _opened_archive(args) as opened:
        manifest, encrypted, private = opened
        kind = manifest["kind"]
        if kind == "host" and (not host or args.project is not None):
            raise BackupError(
                "Restore a host-only archive with --host, without --project."
            )
        if host:
            if "host.age" not in manifest["components"]:
                raise BackupError("This archive contains no host configuration.")
            snapshot = host_payload().decode(
                crypto.decrypt_bytes(encrypted / "host.age", private)
            )
            auth = None
            if native and "host-auth.age" in manifest["components"]:
                auth = host_auth().validate_native_export(
                    crypto.decrypt_bytes(encrypted / "host-auth.age", private)
                )
            extra = {}
            if interaction is not None:
                if destination is not None:
                    validate_manifest(box, manifest)
                    summary = {
                        **dashboard_summary(manifest),
                        "destination": str(destination),
                    }
                else:
                    summary = dashboard_summary({**manifest, "kind": "host"})

                def review(plan, confirm_native):
                    return interaction.review(
                        {
                            **summary,
                            "restore_host": True,
                            "restore_opencode": confirm_native,
                            "host_files": plan.file_count,
                        }
                    )

                extra["review"] = review
            plan, binary, data_home = _prepare_host_restore(
                snapshot, native, destination, auth=auth, **extra
            )
            if interaction is not None and binary is not None:
                interaction.stage("host-verify")
            prepared_auth = (
                host_auth().prepare_native_export(
                    auth, binary=binary, data_home=data_home, confirmed=True
                )
                if binary is not None
                else None
            )
        if destination is not None:
            _restore_workspace(
                box,
                destination,
                opened,
                interaction=interaction,
                host=host,
                reviewed_parent=_reviewed_paths(args).get("destination_parent"),
            )
        if host:
            try:
                if interaction is not None:
                    interaction.stage("host-restore")
                    interaction.dispatched()
                installed = _apply_host_restore(
                    plan, native, prepared_auth=prepared_auth
                )
                if interaction is not None:
                    interaction.inspection(
                        {
                            "format": "host",
                            "kind": "host",
                            "status": "restored",
                            "host_file_links": installed,
                        }
                    )
            except (BackupError, KeyboardInterrupt) as error:
                if interaction is not None and isinstance(
                    error, host_restore().HostRestoreError
                ):
                    progress = error.result
                    try:
                        interaction.inspection(
                            {
                                "format": "host",
                                "kind": "host",
                                "status": "incomplete",
                                "host_file_links": progress.files_installed,
                                "host_directories_created": progress.directories_created,
                                "host_lock_files_created": progress.lock_files_created,
                                "host_completion_uncertain": progress.completion_uncertain,
                                "host_failure_category": error.category,
                            }
                        )
                    except (Exception, KeyboardInterrupt):
                        pass  # do not change the failure or its cancellation category
                if args.project is not None:
                    print(
                        "The workspace was restored and remains stopped; host configuration restore did not finish.",
                        file=sys.stderr,
                    )
                raise


def _workspace_destination(box, project):
    destination = Path(project).expanduser().absolute()
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise BackupError("Restore parent must exist and not be a symlink.")
    destination = destination.parent.resolve() / destination.name
    default_config = Path.home() / ".config"
    default_data = Path.home() / ".local/share"
    # Protect both the launcher/native fallback and legacy readers that
    # interpret an explicitly empty XDG value relative to their working dir.
    config = {default_config, Path(os.environ.get("XDG_CONFIG_HOME", default_config))}
    data = {default_data, Path(os.environ.get("XDG_DATA_HOME", default_data))}
    protected = {
        box.state_root(),
        box.APP,
        box.vault.vault_path().parent,
        *(root / name for root in config for name in ("oc-box", "opencode")),
        *(root / name for root in data for name in ("opencode", "oc-box/credentials")),
    }
    if any(
        destination.is_relative_to(location.resolve())
        or location.resolve().is_relative_to(destination)
        for location in protected
    ):
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
    return destination


def _rollback_restore(
    box,
    data,
    record_path,
    registry_identity,
    staged_target,
    activated_target,
    *,
    stage_attempted,
    stage_identity,
    project_extracted,
    activation_attempted,
    completion_attempted,
    create_returned,
    interaction,
):
    directory = record_path.parent
    fenced = _stage_entry_matches(directory, registry_identity)
    if fenced and completion_attempted:
        # An interrupted atomic write may have exposed a complete record.
        # Re-fence under the workspace lock before removing any resources.
        try:
            box.atomic_json(record_path, data)
        except (Exception, KeyboardInterrupt):
            fenced = False
    cleanup_ok = fenced
    if fenced:
        for kind in ("container", "volume"):
            if not data.get(f"restore_{kind}_create_attempted"):
                continue
            # create_container may re-create an unexpectedly missing home.
            # Its own unacknowledged attempt also makes volume absence uncertain.
            allow_missing = kind in create_returned and (
                kind != "volume"
                or not data.get("restore_container_create_attempted")
                or "container" in create_returned
            )
            try:
                removed = remove_restore_resource(
                    box, kind, data, allow_missing=allow_missing
                )
                cleanup_ok = removed and cleanup_ok
            except (Exception, KeyboardInterrupt):
                cleanup_ok = False
        if stage_attempted:
            try:
                if _stage_entry_matches(staged_target, stage_identity):
                    if project_extracted:
                        remove_staged_tree(staged_target, expected=stage_identity)
                    else:
                        # The inode observed after mkdir is not proof of
                        # provenance. Do not traverse or modify an unconfirmed
                        # tree, including interrupted extraction output.
                        staged_target.rmdir()
                elif (
                    cleanup_ok
                    and activation_attempted
                    and _stage_entry_matches(activated_target, stage_identity)
                ):
                    remove_staged_tree(activated_target, expected=stage_identity)
                else:
                    cleanup_ok = False
            except (Exception, KeyboardInterrupt):
                cleanup_ok = False
    if cleanup_ok:
        try:
            remove_staged_tree(directory, expected=registry_identity)
            return
        except (Exception, KeyboardInterrupt):
            # A partial removal could have already unlinked the journal.
            if _stage_entry_matches(directory, registry_identity):
                try:
                    box.atomic_json(record_path, data)
                except (Exception, KeyboardInterrupt):
                    fenced = False
            else:
                fenced = False
    ident = data["id"]
    if not fenced:
        print(
            f"Restore cleanup or its registry fence could not be verified. Inspect workspace {ident} on the host before retrying.",
            file=sys.stderr,
        )
        return
    print(
        f"Restore cleanup could not remove all new resources. Incomplete workspace {ident} retained for host inspection/purge; it cannot be started.",
        file=sys.stderr,
    )
    if interaction is not None:
        try:
            interaction.inspection(
                {
                    "format": "workspace",
                    "workspace_id": ident,
                    "destination": data["project"],
                    "status": "incomplete",
                }
            )
        except (Exception, KeyboardInterrupt):
            pass  # never mask the original failure to report UI metadata


def _restore_workspace(
    box, destination, opened, *, interaction=None, host=False, reviewed_parent=None
):
    with nullcontext(opened) as (manifest, encrypted, private), ExitStack() as stack:
        validate_manifest(box, manifest)
        if manifest["architecture"] != platform.machine():
            raise BackupError(
                "Backup CPU architecture differs from this host. Native home/OS binaries cannot be restored safely here."
            )
        config = manifest["configuration"]
        if (
            interaction is not None
            and not host
            and not interaction.review(
                {
                    **dashboard_summary(manifest),
                    "destination": str(destination),
                }
            )
        ):
            raise KeyboardInterrupt
        print(
            "Restore keeps original in-container project/home paths to preserve session and tool references. It creates fresh host workspace IDs and leaves the container stopped."
        )
        if config["devices"] or config["podman_args"]:
            print(
                "Archived device mappings/additional Podman options are NOT activated; reconfigure them explicitly after inspecting backup-info."
            )
        ident = uuid.uuid4().hex
        parent_fd = stack.enter_context(
            crypto.reviewed_directory(destination.parent, reviewed_parent)
        )
        expected_parent = _entry_identity(os.fstat(parent_fd))
        if interaction is not None:
            interaction.dispatched()
        directory = box.state_root() / "workspaces" / ident
        record_path = directory / "workspace.json"
        name = ".oc-box-restore-" + ident
        project_stage = destination.parent / name
        # Child extraction and rollback address the pinned parent even
        # if its public pathname changes after the review.
        pinned = Path(f"/proc/{os.getpid()}/fd/{parent_fd}")
        staged_target = pinned / name
        activated_target = pinned / destination.name
        project_fd = None
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
        stage_attempted = False
        stage_identity = None
        project_extracted = False
        activation_attempted = False
        completion_attempted = False
        create_returned = set()
        success = False
        _check_reviewed_parent(destination.parent, expected_parent)
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        registry_identity = _entry_identity(directory.lstat())
        with box.workspace_lock(record_path):
            try:
                # Record the stage name before mkdir. An interruption before
                # we capture its inode must leave a fence, not authorize
                # deleting whatever is at that pathname during rollback.
                box.atomic_json(record_path, data)
                (directory / "controller").mkdir(mode=0o700)
                _check_reviewed_parent(destination.parent, expected_parent)
                stage_attempted = True
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    stage_attempted = False
                    raise BackupError(
                        "Fresh project staging name is occupied."
                    ) from None
                entry = staged_target.lstat()
                if (
                    not stat.S_ISDIR(entry.st_mode)
                    or entry.st_uid != os.geteuid()
                    or entry.st_mode & 0o077
                ):
                    raise BackupError("New project staging is not a private directory.")
                stage_identity = _entry_identity(entry)
                project_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                stack.callback(os.close, project_fd)
                if _entry_identity(os.fstat(project_fd)) != stage_identity:
                    raise BackupError(
                        "Reviewed project staging changed before opening."
                    )
                _check_reviewed_parent(destination.parent, expected_parent)
                _check_stage_entry(staged_target, stage_identity)
                with os.scandir(project_fd) as entries:
                    if next(entries, None) is not None:
                        raise BackupError("New project staging was not empty.")
                box.validate_project(project_stage)
                project_contents = Path(f"/proc/{os.getpid()}/fd/{project_fd}")
                workspace_identity.record(
                    data, *workspace_identity.snapshot_fd(project_fd)
                )
                box.atomic_json(record_path, data)
                with box.loading_stage("Restoring project into private staging"):
                    if interaction is not None:
                        interaction.stage("project")
                    crypto.decrypt_stream(
                        encrypted / "project.age",
                        private,
                        archive_command(
                            box,
                            "extract",
                            project_contents,
                            limit=manifest["expanded_byte_limit"],
                            pinned=True,
                        ),
                    )
                marker = project_contents / box.MARKER
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
                project_extracted = True
                _check_reviewed_parent(destination.parent, expected_parent)
                _check_stage_entry(staged_target, stage_identity)
                box.validate_project(project_stage)
                for kind, name in (
                    ("volume", volume_name),
                    ("container", data["container"]),
                ):
                    if command(box, kind, "exists", name, check=False).returncode != 1:
                        raise BackupError(
                            "Fresh restore resource name already exists or could not be checked; refusing to reuse it."
                        )
                # A create can commit in Podman before its caller receives
                # success (or cancellation). Roll back an attempt, not only
                # a successful return, against the new workspace label.
                data["restore_volume_create_attempted"] = True
                box.atomic_json(record_path, data)
                command(
                    box,
                    "volume",
                    "create",
                    "--label",
                    f"{box.LABEL}={ident}",
                    volume_name,
                )
                create_returned.add("volume")
                volume = owned_volume(box, data)
                with box.loading_stage("Restoring private home and session database"):
                    if interaction is not None:
                        interaction.stage("home")
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
                if interaction is not None:
                    interaction.stage("registry")
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
                    if interaction is not None:
                        interaction.stage("image")
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
                _check_reviewed_parent(destination.parent, expected_parent)
                _check_stage_entry(staged_target, stage_identity)
                if (
                    destination.exists()
                    or destination.is_symlink()
                    or any(
                        record != record_path
                        for record, _ in box.registered_projects(destination)
                    )
                ):
                    raise BackupError(
                        "Restore destination appeared during staging; no overwrite was attempted."
                    )
                activation_attempted = True
                activate_project(project_stage, destination, parent_fd=parent_fd)
                _check_reviewed_parent(destination.parent, expected_parent)
                _check_stage_entry(activated_target, stage_identity)
                saved = manifest["sessions"]
                box.atomic_json(
                    box.session_file(record_path),
                    {"sessions": saved.get("sessions", []), "last": saved.get("last")},
                )
                # create_container adds only standard known limits/mounts and
                # overrides the image entrypoint; it does not start image code.
                _check_reviewed_parent(destination.parent, expected_parent)
                _check_stage_entry(activated_target, stage_identity)
                data["restore_container_create_attempted"] = True
                box.atomic_json(record_path, data)
                box.create_container(
                    record_path, data, environment=crypto.environment()
                )
                create_returned.add("container")
                data["ready"] = manifest["kind"] == "full" and bool(config.get("ready"))
                _check_reviewed_parent(destination.parent, expected_parent)
                _check_stage_entry(activated_target, stage_identity)
                completion_attempted = True
                completed = data.copy()
                for key in (
                    "restore_incomplete",
                    "restore_staging_project",
                    "restore_volume_create_attempted",
                    "restore_container_create_attempted",
                ):
                    completed.pop(key)
                box.atomic_json(record_path, completed)
                _check_reviewed_parent(destination.parent, expected_parent)
                _check_stage_entry(activated_target, stage_identity)
                success = True
            finally:
                if not success:
                    _rollback_restore(
                        box,
                        data,
                        record_path,
                        registry_identity,
                        staged_target,
                        activated_target,
                        stage_attempted=stage_attempted,
                        stage_identity=stage_identity,
                        project_extracted=project_extracted,
                        activation_attempted=activation_attempted,
                        completion_attempted=completion_attempted,
                        create_returned=create_returned,
                        interaction=interaction,
                    )
        if interaction is not None:
            interaction.inspection(
                {
                    "format": "workspace",
                    "workspace_id": ident,
                    "destination": str(destination),
                    "kind": manifest["kind"],
                    "status": "stopped",
                }
            )
        print(f"Restored stopped workspace {ident} at {destination}.")
        print(f"Resume with: oc-box --workspace {ident}")
        if not host:
            print(
                "The host vault was not restored/unlocked. Recreate required vault/env values before a selected-mode launch. Portable restores reinstall baseline/OS dependencies on their first normal start."
            )


def parser_for(action):
    descriptions = {
        "backup": (
            "Create an encrypted archive of a registered workspace, host settings, "
            "or both. A workspace backup stops its container if needed and leaves it "
            "stopped; keep host editors and other writers idle during capture."
        ),
        "backup-info": (
            "Verify an encrypted backup's manifest and ciphertext checksums and "
            "report metadata without extracting payloads or using Podman. Host-only "
            "archives show only their type and format version."
        ),
        "restore": (
            "Restore an encrypted archive's workspace, host settings, or both, "
            "without overwriting existing destinations. A restored workspace "
            "is left stopped."
        ),
    }
    requirements = (
        "Run as a regular Linux host user with age, age-keygen, and memfd support."
    )
    if action != "backup-info":
        requirements += " Workspace operations also require rootless Podman."
    parser = argparse.ArgumentParser(
        prog="oc-box " + action, description=descriptions[action], epilog=requirements
    )
    if action == "backup":
        selectors = parser.add_mutually_exclusive_group()
        selectors.add_argument(
            "--project",
            type=Path,
            metavar="PATH",
            help="select a registered project path (defaults to the current directory for workspace backups)",
        )
        selectors.add_argument(
            "--workspace", metavar="ID", help="select a registered workspace by ID"
        )
        parser.add_argument(
            "--host",
            action="store_true",
            help="include host oc-box/OpenCode settings, legacy OpenCode credentials and the encrypted vault; without a workspace selector or --portable, back up only the host",
        )
        parser.add_argument(
            "--opencode-auth-export",
            metavar="FILE",
            help="with --host, include a native OpenCode V2 credential export from a private file outside archived directories, or '-' for stdin",
        )
        parser.add_argument(
            "--output",
            type=Path,
            required=True,
            metavar="FILE",
            help="write a new encrypted archive in an existing directory outside captured data; never overwrite",
        )
        parser.add_argument(
            "--portable",
            action="store_true",
            help="skip the container OS image (included by default in workspace backups); recreate it on first launch after restore",
        )
        parser.add_argument(
            "--recipient",
            action="append",
            metavar="AGE_KEY",
            help="encrypt for a native age X25519 public recipient (repeatable); otherwise prompt for a new backup passphrase on the controlling terminal",
        )
        parser.add_argument(
            "--min-passphrase-length",
            type=int,
            metavar="N",
            help="minimum characters for a new backup passphrase (8..1024; default: host config or 8); incompatible with --recipient",
        )
    else:
        parser.add_argument(
            "file", type=Path, metavar="FILE", help="encrypted backup archive"
        )
        parser.add_argument(
            "--identity",
            type=Path,
            action="append",
            metavar="FILE",
            help="private native age X25519 identity file (repeatable); otherwise age prompts for the backup passphrase on the controlling terminal",
        )
        if action == "backup-info":
            output = parser.add_mutually_exclusive_group()
            output.add_argument(
                "--table",
                action="store_true",
                help="show a human-readable metadata table",
            )
            output.add_argument(
                "--json", action="store_true", help="show JSON metadata (default)"
            )
        else:
            parser.add_argument(
                "--project",
                type=Path,
                metavar="PATH",
                help="restore the workspace to a nonexistent host project path with an existing parent; required unless using --host",
            )
            parser.add_argument(
                "--host",
                action="store_true",
                help="restore oc-box host settings and the encrypted vault if present; omit --project to restore only the host component",
            )
            parser.add_argument(
                "--include-opencode",
                action="store_true",
                help="with --host, also restore native OpenCode settings and saved credentials when present, after separate typed confirmation on the controlling terminal",
            )
    return parser


def cli(
    box,
    arguments,
    *,
    interaction=None,
    reviewed_auth_export=None,
    reviewed_paths=None,
    reviewed_workspace=None,
):
    action = arguments[0]
    parser = parser_for(action)
    args = parser.parse_args(arguments[1:])
    if reviewed_paths is not None:
        args._reviewed_paths = reviewed_paths
    if action == "restore" and not (args.project or args.host):
        parser.error("restore requires --project or --host")
    if getattr(args, "opencode_auth_export", None) is not None and (
        interaction is not None or reviewed_auth_export is not None
    ):
        if args.opencode_auth_export == "-" or reviewed_auth_export is None:
            raise BackupError(
                "Select a reviewed private credential export file in the dashboard."
            )
        args._reviewed_auth_export = reviewed_auth_export
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
        crypto.secret_prompt.cancel_operation()

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, cancel)
        try:
            if action == "backup":
                if interaction is None and reviewed_workspace is None:
                    make_backup(box, args)
                else:
                    make_backup(
                        box,
                        args,
                        interaction=interaction,
                        reviewed_workspace=reviewed_workspace,
                    )
            elif action == "backup-info":
                if interaction is None:
                    inspect_backup(box, args)
                else:
                    inspect_backup(box, args, interaction=interaction)
            else:
                if interaction is None:
                    restore_backup(box, args)
                else:
                    restore_backup(box, args, interaction=interaction)
        except (KeyError, TypeError, IndexError, UnicodeError) as exc:
            raise BackupError(
                "Invalid backup metadata or resource response; no existing workspace was overwritten."
            ) from exc
    finally:
        for sig, previous in handlers.items():
            signal.signal(sig, previous)
    return 0
