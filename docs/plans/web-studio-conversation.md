# Studio conversation reader (source only)

The Studio reader is a bounded, read-only projection of one explicitly selected
conversation. It is not served yet. It does not change the native iframe,
OpenCode Home, existing tab/history routes, shell authority, or either
browser sign-in flow. The native UI and terminal remain authoritative for
composer input, permissions, forms, files, shell and tool details, and live
updates. Studio does not send, start, interrupt, approve, or stream anything.

## Native contract and scope

The reader is pinned to native **2.4.1**. It uses the official [V2 API
reference](https://opencode.ai/docs/api) and the wire shapes checked against the
unmodified 2.4.1 server's `/openapi.json`. The experimental API may change in
subsequent patches; falling back to another version or transcript source is not
supported.

`web_studio_conversation.conversation(box, data, connection, ident, *,
deadline=None)` is a trusted host-side callable. The caller must independently
authorize the browser and exact selected session and keep a verified current
workspace owner/client lease through response delivery. Never supply
browser-selected connection, workspace, project, native URL or cursor values.
The worker uses the existing providerless client command, not the host service,
history archive, private DB, native DOM, or native JavaScript. It reads only
fixed GET routes on that authenticated container-loopback origin. The owner
credential is used only for the existing private transport; it is not returned
or used to refresh credentials.

- Verify `/api/info` native version, loopback origin and PID, then
  `/api/location` default project and exact registered container directory.
  Bind the exact session GET to that project, directory, subpath and ID before
  reading messages. A same-project sibling, unrelated project, missing ID,
  staged revert or fork is unavailable. Child sessions can be read by their
  own exact ID; no parent lookup or borrowed history.
- Refuse any recorded location switch, even when the session has moved back.
  Check this separately from the visible message window with the native type
  filter. Recheck it and the same owner/location/session after the read.
- Obtain delivered messages newest first from the session's message list.
  Native list rows don't echo the session ID; independently match each visible
  row to the same session's single-message GET. Unknown message/part forms,
  observed mutations or inconsistent results refuse the entire projection.
  These checks do not make separate native API calls transactional or establish
  the origin of text that someone already imported natively into this project.

## Projection and integration limits

The result contains only `sessionID`, `title`, `messages` and `hasEarlier`.
Rows are ordered oldest first within the newest window. Each row has only
`id`, `role` (`user` or `assistant`), `text` and `truncated`. Known non-chat
records, attachments, model reasoning, tool payloads and native metadata are
omitted. Untrusted transcript text can contain HTML, commands or secrets the
user or model wrote; render it only as literal text, never as HTML, scripts or
active links. This is not arbitrary-secret redaction.

The worker reads at most three pages of 24 native messages. `hasEarlier` means
the native service offered a continuation at the cutoff; it can conservatively
be true when one more read would have returned an empty page. The caller never
receives a native cursor. Titles are clipped at 256 UTF-8 bytes and text at
4 KiB per row without breaking code points. Native replies are capped at
512 KiB each and 2 MiB aggregate; output is capped at 512 KiB. The worker has a
four-second wall clock budget and the host bridge has six seconds, or less if
the caller supplies an earlier monotonic deadline. A limit or refused read
produces a fixed unavailable error, not partial unverified rows or raw native
errors. The intentional bounded recent window and per-row text clipping are
explicitly marked. No progressive delivery, background polling, or reload is
performed by the reader.

Future HTTP integration must still independently enforce the correct browser
origin, authentication, rate/concurrency bounds, current owner lease,
revocation and cancellation before delivery. Nothing in this adapter grants
access based on a browser-reported ID or replaces those checks.

## Verification

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_web_studio_conversation.py -v
python3 -B tests/smoke_web_studio_conversation_v2.py --opencode /path/to/opencode-2.4.1
```

The smoke uses a fresh synthetic private native server with no provider
credentials and replaces only the Podman exec boundary with a local process.
It checks real pagination, cross-session message refusal, directory and
move-away-and-back fences, and unchanged session/messages/inbox/activity.
It is not a browser, deployment, or live rootless-Podman acceptance check.
