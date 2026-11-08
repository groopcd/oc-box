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
- Default V2 workspaces without an explicit credential policy receive **all
  current host vault entries** on each new owner when the vault is present.
  This replaces ambient suffix/pass_env forwarding. Every default V2 project
  can therefore read every stored entry; select a narrower workspace policy when
  needed. V1, explicit env mode and no-vault compatibility retain legacy
  `_API_KEY`/`_TOKEN` and configured `pass_env` forwarding; selected mode limits
  credential names. Values pass through the Podman client's environment, not its
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

The existing final Build-agent wildcard overrides copied ordinary global/Build
tool permission rules, including configured denies, as before. The new V2
helper changes only final `ask` to `allow` for **actual container Build asks**;
it never overwrites a deny passed to it. Explicit session/mandatory-policy
denials and managed-rule denials applying after the private overlay remain denied.
Unknown-agent requests retain native ask/deny outcomes and are never treated as
Build. All non-Build/Plan/child rules remain intact.
An explicit event agent wins; otherwise resolve the current session, then native
configured default, never the parent's agent. Plan/child requests retain their
own rules, and session permission arrays are not overwritten. Question and MCP
elicitation prompts are not permission asks. V1 behavior and plain host
permissions remain unchanged.

The final private `OPENCODE_CONFIG_CONTENT` overlay reenables only the generated
Build helper against lower-priority project plugin removals; managed policy
still wins. This does not rely on external agent transforms running after all
config. No `--auto` or client-wide autoaccept is added to oc-box clients. The new
native evaluator/import/reload fixture is offline, not live host acceptance.
Native V2 `--auto` / TUI `session.permissions: autoaccept` are broader optional
user choices that approve asks, not explicit denials (including configured denies
on the plain host). oc-box does not enable
client-wide autoaccept or modify normal host `cli.json`. A one-off host
`opencode --auto --standalone` still runs against normal host env/auth, never the
oc-box vault; treat its host authority separately. No new live permission-prompt/
reload pass has been accepted yet.

## Global V2 host credential vault and policy overrides

With OpenCode 2.x, workspace clients use native private
workspace-owned stdio servers with per-launch environment snapshots; clients
connect using native `--server`. The host shared
service is never exposed or restarted. Vault values are not saved in service
config; copied OpenCode auth still belongs to the private container DB. Credential
export/refresh uses private memfd/pipe transport and supported CLI/API operations.
Original V1 container DB bytes are retained separately from the migrated V2
snapshot. See `docs/opencode-2.md` for scope, limitations and accepted validation.

Concurrent clients obtain ephemeral control credentials through a private
same-user host runtime socket, without copying provider/vault values or storing
service passwords in workspace metadata. Browser access is limited to host
loopback and native one-use pairing/cookies; the URL grants workspace access.
The relay forwards HTTP/streams/WebSocket bytes over Podman exec, never publishes
a container port or exposes a shared host service. Browser launch receives a
sanitized environment. The owner retains the exclusive lifecycle lock until all
attached clients finish; backup/reset/purge cannot race their writes.
Default V2 terminal access connects to the session's native persistent shell PTY
with authenticated WebSocket framing. Ctrl-] releases the attachment without
terminating the shell; its environment/state belong to the running service.
The connecting host's provider/vault values are not forwarded. Maintenance
shells retain their separate allowlisted environment and exclusive lock.

V1's original env-file default is unchanged. V2 without an explicit credential
policy is global vault-first, including existing workspaces with no policy.
Existing explicit env/selected policies remain intentional overrides. When the
host vault is present, every normal owning start/reset/import/web unlocks once
and supplies all current entries, including the first project launch. Selection
is dynamic; names are not frozen into an automatic registry list. New values,
additions and removals apply on the next new owner, never an attached client.

Without a vault, V2 keeps env-file fallback and concise setup
guidance; no crypto installation, vault creation, config edits or JSON-secret
imports happen automatically. An initialized empty vault unlocks but supplies no
entries and suggests `oc-box secrets set NAME`. A present corrupt vault, wrong
passphrase, or missing crypto support aborts before setup/reset deletion, never
silently falling back. Install the `oc-box[vault]` extra or curl `--with-vault`
and use `secrets init/set` before the first V2 project launch for global delivery;
no per-project registration is needed. Native OpenCode saved/OAuth credential
refresh and copying remain independent in every credential mode, including
global vault mode. Global vault delivery concerns env-variable entries, not
structured OAuth refresh-token records. Configured global mode suppresses legacy
ambient token/env overrides, including `OPENCODE_AUTH_CONTENT`, but retains
ordinary supported native auth copying. No plaintext OpenCode JSON secrets are
imported or rewritten; existing copied/container auth/config secrets remain
independent and no revocation is implied. Configuration should contain
published V2 provider/MCP `{env:NAME}` references, not secret values.

