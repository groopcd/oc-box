# oc-box development

Run development commands from the repository root. Keep public documentation
portable: use example project paths and synthetic session IDs, not local data.

## Delegation

Use subagents proactively for substantial, independent investigations, bounded
testing, and independent reviews; no repeated user request is needed. Handle
small or tightly coupled tasks directly when delegation would add more overhead
than value. Keep one primary agent responsible for scope, integration, reviewing
subagent results, and final verification.

Give each subagent a bounded objective, the necessary context, and explicit
read-only or editing responsibilities. Avoid overlapping edits; use separate Git
worktrees when parallel edits require isolation. Respect read-only and
planning-only requests across all delegated work, and never use delegation to
bypass permissions or host-only test restrictions.

Coordinate tests with edits: record the tested commit and working-tree state,
and do not claim a result validates a revision changed underneath the test run.
Wait for shared-checkout tests to finish or run parallel work in isolated
worktrees. Independently opened sessions do not coordinate automatically; use
explicit handoffs rather than assuming shared conversation context.

## Architecture

Runtime implementation is under `src/oc_box/`; module names below refer to that
directory. `paths.py` separates runtime files from checkout/sdist metadata and
documentation. Wheels bundle resources alongside the package; the GitHub
installer retains its immutable flat release layout. Root `install.py`,
`install.sh` and `setup.py` are compatibility/build entrypoints, not application
implementation. Build implementation is `src/_oc_box_build.py`. Preserve older
installer/update callers; do not remove these compatibility entrypoints.
Tests/tools stay in their conventional top-level directories. Do not remove
ignored caches, environments or `.oc-box-id` as part of source-layout cleanup.
The src-layout refactor's offline checks are not a new live Podman acceptance.
Editable source installations must also refuse the GitHub self-updater; keep
their checkout ownership distinct from unrelated source and flat installations.

- `oc_box.py`: external rootless-Podman launcher and workspace lifecycle.
  Keep top-level help a short grouped command index and launcher command pages
  scoped to applicable options. Preserve dedicated command help, implicit
  `start` and legacy selector-first parsing. Help must not resolve workspaces,
  acquire locks, consult the vault, call Podman or touch launcher state.
- `bootstrap.py`: baseline tools installed inside the container.
- `sync_config.py`: container-only config refresh with dependency-cache reuse.
- `terminal_ui.py`: native TUI relay and startup loading indicator. Keep native
  OpenCode output/UI unchanged. Launcher stage progress is transient on a
  capable TTY and has no default start/done lines in pipes or `TERM=dumb`.
  Preserve actionable warnings/prompts and one final history result per attempt,
  with interruption/unconfirmed-completion cautions on recovery. Report partial
  or uncertain cleanup honestly; do not change lifecycle, eligibility or exits.
  `--lifecycle-diagnostics` explicitly opts in to expanded launcher messages for
  one invocation; no saved default beyond quiet. The sharing host fixture uses
  it only for the owner expecting an explicit stop and checking the legacy
  pending-history line. Keep historical reports intact; offline output checks
  are not new host acceptance.
- `status_report.py`: read-only host resource and storage reporting.
- `tui.py` / `tui_app.py` / `tui_backend.py`: optional, host-side Textual manager.
  Keep ordinary CLI imports stdlib-only, demo strictly synthetic, browsing free
  of workspace starts/unlocks/sync, and resource values informational. Live
  CPU/RAM/storage edits are deferred; do not introduce resource-control RPCs.
  Host session Connect may use normal native host discovery; workspace sources
  must use an existing private owner, with explicit visible client leases.
  To manage the oc-box checkout itself, run an isolated non-editable package,
  not `--with-editable` launcher code inside that mount. Preserve APP/state
  exposure validation; a self-workspace refusal is not permission to bypass it.
  The first integrated TUI release requires the current focused host runner's
  42 checks (33 retained plus nine archived-root resume checks), not an older
  33-check pass. Its ordered exits are [0,0,0,1,0,0,0,0]. Use the interactive
  fixture's explicit `manager` mode for Textual observations; native `tui`
  observations do not cover the dashboard. Manager browsing has no outer lease.
- `tui_status.py` / `tui_settings.py`: cell-bounded diagnostics and session-only
  categorized preferences. Keep meter columns aligned and scales fixed; unknown
  is not zero, and container CPU bars use observed quotas. Requested refresh
  defaults to one second with no overlapping queries. Settings must not weaken
  CLI read-only mode, connect sessions, persist search values, or edit resources.
- `tui_sessions.py`: paginated metadata-only catalogs and private UI favorites.
  Preserve source/store identity, verified controller-archive provenance, native
  child cascades and process-local activity limits. Archive/delete are explicit,
  source-specific actions, never bidirectional mirror synchronization. Archived
  V2 roots remain synchronized but are not automatic resume targets.
- `tui_terminal.py`: bounded local PTY/emulator for the existing native session
  terminal client. Require explicit validated attachment and remote exec TTY;
  do not create a replacement container shell or record terminal output. Local
  client state is not proof of native readiness/controller ownership. Native
  and headless checks are not interactive/rootless-Podman acceptance.
- `tui_clipboard.py`: explicit optional dashboard clipboard/viewport selection.
  Never read the desktop clipboard in the background, journal copied/pasted
  text or record terminal output. User-requested auto-copy may write only when
  a real user selection gesture finishes, never during dragging or from
  programmatic selection, refresh, focus or container output. Explicit UI copy
  and completed user selections use host clipboard support; container OSC
  clipboard requests stay stripped.
  Paste copied is the in-app cache, not the current desktop clipboard. Preserve
  focused shell control keys, byte/queue bounds and CLI read-only fences.
