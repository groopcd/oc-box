# LinkedIn articles and PDF carousels via Buffer

This is a manual GitHub Actions workflow for a **personal LinkedIn profile**
already connected to Buffer. It is separate from the oc-box release workflow:
no release tag, PyPI package, release asset, automatic trigger, or change to
release settings is involved.

| Mode | What LinkedIn receives |
|---|---|
| `article` | The caption and one link card with the article URL and title. |
| `pdf` | The caption and one PDF document with a title and cover thumbnail. If you also provide an article URL, put it in the caption. Buffer does not allow a link card and a document in the same post. |

## Install and configure

Copy `.github/workflows/linkedin-editorial.yml` and
`tools/linkedin_editorial.py` into the publishing repository's **default
branch**. The workflow refuses to run on other branches or tags. The
optional `tests/test_linkedin_editorial.py` uses only the Python standard
library and mocks every network request.

In the publishing repository's GitHub Actions settings:

| Setting | Value |
|---|---|
| Secret `BUFFER_API_KEY` | Buffer key with account read, posts read, and posts write. Reuse an existing repository secret if it has those scopes. Keep it out of `gh` arguments and local files. |
| Variable `BUFFER_LINKEDIN_CHANNEL_ID` | Exact Buffer ID of the intended, connected personal LinkedIn profile. This can be the same ID as the oc-box release workflow. Confirm the profile in Buffer before approving a preview. |
| Variable `LINKEDIN_EDITORIAL_HOSTS` | Space-separated, lowercase hostnames allowed for article and media URLs. The installed default is `www.padoka.org`; override it to use another site or to add `raw.githubusercontent.com`. |
| Variable `LINKEDIN_EDITORIAL_ENABLED` | Leave unset for preview-only setup. Set to `true` when manual publishing should be allowed. It is independent of `LINKEDIN_ANNOUNCEMENTS`. |
| Environment `linkedin-editorial` | Configure required reviewers and deployment only from the default branch if available. Only the publish job uses this environment and has `contents: write`. |

GitHub must permit the publish job to create lightweight tags under
`refs/tags/linkedin-editorial/`. These are duplicate-prevention records, not
releases. The GitHub token for this operation is supplied by the runner,
not the operator. Restrict changes to the default branch and protect the
claim tags from deletion. Use only a repo and workflow that you trust with
the Buffer key.

## Prepare content and preview

Write the complete public caption to a UTF-8 file. The workflow preserves
whitespace and line breaks apart from converting CRLF to LF. Captions are
limited to 3,000 UTF-16 units. Do not put credentials or private content
in the caption, input URLs, or media: inputs and artifacts may be visible to
repository readers.

For PDF mode, Buffer needs **two publicly accessible, stable URLs**: the
document and a PNG or JPEG cover. The workflow does not upload those files.
Use a full Git commit in `raw.githubusercontent.com` URLs, or name hosted
files `name-FULL_SHA256.pdf` and `name-FULL_SHA256.png` (or `.jpg`). Upload
them without changing their bytes; some media hosts transform images. URLs
must be direct HTTPS, without redirects or query parameters. Keep both
assets reachable until delivery. LinkedIn caps PDFs at 300 pages; the
workflow caps download size at 100 MB for the PDF and 10 MB for the cover.
See [Buffer's media hosting guide](https://developers.buffer.com/guides/hosting-media.md).

Use your authorized repository owner account with `gh`. In an oc-box
container, use the owner-authenticated wrapper from the release runbook
instead of inherited service credentials.

```sh
REPO=owner/publishing-repo
REF=main  # replace with the actual default branch
TITLE='Measured results'
ARTICLE='https://www.example.org/article/'
PDF='https://raw.githubusercontent.com/owner/media/FULL_COMMIT_SHA/article.pdf'
COVER='https://raw.githubusercontent.com/owner/media/FULL_COMMIT_SHA/cover.png'

gh workflow run linkedin-editorial.yml --repo "$REPO" --ref "$REF" \
  -f action=preview -f kind=pdf -f title="$TITLE" \
  -F caption=@caption.txt -f article_url="$ARTICLE" \
  -f pdf_url="$PDF" -f thumbnail_url="$COVER"
```

For a link-only post, use `kind=article` and omit `pdf_url` and
`thumbnail_url`. `article_url` is then required. Preview mode never contacts
Buffer; for PDFs it downloads the public media so reviewers can open the
exact bytes.

Download the artifact from that **specific** run:

```sh
gh run download "$PREVIEW_RUN_ID" --repo "$REPO" \
  --name linkedin-editorial-preview --dir ./review
```

Read all of `linkedin-caption.txt` and `approval.json`. Open
`linkedin-document.pdf` and the thumbnail if present. Confirm the exact
channel ID and that you're posting to the intended profile. Approve only
those specific files. The digest in `approval.sha256` binds the repository,
channel, title, caption, URL, prepared Buffer payload, and media bytes.

## Publish and verify delivery

After explicit approval, repeat the same inputs with `action=publish` and
include `-f approval_sha256="$(cat ./review/approval.sha256)"`. Changed
inputs or media require a new preview and approval.

Before submitting one `shareNow` request, the workflow confirms the
configured profile is connected, unlocked, and unpaused. It then reserves
the digest as a durable GitHub tag. If that reservation fails or the tag
already exists, no Buffer create request is sent. If Buffer acceptance
cannot be confirmed after reservation, the tag remains: **check Buffer
before doing anything else. Do not remove the tag or change the digest just
to retry.**

Read `receipt.json` from the `linkedin-editorial-receipt` artifact or the job
log. A Buffer post ID means **submitted**, not delivered. Check in Buffer or
run the read-only status action for the same profile:

```sh
gh workflow run linkedin-editorial.yml --repo "$REPO" --ref "$REF" \
  -f action=status -f post_id="$BUFFER_POST_ID"
```

The status action reports the status and the LinkedIn URL when Buffer
provides one. Confirm visibility on LinkedIn itself.

## Offline checks

From the publishing repo root:

```sh
python3 -m unittest discover -s tests -p test_linkedin_editorial.py
```

The provided XDNA1 carousel can use its first card as the thumbnail and its
prepared LinkedIn copy as the caption. Both the PDF and cover need hosted
URLs before a GitHub preview can run.
