# oc-box development

Run development commands from the repository root. Keep public documentation
portable: use example project paths and synthetic session IDs, not local data.

## Architecture

- `oc_box.py`: external rootless-Podman launcher and workspace lifecycle.
- `bootstrap.py`: baseline tools installed inside the container.
- `sync_config.py`: container-only config refresh with dependency-cache reuse.
- `terminal_ui.py`: native TUI relay and startup loading indicator.
- `status_report.py`: read-only host resource and storage reporting.
- `version_info.py`: commit-derived version and author/license information.
- `install.py`: copies the launcher to `~/.local/share/oc-box` and creates
  `~/.local/bin/oc-box`.
- `tests/`: stdlib unit tests and bounded offline mainline integration fixtures.
- `docs/cli.md`: canonical command reference; `tools/build_docs.py` generates
  the committed man/Texinfo/Info manuals from it.

OpenCode itself and its integrations run natively inside the container. Preserve
MCPs, formatters, language servers, and plugins. Do not return to replacing
built-in tools or disabling integrations for containment. Keep mainline OpenCode
unmodified. Live sessions persist in a private named volume, and host-side
history is synchronized with supported CLI export/import commands on exit.

## Git workflow

Commit completed changes after reviewing the diff and running the relevant
checks. The user explicitly requests automatic commits for work in this
repository. Stage intended source files explicitly; sandbox-generated empty
placeholder dotfiles are not project source. The user authorizes automatic
pushes of reviewed commits to an explicitly configured upstream, provided the
hosting service permits this AI-assisted project. Do not create a remote or
push to a service with incompatible policies. Do not store credentials in Git
URLs, config, source files, or command-line arguments.

The public upstream is `https://github.com/groopcd/oc-box.git`. Use only the
explicit `PERSONAL_GITHUB_TOKEN` for publication. Inherited `GITHUB_TOKEN` and
`GH_TOKEN` belong to a separate Microsoft/Copilot account and must not be used
for repository management or pushes. Give publishing subprocesses an isolated
GitHub CLI config and set their local `GH_TOKEN` from `PERSONAL_GITHUB_TOKEN`,
without modifying the parent environment. Verify `gh api user` reports
`groopcd` before publishing. Pass Git credential helpers per command rather
than storing tokens or changing global authentication settings.

Version `0.1.N+gHASH` is derived from the full Git history; every commit advances
N automatically. The installer embeds the source identity in its private
`build-info.json`. Original project code/documentation is GPL-3.0-or-later;
retain LICENSE and NOTICE when distributing/installing it.

Update `docs/cli.md` when command-line behavior changes, then regenerate with
`python3 tools/build_docs.py` (requires GNU makeinfo). Prebuilt manuals ship in
the repository; installing oc-box must not require documentation build tools.

## Checks

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
```

The smoke test uses temporary databases and offline fixture services, with
bounded timeouts. It does not verify actual Podman isolation. Live Podman
verification must run on the host, outside another container/sandbox.

A host-terminal Tumbleweed container/OpenCode launch has succeeded. Full live
MCP, formatter, language-server, and stop/resume verification is still pending;
the automated integration checks use offline fixtures.

The host-terminal warm-start test is now effectively instant after retaining
the container-generated plugin SDK manifest, lockfiles, and dependency cache.

Host-session import and mapped NPU access have been exercised on Tumbleweed.
A native warm plugin-initialization probe completed in under one second.
New mounts use `:z`, and cold plugin resolution is timed before opening the TUI.

Container launches give Build mode allow-all OpenCode permissions through a
generated private config/plugin. Keep Plan/subagent rules and plain host
OpenCode permissions unchanged.

Do not treat a lost marker or changed filesystem device/inode as permission to
create a replacement workspace. Look up registered canonical paths, direct the
host operator to explicit `repair`, and never overwrite another workspace's
marker. Plain stale-workspace purge preserves project files, host history, and
private archive-origin evidence under `retired/`; keep that recovery path tested.

`terminal` is maintenance-only: no host credential/pass_env forwarding, no
OpenCode/auth/config refresh, startup profiles, or persistent shell history.
Keep its environment allowlisted and preserve the original container run state.
