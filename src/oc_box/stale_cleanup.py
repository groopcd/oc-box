"""Review and retire workspaces whose original project pathname is gone."""

import contextlib
from dataclasses import dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import re
import sys

if __package__:
    from . import (
        backups,
        entrypoint_migration,
        guarded_discard as disposal,
        runtime_safeguard,
        uninstall_tty,
        workspace_control,
        workspace_identity,
        workspace_podman,
        workspace_preferences,
        workspace_service,
        workspace_uninstall as registry,
    )
else:
    import backups
    import entrypoint_migration
    import guarded_discard as disposal
    import runtime_safeguard
    import uninstall_tty
    import workspace_control
    import workspace_identity
    import workspace_podman
    import workspace_preferences
    import workspace_service
    import workspace_uninstall as registry


_ID = re.compile(r"[a-f0-9]{32}")
_PATH_DIR = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PHRASE = "CLEANUP"


def _require(condition, message):
    if not condition:
        raise disposal.Refused(message)


def _ids(root):
    try:
        with workspace_podman._path_fd(
            str(root / "workspaces"), os.geteuid(), directory=True
        ) as (pinned, info):
            _require(
                info.st_uid == os.geteuid(),
                "The active registry is not owned by this user.",
            )
            fd = os.open(
                ".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=pinned
            )
            try:
                with os.scandir(fd) as entries:
                    found = []
                    for entry in entries:
                        _require(
                            len(found) < registry.MAX_WORKSPACES,
                            "The active registry exceeds the cleanup review limit.",
                        )
                        _require(
                            _ID.fullmatch(entry.name),
                            "An unexpected entry prevents a complete registry review.",
                        )
                        found.append(entry.name)
                    return sorted(found)
            finally:
                os.close(fd)
    except FileNotFoundError:
        return []


