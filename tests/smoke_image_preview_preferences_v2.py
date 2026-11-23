"""Provider-free native 2.3.0 proof for ephemeral image CLI overrides.

Run with TMPDIR=/tmp/opencode python3 -B
tests/smoke_image_preview_preferences_v2.py --opencode /path/to/opencode-v2.
Fresh synthetic HOME/XDG directories, owned stdio servers and bounded PTYs only.
No Podman, real user state, providers, external endpoints or PTY recordings.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from smoke_native_preferences_v2 import Client, host_archive, selected
from smoke_session_sync_v2 import bounded, private_environment, require

from oc_box import opencode_compat, opencode_v2_io as bridge, sync_config


def image_values(preferences):
    config = json.loads(preferences.read_bytes())
    return tuple(
        config.get(key, {}).get("image_preview") for key in ("prompt", "session")
    )


def toggle_image(client, label, key, expected):
    before = client.preferences.stat().st_mtime_ns
    client.setting(label)
    client.send(b"\x1b[C")
    client.wait(
        lambda: client.preferences.stat().st_mtime_ns != before,
        label + " explicit native image write",
        timeout=5,
    )
    value = json.loads(client.preferences.read_bytes())
    require(
        value.get(key, {}).get("image_preview") is expected,
        "Native image setting did not use the expected effective value.",
    )
    client.send(b"\x1b", 0.25)


def exercise(binary, root, *, explicit_false):
    project = root / "project"
    project.mkdir(parents=True)
    env = private_environment(root / "native")
    home = Path(env["HOME"])
    for name, relative in (
        ("CONFIG", ".config"),
        ("STATE", ".local/state"),
        ("DATA", ".local/share"),
        ("CACHE", ".cache"),
    ):
        env["XDG_" + name + "_HOME"] = str(home / relative)
    env.update(
        TERM="xterm-256color",
        COLORTERM="truecolor",
        OPENCODE_DISABLE_AUTOUPDATE="1",
    )
    release = opencode_compat.version(bounded([binary, "--version"], env, project))
    require(release == "2.3.0", "Native image preferences fixture requires 2.3.0.")
    host = root / "host-config/opencode"
    host.mkdir(parents=True)
    defaults = {
        "$schema": "https://opencode.ai/v2/cli.json",
        "session": {"permissions": "prompt"},
        "attention": {"notifications": False, "sound": False},
        "theme": {"name": "opencode"},
        "animations": False,
        "highlights": False,
    }
    if explicit_false:
        defaults["prompt"] = {"image_preview": False}
        defaults["session"]["image_preview"] = False
    (host / "cli.json").write_text(json.dumps(defaults, indent=2) + "\n")
    (host / "opencode.json").write_text('{"update":"disable"}\n')
    original_host = (host / "cli.json").read_bytes()
    sync_config.sync_config(home, host_archive(host), release)
    preferences = home / ".config/opencode/cli.json"
    baseline_images = image_values(preferences)
    previous = Path.cwd()
    os.chdir(project)
    try:
        with patch.dict(os.environ, env, clear=True):
            first_exit = {}
            with bridge.server(binary, exit_info=first_exit) as native:
                require(
                    native("GET", "/api/credential")["data"] == []
                    and bridge.session_inventory(native, str(project)) == [],
                    "Synthetic native image preferences fixture was not empty.",
                )
                client = Client(
                    binary,
                    env,
                    project,
                    native.endpoint,
                    preferences,
                    image_preview=True,
                )
                try:
                    client.ready()
                    client.change(
                        "Permissions", ("session", "permissions"), "autoaccept"
                    )
                    client.change("Notifications", ("attention", "notifications"), True)
                    require(
                        image_values(preferences) == baseline_images,
                        "BLOCKER: unrelated native UI saves persisted forced image values.",
                    )
                    # A right-arrow boolean toggle must write false, proving the
                    # real UI loaded each effective true override, not base false.
                    toggle_image(client, "Image previews", "prompt", False)
                    toggle_image(client, "Transcript images", "session", False)
                finally:
                    client.close()
                require(
                    native("GET", "/api/credential")["data"] == []
                    and bridge.session_inventory(native, str(project)) == [],
                    "Native image settings created sessions or credentials.",
                )
            require(
                first_exit == {"code": 0, "forced": False}
                and selected(preferences) == ("autoaccept", True),
                "Flagged native client/server did not retain explicit preferences.",
            )
            native_preferences = preferences.read_bytes()
            sync_config.sync_config(home, host_archive(host), release)
            require(
                preferences.read_bytes() == native_preferences,
                "Production refresh replaced native preferences.",
            )
            second_exit = {}
            with bridge.server(binary, exit_info=second_exit) as native:
                client = Client(binary, env, project, native.endpoint, preferences)
                try:
                    client.ready()
                    # No override on the next owner: both saved base false
                    # choices now cycle false -> true -> false in the real UI.
                    for label, key in (
                        ("Image previews", "prompt"),
                        ("Transcript images", "session"),
                    ):
                        toggle_image(client, label, key, True)
                        toggle_image(client, label, key, False)
                    client.change("Permissions", ("session", "permissions"), "prompt")
                    client.change(
                        "Notifications", ("attention", "notifications"), False
                    )
                finally:
                    client.close()
                require(
                    native("GET", "/api/credential")["data"] == []
                    and bridge.session_inventory(native, str(project)) == [],
                    "Restarted native image fixture created sessions or credentials.",
                )
            require(
                second_exit == {"code": 0, "forced": False}
                and image_values(preferences) == (False, False)
                and selected(preferences) == ("prompt", False)
                and (host / "cli.json").read_bytes() == original_host,
                "Unflagged native client/host preference separation failed.",
            )
            print(
                "PASS: base image fields "
                + ("false" if explicit_false else "absent")
                + "; real UI saves other preferences without image persistence; "
                "both effective inline true values and next unflagged base false "
                "are confirmed by native UI toggles; host unchanged.",
                flush=True,
            )
    finally:
        os.chdir(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", required=True, type=Path)
    arguments = parser.parse_args()
    binary = str(arguments.opencode.resolve())
    print("Native sha256=" + hashlib.sha256(Path(binary).read_bytes()).hexdigest())
    with tempfile.TemporaryDirectory(
        prefix="oc-box-image-preferences-v2-"
    ) as directory:
        for explicit_false in (False, True):
            exercise(
                binary,
                Path(directory) / str(explicit_false),
                explicit_false=explicit_false,
            )
    print(
        "Native synthetic PTY/config integration only; not host/Podman, real "
        "image rendering, permission enforcement or notification delivery acceptance.",
        flush=True,
    )


if __name__ == "__main__":
    main()