- `activity_log.py` / `logs_cli.py` / `tui_logs.py`: private bounded
  fixed-category activity journal plus explicit in-memory dashboard external
  log views. `oc-box logs` is host-only and read-only, with a stdlib-only
  private default. It must not write journal, workspace or history state or
  read native OpenCode or container logs.
  CLI and dashboard share validated severity and message formatting; only the
  CLI defaults to `info` and higher. The dashboard includes `debug`. Valid
  legacy rows remain readable. Missing or unsafe journal data fails closed to
  empty; JSON Lines with no matches emits nothing. Preserve legitimate owned
  legacy state permissions while keeping log directories/files private. Reason
  codes and metric names use closed sets; metric values are bounded integers.
  Never journal raw argv, errors, env, stdout/stderr, prompts, transcripts,
  pairing links, authentication, clipboard data or other free-form text.
  Dashboard external-log redaction remains best-effort, not a guarantee of
  arbitrary-secret removal.
  RAW dashboard output uses only validated/retained display data and follows
  source/query filters without table formatting. Explicit bulk copy stays
  clipboard-bounded and never exposes pre-redaction input. System-journal
  browsing is explicit, never merged into the default source view.
  `log_config.py` loads bounded host `logs` policy without writes or following
  config symlinks. Only a separate, explicit Host logging save may persist
  the private directory and mirror choice, preserving all other configuration.
  Demo and read-only views cannot save; there is no log migration or setup
  during config edits. Existing custom destinations are checked without
  creation; custom writes verify reserved names and the records of every file
  affected by rotation or retention before changing the ring. Never adopt
  unrelated files or guess at corrupt custom records. Keep default-journal
  crash recovery unchanged. Logger flags are invocation-local, never environment
  switches or config edits; invalid saved policy fails closed even with
  explicit overrides. Only default/start, reset, stop, sync, terminal and web
  accept launcher `--log-dir PATH`, `--journald` / `--no-journald`, and
  `--lifecycle-diagnostics`. `--log-dir PATH` overrides valid `logs.directory`
  and also works for private `logs` reads, including `logs --path`; only an
  append can create the directory. Journal location is independent of workspace
  registration. Rotation and age/space retention apply only on appends, only
  to the owner's validated managed files, and never modify the system journal
  or move old logs.
  `logs.journald` is a host boolean, default false. The launcher flags override
  it for one invocation. Mirror only successfully appended, validated activity
  events to existing local systemd-journald with native priorities. The private
  file remains primary; sink errors cannot affect lifecycle results. Do not
  start/configure daemons or add ordinary systemd dependencies. System journal
  visibility and retention follow host policy. `oc-box logs --journald` is
  read-only, independent of the writer setting, and incompatible with
  `--log-dir`. It expressly selects the current user's mirror via local
  `journalctl`, with the same severity and workspace filters, limit, follow and
  JSON Lines support. On unavailability give fixed safe guidance to use the
  private default, never fall back automatically or expose raw errors. Mock
  backends are not host journal acceptance.
- `tui_storage.py` / `tui_operations.py` / `tui_management.py`: three distinct
  host/selected/all-workspace scopes, canonical bounded registry reads and explicit
  operation reviews, typed only for destructive actions. Host operations target
  individual session trees, never a whole database. Workspace removal retains
  project/history and uses normal locks/validators; no bulk wipe, shared-cache
  pruning or resource-limit edits.
  Hand secrets to existing masked foreground TTY prompts in the same pinned runtime,
  not selectable dashboard inputs or recorded output. Recheck review bindings in
  the child; ordinary CLI imports remain stdlib-only.
- `tui_operation_io.py` / `tui_backup_workflow.py`: the explicitly approved
  backup-only in-dashboard exception to foreground prompt presentation. Use a
  separate private controlling PTY and the existing child `secret_prompt` reader,
  explicit bounded readiness/review/result channels, and a nonselectable custom
  `PRIVATE_INPUT` editor/screen, never an ordinary Textual/ClipboardInput field.
  The manager's transient private editor is now inside the secret-memory trust
  boundary; do not claim guaranteed erasure or an unchanged memory boundary.
  Passwords cross only the echo-disabled child PTY on completed submission, not
  protocol metadata, widget values/reactives/messages, argv/env/files, clipboard
  caches or logs. Hold an app-wide private-input shield through prompt closure.
  Keep ordinary CLI controlling-TTY behavior unchanged, all production host
  guards, native verification, archive formats and lifecycle safety authoritative.
  Parent loss/cancel requests one interrupt and waits for real cleanup/exit;
  no automatic retry, blind process-group kill or unacknowledged success.
  Session cancellation retains attempted versus verified/observed fresh IDs,
  never deletes them or imports more history to manufacture rollback. Core dumps
  are disabled in manager/children before private input; this is not protection
  against a same-user debugger, swap, terminal recording or all memory copies.
  Refuse private dashboard operations when Textual debug, developer tools or
  event-file logging are configured: framework input logging precedes widget
  handlers, so selection exclusion alone is not a sufficient privacy boundary.
- `session_backups.py`: encrypted bounded native V2 history, separate from full/
  portable workspace archives. Plaintext/key material stays in memory; no-overwrite
  age envelopes, quiet source checks, fresh IDs and cleared privileges on restore.
  Verify native GET/export/inventory, report honest partial-import recovery IDs,
  and never infer success from native conflict exit zero or automatically roll back
  by deleting unknown sessions. Session import transcripts must use native stdin,
  not argv; retain the explicit source/service and process-local ownership checks.
- `tui_notifications.py`: allowlisted actionable toasts plus persistent notices.
  Never show raw errors or journal them. Distinguish preflight refusal, unverified
  mutation acknowledgement and completed mutation followed by failed refresh.
- `dependencies.py`: read-only feature-scoped doctor, OS package providers and
  early preflight guidance. Keep host and workspace-container scopes distinct;
  never install packages automatically from diagnostics.
