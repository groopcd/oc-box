// Offline transport and state fixture. No sockets, WASM or authentication.
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createLocalPakeClient } from "../src/oc_box/web_pake_client.mjs";

const vectors = JSON.parse(readFileSync(0, "utf8"));
const REFUSAL = "Named sign-in refused.";
const SECRET = "offline-only credential marker";
const toBytes = b64 => Uint8Array.from(Buffer.from(b64, "base64"));
const LOCAL = ["open", "local start", "read password"];
const START = [...LOCAL, "/start"];
const PROOF = [...START, "check parameters", "discard", "/finish"];
const refused = error => {
  assert.equal(error.message, REFUSAL);
  assert.equal(error.cause, undefined);
  assert.doesNotMatch(error.stack, /offline-only credential marker/);
  return true;
};

function response(call, replace = {}) {
  const { status, headers, body } = call.reply;
  const bytes = new TextEncoder().encode(replace.text ?? body);
  const envelope = {
    status,
    url: call.url,
    type: "basic",
    redirected: false,
    headers: new Headers({ ...headers, "Content-Length": String(bytes.length) }),
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(bytes.subarray(0, 7));
        if (bytes.length > 7) controller.enqueue(bytes.subarray(7));
        controller.close();
      },
    }),
    ...replace,
  };
  return envelope;
}

function harness(t, vector = vectors[0]) {
  const page = new EventTarget();
  const calls = [];
  const trace = [];
  const config = {
    accountOrigin: vector.accountOrigin,
    workspaceOrigin: vector.workspaceOrigin,
    openAttempt(...args) {
      assert.deepEqual(args, []); // The binding factory never gets a password reader.
      trace.push("open");
      return f.changeAttempt({
        start(readPassword) {
          trace.push("local start");
          const password = readPassword();
          try {
            assert.equal(new TextDecoder().decode(password), SECRET);
            return toBytes(vector.ke1);
          } finally {
            password.fill(0);
          }
        },
        finish(params) {
          trace.push("check parameters");
          assert.deepEqual(params, {
            ke2: toBytes(vector.ke2),
            context: toBytes(vector.context),
            clientIdentifier: toBytes(vector.clientIdentifier),
            verifierIdentifier: toBytes(vector.verifierIdentifier),
          });
          return toBytes(vector.ke3);
        },
        discard() {
          trace.push("discard");
          return true;
        },
      });
    },
  };
  let now = 0;
  const f = { page, config, calls, trace, send: response,
    elapse: (ms = 60_000) => { now += ms; }, changeAttempt: attempt => attempt };
  const environment = {
    window: globalThis,
    top: globalThis,
    document: {},
    location: { origin: config.accountOrigin },
    isSecureContext: true,
    navigator: { serviceWorker: { controller: null } },
    addEventListener: page.addEventListener.bind(page),
    removeEventListener: page.removeEventListener.bind(page),
    performance: { now: () => now },
    crypto: webcrypto,
    fetch(url, options) {
      const path = new URL(url).pathname;
      const call = { url, options, reply: vector[path.slice(1)] };
      calls.push(call);
      trace.push(path);
      return f.send(call);
    },
  };
  for (const [name, value] of Object.entries(environment)) {
    const saved = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { value, writable: true, configurable: true });
    t.after(() => saved ? Object.defineProperty(globalThis, name, saved) : delete globalThis[name]);
  }
  f.input = (overrides = {}) => ({
    username: vector.username,
    readPassword() {
      trace.push("read password");
      return new TextEncoder().encode(SECRET);
    },
    ...overrides,
  });
  f.client = createLocalPakeClient(config);
  return f;
}

