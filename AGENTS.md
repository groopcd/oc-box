# oc-box development

Run development commands from the repository root. Keep public documentation
portable: use example project paths and synthetic session IDs, not local data.

## Architecture

- `oc_box.py`: external rootless-Podman launcher and workspace lifecycle.
- `bootstrap.py`: baseline tools installed inside the container.
- `sync_config.py`: container-only config refresh with dependency-cache reuse.
- `terminal_ui.py`: native TUI relay and startup loading indicator.
- `status_report.py`: read-only host resource and storage reporting.
- `dependencies.py`: read-only feature-scoped doctor, OS package providers and
  early preflight guidance. Keep host and workspace-container scopes distinct;
  never install packages automatically from diagnostics.
- `version_info.py`: commit-derived version and author/license information.
- `opencode_compat.py` / `opencode_v2_io.py`: version-specific 1.x/2.x CLI,
  native transcripts, private V2 service leases and credential refresh. Preserve
  V1 database/history; never save forwarded vault env in service config or
  connect workspace activity to the host shared service.
- `workspace_service.py`: foreground V2 owner, same-user runtime-socket client
  leases and host-loopback HTTP/WebSocket relay. Control credentials remain
  ephemeral memory only; no published container/LAN ports or shared host service.
  Owners wait for attached clients before sync/stop, and lifecycle locks remain
  exclusive against reset/purge/backup. Attach never refreshes credentials.
- `session_terminal.py`: native persistent V2 session shell PTY client with
  authenticated WebSocket framing, resize/checkpoint replay and Ctrl-] detach.
  Reuse the session terminal, never replace it with the mini chat interface.
- `plugin_compat.py` / `plugin_bridge.js`: bounded copied-V1-plugin adapters on
  native V2 plugin APIs. Keep host originals and V1 behavior intact, propagate
  refusal errors and preserve attachments/structured output; reject unsupported
  hooks explicitly. Do not modify mainline tools or reactivate nested sandboxing.
- `workspace_identity.py`: generic Linux VFS directory identity, capability-based
  persistent handles and strict fallback; shared by lifecycle/status checks.
- `vault.py` / `credentials.py`: optional encrypted host vault and names-only
  per-workspace credential policy. Legacy env-file forwarding remains default.
- `secret_prompt.py`: controlling-TTY masked input, explicit value reveal, safe
  terminal restoration. Never reveal passphrases or accept non-TTY fallback.
- `backups.py` / `backup_crypto.py` / `backup_archive.py`: host-only encrypted
  backup/restore feature; use established age streaming encryption, no plaintext
  archive staging, fresh restore IDs, and no archived privilege reactivation.
- `install.py`: copies the launcher to `~/.local/share/oc-box` and creates
  `~/.local/bin/oc-box`.
- `installation.py`: staged immutable installed copies and launcher activation.
- `completions/oc-box`: standalone Bash command/option completion, emitted by
  `oc-box completion bash` and shipped in both installation channels. Keep Tab
  handling local and noninteractive; no lifecycle or credential commands.
- `updater.py` / `install.sh`: public GitHub upgrades and one-line bootstrap.
- `pyproject.toml` / `setup.py`: Python distributions with an isolated `oc_box`
  namespace, resources, public versions, and source metadata.
- `.github/workflows/publish.yml`: selected PyPI releases via Trusted Publishing.
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

Version `1.5.N+gHASH` is derived from the full Git history, with N the commit
count minus the 1.0.0 baseline (29); every later commit advances N automatically.
The 1.5 series deliberately retains this baseline; do not reset the patch count
or rewrite embedded 0.1/1.0 identities when preparing the V2 release.
Keep the absolute `commit_count` for old updater compatibility, require full
history, and fast-forward tested release commits into master. The installer
embeds the source identity in its private `build-info.json`. Original project
code/documentation is GPL-3.0-or-later;
retain LICENSE and NOTICE when distributing/installing it.
PyPI uses the public `1.5.N` version; retain the full `+gHASH` identity in package
resources. Python-package installations upgrade through pipx/uv/pip, never the
GitHub self-updater. Preserve the normal curl/GitHub update path in parallel.

Update `docs/cli.md` when command-line behavior changes, then regenerate with
`python3 tools/build_docs.py` (requires GNU makeinfo). Prebuilt manuals ship in
the repository; installing oc-box must not require documentation build tools.

## Checks

