# Studio integration gates

The approved Studio layout and the chat behavior are not the outstanding
design questions. The remaining gates concern who can authorize a browser
request, what must have stopped before the launcher releases a workspace,
and what data the host is allowed to remove.

## One-use Studio: bind the existing gateway, do not add a new login

The native pairing cookie authenticates a browser to OpenCode. It does not
identify which host gateway reserved the original socket or which launcher
holds the workspace. The owner, listener and native cookie proofs now have
separate sources of evidence. A first-party adapter must join them for the
same lease, without changing how the native link or native UI works.

The owning launcher can pass its original owner directly. An attach-only
client cannot reconstruct that proof from a same-user connection, a copied
URL, or the native PID alone. Support attach-only Studio only after a
reviewed generation-bound exchange on the original counted stream. Never
redial, start another owner, or unlock credentials on an attach failure.

Revocation and drain are separate. To turn on routes, account for all
accepted sockets and relay processes from before dispatch through verified
retirement, including persistent native WebSockets. A socket being closed,
or a client count reaching zero, does not establish that in-container work
has stopped. For ordinary traffic, a finite metadata reader cannot
stand in for a duplex native WebSocket relay. The missing evidence is an
acknowledged native-side retirement on cancellation, followed by strict
EOF and exact process reap, even when an upgraded stream is idle or
backpressured. Privately tracking host workers alone is not that evidence.
The unserved framed duplex worker and host verifier now prove this on
local synthetic pipes and TCP. The next gate must show the same contract
through rootless `podman exec -i`: the original exec must carry cancellation
and stdout EOF correctly even under backpressure. Then the gateway must
register that exact process and every other native helper before dispatch.
The unserved exec binding now does that registration for one tunnel,
rechecks the original owner and stop admission at spawn, and uses the
fixed providerless worker with the owner's pinned engine environment.
It cannot retire an attempted exec without its exact transport evidence,
and does not authorize or account for a native application job.

A Studio grant must not outlive native cookie authority. Recheck the
current browser cookie for every bounded protected exchange and fence an
observed loss. A missing cookie retires the page; that does not prove
server-side invalidation of an earlier copied cookie, or cancellation of a
write that the native server already admitted. Require measured native
behavior for any stronger logout claim. Never turn a browser cookie into
a host capability or add a replacement plaintext sign-in.

The guest selection registry now binds page generations to the original
guest grant and a registered use with fresh native authentication. A new
selection invalidates that page's old view before verification, and a
late response cannot revive it. The caller must still verify membership
on the held native workspace and retire every admitted helper.
The unserved guest Send seam consumes that request's native-cookie
authorization, checks the exact selected generation, and uses the
existing workspace/session preflight and native queue-acknowledgment
schema through the registered OGD tunnel. It never uses host Basic auth
or retries a possible POST. Its current tests cover exact private wire
bytes and refusal/acknowledgment handling without enabling any route.
The read seam uses the existing bounded project/catalog and transcript
projections, checks the exact native PID and workspace, and repeats the
live selection check after retiring its transport. It is not a general
native JSON proxy.

## Named HTTPS: the stop fence must cover every supported writer

A stop requested from another terminal must be an intent for this exact
owner. The owner must revoke its browser admission and confirm worker
drain before history synchronization and stopping the same container.
Neither a lost control socket nor PID reuse proves completion. The
separately tested fence is durable across launcher loss, but the ordinary
older launchers can still recognize the same workspace and ignore it.
A fresh-registration-only model needs an enforced compatibility boundary,
not a rule that treats a missing fence as an ordinary workspace. Backup,
reset, sync and purge must honor the same lifetime rules.

Enrollment also needs a trusted browser entry and a separate local
approval before atomically comparing and replacing the exact encrypted
account store. The tested final reply gate only makes a proof eligible for
review. It does not grant this approval or write the store. Select and
independently review the exact authentication artifacts, dependencies,
licenses and applicable IPR before deployment or distribution. Native
workspace/provider vault credentials stay on the host; named browser
credentials give no host-management authority.

## Full uninstall: prove that deletion cannot target a replacement

The mounted-rootfs and same-device alias controls show that the diagnostic
can recognize known examples in a pinned disposable store. They do not
prove that every consumer and mount is visible in an ordinary per-user
Podman store. Nor do launcher locks stop another Podman process from
replacing a volume after a check. A name-based `volume rm` is not an
identity-conditional operation.

Admit only a store profile that can enforce exclusion through the final
native mutation and independent postcheck. Require exact reviewed scope,
durable records for uncertain results, and a separate typed confirmation.
Refuse the whole operation on ambiguity, rather than doing a narrower
deletion. Never access or remove host OpenCode history, project files or
markers, or external backups. Launcher-only uninstall is a separate path
with its own acceptance.

## Browser manager and runtime safeguard

Provide live Status and Sessions only for the authorized workspace.
Page-local Settings are not saved host policy. Host-wide Terminals, Logs,
Storage and Backup need separately authorized operations; guidance is not
an implementation of them.

The guarded-owner same-version lifecycle and a real forward upgrade are
separate acceptance cases. The forward case must use the original
container/volume, verify actual authenticated readiness and retained
history, and confirm owner and container shutdown. It must not introduce
automatic downgrade or cold fallback. Final branch and non-publishing CI
checks must use the exact combined candidates and do not authorize release
publication.
