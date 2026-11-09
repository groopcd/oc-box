# Contributing

Use the full Git history: the release identity is `1.6.N+gHASH`, with N the
commit count minus the 1.0.0 baseline (29). Keep the absolute `commit_count` in
metadata for upgrade compatibility with 0.1/1.0/1.5 releases. The 1.6 refactoring
release preserves that patch-count baseline, just as 1.5 did. Changes should keep
mainline OpenCode unmodified and preserve MCPs, formatters, language servers, plugins, and
the host/container storage boundary.

## Source layout

Application Python modules and the plugin bridge live in `src/oc_box/`.
`src/_oc_box_build.py` contains the build integration. Tests, developer/host
fixtures, documentation and completions remain in `tests/`, `tools/`, `docs/`
and `completions/`. Root `install.py`, `install.sh` and `setup.py` remain small
compatibility/build entrypoints: older GitHub/curl callers must still work.
Canonical docs/licenses stay at the root and are bundled into wheels/releases
by the build/installer, not duplicated in the source package.

Use an ephemeral editable environment to run the source CLI without creating a
project or home venv:

```sh
uv run --no-project --with-editable . oc-box --version
uv run --no-project --with-editable . python -B -m oc_box --help
```

The stdlib-only alternative is `PYTHONPATH=src python3 -B -m oc_box`. Native
fixture scripts add the source package path themselves. Existing installed
launchers keep their immutable flat GitHub-release/package-resource layouts.
Editable source installs are package-managed and cannot invoke the GitHub
self-updater; update the checkout and rerun the development command instead.
Ignored `.venv`, bytecode and egg-info directories are not source files and
must not be deleted or moved merely to make this refactor's tree look cleaner.
Offline src-layout checks do not establish a new live Podman lifecycle pass.

## Checks

See [the test strategy and audit](docs/testing.md) before adding/removing tests.
Keep distinct data-loss/security regressions; consolidate repeated cases and
fixture/prose checks, not an arbitrary number of test methods.

Python 3.10+ and stdlib run the base tests. Full vault coverage needs the optional
cryptography library; backup integration tests need host age/age-keygen. CI
installs age explicitly. The mainline smoke test also needs Git and OpenCode
(integration checked with 1.19.5):

```sh
uv run --no-project --with-editable . --with cryptography python -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
python3 -B tools/build_docs.py --check
git diff --check
```

Tests use isolated temporary app data. Mainline model/MCP/LSP/formatter fixtures
are local and bounded. These checks do not replace live host-side Podman tests.
Do not run host lifecycle tests from inside the project's running container.

For OpenCode 2.x compatibility, keep a separate native binary and run
`python3 -B tests/smoke_v2.py --opencode /path/to/opencode-v2`. The 1.5 release
retains 1.x support and has accepted disposable host-terminal lifecycle,
selected-vault credentials, backup/restore, reset and downgrade validation.
See `docs/opencode-2.md` for upstream differences and the private-service env
strategy. Never test against real host service/config/data from this container.
`plugin_bridge.js` adapts only its enumerated V1 hooks. The offline Node fixture
checks refusal and attachment/structured-output preservation; the native V2
smoke exercises synthetic hooks and the sandbox package's container opt-out.
To inspect installed local plugins without changing them, optionally add
`--legacy-plugin-dir /path/to/copied-v1-plugins` to the V2 smoke. It copies source
files into temporary storage and never uses host provider credentials.
V2 shared-client tests use real temporary native servers and local Podman-transport
substitutes for authenticated browser pairing/cookies, same-session/API process
identity, event streaming and binary upgrades. Unit tests cover same-user leases,
concurrent attach without unlock, owner wait ordering, maintenance exclusion and
port failure. Native session-shell PTY tests verify persistent identity, viewport,
detach/reattach and shell state alongside web access. Host-terminal web/TUI/shell
coexistence is accepted. Copied plugin tests cover Unicode byte
budgets, primary-request JSON schemas, configured sources outside autoload
directories, PDF/history transforms and model-variant request rewriting.
Native V1/V2 integration runs continuously on master, feature branches and pull
requests; package checks assert the compatibility guide, V2 runtime resources and
offline JS fixtures ship through their intended distribution channels. Accepted
host TUI/web/shell, plugin and selected-vault/backup/reset/downgrade tests are recorded in
`docs/opencode-2.md`. Repeat relevant host checks before releasing lifecycle or
credential changes. Use `tools/test_v2_podman.py` for host acceptance; setup and
precise scope are in `docs/opencode-2.md`. It uses isolated synthetic resources,
scripted prompt/TUI actions and real rootless lifecycle/native inference fixtures.
Never run it under sudo, inside oc-box, or against real workspaces.
Use `--work-dir` for a new fixture's isolated image store and `--temp-dir` for
private temporary-image/encrypted staging placement when system temp is limited.
Neither option moves retained Podman storage; retry preserves encrypted evidence.
`--retry-downgrade` completes a retained three-workspace fixture's V1/V2
transition after restore/reset. Native V1 may update project/access metadata on
read; compare exact messages/parts after reopening while keeping the inactive
major's database bytes unchanged. Earlier restore/reset evidence is not rerun
or relabeled by that targeted retry.