```bash
uv run --no-project --with cryptography python -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
python3 -B tests/smoke_v2.py --opencode /path/to/opencode-v2
```

The smoke test uses temporary databases and offline fixture services, with
bounded timeouts. It does not verify actual Podman isolation. Live Podman
verification must run on the host, outside another container/sandbox.

The V2 smoke uses unmodified 2.2.6 with private servers, native tool/formatter/MCP
Code Mode execution, env delivery, credential rotation, large transcript sync,
and upstream V1 migration from a retained snapshot. V2 lacks upstream native LSP.
Keep `feature/opencode-2-compat` unmerged/unreleased until accepted disposable
host-terminal V2 lifecycle/credential/backup validation; no real workspaces in tests.
Host TUI/web/persistent-shell coexistence, detach/reattach, stop/resume and updated
plugin behavior are accepted. Selected-vault credentials, V2 full/portable
backup/restore, reset and V1 downgrade still require host acceptance. Keep native
V1/V2 integration enabled on master and pull requests after the feature merges.
`tools/test_v2_podman.py` is the opt-in host acceptance fixture for these remaining
checks: isolated synthetic vault/provider/MCP, native version executables, real
rootless backup/reset/downgrade. Prompt/TUI actions are scripted; do not claim a
live pass from its offline unit tests or run it inside this container.
Native V2 fixture `run` calls require explicit stdin EOF: Podman exec supplies a
FIFO that upstream otherwise waits to read before inference. Source-only retry
must validate its private registry/graphroot/runroot and refuse backup evidence.

Package/dependency mappings must pass `.github/workflows/distros.yml` on all
seven catalog images using `tools/test_dependency_distro.py`. That disposable
package installation fixture does not test nested/rootless Podman lifecycle.

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
marker. Device-number changes may be accepted only against a recorded matching
filesystem ID, persistent directory handle, and inode; never infer a baseline
from an old device number and unchanged inode alone. Upgrade legacy records
under the workspace lock only after verification or explicit repair.
Use the common VFS APIs without filesystem-name branches. Do not promise
physical identity on unsupported drivers or replace it with a copyable marker.
Plain stale-workspace purge preserves project files, host history, and
private archive-origin evidence under `retired/`; keep that recovery path tested.

V1 `terminal` and `terminal --maintenance` are maintenance-only: no host
credential/pass_env forwarding, OpenCode/auth/config refresh, startup profiles,
or persistent shell history. Preserve the original container run state.
Default V2 terminal joins the current session's actual persistent shell PTY;
it may coexist with TUI/web through same-user leases without changing lifecycle
state. Browser/client processes must not inherit provider/vault values.
Copied V2 plugin fixes keep host/V1 originals intact. Clamp UTF-8 by bytes with
whole-code-point slices; definition audits use primary-request JSON schemas.
Configured local source files outside autoload directories still need adapters;
only bridge explicit supported outbound-message/header semantics, preserving
native media/reasoning and rejecting unsupported identity/order changes.

Updates fetch the fixed public HTTPS upstream anonymously with sanitized Git
configuration/environment. Never reuse publishing/provider credentials for
downloads. Keep automatic updating opt-in, preserve running release copies,
workspaces and source checkouts, and do not self-update from inside a container.

Vault values/passphrases never belong in argv, logs, project files, registry
metadata, plaintext temporary files, or host os.environ. Keep the optional crypto
dependency lazy for default installs. Maintenance and informational commands
never unlock. Wrong/missing selected credentials must abort before reset deletion.
Secret values may be deliberately revealed only in the interactive host prompt;
start every field masked, keep passphrases masked, and document recording risk.
Vault/backup new-password minimums default to 8 characters, configurable separately
in host config or by `--min-passphrase-length` (8..1024, 1024 UTF-8-byte cap).
Enforce creation policy before unlock/lifecycle changes; never reject existing
unlock passwords against a newer creation policy. age passwords go only through
a private echo-disabled PTY, never argv/env/files; preserve standard age format.

Full and portable backup/restore passed accepted host rootless-Podman validation
for 1.0.0, including synthetic database bytes, session selection, rootless
ownership, internal paths, source preservation, stopped restores, and OS-state
differences. Repeat `tools/test_backup_podman.py` on a host for relevant lifecycle
changes, isolated from real workspace state. This fixture does not test restored
real-project model inference.
Restores retain original internal project paths via `container_project`; host
destinations/physical identities and resource IDs are fresh, not DB path edits.
