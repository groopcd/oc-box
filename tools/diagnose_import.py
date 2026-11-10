#!/usr/bin/env python3
"""Compare explicitly supplied session exports offline without printing payloads.

Offline mode only reads --source-export and --container-export. It does not
inspect/start/stop an engine or invoke OpenCode, migration, import or history
synchronization. The source directory is the expected container directory.

--live instead reads the workspace's saved import and uses the production
container exporter under its workspace lock. It inspects the container, starts
it if stopped and attempts to stop it again, including after a partial start.
An already-running container is left running. Native export can initialize or
migrate application state and use/start/stop a private native history service.
It does not request oc-box credential unlock/refresh, session import or
host-history synchronization. Running-state restoration is best effort, not
data rollback.

Exit 0 means import verification matches (including the existing narrow obsolete
tool raw-buffer omission); 1 means mismatch; 2 means operational/options failure.
Interruption exits 130. Reports retain exact-match and positional differences.
"""

import argparse
import contextlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from oc_box import oc_box as box

MAX_DIFFERENCES = 20
MAX_DEPTH = 32


class InvalidOptions(ValueError):
    pass


OPTIONS_GUIDANCE = (
    "Use --source-export FILE --container-export FILE --session ID for offline "
    "comparison; use --live --workspace ID --session ID for production export."
)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes untrusted options and argument values.
        raise InvalidOptions()


def differences(expected, actual, path="", limit=20):
    """Use only /field/N and /item/N paths, never transcript dictionary keys.

    Positions refer to sorted dictionary keys or list indices. Reports contain
    at most 20 differences and traverse at most 32 levels of plain JSON data.
    Caller-supplied path prefixes are refused rather than copied to output.
    """
    if type(path) is not str or path != "" or type(limit) is not int or limit < 0:
        raise ValueError("Invalid comparison options")
    limit = min(limit, MAX_DIFFERENCES)
    if not limit:
        return []

    def validate(value, depth=0):
        if depth > MAX_DEPTH:
            raise ValueError("Invalid comparison data")
        kind = type(value)
        if kind is dict:
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError("Invalid comparison data")
                validate(child, depth + 1)
        elif kind is list:
            for child in value:
                validate(child, depth + 1)
        elif kind is float and not math.isfinite(value):
            raise ValueError("Invalid comparison data")
        elif kind not in (str, int, float, bool, type(None)):
            # Do not expose user-defined type names or call their repr/equality.
            raise ValueError("Invalid comparison data")

    validate(expected)
    validate(actual)
    result = []

    def walk(left, right, location):
        if len(result) >= limit:
            return
        if type(left) is not type(right):
            result.append(
                {
                    "path": location,
                    "difference": "type",
                    "source_type": type(left).__name__,
                    "container_type": type(right).__name__,
                }
            )
        elif left == right:
            return
        elif isinstance(left, dict):
            for number, key in enumerate(sorted(left.keys() | right.keys())):
                child = f"{location}/field/{number}"
                if key not in left:
                    result.append({"path": child, "difference": "container-only field"})
                elif key not in right:
                    result.append({"path": child, "difference": "source-only field"})
                else:
                    walk(left[key], right[key], child)
                if len(result) >= limit:
                    break
        elif isinstance(left, list):
            if len(left) != len(right):
                result.append(
                    {
                        "path": location,
                        "difference": "list length",
                        "source_count": len(left),
                        "container_count": len(right),
                    }
                )
            for number, (a, b) in enumerate(zip(left, right)):
                if len(result) >= limit:
                    break
                walk(a, b, f"{location}/item/{number}")
        else:
            # Do not print conversation text, commands, credentials, or values.
            result.append(
                {"path": location, "difference": "value", "type": type(left).__name__}
            )

    walk(expected, actual, path)
    return result


@contextlib.contextmanager
def workspace_lock(path, state):
    """Keep a lock finalizer from replacing this invocation's primary error."""
    lock = box.workspace_lock(path)
    lock.__enter__()
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            lock.__exit__(
                type(primary) if primary is not None else None,
                primary,
                primary.__traceback__ if primary is not None else None,
            )
        except BaseException as error:
            if primary is None:
                state["stage"] = "workspace-unlock"
                raise
            if error is not primary:
                state["secondary_failures"].append(
                    {"stage": "workspace-unlock", "category": error_category(error)}
                )


def live_export(path, data, session, state):
    state["stage"] = "workspace-lock"
    with workspace_lock(path, state):
        state["stage"] = "inspect"
        info = box.inspect_owned(data)
        if info is None:
            raise box.BoxError("Workspace container is missing")
        running = info["State"]["Running"]
        if type(running) is not bool:
            raise ValueError("Invalid container state")
        primary = None
        try:
            if not running:
                state["stage"] = "start"
                box.podman("start", data["container"], capture=True, private=True)
            state["stage"] = "export"
            actual = box.export_container_session(data, session)
            validate_export(actual, session)
            return actual
        except BaseException as error:
            primary = error
            raise
        finally:
            if not running:
                try:
                    box.stop_container(data)
                except BaseException as error:
                    if primary is None:
                        state["stage"] = "stop"
                        raise
                    if error is not primary:
                        state["secondary_failures"].append(
                            {"stage": "stop", "category": error_category(error)}
                        )


