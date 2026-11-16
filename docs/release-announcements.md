# Optional LinkedIn release announcements

The release publisher can send public release copy and a locally rendered
1200×630 PNG to a **personal LinkedIn profile**, through Buffer. The same
integration supports stable final releases and experimental devel `.dev0`
prereleases, without merging devel product code into stable.

**Devel publication only generates a preview, never a post**, even when posting
is globally enabled. Publish software first, show the complete preview text and
card to the release owner, and obtain explicit approval before dispatching a
devel announcement with the reviewed content hashes. Stable auto-announcements
retain their existing opt-in behavior.

**Disabled by default.** Storing a key alone does not enable posting. This is
release-owner automation, not an installed oc-box feature, and needs no provider
key, LinkedIn developer app, paid image service or additional runtime dependency.
Buffer Free currently includes one API key and API access within its quotas;
service plans and limits can change.

## One-time setup

1. Connect the intended **personal LinkedIn profile** in Buffer.
2. In [Buffer Settings → API](https://publish.buffer.com/settings/api), choose
   **Personal Access → New Key**. Grant only `accountRead`, `postsRead` and
   `postsWrite`. A one-year expiry reduces renewal work; renew before expiry.
3. Store the key as repository **Actions secret** `BUFFER_API_KEY`.
   Never commit it, put it in a command argument, paste it into an issue/chat or
   include it in a screenshot. There is no need to create a Buffer App Client.
4. Once this workflow is integrated, open **Actions → LinkedIn release
   announcement → Run workflow**, select a branch containing this integration,
   choose **channels**, and leave the release tag/post ID blank.
   This action only queries Buffer. It prints LinkedIn **profile** IDs, public
   names and connection/queue availability; it creates no posts or GitHub assets.
   Repository Actions logs are visible to readers of this public repository.
5. In **Settings → Secrets and variables → Actions → Variables**, add:

   | Repository variable | Value |
   | --- | --- |
   | `BUFFER_LINKEDIN_CHANNEL_ID` | The exact profile `id` from step 4, not a LinkedIn URL or Buffer organization ID |
   | `LINKEDIN_ANNOUNCEMENTS` | `true` enables stable automatic posting and approved manual posting; devel publication still only previews |

   Check the display name and that the profile is connected, unlocked and its
   queue unpaused. A different profile or Page is never selected automatically.
   The channel ID is not a credential; the API key must remain a secret.

Manual Actions workflows generally need to exist on the repository's default
branch before they are available for dispatch. Installing this feature on a
feature branch alone does not activate either release channel. Apply the narrow
workflow/tool/documentation change to both channel lines as approved, preserving
their different build extras and runtime source. Do not rewrite published tags
or merge devel runtime into master just to enable announcements.

Buffer keys expire, and LinkedIn connections can occasionally need reconnecting.
The Free plan's single-key limit means replacing a key can invalidate its old
value; update the GitHub secret immediately. To disable posting, remove or set
`LINKEDIN_ANNOUNCEMENTS` to `false`; revoke the key in Buffer if compromised.

## Feature summary and image

The card uses at most three short bullets, preferring these release-note headings:

1. `## LinkedIn highlights` — optional announcement-specific wording.
2. `## Highlights`
3. `## What's new`

For example:

```markdown
## Highlights
- Faster workspace startup with retained dependency caches.
- Clearer session-history recovery notices.
- Safer encrypted backup inspection.
```

Continuation lines are folded, Markdown formatting is removed, and each card
highlight is bounded to 350 characters. Code fences are ignored. Missing
highlights refuse the announcement rather than inventing features or summarizing
unrelated commits. Without a full-copy block, the post retains the existing
three-bullet template and 2,800-character bound.

### Complete first-post copy

For an introduction and a comprehensive feature list, put the **complete public
post** between these exact, standalone release-body markers. Keep the three card
highlights in their own section. For example, with a synthetic release identity:

```markdown
## LinkedIn highlights
- Manage private workspaces from the terminal.
- Browse sessions and inspect workspace state.
- Review encrypted backup operations.

## LinkedIn post
<!-- linkedin-post:start -->
oc-box 1.7.250.dev0 · development preview — experimental

oc-box runs OpenCode in private rootless-Podman workspaces on Linux.

New in this development preview:
• First feature, with its precise experimental scope.
• Second feature.
• Third feature.
• Fourth feature: full-copy bullets are not dropped or truncated.

This is a development prerelease, not a stable recommendation.

Release notes: https://github.com/groopcd/oc-box/releases/tag/v1.7.250.dev0

#OpenSource #Linux #DeveloperTools
<!-- linkedin-post:end -->
```

Replace the example features and version with verified release information.
The first line must be exactly `oc-box VERSION · development preview — experimental`
for devel, or `oc-box VERSION · stable release` for stable. The canonical
`Release notes: https://github.com/groopcd/oc-box/releases/tag/TAG` line is required;
devel also requires the exact warning line shown above.

Full copy is plain text, not Markdown to render: punctuation, paragraphs and
every bullet are retained. Nothing is appended after approval. Only marker
framing line breaks are removed and CRLF becomes LF. Empty, duplicate or malformed
blocks and hidden/control characters (including tabs and zero-width/bidi
controls) are refused. Full copy is limited to **3,000 UTF-16 code units**,
including heading, whitespace, URL and hashtags; supplementary characters such
as many emoji count as two. It is never silently shortened. The fallback
template must also fit that UTF-16 bound.

The branded card contains the release version, selected channel, key features
and drawn workspace/pipeline artwork. Devel text and cards explicitly say
experimental; they do not present incomplete acceptance as a stable recommendation.
No generative AI service is involved. Rendering uses Pillow in a throwaway
Actions virtual environment, not in the ordinary oc-box installation.

The text includes the immutable release URL. The PNG is uploaded as a public
release asset because Buffer fetches images from a public URL. Keep that asset
available while Buffer delivers the post. Public release text/artwork and
posting metadata go to GitHub, Buffer and LinkedIn; no private host reports or
credentials belong in the highlights.

Choose **preview** with a published release tag to download a `linkedin-preview`
artifact containing exact UTF-8/LF `linkedin-post.txt` (no extra trailing newline),
`linkedin-card.png`, and public `linkedin-approval.json`. The manifest identifies
the release/tag/commit and contains `text_sha256`, `image_sha256`, and the UTF-16
length. The hashes cover the actual post text and PNG bytes. Preview does not
call Buffer, upload release
assets or create a post. The chosen tag must contain the integration; old tags
are not modified to add it. Preview needs a published GitHub release but not a
published PyPI version; it is not acceptance or permission to publish software.

## Publication hook and verification

The `linkedin` job runs only after the actual PyPI `publish` job succeeds
**and** the build's existing `publish_validated` output is true. Both release
events and manual `publish=true` dispatches pass action **preview** for validated
`.dev0` tags. This needs no Buffer key and works even when the posting enable
variable is false. Stable releases still pass **announce** only when
`LINKEDIN_ANNOUNCEMENTS` is exactly `true`. Build-only dispatches, push/PR checks
and failed/skipped publication do not post. Both paths use the validated
immutable release tag, not moving branch HEAD.

The reusable workflow's `workflow_call` interface accepts `action` (default
`announce` for older stable callers) and `release_tag`. It does not interpret
a blank action as permission to announce. For **every devel announce dispatch**,
the workflow requires both lowercase 64-hex approval digests **before checkout**,
not merely when the selected tool eventually runs. Stable automatic calls
without hashes retain their existing behavior.