def _missing_project(data):
    """Only leaf ENOENT on the recorded filesystem is a cleanup candidate."""
    project = Path(data["project"])
    fd = os.open("/", _PATH_DIR)
    try:
        try:
            for name in project.parts[1:-1]:
                child = os.open(name, _PATH_DIR, dir_fd=fd)
                os.close(fd)
                fd = child
        except OSError:
            raise disposal.Refused(
                "The original project parent is missing or inaccessible."
            ) from None
        try:
            os.stat(project.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            info = os.fstat(fd)
            _require(
                info.st_dev == data["project_identity"][0],
                "The original project filesystem could not be verified.",
            )
            stable = data.get(workspace_identity.STABLE_IDENTITY_KEY)
            if stable is not None:
                fsid = os.fstatvfs(fd).f_fsid
                _require(
                    fsid != 0 and format(fsid, "x") == stable["fsid"],
                    "The original project filesystem could not be verified.",
                )
            return info.st_dev, info.st_ino, registry._mount_id(fd)
        return None  # Any existing object, including a dangling symlink.
    finally:
        os.close(fd)


@contextlib.contextmanager
def _admission(path):
    control = path.parent / "control"
    if disposal._absent(control):

        def absent():
            _require(
                disposal._absent(control),
                "Workspace stop intent appeared during cleanup.",
            )
            return None

        yield absent
    else:
        with workspace_control.disposal_admission(path, timeout=0) as observe:
            yield observe


@contextlib.contextmanager
def _retirement(root, registration, *, create=False):
    """Pin both same-mount parent directories; never follow or replace retired data."""
    ident = registration.workspace_id
    with contextlib.ExitStack() as stack:
        state, state_info = stack.enter_context(
            workspace_podman._path_fd(str(root), os.geteuid(), directory=True)
        )
        _require(
            state_info.st_uid == os.geteuid(),
            "The private registry is not owned by this user.",
        )
        mount = registry._mount_id(state)
        active, active_stamp = stack.enter_context(
            registry._child_directory(state, "workspaces", mount, owned=True)
        )
        if create:
            try:
                os.mkdir("retired", mode=0o700, dir_fd=state)
            except FileExistsError:
                pass
        try:
            retired, retired_stamp = stack.enter_context(
                registry._child_directory(state, "retired", mount, owned=True)
            )
        except FileNotFoundError:
            if not create:
                yield None
                return
            raise

        def check():
            with workspace_podman._path_fd(str(root), os.geteuid(), directory=True) as (
                now,
                info,
            ):
                _require(
                    (info.st_dev, info.st_ino, registry._mount_id(now))
                    == (state_info.st_dev, state_info.st_ino, mount),
                    "The private state directory changed during cleanup.",
                )
            for name, expected in (
                ("workspaces", active_stamp),
                ("retired", retired_stamp),
            ):
                with registry._child_directory(state, name, mount, owned=True) as (
                    _,
                    current,
                ):
                    _require(
                        current == expected,
                        "The registry parents changed during cleanup.",
                    )
            # Plain purge could create an owned 0755 retired/ directory with
            # private children. Reuse it without widening the moved leaf.
            _require(
                not retired_stamp[4] & 0o077
                or not registration.directory_stamp[4] & 0o077,
                "The retired registry would expose non-private workspace data.",
            )
            try:
                os.stat(ident, dir_fd=retired, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise disposal.Refused(
                "Retired evidence for this workspace already exists."
            )

        def retire():
            check()
            source = root / "workspaces" / ident
            target = root / "retired" / ident
            backups.activate_project(
                source, target, parent_fd=active, destination_fd=retired
            )
            _require(
                registry._read_record(retired, ident, mount, str(root)) == registration,
                "The archived registration could not be verified.",
            )
            _require(
                disposal._absent(source),
                "The old active registration did not disappear.",
            )
            os.fsync(active)
            os.fsync(retired)
            # The retired/ directory may have just been created.
            with registry._child_directory(state, ".", mount, owned=True) as (
                directory,
                _,
            ):
                os.fsync(directory)

        check()
        yield check, retire


@contextlib.contextmanager
def _locks(root, registration):
    path = root / "workspaces" / registration.workspace_id / "workspace.json"
    with (
        disposal._workspace_lock(path, registration) as check_lock,
        _admission(path) as observe,
    ):
        # Also exclude a native history writer that took the stable directory
        # lock. No write-intent, session or archive file is opened here.
        with workspace_control._workspace_directory(path) as directory:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise disposal.Refused(
                    "A history writer still owns this workspace."
                ) from None
            _require(
                registry._directory_stamp(directory, owned=True)
                == registration.directory_stamp,
                "The workspace registration changed.",
            )
            yield path, check_lock, observe


@dataclass(frozen=True)
class Plan:
    registration: registry.Registration
    missing: tuple
    guard: object
    stop: tuple | None
    resources: disposal.Resources


def _stale_bind(missing_records, row, item):
    labels = row["Config"].get("Labels") or {}
    data = missing_records.get(labels.get("io.oc-box.workspace"))
    return (
        data is not None
        and labels.get("io.oc-box.managed") == "true"
        and row["Name"] == data["container"]
        and item["Source"] == data["project"]
        and item["Destination"] == data.get("container_project", data["project"])
        and entrypoint_migration._stopped(
            row, ("created", "configured", "exited", "stopped")
        )
        and _missing_project(data) is not None
    )


def _plan(box, root, engine, expected, missing_records, locks, *, reviewed=None):
    ident = expected.workspace_id
    path, check_lock, observe = locks
    check_lock()
    registration, data = registry.read_discard_registration(root, ident)
    _require(registration == expected, "The registered workspace changed.")
    missing = _missing_project(data)
    _require(missing is not None, "The project pathname now exists.")
    with _retirement(root, registration):
        pass
    stop = observe()
    guard = runtime_safeguard.load(box, path)
    _require(
        guard is None or guard.volume == registration.home_volume,
        "The historical guarded runtime record does not match the registration.",
    )
    receipt = data.get(entrypoint_migration.RECEIPT)
    container_id = guard.container if guard else None
    if entrypoint_migration.RECEIPT in data:
        _require(
            type(receipt) is dict
            and entrypoint_migration.replacement_matches(
                data, {"Id": receipt.get("id")}
            )
            and receipt["bootstrap"] is False
            and (container_id is None or container_id == receipt["id"]),
            "The original container replacement is not settled.",
        )
        container_id = receipt["id"]
    if reviewed and reviewed.resources.container:
        container_id = reviewed.resources.container[0]
    _require(
        not workspace_service.controller_running(box, data, create_runtime=False),
        "A workspace controller is still present. Exit it before cleanup.",
    )
    _require(
        not engine.labeled_networks(ident),
        "The workspace has additional labeled networks; inspect it separately.",
    )
    resources = disposal._review_resources(
        box,
        registration,
        data,
        engine,
        container_id,
        reviewed=reviewed.resources if reviewed else None,
        stale=True,
        missing_bind=lambda row, item: _stale_bind(missing_records, row, item),
    )
    _require(
        (guard is None and (stop is None or not stop[1]))
        or (resources.container is None and resources.volume is None),
        "A guarded or uncertain workspace still has Podman resources; inspect it separately.",
    )
    _require(
        not engine.labeled_networks(ident)
        and registry.read_discard_registration(root, ident) == (registration, data)
        and runtime_safeguard.load(box, path) == guard
        and _missing_project(data) == missing
        and observe() == stop,
        "The reviewed workspace or stop intent changed.",
    )
    check_lock()
    return Plan(registration, missing, guard, stop, resources)


def _review(plans):
    lines = ["Clean up the following registrations with missing project directories:"]
    for plan in plans:
        item = plan.registration
        resources = plan.resources
        lines += [
            f"{item.workspace_id}  {json.dumps(item.project)}",
            f"  Stopped container: {resources.container[0] if resources.container else 'absent in this store'}",
            f"  Private home: {resources.volume[0] if resources.volume else 'absent in this store'}",
            "  Archive the active registration and existing recovery files intact.",
        ]
    return "\n".join(
        lines
        + [
            "No OpenCode history sync or deletion will run. Unsynced conversations in removed volumes will be lost.",
            "Do not change these resources with Podman or filesystem commands during cleanup.",
            f"To proceed, type {_PHRASE}",
        ]
    )


def _errors(box):
    return (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        OSError,
        box.BoxError,
    )


def _skip(ident, exc):
    detail = (
        str(exc)
        if isinstance(exc, disposal.Refused)
        else "Ownership or state could not be verified."
    )
    print(f"oc-box: {ident}: skipped. {detail}", file=sys.stderr)


def _run_plans(box, root, engine, plans, records):
    completed = skipped = 0
    for reviewed in plans:
        registration = reviewed.registration
        ident = registration.workspace_id
        attempted = False
        try:
            with _locks(root, registration) as locks:

                def check():
                    return _plan(
                        box,
                        root,
                        engine,
                        registration,
                        records,
                        locks,
                        reviewed=reviewed,
                    )

                current = check()
                # Earlier workspace removals legitimately change the batch
                # inventory. Compare this subject, not a prior bystander list.
                _require(
                    replace(
                        current,
                        resources=replace(
                            current.resources,
                            other_resources=reviewed.resources.other_resources,
                        ),
                    )
                    == reviewed,
                    "The confirmed workspace resources changed.",
                )
                with _retirement(root, registration, create=True) as (
                    check_target,
                    retire,
                ):
                    for kind in ("container", "volume"):
                        target = getattr(current.resources, kind)
                        if target is None:
                            continue
                        _require(
                            check() == current, "The reviewed cleanup scope changed."
                        )
                        check_target()
                        attempted = True
                        engine.remove(kind, target[0])
                        current = replace(
                            current,
                            resources=replace(current.resources, **{kind: None}),
                        )
                        _require(
                            check() == current,
                            "Resource absence could not be confirmed.",
                        )
                    path, check_lock, observe = locks

                    def retire_checked():
                        nonlocal attempted
                        check_lock()
                        latest, data = registry.read_discard_registration(root, ident)
                        _require(
                            latest == registration
                            and _missing_project(data) == current.missing
                            and runtime_safeguard.load(box, path) == current.guard
                            and observe() == current.stop
                            and not workspace_service.controller_running(
                                box, data, create_runtime=False
                            ),
                            "The workspace changed before retirement.",
                        )
                        attempted = True
                        retire()

                    with (
                        workspace_preferences._directory(path, ident) as preferences,
                        workspace_preferences._locked(preferences),
                        workspace_control.retirement_gate(path, timeout=0),
                    ):
                        # Do not reuse the earlier result after preparing
                        # retired/ or waiting for the independent writers.
                        changed = check() != current
                        if not changed:
                            retire_checked()
                    _require(not changed, "Podman state changed before retirement.")
                completed += 1
                print(f"{ident}: retired stale workspace; host archives kept.")
        except KeyboardInterrupt:
            if attempted:
                print(
                    "oc-box: Cleanup was interrupted after a removal or retirement request; inspect what remains.",
                    file=sys.stderr,
                )
            raise
        except _errors(box) as exc:
            if attempted:
                raise box.BoxError(
                    f"Cleanup stopped at {ident} after an attempted removal or retirement. "
                    "The result may be partial; inspect what remains before retrying."
                ) from None
            _skip(ident, exc)
            skipped += 1
    return completed, skipped


def cleanup(box):
    try:
        disposal._require_host()
        root = box._state_path().absolute()
        records, candidates, skipped = {}, [], 0
        for ident in _ids(root):
            try:
                registration, data = registry.read_discard_registration(root, ident)
                if _missing_project(data) is not None:
                    records[ident] = data
                    candidates.append(registration)
            except _errors(box) as exc:
                _skip(ident, exc)
                skipped += 1
        if not candidates:
            print("No verified missing project directories to clean up.")
            return int(bool(skipped))
        with disposal.Engine(box) as engine:
            plans = []
            for registration in candidates:
                try:
                    with _locks(root, registration) as locks:
                        plans.append(
                            _plan(box, root, engine, registration, records, locks)
                        )
                except _errors(box) as exc:
                    _skip(registration.workspace_id, exc)
                    skipped += 1
            if not plans:
                print("No stale workspaces could be verified for cleanup.")
                return 1
            if not uninstall_tty.confirm_review(_review(plans), _PHRASE):
                raise disposal.Refused(
                    "Cleanup was not confirmed on the controlling terminal."
                )
            completed, failed = _run_plans(box, root, engine, plans, records)
        print(f"Cleanup complete: {completed} retired, {skipped + failed} skipped.")
        return int(bool(skipped or failed))
    except (box.BoxError, disposal.Refused) as exc:
        raise box.BoxError(str(exc)) from None
    except _errors(box):
        raise box.BoxError(
            "Could not verify the local cleanup scope; no further removals were attempted."
        ) from None