The encrypted vault uses cryptography's Scrypt KDF and AES-256-GCM; it does not
implement cryptographic primitives itself. Credentials are entered only on a
controlling host terminal with kernel echo disabled; a custom masked editor
provides a character count. There is no passphrase argument,
environment variable, plaintext export, key file, daemon, or permanent unlock.

At secret-value and confirmation prompts, the user may deliberately press Ctrl+R
to show/hide the current input. Every prompt starts masked; passphrases have no
reveal mode. The visible field is single-line/width-bounded and erased on return,
cancel, and handled signals, but terminal recordings, screenshots, observers,
scrollback, or resize reflow can retain revealed values. Do not reveal in a
recorded/shared terminal. Mask/count feedback discloses length. Kernel input
echo remains disabled even in reveal mode; the renderer never executes secret
text as terminal control sequences, and bracketed control/multiline pastes are
rejected. No values are displayed on stdout/stderr or exported as command-line
arguments. This changes interactive display only, not at-rest encryption.

Vault and backup passphrase creation default to a minimum of 8 characters.
`vault_min_passphrase_length` and `backup_min_passphrase_length` in host oc-box
config, or the per-operation `--min-passphrase-length` override, can require
8..1024 characters (maximum 1024 UTF-8 bytes). These are creation policies:
changing them does not invalidate existing passwords or rewrite encrypted data.

The encrypted vault is outside the project/container at
`$XDG_DATA_HOME/oc-box/credentials/vault.json` (default
`~/.local/share/oc-box/credentials/vault.json`). Its directory/file must be
private and owned by the host user. Operations reject symlinks, extra hard
links, nonregular files, unknown/modified formats, unbounded KDF parameters, and
insecure permissions. Read-modify-write uses a file lock, fresh random salt and
nonce, authenticated format metadata, and fsynced atomic ciphertext replacement.
No plaintext file is created; names are encrypted along with values. Scrypt's
fixed n=131072/r=8/p=1 parameters require approximately 128 MiB memory per KDF.

Explicit workspace policy stores mode (`env`, `selected`, or `vault`) and selected
env/vault names only in the private host registry. Global vault mode saves no
automatic entry-name list and refuses nonempty per-name selections; switch an
existing selection with `--credential-mode vault --clear-credentials`.
Selected mode replaces auto suffix/pass_env
forwarding with explicit names. Runtime/loader/shell control names cannot be
credentials, including global vault entries. Missing selected names or failed
unlock prevent setup or reset destruction. No vault prompt is made for attached
clients, terminal, information, explicit sync, backup/info/restore or upgrade
operations. The passphrase/vault file never enter or mount into the container.
Decrypted values stay in launcher memory and private container process
environments, never host os.environ, Podman argv or saved registry/config data.

V2-only `--no-vault` is an ephemeral opt-out for one owning start/reset/web
invocation; it does not save policy. Global mode uses legacy env forwarding for
that invocation, while explicit env/selected modes skip vault entries but retain
their env constraints. It is refused on attached clients, V1 and all unrelated
info/maintenance/sync/backup/credential commands. Attach never unlocks/refreshes.
Runtime V1 with saved vault mode uses legacy env forwarding without all-global
unlock; explicit old env/selected vault selections work as before.

Plain host `opencode --standalone` uses normal host environment/auth and does not
read the oc-box vault. It is not an oc-box flag or a replacement for oc-box's
private foreground V2 owner service. Env-file sourcing is a one-off compatibility
option, not the primary V2 vault workflow.

This is **at-rest protection and reduced forwarding, not credential hiding
from an unrestricted agent**. Delivered secrets remain readable inside the
container. Previously copied config/auth, Podman create-time Env values, files,
session exports, other processes, and old backups are independent and are not
purged merely by switching forwarding mode. Env mode deliberately retains its
original broad automatic forwarding and can optionally supplement it with vault
entries. Python cannot guarantee zeroization of secret/key strings. Normal
launcher core dumps are disabled on secret prompts where permitted, but host
root/same-user debugging, swap, malicious binaries, terminal compromise, and
unusual crash collection remain outside the protection.

