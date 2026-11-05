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