test("Python profile exchange with synthetic session", async t => {
  for (const [index, vector] of vectors.entries()) {
    await t.test(`configured origin pair ${index + 1}`, async sub => {
      const f = harness(sub, vector);
      const ticket = await f.client.exchange(f.input());
      assert.deepEqual(f.trace, PROOF);
      assert.equal(JSON.stringify(ticket), "{}");
      assert.equal(ticket.take(), vector.ticket);
      assert.equal(ticket.take(), null);
      assert.deepEqual(f.calls.map(call => call.url), [
        vector.accountOrigin + "/start", vector.accountOrigin + "/finish",
      ]);
      for (const { options: { body, signal, ...policy } } of f.calls) {
        assert.deepEqual(policy, {
          method: "POST",
          mode: "same-origin",
          credentials: "same-origin",
          redirect: "error",
          cache: "no-store",
          referrerPolicy: "origin",
          keepalive: false,
          headers: { "Content-Type": "application/json" },
        });
        assert.ok(signal instanceof AbortSignal);
        assert.ok(!body.includes(SECRET));
      }
      assert.equal(f.calls[0].options.body, vector.startRequest);
      assert.equal(f.calls[1].options.body, vector.finishRequest);
      assert.equal(f.calls[0].options.signal, f.calls[1].options.signal);
    });
  }
});

test("refuses other origins and untrusted page contexts before credentials", async t => {
  const f = harness(t);
  const { accountOrigin, workspaceOrigin } = f.config;
  const mountedDocument = globalThis.document;
  globalThis.document = {}; // Check validation independently of the single-controller guard.
  for (const config of [
    { accountOrigin: accountOrigin.replace("https:", "http:") },
    { accountOrigin: accountOrigin + "/" },
    { accountOrigin: accountOrigin.replace("accounts.", "accounts.evil.") },
    { accountOrigin: accountOrigin.replace(":9443", ":09443") },
    { workspaceOrigin: workspaceOrigin.replace(":9444", ":443") },
    { workspaceOrigin: workspaceOrigin.replace(":9444", ":9443") },
    { workspaceOrigin: workspaceOrigin + "?credential=" + SECRET },
    { workspaceOrigin: accountOrigin },
    { openAttempt: undefined, createSession() { assert.fail("old binding called"); } },
  ]) {
    assert.throws(() => createLocalPakeClient({ ...f.config, ...config }), refused);
  }
  globalThis.document = mountedDocument;
  assert.throws(() => createLocalPakeClient(f.config), refused);
  for (const [name, invalid] of [
    ["document", {}],
    ["location", { origin: workspaceOrigin }],
    ["top", {}],
    ["isSecureContext", false],
    ["navigator", { serviceWorker: { controller: {} } }],
  ]) {
    const saved = globalThis[name];
    globalThis[name] = invalid;
    await assert.rejects(f.client.exchange(f.input()), refused);
    globalThis[name] = saved;
  }
  for (const username of ["", "UPPER", "user\n", "a".repeat(33), 12, "úser"]) {
    await assert.rejects(f.client.exchange(f.input({ username })), refused);
  }
  assert.deepEqual(f.trace, []);
});

test("attempt ownership precedes the one-shot password read and local failures clean up", async t => {
  const f = harness(t);
  const accessor = Object.defineProperty(f.input(), "readPassword", {
    get() { f.trace.push("password accessor"); throw new Error(SECRET); },
  });
  await assert.rejects(f.client.exchange(accessor), refused);
  assert.deepEqual(f.trace, []);
  f.changeAttempt = attempt => ({ ...attempt, start: undefined });
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace.splice(0), ["open", "discard"]);
  f.changeAttempt = attempt => ({ ...attempt, start(read) {
    attempt.start(read);
    throw new Error(SECRET);
  } });
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace.splice(0), [...LOCAL, "discard"]);
  f.changeAttempt = attempt => attempt;
  await assert.rejects(f.client.exchange(f.input({ readPassword() { throw new Error(SECRET); } })), refused);
  assert.deepEqual(f.trace.splice(0), ["open", "local start", "discard"]);
  assert.deepEqual(f.calls, []);

  let retainedReader;
  f.changeAttempt = attempt => ({
    ...attempt,
    get discard() {
      f.trace.push("get discard");
      return attempt.discard;
    },
    start(read) {
      retainedReader = read;
      const ke1 = attempt.start(read);
      assert.throws(read, refused);
      return ke1;
    },
  });
  const ticket = await f.client.exchange(f.input());
  assert.throws(retainedReader, refused);
  assert.equal(ticket.take(), vectors[0].ticket);
  assert.deepEqual(f.trace, ["open", "get discard", ...PROOF.slice(1)]);
  assert.throws(() => createLocalPakeClient(f.config), refused);
});