- `version_info.py`: commit-derived version and author/license information.
- `opencode_compat.py` / `opencode_v2_io.py`: version-specific 1.x/2.x CLI,
  native transcripts, private V2 service leases and credential refresh. Preserve
  V1 database/history; never save forwarded vault env in service config or
  connect workspace activity to the host shared service.
  V2 history discovery includes paginated child sessions; only a complete valid
  inventory may retire missing source IDs, retaining their archives. Keep host
  archives independently replaceable, with namespaced relationships as metadata,
  not native parent links or revived permissions. Reject unsettled/omitted history
  and verify native imports; CLI conflict success is not proof of synchronization.
- `workspace_service.py`: foreground V2 owner, same-user runtime-socket client
  leases and host-loopback HTTP/WebSocket relay. Control credentials remain
  ephemeral memory only; no published container/LAN ports or shared host service.
  Owners wait for attached clients before sync/stop, and lifecycle locks remain
  exclusive against reset/purge/backup. Attach never refreshes credentials.
- `history_recovery.py` / `history_reader.py` / `workspace_control.py`: one bounded
  persisted-history snapshot after audited live timeouts: shell-only on native
  2.2.6/2.2.7; shell/subagent on canonical stable 2.3.x. Never a live readiness
  waiver or native job cleanup. The shared 2.3.x profile checks durable job/recovery
  metadata before source/destination native history processing; image/unknown jobs, unknown forms,
  legacy background rows and incompatible schemas refuse. Actual binary versions
  must match the captured exact patch on both sides; a shared series is not a
  patch-mismatch waiver or untested-future-native acceptance. Prereleases,
  modified/malformed identities and unknown minor/major series refuse.
  Detected contract/version incompatibilities give fixed-category guidance to
  update oc-box/OpenCode as appropriate, never a reset or state-downgrade bypass.
  Do not misclassify ordinary activity, timeouts or cleanup failures as version
  drift. Never substitute normal manual-sync preparation or managed restart
  continuity. Require actual verified whole-container stop,
  immutable same-resource restart, stop intent/admission and persistent uncertainty
  fences. Readers are unregistered/non-executing, with source/destination scheduler
  metadata and system-config refusals, one endpoint, unchanged checked exports and
  host verification and separate cleanup. Pass-local export helpers require
  admission per request, bounded private framing, no restart/replay, and
  reaping/strict EOF before success. Transport reuse is
  post-stop only, never bulk work admission or destination writer reuse.
  Preserve original client nonzero results
  and interruption/unconfirmed-completion warnings; only raw client exit 0 plus
  complete verified archival, recovery cleanup and final stop may return 0 after a
  live timeout. Unknown client status, partial/failure or uncertain cleanup remains
  nonzero with retry-sync guidance; historical accepted exit records stay unchanged.
  The new 2.3 foreground-interruption hint requires two complete validated busy
  pairs and only supported job kinds; it authorizes post-stop archival, never live
  export or job completion. Observed foreground interruption stays nonzero even
  when its persisted history saves. Ordinary expected 2.3 live-sync failures may
  route one guarded Reader attempt, not a second unrestricted manual launch.
  Preserve explicit compatibility/cleanup refusals and legacy routing. Require
  native queued-input/execution-claim non-consumption evidence and fresh focused
  external-host validation before publishing this extension; earlier idle-owner
  recovery evidence does not establish that acceptance.
  Never clear an uncertain start without verified transition completion, resume
  work, refresh credentials, create replacement resources or infer host acceptance
  from local native/process fixtures. The focused host recovery gate is additional
  to the retained 42-check history/readiness runner, not a relabeling of it.
- `session_terminal.py`: native persistent V2 session shell PTY client with
  authenticated WebSocket framing, resize/checkpoint replay and Ctrl-] detach.
  Reuse the session terminal, never replace it with the mini chat interface.
- `sharing.py`: experimental host-only foreground live read/list/image tools
  through native V2 client-MCP. Require explicit disclosure consent and a verified
  existing workspace lease; no mounts, listener, staging copies, saved MCP config,
  credential refresh or automatic regrant. Keep pinned-root/openat2 confinement,
  special/link/protected-path refusals, bounded private pipe/WebSocket I/O and
  fixed errors. Disconnect is not recall of disclosed data. Native/offline checks
  are not host acceptance; defer publication until TUI/regression and focused host
  sharing checks are complete.
- `attachments.py` / `attachment_io.py`: explicit host image snapshots and
  session-bound, expiring, one-use native client-MCP grants, or direct native
  image input through stdin-only private admission. Remote path proposals are
  never authorization: broker mode requires independent per-image host TTY
  consent, including uniform denial for missing/protected/special paths. Preserve
  pre-consent descriptor pinning, response backpressure and no automatic regrant.
  Keep the native TUI/container boundary unchanged; there is no public transparent
  host-drop interceptor. Native queued input is not a composer draft or an
  inactivity guarantee. Refuse observed staged reverts and document the residual
  native check/admission race; no host acceptance follows from offline probes.
- `host_drop.py` / `host_drop_input.py` / `host_drop_io.py`: native TUI input
  bridge, enabled by default per workspace on supported clients.
  `workspace_preferences.py` stores private host-only future-client policy beside
  workspace metadata under a separate lock; never mutate owner metadata, unlock,
  launch or live-toggle a client from `preferences`. Explicit startup overrides
  persist; automatic unsupported-client mode skips importing, explicit enable
  remains strict, and corrupt/unsafe preferences refuse. Qualifying pathname pastes authorize
  imports anywhere in that connection, not proof of a physical drag/composer
  focus. Only host stdin or terminal-offered structured drops select sources;
  never add model/MCP/prompt-text triggers. Keep opaque terminal replies,
  UTF-8/C1 boundaries and fail-closed framing. Konsole is pathname-paste-only,
  with no OSC 72 probes/registration and unchanged child output. Only the known
  Kitty profile reserves its audited C0 OSC 72 wire traffic; do not claim a
  generic C1-output sandbox. Unknown/ambiguous host compatibility markers refuse,
  but markers are not authentication. Preserve valid graphics/keyboard traffic;
  unsupported output framing limits fail closed. Graphics support is not DnD
  support. Stream only bounded whitelisted snapshots into a
  pinned private container temp root; never synthesize Enter or native prompt
  admission. Native TUI/server/plugins stay container-side. Cleanup is bounded
  best-effort, not recall or isolation from other workspace tools, and crashes
  can retain staging/history/provider/backup copies. Default relay behavior and
  the independent-confirmation broker remain unchanged. No host acceptance is
  established by synthetic parser/process/native checks.
