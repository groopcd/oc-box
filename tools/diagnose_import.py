#!/usr/bin/env python3
"""Compare a saved import and container transcript without printing their text."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oc_box", ROOT / "oc_box.py")
assert spec is not None and spec.loader is not None
box = importlib.util.module_from_spec(spec)
spec.loader.exec_module(box)


def differences(expected, actual, path="", limit=20):
    result = []

    def walk(left, right, location):
        if len(result) >= limit or left == right:
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
        elif isinstance(left, dict):
            for key in sorted(left.keys() | right.keys()):
                child = f"{location}/{key}"
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
                walk(a, b, f"{location}/{number}")
        else:
            # Do not print conversation text, commands, credentials, or values.
            result.append(
                {"path": location, "difference": "value", "type": type(left).__name__}
            )

    walk(expected, actual, path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    if not box.valid_session_id(args.session):
        raise box.BoxError("Invalid session ID")
    box.check_backend()
    path, data = box.find_workspace(workspace_id=args.workspace)
    backup = path.parent / "imports" / (args.session + ".json")
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
    expected_messages = {
        message["info"]["id"]: message for message in source["messages"]
    }
    actual_messages = {message["info"]["id"]: message for message in actual["messages"]}
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
        "directory_matches": actual["info"].get("directory") == data["project"],
        "source_messages": len(source["messages"]),
        "container_messages": len(actual["messages"]),
        "source_parts": sum(len(message["parts"]) for message in source["messages"]),
        "container_parts": sum(len(message["parts"]) for message in actual["messages"]),
        "message_ids_match": expected_messages.keys() == actual_messages.keys(),
        "transcript_exact_match": expected_messages == actual_messages,
        "transcript_verified": verified,
        "obsolete_tool_raw_buffers_omitted": omitted,
        "first_differences": differences(expected_list, actual_list),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (box.BoxError, OSError, ValueError) as exc:
        print(f"diagnose-import: {exc}", file=sys.stderr)
        sys.exit(1)
