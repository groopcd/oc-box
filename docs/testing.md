# Test strategy and suite audit

Tests protect behavior, not a target number of methods. A validation table can
have many necessary negative cases; moving them into one method is consolidation,
not permission to stop checking those cases. V1 compatibility is supported code,
not a deprecated feature that can be removed from the regression suite.

## Layers

- **Unit/contract tests:** public CLI/config behavior, failures before destructive
  actions, ownership/identity checks, immutable installations and secret lifetime.
  Import the normal `oc_box` package; avoid private duplicate copies of runtime
  modules, paragraph-by-paragraph documentation assertions or tests of unittest's
  own setup/tempfile behavior.
- **Real local process tests:** age streaming/envelopes, controlling-TTY input,
  terminal restoration, websocket framing and filesystem archive defenses. These
  do not become redundant merely because a parent-process coverage report does
  not instrument their children.
- **Native offline integration:** unmodified OpenCode V1/V2, tools/MCP/formatter,
  history import/export/migration, permission evaluation, plugin compatibility,
  credential delivery and terminal/browser coexistence. Keep actual CLI transport
  checks as well as HTTP helpers: HTTP-only tests missed the 204-response bug.
- **Packaging/distro CI:** build and install wheel/sdist artifacts, check their
  real metadata/resources, and exercise all seven dependency catalogs. Preserve
  the released updater against the new src layout, not just the current updater.
- **Opt-in host acceptance:** synthetic, isolated rootless-Podman lifecycle and
  encrypted backup/restore. Never run these fixtures inside this container or use
  real workspaces/credentials. Native/offline results do not establish a new live
  host pass; retained evidence and each accepted scope remain separate.

### Reviewed host reports — 2026-11-09

These are user-supplied reports reviewed here, **not executions from this
container**, and do not replace earlier 1.5 evidence:

- Global-vault delivery, Build tools/permissions and full encrypted restore were
  accepted on clean **1.6.42+ge66fb39**, with native OpenCode **2.2.6**.
- Separate selected-vault V1/V2 full/portable restore, reset, downgrade and V2
  return were accepted on reported **1.6.43+ge06896f**, with native OpenCode
  **1.19.5 / 2.2.6** and **18 synthetic provider requests**. That source revision
  differs from 1.6.42 only in `AGENTS.md`; keep the execution versions distinct.

- A separate **operator-observed interactive** run on clean
  **1.6.44+ga043049** passed native Build tool execution without permission or
  credential prompts, TUI resize, simultaneous browser/TUI/persistent-shell use,
  shell-state retention across detach/reattach, independent web closure and owner
  waiting for the final shell lease before normal sync/shutdown. Four reports
  (owner, two terminal attachments and web) returned exit 0 without secondary
  errors; the synthetic provider recorded **9 requests, 0 errors** and all three
  tool markers. The helper requires native OpenCode 2.2.6.

The first two runs use scripted TUI/prompt input. The third combines reports
with the operator's explicit stepwise confirmations: its unchanged
`NOT_AUTOMATICALLY_PASSED` field correctly avoids self-certifying observations.
It uses `--no-vault` and production attach-only client helpers, not ordinary
owner-capable secondary CLI routing. It does not cover interactive vault input,
Plan/deny conversations, reload permission prompts, a second full TUI attachment,
formatter/LSP/devices, real-provider inference or security certification.

### Fresh manual UI observation (host only)

`tools/test_interactive_podman.py` is a bounded **interactive-only** fixture, not
another full vault/lifecycle acceptance run. Use an unprivileged host terminal,
a clean full-history source checkout, explicit unmodified OpenCode 2.2.6 and an
existing disk-backed work parent. Never run it inside oc-box or reuse completed
fixture roots. No real project, operator credentials or vault unlock is needed:
the owning start explicitly uses `--no-vault` and a synthetic `_API_KEY`.

```bash
python3 -B tools/test_interactive_podman.py start \
  --work-dir /path/to/fixtures --opencode-v2 /path/to/opencode-2.2.6
```

