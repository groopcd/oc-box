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

## Focused launcher-only guest evidence

Exact source `1466076c17f2205633788f0424ef9cb886d9c405` passed 20 checks
in a private, no-NIC Fedora 44/KVM guest. Dedicated help and dry run wrote
nothing; full requests, pipe confirmation and an incorrect controlling-PTY
phrase refused. The exact phrase removed the verified active launcher and
its selected release. An older installation of the same source, the project
marker, vault, registry and native OpenCode history fixtures were preserved.
Neither Podman nor OpenCode ran. Guest, unmount and QEMU exits were all zero;
the guest was reaped and the writable disk, socket and ISO were removed.

Sanitized receipt SHA-256:
`909e26e5f4d2869cc3cdd5cc7ff741f1f761cd37d6df9d3011665af5fb9405f0`.
This is launcher-only synthetic host evidence for that source, not a general
filesystem guarantee, real-user uninstall, full cleanup or final release gate.