### V2 global-vault acceptance and additional host checks

Use the focused V2-only host fixture before important project imports:

```sh
uv run --isolated --no-project --with cryptography python -I -B tools/test_global_vault_podman.py \
  --opencode-v2 "$(command -v opencode)" \
  --distro tumbleweed --work-dir /var/tmp --temp-dir /var/tmp
```

It creates fresh private project/history/vault and Podman graphroot/runroot
state, validates store isolation before resources, and uses a synthetic local
provider/MCP. No installation, real credentials or normal host OpenCode service
is used. TUI and password input are scripted; the owning launcher, decryption,
native tools, full backup/restore and resource lifecycle use production code.
Run only on the regular host, never inside this container or another shell
sandbox. Existing disk-backed work/temp parents must have sufficient free space
and quota. Keep reports/evidence until reviewed; the printed cleanup command
only removes validated fixture-owned resources, retaining its files/archives.
See `docs/opencode-2.md` for precise scope. Preparing this fixture is not a live
host pass and does not repeat the older V1/selected-vault acceptance stages.

V1 keeps legacy env-file defaults. V2 without an explicit policy is global
vault-first; saved env/selected policies remain intentional overrides. Unit and
native offline checks for the new global default and `--no-vault` must be
distinguished from the **earlier accepted selected-vault host probes**. No new
result should be inferred from those older probes. A separate full scripted
global-vault host run passed on clean `1.5.38+g0611104`, using OpenCode 2.2.6 and
reported Podman 6.1.4/Tumbleweed. Its reviewed report covers first launch/import,
new-owner rotation/additions, native Build tools and scoped denials, reload/attach,
`--no-vault`, and full encrypted backup/restore. It is not interactive TUI
acceptance or a real-project backup; see `docs/opencode-2.md` for exact limits.
For relevant changes, repeat isolated host validation. Additional host edge
cases below are not all covered by that accepted fixture; never use real
vault/workspaces:

- Initialize/populate a vault before the first project launch using the
  `oc-box[vault]` extra or curl `--with-vault`; no project registration/selection
  should be required for all current entries to reach provider/MCP/shell.
- Verify dynamic values/additions/removals on the next new owner, with no frozen
  automatic name lists. Configured global mode replaces ambient suffix/pass_env
  forwarding, including legacy `OPENCODE_AUTH_CONTENT` overrides. Native
  OpenCode saved/OAuth credential refresh and copying must remain independent
  in every mode, including global vault mode: env entries do not replace
  structured OAuth refresh-token records. Verify a vault with only a synthetic
  search key does not disable saved provider auth. Never import or rewrite
  plaintext OpenCode JSON secrets. Use published V2
  `providers.PROVIDER.settings` / `mcp.servers.NAME.environment` references,
  never literal secret values in JSON.
- Check absent-vault env-file fallback and guidance, empty-vault unlock
  with no entries, and fail-closed wrong-passphrase/corrupt/missing-crypto errors
  before setup/reset deletion. Never install crypto, edit config or import JSON
  secrets automatically.
- Check explicit env/selected overrides, vault-mode refusal of nonempty name
  lists and `--credential-mode vault --clear-credentials` conversion. Runtime V1
  with saved vault mode must use legacy env without global unlock; old explicit
  selections must still work.
- Check one-owning-start/reset/web `--no-vault` without policy persistence,
  retaining explicit env constraints. Attach rejects it and never unlocks or
  refreshes; info/maintenance/sync/backup and other unrelated commands reject it.
- Verify values remain only in host memory/private container environments,
  never argv, host os.environ, service config, registry, or mounted vault files.
  Backups retain policy but exclude global host vault/outside env files;
  restored default/global V2 uses the restoring host vault, not old privileges.
  Persisted workspace auth/config and agent-written credentials remain sensitive.

Plain host `opencode --standalone` uses native host env/auth, not the oc-box
vault; do not add an oc-box standalone command or replace its private foreground
V2 owner service. Source env files only for one-off compatibility, not the primary
V2 workflow. Report unit/native offline and live host results separately.

