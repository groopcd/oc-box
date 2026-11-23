# Back up and restore an encrypted workspace

[Documentation](../README.md) / Guides

> **Primary examples: OpenCode 2.x.** The common encrypted workspace archive
> also retains legacy state; see [1.x migration/downgrade boundaries](../legacy/README.md#history-migration-and-downgrade)
> before changing native major versions.

Use full or portable archives to preserve a workspace independently of its live
container and session-history mirrors.

**Prerequisites:** an [installation](../installation.md), the optional host
`age` package (provides `age` and `age-keygen`), and a compatible CPU architecture
for restore. These CLI workflows are available in stable 1.6.94. The optional
development dashboard has [separate incomplete UI validation](clients.md#know-the-dashboard-validation-limitations).

**Contents:** [create and inspect](#create-inspect-and-restore-an-archive),
[included data](#understand-what-is-included), and
[limits and validation](#check-format-limits-and-evidence).

## Create, inspect, and restore an archive

Exit the source workspace's OpenCode launcher and keep host editors/builds idle:

```sh
# Complete project, private home/session DB, registry evidence, and OS image:
oc-box backup --project ~/devel/demo --output /backup/demo.ocbox
# Smaller backup; rebuild the distro OS on first restored launch:
oc-box backup --project ~/devel/demo --portable --output /backup/demo-portable.ocbox
# Both prompt for a masked backup passphrase, separate from the credential vault.
oc-box backup-info /backup/demo.ocbox
oc-box restore /backup/demo.ocbox --project ~/devel/demo-restored
```

Backups are always encrypted with the standard age format. Project/home/registry
archive and image bytes stream directly into encryption; only ciphertext is
temporarily staged. Native age public recipients can replace the passphrase for
unattended backups using `--recipient age1...`; restore/info use the matching
private `--identity FILE`. See [passphrase creation policy](credentials.md#enter-and-protect-secret-values)
for separate vault/backup minimums and existing-password compatibility.
Choose an output directory that supports unnamed temporary files and hard links
on Linux. To store the archive on other media, create and verify it locally
first, then copy the encrypted file.

## Understand what is included

An ordinary workspace backup does **not** include the host-wide credential
vault or host configuration. Add `--host` for a migration archive. Outside
env files are still excluded; files inside the project are ordinary project data.

Restore is non-overwriting and creates fresh workspace/container/volume IDs.
It preserves conversation IDs, full live database/subagent history, source/Git
edits, private home, and resource/credential modes and names-only policies. A
restored default/global V2 workspace uses the restoring host's current vault,
not the original vault or old host privileges.

Restore keeps the old **in-container** project/home paths and maps the new host
project there, rather than rewriting database paths. Thus the host directory may
move while the agent still sees the original internal path. It leaves the new
container stopped; devices and extra Podman settings are recorded but not
activated. Reconfigure required hardware explicitly. Full backups keep the OS
image; portable restores need system packages reinstalled.

**Previously stored credentials/auth/history are included in private
home/project/OS files: treat the encrypted backup and its key as sensitive.
Deleting a backup does not revoke those credentials.**

## Move a workspace and its host settings

In stable `1.8` or development `1.9`, add `--host` to explicitly include
host settings in a workspace backup. If you rely on saved
OpenCode V2 provider or MCP logins, pipe in the native credential export:

```sh
set -o pipefail
opencode auth export --standalone | oc-box backup --host --project ~/devel/demo \
  --opencode-auth-export - --output /backup/demo-and-host.ocbox
```

On the new host, install oc-box, age, rootless Podman and the compatible native
OpenCode version. Before the first OpenCode launch, restore to fresh paths:

```sh
oc-box restore /backup/demo-and-host.ocbox --host --include-opencode \
  --project ~/devel/demo-restored
```

The archive restores oc-box settings, the encrypted vault, the supported
OpenCode profile and the stopped workspace. `--include-opencode` asks for typed
consent because restored config and plugins can execute code on later launches.
Use the original vault passphrase when you resume. You can also use `backup
--host --output FILE` and `restore FILE --host` without a workspace; add
`--include-opencode` to that restore if you want the native profile.
If an installer creates host preferences before migration, keep them aside
rather than overwriting them, or use a clean XDG profile from the beginning.

Restore never overwrites host files or an existing native credential database.
External paths/dependencies, shell environment, hardware and installation
state are not restored. See the [host migration reference](../cli.md#restore-host-settings-on-a-new-machine)
for file bounds, unsupported files and partial-failure behavior.

The [dashboard Backup/restore tab](../tui.md#7-backuprestore) offers the
same host-only and combined choices as ON/OFF switches. For an optional native
V2 credential export, the dashboard takes a private file path rather than
piped input.

## Check format limits and evidence

The initial format refuses extra bind mounts/volumes and nested project mounts,
does not copy ACLs/xattrs/SELinux labels, and is not a running-process checkpoint.
Source symlinks are stored, not followed. Restores reject path traversal, device
nodes/FIFOs, duplicate paths, hardlink/symlink escapes, unbounded archive metadata,
and elevated permission bits.

Version 1.0.0 introduced encrypted full and portable workspace backups. Both
modes passed that version's disposable Linux host/rootless-Podman fixture,
including project/Git edits, private-home database bytes, session IDs, internal
paths, rootless ownership, and stopped restores. This historical CLI evidence
is not a new-source or dashboard acceptance claim; see
[host fixture validation](../../CONTRIBUTING.md#live-backuprestore-validation)
and the [testing evidence boundaries](../testing.md).

## Related

- [Backup reference](../cli.md#backup) · [Inspect](../cli.md#backup-info) · [Restore](../cli.md#restore)
- [Credentials](credentials.md) · [Workspace persistence and deletion](workspaces.md#choose-reset-or-purge)
- [Security](../../SECURITY.md) · [Experimental dashboard](../tui.md)
