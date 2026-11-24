# Native-side guest duplex worker (unserved)

`web_guest_duplex_worker.py` is a standalone proof between two private,
distinct pipes and at most one TCP socket to literal `127.0.0.1`. Its sole
argument is a trusted, canonical decimal port. It does not authenticate that
endpoint or choose a workspace: a future caller must supply the port from the
original owning proof and run this fixed worker in the selected namespace.
There is no caller, Gateway path, launcher option, or change to the named or
legacy proxies. The worker does not accept or look up credentials, add a
listener, parse HTTP, or interpret WebSocket bytes.

## Private wire

Each record is `OGD\x03`, one opcode byte, a big-endian unsigned 16-bit
payload length, and exactly that payload. Tags are opaque 16-byte per-attempt
labels, not credentials. A separate fresh 16-byte challenge first appears in
`CANCEL` and is echoed only in the terminal receipt. Older versions are refused.
Serial numbers are unsigned 64-bit big-endian integers starting at one
independently in each direction, with no wrap.

| Opcode | Direction | Payload and meaning |
| --- | --- | --- |
| 1 `OPEN` | to worker | Tag; start the only nonblocking connect. |
| 2 `READY` | from worker | Same tag; connect succeeded. No stream data before this record. |
| 3 `DATA` | both | Serial, fresh 16-byte credit challenge, then 1–16,384 opaque bytes. |
| 4 `ACK` | both | Exact serial and credit challenge; returns one `DATA` credit. |
| 5 `END` | from worker | Empty; native read EOF and local socket close, not a retirement receipt. |
| 6 `CANCEL` | to worker | Original tag (the first tag if no `OPEN` occurred), then the fresh challenge. |
| 7 `CLOSED` | from worker | Tag, challenge, then one byte: `0` never opened, `1` worker's exact socket closed. |

After `READY`, each direction has one `DATA` credit. The worker returns
incoming credit only after the whole payload was accepted by the local TCP
socket and its ACK was emitted; that does not establish server consumption.
The host must return output credit as it consumes bytes into bounded capacity.
An ACK for an un-emitted, unknown, or old data frame is a protocol failure.
An ACK already queued before transmission cannot know that frame's fresh
credit challenge. This is frame sequencing, not proof of application
consumption or protection against a replaced worker. A
second `OPEN` never reconnects. After native read EOF, a single already
in-flight host `DATA` can cross `END`; it is neither written nor credited.
`END` requires the caller to finish the exchange with `CANCEL`; there is no
TCP half-close control. A connect or socket I/O error is a conservative
failure, not an `END` or a cleanup acknowledgment.

The worker polls control input even when either data sink is blocked. It
keeps one bounded input record, one pending TCP write, and at most two output
records (ordinary data plus control, or a partial record plus the receipt).
On explicit `CANCEL`, it closes the socket immediately, discards unsent
payload, and keeps only the tail of an already-emitted stdout record to
preserve framing. It requires actual EOF on the original stdin **after**
`CANCEL`; extra input, mismatched tags, bare EOF, and protocol or I/O failures
do not earn a receipt. The receipt is terminal. The host must keep draining
the original stdout after cancellation, including any old payload in front
of the receipt, and must not deliver that payload to the browser.

Startup through `READY` has a six-second absolute budget. A partial input
record, queued output or TCP write, unreturned credit, or wait for `CANCEL`
after `END` is bounded to 15 seconds, not refreshed by partial progress.
After `CANCEL` there are five seconds for the remaining pipe handshake.
A connected stream without pending work has no idle deadline. There is no
restart, fallback, payload diagnostic, or receipt on emergency pipe cleanup.

## Evidence boundary

`CLOSED` confirms only retirement of the exact worker's local TCP descriptor
(or that the worker never opened it); already-sent bytes, peer processing,
and actions already admitted by OpenCode can survive that closure. It does
not prove rollback, safe replay, native job completion, or history readiness.
`web_guest_duplex_host.DuplexHost` implements the matching unserved consumer.
It takes exclusive ownership of two distinct original unbuffered exec pipes
and the original unreaped process, with a pidfd and bounded poll slices. It
keeps one send and one receive data credit, permits cancellation before
`OPEN`, and finishes only a record that was partly written before
cancellation. After sending `CANCEL` and closing the original input, it
drains and discards any remaining old data. It then requires the exact tag,
challenge and expected closure disposition, genuine stdout EOF with no
surplus, and a terminal `waitid(P_PIDFD, …)` status of zero before its
deadline. A premature receipt header cannot become authorized by a later
write, and a reply queued before cancellation cannot know the fresh
challenge. Unknown or forced local cleanup never supplies confirmation or
resends cancellation. If local cleanup returns `False`, retain the original
handle and never release the workspace on that basis. The
binding and pre-spawn registration are exclusively the caller's responsibility;
the host consumer does not run a command, accept a browser, or choose an
owner. Callers must not duplicate or retain the original pipe handles, poll
or reap the child independently, or treat the kernel descriptor checks as
protection against a concurrent same-process substitution.

`tests/test_web_guest_duplex_worker.py` runs this exact worker on direct
local subprocess pipes and synthetic loopback sockets. It does not exercise
Podman's stdin EOF propagation, the native OpenCode server, browser behavior,
Gateway or owner lifetime coordination, or an installed host. The matching
host tests cover idle and blocked loopback traffic, wrong/absent/duplicated
receipt evidence, a kept-open pipe, nonzero or lost reap status, and an
observably substituted descriptor. Actual Podman stdio, owner coordination
and live browser behavior remain separate validation gates.

## Unwired browser byte pump

`web_guest_duplex_host.pump_worker` takes the original `AcceptedWorkers`
callback handle, a fresh same-thread `DuplexHost` for an already-admitted
helper, and the preclassified request prefix (including any coalesced bytes).
It does not start a helper, serve routes, own the browser socket, or complete
any registered activity. The existing `NativeResponseHeaders` handles
interims, pairing `Location` rewrite and cookie passthrough, and final
non-upgrade `Connection: close`; bytes after the final head, including a
`101` stream in both directions, remain opaque. This is not HTTP message
framing, pipelining, a half-close bridge, or a per-request dispatcher.

The pump retains the bounded initial read, sends one native frame per credit,
and returns native output credit only once the original parser has buffered
partial headers or the browser socket has accepted the emitted bytes. It
does not equate a socket write with browser or native application consumption.
An idle upgrade does not time out on inactivity; startup and stalled
pending bytes are bounded. A worker fence, lost browser, or expired caller
deadline stops byte forwarding and uses the original tunnel cancellation
handshake with its separate bounded cleanup budget. A positive return
verifies that tunnel's retirement only. A negative return leaves the original
handle and drain uncertainty with the caller. Closing a socket cannot undo
already-delivered bytes or show that any admitted application work has ended.

The pump tests use synthetic browser socket pairs and local native peers over
private subprocess pipes. There is no production Gateway caller, proof of
real Podman stdio, or owner/stop integration.
