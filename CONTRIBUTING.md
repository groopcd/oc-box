# Contributing

Use the full Git history: the release identity is `1.0.N+gHASH`, with N the
commit count minus the 1.0.0 baseline (29). Keep the absolute `commit_count` in
metadata for upgrade compatibility with 0.1 releases. Changes should keep mainline
OpenCode unmodified and preserve MCPs, formatters, language servers, plugins, and
the host/container storage boundary.

## Checks

Python 3.10+ and stdlib run the base tests. Full vault coverage needs the optional
cryptography library; backup integration tests need host age/age-keygen. CI
installs age explicitly. The mainline smoke test also needs Git and OpenCode
(integration checked with 1.19.5):

```sh
uv run --no-project --with cryptography python -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
python3 -B tools/build_docs.py --check
git diff --check
```

Tests use isolated temporary app data. Mainline model/MCP/LSP/formatter fixtures
are local and bounded. These checks do not replace live host-side Podman tests.
Do not run host lifecycle tests from inside the project's running container.

For OpenCode 2.x compatibility, keep a separate native binary and run
`python3 -B tests/smoke_v2.py --opencode /path/to/opencode-v2`. The branch
`feature/opencode-2-compat` retains 1.x support and stays unmerged until accepted
disposable host-terminal lifecycle, credentials, and backup/restore testing.
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
coexistence must still be validated. Copied plugin tests cover Unicode byte
budgets, primary-request JSON schemas, configured sources outside autoload
directories, PDF/history transforms and model-variant request rewriting.

## Bash completion

Bash completion lives in `completions/oc-box`, ships in both distribution
channels, and can be emitted with `oc-box completion bash`. Keep its static
commands/options/choices aligned with the parsers. `tests/test_completion.py`
checks that coverage and exercises real Bash with no external commands on PATH,
including context, equals syntax, paths with spaces, and installation. Lint with
`bash -n completions/oc-box` and `shellcheck --shell=bash completions/oc-box`.
Completion must never invoke lifecycle commands, a secret prompt, or provider
queries; open-ended identifiers are left for the user to enter.

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
prebuilt manuals, license/notice, and immutable source metadata. Its default
env workflow has no third-party runtime Python dependencies. The optional
`vault` extra adds maintained cryptography for Scrypt/AES-GCM; full CI/security
tests install that extra in an isolated environment, while plain installs still
test that no crypto dependency is required to launch. GitHub source/curl installations stay
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
`1.0.N` from full-history commit count minus `VERSION_BASE_COMMIT_COUNT`, and
packaged `build-info.json` keeps `1.0.N+gHASH` plus the full commit and absolute
commit count. The first stable release has count 29 and version 1.0.0; every
subsequent commit advances the patch automatically. Version calculation rejects
shallow history. Fast-forward the tested release commit into master so merging
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
run. A published GitHub release named `v1.0.N` (matching `public_version(build)`)
builds, validates, and publishes to PyPI. Alternatively manually dispatch
`publish.yml` on a reviewed commit with `publish` true. Environment protection
rules may require owner approval. Do not publish from untrusted pull requests.
The repository's CI builds/verifies packages on Python 3.10 and 3.13; the full
mainline smoke fixture remains part of local verification with OpenCode.