Before exposing the Buffer key or executing selected devel release code, an
isolated inline Python preflight checks the exact tag commit and its
`tools/announce_release.py` Git blob. The checked-out tool must match that blob
byte-for-byte and declare the single literal
`DEVEL_ANNOUNCEMENT_APPROVAL_POLICY = 1`, parsed with AST without importing or
executing the tool. Missing, duplicate, computed, unknown-version or nonliteral
markers refuse both devel announcement and reconciliation. This uses the
checkout's canonical Git objects, not another Buffer request. A historical
pre-fence tool is refused **even when valid-looking hashes are supplied**.

This is a supported-tool capability/provenance check, not a defense against a
malicious repository owner falsifying the policy declaration. **Old workflow
versions are not retroactively secured**: use the reviewed fenced workflow ref
and freeze that workflow together with the new release/tool. Do not select an
old workflow ref to bypass a refusal. Historical devel tags without this policy
marker cannot be announced or reconciled through the new workflow; do not
rewrite those tags or delete their claims to work around that boundary.

### Approval and manual announcement

1. Finish software publication and the separate package-asset/public-download
   audit. Do not retry PyPI upload just to obtain a social preview.
2. Download `linkedin-preview`, or manually dispatch **preview** with the
   published release tag. Show the **entire text and actual PNG** to the owner.
   Also verify the intended personal profile separately using **channels** and
   the configured `BUFFER_LINKEDIN_CHANNEL_ID`; preview never contacts Buffer.
3. After explicit approval, dispatch **announce** with the same tag and both
   digests from the reviewed `linkedin-approval.json`:

   ```sh
   gh workflow run linkedin.yml --repo groopcd/oc-box --ref devel \
     -f action=announce -f release_tag="$TAG" \
     -f expected_text_sha256="$REVIEWED_TEXT_SHA256" \
     -f expected_image_sha256="$REVIEWED_IMAGE_SHA256"
   ```

   Use the release owner's authorized GitHub authentication. These are public
   lowercase 64-hex SHA-256 digests, not tokens. Never put the Buffer key in argv.

