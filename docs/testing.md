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
