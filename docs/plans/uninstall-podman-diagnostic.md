# Podman 5.8.1 uninstall evidence — source audit and guest diagnostic

This is a recipe for a later, independently approved disposable KVM probe,
**not** an acceptance report or a runnable uninstall. No VM or Podman was run
to produce this audit. The old 5.8.1 guest's unrelated runtime results do not
validate inventory or cleanup.

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

## Reproducible, read-only guest probe

Use an **already provisioned disposable KVM guest and synthetic store** only.
Do not run this on a normal host or an existing user's store. Use a fresh
per-run guest disk, no host shares, no credentials, no remote connection,
and a dedicated non-root `ocbox-probe` login. Its default rootless overlay
graph/run/volume directories must already exist. Supply the reviewed checkout
and known 5.8.1 executable via the guest fixture image. Do not remove or edit
distribution configuration just to make a preflight pass; record a fixed
refusal if the fixture does not match the no-override profile.

Prepare test states separately before freezing the diagnostic inputs. Use
only images/data generated for this disposable fixture. One starting state
should have a stopped canonical owner, local private named home, and a
second **stopped** container referencing that home by name/subpath and the
owner by a Libpod dependency. Use the synthetic workspace ID `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`.
For separate fixture states, retain a preexisting mounted working container,
an external storage container, and a known bind alias to the synthetic
volume at `/home/ocbox-probe/.uninstall-probe/volume-alias`. Include negative
controls with a distinct directory at that same path and an unsupported
external volume. Do not create, start, stop, mount, unmount, or delete
anything during this diagnostic. Any provisioning or eventual guest teardown
belongs to the separate fixture owner.

Run from the root of the guest's reviewed checkout as that login:

```bash
python3 -B -I - "$PWD" <<'PY'
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

if Path.home() != Path("/home/ocbox-probe") or os.geteuid() == 0:
    print('{"result":"INCOMPLETE","reason":"not-dedicated-probe-user"}')
    raise SystemExit(1)

sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from oc_box.workspace_podman import InventoryError, _path_fd, local_inventory
from oc_box.workspace_uninstall import _mount_id


def alias_facts(home):
    unavailable = (False, None, None)
    if home is None:
        return unavailable
    try:
        with ExitStack() as stack:
            paths = (home.mountpoint.path, "/home/ocbox-probe/.uninstall-probe/volume-alias")
            entries = [
                stack.enter_context(_path_fd(path, os.geteuid(), directory=True))
                for path in paths
            ]
            (left, a), (right, b) = entries
            return (
                True,
                (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino),
                _mount_id(left) != _mount_id(right),
            )
    except (OSError, ValueError):
        return unavailable

ident = "a" * 32
names = ("oc-box-" + ident, "oc-box-home-" + ident)
try:
    with local_inventory("/usr/bin/podman") as reader:
        snapshot = reader.scan((names,))
        owners = [row for row in snapshot.containers if row.name == names[0]]
        homes = [row for row in snapshot.volumes if row.name == names[1]]
        owner = owners[0] if len(owners) == 1 else None
        home = homes[0] if len(homes) == 1 else None
        others = [row for row in snapshot.containers if row is not owner]
        stopped = [row for row in others if row.status in ("created", "exited", "stopped")]
        alias_available, alias_same_directory, alias_mount_differs = alias_facts(home)
        print(json.dumps({
            "result": "INCOMPLETE",
            "rootless_581_profile": snapshot.profile.scope.rootless and snapshot.profile.version == "5.8.1",
            "owned_container_observed": bool(owner and owner.managed and owner.workspace_label == ident),
            "owned_home_observed": bool(home and home.workspace_label == ident),
            "owner_writable_directory_pinned": bool(owner and owner.writable),
            "mounted_catalog_available": bool(owner and owner.mounted is not None),
            "owner_observed_mounted": bool(owner and owner.mounted is True),
            "stopped_named_volume_consumer": any(
                name == names[1] for row in stopped for name, _ in row.volume_refs
            ),
            "stopped_subpath_consumer": any(
                name == names[1] and nested
                for row in stopped for name, nested in row.volume_refs
            ),
            "stopped_dependency_observed": bool(owner and any(
                owner.ident in row.dependencies for row in stopped
            )),
            "home_mount_count_zero": bool(home and home.mount_count == 0),
            "lexical_store_overlap_observed": any(
                row.lexical_store_overlap for row in snapshot.containers
            ),
            "bind_alias_descriptors_available": alias_available,
            "bind_alias_same_synthetic_directory": alias_same_directory,
            "bind_alias_mount_ids_differ": alias_mount_differs,
            "inventory_populated": bool(snapshot.containers or snapshot.volumes),
            "adapter_complete": snapshot.complete,
        }, sort_keys=True))
except InventoryError as exc:
    print(json.dumps({"result": "INCOMPLETE", "reason": exc.reason}))
    raise SystemExit(1)
except Exception:
    print('{"result":"INCOMPLETE","reason":"diagnostic-unverified"}')
    raise SystemExit(1)
PY
```

The bind-alias comparison uses only the known synthetic home and fixture
alias, with no-follow directory opens, device/inode and kernel mount IDs
(not `realpath` or device alone). A symlink refusal is **unverified**, not
“no alias”; unavailable descriptor comparisons emit `null`. A nested
subpath/ancestor alias needs its own ancestry proof; a matching inode test
of just the named roots cannot cover it.

If the mount catalog is unavailable, the next native experiment has to
verify that a separately provisioned rootless namespace is the same engine
and store before the exact operand-free JSON list. No automatic `unshare`
fallback is part of this recipe or adapter. Use bounded private pipe
capture and only emit fixed shape/identity booleans; never print `info`,
`inspect`, environment, mount paths, engine errors, or logs. `info` and
other read-only Podman queries can update native runtime bookkeeping;
the recipe does not promise byte-for-byte unchanged store metadata.

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