test("unknown construction or discard outcome permanently fences the page", async t => {
  for (const [name, changeAttempt, expected] of [
    ["construction exception", () => { throw new Error(SECRET); }, ["open"]],
    ["async construction failure", async () => { throw new Error(SECRET); }, ["open"]],
    ["no handle", () => undefined, ["open"]],
    ["missing discard", attempt => ({ ...attempt, discard: undefined }), ["open"]],
    ["failed discard access", attempt => ({ ...attempt, get discard() {
      throw new Error(SECRET);
    } }), ["open"]],
    ["unconfirmed discard after start failure", attempt => ({
      ...attempt,
      start(read) {
        attempt.start(read);
        throw new Error(SECRET);
      },
      discard() { return "true"; },
    }), LOCAL],
  ]) {
    await t.test(name, async sub => {
      const f = harness(sub);
      f.changeAttempt = changeAttempt;
      await assert.rejects(f.client.exchange(f.input()), refused);
      assert.deepEqual(f.trace, expected);
      assert.deepEqual(f.calls, []);
      f.changeAttempt = attempt => attempt;
      await assert.rejects(f.client.exchange(f.input()), refused);
      assert.throws(() => createLocalPakeClient(f.config), refused);
      assert.deepEqual(f.trace, expected);
    });
  }
});

test("actual abort during synchronous work gates progress at the next checkpoint", async t => {
  for (const phase of ["opening", "starting", "response"]) {
    await t.test(phase, async sub => {
      const f = harness(sub);
      const caller = new AbortController();
      caller.signal.addEventListener("abort", event => event.stopImmediatePropagation());
      const cancel = () => caller.abort(new Error(SECRET));
      f.changeAttempt = attempt => {
        if (phase === "opening") cancel();
        return { ...attempt, start(read) {
          const ke1 = attempt.start(read);
          if (phase === "starting") {
            cancel();
            f.trace.push("local call returned");
          }
          return ke1;
        } };
      };
      f.send = call => {
        cancel();
        return response(call);
      };
      await assert.rejects(f.client.exchange(f.input({ signal: caller.signal })), refused);
      assert.deepEqual(f.trace, phase === "opening" ? ["open", "discard"]
        : phase === "starting" ? [...LOCAL, "local call returned", "discard"]
          : [...START, "discard"]);
    });
  }
});

test("refuses noncanonical or unbound start replies before proof", async t => {
  const vector = vectors[0];
  const body = JSON.parse(vector.start.body);
  const cases = {
    "HTTP refusal with diagnostic body": { status: 403, text: SECRET },
    "redirected reply": { redirected: true },
    "other path": { url: vector.accountOrigin + "/start?wrong=1" },
    "foreign response": { url: vector.workspaceOrigin + "/start", type: "cors" },
    "unexpected MIME": { headers: new Headers({ ...vector.start.headers, "Content-Type": "text/html" }) },
    "encoded response": { headers: new Headers({ ...vector.start.headers, "Content-Encoding": "gzip" }) },
    "declared body overflow": { text: "A".repeat(8193) },
    "stream exceeds declaration": {
      text: vector.start.body + "x", headers: new Headers(vector.start.headers),
    },
    "short stream": { text: "{}", headers: new Headers(vector.start.headers) },
    "non-ASCII bytes": { text: vector.start.body.replace("ke2", "ké2") },
    "duplicate property": { text: vector.start.body.replace("{", '{"ke2":"ignored",') },
    "extra field": { text: JSON.stringify({ ...body, password: SECRET }) },
    "noncanonical JSON": { text: JSON.stringify(body, null, 2) },
    "wrong type": { text: JSON.stringify({ ...body, ke2: [body.ke2] }) },
    "malformed challenge": { text: JSON.stringify({ ...body, challenge_id: "A".repeat(42) + "B" }) },
    "wrong KE2 size": { text: JSON.stringify({ ...body, ke2: vector.ke1 }) },
    "noncanonical KE2 padding bits": { text: JSON.stringify({ ...body, ke2: body.ke2.slice(0, -2) + "9=" }) },
    "different transcript context": { text: JSON.stringify({ ...body, context: vectors[1].context }) },
  };
  for (const [name, replace] of Object.entries(cases)) {
    await t.test(name, async sub => {
      const f = harness(sub);
      f.send = call => response(call, replace);
      await assert.rejects(f.client.exchange(f.input()), refused);
      assert.deepEqual(f.trace, [...START, "discard"]);
      assert.ok(f.calls[0].options.signal.aborted);
    });
  }
});

