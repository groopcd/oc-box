# Studio text Send foundation (source only)

There is no browser route, button, composer, named-HTTPS integration or
automatic send in this slice. `web_studio_send` is a host-internal operation
for one explicitly authorized user action on the existing private workspace
owner. Native OpenCode Home and the normal native controls are unchanged.

## Audited native operation

The official [V2 API reference](https://opencode.ai/docs/api) and the
unmodified **2.4.1** server's `/openapi.json` describe `session.prompt`:
`POST /api/session/{sessionID}/prompt` durably admits one input and requests
agent execution unless `resume` is false. The production request has exactly
three fields: nonblank `text`, `delivery: "queue"`, and `resume: true`. The
native server assigns the message ID. The adapter verifies the bounded
`200` `Session.Inbox.User` acknowledgment, exact session and text, `user` type,
`queue` delivery, native message ID and timestamp. It returns no native body,
echoed text, message ID, credentials or upstream errors.

- **Refused:** this operation did not attempt the native POST.
- **Queued:** a matching acknowledgment was verified while the authorized
  selection and owner were still current, with confirmed transport cleanup.
  Execution was requested, not observed; this does not prove a running or
  completed turn, a reply, successful model resolution or permission approval.
- **Unconfirmed:** the write was attempted but queue admission could not be
  confirmed. The input may be queued or executing. Do not retry automatically.
  Inspect the exact conversation in native OpenCode before manually trying again.

The adapter checks the private server's exact version and loopback origin,
native default project, registered in-container directory, project ID,
session ID and subpath. It refuses child, fork, archived, observed staged
revert and recorded location-switch cases, even if a moved session came
back. Before the final scope check, the selected session must not appear
active and its native inbox must be empty. These observations are not proof
that there are no jobs, shell activity or pending forms. Native 2.4.1 offers
no conditional prompt POST; a concurrent native move, staged revert or
execution change between checks and admission remains possible. An observed
staged revert is refused rather than implicitly committed. This is a gate
for any future serving review, not a race-free mutation claim.

The body is capped at 8 KiB and text at 4 KiB UTF-8. Existing native rules,
agents, models, mandatory permissions and forms remain authoritative; this
operation does not set or bypass them. Explicit agent/model selection,
steering, attachments, commands, forms, permission replies, interruption,
execution tracking and streaming are out of scope. Use native OpenCode to
inspect a turn or respond to a prompt. There is no transcript refresh, host
database inspection, config reload, terminal action or vault access.

## Trusted integration contract

1. Authenticate and authorize the browser, workspace and explicit Send
   gesture before constructing `SendScope`. Bind one opaque browser session
   handle, the verified owner generation, immutable container ID and private
   endpoint/project, plus the exact selected session and selection generation.
   Never construct these from request fields, a catalog, an ID guessed from
   the owner, or a stale browser report. Advance the selection generation on
   observed away/back and loss of the current selection.
2. Construct `StudioSend` with the original, still-held same-user control
   stream, the absolute monotonic request/admission deadline, and a bounded
   `observe(deadline)` callback that rechecks **that same** live owner and
   currently authorized selection. Construction is not an owner lookup or
   proof of browser authority. The module checks the original control
   descriptor and does not reconnect or extend a lease.
3. Register `cancel()` with the actual browser admission before `execute()`;
   use the first deadline to expire and cancel on logout, revocation or loss
   of selection/owner. Hold admission through cleanup and response delivery,
   rechecking again before sending any browser response. A permit is one-use
   even after refusal or uncertainty. Do not construct replacement permits as
   retries or treat a newer login as authority for the old action.

Only the existing providerless managed relay is started, pinned to the full
container ID. Its private stream carries the prompt; no prompt goes into
arguments, environment, temporary files or oc-box diagnostics. This is not
redaction or a promise about what native OpenCode may retain in sessions or
send to its configured provider. The module never forwards a browser cookie
or vault secret. It uses only the existing private owner control credential.

The operation checks authorization at startup, immediately before sending,
and again after acknowledgment and cleanup. Cancellation interrupts the
registered stream; it cannot retract bytes already written or undo native
admission. Active I/O is limited to six seconds or the earlier caller
deadline; the managed relay has a separate bounded cleanup grace.
If `cleanup_confirmed` is false, separately report/fence cleanup
uncertainty: keep the admission charge and do not claim clean drain or
release a verified owner lease early. Any host-only relay termination is
not proof that native work stopped.

The existing named pairing/owner profile is pinned separately. A verified
2.3.2 named owner is **not** authority to run this 2.4.1 operation; do not
expand or bypass that profile based on this source-only Send fixture.

## Verification

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_web_studio_send.py -v
python3 -B tests/smoke_web_studio_send_v2.py --opencode /path/to/opencode-2.4.1
```

The native smoke uses a fresh synthetic 2.4.1 private service and the actual
managed relay framing, substituting only the outer Podman exec and a test-only
`resume: false` body. It verifies the native schema and inbox acknowledgment
with no foreground work or inference. Separate socket tests verify the exact
production `resume: true` body, cancellation and closed outcomes; they do not
exercise execution, policy enforcement during real model calls, browser
admission, HTTPS or live Podman.