- `host_clipboard.py`: opt-in host PNG/copied-file clipboard reader. Workspace
  `ui-preferences.json` saves future-client clipboard/symlink booleans under the
  independent host preference lock; absent clipboard choice means OFF, absent
  symlink choice means ordinary following. Explicit native start/reset
  --host-clipboard / --no-host-clipboard choices persist, including on additional
  clients without owner restart/unlock/config refresh; existing clients never
  live-toggle. Saved enable skips unsupported terminals/native V1/missing desktop
  helpers without clearing policy; explicit enable remains strict. Web, terminal,
  manager, sync and informational commands ignore saved policy and reject startup
  flags. `preferences --host-clipboard on|off` and
  `--host-clipboard-symlinks on|off` never launch/unlock/discover helpers and work
  without a TTY. Store only booleans, never helper paths or clipboard data;
  host-only preferences are not restore authority. Discovery
  never reads; only the dedicated top-level host shortcut may call the bounded
  desktop helper. Typed local URI lists reuse the existing pinned host-drop
  source guards; type discovery is not proof of file-manager provenance.
  Clipboard file sources follow ordinary symlinks by default;
  --host-clipboard-symlinks / --no-host-clipboard-symlinks save the future-client
  choice and require effective clipboard enable on native start/reset.
  `preferences --host-clipboard-symlinks on|off` is independent and can save the
  future choice even while clipboard is OFF, without runtime checks.
  Keep strict choices through clipboard disable/re-enable; never silently revert
  to link following. Keep this link-following exception
  out of normal drops and live sharing. Validate selected and canonical protected
  paths; use kernel NO_MAGICLINKS O_PATH selection and match the strict export
  against its still-pinned descriptor before reading. Never use Path.resolve
  followed by an unbound reopen as source authority. Copy-to-shortcut link
  retargeting is an accepted disclosed tradeoff, not immutable source consent.
  Imports always copy, never move/cut or modify the source/clipboard. No
  background polling, model/output/MCP triggers, arbitrary text/plain requests,
  unsafe fallback, socket mounts, listeners or automatic Send. Preserve ordinary
  Ctrl+V, file-drop opt-outs and the helper's desktop-only environment. Synthetic
  tests are not desktop/native composition acceptance; keep host checks minimal.
  Refusal markers may include closed fixed categories only, never raw errors,
  clipboard data or host paths; missing/strict-linked/private denials stay uniform.
  Host drop/clipboard eligibility covers valid native 2.x releases rather than
  an exact patch allowlist; retain private-client, transfer and terminal contract
  refusals. No public API proves future composer attachment behavior. Do not
  reuse this eligibility policy as a shortcut for unrelated compatibility guards.
- `ui_state.py` / `ui_state_*.js`: public-SDK container TUI/server/RPC reports,
  installed only during normal eligible V2 owning preparation, never on attach.
  Canonical stable native `2.x.y` is eligible, not proof of SDK compatibility.
  Require a compatible protocol handshake, matching generation, identity
  verification through the same private service and a snapshot recheck.
  Incompatible responses leave reports unavailable, without refreshing an
  attached owner. Keep exact routed children separate from active root
  membership. Custom client IDs are cooperative metadata, not attestation or
  host authority. Prefer the fresh owner even when blurred; missing, expired
  or ambiguous reports never justify focus guesses.
  Bound receipt freshness, retain sequence/release high-water history through
  expiry, and fail closed at capacity without evicting replay protection.
- `web_sessions.py` / `web_sessions.js` / `web_attachments.py` /
  `web_attachments.js`: one first-party tabstrip around one unmodified native
  iframe, never private native-store seeding, asset patches or nested wrappers.
  Preserve authenticated `/auth/` routing and native cookie/Origin checks.
  Explicit session selection wins; fresh reports supply ordered membership and
  exact selection. The web-only saved fallback attempts the pinned private
  native `tabs.json` layout on canonical stable `2.x.y`, rechecks state and
  verifies displayed IDs through the same private service. It never indicates
  focus, guesses a path/session, or substitutes history. Refusals retain the
  existing unavailable/index behavior and native Home option. Keep the exact
  automatic owner startup/default-session allowlist and the separate history,
  recovery, browser attachment, sharing and authentication guards unchanged.
  Public SDK and private layout compatibility are not established for all 2.x.
  Browser attachments remain explicitly enabled, trusted File-only and manually
  sent. Recheck exact live URL, document, original target and selection generation
  after asynchronous reads, including explicit away/back changes. Silent SPA
  excursions between public observations remain a documented residual race.
- `tools/test_sharing_podman.py` and its provider/archive helpers are a separate
  source-pinned host harness, not a change to an already frozen release candidate.
  Keep host guards, verified fresh private stores, controlling-PTY consent,
  unmodified production sharing/native transport, strict PASS/INCOMPLETE scopes,
  streaming encrypted-backup inspection and original failure evidence. Never run
  it inside oc-box or substitute its local/native preparation for host acceptance.
- `plugin_compat.py` / `plugin_bridge.js`: bounded copied-V1-plugin adapters on
  native V2 plugin APIs. Keep host originals and V1 behavior intact, propagate
  refusal errors and preserve attachments/structured output; reject unsupported
  hooks explicitly. Do not modify mainline tools or reactivate nested sandboxing.
