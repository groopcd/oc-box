"""Read the private activity journal, without lifecycle or native log access."""

import argparse
import json
import os
import re
import stat
import sys
import time
from contextlib import ExitStack
from pathlib import Path

if __package__:
    from . import activity_log, cli_table, log_config
else:
    import activity_log
    import cli_table
    import log_config


_DEFAULT_LIMIT = 100
_MAX_LIMIT = 10_000
_POLL_SECONDS = 0.5
_WORKSPACE_ID = re.compile(r"[0-9a-f]{32}")
_REGISTRATION_BYTES = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _limit(value):
    try:
        number = int(value)
        if 1 <= number <= _MAX_LIMIT:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(f"must be an integer from 1 to {_MAX_LIMIT}")


def parser():
    result = argparse.ArgumentParser(
        prog="oc-box logs",
        description="Read the private host activity journal (not native or container logs).",
        epilog=(
            "Use 'oc-box logs --path [--log-dir PATH]' to show just the private log location. "
            "Follow may miss records that rotate out between polls or are indistinguishable from old records after rotation."
        ),
        allow_abbrev=False,
    )
    selectors = result.add_mutually_exclusive_group()
    selectors.add_argument(
        "--project", metavar="PATH", help="filter by a registered project directory"
    )
    selectors.add_argument(
        "--workspace", metavar="ID", help="filter by a registered workspace ID"
    )
    level = result.add_mutually_exclusive_group()
    level.add_argument(
        "--level",
        choices=activity_log.LEVELS,
        help="minimum severity (default: info, or all levels with --tail)",
    )
    log_config.add_verbosity(
        level, help_text="show validated activity at this level or higher"
    )
    result.add_argument(
        "--limit",
        type=_limit,
        metavar="N",
        help=f"maximum initial records, 1..{_MAX_LIMIT} (default: {_DEFAULT_LIMIT}, or {_MAX_LIMIT} with --verbose=all)",
    )
    result.add_argument(
        "-f", "--follow", action="store_true", help="keep printing new retained records"
    )
    result.add_argument(
        "--tail",
        action="store_true",
        help="show recent environment preparation tasks and results (add -f to watch live)",
    )
    formats = result.add_mutually_exclusive_group()
    formats.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON object per line; nothing if no records match",
    )
    formats.add_argument(
        "--table",
        action="store_true",
        help="show matching activity in a human-readable table; also works with --follow",
    )
    source = result.add_mutually_exclusive_group()
    source.add_argument(
        "--journald",
        action="store_true",
        help="read this user's mirrored oc-box entries from the local system journal; no private fallback",
    )
    source.add_argument(
        "--path",
        action="store_true",
        help="show the selected private journal path without reading it",
    )
    result.add_argument(
        "--log-dir",
        type=log_config.directory_argument,
        metavar="PATH",
        help="override the private journal directory for this read (not with --journald)",
    )
    return result


def _owner_controlled(info):
    return info.st_uid == os.getuid() and not info.st_mode & 0o022


def _bounded_read(directory, name, maximum, *, private=False):
    def valid(info):
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_size <= maximum
            and (not private or (_owner_controlled(info) and info.st_nlink == 1))
        )

    if not valid(os.stat(name, dir_fd=directory, follow_symlinks=False)):
        raise ValueError
    with os.fdopen(os.open(name, _READ_FLAGS, dir_fd=directory), "rb") as stream:
        if not valid(os.fstat(stream.fileno())):
            raise ValueError
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError
    return data


def _open_registry_root(root):
    # The journal's raw configured state path must not be resolved. Open every
    # ancestor from / without following links, keeping only the current fd.
    directory = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in root.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
            os.close(directory)
            directory = child
    except BaseException:
        os.close(directory)
        raise
    return directory


def _registration(root, workspace):
    def unique_keys(pairs):
        record = dict(pairs)
        if len(record) != len(pairs):
            raise ValueError
        return record

    with ExitStack() as stack:
        directory = _open_registry_root(root)
        stack.callback(os.close, directory)
        if not _owner_controlled(os.fstat(directory)):
            raise ValueError
        for component in ("workspaces", workspace):
            directory = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
            if not _owner_controlled(os.fstat(directory)):
                raise ValueError
        return json.loads(
            _bounded_read(
                directory, "workspace.json", _REGISTRATION_BYTES, private=True
            ),
            object_pairs_hook=unique_keys,
        )