Keep the existing final Build-agent wildcard override of copied ordinary
global/Build tool rules, including their configured denies, as before. Native
smoke deliberately checks inherited ordinary Build/global denies becoming allow.
The new V2 hook must change only final Build asks (`ask` to `allow`), never
overwrite a deny passed to it, and preserve explicit session/mandatory-policy
denials, native unknown-agent ask/deny outcomes, managed rules after the private overlay
and all non-Build/Plan/child rules. Explicit event agent wins; omitted agent
resolves current session then native configured default, never parent.
Keep Plan/child rules and session
permission arrays intact: overwriting those arrays leaks Build allowances into
other requests. Question and MCP elicitation prompts are not permission asks.
V1 behavior remains unchanged. The final private `OPENCODE_CONFIG_CONTENT`
overlay reenables only the generated Build helper against lower-priority project
plugin removals; managed policy still wins. Do not assume an external agent
transform runs after all config sources. Cover native evaluator, import and
reload boundaries in offline fixtures; they are not a new live host pass.

Use no host `cli.json` changes, `--auto` on oc-box clients or blanket all-agent
client autoaccept. Native V2 `--auto` and TUI
`session.permissions: autoaccept` approve asks, not explicit denials (plain-host
`--auto` still respects configured denies); an optional
one-off host `opencode --auto --standalone` is a user choice, not the oc-box
default. The scripted host fixture covers native permission/tool/reload checks;
new interactive TUI permission-prompt/reload validation has not been accepted.

## Plain host plugin migration

`tools/migrate_host_plugins.py` creates an opt-in independent native V2 host
profile; it never edits the source profile, host databases, or installed CLI.
`tools/host_sandbox.js` is a separate host shell-tool port using the maintained
sandbox runtime, not the container opt-out bridge. Native profile tests use a
mocked runtime; real host filesystem/network enforcement remains a manual
acceptance check. Foreground native host project/outside-project write and
allowed/disallowed network probes are accepted before and after reload; background
behavior remains a separate host check. The first host reload failed closed
because SDK reset retains its config object; native same-process reload fixtures cover idle lease
ownership and reset/init serialization, and the corrected host reload passed. See
`docs/host-plugins.md`. Keep defaults fail-closed, avoid
Build permission overrides, preserve source-relative supporting files, and
reject unsupported migration paths instead of silently disabling protection.
`tools/port_host_plugins.py` is the V2-only path: reviewed known-source replacements,
an inactive private safety snapshot, direct V2 implementations under
`tools/native_plugins/`, and no V1 SDK/bridge. Retirement decisions require source
analysis and behavioral proof, not plugin status alone. Never remove unknown
plugins, actual audit overrides, auth files, databases or the unused alternate
profile automatically. Native smoke tests mock the sandbox engine, not host isolation.

## Bash completion

Bash completion lives in `completions/oc-box`, ships in both distribution
channels, and can be emitted with `oc-box completion bash`. Keep its static
commands/options/choices aligned with the parsers. `tests/test_completion.py`
checks that coverage and exercises real Bash with no external commands on PATH,
including context, equals syntax, paths with spaces, and installation. Lint with
`bash -n completions/oc-box` and `shellcheck --shell=bash completions/oc-box`.
Completion must never invoke lifecycle commands, a secret prompt, or provider
queries; open-ended identifiers are left for the user to enter.
Include `vault` as a credential-mode choice and `--no-vault` only in owning
start/reset/web contexts (including implicit start). Static completion does not
probe the OpenCode major or active service; runtime validation rejects V1/attach.

## Distro dependency checks

`.github/workflows/distros.yml` tests all seven catalog images in disposable
containers. It installs mapped packages from `dependencies.py` using actual
package managers, checks command providers (including private Podman helper
paths and GNU du), tests an age recipient round-trip, and checks doctor output.
This is package/dependency validation, not nested rootless Podman validation.
Run only in a disposable root container with a read-only checkout:
`python3 -B tools/test_dependency_distro.py --distro NAME --install`.
The fixture intentionally needs no Python packages. Package-map changes must
pass the distro matrix before release; keep host guidance separate from the
workspace baseline and Python-tool extras.

## Live backup/restore validation

Full and portable backup/restore passed the host fixture and were accepted for
the 1.0.0 release. This exercises real rootless Podman and age with a synthetic
SQLite database, rather than a restored project's live model inference. Repeat
it for changes to backup transport, restore lifecycle, or host ownership.
Do not reset/purge real projects to validate it.
From an ordinary Linux **host** terminal, without sudo, install the age package
and ensure rootless Podman works. Then run the fixture from the repository root:

