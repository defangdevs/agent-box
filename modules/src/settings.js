(function () {
  "use strict";
  function applyDoc(doc, ids) {
    ids.forEach(function (id) {
      var from = doc.getElementById(id);
      var to = document.getElementById(id);
      if (!from || !to) { return; }
      // Which rows the operator has folded open is state this HTML does
      // not carry: the live feed replaces #sessions-list on every session
      // change, so without this a subscription fold snaps shut under
      // whoever just opened it. data-fold is the row's identity.
      var open = {};
      Array.prototype.forEach.call(
        to.querySelectorAll("details[data-fold][open]"),
        function (d) { open[d.getAttribute("data-fold")] = 1; });
      Array.prototype.forEach.call(
        from.querySelectorAll("details[data-fold]"),
        function (d) { if (open[d.getAttribute("data-fold")]) { d.open = true; } });
      to.replaceWith(document.importNode(from, true));
    });
  }
  function parseHTML(text) {
    return new DOMParser().parseFromString(text, "text/html");
  }

  // Shared writer for the Danger-zone progress spans (#restart-status,
  // #update-status): set the data-state colour + text, optionally
  // appending a trailing link.
  function setStatus(el, state, text, linkText, href) {
    if (!el) { return; }
    el.setAttribute("data-state", state);
    el.textContent = text;
    if (linkText && href) {
      var link = document.createElement("a");
      link.href = href;
      link.textContent = linkText;
      link.rel = "noreferrer";
      el.appendChild(document.createTextNode(" "));
      el.appendChild(link);
    }
  }
  // One GET of the JSON progress feed; resolves null on any error so
  // callers treat "daemon briefly gone" (mid-rebuild) like any other
  // not-yet-ready poll rather than a hard failure.
  function fetchStatus(url) {
    return fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (r) { if (!r.ok) { throw new Error(); } return r.json(); })
      .catch(function () { return null; });
  }
  // Re-fetch the page this script is running on.
  //
  // The daemon can decide the current URL is no longer the right page:
  // /<user>/ redirects to the settings page once the last session is gone.
  // Splicing fragments out of THAT answer patches nothing — applyDoc skips an
  // id the other document does not have — so the tab bar would sit there
  // showing a session that no longer exists until someone reloaded by hand.
  // Follow the redirect for real instead.
  function fetchPage() {
    var here = window.location.pathname;
    return fetch(here + window.location.search).then(function (r) {
      if (r.redirected && new URL(r.url).pathname !== here) {
        window.location.replace(r.url);
        return null;
      }
      return r.text();
    });
  }
  // Re-fetch the current page and patch the live session list/tabs, so
  // the visible list tracks a restart even when nothing was "starting"
  // at submit time (which is what otherwise gates schedulePoll).
  function pollPageOnce() {
    return fetchPage()
      .then(function (t) {
        if (t === null) { return; }
        applyDoc(parseHTML(t), ["sessions-list", "tab-bar"]);
        wsSync();
      })
      .catch(function () {});
  }
  // Coalescing wrapper for the live feed, which can report a burst of
  // changes (a session added, then coming live a beat later): one
  // re-fetch shortly after the last of them, instead of one each.
  // Deliberately not an in-flight guard — a request that never settles
  // would wedge every later refresh, which is the exact staleness this
  // whole feed exists to prevent.
  var refreshTimer = null;
  function scheduleRefresh() {
    if (refreshTimer) { return; }
    refreshTimer = window.setTimeout(function () {
      refreshTimer = null;
      pollPageOnce();
    }, 150);
  }

  // After "Update box": watch the update oneshot from `baseline` (its
  // start time before we triggered) until a strictly newer run
  // finishes. The rebuild may restart this daemon, so a failed fetch is
  // "still rebuilding", not an error.
  function watchUpdate(url, baseline, rev0) {
    var el = document.getElementById("update-status");
    if (!el) { return; }
    var tries = 0, MAX = 300;             // ~12 min at 2.5s
    setStatus(el, "checking", "Starting update…");
    (function tick() {
      if (tries++ > MAX) {
        setStatus(el, "blocked", "Update still running — check the box shortly.");
        return;
      }
      fetchStatus(url).then(function (s) {
        if (!s || !s.update) {           // daemon switching, or no unit to watch
          setStatus(el, "checking", "Rebuilding the system…");
          window.setTimeout(tick, 2500);
          return;
        }
        var u = s.update;
        if (u.active === "activating" || u.active === "active") {
          setStatus(el, "available", "Update in progress…");
          window.setTimeout(tick, 2500);
          return;
        }
        if (u.since > baseline) {        // a newer run started and is no longer active → done
          if (u.active === "failed" || u.result !== "success") {
            setStatus(el, "blocked", "Update failed — check the update service journal.");
          } else if (s.rev && rev0 && s.rev !== rev0) {
            var repo = el.getAttribute("data-repo");
            var short = s.rev.slice(0, 12);
            if (repo) {
              var href = "https://github.com/" +
                repo.split("/").map(encodeURIComponent).join("/") +
                "/commit/" + encodeURIComponent(s.rev);
              setStatus(el, "current", "Updated — now at " + short + ".", "View commit", href);
            } else {
              setStatus(el, "current", "Updated — now at " + short + ".");
            }
          } else {
            setStatus(el, "current", "Update finished.");
          }
          return;
        }
        setStatus(el, "checking", "Starting update…");   // triggered, run not registered yet
        window.setTimeout(tick, 2500);
      });
    })();
  }
  // After "Restart all": watch the live session count recover. Wait to
  // see it dip below the configured count before declaring success, so
  // the pre-kill "all live" state isn't misread as "done".
  function watchRestart(url) {
    var el = document.getElementById("restart-status");
    if (!el) { return; }
    var dipped = false, tries = 0, MAX = 40;   // ~100s at 2.5s
    setStatus(el, "checking", "Restarting sessions…");
    (function tick() {
      if (tries++ > MAX) {
        setStatus(el, "blocked", "Still restarting — check the session list.");
        return;
      }
      pollPageOnce();
      fetchStatus(url).then(function (s) {
        if (!s || !s.sessions) { window.setTimeout(tick, 2500); return; }
        var conf = s.sessions.configured, live = s.sessions.live;
        if (conf === 0) { setStatus(el, "current", "Restart requested."); return; }
        if (live < conf) { dipped = true; }
        if (dipped && live >= conf) {
          setStatus(el, "current", "All sessions restarted.");
          return;
        }
        window.setTimeout(tick, 2500);
      });
    })();
  }

  // The page itself never waits on GitHub. Once it is visible, make a
  // single compare request: GitHub reports whether repository HEAD is
  // ahead of the running revision and provides the commit count. The
  // rendered compare link remains useful if the request is blocked or
  // rate-limited.
  function checkForUpdate() {
    var el = document.getElementById("update-status");
    if (!el) { return; }
    var repo = el.getAttribute("data-repo");
    var rev = el.getAttribute("data-rev");
    var fallback = el.getAttribute("data-compare-url");
    if (!repo || !rev || !fallback) { return; }

    function show(state, text, linkText, href) {
      el.setAttribute("data-state", state);
      el.textContent = text;
      if (linkText && href) {
        var link = document.createElement("a");
        link.href = href;
        link.textContent = linkText;
        link.rel = "noreferrer";
        el.appendChild(document.createTextNode(" "));
        el.appendChild(link);
      }
    }

    var repoPath = repo.split("/").map(encodeURIComponent).join("/");
    var api = "https://api.github.com/repos/" + repoPath +
              "/compare/" + encodeURIComponent(rev) + "...HEAD";
    show("checking", "Checking GitHub for agent-box updates…");
    fetch(api, {
      credentials: "omit",
      headers: { "Accept": "application/vnd.github+json" },
      referrerPolicy: "no-referrer"
    })
      .then(function (r) {
        if (!r.ok) { throw new Error("GitHub returned " + r.status); }
        return r.json();
      })
      .then(function (result) {
        if (result.status === "identical") {
          show("current", "No agent-box code update.");
          return;
        }
        if (result.status === "ahead") {
          var count = Number(result.ahead_by) || 0;
          var commits = count ? count + " commit" + (count === 1 ? "" : "s") : "new commits";
          var head = result.head_commit && result.head_commit.sha;
          var href = head
            ? "https://github.com/" + repoPath + "/compare/" +
              encodeURIComponent(rev) + "..." + encodeURIComponent(head)
            : fallback;
          show("available", "agent-box update available — " + commits + ".", "View changes", href);
          return;
        }
        show("blocked", "Automatic agent-box update unavailable.", "Compare revisions", fallback);
      })
      .catch(function () {
        show("unknown", "Couldn’t check agent-box updates.", "Check GitHub", fallback);
      });
  }

  var pollLeft = 0;
  var pollTimer = null;
  function schedulePoll() {
    if (pollTimer || pollLeft <= 0) { return; }
    if (!document.querySelector(
          "#sessions-list [data-state=starting], #tab-bar [data-state=starting]")) { return; }
    pollLeft -= 1;
    pollTimer = window.setTimeout(function () {
      pollTimer = null;
      // Keep the query string: on the workspace it carries ?tab=, so
      // the fetched tab bar marks the same tab current.
      fetchPage()
        .then(function (t) {
          if (t === null) { return; }
          applyDoc(parseHTML(t), ["sessions-list", "tab-bar"]);
          wsSync();
          schedulePoll();
        });
    }, 2500);
  }
  // The burst poll is the no-feed fallback for "starting" → "live"; with
  // the live feed attached the daemon reports that transition itself.
  // Guided sign-in (issues #207, #208, #313). While a card is
  // mid-flight its state moves without this page posting anything: the
  // CLI prints its URL a beat after start, and the sign-in itself
  // completes in another tab entirely. So re-fetch and swap the section
  // while it reports itself busy, and stop as soon as it does not —
  // there is nothing to watch on a page whose cards are all settled.
  var connectTimer = null;
  function connectBusy() {
    var el = document.getElementById("connect-list");
    return !!el && el.getAttribute("data-busy") === "1";
  }
  function connectPoll() {
    if (connectTimer || !connectBusy()) { return; }
    connectTimer = window.setTimeout(function () {
      connectTimer = null;
      if (document.hidden) { connectPoll(); return; }
      fetchPage()
        .then(function (t) { if (t !== null) { applyDoc(parseHTML(t), ["connect-list"]); } })
        .catch(function () {})
        .then(function () { connectPoll(); });
    }, 2500);
  }

  function startPolling(n) {
    if (liveFeed) { return; }
    pollLeft = n;
    schedulePoll();
  }

  // Live session feed. Sessions also change from OUTSIDE this page — the
  // agent-box-session CLI, an agent adding a helper for itself, another
  // browser tab, the supervisor bringing a listed session up — and the
  // page used to see none of that until a reload. The daemon streams a
  // fingerprint of the session state; whenever it differs from the one
  // this HTML was rendered with, re-fetch and patch (same swap the form
  // posts do). The fingerprint, not the state itself, is what travels:
  // rendering stays server-side and nothing sensitive crosses the feed.
  var liveFeed = false;
  var probeTimer = null, probeEvery = 0;
  var NO_FEED_MS = 5000;    // no stream: the fingerprint poll IS the feed
  var BACKSTOP_MS = 30000;  // stream up: slow re-check, so nothing can stick
  function liveUpdates() {
    var meta = document.querySelector('meta[name="agent-box-events"]');
    var url = meta && meta.getAttribute("content");
    if (!url) { return; }
    var seen = meta.getAttribute("data-fp") || "";
    function saw(fp) {
      if (!fp || fp === seen) { return; }
      seen = fp;
      scheduleRefresh();
    }
    // One-shot fingerprint: a small JSON reply that only costs a page
    // re-fetch when it actually moved.
    function probe() {
      fetch(url + "?poll=1", { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (s) { saw(s && s.fp); })
        .catch(function () {});
    }
    // Probing keeps running even with the stream up, just slowly: a
    // stream can die in ways neither end notices (a proxy dropping an
    // idle connection, a laptop resuming from sleep), and a workspace
    // that silently stopped updating is the bug this all fixes. Paused
    // while the tab is hidden — nothing to repaint there — with a probe
    // on the way back, which is also the resume-from-sleep catch-up.
    function setProbe(ms) {
      if (probeEvery === ms) { return; }
      probeEvery = ms;
      if (probeTimer) { window.clearInterval(probeTimer); }
      probeTimer = window.setInterval(function () {
        if (!document.hidden) { probe(); }
      }, ms);
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { probe(); }
    });
    setProbe(NO_FEED_MS);
    if (!window.EventSource) { return; }
    // One stream per page. It is another multiplexed h2 stream on any
    // real box (Caddy always serves TLS); only a plain-HTTP dev rig
    // spends a whole connection out of the browser's per-origin six on
    // it, alongside each pane's terminal WebSocket.
    var es = new EventSource(url);
    es.onopen = function () {
      liveFeed = true;
      setProbe(BACKSTOP_MS);
    };
    es.addEventListener("sessions", function (e) {
      var s = null;
      try { s = JSON.parse(e.data); } catch (err) { return; }
      saw(s && s.fp);
    });
    es.onerror = function () {
      // A dropped stream reconnects on its own (a box update restarts
      // the daemon under us, routinely) and the daemon replays the
      // current fingerprint on connect. CLOSED means it could not be
      // established at all — too many streams open, or a proxy that
      // will not stream — and per spec it never retries, so the poll
      // goes back to being the feed.
      liveFeed = false;
      if (es.readyState === EventSource.CLOSED) { setProbe(NO_FEED_MS); }
    };
  }

  // Tabbed terminal workspace (the HOME root page, issue #119). The
  // server renders tabs as plain ?tab= links and only the selected
  // pane; this upgrades clicks to client-side switching, creating
  // panes lazily on first activation and keeping them mounted after,
  // so background sessions stay attached like a terminal app's tabs.
  // Everything re-queries the DOM — polling replaces #tab-bar
  // wholesale, and a pane may be a placeholder until its session is
  // live (the ttyd attach wrapper errors out on a session that does
  // not exist yet).
  function tabBar() { return document.getElementById("tab-bar"); }
  function tabNames() {
    var bar = tabBar();
    if (!bar) { return []; }
    return [].slice.call(bar.querySelectorAll(".tab[data-tab]")).map(function (t) {
      return t.getAttribute("data-tab");
    });
  }
  function tabEl(name) {
    var bar = tabBar();
    return bar ? bar.querySelector('.tab[data-tab="' + name + '"]') : null;
  }
  function tabState(name) {
    var t = tabEl(name);
    var s = t ? t.querySelector("[data-state]") : null;
    return s ? s.getAttribute("data-state") : "";
  }
  function tabLive(name) { return tabState(name) === "live"; }
  function placeholderText(name) {
    // Mirrors render_pane: a stopped session is not coming up on its
    // own, so don't promise that it is starting.
    return tabState(name) === "stopped"
      ? name + " is stopped — Start on the settings page revives it."
      : name + " is starting…";
  }
  function paneState(name) {
    // The three states a pane is built for; data-ph records which one the
    // mounted pane belongs to, on the iframe as much as on a placeholder.
    if (tabLive(name)) { return "live"; }
    return tabState(name) === "stopped" ? "stopped" : "starting";
  }
  function ensurePane(name) {
    var cur = document.querySelector('#panes .pane[data-pane="' + name + '"]');
    // Keep a pane only while the state it was built for still holds. An
    // iframe used to be exempt from that, so it outlived the session
    // inside it: once a live session stopped (clean exit, or a stop from
    // the CLI), the pane went on showing a terminal wired to a tmux
    // session that no longer existed — the attach wrapper's "no session
    // named X" dead end — and starting it again never swapped in a fresh
    // iframe, which is what made a working Start look broken (issue #241).
    var want = paneState(name);
    // Panes rendered before this stamp existed are server-rendered iframes.
    if (cur && (cur.getAttribute("data-ph") || "live") === want) { return cur; }
    var el;
    if (tabLive(name)) {
      el = document.createElement("iframe");
      // data-term-base is this user's own path with its trailing slash;
      // a session hangs off it as a path segment, not a query.
      el.src = tabBar().getAttribute("data-term-base") +
               encodeURIComponent(name) + "/";
      el.title = name + " terminal";
      el.setAttribute("allow", "clipboard-read; clipboard-write");
      el.className = "pane";
    } else {
      el = document.createElement("div");
      el.textContent = placeholderText(name);
      el.className = "pane placeholder";
    }
    el.setAttribute("data-ph", want);
    el.setAttribute("data-pane", name);
    if (cur) {
      if (cur.classList.contains("active")) { el.classList.add("active"); }
      cur.replaceWith(el);
    } else {
      document.getElementById("panes").appendChild(el);
    }
    return el;
  }
  function wsSelect(name, focus) {
    var bar = tabBar();
    if (!bar || !tabEl(name)) { return; }
    bar.querySelectorAll(".tab").forEach(function (t) {
      if (t.getAttribute("data-tab") === name) { t.setAttribute("aria-current", "page"); }
      else { t.removeAttribute("aria-current"); }
    });
    var pane = ensurePane(name);
    document.querySelectorAll("#panes .pane").forEach(function (p) {
      p.classList.toggle("active", p === pane);
    });
    history.replaceState(null, "", bar.getAttribute("data-term-base") +
                         "?tab=" + encodeURIComponent(name));
    if (focus && pane.tagName === "IFRAME") {
      try { pane.contentWindow.focus(); } catch (err) { /* cross-origin never happens; be safe */ }
    }
  }
  function wsActive() {
    var bar = tabBar();
    var t = bar ? bar.querySelector(".tab[aria-current]") : null;
    return t ? t.getAttribute("data-tab") : null;
  }
  function wsSync() {
    if (!tabBar()) { return; }
    // Drop panes whose sessions are gone; upgrade placeholders whose
    // sessions came live. No focus steal — the user may be typing.
    document.querySelectorAll("#panes .pane[data-pane]").forEach(function (p) {
      var name = p.getAttribute("data-pane");
      if (!tabEl(name)) { p.remove(); return; }
      ensurePane(name);
    });
    var cur = wsActive();
    if (cur) { wsSelect(cur, false); }
  }
  document.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target.closest("#tab-bar .tab[data-tab]") : null;
    if (!t) { return; }
    e.preventDefault();
    wsSelect(t.getAttribute("data-tab"), true);
  });

  // Two-click close on the tab bar's x. Closing kills a live agent and
  // the button sits a few pixels from the session name, so the first
  // click only ARMS it (red "Close?" pill, see .tab-x.arm) and the
  // second lets the form submit for real — a confirm() dialog would be
  // both heavier and skipped entirely when scripting is off, whereas
  // here no-JS simply keeps the plain one-click POST.
  //
  // Anything that could leave a loaded gun on screen disarms: a
  // timeout, Escape, tabbing away, arming another tab, or any click
  // elsewhere. A tab-bar re-render (polling swaps #tab-bar wholesale)
  // detaches the button, which the isConnected check absorbs.
  var ARM_MS = 4000;   // long enough to read the pill and aim at it
  var SETTLE_MS = 350; // a double-click is not a considered confirmation
  var armedX = null;
  var armedAt = 0;
  var armTimer = null;
  function wrapOf(b) { return b.closest ? b.closest(".tab-wrap") : null; }
  function clearArm() {
    if (armTimer) { window.clearTimeout(armTimer); armTimer = null; }
    var b = armedX;
    armedX = null;
    return b;
  }
  function disarmX() {
    var b = clearArm();
    if (!b || !b.isConnected) { return; }
    var label = "Close " + b.getAttribute("data-close");
    var w = wrapOf(b);
    if (w) { w.classList.remove("arm"); }
    b.classList.remove("arm");
    b.textContent = "×";
    b.setAttribute("aria-label", label);
    b.setAttribute("title", label);
  }
  function armX(b) {
    disarmX();
    var hint = "Click again to close " + b.getAttribute("data-close");
    var w = wrapOf(b);
    armedX = b;
    armedAt = Date.now();
    if (w) { w.classList.add("arm"); }
    b.classList.add("arm");
    b.textContent = "Close?";
    b.setAttribute("aria-label", hint);
    b.setAttribute("title", hint);
    armTimer = window.setTimeout(disarmX, ARM_MS);
  }
  document.addEventListener("click", function (e) {
    var x = e.target && e.target.closest ? e.target.closest("#tab-bar .tab-x") : null;
    // Second click on the armed button: fall through with no
    // preventDefault so the form submits (the submit handler below
    // upgrades it to fetch + patch, like every other form here). Only
    // the timer is dropped — the pill stays red until the patched tab
    // bar comes back, so the click has visible effect in flight.
    // Too soon after arming it is the tail of a double-click, not a
    // decision: swallow it and stay armed.
    if (x && x === armedX) {
      if (Date.now() - armedAt < SETTLE_MS) { e.preventDefault(); return; }
      clearArm();
      return;
    }
    disarmX();
    if (!x) { return; }
    e.preventDefault();
    armX(x);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { disarmX(); }
  });
  document.addEventListener("focusout", function (e) {
    if (armedX && e.target === armedX) { disarmX(); }
  });

  // Dismissing the feedback banner ("Session added", "Key saved"…),
  // issue #246. Its x is a link back to the same page without the ?ok=
  // that raised it, which is how a scriptless browser dismisses; here
  // the click is intercepted so the banner just goes away — navigating
  // would reload the workspace and tear down every attached terminal.
  // The URL is rewritten to that same ok-less address, so a later
  // reload does not bring the dismissed banner back.
  document.addEventListener("click", function (e) {
    var x = e.target && e.target.closest ? e.target.closest(".msg-x") : null;
    if (!x) { return; }
    e.preventDefault();
    var msg = x.closest(".msg");
    if (msg) { msg.remove(); }
    try { history.replaceState(null, "", x.getAttribute("href")); } catch (err) { /* opaque origin */ }
  });

  // The editors render expanded (no-JS fallback); collapse them once
  // JS is live so the page opens in list-only, GitHub-style form.
  ["secret-editor", "session-editor", "password-editor"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) { el.hidden = true; }
  });

  document.addEventListener("click", function (e) {
    var t = e.target && e.target.closest ? e.target.closest("[data-toggle],[data-edit]") : null;
    if (!t) { return; }
    var form = document.getElementById("secret-form");
    if (t.hasAttribute("data-edit")) {
      document.getElementById("secret-editor").hidden = false;
      form.reset();
      var key = form.querySelector("input[name=key]");
      key.value = t.getAttribute("data-edit");
      key.readOnly = true;
      form.querySelector("input[name=value]").focus();
      return;
    }
    var el = document.getElementById(t.getAttribute("data-toggle"));
    if (!el) { return; }
    el.hidden = !el.hidden;
    if (!el.hidden && el.id === "secret-editor") {
      form.reset();
      var ki = form.querySelector("input[name=key]");
      ki.readOnly = false;
      ki.focus();
    }
  });

  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f || (f.method || "").toLowerCase() !== "post") { return; }
    // Password rotation invalidates the current cookie and cached basic
    // credentials. Let the browser follow its native 303/401 flow so it
    // can prompt for the new password; fetch() suppresses that UX.
    if (f.hasAttribute("data-native")) { return; }
    e.preventDefault();
    var body = new URLSearchParams();
    new FormData(f).forEach(function (v, k) { body.append(k, v); });
    // On the workspace, adding a session should focus its new tab. The
    // name is auto-derived by the daemon, so the page only learns it
    // from the answer: a successful add redirects to ?tab=<new name>,
    // which the fetched page marks current. Snapshot the tabs first and
    // trust that selection only when it names a tab that did not exist
    // before — a FAILED add re-renders with the default tab current, and
    // must not yank the user off the tab they were on.
    var tabsBefore =
      (f.getAttribute("action") || "").endsWith("/sessions/add") && tabBar()
        ? tabNames() : null;
    var wasActive = wsActive();
    var poll = f.getAttribute("data-poll");
    var statusUrl = f.getAttribute("data-status");

    function afterPost(t) {
      applyDoc(parseHTML(t),
        ["msg-slot", "secrets-list", "sessions-list", "webhooks-list",
         "connect-list", "tab-bar"]);
      var ed = f.closest(".editor");
      if (ed) { f.reset(); ed.hidden = true; }
      var added = wsActive();   // the tab the fetched page marks current
      connectPoll();
      if (tabsBefore && added && tabsBefore.indexOf(added) < 0) { wsSelect(added, true); }
      else if (wasActive && tabEl(wasActive)) { wsSelect(wasActive, false); }
      wsSync();
    }
    function post() {
      return fetch(f.getAttribute("action"), { method: "POST", body: body })
        .then(function (r) { return r.text(); });
    }

    // The two Danger-zone actions get a long-polled progress line;
    // everything else keeps the brief session-state poll.
    if (poll === "update" && statusUrl) {
      // Snapshot the run's start time + rev BEFORE triggering, so the
      // watcher can distinguish the new run from any earlier one.
      fetchStatus(statusUrl).then(function (s0) {
        var baseline = (s0 && s0.update && typeof s0.update.since === "number")
          ? s0.update.since : 0;
        var rev0 = s0 ? s0.rev : null;
        post().then(function (t) { afterPost(t); watchUpdate(statusUrl, baseline, rev0); });
      });
      return;
    }
    if (poll === "restart" && statusUrl) {
      post().then(function (t) { afterPost(t); watchRestart(statusUrl); });
      return;
    }
    post().then(function (t) { afterPost(t); startPolling(8); });
  });

  // Working-directory autocomplete (issue #131). The add-session cwd
  // field browses the filesystem one level at a time: the daemon lists
  // the children of whatever directory the text names so far (up to
  // the last "/"), and the client filters those by the trailing
  // fragment. Picking an entry appends "<name>/" and re-fetches, so
  // the next level appears — like tab-completing a path. Everything is
  // event-delegated so it survives the DOM swaps applyDoc() does; each
  // input carries its own tiny state on the element.
  function acList(input) {
    var combo = input.closest ? input.closest(".combo") : null;
    return combo ? combo.querySelector(".ac") : null;
  }
  function acSplit(v) {
    // Directory portion (browsed) and trailing fragment (filter).
    var slash = v.lastIndexOf("/");
    if (slash < 0) { return { dir: "~", frag: v === "~" ? "" : v }; }
    return { dir: v.slice(0, slash) || "/", frag: v.slice(slash + 1) };
  }
  function acJoin(dir, name) {
    return (dir === "/" ? "/" : dir + "/") + name;
  }
  function acClose(input) {
    var ul = acList(input);
    if (ul) { ul.hidden = true; ul.innerHTML = ""; }
    var st = input._dir;
    if (st) { st.active = -1; }
  }
  function acRender(input) {
    var ul = acList(input);
    var st = input._dir;
    if (!ul || !st) { return; }
    var frag = acSplit(input.value).frag.toLowerCase();
    var matches = st.entries.filter(function (n) {
      return n.toLowerCase().indexOf(frag) === 0;
    });
    ul.innerHTML = "";
    st.active = -1;
    if (!st.entries.length) {
      var e = document.createElement("li");
      e.className = "empty";
      e.textContent = "No subfolders here";
      ul.appendChild(e);
      ul.hidden = false;
      return;
    }
    if (!matches.length) { ul.hidden = true; return; }
    matches.slice(0, 200).forEach(function (name) {
      var li = document.createElement("li");
      li.setAttribute("role", "option");
      li.setAttribute("data-name", name);
      li.textContent = name + "/";
      ul.appendChild(li);
    });
    ul.hidden = false;
  }
  function acFetch(input) {
    var st = input._dir || (input._dir = { dir: null, entries: [], active: -1, seq: 0 });
    var dir = acSplit(input.value).dir;
    if (dir === st.dir) { acRender(input); return; }
    var base = input.getAttribute("data-dir-base") || "";
    var my = ++st.seq;
    fetch(base + "/sessions/dirs?path=" + encodeURIComponent(dir), {
      headers: { "Accept": "application/json" }
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (my !== st.seq) { return; } // a newer keystroke won
        st.dir = dir;
        st.entries = (res && res.dirs) || [];
        acRender(input);
      })
      .catch(function () { acClose(input); });
  }
  function acItems(input) {
    var ul = acList(input);
    return ul ? [].slice.call(ul.querySelectorAll("li[data-name]")) : [];
  }
  function acHighlight(input, idx) {
    var items = acItems(input);
    var st = input._dir;
    if (!items.length || !st) { return; }
    if (idx < 0) { idx = items.length - 1; }
    if (idx >= items.length) { idx = 0; }
    items.forEach(function (li, i) {
      if (i === idx) { li.setAttribute("aria-selected", "true"); li.scrollIntoView({ block: "nearest" }); }
      else { li.removeAttribute("aria-selected"); }
    });
    st.active = idx;
  }
  function acApply(input, li) {
    var dir = acSplit(input.value).dir;
    input.value = acJoin(dir, li.getAttribute("data-name")) + "/";
    input.focus();
    acFetch(input); // reveal the next level
  }
  var acTimer = null;
  document.addEventListener("input", function (e) {
    var input = e.target;
    if (!input || !input.hasAttribute || !input.hasAttribute("data-dir-input")) { return; }
    if (acTimer) { window.clearTimeout(acTimer); }
    acTimer = window.setTimeout(function () { acTimer = null; acFetch(input); }, 120);
  });
  document.addEventListener("focusin", function (e) {
    var input = e.target;
    if (input && input.hasAttribute && input.hasAttribute("data-dir-input")) {
      // The field starts on the untouched default "~": select it so the
      // first keystroke replaces it instead of appending, which is what
      // produced an invalid "~docu" (issue #308).
      if (input.value === "~") { input.select(); }
      acFetch(input);
    }
  });
  document.addEventListener("focusout", function (e) {
    var input = e.target;
    if (!input || !input.hasAttribute || !input.hasAttribute("data-dir-input")) { return; }
    // Delay so a mousedown-selected item still registers its click.
    window.setTimeout(function () { acClose(input); }, 150);
  });
  document.addEventListener("keydown", function (e) {
    var input = e.target;
    if (!input || !input.hasAttribute || !input.hasAttribute("data-dir-input")) { return; }
    var ul = acList(input);
    var open = ul && !ul.hidden;
    var st = input._dir;
    if (e.key === "ArrowDown") { e.preventDefault(); if (open) { acHighlight(input, (st ? st.active : -1) + 1); } else { acFetch(input); } }
    else if (e.key === "ArrowUp") { if (open) { e.preventDefault(); acHighlight(input, (st ? st.active : 0) - 1); } }
    else if (e.key === "Enter") {
      var items = acItems(input);
      if (open && st && st.active >= 0 && items[st.active]) {
        e.preventDefault(); // accept the suggestion, don't submit yet
        acApply(input, items[st.active]);
      }
    } else if (e.key === "Escape") { if (open) { e.preventDefault(); acClose(input); } }
  });
  document.addEventListener("mousedown", function (e) {
    var li = e.target && e.target.closest ? e.target.closest(".ac li[data-name]") : null;
    if (!li) { return; }
    e.preventDefault(); // keep focus on the input (no focusout close)
    var combo = li.closest(".combo");
    var input = combo ? combo.querySelector("[data-dir-input]") : null;
    if (input) { acApply(input, li); }
  });

  checkForUpdate();
  liveUpdates();
  connectPoll();
  // Land in the terminal: focus the server-selected tab's pane.
  if (wsActive()) { wsSelect(wsActive(), true); }
  // Still armed for the moment before the feed reports itself open; it
  // no-ops from then on.
  startPolling(8);
})();