Optional `--temp-dir /path/to/staging-parent` changes only private temporary
placement. Both work and staging parents must be outside retained global-vault,
V1/V2 and backup fixture roots and their descendants, including renamed roots
with fixture evidence markers. No fixture files/store are moved or removed.
The foreground owner prints its fresh exact root and secondary commands;
wait until its TUI is ready, then use those commands in other host
terminals. Only the single synthetic `source-a` workspace is allowed:

```bash
python3 -B tools/test_interactive_podman.py tui --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py terminal --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py web --fixture /path/to/printed-root
python3 -B tools/test_interactive_podman.py stop --fixture /path/to/printed-root
```

Secondary commands require the unchanged source, original boot, verified private
store/resources and a live authenticated owning service; none starts a new
owner, refreshes credentials or accepts `--no-vault`. They dispatch directly to
the production authenticated client helpers, never the owner-capable launcher;
a lost/stopped service fails the client without ownership fallback. Do not
pull, edit or switch the source checkout while the fixture is active: successful
owner/client returns recheck the initial source identity after lease drain and
refuse a changed/dirty checkout without relabeling the recorded execution.
TTYs are required for UI paths. Web always uses `--no-open`: manually open its
private one-use pairing URL
without sharing or recording it. The provider stays alive in the owning
foreground process while its launcher and attached leases remain active.

Use **Build**, with one or two short synthetic prompts only (12 provider requests
per owner window). The unchanged deterministic provider exercises shell, MCP and
the permission tool; it does not support Plan/deny conversations. Manually record
TUI rendering/input, concurrent web/TUI/shell use, Ctrl-] shell detach/reattach and
history/shell-state continuity. Close clients before exiting the owner; `stop`
is an explicit verified-source stop, not cleanup, reset or resume. A later owning
observation requires another fresh fixture. Keep retained files/store in place.

Private `interactive-*-report.json` files record source identity, actual
launcher/client outcomes and owner provider counters/markers, never synthetic values or control
credentials. They explicitly leave manual observations **NOT automatically
passed** and never write the global fixture's `result.json`. Vault rotation,
interactive permission prompts, formatter/LSP/device checks and other lifecycle
acceptance remain separate. Offline `test_interactive_host_fixture` checks these
guards and native relay dispatch, not real Podman/UI acceptance.

## Audit result

The audit reduced 558 unit-test methods to 539 across the same 32 files. The
reduction is intentionally modest: the remaining similar-looking tests protect
different safety/compatibility boundaries. It removed repeated
prose/version assertions, a test of its own temporary-directory setup and
installer checks already covered by the stronger src-layout installer contract.
Repeated cache invalidation, pipeline error, API response and filesystem identity
cases are now named data matrices with their positive/negative regressions kept.
Shared runtime modules are imported normally instead of loading another private
copy in each test file. Unused imports were removed from tests/native fixtures.

Parent-process coverage was measured before/after using the same Python 3.13
environment and `branch = True`, `source = src/oc_box,tools`. Every previously
executed source line and branch remains exercised: 5,704 executed lines before
and after; 1,983 branches before and at least 1,984 after. This is about 68%
combined statement/branch coverage across runtime **and host/developer tools**,
not 100% application coverage. It excludes child processes and JavaScript, so
coverage equivalence is a cross-check, not the sole reason to retire a test.

## Ownership map

All unit-test files were inventoried against their behavior/risk and coverage.
The following groups remain intentionally distinct; an inner validator test and
a CLI abort-before-deletion test protect different boundaries.

