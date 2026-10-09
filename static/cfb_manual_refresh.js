/* Manual refresh controls for /college-football/data-status/.
 *
 * Same design as the NFL page (nfl_manual_refresh.js): an inline confirmation and a status line that follows the run
 * to its result, instead of browser pop-ups that a browser can silently suppress. The logic lives in
 * createRefresher(), which takes everything it touches as arguments so it runs unchanged under Node in tests; the DOM
 * wiring at the bottom is the only browser-specific part.
 *
 * The CFB status endpoint reports the latest finished run and whether a refresh is running, not a history list, so a
 * run is followed by watching for a finished record that was not there before the click.
 */
(function (root) {
  "use strict";

  var TOKEN_KEYS = ["cfbAuditToken", "nflAuditToken"];   // the page has always used the first; either is accepted
  var POLL_MS = 3000;
  var POLL_LIMIT_MS = 600000;                            // a segment can take minutes; the page says so if it is still going

  function readToken(storage) {
    for (var i = 0; i < TOKEN_KEYS.length; i += 1) {
      var value = storage.getItem(TOKEN_KEYS[i]);
      if (value && value.trim()) return value.trim();
    }
    return "";
  }

  function clearTokens(storage) {
    TOKEN_KEYS.forEach(function (key) { storage.removeItem(key); });
  }

  function createRefresher(env) {
    var base = env.baseUrl.replace(/\/?$/, "/");

    function authHeaders(token) { return { Authorization: "Bearer " + token }; }

    async function status(token) {
      var response = await env.fetch(base + "internal/cfb-refresh-status", { headers: authHeaders(token) });
      if (response.status === 401) return { rejected: true };
      if (response.status === 503) return { unconfigured: true };
      if (!response.ok) return { error: "HTTP " + response.status };
      var body = await response.json();
      return { running: !!body.running, last: body.last_refresh || null };
    }

    function fingerprint(record) {
      return record ? [record.profile, record.started_at, record.finished_at].join("|") : "";
    }

    function summarise(record, segment) {
      var seconds = record.seconds !== undefined && record.seconds !== null ? " in " + Math.round(record.seconds) + " s" : "";
      if (record.status === "success") {
        return { tone: "ok", text: "Finished " + segment + " successfully" + seconds + ". Refresh the page to see it." };
      }
      if (record.status === "degraded") {
        return { tone: "warn", text: "Finished " + segment + seconds + " with some steps degraded. Open Segment health for which." };
      }
      var why = record.error || record.message || ("status " + record.status);
      return { tone: "error", text: "The " + segment + " run did not succeed" + seconds + ": " + why };
    }

    async function follow(segment, token, beforeKey) {
      var waited = 0;
      while (waited <= env.pollLimitMs) {
        var snapshot = await status(token);
        if (snapshot.rejected) return { tone: "error", text: "The token was rejected while checking progress." };
        if (!snapshot.error && !snapshot.unconfigured) {
          var last = snapshot.last;
          if (!snapshot.running && last && fingerprint(last) !== beforeKey && last.profile === segment) {
            return summarise(last, segment);
          }
          if (snapshot.running) env.setMessage("Running " + segment + "…", "info");
        }
        await env.sleep(env.pollMs);
        waited += env.pollMs;
      }
      return { tone: "warn", text: "Started " + segment + ", but no result was recorded within " +
        Math.round(env.pollLimitMs / 60000) + " minutes. It may still be running; check Segment health in a few minutes." };
    }

    async function run(segment, typedToken) {
      var token = (typedToken || "").trim() || readToken(env.storage);
      if (!token) {
        env.setMessage("Enter the refresh token or admin PIN to run " + segment + ".", "warn");
        return { status: "needs_token" };
      }
      env.setBusy(segment);
      try {
        env.setMessage("Checking the token…", "info");
        var before = await status(token);
        if (before.rejected) {
          clearTokens(env.storage);
          env.setMessage("That token was rejected, so nothing was started. Enter it again.", "error");
          return { status: "rejected" };
        }
        if (before.unconfigured) {
          env.setMessage("The server has no refresh token or admin PIN configured, so manual refreshes are disabled.", "error");
          return { status: "not_configured" };
        }
        var beforeKey = fingerprint(before.last);
        env.storage.setItem(TOKEN_KEYS[0], token);          // only a token the server has just accepted is remembered
        env.setMessage("Starting " + segment + "…", "info");
        var response = await env.fetch(base + "internal/cfb-refresh?profile=light&segment=" + encodeURIComponent(segment),
          { method: "POST", headers: authHeaders(token) });
        if (response.status === 401) {
          clearTokens(env.storage);
          env.setMessage("That token was rejected, so nothing was started. Enter it again.", "error");
          return { status: "rejected" };
        }
        if (response.status === 503) {
          env.setMessage("The server has no refresh token or admin PIN configured, so manual refreshes are disabled.", "error");
          return { status: "not_configured" };
        }
        if (response.status !== 202 && !response.ok) {
          env.setMessage("The server did not accept the request (HTTP " + response.status + ").", "error");
          return { status: "refused" };
        }
        if (response.status !== 202) {
          // 200 means the server looked and chose not to start one; the usual reason is another refresh holding the lock
          var body = await response.json().catch(function () { return {}; });
          var reason = body.reason === "refresh_already_running"
            ? "Another refresh is already running, so nothing was started. Try again when it finishes."
            : "The server declined to start it (" + (body.reason || body.status || "skipped") + ").";
          env.setMessage(reason, "warn");
          return { status: "skipped", message: reason };
        }
        var outcome = await follow(segment, token, beforeKey);
        env.setMessage(outcome.text, outcome.tone);
        return { status: outcome.tone === "ok" ? "success" : outcome.tone === "error" ? "failed" : "unknown", message: outcome.text };
      } catch (error) {
        env.setMessage("Could not reach the server: " + (error && error.message ? error.message : error), "error");
        return { status: "network_error" };
      } finally {
        env.setBusy(null);
      }
    }

    return { run: run, hasToken: function () { return !!readToken(env.storage); } };
  }

  var api = { createRefresher: createRefresher, readToken: readToken, clearTokens: clearTokens, TOKEN_KEYS: TOKEN_KEYS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.CFBManualRefresh = api;

  // ---- browser wiring -------------------------------------------------------------------------------------------
  if (typeof document === "undefined") return;
  function wire() {
    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-cfb-rerun]"));
    var bar = document.getElementById("cfb-refresh-confirm");
    if (!buttons.length || !bar) return;
    var text = document.getElementById("cfb-refresh-confirm-text");
    var tokenWrap = document.getElementById("cfb-refresh-token-wrap");
    var tokenInput = document.getElementById("cfb-refresh-token");
    var go = document.getElementById("cfb-refresh-go");
    var cancel = document.getElementById("cfb-refresh-cancel");
    var result = document.getElementById("cfb-refresh-result");
    var pending = null;
    var storage;
    try { storage = root.sessionStorage; storage.getItem("probe"); } catch (e) {
      var memory = {};   // storage blocked (private mode): keep the token for this page view only
      storage = { getItem: function (k) { return memory[k] || null; }, setItem: function (k, v) { memory[k] = String(v); },
                  removeItem: function (k) { delete memory[k]; } };
    }

    var refresher = createRefresher({
      baseUrl: root.location.origin + "/",
      fetch: function () { return root.fetch.apply(root, arguments); },
      storage: storage,
      sleep: function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); },
      pollMs: POLL_MS,
      pollLimitMs: POLL_LIMIT_MS,
      setMessage: function (message, tone) { result.textContent = message; result.setAttribute("data-tone", tone || "info"); },
      setBusy: function (segment) {
        buttons.forEach(function (b) { b.disabled = !!segment; b.setAttribute("aria-busy", b.dataset.cfbRerun === segment ? "true" : "false"); });
        go.disabled = !!segment;
        cancel.disabled = !!segment;
      },
    });

    function open(segment, label) {
      pending = segment;
      text.textContent = "Run " + label + " now?";
      var needToken = !refresher.hasToken();
      tokenWrap.hidden = !needToken;
      bar.hidden = false;
      result.textContent = "";
      (needToken ? tokenInput : go).focus();
    }
    function close() { pending = null; bar.hidden = true; tokenInput.value = ""; }

    buttons.forEach(function (button) {
      button.addEventListener("click", function () { open(button.dataset.cfbRerun, button.textContent.trim()); });
    });
    cancel.addEventListener("click", close);
    go.addEventListener("click", async function () {
      if (!pending) return;
      var outcome = await refresher.run(pending, tokenInput.value);
      if (outcome.status === "needs_token" || outcome.status === "rejected") {
        tokenWrap.hidden = false;
        tokenInput.value = "";
        tokenInput.focus();
      } else {
        close();
      }
    });
    tokenInput.addEventListener("keydown", function (event) { if (event.key === "Enter") go.click(); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
})(typeof window !== "undefined" ? window : globalThis);
