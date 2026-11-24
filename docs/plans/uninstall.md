# Host uninstall plan

The private candidate wires the launcher-only executor and its dry run.
`--full` is still a proposed destructive scope and refuses without both the
verified workspace and removable-state executors. No fallback to launcher-only
removal is permitted for a full request.

```sh
oc-box --uninstall
oc-box --uninstall --full --dry-run
oc-box --uninstall --full
```

By default remove only the verified oc-box installation that supplied the
running launcher. Keep workspace containers and home volumes, registry,
settings, encrypted vault, private logs, and backup files. Do not query
Podman or OpenCode in that mode.

Full cleanup adds reviewed, verified oc-box-owned workspace containers,
private home volumes, and removable oc-box state. Do not start or force-stop
anything to make it eligible. Refuse running owners, active clients, invalid
registrations and ambiguous storage. Unrelated or shared Podman resources,
project trees and their markers, host OpenCode history, migration history
backups and externally selected log/backup paths stay outside the plan. No
system-wide reset, prune, filesystem sweep, or automatic final history sync.

The host review should identify each target, retained data and any blocker
before a distinct typed controlling-TTY confirmation. A dry run performs no
deletion and does not ask for confirmation. Show metadata-only expectations
as such where the engine has not verified ownership. Recheck identities and
the entire scope under the appropriate locks before any mutation. If cleanup
becomes partial or uncertain, report confirmed outcomes and keep the
installation and recovery state needed for another review.

Before full cleanup the operator can separately save supported host settings
and the encrypted vault, for example:

```sh
oc-box backup --host --output /backup/host.ocbox
```

Use an output outside the removal scope. This is not a workspace-volume backup
and does not back up native host OpenCode history. Uninstall itself must not
open that history, contact its service, or run a backup.
