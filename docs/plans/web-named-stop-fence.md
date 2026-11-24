# Internal named owner / stop fence

This Linux host-private primitive has no launcher caller. It does not activate a
service, touch Podman, verify drain or stop, acknowledge completion to the second
terminal, or change the recovery control directory and schema.

`enroll_new()` requires the original held workspace lifecycle lock and caller
proof that the registration has **never** enrolled. A missing file is not that
proof. Only exclusive creation followed by a synced record under the new flock
establishes initial unarmed state; stop never initializes or repairs it. Missing,
partial, malformed, unsafe, or substituted state refuses. The `0600`
`named-stop.lock` inode is both gate and state. A fixed-size versioned record binds
the registry path and the directory/file device and inode identities and detects
torn writes. The record is durable, not an archive, credential, or workspace
identity. A path, mount identity, or inode change can require separate review.

`arm()` holds the real workspace lifecycle lock, serializes with direct stop
through this gate, and publishes a fresh opaque generation before returning.
The foreground retains its original pinned descriptors and kernel lock binding.
A second-terminal `route_stop()` does exactly one of two things:

- On a verified unarmed record, synchronously executes the caller's entire
  ordinary stop inside the gate. A durable in-progress record precedes the
  callback; only literal `True` confirming that no stop work can still execute
  restores unarmed state. Death, exceptions, and unknown outcomes retain the
  refusal even after the flock is free. The callback must revalidate its
  registration and resource selection there and must not acquire the lifecycle
  lock or re-enter the gate. `DIRECT` reports routing, not that the callback
  verified container stop.
- On an armed record, durably records intent for that generation and returns
  `PENDING`, without running the callback. This is not a successful stop. A
  future foreground coordinator must observe the scoped intent separately from
  the legacy history stop token; neither establishes drain. No control error
  authorizes fallback to a direct stop.

The lock order is lifecycle → named gate for the owning foreground or other
lifecycle mutations, and named gate → ordinary stop/recovery admission for the
direct-stop callback. The gate does not serialize all ordinary launcher work.
An owner never keeps the gate while running or draining sites. `complete()`
is only for the original foreground with independently
verified true drain and same-run stop, and rechecks the exact lock, generation
and storage before publishing and releasing. Exiting the owner scope, an
interrupt, process death, a missing socket, and elapsed time do not clear the
record. Future lifecycle mutators must call `require_unarmed()` under their
lifecycle lock before making changes.

Integration still needs positive enrollment provenance, foreground retention
and verified cleanup, fixed pending/error reporting, and an external-host
gate. The lock checks require Linux `/proc/self/fdinfo` flock reporting. The
checksum and descriptor checks are not protection against a malicious same-user
writer with direct filesystem or Podman access, or against an older launcher
that ignores the fence.
