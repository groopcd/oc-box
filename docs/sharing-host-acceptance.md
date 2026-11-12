# External host-sharing acceptance harness

This document belongs to a **separate harness branch**, not the frozen release
candidate. `tools/test_sharing_podman.py` tests the candidate selected by
`--source` and its full `--expect-commit` without modifying that checkout. Do not
merge this harness or add an evidence commit to an already accepted release
source just to run it. Harness and source identities are recorded separately.

## Scope and safeguards

Run only in a normal unprivileged **external Linux host** terminal, outside
oc-box, other containers and shell sandboxes. Root/container guards remain
authoritative: never unset markers, wrap another namespace, or patch the guards
to run this on a container. Unit/process/native rehearsals are not host acceptance.

The harness creates a new private synthetic fixture and a separate rootless
Podman image store. It uses no real project, provider credential, vault or normal
native host service. A bounded loopback canned provider selects real native
tools; it is not real model inference. There is no network control endpoint for
arming tools. Production sharing uses the actual host CLI entrypoint, typed
consent over a real local PTY, and the actual container-native client-MCP relay.
Only the owning native TUI interaction is scripted; launch, authentication,
leases, history, tools and Podman remain real.
Tool-check sessions have a fixed explicit synthetic title. Host execution and
the native rehearsal use the same session-payload builder, preventing unrelated
automatic title inference from consuming a strictly armed provider plan. The
rehearsal includes an untitled negative control and a positive cell with the real
generated production Build plugin enabled; unexpected provider errors remain
fatal rather than being ignored or answered heuristically.

Required checks include:

There are 35 core checks and two additional host-scope checks (37 for a complete
PASS). Each is recorded by name; a missing/false check, unfinished phase,
provider error or forced-owner-shutdown secondary error prevents PASS.

- Actual disclosure warning, typed cancellation/acceptance, non-TTY refusal and
  protected-source refusal; explicit noninteractive acknowledgement is separate.
- Live UTF-8 reads, hidden/new files, atomic child replacement, single-file image
  content at the provider and in native history, and source/read-only preservation.
- Traversal, symlink, hardlink, FIFO and optional actual nested-mount refusals;
  native session denial plus successful allowed access; no ordinary container
  shell/file access to the selected host path.
- Two shares and two real private workspace owners, cross-workspace refusal,
  unchanged declared mounts/listeners/configuration, transparent observation of
  forbidden refresh boundaries, container config/native-auth snapshots, and
  unrelated MCP survival. Both entire input-tree baselines predate registration;
  only explicitly declared synthetic file edits advance those baselines.
- PTY Ctrl-C, actual host-client SIGKILL, native reload and explicit stop;
  ordinary owner wait with only the share lease, held lifecycle lock and unchanged
  sync checkpoint, followed by lease release and normal stop.
  Every historical namespace is rechecked after resume, stop/restart, reset and
  restore; native catalog removal and provider-result publication are boundedly
  allowed to settle rather than inferred from the owner's liveness.
- No automatic regrant on resume/reset/full restore; actual encrypted full
  backup scanning of project/home/registry and supported OCI image layers, a fresh
  stopped restore, retained source/ciphertext, and private bounded activity logs.
  Journal inspection accepts the legitimate private `activity.lock` plus up to
  four data files, and requires nonempty, valid fixed-category metadata records.

The backup oracle distinguishes data deliberately disclosed in native tool
results/history from a **never-read external source sentinel**. The former may
legitimately remain in backups. Decryption goes directly to streaming inspectors,
without extraction or plaintext staging. Unsupported image compression, malformed
archives and unknown OCI blob types refuse rather than claim exclusion.

The two host-specific checks are never silently skipped:

1. `--mount-probe` uses **already installed** `bindfs` and `fusermount3` to make
   one foreground, read-only, no-allow-other FUSE mount between fresh synthetic
   directories. It verifies that host reads work while the share refuses crossing
   the mount, then unmounts only the recorded exact mount identity. It never uses
   sudo, grants container mount capabilities, changes propagation, disables labels
   or installs tools. Do not replace it with a symlink test.
2. `--require-selinux` refuses before fixture setup unless the host actually has
   SELinux enforcing. With enforcing SELinux, the run also checks that container
   process labels remain enabled and selected source labels stay unchanged. A
   distro name, AppArmor or permissive/disabled SELinux is not equivalent evidence.

Without the necessary mount/SELinux scope, completed core checks are reported as
`INCOMPLETE`, not `PASS`; exit is nonzero. A failed requested probe is `FAIL`, not
a skip. Neither outcome authorizes stable publication. Do not change host security
policy simply to turn a report green; use an appropriately configured test host.

## Prerequisites

- Rootless Podman/pasta and normal subordinate-ID/cgroup prerequisites.
- System Python 3.10+ **with `os.memfd_create`** for the retained fixture helpers.
- Git with full source history, uv, age and age-keygen.
- An explicitly installed, unmodified native OpenCode **2.2.6** outside both
  checkouts; do not replace the operator's usual installation.
- For complete scope: installed bindfs/fusermount3, working user FUSE, and an
  enforcing SELinux host. No package is automatically installed by diagnostics.
- A short existing **disk-backed** work parent with space for two workspaces,
  image downloads, a full encrypted backup and a restored workspace. `/var/tmp`
  below is an example, not a guarantee of disk backing/free space.

## Host command

