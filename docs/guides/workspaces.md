# Manage, recover, and remove a workspace

[Documentation](../README.md) / Guides

> **OpenCode 2.x workflow.** For 1.x maintenance terminals and legacy behavior,
> see [the legacy workspace guide](../legacy/README.md#workspaces-and-terminal).

Use this guide to resume work, change the environment, recover a directory
registration, or remove only the data you intend to delete.

**Prerequisites:** a configured [installation](../installation.md) and a project
directory. Ordinary lifecycle commands are available in stable 1.6.94; the
optional dashboard and 1.7 exit-history recovery are development-channel
features. See [channels](../update-channels.md) before choosing a release.

**Contents:** [everyday commands](#inspect-and-resume-work),
[persistence and deletion](#choose-reset-or-purge),
[maintenance](#open-a-clean-maintenance-shell),
[directory recovery](#recover-a-directory-registration),
[host sessions](#continue-an-existing-host-session),
[hardware](#select-a-distro-or-map-hardware), and
[history archives](#understand-v2-conversation-archives).

## Inspect and resume work

```sh
oc-box                              # Create/resume in the current directory
oc-box --new                        # New conversation, same environment
oc-box list                         # Workspace table and latest session IDs
oc-box --status                     # CPU, RAM, storage, state, limits, sessions
oc-box --status --project ~/devel/demo --json
oc-box distros                      # Supported images and host default
oc-box stop --project ~/devel/demo
oc-box terminal --project ~/devel/demo # Existing V2 session shell
oc-box sync --project ~/devel/demo   # Retry host history sync after a crash
oc-box repair --project ~/devel/demo # Accept a verified directory identity change
oc-box --about
oc-box upgrade --check
```

`--workspace ID` selects a registered workspace from any directory, including
for cleanup after a project directory was deleted. `--status` does not start
or stop containers and can run while OpenCode is active. The optional development
dashboard offers `oc-box tui --read-only`; see [clients](clients.md).

For one owning V2 compatibility launch without vault delivery, use
`oc-box start --project ~/devel/demo --no-vault`; read the
[credential constraints](credentials.md#make-one-v2-env-file-compatibility-launch)
first. Attached clients reject that option.

## Choose reset or purge

| Operation | Project and unfinished edits | Private home/session IDs | Container OS |
| --- | --- | --- | --- |
| Exit or `stop` | Kept | Kept | Kept, stopped |
| `sync` | Kept | Kept; host archive refreshed | Kept, stopped |
| `terminal --maintenance` | Kept | Kept; no history sync | Kept, prior run state restored |
| `terminal` (V2 attach) | Kept | Shared current session | Kept; owner controls stop |
| `reset` | Kept | Kept | Rebuilt |
| `purge` | Kept | Deleted | Deleted |
| `purge --all` | **Deleted** | **Deleted** | Deleted |

```sh
oc-box reset --project ~/devel/demo --cpus 8 --memory 16g
oc-box purge --project ~/devel/demo  # Remove environment; retain source files
```

Reset removes system packages and files outside the project/private home.
Home-installed dependencies, Git history, and session IDs remain. CPU/RAM/process
limits and device mappings are create-time settings; changing them for an
existing container requires reset, not purge. CPU and RAM limits are ceilings,
not reservations. Defaults are 4 CPU cores, 8 GiB RAM, and 2048 processes.

Purge asks for an ID-prefix confirmation. **`--delete-sessions` additionally
deletes recorded host archive copies; `--all` includes that action and deletes
the project directory.** Shared image caches and unrelated resources remain.
Take an [encrypted backup](backups.md) before destructive work when needed.

## Open a clean maintenance shell

After exiting the workspace's OpenCode client:

```sh
oc-box terminal --maintenance --project ~/devel/demo
# Or, from another directory:
oc-box terminal --maintenance --workspace WORKSPACE_ID
```

This opens a clean interactive shell at the project path with the same mounts,
devices, limits, and rootless permissions. Type `exit` or Ctrl-D to leave. A
container started by this command is stopped on exit; one already running is
left running. No OpenCode launch, configuration refresh, dependency install,
session import/sync, or environment recreation occurs.

Host tokens, API keys, custom `pass_env`, proxies, and agent variables are not
forwarded. The shell's environment is cleared, startup files are skipped, and
history persistence is disabled. Credentials already stored in the container's
files or held by its other processes remain accessible: a maintenance shell is
not isolation from the existing container. It requires a real terminal and the
workspace's exclusive lock, so exit an active OpenCode launcher first.

## Recover a directory registration

Clearing host `~/.cache` does not delete workspace registrations or private
session volumes. Workspace identity uses **generic Linux VFS interfaces**, not
filesystem-specific tools or branches: the canonical path and marker select the
workspace, while the filesystem ID, opaque persistent directory handle, and
inode verify its identity. Device numbers can change after reboot without
changing the directory; a matching recorded stable baseline accepts that change
automatically. The same algorithm is used for ext4, XFS, Btrfs, bcachefs, ZFS,
and other drivers that expose these capabilities, even when the private registry
is on a different filesystem from the project.

Existing registrations are upgraded at a normal verified launch, or by repair.
If an old registration already has a device-number mismatch and no stable
baseline, it needs one explicit repair: an unchanged inode alone cannot prove
the filesystem is the same. Drivers without a usable persistent handle/filesystem
ID retain the conservative device/inode check and explicit repair when it changes.
There is no universal physical-directory identifier on every Linux filesystem.
A UUID marker alone would accept copied/restored directories indistinguishably;
oc-box does not claim that as replacement detection. Install the updated launcher
before the next reboot, then normally resume each healthy workspace to record
its baseline. No container reset or session import is needed.

For a genuine or unverifiable identity change, keep the marker and use
`oc-box repair` after checking the project and stopping its launcher. If the
marker is missing, the launcher detects existing registrations instead of
silently creating a duplicate:

```sh
oc-box --status
oc-box repair --workspace WORKSPACE_ID
oc-box --workspace WORKSPACE_ID --resume
```

Repair restores a missing marker or accepts the current identity while retaining
the container, settings, and session IDs. It never replaces another workspace's
marker. If several records reference the same path, select the intended ID.
`list`/`status` distinguish identity changes, missing markers, and marker
mismatches, with specific recovery or cleanup guidance.

For a stale duplicate left by an older launcher, first recover any needed work,
then run **plain** `oc-box purge --workspace STALE_ID`, without `--all` or
`--delete-sessions`. It leaves the current project/marker and host histories
intact and retains private archive provenance under the state directory's
`retired/` folder. This is saved recovery data, not a third active workspace.

## Continue an existing host session

Exit the host OpenCode client first, then import once:

```sh
oc-box --project ~/devel/demo --import-session ses_example
```

The source session must belong to that project. oc-box saves a private recovery
export, verifies the imported transcript, and resumes the same session ID.
Your original host conversation is unchanged. Later launches need no import
flag. See [session recovery](../cli.md#sessions-and-recovery) for retry behavior.
The same flag can recover an oc-box host archive into a fresh environment after
its marker was replaced; keep the original registry and saved history export.
After plain purge with a current launcher, retained private `retired/` provenance
can verify the old host archive instead. Older already-purged records cannot be
reconstructed automatically; the imported live session is unaffected.

## Select a distro or map hardware

```sh
oc-box --project ~/devel/demo --distro ubuntu
oc-box reset --project ~/devel/demo --distro fedora
oc-box reset --project ~/devel/demo --device /dev/accel/accel0
oc-box reset --project ~/devel/demo --podman-arg=--shm-size=1g
```

Choices: Tumbleweed, Leap, Ubuntu, Debian, Fedora, Arch, and Alpine. Distro and
device settings persist per workspace. Repeat `--device`/`--podman-arg` for
multiple settings; clear old values with `--clear-devices`/`--clear-podman-args`.
Podman arguments are literal long-option tokens, not shell snippets.

Rootless device access may require crun with `--group-add=keep-groups`, host
ACLs, and SELinux's `container_use_devices` setting. oc-box checks these
prerequisites and prints advisory warnings before device-enabled creation.
See [device troubleshooting](../cli.md#devices-and-host-permissions) for exact
host commands; oc-box never changes host policy automatically.

## Understand V2 conversation archives

History synchronization includes paginated root, fork, archived, and child/subagent
conversations at the workspace's exact internal project directory. Each remains
a separate host archive; parent/fork provenance is informational metadata, not
restored permissions or native parent links. Missing sources retain their last
archives without blocking later conversations. Automatic resume stays root-only.

Incomplete inventories, unsettled exports, and unverifiable imports leave sync
pending rather than claiming success. Final owner shutdown checks native activity
for up to 30 seconds after client leases drain; normal cleanup still stops the
container on failure. Archives are mirrors, not bidirectional working copies or
full backups of pending input and live execution. See [sync](../cli.md#sync).

In experimental 1.7, eligible V2 timeouts can trigger one bounded persisted-history
snapshot after the old owner closes and a whole-container stop is verified.
The native 2.2.6/2.2.7 profile is shell-only; canonical stable 2.3.x uses the
guarded shell/subagent profile. Host and container binaries must match the captured
exact patch, not merely the same series. Unsupported identities and incompatible contracts
refuse; series recognition is not acceptance of untested future native behavior.
The same existing container restarts temporarily for an isolated history reader,
without normal runtime preparation, credential refresh or
requested model/tool execution, then stops again. Explicit stop intent suppresses
recovery. A raw client exit of `0` may return `0` after a live timeout only when
checked archival/native verification, recovery cleanup and the final verified
stop all succeed. The interrupted-work/unconfirmed-completion warning remains;
native job rows are not repaired. Client failure/cancellation, unknown status,
partial history or failed/uncertain cleanup remain nonzero with retry-sync
guidance. Unsafe or unsupported cases remain pending. See the
[recovery notes and validation scope](../releases/1.7.md#exit-history-recovery).

An interrupted legacy 1.x assistant can remain unfinished after migration to 2.x
even when later turns have completed. In an inactive session, sync mirrors native OpenCode's
export without those superseded assistants and records only their omitted count
in archive metadata. An unfinished tail, running shell/compaction, or active
superseded-assistant case stays pending; other sessions can still synchronize.

Stable [v1.6.58](https://github.com/groopcd/oc-box/releases/tag/v1.6.58) records
reviewed external history/readiness, global-vault and V1/V2 lifecycle reports,
including that migrated-history correction. This reconciles published release
notes; it is not a fresh raw-report review or acceptance of the later Textual
manager or post-stop recovery. Earlier reports do not validate a later revision:
see [evidence boundaries](https://github.com/groopcd/oc-box/blob/devel/docs/testing.md#published-v1658-evidence-reconciliation)
and the [final-source gates](../releases/1.7.md#final-candidate-and-publication-gates).

## Related

- [CLI lifecycle reference](../cli.md#lifecycle-and-persistence)
- [Credentials](credentials.md) · [Backups](backups.md) · [Clients](clients.md)
- [Security boundary](../../SECURITY.md) · [Core concepts](../concepts.md)