| Files | Responsibility / audit decision |
| --- | --- |
| `test_backup_archive` | Keep traversal, hardlink/symlink, special-file, size, ownership and permission defenses. |
| `test_backup_crypto` | Keep real age/envelope, private-memory and TTY contracts; consolidate stage/category/redaction failures into one matrix. |
| `test_backups` | Keep full/portable transport, activation races, rollback/ownership, source preservation, privilege/environment stripping and no-unlock guards. |
| `test_completion` | Keep parser-driven completion and real Readline behavior; they exercise different interfaces. |
| `test_dependencies` | Keep read-only doctor and feature-specific fail-before-change guards; distro installation is a separate CI layer. |
| `test_documentation` | Keep executable config examples, parser/reference coverage, generated source/rendering/reproducibility, privacy and installation contracts; remove repeated prose assertions and arbitrary paragraph lengths. |
| `test_global_vault` | Keep policy/version precedence, dynamic selection, opt-out/overrides, authenticated content, size budgets and cache/error cleanup. |
| `test_global_vault_launch` | Keep first-owner/import/reset/attach ordering; remove setup self-test. |
| `test_global_vault_host_fixture` | Keep fixture isolation, stale-boot/selector guards, first-failure tracing, refusal-before-replay and immutable evidence; these protect destructive host tooling. |
| `test_host_plugin_migration`, `test_native_host_port`, `test_plugin_compat` | Keep normal-host/copy compatibility boundaries, refusal, media/UTF-8 preservation, SDK/schema and reload regressions. Do not infer real host enforcement from mocked sandbox engines. |
| `test_oc_box` | Keep launcher/container/config/limits/history/reset/purge behavior; remove the weaker duplicate installer smoke. |
| `test_opencode_compat` | Keep major/version routing, private auth/env transport, migration, database preservation and SDK cache regressions. |
| `test_packaging`, `test_src_layout` | Keep console/build-channel and src/legacy/editable/resource contracts; the src installer and artifact checker own runtime-file completeness. |
| `test_podman_options` | Keep literal argv, managed-option rejection, SELinux/device diagnostics, command scoping and reset persistence. |
| `test_process_io` | Keep large regular-file capture and cwd/PWD coherence; private capture/redaction has separate credential contracts. |
| `test_secret_prompt` | Keep editor/paste/byte-limit and real TTY/echo/cancellation/resize regressions, not just masked rendering. |
| `test_session_import` | Keep exact transcript/ID, namespace/origin, duplicate/no-overwrite, known normalization and failed-import recovery regressions. |
| `test_session_terminal`, `test_terminal`, `test_workspace_service` | Keep persistent versus maintenance-shell authority, frame validation, client leases, auth/env isolation and lifecycle locks; consolidate API response outcomes. |
| `test_stale_workspaces` | Keep nondestructive purge/provenance and abort-on-failed-sync/copy recovery boundaries. |
| `test_startup` | Keep warm cache adoption, SDK/lock/config safety and TUI relay; consolidate cache invalidation causes with fresh fixtures per row. |
| `test_status` | Keep honest unavailable metrics, no foreign/project scans, selector behavior and read-only output. |
| `test_updater` | Keep anonymous verified downloads, atomic release switching, failure/timeout/no-downgrade, opt-in scheduling, old callers and bootstrap isolation. |
| `test_v2_host_fixture` | Keep synthetic provider/version/EOF, store/temp/restore/downgrade/evidence safeguards; offline checks are not lifecycle acceptance. |
| `test_vault` | Keep crypto/private files/TTY, explicit named policy, no secret argv/disk/env, no-unlock and fail-before-reset/activation; name the default-env contract explicitly as V1. |
| `test_version` | Keep full-history/legacy identity, installed provenance and licensing; consolidate missing/invalid metadata fallback. |
| `test_workspace_identity`, `test_workspace_repair` | Keep generic VFS capability/fallback, read-only versus locked upgrades, marker/ownership/identity recovery and no replacement/deletion. Consolidate strong-identity mismatch cases, including inode-256 subvolume and invalid-baseline regressions. |

## Reviewing future changes

1. Identify the user-visible contract or named bug a test protects and its owner.
2. If it duplicates an existing test, move any unique input/assertion to that
   owner before deleting it. Give matrix rows explicit names and fresh mutable
   fixtures; do not combine unrelated behaviors into a giant test.
3. Preserve both success and failure paths, timing of refusal, cleanup and
   byte/message/permission invariants. Assert effects, not just mock call counts.
4. Compare coverage and rerun native/process/package layers affected by the edit.
   Do not hide coverage loss by adding exclusions or waiving critical guards.
5. Do not delete a supported V1/legacy/restore regression just because its input
   is old, or relabel previous host failures as accepted results.

Run the standard commands in `CONTRIBUTING.md`. For an optional coverage audit,
use `uv run --no-project --with-editable . --with cryptography --with coverage`
and `python -B -m coverage run --branch --source=src/oc_box,tools -m unittest`
with `discover -s tests`; store coverage data outside the project/HOME. Coverage
is a developer tool, not a runtime dependency or replacement for native tests.
