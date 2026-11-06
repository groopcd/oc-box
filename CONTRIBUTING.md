# Contributing

Use the full Git history: the release identity is `0.1.N+gHASH`, with the commit
count as N. Changes should keep mainline OpenCode unmodified and preserve MCPs,
formatters, language servers, plugins, and the host/container storage boundary.

## Checks

Python 3.10+ and stdlib are sufficient for unit tests. The mainline smoke test
also needs Git and OpenCode (integration checked with 1.19.5):

```sh
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
python3 -B tools/build_docs.py --check
git diff --check
```

Tests use isolated temporary app data. Mainline model/MCP/LSP/formatter fixtures
are local and bounded. These checks do not replace live host-side Podman tests.
Do not run host lifecycle tests from inside the project's running container.

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
prebuilt manuals, license/notice, and immutable source metadata. It has no
third-party runtime Python dependencies. GitHub source/curl installations stay
supported in parallel. Do not make a pip-managed launcher install through the
GitHub self-updater or write a global wrapper as an installation side effect.

Build in isolation (uv uses an ephemeral environment; no project/system pip
install is needed):

```sh
uv run --no-project --with build --with twine python -m build
uv run --no-project --with twine python -m twine check dist/*
python3 tools/check_package.py dist
```

The last check installs only the wheel into a temporary virtual environment,
tests CLI commands/resources, and ensures package upgrades do not invoke the
GitHub installer. Standard build produces the wheel from the sdist to verify
that it works without Git. Do not publish a dirty working tree.

PyPI rejects local `+gHASH` version suffixes: the distribution version is
`0.1.N` from full-history commit count, and packaged `build-info.json` keeps
`0.1.N+gHASH` plus the full commit. Each selected release needs a unique version;
ordinary commits remain on GitHub without requiring a PyPI upload.

Trusted Publisher configuration in PyPI must match:

- Project: `oc-box` (pending publisher until its first successful publication).
- GitHub owner/repository: `groopcd/oc-box`.
- Workflow filename: `publish.yml`.
- GitHub environment: `pypi`.

No long-lived PyPI upload token belongs in GitHub or this repository. Only the
publish job receives `id-token: write`, and it downloads already-tested artifacts
rather than executing package build code with publishing credentials. GitHub
Actions are pinned by commit. The user must create/own the PyPI account and
configure the matching publisher; pending publishers do not reserve names.

First run the workflow with its `publish` input false to verify a build-only
run. A published GitHub release named `v0.1.N` (matching that commit's count)
builds, validates, and publishes to PyPI. Alternatively manually dispatch
`publish.yml` on a reviewed commit with `publish` true. Environment protection
rules may require owner approval. Do not publish from untrusted pull requests.
The repository's CI builds/verifies packages on Python 3.10 and 3.13; the full
mainline smoke fixture remains part of local verification with OpenCode.
