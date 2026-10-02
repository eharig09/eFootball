"use strict";
// Run with: node --test tests/js
const test = require("node:test");
const assert = require("node:assert/strict");
const { createRefresher, TOKEN_KEYS } = require("../../static/nfl_manual_refresh.js");

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

// A scripted server: `statusReplies` are returned in order for GET .../nfl-refresh-status (the last one repeats).
function harness({ token = "good", storage = memoryStorage(), statusReplies, postStatus = 202, throwOn = null, pollLimitMs = 30000 }) {
  const calls = [];
  const messages = [];
  const busy = [];
  let statusIndex = 0;
  const fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    calls.push({ url, method, auth: (options.headers || {}).Authorization });
    if (throwOn === method) throw new Error("network down");
    const authorised = (options.headers || {}).Authorization === "Bearer " + token;
    if (method === "POST") return authorised ? reply(postStatus, { status: "accepted", season: 2026 }) : reply(401);
    if (!authorised) return reply(401);
    const body = statusReplies[Math.min(statusIndex, statusReplies.length - 1)];
    statusIndex += 1;
    return reply(200, { refresh_history: body });
  };
  const refresher = createRefresher({
    baseUrl: "https://example.test", fetch, storage, pollMs: 1000, pollLimitMs,
    sleep: async () => {}, setMessage: (text, tone) => messages.push({ text, tone }), setBusy: (s) => busy.push(s),
  });
  return { refresher, calls, messages, busy, storage };
}

const old = { segment: "weather", started_at: "2026-10-02T04:40:00+00:00", status: "success", seconds: 17.2, pid: 1 };
const running = { segment: "weather", started_at: "2026-10-02T10:05:00+00:00", status: "running", pid: 2 };
const done = { ...running, status: "success", seconds: 12.5 };

test("with no token it asks for one and sends nothing", async () => {
  const h = harness({ statusReplies: [[]] });
  const out = await h.refresher.run("weather", "");
  assert.equal(out.status, "needs_token");
  assert.equal(h.calls.length, 0);
  assert.match(h.messages.at(-1).text, /Enter the refresh token/);
});

test("a rejected token clears BOTH stored tokens and starts nothing", async () => {
  const storage = memoryStorage({ nflAuditToken: "stale-nfl", cfbAuditToken: "stale-cfb" });
  const h = harness({ token: "good", storage, statusReplies: [[]] });
  const out = await h.refresher.run("weather");
  assert.equal(out.status, "rejected");
  assert.deepEqual(h.storage.dump(), {});                       // the old code left the CFB one, so every later click failed
  assert.equal(h.calls.some((c) => c.method === "POST"), false);
  assert.equal(h.messages.at(-1).tone, "error");
  assert.deepEqual(h.busy, ["weather", null]);                  // buttons are released again
});

test("a stale CFB token alone is also cleared (the original bug)", async () => {
  const storage = memoryStorage({ cfbAuditToken: "stale-cfb" });
  const h = harness({ token: "good", storage, statusReplies: [[]] });
  assert.equal((await h.refresher.run("weather")).status, "rejected");
  assert.equal(h.refresher.hasToken(), false);                  // so the next click prompts instead of failing forever
});

test("success: waits for the NEW run, reports it, and remembers only an accepted token", async () => {
  const h = harness({ statusReplies: [[old], [old, running], [old, done]] });
  const out = await h.refresher.run("weather", "good");
  assert.equal(out.status, "success");
  assert.match(h.messages.at(-1).text, /Finished weather successfully in 12.5 s/);
  assert.equal(h.messages.at(-1).tone, "ok");
  assert.ok(h.messages.some((m) => /Running weather/.test(m.text)));      // the user sees progress, not silence
  assert.equal(h.storage.getItem(TOKEN_KEYS[0]), "good");
  assert.deepEqual(h.busy, ["weather", null]);
  const post = h.calls.find((c) => c.method === "POST");
  assert.match(post.url, /internal\/nfl-refresh\?segment=weather$/);
  assert.equal(post.auth, "Bearer good");
});

test("an older success for the same segment is not mistaken for this run", async () => {
  const h = harness({ statusReplies: [[old], [old], [old], [old, done]] });
  const out = await h.refresher.run("weather", "good");
  assert.equal(out.status, "success");
  assert.match(h.messages.at(-1).text, /12.5 s/);                         // 17.2 s was the old one
});

test("a failed run shows the server's error text", async () => {
  const failed = { ...running, status: "failed", seconds: 0.5, error_type: "RuntimeError", error: "Open-Meteo daily quota exhausted" };
  const h = harness({ statusReplies: [[], [failed]] });
  const out = await h.refresher.run("weather", "good");
  assert.equal(out.status, "failed");
  assert.match(h.messages.at(-1).text, /failed in 0.5 s: RuntimeError: Open-Meteo daily quota exhausted/);
  assert.equal(h.messages.at(-1).tone, "error");
});

test("no recorded result within the limit says so instead of hanging", async () => {
  const h = harness({ statusReplies: [[old]], pollLimitMs: 5000 });
  const out = await h.refresher.run("weather", "good");
  assert.equal(out.status, "unknown");
  assert.match(h.messages.at(-1).text, /no result was recorded within 5 s/);
  assert.equal(h.messages.at(-1).tone, "warn");
});

test("the server refusing or being unconfigured is explained", async () => {
  const unconfigured = harness({ statusReplies: [[]], postStatus: 503 });
  assert.equal((await unconfigured.refresher.run("weather", "good")).status, "not_configured");
  assert.match(unconfigured.messages.at(-1).text, /no refresh token or admin PIN configured/);
  const refused = harness({ statusReplies: [[]], postStatus: 500 });
  assert.equal((await refused.refresher.run("weather", "good")).status, "refused");
  assert.match(refused.messages.at(-1).text, /HTTP 500/);
});

test("a network failure is reported and the buttons are released", async () => {
  const h = harness({ statusReplies: [[]], throwOn: "GET" });
  const out = await h.refresher.run("weather", "good");
  assert.equal(out.status, "network_error");
  assert.match(h.messages.at(-1).text, /Could not reach the server: network down/);
  assert.deepEqual(h.busy.at(-1), null);
});

test("a typed token wins over a stored one, and is trimmed", async () => {
  const storage = memoryStorage({ nflAuditToken: "stale" });
  const h = harness({ token: "good", storage, statusReplies: [[], [done]] });
  const out = await h.refresher.run("weather", "  good  ");
  assert.equal(out.status, "success");
  assert.equal(h.calls[0].auth, "Bearer good");
  assert.equal(h.storage.getItem("nflAuditToken"), "good");
});