test("invalid proof, response and cleanup are fixed failures without fallback", async t => {
  const vector = vectors[0];
  const f = harness(t);
  f.changeAttempt = attempt => ({ ...attempt, start(read) {
    attempt.start(read);
    return vector.ke1; // A wire string is not a trusted binding's KE1 bytes.
  } });
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace.splice(0), [...LOCAL, "discard"]);
  for (const finish of [
    () => { throw new Error(SECRET); },
    () => Uint8Array.of(1),
  ]) {
    f.changeAttempt = attempt => ({ ...attempt, finish });
    await assert.rejects(f.client.exchange(f.input()), refused);
    assert.deepEqual(f.trace.splice(0), [...START, "discard"]);
  }
  f.changeAttempt = attempt => attempt;
  f.send = call => {
    if (call.url.endsWith("/start")) return response(call);
    return response(call, { text: JSON.stringify({ ticket: vector.ticket.slice(0, -1) + "B" }) });
  };
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace.splice(0), PROOF);

  f.changeAttempt = attempt => ({ ...attempt, discard() { throw new Error(SECRET); } });
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace.splice(0), [...START, "check parameters"]);
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.throws(() => createLocalPakeClient(f.config), refused);
  assert.deepEqual(f.trace, []);
});

test("out-of-contract async session rejection is redacted and fenced", async t => {
  const f = harness(t);
  const finish = async () => { throw new Error(SECRET); };
  f.changeAttempt = attempt => ({ ...attempt, finish });
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace, [...START, "discard"]);
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.equal(f.calls.length, 1);
});

test("one live attempt; abort fetch, body read and late finish without retry", async t => {
  for (const stage of ["start request", "start body", "finish request", "deadline timer"]) {
    await t.test(stage, async sub => {
      const f = harness(sub);
      const cancel = new AbortController();
      let timeout;
      if (stage === "deadline timer") {
        const schedule = globalThis.setTimeout;
        sub.mock.method(globalThis, "setTimeout", (callback, ms) => {
          if (timeout === undefined) {
            assert.equal(ms, 30_000);
            timeout = callback;
          }
          return schedule(callback, ms);
        });
      }
      let reached;
      const suspended = new Promise(resolve => { reached = resolve; });
      f.send = call => {
        const signal = call.options.signal;
        if (stage === "finish request" && call.url.endsWith("/start")) return response(call);
        reached();
        if (stage === "start body") {
          return response(call, { body: new ReadableStream({
            start(controller) {
              signal.addEventListener("abort", () => controller.error(signal.reason), { once: true });
            },
          }) });
        }
        return new Promise((_, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        });
      };
      const active = f.client.exchange(f.input({ signal: cancel.signal }));
      await suspended;
      await assert.rejects(f.client.exchange(f.input()), refused);
      if (stage === "finish request") f.page.dispatchEvent(new Event("pagehide"));
      else if (stage === "deadline timer") {
        f.elapse(30_000);
        timeout();
      }
      else cancel.abort(new Error(SECRET));
      await assert.rejects(active, refused);
      assert.deepEqual(f.trace, stage === "finish request"
        ? PROOF
        : [...START, "discard"]);
      assert.ok(f.calls.every(call => call.options.signal.aborted));
      assert.ok(f.calls.every(call => !String(call.options.signal.reason).includes(SECRET)));
      f.send = response;
      await assert.rejects(f.client.exchange(f.input({ signal: AbortSignal.abort(SECRET) })), refused);
      assert.equal((await f.client.exchange(f.input())).take(), vectors[0].ticket);
    });
  }
});

