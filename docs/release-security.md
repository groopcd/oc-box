# Security checks for stable and development releases

Both `master` (stable) and `devel` (prerelease) use the same policy:

| When | Checks and effect |
| --- | --- |
| Ordinary changes | Run focused local tests for the changed behavior. Existing GitHub push/PR CI remains available as feedback. Security scans and the full matrix are not required merge checks. GitHub secret scanning and push protection remain separate. |
| Weekly or on demand | Run **Scheduled security reports** from `master`. It snapshots both branch tips, records their exact commits, uploads CodeQL results to GitHub Code Scanning and summarizes Python dependency advisories. Findings are advisory. A failed or missing scan is unknown, not clean. Dependabot security alerts track the default branch; weekly Actions/Python version-update PRs target both branches. Neither auto-merges. |
| Release candidate | From the appropriate channel branch, run **Build and publish Python package** with `publish=false` and no tag for the selected branch commit. Review the candidate SHA and all jobs before creating/publishing a GitHub release. If the branch advanced, publish only the reviewed commit, not the moving tip. After the GitHub release is published, its event reruns the gates against the exact tagged commit before the PyPI job can receive OIDC. |

The release workflow runs the full per-branch source and package CI matrix
(including native smoke tests), the seven distro dependency checks, CodeQL
(Python, JavaScript/TypeScript and GitHub Actions), and `pip-audit` on the resolved
Python 3.13 release build and test environment. The audit includes build backend
requirements, publishing validation tools and the extras present in that branch's
`pyproject.toml`. Build uses that environment without resolving a second set of
build dependencies. Package installation checks may resolve independently; this
is not a lockfile for end-user installs. The regular source matrix still checks
Python 3.10; release-card rendering tests use Pillow 13 only on Python 3.13.
Pillow is not an oc-box runtime dependency. Scanners and builds have no PyPI OIDC
permission. The upload job rechecks candidate identity and checksums and receives
only the validated distributions. It anonymously rechecks the live public tag
immediately before uploading, including annotated tag peeling. This narrows but
cannot eliminate races with an administrator moving a mutable release tag.

**Release policy:** CodeQL high and critical findings (security score at least
7.0), findings without a trusted score, and any unresolved Python advisory block
the release workflow. Python advisories are not assumed to carry reliable severity
metadata. Failed, skipped, or invalid scans also block. Lower scored source
findings remain visible for review. There is no flag to bypass a failed gate.
Fix or upgrade and rerun on the new commit. For source finding details use the
scheduled Code Scanning results; the release jobs themselves do not upload SARIF
under a tag. The GitHub release is still a maintainer action: its **pre-publication**
review is procedural, while the subsequent PyPI upload is technically gated.
For a manual `publish=true` retry, select the matching channel branch and the
existing release tag. The retry refuses if the tag's workflow definitions differ
from the caller's; use the original release run in that case. These workflow-level
gates apply to candidates containing this policy, not to historical tags.

Scheduled CodeQL analysis and report parsing run with read-only permissions.
A separate upload job handles the SARIF with `security-events: write`; it uses
the matching source only for file fingerprints and does not execute it.

## Reading OWASP coverage

Review the job summaries by commit, tool, language/environment and result. Track
new vs. existing open findings and their age in GitHub's security views. Use
advisory aliases to reconcile duplicate Dependabot and `pip-audit` reports for the
same package. The summary maps a small set of CodeQL CWEs to relevant **OWASP
Top 10:2025** categories and places Python advisories under **A03: Software
Supply Chain Failures**. Unmapped findings are still counted and enforced by
severity. These categories describe findings, not an OWASP compliance score
or ASVS attestation.

For control verification, use [the security boundary](../SECURITY.md) and the
[test layers](testing.md#layers): browser and workspace authorization, guarded
host-file access, vault/backup crypto, and release integrity each need their own
behavioral evidence. Keep external host acceptance separate from offline CI.
The seven-distro job tests installation, **not** OS/image CVEs. Actual image and
OS scanning, npm audit, SBOMs and dependency-license policing are outside this
initial gate. Dependabot does not infer arbitrary install commands in workflows.

To reproduce the Python scan locally with Python 3.13:

```sh
work=$(mktemp -d)
python3.13 -m venv "$work/build"
python3.13 tools/release_python_deps.py > "$work/requirements.txt"
"$work/build/bin/python" -m pip install --only-binary=:all: --upgrade -r "$work/requirements.txt"
bash tools/audit_release_python.sh "$work/build" "$(git rev-parse HEAD)"
```

Add `--gate` to enforce the release policy. Use a clean checkout for an
exact-commit result; this command does not start any container or publish.
