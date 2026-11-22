# Browser manager vs. the Textual manager

This compares the offline browser prototype against the seven sections and
shared controls in the [Textual guide](../tui.md), checking the associated
`tui_*.py` implementation at the locally available development ref
`2c69800`. That ref does not change the manager relative to the mockup base.
An example control is not live feature parity or host acceptance.

| Area | In the browser preview | Still to design, implement or validate |
| --- | --- | --- |
| Status | Resource cards, selectable workspace table and inspector, quota meters, expanded sample mount/context, retained activity, explicit Refresh and known/unknown storage values. | Fixed-scale histories and compact resource strip; sample time, stale/absent context, full workspace identity, process/session counts, complete mount and allocated storage diagnostics, selected-workspace scan from Status, and real nonoverlapping refresh. |
| Settings | Staged Apply, Reset and Revert, validated 1–60 second interval, density/details, session scope/search, log source/search/severity/follow, and read-only resource/safety cards. Separate host logging controls are disabled. | Built-in theme selection, fuller saved-vs-observed resource details and safe, independently authorized host logging policy; actual monitor cadence and retained-log follow. |
| Sessions | Explicit sample Connect/refresh and Disconnect; host all/selected-project and workspace scopes; search by title, ID or directory; source/ID/path and selected-row actions; in-memory favorites; archive/unarchive review and typed `DELETE` cascade preview. | Loading/cancellation, failed or incomplete inventory, snapshot/selection retention, verified host archive provenance, and production ownership/activity/terminal refusal cases. The prototype clears the fixture connection on a source change; the live manager must not implicitly release a lease on a view change. Terminal and session backup/restore still need the same verified Sessions target. |
| Terminals | Simulated attachment, detach/reattach, `Ctrl+]`, docked drag/keyboard resizing, and a separate window that automatically fits the available space. | Owner-session and selected-source attachment flows, explicit shell creation/control approval, unavailable and wrong-source states, and a bounded native transport with no transcript recording. Resizing the preview only changes its viewport. |
| Logs | Manager-wide literal search, clear and count; source, severity and Follow controls; sortable table, optional columns, details, RAW, and explicit sample Podman/mirror loading. Mirror stays outside All sources. | Live cadence, all severity levels, stable text selection across refresh, unavailable source and redaction reviews, and bounded explicit Copy all with source guards. There is no reading of external logs or desktop clipboard in this demo. |
| Storage | Three scopes including Host OpenCode session trees; one selectable workspace, sample refresh and sizing, single-target removal explanation, and the shared session deletion review. | Full identity and allocated component sizes; dedicated typed workspace-removal review and verified outcome, disconnected/busy and lock refusals. No purge, whole-database or bulk action. |
| Backup/restore | Full/portable workspaces, host-only and combined settings/vault, native credential export/restore switches, four sample archive layouts, source/target controls, session tree and age options. | Real scope/session binding, refreshed targets and editable absent-destination suggestions; full authenticated review, private prompt presentation, explicit native-restore approval, progress, cancellation, and partial/unverified result states. The preview never accepts a secret or reads an archive. |
| Shared | Workspace picker, current-view refresh, top tabs with keyboard navigation, and global log search. | Persistent success/refusal notices, web-specific shortcuts and clipboard UX, copy-view/paste-copied equivalents, and separate authorization/cleanup for live clients. Do not reproduce shortcuts that conflict with browser or focused shell behavior. |

For a live manager, a workspace pairing is not authorization to read host
catalogs, external logs, or secrets, run host operations, or attach a terminal.
Keep explicit source and target selection, leases, typed reviews, and the
existing no-overwrite, read-only and secret-input rules. Browser mockup checks
do not establish that these live safeguards are implemented.
