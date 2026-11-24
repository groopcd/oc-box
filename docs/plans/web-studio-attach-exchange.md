# Private Studio control attach exchange (unwired)

`web_studio_attach_exchange.ClientExchange` and `OwnerExchange` test a
bounded request/offer/ack on an **already counted** private AF_UNIX stream.
They do not resolve a path, connect, duplicate, close, or change the mode of
that stream. No launcher, controller, or browser calls them. There is no
native connector, probe, password, URL, pairing link or code, or authority to
open a listener, activate a route, or launch a replacement.

The producer must pass the exact connected socket and counted use on its own
end, the original owner and managed listener identities it independently
verified, and the witness it took with `web_named_stop_fence.observe_attach`
for that same armed lifetime. It must preserve the exact stream and count
and construct at most one exchange per use. Its `observe(use, *, deadline)`
callback looks up the original counted use and rechecks the original live
owner, listener and association with the same arm. Return the original
`(stream, owner, listener, arm_generation)` or `None` on any uncertainty.
The callback must be read-only and bounded by the supplied monotonic deadline;
the exchange cannot preempt arbitrary Python callbacks. No production
producer exists. A preflight witness, an affirmative callback alone, matching
fields, a same-user/PID peer, or a copied socket identity is not that proof.

The trusted borrower exclusively uses this stream on the constructing thread
and keeps its lifetime outside the exchange. Its Python timeout must already
be `None` or `0.0`, so socket I/O uses only the exchange's bounded wait, with
no per-call Python wait layered over it. The fixed-size, versioned binary
frames contain only the arm generation and fresh challenges: the offer
echoes the request challenge and adds its own, and the ack echoes both. A
recorded offer/ack cannot complete a fresh exchange. The exchange checks the
same descriptor, count, owner, listener and durable witness before and after
the wire exchanges. Observed extra input, EOF, stale intent, scope replacement,
loss of count, timeout or unknown observations refuse; no attempted exchange
can be replayed. A settled handle can re-observe only the same held stream and
witness. An observed loss is sticky. These are bounded observations, not an
atomic barrier against external changes or unobserved short excursions.

The owner verifies receipt of its ack. The client can observe only that it
sent the ack, not that the owner read or accepted it; neither result grants
access. Refusal is a fixed local error, not another wire frame, a lease
release, or permission to fall back. The producer owns any later stream
retirement. Tests use synthetic registrations, distinct Unix peers and
explicit fixture counts. They do not establish production owner attestation,
crash/drain behavior or host acceptance.
