#!/usr/bin/env python3
"""Finish the initial Git milestone and migrate one OpenCode session on the host.

Run after exiting the OpenCode process opened in the old directory. Session
export/import uses the supported CLI; the live SQLite database is never edited.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitignore", "AGENTS.md", "README.md", "bootstrap.py", "install.py", "oc_box.py", "sync_config.py", "terminal_ui.py",
    "tests/fixture_services.py", "tests/test_oc_box.py", "tests/test_finalize_milestone.py", "tests/test_startup.py", "tests/smoke_mainline.py",
    "tools/finalize_milestone.py",
)
COMMIT = "Initial milestone: rootless Podman environments for OpenCode"


def run(command, *, cwd=ROOT, env=None, capture=False, check=True):
    environment = {**(os.environ if env is None else env), "PWD": str(cwd)}
    if capture:
        # OpenCode 1.19.5 can exit before a large stdout pipe is drained.
        # Regular files make stdout writes synchronous and avoid truncation.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(command, cwd=cwd, env=environment, text=True, stdout=output, stderr=errors, timeout=90)
            output.seek(0)
            errors.seek(0)
            result.stdout = output.read().decode("utf-8")
            result.stderr = errors.read().decode("utf-8", errors="replace")
    else:
        result = subprocess.run(command, cwd=cwd, env=environment, text=True, timeout=90)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise RuntimeError(f"{command[0]} failed ({result.returncode}): {detail}")
    return result


@contextmanager
def opencode_env():
    with tempfile.TemporaryDirectory(prefix="oc-box-relocate-") as temporary:
        private = Path(temporary)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("OPENCODE_", "OC_BOX_"))}
        for directory in ("config", "state", "home"):
            (private / directory).mkdir()
        env.update({
            "OPENCODE_PURE": "1",
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_TEST_HOME": str(private / "home"),
            "XDG_CONFIG_HOME": str(private / "config"),
            "XDG_STATE_HOME": str(private / "state"),
            "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")),
            "OPENCODE_CONFIG_CONTENT": json.dumps({
                "$schema": "https://opencode.ai/config.json",
                "snapshot": False, "formatter": False, "lsp": False,
            }),
        })
        yield env


def export_session(session, env):
    result = run(["opencode", "--pure", "export", session], env=env, capture=True)
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"OpenCode returned invalid/incomplete JSON ({len(result.stdout.encode('utf-8'))} bytes); no session import was attempted: {exc}") from exc
    if data.get("info", {}).get("id") != session or not isinstance(data.get("messages"), list):
        raise RuntimeError("OpenCode returned an invalid session export.")
    return data


def backup_session(data):
    state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = state / "oc-box/migrations" / (stamp + "-" + data["info"]["id"])
    folder.mkdir(parents=True, mode=0o700)
    target = folder / "session-before-relocation.json"
    with target.open("x", encoding="utf-8") as stream:
        os.chmod(target, 0o600)
        json.dump(data, stream, indent=2)
        stream.write("\n")
    return target


def initial_commit():
    if not (ROOT / ".git").exists():
        parent = run(["git", "rev-parse", "--show-toplevel"], capture=True, check=False)
        if parent.returncode == 0:
            raise RuntimeError(f"Refusing to initialize inside another repository: {parent.stdout.strip()}")
        run(["git", "init"])
    top = run(["git", "rev-parse", "--show-toplevel"], capture=True).stdout.strip()
    if Path(top).resolve() != ROOT:
        raise RuntimeError("Unexpected Git worktree root.")
    # Inspect before staging/committing, including the unborn-repository case.
    run(["git", "status", "--short"])
    run(["git", "--no-pager", "diff", "--"])
    log = run(["git", "--no-pager", "log", "--oneline", "-10"], capture=True, check=False)
    if log.returncode == 0:
        print(log.stdout, end="")
        if run(["git", "status", "--porcelain"], capture=True).stdout.strip():
            raise RuntimeError("Repository already has commits and pending changes; review and commit those separately.")
        return run(["git", "rev-parse", "HEAD"], capture=True).stdout.strip()
    print("No existing commits; creating the first milestone.")
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], capture=True).stdout.splitlines()
    unexpected = set(untracked) - set(FILES)
    staged = run(["git", "diff", "--cached", "--name-only"], capture=True).stdout.splitlines()
    if unexpected or set(staged) - set(FILES):
        raise RuntimeError(f"Unexpected files require review: {sorted(unexpected | (set(staged) - set(FILES)))}")
    secret = re.compile(r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|-----BEGIN (?:OPENSSH|RSA|EC) PRIVATE KEY-----")
    for name in FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"Expected a regular source file: {name}")
        if secret.search(path.read_text()):
            raise RuntimeError(f"Potential credential in {name}; inspect before committing.")
    run(["git", "add", "--", *FILES])
    run(["git", "diff", "--cached", "--check"])
    run(["git", "--no-pager", "diff", "--cached", "--stat"])
    run(["git", "--no-pager", "diff", "--cached", "--"])
    run(["git", "commit", "-m", COMMIT])
    run(["git", "status", "--short"])
    return run(["git", "rev-parse", "HEAD"], capture=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="existing OpenCode session to associate with this repository")
    parser.add_argument("--old-directory", required=True, type=Path, help="previous OpenCode working directory")
    args = parser.parse_args()
    if not re.fullmatch(r"ses[A-Za-z0-9_-]+", args.session):
        raise RuntimeError("Invalid session ID.")
    if os.geteuid() == 0:
        raise RuntimeError("Run as your regular user, without sudo.")
    with opencode_env() as env:
        exported = export_session(args.session, env)
        current = Path(exported["info"]["directory"]).resolve()
        if current not in (args.old_directory.expanduser().resolve(), ROOT):
            raise RuntimeError(f"Session belongs to an unexpected directory: {current}")
        backup = backup_session(exported)
        print(f"Private session backup: {backup}", flush=True)
        commit = initial_commit()
        # Importing the same ID in the new repo updates its directory/project
        # association while preserving existing messages. No duplicate session.
        run(["opencode", "--pure", "import", str(backup)], env=env)
        moved = export_session(args.session, env)
        if Path(moved["info"]["directory"]).resolve() != ROOT:
            raise RuntimeError("Session directory migration did not take effect.")
        if moved["info"]["projectID"] == "global":
            raise RuntimeError("Session did not associate with the new Git project.")
        if len(moved["messages"]) < len(exported["messages"]):
            raise RuntimeError("Session verification found fewer messages after migration.")
    print(f"\nMilestone committed: {commit}\nSession now belongs to {ROOT}")
    print(f"Resume with: opencode {ROOT} --session {args.session}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"finalize-milestone: {exc}", file=sys.stderr)
        sys.exit(1)
