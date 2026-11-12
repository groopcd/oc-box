# Temporary host-folder sharing: integration handoff

## Ownership and merge

The sharing session delivers `feature/transient-folder-sharing`. The TUI session
owns its final merge into `feature/release-1.7`, candidate freeze, combined host
acceptance, `master`, release tags and publication. Do not treat this handoff as
permission to publish before those checks.

The sharing branch includes the release integration baseline
`4a76c5184eb8a1ebe673929ca84f6918f8c291b5`. Its application runtime remains
byte-identical to that baseline (`src/oc_box` tree
`8391641286d5d15adf94b385081ee2a52067a645`), which in turn matches the tested
session-recovery runtime `d736bc1bb33ffeedee65ad3ec281de77c1e31847`.
No additional runtime rewrite is needed to integrate temporary sharing.

Use the pushed sharing branch's exact commit, not an earlier local branch pointer.
If the release branch has not advanced past the baseline, its owner can integrate
by fast-forward. If it has advanced, merge/review the latest sources normally and
validate the actual final candidate; never reset unrelated work or silently reuse
an older source identity. The commit-count version rule remains unchanged.

## Delivered sharing work

- Foreground `oc-box share PATH`: typed disclosure consent, transient live
  read-only directory/text/image tools over native V2 client-MCP, confinement,
  existing-owner leases and revocation without mounts or saved MCP configuration.
- Source-pinned external-host harness using production consent/PTY transport,
  strict synthetic provider plans, fixed-title sessions, native denials, two
  workspaces, isolation, lifecycle revocation and encrypted backup/restore checks.
- Corrected image oracle: bounded opaque values only for valid `SCHILY.xattr.*`
  keys, with strict semantic fields, framing, duplicate/resource/path/OCI checks.
  All raw extension payloads are inspected for the never-read sentinel, including
  compressed image-layer metadata.
- Byte-identical historical oracle `tools/sharing_archive_scan_original.py`,
  SHA-256 `455abf6daab5f3777471fc960103350ed316d224dd598c20b9e08b3f4670193c`.
  Do not edit/reformat it or replace old report hashes with current scanner hashes.
- Host-only file diagnostics with independent age authentication, fixed output,
  bounded anonymous captures, evidence preservation and no engine/lifecycle use.

## Checks and CI caveat

The combined runtime plus sharing harness passed a local full run of **1,452
tests with two environment-specific skips**, plus the real private native 2.2.6
sharing rehearsal. Its negative untitled control reproduces the auxiliary
provider refusal; the fixed-title positive cell uses the actual generated Build
plugin and passes consent, live reads/images, denials and revocation. These are
offline/native checks, not rootless-Podman acceptance.

The preceding scanner-only commit
`11fa34cfe9adb27513c171569361305c11f3523f` passed Python 3.13/native integration
and all seven distro jobs. Its initially cancelled Python 3.10 job was retried;
the retry failed only in the existing TUI test
`test_tui_sessions.FavoriteStoreTests.test_locks_serialize_independent_writers_and_no_staging_left`
with fixed category `favorites-busy`. No sharing tests failed. Preserve that
failure and review the final branch's actual CI; do not describe the old run as
fully green. The sharing session does not change favorites locking or weaken the
test. Original run: <https://github.com/groopcd/oc-box/actions/runs/43272643613>.

## Host evidence and work still needed

All three original sharing attempts remain failed evidence. The latest one used
clean `8ac68294d1d607ca9ff57fc5e15d16292ff957c6`, completed 28 checkpoints, then
failed backup inspection. Its provider consumed 43 plans with 86 requests and
zero errors; it did not complete backup/restore or final label preservation.

Subsequent file-only diagnostics authenticated all four components. Project,
home and registry passed the old oracle; the image refused. A verified-original
observer established the first branch as `xattr-value-nul`, on unchanged encrypted
image bytes, with producer exit 0 and scanner exit 1. This motivated the narrow
oracle correction; it does not prove that later image records pass.

The corrected retained-image comparison has **not yet been run on the host**.
For that historical probe only, use the frozen corrected diagnostic checkout
`11fa34cfe9adb27513c171569361305c11f3523f`, the original unchanged `8ac6829`
source and fixture, and `--current-image-scanner`. Newer history helpers must not
replace recorded old helper identities. See [the detailed harness handoff](sharing-host-acceptance.md).
Even `scanner_category: ok` remains `acceptance: false`, not a retroactive PASS.

Before final publication, obtain a **fresh complete sharing report on the actual
frozen integrated candidate**, using the current source-pinned sharing harness:

```bash
uv run --isolated --no-project --python /usr/bin/python3 --with cryptography \
  python -I -B tools/test_sharing_podman.py \
  --source /path/to/clean/frozen/candidate \
  --expect-commit "$CANDIDATE_COMMIT" \
  --opencode-v2 /path/to/opencode-2.2.6 \
  --work-dir /var/tmp --temp-dir /var/tmp \
  --distro tumbleweed --mount-probe --require-selinux
```

Require all **37** checks, complete `PASS`, correct source/harness/native
identities and no secondary failures. Missing mount/SELinux scope is not a pass.
Use fresh private synthetic state, never real projects/credentials. Retain all
old fixtures/reports unchanged; do not move stores, repair boot markers or bypass
host guards. All live Podman checks run on the external host, never in oc-box.

The release owner's remaining combined gates and any recovery fault/race scope
decisions belong to the TUI/session-recovery integration, not this sharing
handoff. Existing component reports and successful builds do not accept a later
candidate or authorize `master` advancement by themselves.
