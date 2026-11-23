# Dashboard screenshot assets

[Documentation](../../README.md) / [Workspace manager](../../tui.md)

These are captures of the actual `DashboardApp` Textual compositor, not generated
imitations of the interface. All project names, paths, IDs, metrics, history
points and catalog records are fixed, synthetic examples.
They illustrate the current OpenCode 2.x documentation path; legacy 1.x operating
instructions are [kept separately](../../legacy/README.md).

## What is shown

| Asset | View |
| --- | --- |
| [status.png](status.png) | Host cards, workspace inventory, selected context and retained activity |
| [settings.png](settings.png) | Expanded-height inline form with all six preference groups |
| [sessions.png](sessions.png) | Metadata-only roots, children and archived entries |
| [terminals.png](terminals.png) | Disconnected native widget with explanatory display text |
| [logs.png](logs.png) | Filtered synthetic activity entries and the actual match counter |
| [storage.png](storage.png) | All-workspace metadata inventory and disabled guarded actions |
| [backups.png](backups.png) | Archive/destination fields and source/target controls |
| [README hero](../readme-hero.png) | The same Status capture with promotional framing outside the UI |

Every image visibly identifies its synthetic, read-only status. No host registry,
real project/session, native OpenCode service, Podman, clipboard, password prompt
or archive is used. The terminal transport remains disconnected: its text is
an explanatory emulator fixture, not a record of commands or a running shell.
Session records come from an in-memory GET-only catalog, not a native inventory.
These images do **not** extend any interactive or external-host acceptance claim.

## Regenerate

From the repository root, using an ephemeral environment:

```sh
uv run --no-project --with-editable . \
  --with 'textual>=8.2.8,<9' --with 'pyte>=0.8.2,<0.9' \
  --with 'Pillow>=11,<13' \
  python -B tools/capture_tui_docs.py --force
```

The renderer needs DejaVu Sans Mono and DejaVu Sans fonts installed on the
documentation-building machine. It uses no browser or image-generation service.
Normal CLI use does not acquire these documentation-only dependencies.

The command replaces only the named screenshot set, README hero and capture
manifest. For a separate output directory, pass `--output-dir /tmp/oc-box-doc-images`
instead. Existing files are refused unless `--force` is explicit.

## Rendering and provenance

[manifest.json](manifest.json) records dimensions, image checksums, dependency
versions and the UI source hashes used for capture. Fixtures and timestamps are
fixed; pixel output can still change with font or rendering-library versions.

Terminal cells are rasterized from the real compositor's text and styles. The
framing/captions sit outside that viewport. Where DejaVu lacks the two-cell cross
mark, a same-color two-cell cross fallback is drawn; actual terminal font/emoji
appearance can differ. The README hero reuses the captured Status viewport rather
than inventing controls, telemetry or product capabilities.