test("deadline and changed account page prevent stale proof", async t => {
  const f = harness(t);
  f.send = call => {
    f.elapse();
    return response(call);
  };
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace.splice(0), [...START, "discard"]);
  f.send = call => {
    globalThis.location.origin = vectors[0].workspaceOrigin;
    return response(call);
  };
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace, [...START, "discard"]);
});

test("the pending ticket retains one controller and invalidates until used or discarded", async t => {
  for (const cause of ["cancel", "pagehide", "discard", "expiry timer", "expiry checkpoint",
    "blocked abort notification", "document loss"]) {
    await t.test(cause, async sub => {
      const f = harness(sub);
      const caller = new AbortController();
      const scheduled = [];
      const schedule = globalThis.setTimeout;
      sub.mock.method(globalThis, "setTimeout", (callback, ms) => {
        scheduled.push({ callback, ms });
        return schedule(callback, ms);
      });
      if (cause === "blocked abort notification") {
        caller.signal.addEventListener("abort", event => event.stopImmediatePropagation());
      }

      const ticket = await f.client.exchange(f.input({ signal: caller.signal }));
      const transport = f.calls[0].options.signal;
      assert.deepEqual(f.trace, PROOF);
      assert.equal(transport, f.calls[1].options.signal);
      assert.equal(transport.aborted, false);
      assert.equal(JSON.stringify(ticket), "{}");
      await assert.rejects(f.client.exchange(f.input()), refused);
      assert.throws(() => createLocalPakeClient(f.config), refused);
      assert.deepEqual(scheduled.map(({ ms }) => ms), [30_000, 5000]);
      caller.signal.dispatchEvent(new Event("abort"));
      assert.equal(transport.aborted, false, "synthetic event must not cancel");

      let document;
      switch (cause) {
        case "cancel":
        case "blocked abort notification":
          caller.abort(new Error(SECRET));
          break;
        case "pagehide":
          f.page.dispatchEvent(new Event("pagehide"));
          break;
        case "discard":
          ticket.discard();
          ticket.discard();
          break;
        case "expiry timer":
          f.elapse(5000);
          scheduled[1].callback();
          break;
        case "expiry checkpoint":
          f.elapse(5000);
          break;
        case "document loss":
          document = globalThis.document;
          globalThis.document = {};
          break;
      }
      if (["cancel", "pagehide", "discard", "expiry timer"].includes(cause)) {
        assert.ok(transport.aborted, "invalidation should release without a take()");
      }
      assert.equal(ticket.take(), null);
      assert.ok(transport.aborted);
      assert.ok(!String(transport.reason).includes(SECRET));
      if (document) globalThis.document = document;
      const fresh = await f.client.exchange(f.input());
      assert.notEqual(f.calls[2].options.signal, transport);
      ticket.discard(); // A retired ticket cannot release this new attempt.
      assert.equal(fresh.take(), vectors[0].ticket);
      assert.equal(fresh.take(), null);
      assert.equal(f.calls[2].options.signal.aborted, true);
    });
  }
});

test("ticket expiry cannot extend the original attempt deadline", async t => {
  const f = harness(t);
  f.send = call => {
    if (call.url.endsWith("/finish")) f.elapse(29_900);
    return response(call);
  };
  const ticket = await f.client.exchange(f.input());
  f.elapse(100);
  assert.equal(ticket.take(), null);
  assert.ok(f.calls[1].options.signal.aborted);
});
