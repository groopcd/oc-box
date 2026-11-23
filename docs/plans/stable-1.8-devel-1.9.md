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
dashboard and sharing. Reconcile still-needed stable-only safeguards into the
common source first, especially exact-native lifecycle and full/portable restore
coverage. Compare the frozen product trees; only release metadata and necessary
channel-specific wording may differ. The local version-only 1.8 reference is
not on the stable branch lineage.
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
external-host evidence. For this release request, disable both LinkedIn and
WordPress publication. Publish only one matching GitHub/PyPI release per
validated version, and compare the public distribution bytes.