- `workspace_identity.py`: generic Linux VFS directory identity, capability-based
  persistent handles and strict fallback; shared by lifecycle/status checks.
- `vault.py` / `credentials.py`: optional encrypted host vault and names-only
  per-workspace credential policy. V1 retains legacy env-file defaults; V2 without
  an explicit policy is global vault-first, with env-file fallback only when no
  vault is present. Native OpenCode saved/OAuth credential refresh/copy remains
  independent in every mode. Explicit env/selected policies remain intentional
  overrides.
- `secret_prompt.py`: controlling-TTY masked input, explicit value reveal, safe
  terminal restoration. Never reveal passphrases or accept non-TTY fallback.
- `backups.py` / `backup_crypto.py` / `backup_archive.py`: host-only encrypted
  backup/restore feature; use established age streaming encryption, no plaintext
  archive staging, fresh restore IDs, and no archived privilege reactivation.
- `host_backup_payload.py` / `host_backup_restore.py` / `host_backup_auth.py`:
  explicit CLI host migration component. Preserve bounded exact file bytes,
  read-only no-follow capture, the already-encrypted vault and independent
  native OpenCode consent. No host configuration writes from workspace-only
  restore. Never restore registry identities, an archived host database,
  external paths or plaintext archive staging. V2 credentials come from an
  explicit native export and are verified in an isolated tmpfs-backed native
  store before publication to a reviewed, absent host database. No joins to the
  live host service, silent overwrite or cross-filesystem transaction claims.
- `install.py` (under the package): copies the launcher to `~/.local/share/oc-box` and creates
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
- `tools/migrate_host_plugins.py` / `tools/host_sandbox.js`: opt-in plain-host
  V2 profile preparation; never edit originals or reuse container sandbox opt-out
  as host protection. Mocked runtime tests are not real sandbox validation.
- `tools/port_host_plugins.py` / `tools/native_plugins/`: V2-only normal-host
  plugin audit and explicit native replacement. Keep unrelated config/data intact,
  take one private safety snapshot, refuse unknown implementations/overrides, and
  never treat an inactive plugin as proof of host sandbox enforcement.
  Native host allow/deny write and network probes are accepted before and after
  reload; background cleanup remains a separate host check. Do not generalize this
  shell-tool sandbox to native file tools, MCPs, plugin processes or session PTYs.
  Runtime reset retains getConfig; keep idle ownership and serialize reset/init
  across reload, never infer an unmarked runtime owner. Host reload initially
  failed closed; the fix passes native fixtures and the host before/after retest.
- `docs/cli.md`: canonical command reference; `tools/build_docs.py` generates
  the committed man/Texinfo/Info manuals from it.

OpenCode itself and its integrations run natively inside the container. Preserve
MCPs, formatters, language servers, and plugins. Do not return to replacing
built-in tools or disabling integrations for containment. Keep mainline OpenCode
unmodified. Live sessions persist in a private named volume, and host-side
history is synchronized with supported native API/CLI export/import on exit.

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

Development version `1.7.N.dev0+gHASH` is derived from the full Git history, with N the commit
count minus the 1.0.0 baseline (29); every later commit advances N automatically.
The 1.5, 1.6 and 1.7 series deliberately retain this baseline; do not reset the patch
count or rewrite embedded 0.1/1.0/1.5/1.6 identities when preparing a release.
Keep the absolute `commit_count` for old updater compatibility, require full
history, and fast-forward tested development releases into `devel`, never
`master`. Stable final releases remain on `master`. The installer
embeds the source identity in its private `build-info.json`. Original project
code/documentation is GPL-3.0-or-later;
retain LICENSE and NOTICE when distributing/installing it.
PyPI uses the public `1.7.N.dev0` development version; retain the full `+gHASH` identity in package
resources. Python-package installations upgrade through pipx/uv/pip, never the
GitHub self-updater. Preserve the normal curl/GitHub update path in parallel.

## Approved experimental development publication

The owner approved a channel-ready successor to the separately tested
`557396562b1b835d15ffcf152428762dc27d7ecd` candidate, with a new derived
identity, development-branch publication, documentation and PyPI prerelease.
Stable v1.6.94 and `master` remain unchanged. Preserve existing source worktrees,
private evidence and the earlier unfinished/conflicted channel integration.

Development is a release channel, not an installation method: GitHub prereleases
and `.dev0` packages come from `devel`; stable finals come from `master`.
Select immutable published releases and verify source/tag/version/branch
membership. Successful explicit selections persist per verified installation
owner; previews do not mutate preferences. Keep updater downloads anonymous,
sanitized and stdlib-only, package upgrades delegated only to verified owners,
GitHub auto-update opt-in and package auto-update disabled. Preserve optional
TUI/vault extras only when supported by the selected target. A launcher downgrade
does not roll back workspace/session state.

This port changes update/install/version/publication paths, not workspace,
credential, history, sharing or dashboard behavior. Do not import unrelated
stable runtime changes merely to obtain channel support. Audit unchanged core
function/module identities against the original candidate and state the
provenance boundary honestly; a new source hash is not an old exact-source pass.
Require relevant source/package/native/build-only validation and independent
review before publishing the frozen successor. No container-only fixture is a
new external-host acceptance.

The owner explicitly intends unfinished UI validation to remain an experimental
development limitation. Keep the recorded full UI restore-verifier refusal and
unperformed portable UI/Storage/removal/final-attestation/Quit checks visible;
never relabel them passed. Existing accepted 42-check history, 12/six recovery,
13-stage vault, 37-check sharing and separate six-stage V1/V2 lifecycle reports
retain their original candidate/native identities. Do not replay completed gates
or stopped fixtures merely to finish documentation. Stable UI promotion remains
separately gated; prerelease publication is not that acceptance or a security
certification.

