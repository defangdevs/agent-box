# VM test for the browser surface over one agent-box user's sessions: the
# tabbed terminal workspace at /<user>/ (the settings daemon in
# AGENT_BOX_HOME mode; issue 119) — one tab per session, panes iframing each
# session's own path, server-side ?tab= selection — the vhost root
# redirecting into it, and its /sessions/* CRUD routes, all behind auth.
# Nothing on the vhost is served unauthenticated (the old picker and its
# public sessions.json are gone). Exercises:
#   - session CRUD on the settings page too (back=settings redirects there),
#   - what the surface says about a session that is DOWN (issue 241),
#   - a name the vhost already owns, and a dispatch-shaped long name that
#     must render as a tab rather than be dropped (issue #236),
#   - the working-directory picker and add-with-cwd (issue 131), and the
#     names both creation paths mint for a second session there (#277),
#   - the live session feed both pages follow (Server-Sent Events through
#     Caddy, plus the one-shot fingerprint its no-stream fallback polls),
#   - a session's transcript downloading from its row (issue #248), and
#     following a /clear rotation (issue #223),
#   - ttyd running with --url-arg, which is what /<user>/<session>/ is
#     rewritten onto (this test swaps the Caddyfile, so the rewrite itself is
#     the session-route eval check's job; here it is the LINKS that matter).
#
# The supervisor and the session CLI underneath are tests/sessions.nix, and
# the box both drive is tests/sessions-common.nix (issue #312). The Caddyfile
# the shared node lib.mkForce-swaps in is a minimal `tls internal` one (no
# ACME in the sandbox) that keeps the same routing shape: /agent/settings*
# and the root catch-all both inside the auth gate, proxied to the settings
# daemon's unix socket.
{ agent-box }:
let
  common = import ./sessions-common.nix { inherit agent-box; };
