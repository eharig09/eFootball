/* Manual refresh controls for /nfl/data-status/.
 *
 * Replaces the browser's modal pop-ups (which a browser can silently suppress, leaving a click that appears to do
 * nothing) with an inline confirmation and a status line that follows the job to its result.
 *
 * The logic lives in createRefresher(), which takes everything it touches as arguments, so it runs unchanged under
 * Node in tests; the DOM wiring at the bottom is the only browser-specific part.
 */
(function (root) {
  "use strict";

  var TOKEN_KEYS = ["nflAuditToken", "cfbAuditToken"];   // the page has always accepted either
  var POLL_MS = 3000;
  var POLL_LIMIT_MS = 150000;

  function readToken(storage) {
    for (var i = 0; i < TOKEN_KEYS.length; i += 1) {
      var value = storage.getItem(TOKEN_KEYS[i]);
      if (value && value.trim()) return value.trim();
    }
    return "";
  }

  function clearTokens(storage) {
    TOKEN_KEYS.forEach(function (key) { storage.removeItem(key); });   // both: a stale CFB token was reused forever before
  }

  function createRefresher(env) {
    var base = env.baseUrl.replace(/\/?$/, "/");

    function authHeaders(token) { return { Authorization: "Bearer " + token }; }

    async function history(token) {
      var response = await env.fetch(base + "internal/nfl-refresh-status", { headers: authHeaders(token) });
      if (response.status === 401) return { rejected: true };
      if (!response.ok) return { error: "HTTP " + response.status };
      var body = await response.json();
      return { entries: Array.isArray(body.refresh_history) ? body.refresh_history : [] };
    }

    function key(entry) { return entry.segment + "|" + entry.started_at + "|" + (entry.pid || ""); }

    function summarise(entry, segment) {
      var seconds = entry.seconds !== undefined && entry.seconds !== null ? " in " + entry.seconds + " s" : "";
      if (entry.status === "success") return { tone: "ok", text: "Finished " + segment + " successfully" + seconds + ". Refresh the page to see it in the tables." };
      var why = entry.error ? (entry.error_type ? entry.error_type + ": " : "") + entry.error : "no error text recorded";
      return { tone: "error", text: "The " + segment + " run failed" + seconds + ": " + why };
    }

    // Follow the run until the history shows its result. New entries are found by comparing with what was there before
    // the click, so the browser's clock never has to agree with the server's.
    async function follow(segment, token, before) {
      var waited = 0;
      while (waited <= env.pollLimitMs) {
        var snapshot = await history(token);
        if (snapshot.rejected) return { tone: "error", text: "The token was rejected while checking progress." };
        if (snapshot.entries) {
          var mine = snapshot.entries.filter(function (e) { return e.segment === segment && !before[key(e)]; });
          var latest = mine[mine.length - 1];
          if (latest && latest.status !== "running") return summarise(latest, segment);
          if (latest) env.setMessage("Running " + segment + "…", "info");
        }
        await env.sleep(env.pollMs);
        waited += env.pollMs;
      }
      return { tone: "warn", text: "Started " + segment + ", but no result was recorded within " + Math.round(env.pollLimitMs / 1000) +
        " s. It may still be running; refresh this page in a minute." };
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
        var before = await history(token);
        if (before.rejected) {
          clearTokens(env.storage);
          env.setMessage("That token was rejected, so nothing was started. Enter it again.", "error");
          return { status: "rejected" };
        }
        var seen = {};
        (before.entries || []).forEach(function (e) { seen[key(e)] = true; });
        env.storage.setItem(TOKEN_KEYS[0], token);          // only a token the server has just accepted is remembered
        env.setMessage("Starting " + segment + "…", "info");
        var response = await env.fetch(base + "internal/nfl-refresh?segment=" + encodeURIComponent(segment),
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
        var outcome = await follow(segment, token, seen);
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
  root.NFLManualRefresh = api;

  // ---- browser wiring -------------------------------------------------------------------------------------------
  if (typeof document === "undefined") return;
  function wire() {
    var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-nfl-rerun]"));
    var bar = document.getElementById("nfl-refresh-confirm");
    if (!buttons.length || !bar) return;
    var text = document.getElementById("nfl-refresh-confirm-text");
    var tokenWrap = document.getElementById("nfl-refresh-token-wrap");
    var tokenInput = document.getElementById("nfl-refresh-token");
    var go = document.getElementById("nfl-refresh-go");
    var cancel = document.getElementById("nfl-refresh-cancel");
    var result = document.getElementById("nfl-refresh-result");
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
        buttons.forEach(function (b) { b.disabled = !!segment; b.setAttribute("aria-busy", b.dataset.nflRerun === segment ? "true" : "false"); });
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
      button.addEventListener("click", function () { open(button.dataset.nflRerun, button.textContent.trim()); });
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
