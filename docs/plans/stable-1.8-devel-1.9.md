# Stable 1.8 and development 1.9

The next stable series is **1.8** (final releases from `master`); the next
development series is **1.9** (`.dev0` prereleases from `devel`). Future stable
minors are even and future development minors are odd. Keep the existing
full-history baseline 29, original tag and package identities, and GitHub/PyPI
artifact provenance. This document does not record a release or host pass.

The branches have diverged. A stable 1.8 promotion needs its own clean candidate
based on the current stable lineage, a review of the changes brought over from
development, and the applicable stable gates in the
[publication checklist](../../CONTRIBUTING.md#release-gate-and-publication-checklist).
The initial 1.8 and 1.9 releases share the full product code, including the
dashboard and sharing. The shared source retains exact-native V2-only lifecycle,
global-vault helper, full/portable restore and legacy dual-major checks. The
stable merge must preserve the stable branch as its first parent, with the
reviewed common source as its second parent. Compare the frozen trees; only
the tracked release series and channel should differ. The earlier local
version-only 1.8 reference is not on the stable branch lineage.

The browser retains native one-use links over loopback HTTP. Named login,
LAN binding and CA setup are deferred. The full Studio mockup is still offline
documentation, not the live `--workspace-layout` shell. The dashboard keeps the
centered text cube; direct Kitty/Konsole graphics and ASCII fallback apply to
native TUI startup, not the dashboard logo.

Do not treat the 1.9 branch, an earlier 1.7 acceptance run, or a rename of the
source version as stable acceptance. When promoting formerly experimental
features, use the [separate stable promotion requirements](../releases/1.7.md#stable-promotion-remains-separate).

After the split, fix bugs and security issues on 1.9 first. Backport the reviewed
fix and its regression tests to 1.8 with source-commit attribution, resolving and
testing any stable-specific differences. New features stay on 1.9. Apply the
release gates to each tested backport; do not move old tags.

The source-identity readers in `tools/test_session_sync_podman.py` and
`tools/test_home_ownership_podman.py` recognize `1.8` finals and `1.9` `.dev0`
identities while preserving historical report formats, exact Git-verified
source hashes and refusal rules. Keep retained stores untouched. A synthetic
identity-parser test is not a new rootless Podman run.

Use the [release checklist](../../CONTRIBUTING.md#release-gate-and-publication-checklist)
on each frozen candidate independently, including build-only CI and relevant
external-host evidence. Keep the 42-check baseline intact and run both
post-stop recovery cases separately with their pinned 2.2.6. Use the
exact-2.3.0 [V2-only lifecycle and vault profiles](../testing.md#current-opencode-npm-upgrade-and-advisories)
and record their actual source identities, refusal checks and unrun legacy
cases. Evidence for the current-native browser path and interactive manager
remains separate from offline native checks or earlier host reports.

For this release request, disable LinkedIn and WordPress. Publish only one
matching GitHub/PyPI release per validated version, and compare the public
distribution bytes.