```sh
python3 tools/test_backup_podman.py
```

It uses a fresh temporary directory and isolated `OC_BOX_STATE_HOME`, creates
only random workspace labels, uses a small Alpine container, and supplies no
OpenCode/provider credentials or vault access. It seeds a synthetic SQLite DB,
private-home auth sentinel, project/Git/untracked edits, and modified system
files. Both full and portable backups use real age recipient encryption,
Podman commit/save/load, rootless home ownership, and stopped container restores.
It verifies checksums, session IDs, internal-path binding, expected OS-file
preservation/omission, and that original project/container data remains intact.
It never invokes normal OpenCode setup or uses real project sessions.

On success/failure it prints its retained fixture directory. Keep that output
for review. Remove only its owned test containers/volumes using the exact path
printed by the test:

```sh
python3 tools/test_backup_podman.py --cleanup /tmp/oc-box-live-backup-EXAMPLE
```

The test-owned backup archives/key and host files remain for manual inspection;
they are synthetic, private, and not release artifacts. Cleanup does not run
global prune or touch registered real projects. CI/offline fixtures exercise
actual age/archive pipelines and substitute only Podman transport; they cannot
certify kernel isolation, SELinux, image loading, or UID mapping on your host.
If the live check fails, stop and retain the test environment for diagnosis.

The accepted host validation covers project/Git edits, private-home/database
bytes, rootless ownership, saved session selection, retained internal paths,
source preservation, stopped restores, and the expected full/portable OS-state
differences. Fixture paths, keys, IDs, and archives are private test data, not
public documentation or release assets. Pushing a branch does not publish to PyPI.

## Manuals

Edit `docs/cli.md` when command-line behavior changes. Generate committed man,
Texinfo, and Info files with GNU makeinfo installed:

```sh
python3 -B tools/build_docs.py
```

End-user installation copies prebuilt manuals and must not require build tools.
Optional man/Info rendering checks run when groff and GNU info are available.
Info output is byte-reproducible with the same makeinfo version. Different
Texinfo releases may change their banner, wrapping, typography, and tag offsets;
tests verify matching navigation across versions rather than requiring identical
binary output from every distribution's documentation toolchain.

## Source and release hygiene

- Stage intended files explicitly. Never commit auth files, tokens, private keys,
  session databases/exports, registry metadata, caches, or sandbox placeholders.
- Review Git history as well as the working tree before a first public push.
  With Gitleaks installed, run `gitleaks git --log-opts=--all --redact .` and
  `gitleaks dir --redact .`. Scan reports belong outside the repository.
- Shell calls should use argument arrays. Forward credentials through the
  process environment, never command-line arguments or credential-bearing URLs.
- Add regression tests for boundary changes, import compatibility, lifecycle
  preservation, and actual command behavior rather than implementation mirrors.
- Keep examples portable and use synthetic session IDs.
- Disclose AI assistance honestly. Check each hosting service's policy before
  publication; a GPL license does not override a service's content rules.

Original contributions are licensed GPL-3.0-or-later. Retain LICENSE and NOTICE.

## Python packages and PyPI releases

The wheel contains a namespaced `oc_box` package, runtime helper scripts,
prebuilt manuals, license/notice, and immutable source metadata. V1 legacy env
and V2 absent-vault env-file compatibility need no third-party runtime Python
dependencies. Native OpenCode saved/OAuth credential refresh/copy remains
independent in every mode, whether a vault exists or not. The optional `vault`
extra adds maintained cryptography for Scrypt/AES-GCM; full CI/security
tests install that extra in an isolated environment, while plain installs still
test absent-vault compatibility without crypto. A configured vault must fail
closed when crypto is missing. GitHub source/curl installations stay
supported in parallel. Do not make a pip-managed launcher install through the
GitHub self-updater or write a global wrapper as an installation side effect.

Build in isolation (uv uses an ephemeral environment; no project/system pip
install is needed):

```sh
uv run --no-project --with build --with twine python -m build
uv run --no-project --with twine python -m twine check dist/*
python3 tools/check_package.py dist --with-vault
```

The last check installs only the wheel into a temporary virtual environment,
tests CLI commands/resources, and ensures package upgrades do not invoke the
GitHub installer. Standard build produces the wheel from the sdist to verify
that it works without Git. Do not publish a dirty working tree.