Use a strong passphrase and keep encrypted backups. A forgotten passphrase has
no recovery path. Changing it or removing a secret does not revoke a provider
credential or erase old ciphertext snapshots. WSL must provide private Unix
permissions and a controlling terminal; do not place the vault in an exposed
project or assume Windows-shared mounts have the same permission behavior.
GitHub installs can opt into private crypto wheels using `--with-vault`, staged
before launcher activation without modifying system/user site-packages. Python
tool installs use the `vault` extra; default installs remain dependency-free.
The earlier selected-vault host probes are accepted; global defaults and
`--no-vault` have no accepted new live Podman lifecycle pass. Unit/native offline
checks do not establish that host acceptance.

## Host-side operations

V1 `oc-box terminal` and V2 `oc-box terminal --maintenance` open a maintenance
shell without copying auth/config or
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
mainline versions; accepting 1.x/2.x does not prove every future release compatible.
Plain purge retains private archive JSON and minimal origin metadata under the
state root's `retired/` directory, without retaining an active workspace or
container storage. Those files may contain sensitive history, have no automatic
expiry, and should be handled like the host session archives they help recover.

Images, OS repositories, npm packages, uv release artifacts, provider APIs,
plugins, and MCPs are upstream dependencies. Baseline downloads use HTTPS where
the launcher fetches them directly; distribution tools follow their configured
repositories and signature policies. Keep the host kernel and Podman patched.

## Backup and restore

Backup/restore uses established age authenticated encryption
for streaming components and an age-protected fresh per-backup key. Only
ciphertext is staged on disk; keys and captured tool errors use anonymous memfd
storage. No provider credentials are forwarded, vault unlocked, container code
executed, or OpenCode config/auth refreshed for backup/info/restore. The complete
project, private home, OS, and registry may contain credentials, so backups and their
decryption keys must be handled as sensitive data. The separate host vault is
not included; its encrypted file needs an independent backup. Outside host env
files are excluded, while env files inside the project are project data.
Workspace credential modes/policies are preserved, with selections as names
only. Restored default/global V2 uses the restoring host's current vault, not the
old host vault or privileges. A full workspace backup may contain config/auth
values already persisted inside it; forwarded credentials are readable by the
agent/tools and may have been saved independently of the vault transport.

New backup passwords use the masked controlling-host-terminal editor, followed
by standard age encryption. The validated password is supplied through a private
echo-disabled PTY; it is never placed in argv, environment variables, or a
plaintext file. age's private prompt output is captured only in memory. Unlock
continues to use age's own terminal prompt for backward compatibility. Recipient
encryption avoids a password prompt and ignores password creation policy.

A stopped container and exclusive workspace lock protect database capture, not
against host editors changing source during backup. Nested mounts and external
bind/volume data are not silently included. Restore stages into new directories
and owned volumes; it rejects existing destinations and archive traversal,
duplicate paths, device/special entries, excessive metadata, sparse extensions,
hardlink escapes, and symlink-ancestor writes. Source ACLs/xattrs and setuid/gid
bits are not restored. Ordinary source symlink targets remain symlinks, never
followed during extraction; later host tools can follow them normally.

An encrypted backup is confidential and tamper-detectable, not proof of trusted
origin: anyone knowing a public recipient can create a new archive for it.
Only restore trusted backups. Container OS files and private home tools/config
are code when later used. Restore never starts an image automatically, does not
reactivate archived device/extra host settings, clears image Env/declared volumes,
disables image healthchecks, and sets the normal fixed entrypoint. Podman remains responsible for OCI layer
validation. No claim is made that arbitrary malicious OCI images are safe.
Partial image imports may remain in Podman's shared cache; cleanup never prunes
unrelated images. Initial backup format is same-architecture, complete/manual,
not an incremental store or running-process checkpoint.

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

Python-package installs use a separate `oc_box` namespace and packaged resource
marker. They do not execute the GitHub self-updater or write the source
installer's global wrapper/release tree. Upgrade them through pipx, uv, or pip
in their environment. The build workflow and PyPI publishing job are separate;
only the latter receives GitHub OIDC permissions and exchanges them for PyPI's
short-lived upload credential. No permanent PyPI token is required. Publishing
is limited to selected reviewed releases/manual dispatches, not pull requests.
This provides authenticated publishing, not a guarantee of harmless package
contents; maintain package/account ownership and review upstream changes.

## Reporting issues

Report security-sensitive issues privately to Paulo Dias at
<paulo.miguel.dias@gmail.com>. Include affected oc-box/OpenCode/Podman versions,
host distro, reproduction steps, and the relevant boundary. Redact access
tokens, auth files, session exports, and unrelated private paths. Do not post
live credentials in a public issue or attach an entire conversation database.

There is no formal support window or security-response SLA. Security fixes are
made in the current source; reinstall and restart to apply launcher/config fixes.
