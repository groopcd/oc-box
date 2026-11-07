"""Version-specific contracts for unmodified OpenCode 1.x and 2.x."""

import hashlib
import json
import re


def version(text):
    match = re.fullmatch(
        r"(?:opencode\s+)?v?([12]\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?)", text.strip()
    )
    if not match:
        raise ValueError(
            "Unsupported OpenCode version; supported release series are 1.x and 2.x."
        )
    return match[1]


def major(release):
    return int(version(release).split(".", 1)[0])


def command(release, executable, arguments):
    args = list(arguments)
    if major(release) == 1:
        return [executable, *args]
    args = [arg for arg in args if arg != "--pure"]
    if args and args[0] in ("export", "import"):
        args.insert(0, "session")
    if args[:2] == ["session", "list"] and "--max-count" not in args:
        args.extend(["--max-count", "2147483647"])
    if "--standalone" not in args:
        args.append("--standalone")
    return [executable, *args]


def tui(release, executable, project, session=None, model=None):
    args = [str(project)]
    if session:
        args.extend(["--session", session])
    if not model or major(release) == 1:
        if model:
            args.extend(["--model", model])
        return command(release, executable, args)
    # V2's full TUI has no --model flag. A launch-local config contribution
    # selects new sessions; callers update an existing session through the API.
    bridge = """import json,os,sys
content=json.loads(os.environ.get('OPENCODE_CONFIG_CONTENT','{}')); content['model']=sys.argv[1]
os.environ['OPENCODE_CONFIG_CONTENT']=json.dumps(content)
os.execvpe(sys.argv[2],sys.argv[2:],os.environ)
"""
    return ["python3", "-c", bridge, model, *command(release, executable, args)]


def model(value):
    match = re.fullmatch(r"([^/#]+)/([^#]+)(?:#([^#]+))?", value)
    if not match:
        raise ValueError(
            "Model must use provider/model or provider/model#variant syntax."
        )
    result = {"providerID": match[1], "id": match[2]}
    if match[3]:
        result["variant"] = match[3]
    return result


def legacy_credentials(content):
    """Translate the supported V1 auth override into native V2 credential input."""
    values = json.loads(content)
    if not isinstance(values, dict):
        raise ValueError("Invalid legacy OpenCode credential object.")
    result = []
    for integration, entry in values.items():
        if not isinstance(entry, dict):
            raise ValueError("Invalid legacy OpenCode credential entry.")
        kind = entry.get("type")
        if kind == "api":
            credential = {"type": "key", "key": entry["key"]}
        elif kind == "wellknown":
            credential = {"type": "key", "key": entry["token"]}
        elif kind == "oauth":
            method = (
                "chatgpt-browser"
                if integration == "openai"
                else "device"
                if integration in ("github-copilot", "opencode", "xai")
                else "oauth"
            )
            credential = {
                "type": "oauth",
                "methodID": method,
                **{key: entry[key] for key in ("access", "refresh", "expires")},
            }
            metadata = {
                target: entry[source]
                for source, target in (
                    ("accountId", "accountID"),
                    ("enterpriseUrl", "enterpriseUrl"),
                )
                if entry.get(source)
            }
            if metadata:
                credential["metadata"] = metadata
        else:
            raise ValueError("Unsupported legacy OpenCode credential type.")
        result.append(
            {
                "id": "cred_ocbox_"
                + hashlib.sha256(integration.encode()).hexdigest()[:24],
                "integrationID": integration.rstrip("/"),
                "label": "oc-box auth override",
                "value": credential,
                "active": True,
            }
        )
    return result


def directory(export):
    info = export.get("info", {})
    return (
        info.get("location", {}).get("directory")
        if "location" in info
        else info.get("directory")
    )


def set_directory(export, path):
    if "location" in export["info"]:
        export["info"]["location"] = {"directory": str(path)}
    else:
        export["info"]["directory"] = str(path)


def native(export):
    return "location" in export.get("info", {})


def validate_native(export, ident):
    if (
        export.get("info", {}).get("id") != ident
        or not isinstance(directory(export), str)
        or not isinstance(export.get("messages"), list)
    ):
        raise ValueError("Invalid OpenCode 2.x session export.")
    seen = set()
    for message in export["messages"]:
        if (
            not isinstance(message, dict)
            or not re.fullmatch(r"msg[A-Za-z0-9_-]+", str(message.get("id", "")))
            or message["id"] in seen
            or not isinstance(message.get("type"), str)
        ):
            raise ValueError("Invalid OpenCode 2.x message identity.")
        seen.add(message["id"])


def native_archive(export, data, remap_id):
    result = json.loads(json.dumps(export))
    ident = result["info"].get("id")
    validate_native(result, ident)
    mapping = {ident: remap_id(ident, "ses")}
    mapping.update(
        {
            message["id"]: remap_id(message["id"], "msg")
            for message in result["messages"]
        }
    )

    result["info"]["id"] = mapping[ident]
    for message in result["messages"]:
        message["id"] = mapping[message["id"]]
        if message["type"] == "compaction":
            metadata = message.get("metadata", {})
            if isinstance(metadata.get("keepID"), str):
                metadata["keepID"] = mapping.get(metadata["keepID"], metadata["keepID"])
    set_directory(result, data["project"])
    for key in ("parentID", "permissions", "fork", "revert"):
        result["info"].pop(key, None)
    result["info"]["title"] = (
        "[oc-box "
        + data["project"].rstrip("/").split("/")[-1]
        + "] "
        + export["info"].get("title", "Session")
    )
    return result