in
{
  name = "agent-box-sessions-web";
  node.pkgsReadOnly = false;

  nodes.machine = common.machineNode;
  nodes.client = common.clientNode;

  testScript = common.prelude + ''
    start_all()
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_for_unit("agent-box-settings@agent.service")
    machine.wait_for_unit("caddy.service")
    client.wait_for_unit("multi-user.target")

    # Every page below is a page ABOUT sessions, so the seeded one has to be
    # up before any of them says anything. That it gets seeded at all, and
    # what from, is tests/sessions.nix's assertion.
    machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=120)

    machine_ip = machine.succeed("ip -4 -o addr show eth1 | head -1").split()[3].split("/")[0]
    curl = f"curl -sk --resolve box.test:443:{machine_ip}"

    # NOTHING on the vhost is public anymore: the root session manager
    # challenges for auth, and the old public sessions.json is gone (its
    # path now falls into the auth-gated catch-all).
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' https://box.test/ | grep -x 401 >/dev/null"
    )
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/sessions.json | grep -x 401 >/dev/null"
    )

    # The vhost root picks a user and lands in their space: with one
    # terminal user there is nothing to pick, so it redirects — carrying the
    # query, so a /?tab=<session> bookmark from before this scheme still
    # opens on its tab.
    redirect = client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}} %{{redirect_url}}'"
        " https://box.test/"
    ).split()
    assert redirect[0] == "303", redirect
    assert redirect[1] == "https://box.test/agent/", redirect
    tab_redirect = client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{redirect_url}}'"
        " 'https://box.test/?tab=main'"
    ).strip()
    assert tab_redirect == "https://box.test/agent/?tab=main", tab_redirect

    # The root page (behind auth) is the tabbed terminal workspace (issue
    # 119): a tab per session, the selected (live) session's pane iframing
    # its ttyd deep link. Never a session's argv/cwd/env (may hold secrets).
    root_page = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/"
    )
    assert 'id="tab-bar"' in root_page, root_page
    assert 'data-tab="main" href="/agent/?tab=main" aria-current="page"' in root_page, root_page
    assert 'src="/agent/main/"' in root_page, root_page
    assert "workingDirectory" not in root_page, root_page

    # The two controls at the end of the tab bar, (i) and the gear, are
    # inline SVG rather than text glyphs (PR #448). U+2699 GEAR carries
    # Emoji_Presentation, so iOS and Android drew it from their COLOUR
    # emoji font: a shaded 3D gear beside a flat monochrome (i), bigger
    # than it and in the wrong palette. Assert the code points are ABSENT
    # (entity spellings included — the old markup emitted `&#9881;`, so a
    # test looking only for the literal character would have passed), not
    # merely that an <svg> is there: the glyph renders acceptably on the
    # desktop a contributor checks the page from, so nothing but the
    # absence of the glyph keeps the regression out.
    for control in ('class="hint"', 'class="gear"'):
        at = root_page.index(control)
        assert "<svg" in root_page[at:at + 400], root_page[at:at + 400]
    for glyph in ("⚙", "&#9881;", "&#x2699;", "ⓘ", "&#9432;", "&#x24d8;"):
        assert glyph not in root_page, glyph

    # The add/delete banner is page-level feedback, so it renders ABOVE the
    # tab bar (issue 188) — below the tabs it read as a message from the
    # terminal in the pane underneath it. An empty slot when there is no
    # message, so the tabs stay flush with the top of the viewport.
    assert '<div id="msg-slot"></div>' in root_page, root_page
    ok_page = client.succeed(
        f"{curl} -u agent:testpassword 'https://box.test/agent/?ok=session_added'"
    )
    assert "Session added" in ok_page, ok_page
    assert ok_page.index('class="msg"') < ok_page.index('id="tab-bar"'), ok_page

    # The banner is dismissible (issue 246). Its x is a LINK back to the
    # same page without the ?ok=, so a scriptless browser can clear it too
    # (with JS the click only removes the element — navigating would reload
    # the workspace and tear down every attached terminal). Dismissing on
    # the workspace keeps the selected tab; on the settings page it goes
    # back to that page's own address.
    assert '<a class="msg-x" href="/agent/?tab=main" aria-label="Dismiss"' in ok_page, ok_page
    ok_settings = client.succeed(
        f"{curl} -u agent:testpassword 'https://box.test/agent/settings/?ok=saved'"
    )
    assert '<a class="msg-x" href="/agent/settings/" aria-label="Dismiss"' in ok_settings, ok_settings

    # Each tab carries a close (x) posting to the same /sessions/delete
    # route the settings page uses — a sibling form, since a <form> inside
    # the tab <a> would be invalid markup. The two-click arming that keeps
    # a stray click from killing a session is client-side (tests/e2e).
    assert '<form class="tab-close" method="post" action="/sessions/delete">' in root_page, root_page
    assert 'class="tab-x" data-close="main" aria-label="Close main"' in root_page, root_page

    # The root page's CRUD routes (behind auth) can add a session; the
    # workspace redirect lands on the new session's tab. The name is always
    # auto-derived from the agent (there is no name field in the form): with
    # no session literally named "claude" yet, the first claude add lands on
    # the bare-agent-name tab — on /<user>/, which is where the tab bar lives.
    # Assert the raw Location header (h2 lowercases it, CRLF line ends — no
    # grep -x): what the daemon EMITS is the contract, curl's
    # %{redirect_url} resolution is not. Any submitted "name" field is
    # ignored, so passing a bogus one changes nothing.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -D - "
        "-d 'name=ignored&agent=claude' "
        "https://box.test/sessions/add "
        "| grep -i '^location: /agent/?ok=session_added&tab=claude'"
    )
    machine.wait_until_succeeds(tmux("has-session -t =claude"), timeout=60)
    machine.succeed(
        "jq -e '.sessions.claude.agent == \"claude\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # ?tab= selects a tab server-side (the no-JS switching path): the new
    # tab is current and its live pane iframes its ttyd URL.
    tab_page = client.succeed(
        f"{curl} -u agent:testpassword 'https://box.test/agent/?tab=claude'"
    )
    assert 'data-tab="claude" href="/agent/?tab=claude" aria-current="page"' in tab_page, tab_page
    assert 'src="/agent/claude/"' in tab_page, tab_page
    # main is still a tab, just not the current one.
    assert 'data-tab="main" href="/agent/?tab=main" title="main">' in tab_page, tab_page

    with subtest("a name the vhost already owns is refused"):
        # /agent/settings/ is a page, so a session called "settings" could
        # never be routed to a terminal — the CLI refuses the name outright.
        for name in ["settings", "downloads", "webhook", "sessions", "token", "ws"]:
            machine.fail(as_agent(f"agent-box-session add {name} --agent shell"))
        machine.succeed(
            "jq -e '.sessions | has(\"settings\") | not'"
            " /home/agent/.config/agent-box/sessions.json"
        )
        # And auto-naming cannot mint one either. The name it derives is the
        # working directory's own basename, and that branch is only reached
        # once the agent's own bare name is taken — so the FIRST session in
        # ~/ws is "shell" and the second is the one that would have been
        # called "ws".
        machine.succeed(as_agent("mkdir -p /home/agent/ws"))
        for _ in range(2):
            machine.succeed(
                as_agent("agent-box-session add --cwd /home/agent/ws --agent shell")
            )
        machine.succeed(
            "jq -e '.sessions | has(\"ws\") | not'"
            " /home/agent/.config/agent-box/sessions.json"
        )
        minted = machine.succeed(
            "jq -r '.sessions | to_entries[]"
            " | select(.value.workingDirectory == \"/home/agent/ws\") | .key'"
            " /home/agent/.config/agent-box/sessions.json"
        ).split()
        assert len(minted) == 2 and "ws" not in minted, minted
        for name in minted:
            machine.succeed(as_agent(f"agent-box-session rm {name}"))

    # A dispatch-shaped long name renders as a tab (issue #236). The daemon
    # used to bound a name at 32 and DROP the rest, so hook-<owner/repo>-<hex>
    # for any repo over 23 characters had no tab, no close button and no
    # subscriptions row while the session ran and owned its topic. Names are
    # never shortened to fit — two repos sharing a prefix would collapse onto
    # one name — so it is the LABEL that gives: its own span, ellipsized in
    # CSS, with the full name as the tab's tooltip.
    long_name = "hook-defangdevs-local-channels-de2d"
    machine.succeed(
        f"su -s /bin/sh agent -c 'agent-box-session add {long_name} --agent shell'"
    )
    long_page = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/"
    )
    assert f'data-tab="{long_name}" href="/agent/?tab={long_name}"' in long_page, long_page
    assert f'title="{long_name}"' in long_page, long_page
    assert f'<span class="tab-name">{long_name}</span>' in long_page, long_page
    assert f'class="tab-x" data-close="{long_name}"' in long_page, long_page
    machine.succeed(f"su -s /bin/sh agent -c 'agent-box-session rm {long_name}'")

    # Delete it (delist + kill) so "claude" is free again for later subtests.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'name=claude' "
        "https://box.test/sessions/delete | grep -x 303 >/dev/null"
    )
    settle()
    machine.fail(tmux("has-session -t =claude"))

    # The add-session form carries an optional kickoff prompt through to
    # initialPrompt (first-spawn only, cleared on resume like the CLI). The
    # name is auto-derived and "claude" is free again, so assert on that key.
    #
    # NOT read straight out of sessions.json after the POST: the supervisor
    # spawns the session within ~2s and CONSUMES the prompt on that first
    # spawn, so that read raced its own subject and only passed on a slow tick
    # (issue #285). What the route stored is instead asserted on the two states
    # that outlive the window — the prompt on the spawned pane's command line,
    # and the consumed entry — each waited for.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'agent=claude' --data-urlencode 'prompt=hello there' "
        "https://box.test/sessions/add | grep -x 303 >/dev/null"
    )
    machine.wait_until_succeeds(tmux("has-session -t =claude"), timeout=60)
    prompt_start_cmd = machine.wait_until_succeeds(
        tmux('list-panes -t "=claude" -F "#{pane_start_command}"'), timeout=60
    )
    # printf %q on the way in, tmux escaping on the way out: compare with the
    # backslashes dropped rather than guessing how many layers survive.
    assert "hello there" in prompt_start_cmd.replace("\\", ""), prompt_start_cmd
    machine.wait_until_succeeds(
        "jq -e '.sessions.claude.hasRun == true "
        "and .sessions.claude.initialPrompt == null' "
        "/home/agent/.config/agent-box/sessions.json",
        timeout=60,
    )
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'name=claude' https://box.test/sessions/delete | grep -x 303 >/dev/null"
    )
    settle()
    machine.fail(tmux("has-session -t =claude"))

    # Session CRUD is rejected without credentials.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "-d 'agent=claude' "
        "https://box.test/sessions/add | grep -x 401 >/dev/null"
    )

    # The session CRUD routes stay at the root for the primary user (the
    # old settings-path routes remain gone)...
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'agent=claude' "
        "https://box.test/agent/settings/sessions/add | grep -x 404 >/dev/null"
    )
    # ...but the settings page renders the session manager again (the root
    # page is the workspace now, issue 119), with back=settings so its forms
    # redirect back to the settings page rather than to the workspace.
    settings_page = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/settings/"
    )
    assert "Add session" in settings_page, settings_page
    assert 'name="back" value="settings"' in settings_page, settings_page
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -D - "
        "-d 'name=main&back=settings' "
        "https://box.test/sessions/restart "
        "| grep -i '^location: /agent/settings/?ok=session_restarted'"
    )
    # (that restart killed main; the supervisor brings it back)
    machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=60)

    # --- what the web surface says about a session that is DOWN (issue 241)
    # A stopped session is listed and reachable from three places, and all
    # three used to describe it as if it were merely late: the row offered
    # "Restart" behind a prompt about losing work that is not running, and
    # the row's terminal link (like the workspace pane, and like any tab
    # opened before the session stopped) landed on the attach wrapper's dead
    # end, which told the operator to CREATE a session that already exists.
    with subtest("a stopped session is described as stopped, not as missing"):
        machine.succeed(as_agent("agent-box-session stop main"))
        machine.wait_until_fails(tmux("has-session -t =main"), timeout=60)

        # The row: Start, and no "unsaved work is lost" confirm on the form
        # that carries it (a live session keeps both).
        stopped_page = client.succeed(
            f"{curl} -u agent:testpassword https://box.test/agent/settings/"
        )
        # Just this row's own markup: from its terminal link to the delete
        # form that closes it (the fold below a row carries forms of its own).
        row = stopped_page[stopped_page.index('href="/agent/main/"'):]
        row = row[:row.index("/sessions/delete")]
        assert 'data-state="stopped"' in row, row
        assert ">Start</button>" in row, row
        assert "confirm(" not in row, row

        # The workspace pane says the same thing, and stamps the state it
        # was built for so the page can tell a pane that has gone stale.
        ws = client.succeed(
            f"{curl} -u agent:testpassword 'https://box.test/agent/?tab=main'"
        )
        assert 'data-ph="stopped"' in ws, ws
        assert "main is stopped" in ws, ws
        assert 'src="/agent/main/"' not in ws, ws
        # ...and the pane offers the start itself. Both stopped surfaces used
        # to end in "go to the settings page": a detour to press a button
        # that changes the pane the operator was already looking at, and one
        # that ends with a reload, because a Start pressed elsewhere does not
        # reconnect a terminal. The pane's form posts the same route the
        # session row posts to, with no back= field, so it comes back here.
        pane = ws[ws.index('data-ph="stopped"'):]
        pane = pane[:pane.index("</div>")]
        assert "/sessions/restart" in pane, pane
        assert ">Start</button>" in pane, pane
        assert "settings page" not in pane, pane
        # back=workspace, spelled out: /<user>/ is a workspace for EVERY
        # user, while the route's default destination is the settings page
        # for anyone but the primary one, so a pane's Start would have taken
        # a second web user away from the pane it belongs to.
        assert 'name="back" value="workspace"' in pane, pane

        # The terminal dead end names the verb that actually revives it.
        # Print the wrapper's path (see the grep -oE note below) — running
        # the substitution as the command would run the WRAPPER instead.
        attach = machine.succeed(
            "{ systemctl show agent-web-terminal@agent --property=ExecStart "
            "--value | grep -oE '/nix/store/[^ ]*/bin/agent-box-attach( |$)' "
            "|| echo /missing; } | sed 's/ $//' | head -n1"
        ).strip()
        assert attach != "/missing", attach

        def dead_end(name):
            return machine.succeed(as_agent(
                "env TMUX_TMPDIR=/run/agent-box-agent "
                f"AGENT_BOX_SESSIONS_FILE={sfile} "
                f"{attach} {name} || true"
            ))
        down = dead_end("main")
        assert "agent-box-session restart main" in down, down
        assert "agent-box-session add main" not in down, down
        # ...and it does NOT offer the keypress here. The wrapper only offers
        # it on a pty, because this driver's own backdoor shell runs on
        # /dev/hvc0 — a tty with nobody behind it — and `su -c` passes that
        # down: gated on [ -t 0 ], the offer's wait-for-a-keypress loop hung
        # this call until CI timed out (25 minutes, run 33421803747). This
        # assertion is that gate's regression test; the pty case is its own
        # subtest below.
        assert "Press Enter" not in down, down
        # ...and a name the box really has never heard of still says add.
        gone = dead_end("ghost")
        assert "agent-box-session add ghost" in gone, gone

        # Start it again from the same route the row posts to: the answer
        # names what was done, and the supervisor brings the session back.
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -D - "
            "-d 'name=main&back=settings' "
            "https://box.test/sessions/restart "
            "| grep -i '^location: /agent/settings/?ok=session_started'"
        )
        machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=60)
        live_ws = client.succeed(
            f"{curl} -u agent:testpassword 'https://box.test/agent/?tab=main'"
        )
        assert 'data-ph="live"' in live_ws, live_ws

    # The terminal pane is the other surface that could only send the
    # operator elsewhere, and it is the one they are looking at when they
    # find out the session is down. Given a terminal on the other end it
    # offers the start on a keypress, and attaches to what it started.
    #
    # On its OWN session, not on main: the keypress reaches the pane that is
    # attached, and a shell session is a pane where that costs nothing —
    # main runs the real claude, whose TUI would be the thing answering a
    # keystroke from a test. `script` supplies the pty the wrapper checks
    # for (with nothing interactive on the other end it keeps the printed
    # advice dead_end() reads above), and a bare newline is the keypress.
    # Bounded by `timeout`, because past the offer this wrapper is meant not
    # to return: it BECOMES the terminal.
    with subtest("a stopped session starts from its own pane"):
        machine.succeed(as_agent("agent-box-session add pane --agent shell"))
        machine.wait_until_succeeds(tmux("has-session -t =pane"), timeout=60)
        machine.succeed(as_agent("agent-box-session stop pane"))
        machine.wait_until_fails(tmux("has-session -t =pane"), timeout=60)

        # What the pane's own button posts: back=workspace lands on the
        # workspace — for the primary user and for any other — and the
        # started session's own tab is the one that comes up, because
        # dropping the operator on some other tab hides the thing they asked
        # for. Raw Location header, as above: what the daemon emits is the
        # contract.
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -D - "
            "-d 'name=pane&back=workspace' "
            "https://box.test/sessions/restart "
            "| grep -i '^location: /agent/?ok=session_started&tab=pane'"
        )
        machine.wait_until_succeeds(tmux("has-session -t =pane"), timeout=60)
        machine.succeed(as_agent("agent-box-session stop pane"))
        machine.wait_until_fails(tmux("has-session -t =pane"), timeout=60)

        offered = machine.succeed(as_agent(
            "printf '\\n' | timeout 20 script -q -c "
            "'env TMUX_TMPDIR=/run/agent-box-agent "
            f"AGENT_BOX_SESSIONS_FILE={sfile} {attach} pane' /dev/null || true"
        ))
        assert "Press Enter to start it" in offered, offered
        machine.wait_until_succeeds(tmux("has-session -t =pane"), timeout=60)
        # The stopped flag is what kept the supervisor away, so the start had
        # to clear it — through the session CLI, which this wrapper reaches
        # by the store path its own generated wrapper pins (the web-terminal
        # unit's PATH is tmux, jq, coreutils and the wrapper itself).
        machine.succeed(
            f"jq -e '.sessions.pane | has(\"stopped\") | not' {sfile} >/dev/null"
        )
        machine.succeed(as_agent("agent-box-session rm pane"))
        settle()

    # ttyd serves per-session deep links: the unit runs with --url-arg.
    machine.succeed("systemctl cat agent-web-terminal@agent | grep -- --url-arg >/dev/null")
    # The attach script is the shared agent-box-attach since issue #154
    # Phase 2. `grep -o ... || echo missing`: an empty substitution would
    # leave `grep -q` reading stdin — the backdoor shell then hangs the whole
    # test until the CI timeout (exactly how the rename was first caught).
    # The pattern must end at a token boundary and name /bin/: Phase 3 made
    # attachScript a writeShellScriptBin, so the store path CONTAINS
    # "-agent-box-attach" as a directory as well. A pattern that can stop
    # short yields a path that does not exist while grep still exits 0, so
    # the `|| echo /missing` guard never fires and the failure surfaces
    # later as a confusing "no such file".
    machine.succeed(
        "grep -q -- '-T hyperlinks' "
        "$({ systemctl show agent-web-terminal@agent --property=ExecStart --value "
        "| grep -oE '/nix/store/[^ ]*/bin/agent-box-attach( |$)' "
        "|| echo /missing; } | sed 's/ $//' | head -n1)"
    )

    # Working-directory picker (issue 131): the add-session form browses the
    # user's home one directory level at a time via a read-only JSON endpoint,
    # and a session can be anchored in a chosen directory.
    with subtest("session working-directory picker + add-with-cwd"):
        machine.succeed("su -s /bin/sh agent -c 'mkdir -p /home/agent/work/repo'")

        # "~" lists home's immediate children (including the fresh work/), a
        # deeper path lists that directory's children, and both report ok.
        dirs = client.succeed(
            f"{curl} -u agent:testpassword 'https://box.test/sessions/dirs?path=~'"
        )
        assert '"ok": true' in dirs, dirs
        assert '"work"' in dirs, dirs
        sub = client.succeed(
            f"{curl} -u agent:testpassword 'https://box.test/sessions/dirs?path=~/work'"
        )
        assert '"repo"' in sub, sub

        # The listing is confined to $HOME: a ../ climb-out and an absolute
        # path outside home are both refused (ok:false, no entries leaked).
        for bad in ["~/../../etc", "/etc"]:
            escaped = client.succeed(
                f"{curl} -u agent:testpassword 'https://box.test/sessions/dirs?path={bad}'"
            )
            assert '"ok": false' in escaped, escaped
            assert '"dirs": []' in escaped, escaped

        # A non-existent directory, or one outside $HOME, is a 400 (tmux -c
        # would fail on a missing cwd) and no session is created. "claude"
        # was deleted above, so a rejected add must leave it absent.
        for bad in ["~/nope", "/etc"]:
            client.succeed(
                f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
                f"-d 'agent=claude&cwd={bad}' "
                "https://box.test/sessions/add | grep -x 400 >/dev/null"
            )
        machine.succeed(
            "jq -e '(.sessions.claude // null) == null' "
            "/home/agent/.config/agent-box/sessions.json"
        )

        # Add a session anchored in ~/work/repo: the name auto-derives to the
        # bare "claude" (free again), it is stored as an absolute path, and the
        # supervisor starts the agent in that directory.
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-d 'agent=claude&cwd=~/work/repo' "
            "https://box.test/sessions/add | grep -x 303 >/dev/null"
        )
        machine.succeed(
            "jq -e '.sessions.claude.workingDirectory == \"/home/agent/work/repo\"' "
            "/home/agent/.config/agent-box/sessions.json"
        )
        machine.wait_until_succeeds(tmux("has-session -t =claude"), timeout=60)
        machine.wait_until_succeeds(
            tmux('display -p -t "=claude:" "#{pane_current_path}"')
            + " | grep -x /home/agent/work/repo >/dev/null",
            timeout=60,
        )

        # A SECOND session in that directory is named after the DIRECTORY
        # (issue #277): "claude" is taken now, and a random "claude-a3f9" named
        # nothing an operator could recognise in a row. Both creation paths
        # mint names, so both are asserted — the daemon's gen_session_name and
        # the CLI's gen_name must agree.
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-d 'agent=claude&cwd=~/work/repo' "
            "https://box.test/sessions/add | grep -x 303 >/dev/null"
        )
        machine.succeed(
            "jq -e '.sessions.repo.workingDirectory == \"/home/agent/work/repo\"' "
            "/home/agent/.config/agent-box/sessions.json"
        )
        machine.succeed(as_agent(
            "agent-box-session add --agent claude --cwd /home/agent/work/repo"
        ))
        machine.succeed(
            "jq -e '.sessions | has(\"repo-2\")' "
            "/home/agent/.config/agent-box/sessions.json"
        )
        # A session in HOME keeps the random suffix: home's basename is the
        # user's own name, which says nothing about the work.
        machine.succeed(as_agent("agent-box-session add --agent claude"))
        machine.succeed(
            "jq -e '[.sessions | keys[] | select(startswith(\"claude-\"))] "
            "| length == 1' /home/agent/.config/agent-box/sessions.json"
        )
        for extra in ["repo", "repo-2"]:
            client.succeed(
                f"{curl} -u agent:testpassword -o /dev/null "
                f"-d 'name={extra}' https://box.test/sessions/delete"
            )
        suffixed = machine.succeed(
            "jq -r '.sessions | keys[] | select(startswith(\"claude-\"))' "
            "/home/agent/.config/agent-box/sessions.json"
        ).strip()
        machine.succeed(as_agent(f"agent-box-session rm {suffixed}"))

        # Clean up so the feed subtest starts from a known session set.
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null "
            "-d 'name=claude' https://box.test/sessions/delete"
        )
        settle()
        machine.fail(tmux("has-session -t =claude"))

    # Live session feed: sessions change from outside whichever page is open
    # — the CLI, an agent adding a helper for itself, another browser tab —
    # and both pages follow along instead of going stale until a reload. The
    # daemon streams a fingerprint of the session state; the page re-fetches
    # itself when it moves. Asserted end to end THROUGH CADDY, because a
    # stream that only works when curled at the unix socket direct would
    # leave the UI exactly as stale as before.
    with subtest("the session feed streams changes through Caddy"):
        # Auth-gated like every other route on the vhost.
        client.succeed(
            f"{curl} -o /dev/null -w '%{{http_code}}' "
            "https://box.test/sessions/events | grep -x 401 >/dev/null"
        )
        # Both pages carry the feed's handle: where to stream from, plus the
        # fingerprint of the state they were rendered with (so a change
        # landing before the stream connects is not missed).
        for page_url in ["https://box.test/agent/", "https://box.test/agent/settings/"]:
            feed_page = client.succeed(f"{curl} -u agent:testpassword {page_url}")
            assert 'name="agent-box-events" content="/sessions/events"' in feed_page, feed_page
            assert re.search(r'data-fp="[0-9a-f]{16}"', feed_page), feed_page

        # Hold a stream open from the client VM. Its first frame replays the
        # fingerprint of the state as it stands now — the baseline a later
        # frame has to differ from, so that merely receiving a frame cannot
        # pass this test.
        client.succeed(
            f"nohup {curl} -N --max-time 120 -u agent:testpassword "
            "https://box.test/sessions/events > /tmp/feed.txt 2>&1 < /dev/null &"
        )
        client.wait_until_succeeds("grep -q '^data:' /tmp/feed.txt", timeout=60)
        baseline = json.loads(
            client.succeed("grep -m1 '^data:' /tmp/feed.txt").split("data:", 1)[1]
        )["fp"]

        # Create a session the way everything outside the browser does.
        machine.succeed(
            "su -s /bin/sh agent -c 'agent-box-session add feedtest --agent claude'"
        )
        client.wait_until_succeeds(
            f"grep '^data:' /tmp/feed.txt | grep -v {baseline} >/dev/null", timeout=60
        )
        # The one-shot fingerprint (what a client that cannot hold a stream
        # open polls instead) moves with it.
        polled = json.loads(
            client.succeed(
                f"{curl} -u agent:testpassword 'https://box.test/sessions/events?poll=1'"
            )
        )["fp"]
        assert polled != baseline, polled

        machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm feedtest'")

    # Transcript download (issue #248): each agent keeps its conversation as
    # a JSONL file under $HOME, and the session's row hands that file over.
    # The path is resolved from the session RECORD, so the route serves the
    # transcripts the panel lists and nothing else in the home directory.
    with subtest("a session's transcript downloads from its row"):
        # main has run, so the supervisor stamped its box session id into
        # sessions.json; claude names the transcript after that id, under a
        # directory of its own cwd mangling. The VM's claude has no
        # credentials and never writes one, so stand it in.
        box_id = machine.succeed(
            "jq -r '.sessions.main.boxSessionId' "
            "/home/agent/.config/agent-box/sessions.json"
        ).strip()
        assert re.match(r"^[0-9a-f-]{36}$", box_id), box_id
        proj = "/home/agent/.claude/projects/-home-agent"
        turn = ('{"type":"user","message":{"role":"user",'
                '"content":"audit the vendored openauth fork"}}')

        def seed(path, content, mode="0600"):
            """Stand in a file the agent would have written: root-created and
            handed over, at the 0600 a real transcript carries. NOT written as
            the agent uid — `install -D` leaves the parent dirs root-owned, so
            the next `su agent` redirect into them would be EACCES. Only
            readability by the settings daemon (which runs as the user)
            matters here."""
            machine.succeed("printf '%s\\n' " + shlex.quote(content) + " > /tmp/seed")
            machine.succeed(f"install -D -o agent -m {mode} /tmp/seed {path}")

        seed(f"{proj}/{box_id}.jsonl", turn)

        # The row offers it as a plain GET link (no form: nothing changes),
        # and the route answers with the file itself, named after the
        # session so a downloads folder full of them stays readable.
        #
        # Waited for, not asserted on the first render: transcript lookups are
        # memoized per session for a few seconds, and the subtests above
        # rendered this page moments ago — with no transcript to find then, so
        # that answer is still cached here.
        client.wait_until_succeeds(
            f"{curl} -u agent:testpassword https://box.test/agent/settings/ "
            "| grep 'transcript?name=main' >/dev/null",
            timeout=60,
        )
        row_page = client.succeed(
            f"{curl} -u agent:testpassword https://box.test/agent/settings/"
        )
        assert 'href="/sessions/transcript?name=main" download' in row_page, row_page

        # The row says WHICH conversation it holds, and so does the tooltip:
        # the opening prompt, the size, and when the file was last appended to
        # (issue #277). Two claude rows under one project tree used to read
        # identically — name, agent, cwd, state and nothing else — so the wrong
        # transcript got downloaded. The prompt is text the operator typed, so
        # it must arrive ESCAPED in both attributes.
        assert 'class="meta topic"' in row_page, row_page
        assert "audit the vendored openauth fork" in row_page, row_page
        assert ('title="Download transcript '
                "&quot;audit the vendored openauth fork&quot; (") in row_page, row_page
        assert "last written " in row_page, row_page
        headers = client.succeed(
            f"{curl} -u agent:testpassword -D - -o /tmp/tr.jsonl "
            "'https://box.test/sessions/transcript?name=main'"
        )
        assert "application/x-ndjson" in headers, headers
        assert f'filename="main-{box_id}.jsonl"' in headers, headers
        assert client.succeed("cat /tmp/tr.jsonl").strip() == turn, headers

        # /clear starts a NEW transcript under a new id and the SessionStart
        # hook records the rotation (issue #223). The download follows that
        # record, so what arrives is the conversation on screen and not the
        # one the user cleared away. Retried: the lookup is memoized for a
        # few seconds per session.
        cleared = "11111111-2222-3333-4444-555555555555"
        record_dir = "/home/agent/.local/state/agent-box/live-session-id"
        seed(f"{proj}/{cleared}.jsonl",
             '{"type":"user","message":{"role":"user",'
             '"content":"after the clear"}}')
        seed(f"{record_dir}/{box_id}", cleared, mode="0644")
        client.wait_until_succeeds(
            f"{curl} -u agent:testpassword -D - -o /dev/null "
            "'https://box.test/sessions/transcript?name=main' "
            f"| grep 'filename=\"main-{cleared}.jsonl\"' >/dev/null",
            timeout=60,
        )

        # The row's label follows that rotation too: it must advertise the
        # conversation the button hands over, never the one /clear replaced.
        rotated_page = client.succeed(
            f"{curl} -u agent:testpassword https://box.test/agent/settings/"
        )
        assert "after the clear" in rotated_page, rotated_page
        assert "audit the vendored openauth fork" not in rotated_page, rotated_page

        # A session with no conversation to hand over (here a shell session,
        # but equally a codex session started with no prompt to stamp) gets
        # no button at all rather than one that 404s.
        machine.succeed(as_agent("agent-box-session add shelltest --agent shell"))
        client.wait_until_succeeds(
            f"{curl} -u agent:testpassword https://box.test/agent/settings/ "
            "| grep 'value=\"shelltest\"' >/dev/null",
            timeout=60,
        )
        shell_page = client.succeed(
            f"{curl} -u agent:testpassword https://box.test/agent/settings/"
        )
        assert "transcript?name=shelltest" not in shell_page, shell_page

        # Only a listed session with a transcript is served, and the route
        # is inside the same auth gate as the page that links to it.
        for bad in ["shelltest", "ghost", "..%2F..%2Fetc%2Fpasswd", ""]:
            client.succeed(
                f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
                f"'https://box.test/sessions/transcript?name={bad}' | grep -x 404 >/dev/null"
            )
        client.succeed(
            f"{curl} -o /dev/null -w '%{{http_code}}' "
            "'https://box.test/sessions/transcript?name=main' | grep -x 401 >/dev/null"
        )

        machine.succeed(as_agent("agent-box-session rm shelltest"))

  '';
}