PyPI rejects local `+gHASH` version suffixes: the distribution version is
`1.6.N` from full-history commit count minus `VERSION_BASE_COMMIT_COUNT`, and
packaged `build-info.json` keeps `1.6.N+gHASH` plus the full commit and absolute
commit count. The original stable release has count 29 and version 1.0.0; every
subsequent commit advances the patch automatically. Version calculation rejects
shallow history. The 1.5 and 1.6 series keep that baseline; a first published patch
need not be zero. Embedded older series retain their identity until upgrade.
Fast-forward the tested release commit into master so merging
does not add an extra version increment. Each selected release needs a unique
version; ordinary commits remain on GitHub without requiring a PyPI upload.

Trusted Publisher configuration in PyPI must match:

- Project: `oc-box`.
- GitHub owner/repository: `groopcd/oc-box`.
- Workflow filename: `publish.yml`.
- GitHub environment: `pypi`.

No long-lived PyPI upload token belongs in GitHub or this repository. Only the
publish job receives `id-token: write`, and it downloads already-tested artifacts
rather than executing package build code with publishing credentials. GitHub
Actions are pinned by commit. The user must create/own the PyPI account and
configure the matching publisher; pending publishers do not reserve names.

First run the workflow with its `publish` input false to verify a build-only
run. A published GitHub release named `v1.6.N` (matching `public_version(build)`)
builds, validates, and publishes to PyPI. Alternatively manually dispatch
`publish.yml` on a reviewed commit with `publish` true. Environment protection
rules may require owner approval. Do not publish from untrusted pull requests.
The repository's CI builds/verifies packages on Python 3.10 and 3.13; the full
mainline smoke fixture remains part of local verification with OpenCode.

### Release gate and publication checklist

GitHub/curl and PyPI are separate distribution channels. `install.sh` and the
GitHub self-updater fetch `master`, not the latest release tag. Do not advance
that branch to an unaccepted runtime candidate merely to start release CI.

1. Integrate changes on a `feature/**` candidate branch, preserving full history.
   Finish documentation and regenerate the manuals before freezing the commit.
   Check for untracked files as well as `build-info.json`'s dirty flag. Keep the
   candidate unchanged throughout validation; record its full commit identity.
2. Require the source/package/native integration workflow to pass, and the
   seven-image distro workflow for package-map changes. Run `publish.yml` with
   `publish=false` on the candidate as a build-only gate. Neither this workflow
   nor the release-triggered publisher automatically checks other workflows or
   host reports; the release owner must review those gates explicitly.
3. For history/lifecycle changes, obtain new external-host reports for the
   focused history/readiness runner, global-vault fixture and V1/V2 lifecycle
   fixture on the same clean candidate. Include regression-specific host cases,
   not just the previously accepted baseline. Use only private synthetic state;
   never bypass host-only guards from an oc-box container. Follow the
   [history/readiness handoff](docs/testing.md#focused-historyreadiness-host-runner)
   and preserve failed attempts separately.
4. Review the real reports, every required check, native and source identities,
   expected refusals, recovery and secondary failures. Offline rehearsals, old
   reports and a successful build cannot substitute for host acceptance. If any
   source changes after testing, freeze and validate the new candidate instead.
5. After acceptance and publication approval, fast-forward the exact tested
   commit into `master`, push it and tag that same commit as `v1.6.N`. Do not
   squash, rebase or add a merge/evidence commit between acceptance and tagging:
   every commit changes both source identity and the derived version. Put the
   accepted report summary in the GitHub release notes; later source-documentation
   updates have their own version and do not retroactively change the release.
6. Publish the stable GitHub Release. Its release event invokes Trusted
   Publishing to PyPI; do not start a second upload. Wait for successful build
   and publish jobs, including any environment approval. Download that run's
   `python-distributions` artifact and attach its exact wheel and sdist, plus a
   `SHA256SUMS` file, to the GitHub Release.
7. Download the public PyPI files and compare their hashes with the GitHub assets
   and checksum file. Re-run `tools/check_package.py DISTDIR --with-vault`, verify
   an isolated installed CLI reports the accepted full `+gHASH` identity, and
   check tag, release and `master` identities. Keep the full commit/count in
   wheel and sdist resources even though the public version has no local suffix.

A later evidence-only commit on `master` also advances the curl/self-update
version, while PyPI stays at its selected release. Do not describe those channels
as byte-identical after such an update; retain the released tag and artifacts.

Use only the repository owner's explicit publication credential, isolated
subprocess-local GitHub authentication and a verified owner identity for GitHub
management and pushes. Never use provider/inherited Copilot credentials for
publication, or reuse publication credentials for public source downloads.
