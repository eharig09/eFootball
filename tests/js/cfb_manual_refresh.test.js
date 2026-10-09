"use strict";
// Run with: node --test tests/js
const test = require("node:test");
const assert = require("node:assert/strict");
const { createRefresher, TOKEN_KEYS } = require("../../static/cfb_manual_refresh.js");

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
    dump: () => ({ ...data }),
  };
}

function reply(status, body = {}) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}

// A scripted server. `statusReplies` are {running, last_refresh} bodies for GET .../cfb-refresh-status, in order (last repeats).
function harness({ token = "good", storage = memoryStorage(), statusReplies, postStatus = 202, postBody = { status: "accepted" },
  throwOn = null, pollLimitMs = 30000, statusCode = 200 }) {
  const calls = [];
  const messages = [];
  const busy = [];
  let statusIndex = 0;
  const fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    calls.push({ url, method, auth: (options.headers || {}).Authorization });
    if (throwOn === method) throw new Error("network down");
    const authorised = (options.headers || {}).Authorization === "Bearer " + token;
    if (method === "POST") return authorised ? reply(postStatus, postBody) : reply(401);
    if (!authorised) return reply(401);
    if (statusCode !== 200) return reply(statusCode);
    const body = statusReplies[Math.min(statusIndex, statusReplies.length - 1)];
    statusIndex += 1;
    return reply(200, body);
  };
  const refresher = createRefresher({
    baseUrl: "https://example.test", fetch, storage, pollMs: 1000, pollLimitMs,
    sleep: async () => {}, setMessage: (text, tone) => messages.push({ text, tone }), setBusy: (s) => busy.push(s),
  });
  return { refresher, calls, messages, busy, storage };
}

const old = { profile: "projections", started_at: "2026-10-02T21:34:00+00:00", finished_at: "2026-10-02T21:35:34+00:00", status: "success", seconds: 67.6 };
const fresh = { profile: "projections", started_at: "2026-10-09T18:00:00+00:00", finished_at: "2026-10-09T18:01:10+00:00", status: "success", seconds: 70.2 };
const idle = (last) => ({ running: false, last_refresh: last });
const busyNow = (last) => ({ running: true, last_refresh: last });

test("with no token it asks for one and sends nothing", async () => {
  const h = harness({ statusReplies: [idle(old)] });
  const out = await h.refresher.run("projections", "");
  assert.equal(out.status, "needs_token");
  assert.equal(h.calls.length, 0);
  assert.match(h.messages.at(-1).text, /Enter the refresh token/);
});

test("a rejected token clears every stored token and starts nothing", async () => {
  const storage = memoryStorage({ cfbAuditToken: "stale", nflAuditToken: "stale" });
  const h = harness({ token: "good", storage, statusReplies: [idle(old)] });
  const out = await h.refresher.run("projections");
  assert.equal(out.status, "rejected");
  assert.deepEqual(h.storage.dump(), {});
  assert.equal(h.calls.some((c) => c.method === "POST"), false);
  assert.deepEqual(h.busy, ["projections", null]);
});

test("success: waits for the NEW finished run, reports it, remembers only an accepted token", async () => {
  const h = harness({ statusReplies: [idle(old), busyNow(old), idle(old), idle(fresh)] });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "success");
  assert.match(h.messages.at(-1).text, /Finished projections successfully in 70 s/);
  assert.equal(h.messages.at(-1).tone, "ok");
  assert.ok(h.messages.some((m) => /Running projections/.test(m.text)));
  assert.equal(h.storage.getItem(TOKEN_KEYS[0]), "good");
  const post = h.calls.find((c) => c.method === "POST");
  assert.match(post.url, /internal\/cfb-refresh\?profile=light&segment=projections$/);
  assert.equal(post.auth, "Bearer good");
});

test("an older run of the same segment is not mistaken for this one", async () => {
  const h = harness({ statusReplies: [idle(old), idle(old), idle(old), idle(fresh)] });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "success");
  assert.match(h.messages.at(-1).text, /70 s/);                         // 68 s was the old one
});

test("a different segment finishing in the meantime is ignored", async () => {
  const other = { ...fresh, profile: "scores" };
  const h = harness({ statusReplies: [idle(old), idle(other), idle(fresh)] });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "success");
});

test("a failed run shows the server's error text", async () => {
  const failed = { ...fresh, status: "failed", seconds: 3, error: "scoreboard-projections timed out" };
  const h = harness({ statusReplies: [idle(old), idle(failed)] });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "failed");
  assert.match(h.messages.at(-1).text, /did not succeed in 3 s: scoreboard-projections timed out/);
  assert.equal(h.messages.at(-1).tone, "error");
});

test("a degraded run is reported as a warning, not as success", async () => {
  const degraded = { ...fresh, status: "degraded" };
  const h = harness({ statusReplies: [idle(old), idle(degraded)] });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "unknown");
  assert.equal(h.messages.at(-1).tone, "warn");
});

test("no recorded result within the limit says so instead of hanging", async () => {
  const h = harness({ statusReplies: [idle(old)], pollLimitMs: 120000 });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "unknown");
  assert.match(h.messages.at(-1).text, /no result was recorded within 2 minutes/);
  assert.equal(h.messages.at(-1).tone, "warn");
});

test("the server declining because another refresh holds the lock is explained, not followed", async () => {
  const h = harness({ statusReplies: [idle(old)], postStatus: 200, postBody: { status: "skipped", reason: "refresh_already_running" } });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "skipped");
  assert.match(h.messages.at(-1).text, /Another refresh is already running/);
  assert.equal(h.messages.at(-1).tone, "warn");
});

test("an unconfigured server and a refusal are each explained", async () => {
  const unconfigured = harness({ statusReplies: [idle(old)], statusCode: 503 });
  assert.equal((await unconfigured.refresher.run("projections", "good")).status, "not_configured");
  assert.match(unconfigured.messages.at(-1).text, /no refresh token or admin PIN configured/);
  const refused = harness({ statusReplies: [idle(old)], postStatus: 500 });
  assert.equal((await refused.refresher.run("projections", "good")).status, "refused");
  assert.match(refused.messages.at(-1).text, /HTTP 500/);
});

test("a network failure is reported and the buttons are released", async () => {
  const h = harness({ statusReplies: [idle(old)], throwOn: "GET" });
  const out = await h.refresher.run("projections", "good");
  assert.equal(out.status, "network_error");
  assert.match(h.messages.at(-1).text, /Could not reach the server: network down/);
  assert.equal(h.busy.at(-1), null);
});

test("a typed token wins over a stored one and is trimmed", async () => {
  const storage = memoryStorage({ cfbAuditToken: "stale" });
  const h = harness({ token: "good", storage, statusReplies: [idle(old), idle(fresh)] });
  const out = await h.refresher.run("projections", "  good  ");
  assert.equal(out.status, "success");
  assert.equal(h.calls[0].auth, "Bearer good");
  assert.equal(h.storage.getItem("cfbAuditToken"), "good");
});