Publish the exact clean tested successor as one matching `v1.7.N.dev0` GitHub
prerelease and PyPI version. Preserve full-history baseline 29, all existing
tags and the stable release. The Trusted Publishing workflow must bind/recheck
source and exact artifacts, verify existing release assets, and transfer only
validated wheel/sdist bytes. Do not start a second upload. Attach that workflow's
exact distributions, identity and checksums, then anonymously compare public
GitHub/PyPI downloads. No raw private evidence or credential material is public.

Update `docs/cli.md` when command-line behavior changes, then regenerate with
`python3 tools/build_docs.py` (requires GNU makeinfo). Prebuilt manuals ship in
the repository; installing oc-box must not require documentation build tools.

## Checks

`docs/testing.md` owns the suite audit/strategy. Prefer behavior-focused tests,
normal package imports and named matrices over duplicate runtime loads, prose
snapshots or self-testing fixture setup. Do not retire a supported V1, data-loss,
secret-lifetime, identity/ownership, permission or host-evidence regression just
to lower a method count. Preserve unique cases/assertions and compare coverage;
parent-process coverage does not replace native JS, process/TTY or host checks.

```bash
uv run --no-project --with-editable . --with cryptography python -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
python3 -B tests/smoke_v2.py --opencode /path/to/opencode-v2
```

The smoke test uses temporary databases and offline fixture services, with
bounded timeouts. It does not verify actual Podman isolation. Live Podman
verification must run on the host, outside another container/sandbox.

The V2 smoke uses unmodified 2.2.6 with private servers, native tool/formatter/MCP
Code Mode execution, env delivery, credential rotation, large transcript sync,
and upstream V1 migration from a retained snapshot. V2 lacks upstream native LSP.
The 1.5 V2 release has accepted disposable host-terminal validation: TUI/web/
persistent-shell coexistence, detach/reattach, stop/resume, updated plugins,
selected-vault provider/shell/MCP delivery, rotation and attach without unlock.
Those accepted host probes do not validate the new V2 global vault default or
`--no-vault`; unit/native offline checks are not a new live Podman lifecycle pass.
The separate global-vault fixture has now passed on clean 1.5.38+g0611104
(061110489f946682a40106175ae49a1c668da94b), with OpenCode 2.2.6 and reported
Podman 6.1.4/Tumbleweed. Its reviewed report covers first global-vault import,
new-owner rotated/added entries, native Build tools with scoped denials/Plan,
running-owner rotation/reload/attach, --no-vault and full encrypted backup/restore
with stopped byte-exact state and restored latest-host-vault/history/OS checks.
This is scripted synthetic host acceptance, not an interactive TUI pass, real
provider inference, security certification or a backup of any real project.
Entry-removal and empty/absent/corrupt-vault host edge cases are outside that
1.5 run; a separate accepted 1.6.46 edge report is scoped below. Keep all earlier
failure evidence; their retries did not establish the full pass and must not be
relabeled as accepted runs.
`tools/test_global_vault_podman.py` is the focused V2-only host fixture for first
global-vault import/new project delivery, scoped Build approvals/reload, opt-out
and full backup/restore. Require private synthetic state, verified isolated
graphroot/runroot and label-checked cleanup; no real credentials/projects or
normal host service. Its scripted TUI/prompt is not an interactive TUI pass.
Do not run it in this container, move retained storage, or infer live acceptance
from offline safety/native helper tests. Leave encrypted evidence on failure.
Separate reviewed new-layout reports passed global-vault/Build/full restore on
clean 1.6.42+ge66fb39 and selected V1/V2 full/portable restore/reset/downgrade on
reported 1.6.43+ge06896f (only AGENTS.md changed from 1.6.42). Operator-observed
interactive TUI/resize/web/persistent-shell/detach/reattach/web-close/owner-wait
checks passed clean 1.6.44+ga043049: four reports returned 0, provider 9 requests,
0 errors and all three tool markers, with explicit manual confirmations.
This --no-vault helper uses production attach-only clients, not normal secondary
owner-capable routing; it does not establish interactive vault/Plan/reload,
formatter/LSP/device, real-provider or real-project restore acceptance. Preserve
each execution's source identity and all older evidence; manual observations
remain NOT_AUTOMATICALLY_PASSED in reports rather than self-certified.
Focused provider-free edge acceptance passed clean 1.6.46+gacddbd8, native2.2.6:
all ten reports/check sets passed, four owner exits 0 and six expected refusal
exits 1, no primary/secondary failures, fixture catalog 0 requests/unexpected.
It covers live/new-owner removal, initialized-empty/absent vaults, and actual
wrong/corrupt/missing-crypto start/reset preservation. Only the exact native
--version diagnostic log is separately hashed/leak-checked; all other protected
data and container-home ownership/mode/link checks remain strict. This adds no
inference, UI, backup, actual OAuth refresh, device or network-containment claim;
policy transitions and structured-auth increments remain separately unverified.
Separate image-free shared home-component ownership acceptance passed clean
1.6.48+g7df5074, count 77: all six checks true, all 30 audited helper/runtime
hashes match Git, source/destination manifest hashes equal and no failures.
This establishes measured rootless 0/1 UID/GID/mode/content transport, source
preservation and root privacy through the common archive/age/extract path only.
It is not a new per-kind full/portable container-origin/first-mount or custom-
mapping pass. Preserve the earlier lifecycle/UI/vault versions and all evidence.
The session-sync/readiness changes from d638907 first had unit/native offline
coverage only. None of the accepted a043049/acddbd8/7df5074 host reports contains
that change; matching 1.6.44 patch numbers do not identify matching source hashes.
Clean 1.6.51+gb5d1ea8 (b5d1ea87729e3f681f530c38945c119133c55b2a, count 80) then
passed three reviewed host runs with native 2.2.6: the focused history/readiness
runner (27 named checks, runner ab26dffb...0528, `tools/test_session_sync_podman.py`,
run from a separate harness checkout via --source/--expect-commit), the global-vault
fixture (13 stages) and the V1/V2 lifecycle fixture (six stages, native 1.19.5/
2.2.6, 18 synthetic provider requests; its report records the version string, not a
dirty flag). It was published unchanged as stable v1.6.51 (GitHub + PyPI). Scopes
are scripted synthetic host acceptance, not interactive TUI/web, real-provider,
network-containment or security certification. The focused runner's first attempt
(runner 1b5ce6b2...) failed on a runner oracle defect and stays failed evidence.
Build-only artifacts and mainline integration are not a new accepted host run;
repeat relevant host lifecycle and focused history/readiness acceptance on the clean
integrated candidate for later runtime changes before stable publication.
Published 1.6.51 refused a real migrated-V1 workspace: a never-completed V1
assistant survives migration, is omitted by native export, and was treated as
still changing. The fix branch archives such superseded assistants as native's
export with a count-only annotation, keeps tail/running refusals, refuses the
superseded exception while active, shows only whitelisted fixed reasons and lets
other sessions sync. It has unit and
native V1-to-V2 offline coverage only; it needs fresh host acceptance on a clean
candidate before any stable release, and 1.6.51 stays as published.
The native regression covers both imported unfinished history and actual
SIGKILL-generated V1 turns with subsequent completed turns, native destination
annotation round-trips and exact V1 reopen preservation. A real held-shell test
checks fixed bridge subprocess reasons, same-pass progress before/after the
pending session, unchanged prior archives/checkpoint and recovery. These run
directly inside oc-box with private synthetic state, not nested Podman. Activity
refusal in the export bridge applies to the superseded-assistant exception, not
universally to every export; normal owners separately wait for activity.
The later published stable v1.6.58 reports reviewed external-host acceptance on
clean 3138a8727c3638983a8b9d19b1cb93a83d94a027 (count 87): focused history/readiness
33 checks, global-vault 13 stages, and full V1/V2 lifecycle six stages including
all nine superseded-history checks. Its public release notes record that evidence;
the raw reports were not re-reviewed during the TUI release preparation. Earlier
pending/failure statements above retain their historical source and scope.
That acceptance does not cover the later Textual manager, journal or archived-root
automatic-resume changes. Freeze their integrated candidate and repeat relevant
host validation before the next stable release; do not infer acceptance from the
v1.6.58 tag or a successful real-project terminal attachment alone.

