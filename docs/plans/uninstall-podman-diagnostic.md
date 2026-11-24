# Podman 5.8.1 uninstall evidence — source audit and guest diagnostic

This is the contract for the source-only guest fixture
[`tools/podman_uninstall_guest.py`](../../tools/podman_uninstall_guest.py),
**not** an acceptance report or an uninstall entry point. No VM or Podman was
run to produce this audit. The old 5.8.1 guest's unrelated runtime results do
not validate inventory or cleanup.

## What the pinned source establishes

Upstream tag `v5.8.1` peels to
[`c6077f645788743258a1a749f8005b4fb3cbe533`](https://github.com/podman-container-tools/podman/commit/c6077f645788743258a1a749f8005b4fb3cbe533).
A distribution's version string alone does not establish that it built this
source without patches.

| Fact | Source and conclusion |
| --- | --- |
| Catalog and inspected identity | [`pkg/ps/ps.go`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/pkg/ps/ps.go) includes stopped Libpod rows with `--all` and separate storage-only rows with `--external`; disappearing rows can be skipped. [`libpod/info.go`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/libpod/info.go) counts Libpod objects separately. [`libpod/container_inspect.go`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/libpod/container_inspect.go) reads the storage container and layer by ID, and projects `Id` and `Created` from the configuration. These are consistency checks, not a transaction. |
| Dependencies and stopped consumers | [`Container.Dependencies`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/libpod/container.go) returns namespace links and configured generic dependencies. [`GetMounts`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/libpod/container_inspect.go) reports named volumes and subpaths from configuration, even when stopped. Resolve all reported references against the full list and reject unsupported storage shapes. |
| Mounted state | [`cmd/podman/containers/mount.go`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/cmd/podman/containers/mount.go) prints `id`, `Names`, `mountpoint` for the operand-free JSON list. [`ContainerMount` in the local ABI](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/pkg/domain/infra/abi/containers.go) queries storage and Libpod mount state only in that no-selection branch. A named operand, `--all` or `--latest` is a different branch and can mount containers. The same source can refuse outside the rootless overlay namespace; `State.Running=false`, `Restarting=false`, zero PID and an overlay `MergedDir` do not substitute for a successful mount-list observation. |
| Container and volume scalar omissions | [`InspectContainerState`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/libpod/define/container_inspect.go) omits a zero `ConmonPid`; its `Restarting` field is marked TODO and the native constructor does not populate it. [`InspectVolumeData`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/libpod/define/volume_inspect.go) omits `Anonymous=false` and empty `StorageID` but not `MountCount`. [`Volume.Inspect`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/libpod/volume_inspect.go) copies generation time and mount count from configuration/state. `Scope` is unconditionally `local` for compatibility; it is not backing-store proof. |
| External storage and plugin effects | [`VolumeList`](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/pkg/domain/infra/abi/volumes.go) calls `Volume.Inspect` before formatting, and `Volume.Inspect` can query a plugin socket. The adapter rejects configured plugins before either list or inspect. The built-in `configFile` slot comes from [vendored storage defaults](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/vendor/go.podman.io/storage/types/options_linux.go); merely seeing that string does not prove which file exists. |
| Storage and bind aliases | [Vendored overlay metadata](https://github.com/podman-container-tools/podman/blob/c6077f645788743258a1a749f8005b4fb3cbe533/vendor/go.podman.io/storage/drivers/overlay/overlay.go) provides the layer's `UpperDir`; the stage pins it and local volume `_data` without following links and checks the mount IDs. `GetMounts` copies bind `Source` from the spec; it does not bind that string to a VFS object, prove canonical ancestry, or inventory all implicit/runtime/host mounts. Absence of lexical overlap is not absence of an alias. |

## Run contract for the disposable guest

The outer harness owns VM isolation, source pinning, guest bootstrap and
disposal. Use a new writable guest disk, no NIC, no host shares or forwarded
sockets, no real projects/credentials, and no existing user's Podman store.
Do not pass through other writable disks. The guest must already have Fedora
44, Python 3, a root-owned native Podman 5.8.1 at `/usr/bin/podman`, and
working rootless overlay prerequisites (including subordinate UID/GID ranges
and their helpers). Do not install dependencies or change host/guest engine
policy to turn a refusal into a pass.

1. Copy the hash-verified clean archive into the guest at the exact
   `/opt/oc-box-uninstall-probe/source` path. Its hierarchy and the relevant
   source files must be root-owned, non-symlinked and not writable by the
   probe UID. The guest does not infer checkout ownership from an arbitrary
   environment variable or writable share.
2. Provision a fresh account `ocbox-probe`, not the console user `ocboxvm`.
   It needs its **empty**, owned, mode-0700 `/home/ocbox-probe` on a guest
   btrfs/ext4/XFS filesystem and no supplementary groups other than its
   primary group. Do not copy skeleton dotfiles or run a login shell which
   populates the home. No normal runtime directory or auth files are needed.
3. Guest root publishes `/run/oc-box-uninstall-probe/permit.json` under
   root-owned, non-writable-to-others, non-symlinked ancestors.
   The permit is a root-owned, single-link, mode-0444 regular file, at most
   2048 bytes, with **exactly** these fields:

   | Field | Value |
   | --- | --- |
   | `version` | integer `1` |
   | `purpose` | `oc-box-uninstall-podman-581` |
   | `nonce` | fresh lowercase 32-hex run identifier, also the sole CLI argument |
   | `uid` | numeric `ocbox-probe` UID |
   | `boot_id` | this guest's lowercase kernel boot UUID |
   | `fixture_sha256` | lowercase SHA-256 of `tools/podman_uninstall_guest.py` from the pinned archive |
   | `podman_sha256` | verified lowercase SHA-256 of the selected guest `/usr/bin/podman` ELF |

The fixture checks the permit, executable and source ownership, real and
effective UID, Fedora release, QEMU DMI, initial user namespace, loopback-only
network interfaces and known share filesystems before provisioning. These
are guest-side checks, not hypervisor attestation or a substitute for the
outer no-share/no-network policy. From a pre-provisioned `ocboxvm` console,
run without preserving groups or a login shell:

```bash
sudo -H -u ocbox-probe -- /usr/bin/python3 -B -I -S \
  /opt/oc-box-uninstall-probe/source/tools/podman_uninstall_guest.py "$probe_nonce"
```

The fixture reserves `fixture-<nonce>` once under that empty home. It pins a
fresh overlay graph/run/volume store and private HOME, XDG runtime/config,
mounts, hooks, registry, empty auth and temp paths. It checks the observed
local engine against the synthetic store before creating anything. Its only
native writes import a tiny locally generated tar (three empty directories),
create one labeled local home volume, and create two containers without
starting either. Both use `--pull=never`, the same full local image ID and
`--network=none`. The owner binds a synthetic project; the stopped foreign
consumer references the home subpath and the owner through `--requires`.
The fixture creates the empty subpath only below the freshly verified volume
descriptor. Uncertain or nonzero results stop provisioning without a retry.

After those exact operations, only read-only queries are admitted. The probe
uses the native adapter's projection with the separately bound **synthetic**
configuration path, not production admission of arbitrary storage overrides.
It evaluates the empty mount list when available and compares an independent,
distinct synthetic directory against the home volume by no-follow descriptor
identity. Positive mounted and same-device bind-alias controls are deliberately
**not provisioned**: the fixture does not enter another namespace or mount a
container or directory to compensate for missing evidence. There is no
start/stop, engine reset, removal, or directory cleanup. Everything is left
on the disposable disk for the outer harness.

## Result and interpretation

Stdout is one bounded JSON line with fixed keys and only booleans or enum
values. Raw ID/path/inspect data, native stderr, environments and logs are
never emitted.

| `result` | Exit | Meaning |
| --- | ---: | --- |
| `complete_diagnostic` | 0 | All intended **synthetic baseline** observations, including the two unmounted rows and distinct-descriptor negative control, agreed. Not uninstall acceptance or positive alias/mount proof. |
| `descriptor_incomplete` | 1 | The named reference and dependency scan agreed, but mounted state or the negative descriptor comparison could not be established. |
| `adapter_incomplete` | 1 | The populated inventory projection or a required fixture observation failed; populated planner admission must stay false. |
| `provision_incomplete` | 1 | Setup or one checked synthetic step refused; optional `stage` is one of `store_setup`, `verify_empty`, `synthetic_image`, `local_home`, `volume_subpath`, `stopped_owner`, `stopped_consumer`, `freeze`. Retain the disk evidence. |
| `guard_refused` | 2 | Guest, owner, boot, source, executable, empty-home or invocation proof failed before provisioning. |

When direct rootless overlay mount listing is unavailable, report it as
unknown; no automatic `unshare` fallback is attempted. A future positive alias
control must prove ancestry and identity in the actual mount namespace, not
just compare the named root's inode. Podman read-only commands can update
private runtime bookkeeping, so the freeze does not claim store metadata is
byte-for-byte unchanged.

## Completeness still missing

- Native acceptance of the exact 5.8.1 profile, including distribution
  config handling and the rootless mounted-list context and postconditions.
- Authorized no-follow bind identity and ancestry across all relevant mounts,
  including same-device binds and implicit/runtime or direct host consumers;
  no safe fixed Podman report supplies that physical proof. A successful
  configured dependency and named-volume reference scan does not supply it.
- The pinned non-force mutation transport, exact review/gating and durable
  partial-uncertainty behavior still need integrated validation. A named
  volume is not removed with atomic identity-conditional semantics.

Do not adapt a refusal into a smaller successful uninstall. The adapter
continues to reject every populated store at the planner boundary.
