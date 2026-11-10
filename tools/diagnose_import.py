#!/usr/bin/env python3
"""Compare a saved import and container transcript without printing their text."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from oc_box import oc_box as box

MAX_DIFFERENCES = 20
MAX_DEPTH = 32


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes untrusted options and argument values.
        self.exit(2, "diagnose-import: category=invalid-options\n")


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


def main(argv=None):
    parser = ArgumentParser(prog="diagnose-import", description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{32}", args.workspace) or not box.valid_session_id(
        args.session
    ):
        raise box.BoxError("Invalid workspace or session ID")
    box.check_backend()
    path, data = box.find_workspace(workspace_id=args.workspace)
    major = box.opencode_compat.major(data.get("opencode_version", "1.0.0"))
    backup = (
        path.parent
        / ("imports-v2" if major == 2 else "imports")
        / (args.session + ".json")
    )
    source = box.read_json(backup)
    box.validate_session_export(source, args.session)
    with box.workspace_lock(path):
        info = box.inspect_owned(data)
        if info is None:
            raise box.BoxError("Workspace container is missing")
        running = info["State"]["Running"]
        try:
            if not running:
                box.podman("start", data["container"], capture=True)
            actual = box.export_container_session(data, args.session)
        finally:
            if not running:
                box.stop_container(data)
    box.validate_session_export(actual, args.session)
    native = box.opencode_compat.native(source)
    expected_messages = {
        (message["id"] if native else message["info"]["id"]): message
        for message in source["messages"]
    }
    actual_messages = {
        (message["id"] if native else message["info"]["id"]): message
        for message in actual["messages"]
    }
    # Use indices for differing messages so the output does not reveal contents.
    expected_list = [expected_messages[key] for key in sorted(expected_messages)]
    actual_list = [actual_messages[key] for key in sorted(actual_messages)]
    try:
        omitted = box.verify_session_import(data, source, actual)
        verified = True
    except box.BoxError:
        omitted = None
        verified = False
    report = {
        "workspace": args.workspace,
        "session": args.session,
        "directory_matches": box.opencode_compat.directory(actual)
        == box.container_project(data),
        "source_messages": len(source["messages"]),
        "container_messages": len(actual["messages"]),
        "source_parts": sum(
            len(message.get("content", []) if native else message["parts"])
            for message in source["messages"]
        ),
        "container_parts": sum(
            len(message.get("content", []) if native else message["parts"])
            for message in actual["messages"]
        ),
        "message_ids_match": expected_messages.keys() == actual_messages.keys(),
        "transcript_exact_match": expected_messages == actual_messages,
        "transcript_verified": verified,
        "obsolete_tool_raw_buffers_omitted": omitted,
        "first_differences": differences(expected_list, actual_list),
    }
    print(json.dumps(report, indent=2))


def error_category(error):
    """Only fixed categories may leave the diagnostic's exception boundary."""
    for kind, category in (
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
    try:
        main(argv)
    except SystemExit as error:
        if error.code == 2:  # ArgumentParser already emitted its fixed category.
            return 2
        if error.code == 0:  # Preserve argparse's harmless help exit.
            raise
        print("diagnose-import: category=unexpected-error", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("diagnose-import: category=interrupted", file=sys.stderr)
        return 130
    except Exception as error:
        print("diagnose-import: category=" + error_category(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