def _journal_scope(box, args):
    if args.project is None and args.workspace is None:
        # With no workspace registry to check, use the normal journal reader's
        # raw state resolution and fail-closed behavior directly.
        return None, None
    try:
        with ExitStack() as stack:
            root = activity_log._root(None)
            project = None
            workspace = args.workspace
            if args.project is not None:
                if not args.project:
                    raise ValueError
                project = Path(args.project).expanduser().resolve(strict=True)
                directory = os.open(project, _DIRECTORY_FLAGS)
                stack.callback(os.close, directory)
                workspace = (
                    _bounded_read(directory, box.MARKER, 128).decode("ascii").strip()
                )
            if not _WORKSPACE_ID.fullmatch(workspace):
                raise ValueError
            record = _registration(root, workspace)
            if not isinstance(record, dict) or record.get("id") != workspace:
                raise ValueError
            registered = record.get("project")
            if not isinstance(registered, str) or "\0" in registered:
                raise ValueError
            registered_path = Path(registered)
            if (
                registered_path.anchor != "/"
                or str(registered_path) != registered
                or ".." in registered_path.parts
            ):
                raise ValueError
            if project is not None:
                if __package__:
                    from . import workspace_identity
                else:
                    import workspace_identity
                identity, stable = workspace_identity.snapshot_fd(directory)
                if project != registered_path or not workspace_identity.matches(
                    record, identity, stable
                ):
                    raise ValueError
                # The marker and identity both came from the pinned directory.
                # Refuse if its canonical entry now names something else.
                entry = project.lstat()
                if not stat.S_ISDIR(entry.st_mode) or identity != [
                    entry.st_dev,
                    entry.st_ino,
                ]:
                    raise ValueError
            return root, workspace
    except (OSError, ValueError, RuntimeError):
        # Never echo selector values, filesystem exceptions, or registry contents.
        raise box.BoxError(
            "The logs selector requires an existing valid workspace registration."
        ) from None


def _new_records(previous, current):
    """Find the longest suffix/prefix overlap in linear time.

    Compare whole records in order, not timestamps or sets: repeated identical
    records within a retained overlap can still be new. A snapshot entirely
    contained in the previous one may be a temporary loss of readable files;
    do not replay it. Events already rotated out (or indistinguishable from an
    old overlap after rotation) cannot be recovered by a read-only follower.
    """
    if not previous or not current:
        return current
    if previous == current:
        return []
    failure = [0] * len(current)
    matched = 0
    for index in range(1, len(current)):
        while matched and current[matched] != current[index]:
            matched = failure[matched - 1]
        if current[matched] == current[index]:
            matched += 1
        failure[index] = matched

    matched = 0
    for record in previous:
        while matched and current[matched] != record:
            matched = failure[matched - 1]
        if current[matched] == record:
            matched += 1
            if matched == len(current):
                return []
    return current[matched:]


def _preparation_text(record):
    task = activity_log.PREPARATION_TASKS[record["stage"]]
    state = record.get("outcome", "started")
    code = record.get("exit_code")
    elapsed = record.get("duration_ms")
    duration = f" ({elapsed / 1000:.1f}s)" if elapsed is not None else ""
    detail = (
        " (timed out)"
        if record.get("reason") == "timeout"
        else f" (exit {code})"
        if code
        else ""
    )
    return f"{record['command']}: {task} — {state}{detail}{duration}"