Focused post-stop snapshot/stop-cancellation host acceptance is now reviewed on
clean d736bc1bb33ffeedee65ad3ec281de77c1e31847 (1.7.86+gd736bc1, count 115),
native 2.2.6. The external 60df669 v2 runner (SHA-256 56668d84...356d20) passed
all 18 checks across two fresh roots; all 44 audited runtime/donor hashes match
Git. Owner exits are [0,1] in each case, explicit stop 0; snapshot restart/reader/
completion are 1/1/1, cancellation 0/0/0, with no preparation/auth calls or extra
synthetic provider/shell execution. See docs/testing.md for exact evidence hashes.
This is focused scripted synthetic host acceptance only, not native job cleanup,
the baseline 42 checks, lifecycle/vault/UI, real inference, exhaustive fault/race
or native 2.2.7 host acceptance. The earlier v1 oracle attempt remains failed;
its later fixture-only correction and fresh successes do not relabel it. Keep raw
reports private and retain every store; repeat remaining relevant integrated
candidate gates before stable publication.

The separate default history/readiness report is also now reviewed on that same
clean d736bc1 runtime, all 44 identical audited hashes, external 60df669 runner
and native 2.2.6: all 42 checks true, ordered exits [0,0,0,1,0,0,0,0], no primary/
secondary failures or trace. Report SHA-256 9c94916f...49619 is recorded fully in
docs/testing.md. Combined accepted scope is 60 scripted synthetic host checks
(42 baseline + 18 focused recovery), not lifecycle/vault/backup, interactive UI,
real-provider, network-containment, exhaustive fault/race or native 2.2.7 host
acceptance. Preserve the earlier failure. The user approved integrating the fix
into feature/release-1.7 for the combined 1.7 rollout (fast-forward through
dcc0482); no standalone fix release, master advancement, tag or publication is
authorized by that integration step. Keep release-facing behavior and both
12/six recovery gates in the 1.7 notes/checklist, and retain separate final-source
publication gates and exact historical evidence identities.

