# Accepted browser worker drain (unserved)

`web_guest_worker_drain.AcceptedWorkers` is private bookkeeping for **one**
already-proven Gateway listener lifetime. The
[managed Gateway composition](web-studio-managed-gateway.md) uses it privately;
there is no launcher caller. The tracker does not listen, accept, spawn a
native process, authenticate a request, store credentials, or release the
original owner. It starts only
explicitly dispatched host threads; the tests use local socket pairs and
synthetic helpers.

## Trusted integration contract

1. The accept coordinator proves that the connected socket came from the
   original listener, then calls `dispatch(stream, run)` **before** routing,
   header parsing or native launch. The tracker enforces a fixed concurrent
   capacity and pins the original socket object, descriptor and device/inode.
   An already-tracked kernel socket cannot enter again using a duplicated
   descriptor. A `False` return leaves ownership with the accept coordinator;
   it closes its own exact rejected stream. Registration transfers ownership
   even if thread startup raises. The startup failure is retained and fenced;
   the accept coordinator must not close it, retry it or treat the exception
   as a clean refusal. A registered thread cannot dispatch before its start
   result is known, and close can fence even while that start is pending.
2. `run(use)` borrows the stream; never close, detach, duplicate, transfer it
   or retain it past the worker. `current(use)` checks the exact worker thread
   and stream and the local fence. The adapter checks the original owner,
   listener and stop baseline independently. No check makes an external
   syscall atomic with later closure, and the descriptor comparison does not
   protect against arbitrary same-process races or an untracked descriptor
   alias. A confirmed foreign descriptor is disowned with `detach()` so
   neither explicit cleanup nor the original socket's finalizer closes it;
   a confirmed OS-closed descriptor is disowned before it can be reused.
   Neither is evidence of retirement, and drain stays unconfirmed. Only
   the registered target closes the original accepted stream and records
   that closure.
3. In the worker, reserve each relay **and** helper with
   `activity = admit(use, wake=...)` before any setup that can start native
   work. A refusal permits no launch. The exact activity handle is private and
   can be completed only by its own worker; another worker, a late signal or a
   wake callback cannot spend it in place of the producer. `wake` must be a
   nonblocking, idempotent cancellation latch or interrupt for that exact
   activity. It must work before launch and throughout startup and tolerate
   delivery overlapping or following successful cleanup. It must not capture
   request credentials, wait for I/O or acquire a lock held through a blocking
   operation. Check cancellation and use the original bounded workspace
   admission around native process creation. If launch races the fence after
   admission, the exact activity remains charged.
4. Call `complete(use, activity, confirmed=True)` only on independent,
   positive evidence: no launch was attempted, or the exact admitted
   activity and its transport/native side have positively retired, including
   exact EOF and reap where required by that transport. Merely returning
   from the handler, closing host pipes, collecting an empty child set, or
   killing a host exec is not that evidence. Pass `confirmed=False` on
   uncertainty. A missing receipt or a failed wake is also sticky for this
   lifetime; a later `True` cannot overwrite an uncertain activity. The
   tracker does not check native receipts or trust browser-supplied values.
5. The foreground coordinator fences the original listener and worker
   registry, interrupts the exact accepted sockets and registered activities,
   and separately stops and joins the actual acceptor. Its out-of-lock
   notifications must be bounded as above; Python cannot preempt arbitrary
   callback or thread-start code. `close(timeout=...)` spends a single bounded
   join budget across registered threads and returns `True` only when every
   registered stream was closed by its worker, that worker actually ended,
   all activities positively retired, and no cancellation delivery or sticky
   failure remains. Concurrent `close` calls cannot report success while a
   notification is in flight. A wait timeout can check the **same** drain
   again; ambiguous startup or cleanup cannot. Never call the final join
   from the worker that it would join.

An empty, fenced tracker proves only that *its own* registration set drained.
It does not account for a pending kernel `accept()` result, an untracked
legacy worker, the listener, `GuestAdmission` or the owner. Before allowing
normal history sync, owner shutdown or lease release, a later foreground
adapter must positively verify all of those separately. Keep the original
scopes on unconfirmed drain; raising into ordinary lifecycle unwind is not
a safe retention policy.

## Transport still required

Do not use the finite named HTTP metadata reader as a transparent relay.
Ordinary native `101` must remain duplex, and request/response backpressure,
framing, pairing redirects and cookies remain the pump's responsibility. A
real integration must show that each admitted relay/helper can produce a
positive retirement receipt even when an open or backpressured stream is
cancelled. Local shutdown can manufacture apparent EOF; forcing the host
process to exit does not prove the in-container socket closed. If the
existing proxy's half-close/EOF behavior is insufficient, a separate managed
tunnel needs explicit native-side retirement evidence, strict final EOF and
exact clean reap. This tracker does not approximate that protocol.

The [unserved native duplex worker and host verifier](web-guest-duplex-worker.md)
exercise that handshake and exact local reap on synthetic pipes and loopback.
Live Gateway/owner lifecycle acceptance remains separate from the private
composition and from the source-specific Podman transport gate.
