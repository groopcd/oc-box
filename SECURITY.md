# Security and trust boundary

oc-box runs the entire OpenCode process and its integrations inside a rootless
Podman container. It aims to contain routine destructive development commands
and environment changes. It is not a VM, a hardened hostile-code service, or a
guarantee against kernel/runtime vulnerabilities.

## What is exposed

- The selected project is writable, including its Git metadata and untracked
  files. Agent commands can delete or alter all of it. Keep independent backups.
- The private home contains the live session database, snapshots, copied config,
  credentials, and caches. These are writable by container processes.
- Global OpenCode config, plugins, MCP definitions, and auth are copied at
  startup. All execute inside the container, where they may use supplied
  credentials and the network. Audit copied integrations as executable software.
- Variables ending in `_API_KEY` or `_TOKEN` and configured `pass_env` values
  are available inside the container. Do not forward unrelated credentials.
  Credential values are passed through the Podman client's environment, not its
  command line; this is not protection from same-user process inspection.
- Host services and LAN endpoints are reachable over the network. An API with
  powerful operations remains powerful even without a host filesystem mount.
- `--device` and `--podman-arg` are explicit host-operator extensions to the
  boundary. Extra mounts, engine sockets, privileged options, and capabilities
  can materially change isolation. Device-driver bugs remain a host-kernel risk.

By default the host home, host OpenCode DB, private oc-box registry, engine
socket, SSH agent, desktop sockets, host PID/network namespaces, and host devices
are not mounted/exposed. The default container has resource ceilings but no disk
quota, so project, image, and volume growth can exhaust host disk space.

## Host-side operations

`oc-box terminal` opens a maintenance shell without copying auth/config or
forwarding host tokens, API keys, custom `pass_env`, proxies, or agent sockets.
Only allowlisted Podman runtime/storage environment names reach the host client.
The shell uses `env -i`, skips startup profiles, and disables history persistence;
it receives no additional mounts or capabilities. It holds the workspace lock,
requires a real TTY and valid ownership/identity, and never creates a replacement
container. Terminal restores the stopped/running state it observed on entry.
This does not hide credentials previously stored in container files or held by
other processes, and starting its existing entrypoint can retain create-time
secrets. Treat that shell as the same trust domain as the container itself.

Lifecycle commands run as the host user and manage only resources with the
workspace's ownership label. Registry metadata and directory identities are
stored outside the project. Directory replacement or marker copying must not
authorize another mount or project deletion. This does not defend against an
attacker who already controls the host user's registry or launcher installation.
Identity probes are filesystem-agnostic: standard Linux VFS interfaces, without
filesystem-type allowlists, filesystem tools, or reading raw storage. Where
supported, identity combines the canonical path/marker and inode with an opaque
persistent file handle and filesystem ID. Driver-specific generation/subvolume
data stays opaque; transient device/mount numbers are not treated as durable
identity. A matching strong baseline permits device-number changes after reboot
without user repair. Changed or unavailable strong identity fails closed, not
back to an inode-only match. Unsupported filesystems retain device/inode checks.
Old records are upgraded only after their original identity matches or the host
operator explicitly repairs; matching inode alone cannot establish a baseline
for a filesystem whose device already changed. Cloned filesystems/snapshots can
still require explicit repair; this does not authenticate content against a
privileged host attacker.

No universal filesystem-independent physical-directory identity exists. Marker
IDs alone (even signed ones) cannot distinguish an exact copy at the same path.
On drivers that lack persistent identity capabilities, oc-box retains the strict
legacy check and explicit repair; it does not claim every filesystem/kernel has
the same reboot behavior. Support is detected by capability, never guessed from
ext4/XFS/Btrfs/bcachefs/ZFS names. Physical identity evidence is distinct from
authenticating the file contents or protection against a host-level clone.
The host operator can explicitly accept a changed identity with `oc-box repair`;
this requires the original canonical path, private registry, workspace lock,
and a stopped container. An existing regular marker must match; a missing
marker can be restored by that explicit repair. Ambiguous registrations require
a selected workspace ID, and other markers are never overwritten. Normal launch
checks both marker and identity; stale `purge --all` is refused before deletion.

Session exports are treated as container data. Host archives use namespaced IDs
and mainline import/export, without running project plugins in the host archive
controller. Transcript files may contain commands, source, paths, or secrets;
they are private, not release artifacts. Synchronization is not live replication
or an independent backup. Version compatibility is tested against specific
mainline versions; accepting 1.x does not prove every future release compatible.
Plain purge retains private archive JSON and minimal origin metadata under the
state root's `retired/` directory, without retaining an active workspace or
container storage. Those files may contain sensitive history, have no automatic
expiry, and should be handled like the host session archives they help recover.

Images, OS repositories, npm packages, uv release artifacts, provider APIs,
plugins, and MCPs are upstream dependencies. Baseline downloads use HTTPS where
the launcher fetches them directly; distribution tools follow their configured
repositories and signature policies. Keep the host kernel and Podman patched.

## Installation and updates

The curl bootstrap and `oc-box upgrade` trust the code published at the public
`https://github.com/groopcd/oc-box` master branch over HTTPS. This is not a signed
release or reproducible-build verification scheme. Review the installer or use
a reviewed source checkout if you need to inspect code before execution.

Upgrades use an anonymous HTTPS Git fetch in a temporary HOME with global/system
Git config, credential helpers, hooks, and non-HTTPS Git transports disabled.
Provider/publishing tokens, shell/loader hooks, and arbitrary host environment
variables are not inherited. The fetched commit is pinned and checked, with full
history for the version count. Its installer then runs as the host user with a
clean environment to stage the new program and prebuilt manuals in `~/.local`.
No sudo, GitHub token, project script, or submodule is needed. A compromise of
the upstream account/source is still a software-supply-chain risk.

Automatic updates are explicit opt-in and run only before normal installed
launches, never while an agent is working. Failed network checks fall back to
the installed version. Installation validates its source files and stages a
complete versioned copy before switching the launcher. Old running launchers
use their existing release path, which is not overwritten or removed. Updates
from an old flat installation require its active launchers to exit first; they
do not yet have pinned-release imports. New versioned installations can be
upgraded while other versioned launchers remain active. Updates
do not modify project source checkouts, workspace registries, private container
volumes, or OpenCode credentials. A host-container boundary is not bypassed to
update the host: use the host terminal.

## Reporting issues

Report security-sensitive issues privately to Paulo Dias at
<paulo.miguel.dias@gmail.com>. Include affected oc-box/OpenCode/Podman versions,
host distro, reproduction steps, and the relevant boundary. Redact access
tokens, auth files, session exports, and unrelated private paths. Do not post
live credentials in a public issue or attach an entire conversation database.

There is no formal support window or security-response SLA. Security fixes are
made in the current source; reinstall and restart to apply launcher/config fixes.
