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
  const config = { accountOrigin: vector.accountOrigin, workspaceOrigin: vector.workspaceOrigin };
  let now = 0;
  const f = { page, config, calls, trace, send: response, elapse: () => { now += 60_000; } };
  const environment = {
    window: globalThis,
    top: globalThis,
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
  f.input = ({ session, ...overrides } = {}) => ({
    username: vector.username,
    createSession() {
      trace.push("create");
      // Synthetic local input to the fake, never a JS transport argument or
      // a value used to generate a proof. Deliberately do not simulate crypto.
      assert.equal(SECRET.length, 30);
      return {
        ke1: toBytes(vector.ke1),
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
        ...session,
      };
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
      assert.deepEqual(f.trace, ["create", "/start", "check parameters", "discard", "/finish"]);
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
    });
  }
});

test("refuses other origins and untrusted page contexts before credentials", async t => {
  const f = harness(t);
  const { accountOrigin, workspaceOrigin } = f.config;
  for (const config of [
    { accountOrigin: accountOrigin.replace("https:", "http:") },
    { accountOrigin: accountOrigin + "/" },
    { accountOrigin: accountOrigin.replace("accounts.", "accounts.evil.") },
    { accountOrigin: accountOrigin.replace(":9443", ":09443") },
    { workspaceOrigin: workspaceOrigin.replace(":9444", ":443") },
    { workspaceOrigin: workspaceOrigin.replace(":9444", ":9443") },
    { workspaceOrigin: workspaceOrigin + "?credential=" + SECRET },
    { workspaceOrigin: accountOrigin },
  ]) {
    assert.throws(() => createLocalPakeClient({ ...f.config, ...config }), refused);
  }
  for (const [name, invalid] of [
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
      assert.deepEqual(f.trace, ["create", "/start", "discard"]);
      assert.ok(f.calls[0].options.signal.aborted);
    });
  }
});

test("invalid proof, response and cleanup are fixed failures without fallback", async t => {
  const vector = vectors[0];
  const f = harness(t);
  await assert.rejects(f.client.exchange(f.input({ session: { ke1: vector.ke1 } })), refused);
  assert.deepEqual(f.trace.splice(0), ["create", "discard"]);
  for (const finish of [
    () => { throw new Error(SECRET); },
    () => Uint8Array.of(1),
  ]) {
    await assert.rejects(f.client.exchange(f.input({ session: { finish } })), refused);
    assert.deepEqual(f.trace.splice(0), ["create", "/start", "discard"]);
  }
  f.send = call => {
    if (call.url.endsWith("/start")) return response(call);
    return response(call, { text: JSON.stringify({ ticket: vector.ticket.slice(0, -1) + "B" }) });
  };
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace.splice(0), ["create", "/start", "check parameters", "discard", "/finish"]);

  await assert.rejects(f.client.exchange(f.input({
    session: { discard() { throw new Error(SECRET); } },
  })), refused);
  assert.deepEqual(f.trace.splice(0), ["create", "/start", "check parameters"]);
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace, []);
});

test("out-of-contract async session rejection is redacted and fenced", async t => {
  const f = harness(t);
  const finish = async () => { throw new Error(SECRET); };
  await assert.rejects(f.client.exchange(f.input({ session: { finish } })), refused);
  assert.deepEqual(f.trace, ["create", "/start", "discard"]);
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
          assert.equal(ms, 30_000);
          timeout = callback;
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
      else if (stage === "deadline timer") timeout();
      else cancel.abort(new Error(SECRET));
      await assert.rejects(active, refused);
      assert.deepEqual(f.trace, stage === "finish request"
        ? ["create", "/start", "check parameters", "discard", "/finish"]
        : ["create", "/start", "discard"]);
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
  assert.deepEqual(f.trace.splice(0), ["create", "/start", "discard"]);
  f.send = call => {
    globalThis.location.origin = vectors[0].workspaceOrigin;
    return response(call);
  };
  await assert.rejects(f.client.exchange(f.input()), refused);
  assert.deepEqual(f.trace, ["create", "/start", "discard"]);
});
