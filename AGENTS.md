# oc-box development

This repository lives at `/home/groo/devel/oc-box`. It was moved from
`/home/groo/lixo/oc-box` at the first milestone.

## Architecture

- `oc_box.py`: external rootless-Podman launcher and workspace lifecycle.
- `bootstrap.py`: baseline tools installed inside the container.
- `sync_config.py`: container-only config refresh with dependency-cache reuse.
- `terminal_ui.py`: native TUI relay and startup loading indicator.
- `install.py`: copies the launcher to `~/.local/share/oc-box` and creates
  `~/.local/bin/oc-box`.
- `tests/`: stdlib unit tests and bounded offline mainline integration fixtures.

OpenCode itself and its integrations run natively inside the container. Preserve
MCPs, formatters, language servers, and plugins. Do not return to replacing
built-in tools or disabling integrations for containment. Keep mainline OpenCode
unmodified. Live sessions persist in a private named volume, and host-side
history is synchronized with supported CLI export/import commands on exit.

## Git workflow

Commit completed changes after reviewing the diff and running the relevant
checks. The user explicitly requests automatic commits for work in this
repository. Stage intended source files explicitly; sandbox-generated empty
placeholder dotfiles are not project source. Do not push unless requested.

## Checks

```bash
python3 -B -m unittest discover -s tests -v
python3 -B tests/smoke_mainline.py
```

The smoke test uses temporary databases and offline fixture services, with
bounded timeouts. It does not verify actual Podman isolation. Live Podman
verification must run outside the existing OpenCode Bubblewrap sandbox.

A host-terminal Tumbleweed container/OpenCode launch has succeeded. Full live
MCP, formatter, language-server, and stop/resume verification is still pending;
the automated integration checks use offline fixtures.

The host-terminal warm-start test is now effectively instant after retaining
the container-generated plugin SDK manifest, lockfiles, and dependency cache.
