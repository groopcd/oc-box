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

Lifecycle commands run as the host user and manage only resources with the
workspace's ownership label. Registry metadata and directory identities are
stored outside the project. Directory replacement or marker copying must not
authorize another mount or project deletion. This does not defend against an
attacker who already controls the host user's registry or launcher installation.
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

## Reporting issues

Report security-sensitive issues privately to Paulo Dias at
<paulo.miguel.dias@gmail.com>. Include affected oc-box/OpenCode/Podman versions,
host distro, reproduction steps, and the relevant boundary. Redact access
tokens, auth files, session exports, and unrelated private paths. Do not post
live credentials in a public issue or attach an entire conversation database.

There is no formal support window or security-response SLA. Security fixes are
made in the current source; reinstall and restart to apply launcher/config fixes.