def _emit(records, *, json_lines, table=False, heading=True, preparation_only=False):
    if not records:
        return
    format_record = _preparation_text if preparation_only else activity_log.format_event
    if table:
        rows = (
            (
                entry["timestamp"],
                activity_log.event_level(entry).upper(),
                format_record(entry),
            )
            for entry in records
        )
        print(
            cli_table.render(
                ("TIMESTAMP", "LEVEL", "ACTIVITY"),
                rows,
                widths=(24, 5),
                heading=heading,
            ),
            flush=True,
        )
        return
    for record in records:
        level = activity_log.event_level(record)
        if json_lines:
            entry = {**record, "level": level}
            print(json.dumps(entry, separators=(",", ":"), ensure_ascii=True))
        elif preparation_only:
            print(f"{record['timestamp']} {format_record(record)}")
        else:
            print(f"{record['timestamp']} {level.upper():5} {format_record(record)}")
    sys.stdout.flush()


def _silence_broken_pipe():
    # A failed buffered flush can be retried at interpreter shutdown. Replace
    # the broken destination so that retry does not print an ignored exception.
    try:
        destination = sys.stdout.fileno()
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(fd, destination)
        finally:
            os.close(fd)
    except (OSError, AttributeError, ValueError):
        pass


def _read_events(
    box, root, *, journald, follow, limit, level, workspace, preparation_only=False
):
    if not journald:
        options = {"limit": limit, "level": level, "workspace": workspace}
        if follow:
            options["strict_snapshot"] = True
        if preparation_only:
            options["preparation_only"] = True
        return activity_log.read_events(root, **options)
    if __package__:
        from . import journal_output
    else:
        import journal_output
    try:
        return journal_output.read_events(limit=limit, level=level, workspace=workspace)
    except journal_output.JournalError:
        raise box.BoxError(
            "The local system journal is unavailable. Use 'oc-box logs' to read the private activity journal."
        ) from None


def _configured_path(box, root=None):
    try:
        return activity_log.log_path(root)
    except (OSError, ValueError, RuntimeError):
        raise box.BoxError(
            "The private activity-log configuration is invalid or unavailable. Check the host oc-box logs settings."
        ) from None


def cli(box, arguments):
    try:
        try:
            options = parser()
            args = options.parse_args(arguments)
            if args.journald and args.log_dir is not None:
                options.error("--log-dir cannot be combined with --journald")
            if args.tail and (args.journald or args.path):
                options.error("--tail cannot be combined with --journald or --path")
        except SystemExit as error:
            if error.code == 0:
                sys.stdout.flush()
            raise
        if os.environ.get("OC_BOX_CONTAINER") == "1":
            raise box.BoxError("Run 'oc-box logs' on the host, not inside a container.")
        with log_config.overrides(directory=args.log_dir):
            if args.path:
                if (
                    args.project is not None
                    or args.workspace is not None
                    or args.follow
                    or args.json
                    or args.table
                    or args.verbose is not None
                ):
                    raise box.BoxError(
                        "'oc-box logs --path' does not take event filters or output modes."
                    )
                path = str(_configured_path(box))
                print(path if path.isprintable() else json.dumps(path), flush=True)
                return 0
            root, workspace = _journal_scope(box, args)
            if not args.journald:
                _configured_path(box, root)
            all_levels = args.verbose == "all"
            initial_limit = args.limit or (_MAX_LIMIT if all_levels else _DEFAULT_LIMIT)
            selectors = {
                "level": None if all_levels else args.verbose or args.level,
                "workspace": workspace,
                "journald": args.journald,
                "follow": args.follow,
            }
            if args.tail:
                # Preparation start/success records are debug. In this
                # focused view they're visible unless an explicit filter
                # excludes them.
                selectors["preparation_only"] = True
            elif selectors["level"] is None and not all_levels:
                selectors["level"] = "info"
            limit = _MAX_LIMIT if args.follow else initial_limit
            previous = _read_events(box, root, limit=limit, **selectors)
            display = dict(
                json_lines=args.json, table=args.table, preparation_only=args.tail
            )
            _emit(previous[-initial_limit:], **display)
            heading = not previous
            while args.follow:
                time.sleep(_POLL_SECONDS)
                current = _read_events(box, root, limit=limit, **selectors)
                if not current:
                    # A busy or incomplete snapshot is not a new follow baseline.
                    continue
                new = _new_records(previous, current)
                if new:
                    _emit(new, heading=heading, **display)
                    heading = False
                    previous = current
            return 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        _silence_broken_pipe()
        return 0
