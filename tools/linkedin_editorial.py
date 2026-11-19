import argparse
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


BUFFER = "https://api.buffer.com"
OUT = Path("linkedin-editorial-output")
DIGEST = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*")
PDF_LIMIT = 100_000_000
COVER_LIMIT = 10_000_000
POST_FIELDS = "id channelId status externalLink"


class Refusal(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, message):
    if not condition:
        raise Refusal(message)


def encoded(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def digest(data):
    return sha256(data).hexdigest()


def request(url, *, data=None, token=None, limit=1_000_000, github=False):
    headers = {"User-Agent": "linkedin-editorial-actions"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/json"
    if github:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = Request(
        url, headers=headers, data=encoded(data) if data is not None else None
    )
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as reply:
            length = reply.headers.get("Content-Length")
            require(
                not length or (length.isdecimal() and int(length) <= limit),
                "response exceeds its size limit",
            )
            result = reply.read(limit + 1)
            require(len(result) <= limit, "response exceeds its size limit")
            return result
    except HTTPError as err:
        raise Refusal(f"HTTP {err.code}; redirects are not followed") from None
    except (URLError, TimeoutError, OSError):
        raise Refusal("network request did not complete") from None


def public_url(value, allowed):
    try:
        parts = urlsplit(value)
    except ValueError:
        raise Refusal("invalid public URL") from None
    require(
        value.isascii()
        and all(32 < ord(c) < 127 and c != "\\" for c in value)
        and parts.scheme == "https"
        and parts.netloc in allowed
        and not parts.query
        and not parts.fragment,
        "use a direct HTTPS URL on an approved host, without credentials, port, query, or fragment",
    )
    return parts


def media(url, allowed, kind):
    parts = public_url(url, allowed)
    data = request(url, limit=PDF_LIMIT if kind == "pdf" else COVER_LIMIT)
    suffix = Path(parts.path).suffix.lower()
    if kind == "pdf":
        require(
            suffix == ".pdf" and data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:],
            "expected a direct PDF file",
        )
    else:
        require(
            (suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff")),
            "expected a direct PNG or JPEG thumbnail",
        )
    sha = digest(data)
    raw_path = parts.path.strip("/").split("/")
    pinned_raw = (
        parts.netloc == "raw.githubusercontent.com"
        and len(raw_path) >= 4
        and COMMIT.fullmatch(raw_path[2])
    )
    require(
        pinned_raw or Path(parts.path).stem.endswith("-" + sha),
        "host media at a Git commit URL or end its filename stem with its full SHA-256",
    )
    return {"url": url, "sha256": sha, "bytes": len(data)}, data, suffix


def checked_text(value, name, limit, multiline=False):
    value = value.replace("\r\n", "\n") if multiline else value
    require(
        bool(value.strip())
        and all(
            (multiline and ch == "\n") or not unicodedata.category(ch).startswith("C")
            for ch in value
        ),
        f"{name} must be nonempty and not contain control characters",
    )
    require(
        len(value.encode("utf-16-le")) // 2 <= limit,
        f"{name} exceeds {limit} UTF-16 units",
    )
    return value


def channel_id(env):
    value = env.get("EDITORIAL_CHANNEL_ID", "")
    require(IDENTIFIER.fullmatch(value), "configure BUFFER_LINKEDIN_CHANNEL_ID")
    return value


def prepare(env):
    repo = env.get("GITHUB_REPOSITORY", "")
    require(REPO.fullmatch(repo), "invalid GitHub repository")
    allowed = frozenset(env.get("EDITORIAL_ALLOWED_HOSTS", "").split())
    require(
        bool(allowed) and all(HOST.fullmatch(host) for host in allowed),
        "configure LINKEDIN_EDITORIAL_HOSTS as a space-separated list of lowercase hosts",
    )
    kind = env.get("EDITORIAL_KIND")
    require(kind in {"article", "pdf"}, "pick article or pdf")
    title = checked_text(env.get("EDITORIAL_TITLE", ""), "title", 100)
    caption = checked_text(
        env.get("EDITORIAL_CAPTION", ""), "caption", 3000, multiline=True
    )
    article_url = env.get("EDITORIAL_ARTICLE_URL", "")
    pdf, cover = (
        env.get(name, "") for name in ("EDITORIAL_PDF_URL", "EDITORIAL_THUMBNAIL_URL")
    )
    if article_url:
        public_url(article_url, allowed)
    channel = channel_id(env)
    payload = {
        "channelId": channel,
        "text": caption,
        "mode": "shareNow",
        "schedulingType": "automatic",
    }
    manifest = {
        "schema": 1,
        "repository": repo,
        "kind": kind,
        "channel_id": channel,
        "title": title,
        "article_url": article_url,
        "caption_sha256": digest(caption.encode()),
    }
    files = {"linkedin-caption.txt": caption.encode()}
    if kind == "article":
        require(
            article_url and not pdf and not cover,
            "article mode needs only an article URL",
        )
        payload["metadata"] = {
            "linkedin": {"linkAttachment": {"url": article_url, "title": title}}
        }
    else:
        require(bool(pdf and cover), "PDF mode needs both a PDF and a thumbnail URL")
        require(
            not article_url or article_url in caption,
            "include the article URL in the PDF caption",
        )
        manifest["pdf"], files["linkedin-document.pdf"], _ = media(pdf, allowed, "pdf")
        manifest["thumbnail"], thumbnail, extension = media(cover, allowed, "thumbnail")
        files["linkedin-thumbnail" + extension] = thumbnail
        payload["assets"] = [
            {"document": {"url": pdf, "thumbnailUrl": cover, "title": title}}
        ]
    manifest["request_sha256"] = digest(encoded(payload))
    return manifest, payload, files


def buffer_query(token, query, variables):
    require(bool(token), "BUFFER_API_KEY is missing")
    response = json.loads(
        request(BUFFER, token=token, data={"query": query, "variables": variables})
    )
    require(
        isinstance(response, dict)
        and not response.get("errors")
        and isinstance(response.get("data"), dict),
        "Buffer rejected the API request",
    )
    return response["data"]


def verify_channel(token, channel):
    value = buffer_query(
        token,
        """
        query($input: ChannelInput!) {
          channel(input: $input) { id service type isDisconnected isLocked isQueuePaused }
        }
    """,
        {"input": {"id": channel}},
    ).get("channel")
    require(
        isinstance(value, dict)
        and value.get("id") == channel
        and value.get("service") == "linkedin"
        and value.get("type") == "profile"
        and all(
            value.get(key) is False
            for key in ("isDisconnected", "isLocked", "isQueuePaused")
        ),
        "configured LinkedIn profile is unavailable",
    )


def claim(env, repo, sha):
    ref = "refs/tags/linkedin-editorial/" + sha
    commit, token = env.get("GITHUB_SHA", ""), env.get("EDITORIAL_GITHUB_TOKEN", "")
    require(
        COMMIT.fullmatch(commit) and token, "missing trusted GitHub workflow context"
    )
    try:
        request(
            f"https://api.github.com/repos/{repo}/git/refs",
            data={"ref": ref, "sha": commit},
            token=token,
            github=True,
        )
    except Refusal as err:
        raise Refusal(
            f"could not reserve {ref}: {err}. Check the existing claim; no Buffer create was sent"
        ) from None
    print(
        f"Reserved {ref}. If the result is uncertain, check Buffer before taking further action.",
        flush=True,
    )


def post_result(value, channel):
    require(
        isinstance(value, dict)
        and value.get("channelId") == channel
        and isinstance(value.get("id"), str)
        and IDENTIFIER.fullmatch(value["id"])
        and isinstance(value.get("status"), str)
        and IDENTIFIER.fullmatch(value["status"]),
        "Buffer did not return a verifiable post record",
    )
    result = {"post_id": value["id"], "status": value["status"]}
    url = value.get("externalLink")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        if (
            parts
            and parts.scheme == "https"
            and parts.netloc in {"www.linkedin.com", "linkedin.com"}
        ):
            result["linkedin_url"] = url
    return result


def run(action, env, out):
    token = env.get("BUFFER_API_KEY", "")
    if action == "status":
        post_id = env.get("EDITORIAL_POST_ID", "")
        require(IDENTIFIER.fullmatch(post_id), "supply an existing Buffer post ID")
        value = buffer_query(
            token,
            "query($input: PostInput!) { post(input: $input) { " + POST_FIELDS + " } }",
            {"input": {"id": post_id}},
        ).get("post")
        result = post_result(value, channel_id(env))
        require(result["post_id"] == post_id, "Buffer returned an unexpected post ID")
        print(encoded(result).decode(), end="")
        return

    approval = env.get("EDITORIAL_APPROVAL", "")
    if action == "publish":
        require(
            env.get("EDITORIAL_ENABLED") == "true", "editorial publishing is disabled"
        )
        require(
            DIGEST.fullmatch(approval), "supply the reviewed preview approval SHA-256"
        )
    manifest, payload, files = prepare(env)
    manifest_bytes = encoded(manifest)
    prepared = digest(manifest_bytes)
    if action == "preview":
        out.mkdir(exist_ok=False)
        files["approval.json"] = manifest_bytes
        files["approval.sha256"] = (prepared + "\n").encode()
        for name, data in files.items():
            (out / name).write_bytes(data)
        print(
            f"Preview prepared for channel {manifest['channel_id']}; no Buffer request made."
        )
        return

    require(
        hmac.compare_digest(prepared, approval),
        "approved inputs or media differ; preview again",
    )
    channel = manifest["channel_id"]
    verify_channel(token, channel)
    out.mkdir(exist_ok=False)
    claim(env, manifest["repository"], approval)
    try:
        value = buffer_query(
            token,
            """
          mutation($input: CreatePostInput!) {
            createPost(input: $input) {
              ... on PostActionSuccess { post { """
            + POST_FIELDS
            + """ } }
              ... on MutationError { message }
            }
          }
        """,
            {"input": payload},
        ).get("createPost")
        require(isinstance(value, dict), "Buffer did not return a create result")
        receipt = post_result(value.get("post"), channel)
    except (Refusal, ValueError):
        raise Refusal(
            "submission not confirmed; the claim remains. Check Buffer, do not retry blindly"
        ) from None
    receipt.update(approval_sha256=approval, channel_id=channel)
    print(encoded(receipt).decode(), end="", flush=True)
    (out / "receipt.json").write_bytes(encoded(receipt))
    print("Buffer accepted the request. Check delivery before calling it published.")


def main():
    parser = argparse.ArgumentParser(
        description="Review and submit LinkedIn editorial posts via Buffer"
    )
    parser.add_argument("action", choices=("preview", "publish", "status"))
    action = parser.parse_args().action
    try:
        run(action, os.environ, OUT)
    except (Refusal, ValueError, OSError) as exc:
        print(
            exc
            if isinstance(exc, Refusal)
            else "invalid API response or filesystem error",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