Use separate clean full-history source and harness checkouts. Preserve any
existing untracked evidence by using a fresh checkout, never deleting it to pass
a cleanliness gate. Do not pull, edit, switch or replace binaries during a run.
The work/temp parents must be outside both checkouts and all retained fixtures.

From the harness repository root, on the host:

```bash
uv run --isolated --no-project --python /usr/bin/python3 --with cryptography \
  python -I -B tools/test_sharing_podman.py \
  --source /path/to/clean/candidate \
  --expect-commit 8ac68294d1d607ca9ff57fc5e15d16292ff957c6 \
  --opencode-v2 /path/to/opencode-2.2.6 \
  --work-dir /var/tmp --temp-dir /var/tmp \
  --distro tumbleweed --mount-probe --require-selinux
```

The candidate above is `1.7.83+g8ac6829`; the harness has its own later identity.
If the host lacks enforcing SELinux, stop at the preflight refusal and report
that fact. A run without `--require-selinux` can provide separately scoped core
evidence but cannot complete the currently declared release gate.

## Reports, failures and cleanup

The command prints a fresh `Retained sharing root` and an exclusively created
`sharing-result.json`. Return that report, the exact source/harness identities,
and only the fixed progress/failure categories. Do not return the whole fixture,
private provider bodies, raw native errors, synthetic image/text tool results,
encrypted backup identities or environment dumps. PTY output and child/native
errors remain bounded/private memory, not project files or plaintext logs.
Owner cleanup first permits bounded normal stdin-EOF shutdown; any forced
termination is separately recorded and cannot yield PASS. An owner disconnected
by the deliberate explicit-stop case may return 0 or 1; its actual exit is
recorded separately, and subsequent owning-start/no-regrant checks remain required.

Owner readiness and lifecycle observations use a dedicated inherited control
pipe, never stdout. The child consumes its descriptor setting and marks the pipe
close-on-exec before native commands run. Ordinary fixture/native stdout and
stderr remain in anonymous private memory; they are not parsed as control JSON
or echoed on protocol failure. Reports add bounded fixed owner stages and Python
code-location-only failure traces, never exception messages, locals or full paths.
Owner finish/cleanup drains and validates terminal control frames, including
errors emitted after readiness. A requested-stop exit 1 is accepted only after
readiness was published, the private output contains the fixed history-pending
diagnostic, owned resources are verified stopped, and source identity is
rechecked. Its explicit terminal event must agree with the actual process exit
and the parent's requested-stop policy. Unexpected error frames never become an
allowed exit-1 result.

Reports record each check, `not_run` scope, native/source/harness hashes, provider
counters and primary/secondary failures. Manual observations remain
`NOT_AUTOMATICALLY_PASSED`; this scripted fixture cannot certify real screenshot
capture, desktop clipboard, interactive rendering, real providers, network
containment or general security.

Stop on the first failure and retain its original root/evidence. There is **no
retry or cleanup command in this harness**. Do not rerun an already used root,
move graphroot/runroot, rewrite boot/identity markers, prune Podman globally or
remove encrypted evidence. A subsequent fresh run is separate evidence. Exact
fixture resources can be reviewed for label/ownership-checked cleanup after the
report is inspected; source files and original reports are never disposable.

## Local preparation only

From this harness checkout, offline tests and real native helper rehearsals are:

```bash
uv run --no-project --with-editable . --with cryptography python -B -m unittest discover -s tests -p 'test_sharing*py' -v
python3 -B tests/smoke_sharing_host_helpers.py --opencode /path/to/opencode-2.2.6
```

The native helper rehearsal uses a private native server and an explicitly local
test adapter for workspace discovery/exec; it does not execute this host runner,
weaken its host guard or prove real Podman leases/mount/restore/SELinux behavior.

## First host attempt: failed harness protocol, not acceptance

The original `f28bbd7` harness failed before its first named check on candidate
`8ac6829`, with zero provider requests/errors and a secondary forced-owner
termination. Its second `temporary_location()` entry printed a staging diagnostic
outside owner output suppression. The parent incorrectly treated stdout as a
JSON readiness channel, mapped the resulting parse error to `operation-failed`,
and began cleanup while cold startup could still be active.

A local synthetic process using the actual retained helper reproduced that
protocol defect without Podman, native services or bypassing host guards. The
correction separates control from stdout and adds real-helper/descriptor/error-
framing regressions. These preparation checks are not a corrected host pass.
Keep the original failed report/store unchanged. A new reviewed harness must run
against the unchanged candidate in a fresh fixture; never replay, relabel or
overwrite the failed attempt to establish acceptance.

## Second host attempt: failed provider-plan isolation, not acceptance

The `cae879f` run reached owner readiness and completed the consent, protected-
source and registration invariants, then failed on the first shared-text check.
Its provider recorded three requests and one error; owner shutdown returned 0
with no secondary errors. The host runner omitted a native session title while
the passing local rehearsal supplied one.

A private native 2.2.6 reproduction with an untitled synthetic read session
produced the same three-request/one-error signature: an auxiliary no-tools
title-generation request reached the strict tool-plan provider. The correction
gives host and rehearsal sessions the same explicit content-free title, retaining
the strict refusal/error criterion. Fixed-enum provider diagnostics expose counts
and plan phases, never prompts, tokens, headers, paths or tool-result content.

The original report remains failed evidence. Neither its completed setup checks
nor the corrected local/native controls constitute a new host-sharing PASS.
Keep both earlier failed fixtures unchanged and use another fresh fixture for
the corrected, reviewed harness against the same frozen candidate.