def validate_export(exported, session):
    # Validate all JSON, including metadata outside messages, before comparing.
    differences(exported, exported)
    box.validate_session_export(exported, session)
    if box.opencode_compat.native(exported):
        for message in exported["messages"]:
            if type(message.get("content", [])) is not list:
                raise ValueError("Invalid native message content")


def comparison_report(source, actual, data, workspace, session):
    validate_export(source, session)
    validate_export(actual, session)

    def message_map(exported):
        native = box.opencode_compat.native(exported)
        return {
            (message["id"] if native else message["info"]["id"]): message
            for message in exported["messages"]
        }

    def parts_count(exported):
        native = box.opencode_compat.native(exported)
        return sum(
            len(message.get("content", []) if native else message["parts"])
            for message in exported["messages"]
        )

    expected_messages = message_map(source)
    actual_messages = message_map(actual)
    # Use indices for differing messages so the output does not reveal contents.
    expected_list = [expected_messages[key] for key in sorted(expected_messages)]
    actual_list = [actual_messages[key] for key in sorted(actual_messages)]
    try:
        if box.opencode_compat.native(source) != box.opencode_compat.native(actual):
            raise box.BoxError("Export formats differ")
        omitted = box.verify_session_import(data, source, actual)
        verified = True
    except box.BoxError:
        omitted = None
        verified = False
    return {
        "workspace": workspace,
        "session": session,
        "directory_matches": box.opencode_compat.directory(actual)
        == box.container_project(data),
        "source_messages": len(source["messages"]),
        "container_messages": len(actual["messages"]),
        "source_parts": parts_count(source),
        "container_parts": parts_count(actual),
        "message_ids_match": expected_messages.keys() == actual_messages.keys(),
        "transcript_exact_match": expected_messages == actual_messages,
        "transcript_verified": verified,
        "obsolete_tool_raw_buffers_omitted": omitted,
        "first_differences": differences(expected_list, actual_list),
    }


def main(argv=None, *, state=None):
    if state is None:
        state = {"stage": "options", "secondary_failures": []}
    parser = ArgumentParser(
        prog="diagnose-import",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace")
    parser.add_argument("--session", required=True)
    parser.add_argument(
        "--source-export", help="saved expected native export (offline)"
    )
    parser.add_argument(
        "--container-export", help="saved observed native export (offline)"
    )
    parser.add_argument(
        "--live", action="store_true", help="opt in to production export effects"
    )
    args = parser.parse_args(argv)
    if not box.valid_session_id(args.session) or (
        args.workspace is not None and not re.fullmatch(r"[0-9a-f]{32}", args.workspace)
    ):
        raise InvalidOptions()
    if args.live:
        if not args.workspace or args.source_export or args.container_export:
            raise InvalidOptions()
        state["stage"] = "backend"
        box.check_backend()
        state["stage"] = "workspace"
        path, data = box.find_workspace(workspace_id=args.workspace)
        major = box.opencode_compat.major(data.get("opencode_version", "1.0.0"))
        backup = (
            path.parent
            / ("imports-v2" if major == 2 else "imports")
            / (args.session + ".json")
        )
        state["stage"] = "read-source"
        source = box.read_json(backup)
        state["stage"] = "validate-source"
        validate_export(source, args.session)
        actual = live_export(path, data, args.session, state)
    else:
        if args.workspace or not args.source_export or not args.container_export:
            raise InvalidOptions()
        state["stage"] = "read-source"
        source = json.loads(Path(args.source_export).read_text(encoding="utf-8"))
        state["stage"] = "read-container"
        actual = json.loads(Path(args.container_export).read_text(encoding="utf-8"))
        state["stage"] = "compare"
        differences(source, actual)
        directory = box.opencode_compat.directory(source)
        if type(directory) is not str:
            raise ValueError("Invalid source directory")
        data = {"project": directory}
    state["stage"] = "compare"
    report = comparison_report(source, actual, data, args.workspace, args.session)
    state["stage"] = "report"
    print(json.dumps(report, indent=2))
    return 0 if report["transcript_verified"] else 1


def error_category(error):
    """Only fixed categories may leave the diagnostic's exception boundary."""
    for kind, category in (
        (InvalidOptions, "invalid-options"),
        (KeyboardInterrupt, "interrupted"),
        (box.BoxError, "box-error"),
        (subprocess.TimeoutExpired, "timeout"),
        (subprocess.CalledProcessError, "subprocess-exit"),
        (OSError, "os-error"),
        (
            (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                IndexError,
                RecursionError,
            ),
            "invalid-data",
        ),
    ):
        if isinstance(error, kind):
            return category
    return "unexpected-error"


def cli(argv=None):
    state = {"stage": "options", "secondary_failures": []}
    try:
        return main(argv, state=state)
    except SystemExit as error:
        if state["stage"] == "options" and error.code == 0:
            raise
        category = "unexpected-error"
        code = 2
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - privacy boundary
        category = error_category(error)
        code = 130 if isinstance(error, KeyboardInterrupt) else 2
    report = {
        "primary_failure": {"stage": state["stage"], "category": category},
        "secondary_failures": state["secondary_failures"],
    }
    if category == "invalid-options":
        report["guidance"] = OPTIONS_GUIDANCE
    print(json.dumps(report, indent=2), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(cli())
