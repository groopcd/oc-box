# Approved introduction, 2026-11-16

The project owner reviewed this exact text/image preview, then requested posting
it to the connected personal LinkedIn profile. This is a LinkedIn introduction
of the **existing** stable v1.6.94 release, not approval to publish software.

The one-off `feature/linkedin-introduction` controller pins both file digests,
the existing stable tag/commit/release ID and the selected profile hash. It
retains the normal public-PyPI identity checks and immutable pre-create
claim/receipt protection. No existing package asset or release body is changed.

The manual `introduce` action is branch-restricted; future ordinary automatic
announcements are unchanged. `introduction-status` only reads the matching post.
Do not silently edit these approved inputs or delete posting records to retry.