The global-vault fixture's initial host run failed during the first owning
launch, not an accepted pass.
Use --diagnose for validated read-only checkpoints; never print raw errors/env
or treat inspection as replay/acceptance. Keep the retained fixture intact.
Store refusals retain fixed-category selector/native version/info diagnostics,
with both private config selectors on direct native queries. Never inspect
resources after an initial refusal or use probe success to bypass validation;
no raw stdout/stderr, config rewrite, engine reset or workspace launch.
Compare the retained engine's cached/current boot IDs using files before any
engine command; report booleans only. Refuse stale/absent/invalid retained
markers for diagnose/retry/cleanup, without deletion, marker rewriting or store
movement. Disk-backed fixture runtime state is original-boot-only; a fresh
synthetic fixture is separate acceptance, never a replay of retained evidence.
Podman info can initialize/refresh runtime metadata, not guarantee byte-level
immutability. New first-launch failures retain fixed primary substeps separately
from cleanup/reporting errors; never overwrite an earlier failure report.
Native API CLI success with no body (HTTP 204 reply/delete/reload) returns None,
matching the HTTP bridge; preserve failed-command and malformed nonempty JSON
errors. Native fixtures exercise that CLI transport, not only HTTP. Repropagated
exception identity must not create a false failure after completed shutdown.
First-owner host-clean checks run after synthetic ambient controls are removed;
also check vault/cache retention before restoring that environment so fixture
restoration cannot hide an inserted vault entry. Never weaken the leak guards.
--retry-source is limited to a stopped baseline-ready, already-imported source
with verified private store/identity and no later backup/restore evidence. Use
new companion synthetic vault/config/report files; never overwrite original
ciphertext/seed/config, reimport, reset, purge, or claim the full fixture reran.
Keep first-failure diagnostics separate from shutdown/journal failures and
report fixed substep/category/exit data only, never private stderr/env/argv.
Recorded secondary fixture roots may be selected by normal resume: validate
their actual root/project metadata and retain their messages, while checking
the verified seed explicitly. Never accept unrecorded/foreign/child selection,
rewrite the last-used pointer, reimport, or infer acceptance from this retry.
A disk-backed fixture passed full/portable restore and reset; a targeted retry
passed exact V1 history preservation, inactive-major database-byte preservation
and V2 return with retained native messages plus synthetic tool execution. The
retry did not repeat earlier restore/reset stages. Keep native V1/V2 integration
enabled on master and pull requests. No real workspaces in tests.
`tools/test_v2_podman.py` is the opt-in host acceptance fixture for these
checks: isolated synthetic vault/provider/MCP, native version executables, real
rootless backup/reset/downgrade. Prompt/TUI actions are scripted; do not claim a
live pass from its offline unit tests or run it inside this container.
Native V2 fixture `run` calls require explicit stdin EOF: Podman exec supplies a
FIFO that upstream otherwise waits to read before inference. Source-only retry
must validate its private registry/graphroot/runroot and refuse backup evidence.
Full-restore diagnostic retry preserves encrypted evidence, authenticates the
image component to a discard stream and refuses an existing destination. Pipeline
errors report fixed categories/exit codes only, never raw private stderr.
Host acceptance `--work-dir` creates fresh disk-backed fixture storage, while
`--temp-dir` redirects only private temporary files through honored `TMPDIR`.
Never move a retained graphroot/runroot or overwrite encrypted evidence to retry.
Native V1 reads update project/access metadata. After V1 itself reopens its DB,
verify exact retained messages/parts, not physical DB-byte equality; require the
inactive major's DB bytes unchanged. Late-stage downgrade retry validates all
three stopped fixture workspaces, encrypted archives and original import evidence,
and must not claim earlier restore/reset checks were rerun.

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

The existing final Build-agent wildcard overrides copied ordinary global/Build
tool permission rules, including their configured denies, as before; native
smoke deliberately checks those inherited denies becoming allow. The new V2
hook changes only final asks for the resolved actual Build agent (`ask` to
`allow`), never a deny passed to it. Preserve explicit session/mandatory-policy
denials, native unknown-agent ask/deny outcomes, managed rules after the private overlay
and all non-Build/Plan/child rules. Explicit event agent wins;
omitted agent resolves current session then native configured default, never
parent. Never overwrite session permission arrays or leak Build allowances into
Plan/child rules. Question/MCP elicitation prompts are not permission asks.
The final private OPENCODE_CONFIG_CONTENT overlay reenables only the generated
Build helper against lower-priority project plugin removals; managed policy
still wins. Do not assume an external agent transform runs after all config.
Keep V1 behavior, Plan/subagent rules and plain host permissions unchanged.
Do not enable blanket native V2 client autoaccept or modify normal host
`cli.json` to fix a Build-only prompt regression. Native `--auto` and TUI
`session.permissions: autoaccept` approve asks, not explicit denials; plain-host
`--auto` still respects configured denies. Optional
plain-host `opencode --auto --standalone` is a user choice, not an oc-box command
or vault consumer. No `--auto`/client autoaccept flag is added to oc-box clients.
New native evaluator/import/reload fixtures do not establish live host acceptance;
the separate scripted global-vault host fixture covers native tool/permission
and reload checks, but no new interactive TUI permission-prompt/reload pass is
accepted yet.

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
never unlock. V2 global mode selects all current encrypted entries on each new
owner, including the first launch; never save an automatic name list. It replaces
ambient suffix/pass_env forwarding when a vault is present. Preserve supported
native OpenCode saved/OAuth credential refresh and copying in every mode: global
mode governs env-variable entries, not structured OAuth records.
Suppress legacy ambient token/env overrides, including OPENCODE_AUTH_CONTENT,
in configured global mode; retain native auth integration in every mode. Never
import plaintext opencode.json secrets or rewrite them into the vault.
Existing copied/container config/auth secrets remain independent; mode changes
do not revoke credentials.
Empty initialized vaults unlock and deliver no vault entries; absent vaults give
env-file compatibility fallback
and setup guidance, never automatic installation/config or JSON-secret import.
Wrong passphrases, corrupt configured vaults, missing crypto and wrong/missing
selected credentials must abort before setup/reset deletion, never silently
fall back. Saved vault mode on V1 uses legacy env forwarding without global
unlock; explicit old env/selected vault selections retain their behavior.
Vault mode refuses nonempty per-name lists; switching selections requires
`--credential-mode vault --clear-credentials`. V2-only `--no-vault` is ephemeral
for one owning start/reset/web invocation, skips vault entries while retaining
explicit env constraints, and is refused on attach and unrelated commands.
Attach never unlocks/refreshes. The vault/passphrase are never mounted; delivered
values remain readable by the agent/tools in private container environments.
Plain host `opencode --standalone` uses native host env/auth, not the oc-box vault;
oc-box continues to own its private foreground V2 workspace service.
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
Workspace-only backups retain credential modes/policies but exclude the global
host vault and outside env files. Restored default/global V2 uses the restoring
host's vault, not old host privileges; persisted workspace config/auth may
still contain secrets. The explicit `--host` component can carry the encrypted
vault unchanged, with the original passphrase still required after restore.