Every devel **announce dispatch** requires both hashes; the CLI independently
requires them before a new devel post. Stable announcements may supply both for
the same protection but retain backward-compatible no-hash automation.
The CLI equivalents are `--expected-text-sha256` and
`--expected-image-sha256`. Regenerated content must match **before any card,
claim, receipt or Buffer post is created**. Mutable release-note changes, card
highlight changes, and renderer/font differences that change image bytes refuse
the announcement. Generate a new preview and obtain new approval; do not simply
copy new hashes into the dispatch without showing the changed preview.

The digests are a content-consistency fence, not proof of human consent: the
release operator is responsible for obtaining approval and confirming the target.
Digest fields are for **announce** only, not discovery, preview or reconciliation.

Before creating a post, the bridge:

- Reuses the existing clean/full-history tag, channel and lineage validator.
- Fetches the canonical published release and checks its metadata.
- Downloads the public PyPI wheel/sdist anonymously, verifies sizes/digests and
  their embedded exact source identity without importing downloaded code.
- Validates the selected connected LinkedIn profile.
- Uploads the generated card and checks its public bytes before asking Buffer
  to fetch it.

This hook is **after PyPI upload**, not a claim that the separate release-owner
checklist's package-asset attachment and final public-download audit have been
completed. It does not relax software acceptance or publication approval.
An announcement failure is a separate job failure; it cannot roll back a
published package. Retry the announcement workflow, **not the PyPI upload**.

The requested Buffer mode is automatic `shareNow`, not a manual notification or
queue slot. A successful response means **submitted to Buffer**, not necessarily
already visible on LinkedIn. The output records the observed Buffer status.
Check delivery/errors in Buffer; disconnected profiles or later network failures
can prevent delivery after submission.

## Durable duplicate protection and recovery

Each release/profile/channel has immutable `linkedin-…-claim.json` and
`linkedin-…-receipt.json` release assets. Their binding includes the repository,
release ID, tag, commit, channel and SHA-256 of the Buffer profile ID. Records
contain text/image hashes and, after success, the Buffer post ID/status—not the
API key or full account ID. They are public metadata and deliberately survive
Actions artifact/cache expiry. **Do not remove them during release housekeeping.**

The claim is uploaded **before** the only `createPost` request. Duplicate asset
names fail atomically. A receipt makes later attempts skip; an existing claim
without a receipt stops with `pending-claim-reconcile-in-buffer`. Network errors,
GraphQL errors, uncertain acknowledgements and lost receipts never trigger an
automatic second post.

If Buffer contains the matching post but the receipt was lost:

1. Inspect Buffer yourself and find its post ID; do not confuse it with the
   LinkedIn URL or release ID.
2. Dispatch **reconcile** with the same release tag and that Buffer post ID.
3. The bridge only **reads** the existing Buffer post, verifies its ID, profile,
   exact text hash and scheduled/sending/sent status, then writes the missing
   GitHub receipt. It never creates or republishes a Buffer post.

If no matching post is found, the claim remains blocked. Do not delete the claim
or blindly rerun publication; investigate the uncertain outcome explicitly.
This intentionally favors avoiding duplicates over automatic retries. Changing
release notes after a submission does not edit the existing post, and changing
the selected profile creates a different destination binding.

An announcement that failed before any claim may be retried with **announce**
and the same published release tag and approved digests. The enable variable,
public PyPI identity verification and profile checks still apply; a manual action
cannot bypass them. Existing receipts still skip without creating anything.
**Reconcile** needs no new approval hashes: it validates the existing claim/post,
never creates a new Buffer post, and can only write the missing GitHub receipt.

## Credentials and checks

Only the announcement job has `contents: write`, to add the public card and
claim/receipt assets. It has no PyPI OIDC permission. Discovery/preview jobs have
`contents: read`; preview receives no Buffer key. Authenticated API requests
refuse redirects; anonymous downloads have bounded allowlisted HTTPS origins
and no ambient proxy credentials. Raw API errors, signed URLs and credentials
are never logged. Mutations are not retried.

Offline checks:

```sh
uv run --no-project --with 'Pillow>=11,<13' python -B -m unittest discover -s tests -p 'test_release_*.py' -v
```

These cover rendering, both channel summaries, full-copy/UTF-16 bounds, exact
review hashes, workflow routing and pre-checkout approval, historical-tool
refusals and canonical selected-source verification, public package verification,
fixed-origin requests, disabled posting, expected refusals, durable claims,
reruns and uncertain-outcome reconciliation using synthetic mocked services.
They do not certify a live Buffer connection or LinkedIn delivery. No live
posting test is required or performed just to install this integration.

API references: [Buffer image posts](https://developers.buffer.com/examples/create-image-post.md),
[scheduling](https://developers.buffer.com/guides/posts-and-scheduling.md),
[key setup](https://support.buffer.com/en-us/articles/how-to-create-your-buffer-api-key-ShIgYVwM6j).
