# VM test for issue #59: sessions are runtime data, decoupled from linux
# users. One hardened unit per USER supervises tmux sessions declared in the
# user-owned ~/.config/agent-box/sessions.json.
#
# This half owns everything that happens BELOW the browser — the supervisor,
# the session CLI and the agent harnesses. The browser surface on top of the
# same box is tests/sessions-web.nix, and the box both drive is
# tests/sessions-common.nix (issue #312: this was one 1870-line test that ran
# for 325s and held all of CI open behind it). Exercises:
#   - first-boot seeding of the Nix-declared config into sessions.json
#     (legacy per-user options standing in for a session named "main"),
#   - runtime session add/rm via the agent-box-session CLI — as the user,
#     no sudo, no nixos-rebuild,
#   - runtime-created sessions still living inside the hardened agent unit's
#     cgroup (the tmux server is a child of the supervisor),
#   - the supervisor recreating a killed listed session (restart semantics)
#     and NOT recreating a delisted one (destroy semantics),
#   - stop semantics (issue 167): a clean agent exit (or agent-box-session
#     stop) parks the session — listed, flagged stopped, left down — and
#     restart clears the flag and revives it,
#   - both agent CLIs installed regardless of what sessions run — this box
#     asks for them with eagerAgents, which since issue #416 is what puts a
#     harness in the closure instead of fetching it on first use,
#   - the instruction files each harness reads (issue #305): the editable
#     AGENTS.md plus a user-scope pointer at the canonical guide under the
#     name that harness discovers — ~/.claude/CLAUDE.md for claude (which
#     also gets a project-scope CLAUDE.md beside the notes) and
#     ~/.codex/AGENTS.md for codex — seeded IFF absent, never clobbering,
#     and skipped entirely for shell sessions,
#   - browser tmux clients advertise OSC 8 support, preserving a long hidden
#     hyperlink target when its visible URL wraps across terminal rows (#18),
#   - the codex arms: the app-server daemon, box-wide autonomy through
#     /etc/codex/config.toml (issue 234), the pane driving its own sign-in
#     (issue 159) and re-authenticating rejected credentials (issue 187),
#   - the kickoff prompt firing once and then resuming, the state file the
#     supervisor keeps per session and the sweep that reclaims it (#282),
#     segment rotation (#223) and agent profiles (#321),
#   - two writers of sessions.json racing each other (#254, tests from #285).
#
# Like the other tests, the shared node lib.mkForce-swaps the module
# Caddyfile for a minimal `tls internal` one (no ACME in the sandbox).
{ agent-box }:
let
  common = import ./sessions-common.nix { inherit agent-box; };
in
{
  name = "agent-box-sessions";
  node.pkgsReadOnly = false;

  nodes.machine = common.machineNode;
  # Nothing here talks to the client — it is declared so that this test's node
  # SET matches tests/sessions-web.nix's. The test framework writes every
  # node's address into every node's /etc/hosts, so a machine with a client
  # beside it and a machine without one are different systems, and nix would
  # build the guest twice. It is never started (see start below), so it costs
  # nothing at run time either.
  nodes.client = common.clientNode;

  testScript = common.prelude + ''
    # Only this half reads a terminal back byte for byte (the OSC 8 assertion).
    import base64

    machine.start()
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_for_unit("agent-box-settings@agent.service")

    # --- first boot: legacy options seeded a "main" session --------------
    machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=120)
    # Issue #78: this disk is fresh on every run, so ~/.config does not
    # exist yet when tmpfiles processes the rules — the exact condition
    # under which the parent-dir rule (#356) matters. Before that fix,
    # tmpfiles auto-created ~/.config itself as root, then refused to
    # descend into it ("Detected unsafe path transition ... owned by
    # root", exit 73) and silently skipped the child rule below. Asserting
    # the directory itself, not just the file inside it, is what actually
    # proves that path rather than assuming it from the file's existence.
    machine.succeed(
        "stat -c '%U %a' /home/agent/.config/agent-box | grep -x 'agent 700'"
    )
    machine.succeed(
        "stat -c '%U %a' /home/agent/.config/agent-box/sessions.json "
        "| grep -x 'agent 600'"
    )
    machine.succeed(
        "jq -e '.sessions.main.agent == \"claude\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # With remoteControlHost unset, the auto-derived "<user>-<session>@<host>"
    # Remote Control name takes its host suffix from the public web.domain,
    # NOT the internal kernel hostname — and every session (including "main")
    # gets the "-<session>" suffix (no "main" special case). Since issue #154
    # Phase 2 the supervisor is a shared user-independent script: the host
    # label rides the unit environment and the name derivation uses $USER,
    # so assert the env var plus the script's derivation line.
    # head -n1: `systemctl show --value` prints BOTH path= and argv[]=, so the
    # grep -o matches the store path twice. Without it, interpolating the
    # two-line value below makes the second line its own shell command — the
    # backdoor shell EXECUTES the supervisor script as root and never returns
    # (the CI hang on this PR's first three runs). The pattern has to end in
    # "/bin/agent-box-supervisor", not just "-agent-box-supervisor": the
    # writeShellScriptBin derivation directory ITSELF ends in
    # "-agent-box-supervisor" too, one path segment earlier — matching only
    # the shorter suffix greps the directory, not the script, and `grep` on
    # a directory fails with "Is a directory" (exit 2) below.
    start_script = machine.succeed(
        "systemctl show agent-box@agent --property=ExecStart --value "
        "| grep -o '/nix/store/[^ ;]*/bin/agent-box-supervisor' | head -n1"
    ).strip()
    machine.succeed(
        "systemctl show agent-box@agent -p Environment --value "
        "| grep -F 'AGENT_BOX_HOST_LABEL=box.test' >/dev/null"
    )
    machine.succeed(f"grep -qF 'rcname=$USER-$sname' {start_script}")

    # Both agent CLIs are installed even though no session uses codex yet:
    # this box lists both in eagerAgents. On a DEFAULT box that list is empty
    # and neither is here until a session asks (issue #416) — the rendering
    # of which is locked by the golden fixture, not by a VM that cannot
    # reach the network to prove the fetch.
    machine.succeed("test -x /run/current-system/sw/bin/claude")
    machine.succeed("test -x /run/current-system/sw/bin/codex")
    machine.succeed("su -s /bin/sh agent -c 'test -x /home/agent/.codex/packages/standalone/current/codex'")
    machine.succeed("test -x /run/current-system/sw/bin/bwrap")
    # Tools agents assume exist resolve by bare name in agent tool shells.
    # Assert against the UNIT's PATH, not via as_agent(): that runs under su,
    # which gets the full system path and would pass even when the unit PATH
    # is missing them (the bug this guards against).
    unit_path = machine.succeed(
        "systemctl show agent-box@agent -p Environment --value"
    ).split("PATH=")[1].split()[0]
    for tool in ["curl", "wget", "awk", "tar", "gzip", "bzip2", "xz", "zip",
                 "unzip", "diff", "patch", "less", "file", "ps", "killall",
                 "lsblk", "ssh", "rsync", "ping", "ip", "dig", "nc",
                 "openssl", "gpg", "python3", "nano", "rg", "jq"]:
        machine.succeed(
            f"su -s /bin/sh agent -c 'PATH={unit_path} command -v {tool}'"
        )

    # HTTPS github clones authenticate via GH_TOKEN: gh's credential helper
    # is wired system-wide, and gh itself is on the agent unit's PATH.
    machine.succeed(
        "su -s /bin/sh agent -c "
        "'git config --get credential.https://github.com.helper' | grep 'gh auth git-credential' >/dev/null"
    )
    machine.succeed("systemctl cat agent-box@agent | grep -- '-gh-' >/dev/null")

    # Claude emits its long OAuth URL inside one complete OSC 8 sequence.
    # tmux stores that metadata, but redraws plain text unless the attaching
    # xterm-256color client explicitly advertises hyperlink support. Emit a
    # 450-byte wrapped link before attach, like opening ttyd after Claude has
    # printed it, and assert tmux sends the full hidden target to the client.
    link_prefix = "https://httpbin.invalid/anything?state="
    link_suffix = "&sentinel=END_OF_FULL_URL"
    link_url = link_prefix + "a" * (450 - len(link_prefix) - len(link_suffix)) + link_suffix
    link_sequence = (
        "\x1b]8;;" + link_url + "\x1b\\"
        + link_url
        + "\x1b]8;;\x1b\\"
    )
    tmux_browser_command = "printf %s " + shlex.quote(link_sequence) + "; sleep 5"
    machine.succeed(
        tmux(
            "new-session -d -s browser-link-test "
            + shlex.quote(tmux_browser_command)
        )
    )
    tmux_attach_command = (
        "env TMUX_TMPDIR=/run/agent-box-agent "
        "tmux -T hyperlinks -L agent-box attach -t =browser-link-test"
    )
    machine.succeed(
        as_agent(
            f"TERM=xterm-256color script -q -c {shlex.quote(tmux_attach_command)} /dev/null "
            "> /tmp/tmux-browser-link"
        )
    )
    tmux_browser_output = base64.b64decode(
        machine.succeed("base64 -w0 /tmp/tmux-browser-link").strip()
    )
    hyperlink_targets = re.findall(
        rb"\x1b]8;[^;]*;([^\x1b]*)\x1b\\", tmux_browser_output
    )
    assert link_url.encode() in hyperlink_targets, tmux_browser_output
    assert b"END_OF_FULL_URL" in tmux_browser_output, tmux_browser_output

    # --- instruction files the harnesses actually read (issue #305) -------
    with subtest("seeded AGENTS.md and each harness's guide pointers"):
        # The boot "main" session is claude in $HOME, so all three files are
        # seeded already: the editable notes file plus the two pointers claude
        # needs (it discovers CLAUDE.md only and never reads AGENTS.md).
        machine.succeed("test -f /home/agent/AGENTS.md")
        machine.succeed(
            "grep -F /etc/agent-box-guides/AGENTS.agent.md /home/agent/AGENTS.md"
            " >/dev/null"
        )
        # Project scope is a plain symlink to the sibling notes file.
        machine.succeed("test -L /home/agent/CLAUDE.md")
        machine.succeed("readlink /home/agent/CLAUDE.md | grep -Fx AGENTS.md")
        # User scope is a plain symlink straight at the canonical guide, so
        # it stays live across box updates without ever being reseeded.
        machine.succeed("test -L /home/agent/.claude/CLAUDE.md")
        machine.succeed(
            "readlink /home/agent/.claude/CLAUDE.md"
            " | grep -Fx /etc/agent-box-guides/AGENTS.agent.md"
        )
        # That target is readable by the agent and not writable by it.
        machine.succeed(as_agent("test -r /etc/agent-box-guides/AGENTS.agent.md"))
        machine.fail(as_agent("test -w /etc/agent-box-guides/AGENTS.agent.md"))
        machine.succeed("stat -c '%U %a' /home/agent/AGENTS.md | grep -x 'agent 644'")
        for seeded in ["/home/agent/CLAUDE.md", "/home/agent/.claude/CLAUDE.md"]:
            machine.succeed(f"stat -c '%U' {seeded} | grep -x agent")

        # Nothing is ever clobbered: a directory that already holds both files
        # keeps its own content, so a repo checkout's CLAUDE.md and any hand
        # edit survive every (re)spawn.
        machine.succeed(as_agent("mkdir -p /home/agent/keep"))
        machine.succeed(as_agent("echo MINE-AGENTS > /home/agent/keep/AGENTS.md"))
        machine.succeed(as_agent("echo MINE-CLAUDE > /home/agent/keep/CLAUDE.md"))
        machine.succeed(as_agent(
            "agent-box-session add keeper --harness claude --cwd /home/agent/keep"
        ))
        machine.wait_until_succeeds(tmux("has-session -t =keeper"), timeout=60)
        machine.succeed("grep -Fx MINE-AGENTS /home/agent/keep/AGENTS.md >/dev/null")
        machine.succeed("grep -Fx MINE-CLAUDE /home/agent/keep/CLAUDE.md >/dev/null")
        machine.succeed(as_agent("agent-box-session rm keeper"))

        # A codex session gets AGENTS.md alone in its working directory —
        # codex reads that name natively, so a CLAUDE.md beside it would be
        # dead weight. Nothing has claimed codex's global instructions yet.
        machine.fail("test -e /home/agent/.codex/AGENTS.md")
        machine.succeed(as_agent("mkdir -p /home/agent/cxdir"))
        machine.succeed(as_agent(
            "agent-box-session add cx --harness codex --cwd /home/agent/cxdir"
        ))
        machine.wait_until_succeeds("test -f /home/agent/cxdir/AGENTS.md", timeout=60)
        machine.fail("test -e /home/agent/cxdir/CLAUDE.md")
        # ...and the counterpart of claude's user-scope pointer: codex reads
        # project docs from the project root DOWN, so a session working in a
        # checkout below its working directory loses the seeded notes file
        # and with it every mention of the guide. $CODEX_HOME/AGENTS.md is
        # the scope that survives that, pointed straight at the canonical
        # guide so box updates keep it current.
        machine.wait_until_succeeds("test -L /home/agent/.codex/AGENTS.md", timeout=60)
        machine.succeed(
            "readlink /home/agent/.codex/AGENTS.md"
            " | grep -Fx /etc/agent-box-guides/AGENTS.agent.md"
        )
        machine.succeed("stat -c '%U' /home/agent/.codex/AGENTS.md | grep -x agent")
        machine.succeed(as_agent("agent-box-session rm cx"))

        # A shell session gets neither: no agent there reads them.
        machine.succeed(as_agent("mkdir -p /home/agent/shdir"))
        machine.succeed(as_agent(
            "agent-box-session add sh --harness shell --cwd /home/agent/shdir"
        ))
        machine.wait_until_succeeds(tmux("has-session -t =sh"), timeout=60)
        machine.fail("test -e /home/agent/shdir/AGENTS.md")
        machine.fail("test -e /home/agent/shdir/CLAUDE.md")
        machine.succeed(as_agent("agent-box-session rm sh"))

    # --- pre-accepted claude first-run dialogs ----------------------------
    with subtest("claude's startup dialogs are seeded, not asked"):
        # The boot "main" session is claude in $HOME, so the flags are already
        # written. Sign-in is meant to be the ONLY interactive step: a session
        # parked on a dialog is one Remote Control cannot answer.
        machine.wait_until_succeeds(
            "jq -e '.projects[\"/home/agent\"].hasTrustDialogAccepted == true'"
            " /home/agent/.claude.json",
            timeout=60,
        )
        # hasCompletedOnboarding skips the first-run wizard, whose opening
        # screen is the theme picker. The wizard pushes that step
        # unconditionally, so a seeded theme alone would not skip it.
        machine.succeed(
            "jq -e '.hasCompletedOnboarding == true' /home/agent/.claude.json"
        )
        machine.succeed(
            "jq -e '.theme == \"dark\"' /home/agent/.claude/settings.json"
        )
        # The "Try the new fullscreen renderer?" upsell sits outside that
        # wizard and survives hasCompletedOnboarding (issue #395): any
        # explicit tui setting retires it, seeded here as "default" to
        # match the classic renderer this box is built around.
        machine.succeed(
            "jq -e '.tui == \"default\"' /home/agent/.claude/settings.json"
        )
        # Regression guard: the pane itself must never show the upsell text,
        # not just the settings.json seed that is meant to prevent it. -S -
        # (not a fixed -50) so the assertion can't pass just because the
        # upsell scrolled out of a truncated capture window.
        main_pane = machine.succeed(tmux('capture-pane -p -S - -t "=main:"'))
        assert "fullscreen renderer" not in main_pane.lower(), main_pane

        # The theme seed never clobbers a hand-picked one: /theme writes the
        # same key, and the seeder re-runs on every session start.
        machine.succeed(as_agent(
            "jq '.theme = \"light\"' /home/agent/.claude/settings.json"
            " > /home/agent/s.tmp && mv /home/agent/s.tmp"
            " /home/agent/.claude/settings.json"
        ))
        machine.succeed(as_agent("agent-box-session add themed --harness claude"))
        machine.wait_until_succeeds(tmux("has-session -t =themed"), timeout=60)
        machine.succeed(
            "jq -e '.theme == \"light\"' /home/agent/.claude/settings.json"
        )
        machine.succeed(as_agent("agent-box-session rm themed"))

    # --- runtime add: no sudo, no rebuild ---------------------------------
    machine.succeed(
        "su -s /bin/sh agent -c 'agent-box-session add helper --harness codex'"
    )
    machine.wait_until_succeeds(tmux("has-session -t =helper"), timeout=60)
    machine.succeed(
        "jq -e '.sessions.helper.agent == \"codex\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # Codex honours remoteControl (issue 103): with the default
    # remoteControl=true, a codex session starts the local app-server daemon,
    # enables Remote Control on it, and does NOT run the interactive TUI. The
    # offline-safe local start matters here because the VM has no Codex login.
    # The daemon detaches, so the session's foreground command is the agent-box
    # supervisor wrapper that owns its lifecycle;
    # assert the wrapper runs and passes the autonomy -c overrides (the
    # subcommand rejects the TUI's --dangerously-bypass flag, so skipPermissions
    # rides in as -c approval_policy / sandbox_mode instead).
    # Those overrides are NOT what makes a remote thread autonomous — see the
    # /etc/codex/config.toml assertions below (issue 234).
    # pgrep runs as ROOT (not via as_agent): `-u agent` then filters out the
    # invoking shell, whose own command line contains the pattern — as the
    # agent it self-matched and "succeeded" with the wrapper long dead.
    helper_cmdline = machine.wait_until_succeeds(
        "pgrep -u agent -af agent-box-codex-remote-control", timeout=60
    )
    assert "-c approval_policy=never" in helper_cmdline, helper_cmdline
    assert "-c sandbox_mode=danger-full-access" in helper_cmdline, helper_cmdline
    assert "--dangerously-bypass-approvals-and-sandbox" not in helper_cmdline, helper_cmdline
    # The wrapper actually brings the daemon up: its control socket answers
    # (`app-server daemon version` exits 0 only against a live daemon). This
    # needs no codex login — starting the daemon is separate from pairing.
    machine.wait_until_succeeds(
        as_agent("codex app-server daemon version"), timeout=60
    )

    # --- codex autonomy reaches the app-server (issue 234) ----------------
    # The -c overrides above never arrive: `app-server daemon start` parses
    # them and then spawns the app-server with a fixed argv. A phone paired to
    # this daemon therefore kept asking for approvals while the wrapper
    # assertions above passed. services.agent-box.codexFullAccess writes
    # codex's SYSTEM config layer instead, which every codex entry point reads.
    machine.succeed("grep -F 'approval_policy = \"never\"' /etc/codex/config.toml")
    machine.succeed(
        "grep -F 'sandbox_mode = \"danger-full-access\"' /etc/codex/config.toml"
    )
    # End-to-end, against the same binary the daemon runs: a thread that names
    # no policy — what the Codex apps open — must come back fully autonomous.
    # `thread/start` needs neither a login nor the network, so this works in
    # the sandbox. The trailing sleep holds stdin open while the server
    # answers; feeding both lines and closing it EOFs the server before it
    # replies.
    #
    # Backgrounded and polled rather than sat through (issue #312): the reply
    # lands in well under a second, but a foreground pipeline paid the whole
    # 13s hold on every run. The hold is still there — it just no longer
    # gates the assertions, and the server exits on its own afterwards.
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"clientInfo": {"name": "agent-box-test", "version": "0"}},
    })
    start = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "thread/start",
        "params": {"ephemeral": True, "cwd": "/home/agent"},
    })
    app_server = (
        "{ printf '%s\\n' " + shlex.quote(init)
        + "; sleep 1; printf '%s\\n' " + shlex.quote(start)
        + "; sleep 30; } | codex app-server --listen stdio:// 2>/dev/null"
    )
    machine.succeed(as_agent(
        "nohup sh -c " + shlex.quote(app_server)
        + " > /tmp/app-server.out 2>/dev/null < /dev/null &"
    ))
    machine.wait_until_succeeds("grep -q approvalPolicy /tmp/app-server.out", timeout=60)
    thread = machine.succeed("cat /tmp/app-server.out")
    assert '"approvalPolicy":"never"' in thread, thread
    assert '"dangerFullAccess"' in thread, thread

    # The pane DRIVES sign-in itself (issue 159): a logged-out box must run the
    # device-code flow in this pane, not print commands for the user to paste
    # into a second session — this VM has no Codex login, which is exactly that
    # state. -S -50 because the codex sign-in output pushes the banner up.
    helper_pane = machine.wait_until_succeeds(
        tmux('capture-pane -p -S -50 -t "=helper:"')
        + " | grep -F 'Signing this box in'",
        timeout=90,
    )
    assert "you do not need another terminal" in helper_pane, helper_pane
    # Regression guard on the old onboarding: no copy-paste command list, and no
    # instruction to open a shell session to run it.
    full_pane = machine.succeed(tmux('capture-pane -p -S -50 -t "=helper:"'))
    assert "--harness shell" not in full_pane, full_pane
    assert "codex login --device-auth" not in full_pane, full_pane

    # --- rejected credentials re-authenticate with no keystroke (issue 187) ---
    # `codex login status` is a LOCAL check — credentials the backend has
    # already invalidated still report "logged in", so the sign-in guard
    # (correctly) declines and PAIRING is what returns HTTP 401. The pane used
    # to dump that JSON-RPC blob and wait for a user who had to know the
    # undocumented `login` word to recover.
    #
    # A real server-side rejection is not producible in the sandbox (no
    # network), so drive the supervisor wrapper straight against a stub codex.
    # `app-server daemon version` fails in the stub, which ends the wrapper's
    # health loop instead of blocking the test; the "" first argument skips the
    # UTS re-exec, which is not what this asserts.
    # Assigned and asserted, not `re.search(...).group(0)`: the driver
    # type-checks testScript, and a Match|None cannot be subscripted there.
    wrapper_match = re.search(
        r"/nix/store/\S*agent-box-codex-remote-control", helper_cmdline
    )
    assert wrapper_match, helper_cmdline
    wrapper = wrapper_match.group(0)
    machine.succeed(
        "cat > /tmp/stub-codex <<'EOF'\n"
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> /tmp/stub/log\n'
        'case "$*" in\n'
        '"app-server daemon version") exit 1 ;;\n'
        '"login status") test -f /tmp/stub/loggedin ;;\n'
        '"login --device-auth")\n'
        "  touch /tmp/stub/loggedin\n"
        "  test -f /tmp/stub/persist || rm -f /tmp/stub/stale\n"
        '  echo "Successfully logged in" ;;\n'
        '"logout") rm -f /tmp/stub/loggedin ;;\n'
        '"remote-control pair")\n'
        '  test -f /tmp/stub/loggedin || { echo "not signed in" >&2; exit 1; }\n'
        # Single-quoted printf, not echo: the real error embeds a JSON body, and
        # whether `echo` eats the backslashes of an escaped one is shell-
        # dependent — the pane greps that JSON for the message it shows.
        "  if test -f /tmp/stub/stale; then\n"
        "    printf '%s\\n' 'Error: remoteControl/pairing/start failed: remote"
        " control server refresh failed: HTTP 401 Unauthorized, cf-ray: test,"
        ' body: {"error":{"message":"Your authentication token has been'
        ' invalidated. Please try signing in again.","type":'
        '"invalid_request_error","code":"token_invalidated"},"status":401}'
        "' >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  if test -f /tmp/stub/offline; then\n"
        "    printf '%s\\n' 'Error: remoteControl/pairing/start failed: error"
        " sending request: tcp connect error: Connection refused (os error"
        " 111)' >&2\n"
        "    exit 1\n"
        "  fi\n"
        '  echo "Pairing code: TEST-PAIR" ;;\n'
        "esac\n"
        "EOF"
    )
    machine.succeed("chmod 0755 /tmp/stub-codex")

    def run_pane(*markers):
        machine.succeed("rm -rf /tmp/stub && install -d -m 0777 /tmp/stub")
        for marker in markers:
            machine.succeed(f"touch /tmp/stub/{marker}")
        machine.succeed(
            # The empty first argument is written with DOUBLE quotes: a bare
            # pair of single quotes would end this Nix indented string here.
            as_agent(f'{wrapper} "" /tmp/stub-codex > /tmp/stub/out 2>&1 || true')
        )
        return (
            machine.succeed("cat /tmp/stub/out"),
            machine.succeed("cat /tmp/stub/log"),
        )

    # Invalidated token: the pane explains itself in the server's own words,
    # drops the dead credentials, runs the device flow and pairs — all without a
    # keystroke, and without showing anyone an HTTP status line.
    stale_out, stale_calls = run_pane("loggedin", "stale")
    assert "rejected this box's stored credentials" in stale_out, stale_out
    assert "authentication token has been invalidated" in stale_out, stale_out
    assert "HTTP 401" not in stale_out, stale_out
    assert "Press Enter to try again" not in stale_out, stale_out
    assert "Pairing code: TEST-PAIR" in stale_out, stale_out
    assert "logout" in stale_calls, stale_calls
    assert "login --device-auth" in stale_calls, stale_calls
    # A rejection short-circuits the enrollment-race retries: one failed pair,
    # then one that succeeds after signing in. Not three failures in six seconds.
    assert stale_calls.count("remote-control pair") == 2, stale_calls

    # Credentials that stay rejected after a fresh sign-in are an account
    # problem, so the pane says so ONCE and stops — a logout/device-auth spin
    # would make the box unusable.
    hopeless_out, hopeless_calls = run_pane("loggedin", "stale", "persist")
    assert "still rejects the credentials" in hopeless_out, hopeless_out
    assert hopeless_calls.count("logout") == 1, hopeless_calls
    assert hopeless_calls.count("login --device-auth") == 1, hopeless_calls

    # A pairing failure that is NOT about auth keeps the old behaviour: retry
    # (enrollment races clear on their own), report, offer Enter — and never
    # drop working credentials.
    offline_out, offline_calls = run_pane("loggedin", "offline")
    assert "Could not mint a pairing code" in offline_out, offline_out
    assert "Press Enter to try again" in offline_out, offline_out
    assert "logout" not in offline_calls, offline_calls
    assert offline_calls.count("remote-control pair") == 3, offline_calls

    # Re-adding an existing name errors out and must not clobber the stored
    # config (issue 100): helper keeps its codex agent.
    machine.fail(
        "su -s /bin/sh agent -c 'agent-box-session add helper --harness claude'"
    )
    machine.succeed(
        "jq -e '.sessions.helper.agent == \"codex\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # The runtime-created session runs INSIDE the hardened agent unit's
    # cgroup: the tmux server is a child of the supervisor, so systemd
    # sandboxing covers sessions added long after boot.
    # NOTE the "=helper:" (trailing colon): display/capture take a target-
    # PANE, and a bare "=name" only resolves when that session is tmux's
    # idea of the current one — otherwise it silently expands to "" (rc 0).
    server_pid = machine.succeed(tmux('display -p -t "=helper:" "#{pid}"')).strip()
    machine.succeed(f"grep -q agent-box@agent.service /proc/{server_pid}/cgroup")

    # ls shows both sessions with their agents.
    listing = machine.succeed("su -s /bin/sh agent -c 'agent-box-session ls'")
    assert "main" in listing and "helper" in listing, listing
    assert "codex" in listing, listing

    # --- peers: who else is live, where, and what they claim (#420) --------
    # The question no session could ask. `ls` above says which sessions
    # EXIST; it never says what one is doing, and a session's webhook claims
    # live in a file only that session reads (defangdevs/local-channels#27).
    # So a second session walked into a live one's git worktree and committed
    # over it (#417), and a dispatched session pushed to a branch another
    # session owned three times in one evening (#319). A hook session's spawn
    # prompt now carries this output and is told to yield on it, so what it
    # yields on is asserted here.
    peers_out = machine.succeed(as_agent("agent-box-session peers"))
    assert re.search(r"^main .*claude, interactive, cwd /home/agent",
                     peers_out, re.M), peers_out
    assert re.search(r"^helper .*codex, interactive, cwd /home/agent",
                     peers_out, re.M), peers_out
    # Said, not left blank: a session with no claim is exactly the one whose
    # work a standing watch will start a second agent onto.
    assert peers_out.count("no subscription file") >= 2, peers_out

    # The caller is not its own neighbour, and is identified ONLY from $TMUX
    # (a pane) or the supervisor's LOCAL_WEBHOOK_SESSION (<user>-<session>).
    # Never from tmux's own idea of a current session: `display-message` with
    # no client does not fail, it answers with the most recently ACTIVE
    # session — so asking it from a caller that has no pane (the webhook spawn
    # wrapper, or this su) would drop whichever session was busiest from the
    # very list that exists to reveal it. The assertions above are that case,
    # and this one is the identification working when it can:
    mine = machine.succeed(
        as_agent("env LOCAL_WEBHOOK_SESSION=agent-main agent-box-session peers")
    )
    assert not re.search(r"^main ", mine, re.M), mine
    assert re.search(r"^helper ", mine, re.M), mine

    # A CLAIM and a bare subscription are different answers and must not be
    # collapsed: only a topic with an include predicate suppresses a standing
    # watch (local-channels#16), so a session listening to a whole repo has
    # NOT told the box the object is taken. The note is folded onto one line,
    # because it is agent-written text landing in another agent's prompt.
    machine.succeed(as_agent(
        "mkdir -p ~/.local/state/local-webhook; "
        "cat > ~/.local/state/local-webhook/filter.agent-helper.json <<'EOF'\n"
        '{"enabled":true,"topics":['
        '{"topic":"github:defangdevs/agent-box","note":"PR 42:\\tholding it",'
        '"include":{"any":[{"path":"pull_request.number","in":[42]}]}},'
        '{"topic":"github:defangdevs/other","note":"just watching"}]}\n'
        "EOF"
    ))
    claims_out = machine.succeed(as_agent("agent-box-session peers"))
    assert "CLAIMS github:defangdevs/agent-box" in claims_out, claims_out
    assert 'note: "PR 42: holding it"' in claims_out, claims_out
    assert "not a claim) github:defangdevs/other" in claims_out, claims_out
    machine.succeed(
        as_agent("rm ~/.local/state/local-webhook/filter.agent-helper.json")
    )

    # The rank the yield rule turns on is read off the NAME: hook-* is what
    # every webhook-dispatched session is called, and it must be told apart
    # from a session a person or the config started — a dispatched session
    # defers to the second and merely hands off to the first. A `shell`
    # session, because it stays up for the length of the assertion.
    machine.succeed(as_agent("agent-box-session add hook-rank-probe --harness shell"))
    machine.wait_until_succeeds(tmux("has-session -t =hook-rank-probe"), timeout=60)
    ranks = machine.succeed(as_agent("agent-box-session peers"))
    assert re.search(r"^hook-rank-probe .*dispatched \(hook session\)",
                     ranks, re.M), ranks
    assert re.search(r"^main .*interactive", ranks, re.M), ranks
    machine.succeed(as_agent("agent-box-session rm hook-rank-probe"))

    # --- auto-named add: no NAME → derived from the harness ---------------
    # First codex-derived name is the bare harness name (no session is
    # literally "codex" yet — "helper" runs codex but under its own name).
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session add --harness codex'")
    machine.wait_until_succeeds(tmux("has-session -t =codex"), timeout=60)
    machine.succeed(
        "jq -e '.sessions.codex.agent == \"codex\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )
    # A second codex-derived name collides with "codex", so it gets a short
    # random suffix ("codex-XXXX") — a distinct, valid session name.
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session add --harness codex'")
    machine.succeed(
        "jq -e '[.sessions | keys[] | select(test(\"^codex-[0-9a-f]+$\"))] | length == 1' "
        "/home/agent/.config/agent-box/sessions.json"
    )
    suffixed = machine.succeed(
        "jq -r '.sessions | keys[] | select(test(\"^codex-[0-9a-f]+$\"))' "
        "/home/agent/.config/agent-box/sessions.json"
    ).strip()
    machine.wait_until_succeeds(tmux(f'has-session -t "={suffixed}"'), timeout=60)
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm codex'")
    machine.succeed(f"su -s /bin/sh agent -c 'agent-box-session rm {suffixed}'")

    # --- `--agent` is the deprecated spelling of `--harness` --------------
    # The flag was renamed because the harnesses took the word: `claude
    # --agent` and `opencode --agent` both name a WORKER, which on this box
    # is `--profile`. The old spelling keeps working — it is written into
    # every note, README and transcript a deployed box already carries — and
    # says on stderr that it is old. Both halves are the assertion: a warning
    # that dropped the session would break running boxes, and a silent alias
    # would leave the collision in place.
    deprecated = machine.succeed(
        "su -s /bin/sh agent -c "
        "'agent-box-session add legacyflag --agent shell 2>&1'"
    )
    assert "--agent is deprecated" in deprecated, deprecated
    assert "--harness" in deprecated, deprecated
    machine.succeed(
        "jq -e '.sessions.legacyflag.agent == \"shell\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )
    # ...and the new spelling says nothing at all.
    quiet = machine.succeed(
        "su -s /bin/sh agent -c "
        "'agent-box-session add newflag --harness shell 2>&1'"
    )
    assert "deprecated" not in quiet, quiet
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm legacyflag'")
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm newflag'")

    # --- a hostile TMUX_TMPDIR must not disarm the CLI (issue #268) --------
    # The CLI used to defer to an inherited TMUX_TMPDIR, so any system-wide
    # setting (programs.tmux with secureSocket on exports one through
    # /etc/profile) pointed it at an empty socket dir: every verb found no
    # sessions, killed nothing, and still exited 0. The rest of this file
    # cannot catch that — `su -s /bin/sh agent -c` is neither a login nor an
    # interactive shell, so it never inherits one. Set it explicitly instead.
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session add hostile'")
    machine.wait_until_succeeds(tmux("has-session -t =hostile"), timeout=60)
    machine.succeed(
        "su -s /bin/sh agent -c "
        "'TMUX_TMPDIR=/run/user/1000 agent-box-session rm hostile'"
    )
    machine.fail(tmux("has-session -t =hostile"))
    machine.succeed(
        "jq -e '.sessions | has(\"hostile\") | not' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # --- the name length the web UI can render (issue #236) ----------------
    # The settings daemon filters every rendered name through SESSION_RE and
    # DROPS the rest, so a name past its bound costs more than looks: the
    # session runs, holds webhook subscriptions and receives events, yet has
    # no row in the Sessions list and none in the subscriptions panel —
    # nothing to delete, restart or attach in the UI. The bound is what the
    # name-minting paths can emit (hook-<owner/repo>-<4 hex> reaches 150 for
    # GitHub's maxima), NOT a length names are shortened to fit: two repos
    # sharing a prefix would collapse onto one name. So a name well past the
    # old 32 must be accepted end to end...
    at_bound = "n" * 150
    too_long = "n" * 151
    machine.succeed(f"su -s /bin/sh agent -c 'agent-box-session add {at_bound}'")
    machine.succeed(
        f"jq -e '.sessions | has(\"{at_bound}\")' "
        "/home/agent/.config/agent-box/sessions.json"
    )
    machine.succeed(f"su -s /bin/sh agent -c 'agent-box-session rm {at_bound}'")
    # ...and past THAT the CLI says so instead of minting a session the UI
    # would drop. valid_new_name is the gate every creation path shares (the
    # webhook spawn wrapper adds through it).
    add_out = machine.fail(
        f"su -s /bin/sh agent -c 'agent-box-session add {too_long}' 2>&1"
    )
    assert "at most 150 characters" in add_out, add_out
    machine.succeed(
        f"jq -e '.sessions | has(\"{too_long}\") | not' "
        "/home/agent/.config/agent-box/sessions.json"
    )
    # ...but the length rule is on the way IN only. A name that reached
    # sessions.json before this bound existed — or by hand — is invisible in
    # the web UI, so the CLI has to stay able to delete it: gating rm on the
    # length too would leave that session unreachable from anywhere.
    hand_edit = (
        f"jq --arg n {too_long} '.sessions[$n] = {{\"agent\":\"claude\"}}' "
        "$HOME/.config/agent-box/sessions.json > /tmp/long.json && "
        "install -m 0600 /tmp/long.json $HOME/.config/agent-box/sessions.json"
    )
    machine.succeed("su -s /bin/sh agent -c " + shlex.quote(hand_edit))
    machine.succeed(f"su -s /bin/sh agent -c 'agent-box-session rm {too_long}'")
    machine.succeed(
        f"jq -e '.sessions | has(\"{too_long}\") | not' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # --- shell pseudo-agent (issue 113): supervised plain login shell ------
    machine.succeed(
        "su -s /bin/sh agent -c 'agent-box-session add scratch --harness shell'"
    )
    machine.wait_until_succeeds(tmux("has-session -t =scratch"), timeout=60)
    machine.succeed(
        "jq -e '.sessions.scratch.agent == \"shell\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )
    # The pane runs the user's login shell (bash on this box), not an agent.
    machine.wait_until_succeeds(
        tmux('display -p -t "=scratch:" "#{pane_current_command}"')
        + " | grep -x bash",
        timeout=60,
    )
    # A clean `exit` must NOT land in the post-mortem bash (that fallback is
    # for agents only — for a shell it would be a confusing nested shell):
    # the session dies and the reconcile loop respawns a fresh login shell.
    old_shell_pane = machine.succeed(
        tmux('display -p -t "=scratch:" "#{pane_pid}"')
    ).strip()
    machine.succeed(tmux('send-keys -t "=scratch:" exit Enter'))
    machine.wait_until_succeeds(
        tmux('display -p -t "=scratch:" "#{pane_pid}"')
        + f" | grep . | grep -vx '{old_shell_pane}'",
        timeout=60,
    )
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm scratch'")

    # --- restart semantics: killed listed sessions come back --------------
    # Compare pane PIDs, not session ids: killing the LAST session also ends
    # the tmux server, and a fresh server restarts session-id numbering, so
    # ids can repeat. A recreated session always has a new pane process.
    old_pane = machine.succeed(tmux('display -p -t "=main:" "#{pane_pid}"')).strip()
    assert old_pane, "pane_pid of main must not be empty"
    machine.succeed(tmux("kill-session -t =main"))
    machine.wait_until_succeeds(
        tmux('display -p -t "=main:" "#{pane_pid}"') + f" | grep . | grep -vx '{old_pane}'",
        timeout=60,
    )

    # --- destroy semantics: delisted sessions stay gone -------------------
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm helper'")
    machine.fail(tmux("has-session -t =helper"))
    settle()
    machine.fail(tmux("has-session -t =helper"))
    machine.succeed(
        "jq -e '.sessions | has(\"helper\") | not' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # --- kickoff prompt: fires once, then resumes -------------------------
    with subtest("kickoff prompt is delivered once and consumed"):
        machine.succeed(
            "su -s /bin/sh agent -c "
            + shlex.quote("agent-box-session add task1 --harness codex --prompt 'do the thing'")
        )
        # Stored as initialPrompt with an id minted up front; not yet run.
        machine.succeed(
            f"jq -e '.sessions.task1.initialPrompt == \"do the thing\"' {sfile}"
        )
        machine.succeed(
            f"jq -e '.sessions.task1.hasRun == false and (.sessions.task1.boxSessionId | type == \"string\")' {sfile}"
        )
        # Once the supervisor spawns it, the prompt is consumed and the launch
        # id is recorded, so a later respawn resumes instead of re-running the
        # task. The record lives in the supervisor's own file; the registry
        # copy is the migration mirror this release still writes.
        machine.wait_until_succeeds(tmux("has-session -t =task1"), timeout=60)
        machine.wait_until_succeeds(
            f"jq -e '.launchSessionId | test(\"^[0-9a-f-]{{36}}$\")' {state_file('task1')}",
            timeout=60,
        )
        machine.wait_until_succeeds(
            f"jq -e --arg id \"$(jq -r .launchSessionId {state_file('task1')})\" "
            f"'.sessions.task1.boxSessionId == $id' {sfile}",
            timeout=60,
        )
        machine.wait_until_succeeds(
            f"jq -e '.sessions.task1.hasRun == true and .sessions.task1.initialPrompt == null' {sfile}",
            timeout=60,
        )
        # A respawn must NOT re-prime the kickoff prompt.
        machine.succeed(tmux("kill-session -t =task1"))
        settle()
        machine.succeed(
            f"jq -e '.sessions.task1.initialPrompt == null and .sessions.task1.hasRun == true' {sfile}"
        )
        # Delisting reclaims the supervisor's state for the name. `rm` prunes
        # its own as an optimisation; the sweep below is what makes it
        # reclaimable when nobody runs `rm` at all.
        machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm task1'")
        machine.fail(f"test -e {state_file('task1')}")

    with subtest("session state is swept against the registry, not on delist"):
        # Nothing guarantees a delete path ever runs — a hook agent may never
        # call `agent-box-session rm`, and a session can be delisted while the
        # unit is down — so the supervisor sweeps this directory against
        # sessions.json on every reconcile tick (issue #282). Stale state here
        # is not litter: a session name is re-usable, and the file would hand
        # the next holder of the name the dead session's launch id.
        # Written as the agent, into the directory the supervisor already
        # owns: a root-created parent would lock its own writer out of it.
        machine.succeed(
            as_agent(
                "printf '%s' "
                + shlex.quote('{"launchSessionId":"deadbeef-0000-4000-8000-000000000001"}')
                + " > " + state_file("ghost")
            )
        )
        machine.wait_until_fails(f"test -e {state_file('ghost')}", timeout=60)
        # A LISTED session's state is never swept — it is that session's
        # conversation, and being merely down (stopped, or between respawns)
        # is not being gone.
        machine.succeed(as_agent("agent-box-session add kept --harness shell"))
        machine.wait_until_succeeds(tmux("has-session -t =kept"), timeout=60)
        machine.wait_until_succeeds(f"test -e {state_file('kept')}", timeout=60)
        machine.succeed(as_agent("agent-box-session stop kept"))
        settle()
        machine.succeed(f"test -e {state_file('kept')}")
        machine.succeed(as_agent("agent-box-session rm kept"))

    # --- a variadic extraArg must not swallow the prompt ------------------
    with subtest("trailing variadic extraArg does not eat the kickoff prompt"):
        # Several claude flags take MULTIPLE values (--channels, --add-dir,
        # --allowedTools, --betas ...). With the prompt appended straight after
        # extraArgs, claude read it as one more value of the last flag and died
        # in argument parsing before the TUI started — on the office box a
        # resumed session failed with "--channels entries must be tagged: You
        # were interrupted and automatically restarted (agent-box session
        # <uuid>)", the supervisor's own resume prompt quoted back at it. The
        # supervisor now closes the option list with "--" first. --add-dir
        # stands in for --channels here: same variadic shape, but it needs no
        # plugin marketplace in the VM.
        machine.succeed(
            as_agent(
                "agent-box-session add variadic --harness claude "
                "--prompt 'do the variadic thing' -- --add-dir /home/agent"
            )
        )
        machine.wait_until_succeeds(tmux("has-session -t =variadic"), timeout=60)
        # pgrep runs as ROOT (not via as_agent): as the agent the invoking
        # shell's own command line self-matches the pattern.
        variadic_cmdline = machine.wait_until_succeeds(
            "pgrep -u agent -af add-dir", timeout=60
        )
        assert (
            "--add-dir /home/agent -- do the variadic thing" in variadic_cmdline
        ), variadic_cmdline
        machine.succeed(as_agent("agent-box-session rm variadic"))

    with subtest("codex: same guard, and it also protects the resume target"):
        # codex's one variadic option is `-i/--image <FILE>...`, so extraArgs
        # ending in it swallow the prompt exactly as --channels did for claude
        # (verified against codex 0.146.0: `codex exec -i x.png "say ok"` reads
        # NO prompt, `codex exec -i x.png -- "say ok"` reads it).
        png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42m"
               "NkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
        machine.succeed(as_agent(f"echo {png} | base64 -d > /home/agent/shot.png"))
        # Written straight into sessions.json instead of through
        # `agent-box-session add`: the CLI always writes remoteControl=true, and
        # a remote-controlled codex session runs the app-server daemon, which
        # takes no positional prompt at all — remoteControl=false is what puts
        # the prompt on a command line. One write, so the supervisor's first
        # spawn already sees the final config (no add-then-edit race).
        machine.succeed(
            as_agent(
                'jq \'.sessions.vcodex = {agent: "codex", skipPermissions: true, '
                'remoteControl: false, remoteControlName: null, '
                'workingDirectory: null, extraArgs: ["-i", "/home/agent/shot.png"], '
                'initialPrompt: "do the codex thing", resumePrompt: null, '
                f"boxSessionId: null, hasRun: false}}' {sfile} > {sfile}.t "
                f"&& mv {sfile}.t {sfile}"
            )
        )
        machine.wait_until_succeeds(tmux("has-session -t =vcodex"), timeout=60)
        # tmux records the pane's start command when the session is created, so
        # this reads what the supervisor BUILT and does not depend on codex
        # still running (a logged-out TUI may or may not stay up). The prompt is
        # printf %q-escaped on the way in and tmux escapes it again on the way
        # out, so compare with every backslash dropped rather than guessing how
        # many layers survive.
        codex_start_cmd = machine.wait_until_succeeds(
            tmux('list-panes -t "=vcodex" -F "#{pane_start_command}"'), timeout=60
        )
        unescaped = codex_start_cmd.replace("\\", "")
        assert (
            "-i /home/agent/shot.png -- [agent-box session" in unescaped
        ), codex_start_cmd
        assert "do the codex thing" in unescaped, codex_start_cmd
        machine.succeed(as_agent("agent-box-session rm vcodex"))

    with subtest("codex: skipPermissions=false survives the box-wide default"):
        # /etc/codex/config.toml makes full access the BOX default (issue 234),
        # so a session that opted out has to pin the restricted values back on
        # the command line — otherwise the system layer silently grants it
        # everything. Only the TUI arms can do this: the app-server daemon is
        # one per user and takes no per-session config.
        machine.succeed(
            as_agent(
                'jq \'.sessions.careful = {agent: "codex", skipPermissions: false, '
                'remoteControl: false, remoteControlName: null, '
                'workingDirectory: null, extraArgs: [], '
                'initialPrompt: "do the careful thing", resumePrompt: null, '
                f"boxSessionId: null, hasRun: false}}' {sfile} > {sfile}.t "
                f"&& mv {sfile}.t {sfile}"
            )
        )
        machine.wait_until_succeeds(tmux("has-session -t =careful"), timeout=60)
        careful_cmd = machine.wait_until_succeeds(
            tmux('list-panes -t "=careful" -F "#{pane_start_command}"'), timeout=60
        )
        assert "-c approval_policy=on-request" in careful_cmd, careful_cmd
        assert "-c sandbox_mode=workspace-write" in careful_cmd, careful_cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in careful_cmd, careful_cmd
        machine.succeed(as_agent("agent-box-session rm careful"))

    # --- segment rotation: respawn follows the recorded live id -----------
    with subtest("segment rotation: respawn resumes the recorded live id"):
        # A clear, a compact or a resume rotates claude's live session id
        # mid-process (issue #223): the process keeps running (and keeps its
        # Remote Control name) but opens a NEW transcript under a new uuid,
        # while the supervisor still holds the id it launched with. The
        # SessionStart hook records live-id-by-launch-id; a respawn must
        # resume the RECORDED id — not the segment the session moved off —
        # and record the adopted id for the next hop.
        #
        # Two hops, because one hop cannot tell a chain from a coincidence:
        # the record the supervisor follows on the second hop is keyed by an
        # id it ADOPTED rather than minted, so a supervisor that only ever
        # looks under its original launch id passes the first hop and fails
        # here (issue #282).
        rot_state = state_file("rot")
        machine.succeed(
            as_agent(
                "agent-box-session add rot --harness claude --prompt 'do the rotation thing'"
            )
        )
        machine.wait_until_succeeds(tmux("has-session -t =rot"), timeout=60)
        # The launch id comes from the SUPERVISOR's state file, which is where
        # it lives now — sessions.json carries the migration copy only.
        machine.wait_until_succeeds(
            f"jq -e '.launchSessionId | test(\"^[0-9a-f-]{{36}}$\")' {rot_state}",
            timeout=60,
        )
        rot_bid = machine.succeed(f"jq -r '.launchSessionId' {rot_state}").strip()
        # The spawn hands the launch id to the pane environment (the hook's
        # key) and the hook settings file to claude via --settings; recover
        # the hook script's store path from the live command line rather
        # than hardcoding it.
        machine.succeed(
            tmux('show-environment -t "=rot" AGENT_BOX_SESSION_ID')
            + f" | grep -x AGENT_BOX_SESSION_ID={rot_bid} >/dev/null"
        )
        rot_cmdline = machine.wait_until_succeeds(
            "pgrep -u agent -af agent-box-claude-hook-settings", timeout=60
        )
        settings_m = re.search(
            r"--settings (\S*agent-box-claude-hook-settings\S*)", rot_cmdline
        )
        assert settings_m, rot_cmdline
        hook = machine.succeed(
            f"jq -r '.hooks.SessionStart[0].hooks[0].command' {settings_m.group(1)}"
        ).strip()

        def rotate(launch_id, new_id):
            """One hop: drive the REAL hook exactly as claude does on a
            clear/compact/resume (SessionStart payload on stdin, launch id in
            the environment), stand in the transcript the rotation leaves
            behind, then bounce the session and return what the supervisor
            BUILT. Reading the recorded pane start command rather than a live
            process, as in the codex subtest: it must not depend on claude
            surviving its (unauthenticated) resume attempt."""
            payload = json.dumps(
                {
                    "session_id": new_id,
                    "hook_event_name": "SessionStart",
                    "source": "clear",
                }
            )
            machine.succeed(
                as_agent(
                    f"printf %s {shlex.quote(payload)} | "
                    f"AGENT_BOX_SESSION_ID={launch_id} {hook}"
                )
            )
            machine.succeed(
                f"grep -qx {new_id} "
                f"/home/agent/.local/state/agent-box/live-session-id/{launch_id}"
            )
            # claude_has_transcript gates the switch, and a real rotation
            # always leaves the new segment's file behind.
            machine.succeed(
                "install -D -o agent /dev/null "
                f"/home/agent/.claude/projects/-home-agent/{new_id}.jsonl"
            )
            machine.succeed(tmux("kill-session -t =rot"))
            return machine.wait_until_succeeds(
                tmux('list-panes -t "=rot" -F "#{pane_start_command}"'), timeout=60
            )

        # Hop 1: from the id the supervisor minted.
        first = "12345678-9abc-4def-8123-456789abcdef"
        cmd1 = rotate(rot_bid, first)
        assert f"--resume {first}" in cmd1.replace("\\", ""), cmd1
        # The rotated segment's transcript is the empty stand-in `rotate`
        # installed — no real assistant turn yet, exactly what a /clear
        # leaves behind before the next prompt lands. The respawn must not
        # claim an interruption there is nothing in that segment to back up.
        assert "You were interrupted" not in cmd1.replace("\\", ""), cmd1
        # The adopted id is recorded, so the NEXT respawn needs no record —
        # and it is recorded in the supervisor's own file. sessions.json gets
        # the migration copy for one more release.
        machine.wait_until_succeeds(
            f"jq -e '.launchSessionId == \"{first}\"' {rot_state}", timeout=60
        )
        machine.wait_until_succeeds(
            f"jq -e '.sessions.rot.boxSessionId == \"{first}\"' {sfile}", timeout=60
        )
        machine.succeed(
            tmux('show-environment -t "=rot" AGENT_BOX_SESSION_ID')
            + f" | grep -x AGENT_BOX_SESSION_ID={first} >/dev/null"
        )

        # Hop 2: from the id it ADOPTED. The newest segment wins.
        second = "22345678-9abc-4def-8123-456789abcdef"
        cmd2 = rotate(first, second)
        assert f"--resume {second}" in cmd2.replace("\\", ""), cmd2
        assert f"--resume {first}" not in cmd2.replace("\\", ""), cmd2
        machine.wait_until_succeeds(
            f"jq -e '.launchSessionId == \"{second}\"' {rot_state}", timeout=60
        )

        # And the SIDE FILE is what carries it, not the registry. Corrupt the
        # registry copy exactly as a lost update did (issue #254) — a stale id
        # and hasRun back to false, which used to mean "first spawn": new id,
        # kickoff prompt fired again, conversation orphaned. The supervisor
        # reads its own record, so the respawn still resumes the newest
        # segment.
        stale = "32345678-9abc-4def-8123-456789abcdef"
        machine.succeed(
            as_agent(
                f"jq '.sessions.rot.boxSessionId = \"{stale}\" | "
                f".sessions.rot.hasRun = false' {sfile} > {sfile}.t "
                f"&& mv {sfile}.t {sfile}"
            )
        )
        machine.succeed(tmux("kill-session -t =rot"))
        cmd3 = machine.wait_until_succeeds(
            tmux('list-panes -t "=rot" -F "#{pane_start_command}"'), timeout=60
        )
        assert f"--resume {second}" in cmd3.replace("\\", ""), cmd3
        assert stale not in cmd3, cmd3
        machine.succeed(as_agent("agent-box-session rm rot"))
        machine.fail(f"test -e {rot_state}")

    # --- restart notice is opt-in, off on this host (#507) -----------------
    with subtest("claude: no injected prompt on respawn when restartNotice is off"):
        # This host leaves services.agent-box.restartNotice at its default
        # (false, issue #507): `--resume` already restores the transcript on
        # its own, so a respawn whose transcript holds REAL work (unlike the
        # empty stand-in the segment-rotation subtest above uses, which hits
        # the earlier claude_transcript_has_work branch) must get no
        # injected prompt at all — not the built-in "you were interrupted"
        # text this option used to always stamp on.
        notice_state = state_file("notice")
        machine.succeed(
            as_agent(
                "agent-box-session add notice --agent claude "
                "--prompt 'do the notice thing'"
            )
        )
        machine.wait_until_succeeds(tmux("has-session -t =notice"), timeout=60)
        machine.wait_until_succeeds(
            f"jq -e '.launchSessionId | test(\"^[0-9a-f-]{{36}}$\")' {notice_state}",
            timeout=60,
        )
        notice_bid = machine.succeed(f"jq -r '.launchSessionId' {notice_state}").strip()
        # No "model":"<synthetic>" is what claude_transcript_has_work treats
        # as a real assistant turn, unlike the empty stand-in above.
        machine.succeed(
            "install -D -o agent /dev/null "
            f"/home/agent/.claude/projects/-home-agent/{notice_bid}.jsonl"
        )
        machine.succeed(
            "printf %s " + shlex.quote('{"type":"assistant","model":"claude-x"}')
            + " > /home/agent/.claude/projects/-home-agent/"
            f"{notice_bid}.jsonl"
        )
        machine.succeed(tmux("kill-session -t =notice"))
        notice_cmd = machine.wait_until_succeeds(
            tmux('list-panes -t "=notice" -F "#{pane_start_command}"'), timeout=60
        )
        unescaped_notice = notice_cmd.replace("\\", "")
        assert f"--resume {notice_bid}" in unescaped_notice, notice_cmd
        assert "You were interrupted" not in unescaped_notice, notice_cmd
        machine.succeed(as_agent("agent-box-session rm notice"))

    # --- env CLI writes the same file the settings page + wrapper use -----
    with subtest("env set/ls/rm on ~/.config/agent-box/env"):
        machine.succeed("su -s /bin/sh agent -c 'agent-box-session env set MY_TOKEN sekret'")
        machine.succeed("grep -qx 'MY_TOKEN=sekret' /home/agent/.config/agent-box/env")
        machine.succeed("stat -c '%a' /home/agent/.config/agent-box/env | grep -x 600")
        env_ls = machine.succeed("su -s /bin/sh agent -c 'agent-box-session env ls'")
        # ls surfaces the KEY but never the value (mirrors the settings page).
        assert "MY_TOKEN" in env_ls and "sekret" not in env_ls, env_ls
        machine.succeed("su -s /bin/sh agent -c 'agent-box-session env rm MY_TOKEN'")
        machine.fail("grep -q MY_TOKEN /home/agent/.config/agent-box/env")

    # --- a value may span lines (issue #212) ------------------------------
    with subtest("a multi-line secret survives the store, the CLI and a spawn"):
        # The value people actually need to paste here is a PEM, and it is
        # also the value every pre-#212 reader mangled: a line-per-pair loop
        # exported the first line and then read the body as more assignments.
        # The base64 line ends in `=` on purpose — that is what made a body
        # line look like one.
        pem = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEA1x==\n"
            "-----END PRIVATE KEY-----"
        )
        machine.succeed(
            "printf '%s\\n' " + shlex.quote(pem) + " > /tmp/pem && chmod a+r /tmp/pem"
        )
        # --stdin is how it gets in: the value reaches no command line, so it
        # is in no shell history and in nobody's `ps` output.
        machine.succeed(as_agent("agent-box-session env set MY_PEM --stdin < /tmp/pem"))
        # Stored as ONE quoted entry, and still 0600.
        machine.succeed(
            "grep -qx 'MY_PEM=\"-----BEGIN PRIVATE KEY-----' "
            "/home/agent/.config/agent-box/env"
        )
        machine.succeed("stat -c '%a' /home/agent/.config/agent-box/env | grep -x 600")
        # ls sees one key, not four: the reader knows where the entry ends.
        env_ls = machine.succeed(as_agent("agent-box-session env ls"))
        assert env_ls.split() == ["MY_PEM"], env_ls
        # Writing a second key must not disturb the first: writer and reader
        # agree about continuation lines, or a set corrupts what it did not
        # touch.
        machine.succeed(as_agent("agent-box-session env set PLAIN tok"))
        machine.succeed("grep -qx 'PLAIN=tok' /home/agent/.config/agent-box/env")
        assert machine.succeed(as_agent("agent-box-session env ls")).split() == [
            "MY_PEM",
            "PLAIN",
        ]
        # And the whole value reaches a session's environment at spawn, which
        # is the end of the path the settings page starts (issue 89): the
        # env-exec wrapper reads this file with the same parser that wrote it.
        machine.succeed(as_agent("agent-box-session add pemsess --harness shell"))
        machine.wait_until_succeeds(tmux("has-session -t =pemsess"), timeout=60)
        # /proc/<pid>/environ is an EXEC-TIME snapshot, and the pane command is
        # `<env-exec wrapper> <shell>` run by tmux through sh: whether the
        # loaded environment lands on the pane pid itself or on its child
        # depends on whether that sh exec'd or forked. So look at both pids,
        # and WAIT — a session that answers has-session does not yet have a
        # shell that finished exec'ing (CI 32649971831 read 49 entries with no
        # MY_PEM at all, where the run before it passed).
        pids = (
            tmux('display -p -t "=pemsess:" "#{pane_pid}"')
            + " | xargs -I{} sh -c 'echo {}; pgrep -P {} || true'"
        )
        machine.wait_until_succeeds(
            pids
            + " | xargs -I{} sh -c \"tr '\\0' '\\n' < /proc/{}/environ\""
            + " | grep -x 'MY_PEM=-----BEGIN PRIVATE KEY-----' >/dev/null",
            timeout=60,
        )
        # Then the exact entry, from whichever of those pids carries it.
        # Compare the NUL-delimited ENTRY, not a substring of a joined dump: a
        # substring match cannot tell "the value ends here" from "the value
        # continues", which is the very thing a continuation-line parser has
        # to get right. base64 keeps the NULs intact through the shell, and
        # chr(0) splits on them — "\0" here would be a literal backslash-zero,
        # because a Nix indented string passes backslashes through untouched.
        examined = machine.succeed(pids).split()
        entries = []
        for pid in examined:
            raw = machine.succeed(f"base64 -w0 < /proc/{pid}/environ || true")
            entries += base64.b64decode(raw).decode().split(chr(0))
        assert "MY_PEM=" + pem in entries, (
            f"pids {examined}, {len(entries)} entries",
            [e for e in entries if e.startswith("MY_PEM")],
        )
        machine.succeed(as_agent("agent-box-session rm pemsess"))
        machine.succeed(as_agent("agent-box-session env rm MY_PEM"))
        machine.succeed(as_agent("agent-box-session env rm PLAIN"))
        assert machine.succeed(as_agent("agent-box-session env ls")).split() == []

    # --- prepopulated profiles (issue #493) --------------------------------
    with subtest("a box arrives with one profile per installed harness"):
        # "Add session" is profile-first, so a box whose profile list is
        # empty offers nothing to start. The supervisor seeds one profile
        # per INSTALLED harness, named after it.
        pdir = "/home/agent/.config/agent-box/profiles"
        seeded_ls = machine.succeed(as_agent("agent-box-profile ls"))
        for harness in ("claude", "codex"):
            assert re.search(rf"^{harness}\s+{harness}\s", seeded_ls, re.M), seeded_ls
        # `shell` is not among them: it is a session kind, not a worker.
        machine.fail(f"test -e {pdir}/shell.env")

        # MODEL and EFFORT are EMPTY on purpose, so the harness applies its
        # own default. A value baked in at first boot would freeze that
        # default and go stale the next time a model alias moves.
        seeded = json.loads(machine.succeed(as_agent("agent-box-profile launch claude")))
        assert seeded["harness"] == "claude", seeded
        assert seeded["args"] == [], seeded

        # Deleted stays deleted. The supervisor calls `seed` on EVERY start,
        # so seeding whenever the file is absent would resurrect a profile
        # the user removed: nothing should stop somebody deleting the codex
        # profile if they never use codex. The stamp records the name.
        machine.succeed(as_agent("agent-box-profile rm codex"))
        machine.succeed(as_agent("agent-box-profile seed"))
        machine.fail(f"test -e {pdir}/codex.env")
        # And an EDIT is never clobbered, for the same reason: the profile is
        # the user's to change, by hand or by asking an agent to.
        machine.succeed(as_agent("agent-box-profile set claude MODEL=opus"))
        machine.succeed(as_agent("agent-box-profile seed"))
        edited = json.loads(machine.succeed(as_agent("agent-box-profile launch claude")))
        assert edited["args"] == ["--model", "opus"], edited
        # Deleting the stamp is the way back to the whole set.
        machine.succeed(as_agent(f"rm -f {pdir}/.seeded"))
        machine.succeed(as_agent("agent-box-profile seed"))
        machine.succeed(f"test -e {pdir}/codex.env")
        # ...but it still does not overwrite what is already there.
        again = json.loads(machine.succeed(as_agent("agent-box-profile launch claude")))
        assert again["args"] == ["--model", "opus"], again
        machine.succeed(as_agent("agent-box-profile rm claude"))
        machine.succeed(as_agent("agent-box-profile rm codex"))

    # --- agent profiles (issue #321) --------------------------------------
    with subtest("a profile resolves a worker: harness, args and session env"):
        # A profile is the WORKER (harness + model + effort + appended system
        # prompt + env), as opposed to the harness `--harness` picks. Runtime
        # data like the env store: written by the CLI, no rebuild, no root.
        machine.succeed(
            as_agent(
                "agent-box-profile set triage HARNESS=claude MODEL=sonnet "
                "EFFORT=low PROFILE_TOKEN=sekret"
            )
        )
        pfile = "/home/agent/.config/agent-box/profiles/triage.env"
        machine.succeed(f"stat -c '%a' {pfile} | grep -x 600")
        prof_ls = machine.succeed(as_agent("agent-box-profile ls"))
        assert re.search(r"^triage\s+claude\s+sonnet\s+low", prof_ls, re.M), prof_ls
        # show prints the launch config, and the env KEY without its value —
        # the same rule `agent-box-session env ls` holds (a profile is exactly
        # where a token ends up).
        prof_show = machine.succeed(as_agent("agent-box-profile show triage"))
        assert "PROFILE_TOKEN" in prof_show, prof_show
        assert "sekret" not in prof_show, prof_show
        # A harness this box does not install is refused at write time, so a
        # profile can never resolve to a session that cannot start.
        machine.fail(as_agent("agent-box-profile set triage HARNESS=nosuch"))
        # And neither is `shell` one (issue #493). It is a session KIND, not
        # a worker: it has no model, effort or system prompt to configure, so
        # a profile built around it would configure nothing. The refusal says
        # which of the two the caller wanted.
        shell_refusal = machine.fail(
            as_agent("agent-box-profile set triage HARNESS=shell 2>&1")
        )
        assert "session kind, not a worker" in shell_refusal, shell_refusal
        assert "--harness shell" in shell_refusal, shell_refusal
        # MODEL/EFFORT still map to nothing when a shell session is started
        # FROM a profile with `--harness shell`, and the resolver says so
        # rather than inventing flags for it.
        shell_launch = json.loads(
            machine.succeed(as_agent("agent-box-profile launch triage shell"))
        )
        assert shell_launch["args"] == [], shell_launch
        assert any("ignored for the 'shell' harness" in w
                   for w in shell_launch["warnings"]), shell_launch
        # But a profile whose OWN file names shell is refused at launch, not
        # just at write time. `set` cannot write one and the settings page
        # cannot save one, yet neither governs a file that predates this
        # release or was edited by hand - and the profile file is explicitly
        # the user's to edit. Launchable-but-unsaveable is the state this
        # closes.
        legacy = "/home/agent/.config/agent-box/profiles/legacy.env"
        machine.succeed(as_agent(f"printf 'HARNESS=shell\\n' > {legacy}"))
        legacy_err = machine.fail(as_agent("agent-box-profile launch legacy 2>&1"))
        assert "session kind, not a worker" in legacy_err, legacy_err
        # ...and the override is still the way to start one from it.
        machine.succeed(as_agent("agent-box-profile launch legacy shell"))
        machine.succeed(as_agent(f"rm -f {legacy}"))

        # add --profile: the harness comes from the profile, and the caller's
        # own `--` tail is appended AFTER the profile's args, so an explicit
        # flag still has the last word.
        # `--harness shell` overrides the profile's own harness, which is also
        # what keeps this session cheap: the assertions below are about the
        # profile's ENVIRONMENT reaching the pane, and a bare shell is the
        # cheapest pane to read it from.
        machine.succeed(
            as_agent("agent-box-session add worker --profile triage --harness shell")
        )
        machine.succeed(f"jq -e '.sessions.worker.agent == \"shell\"' {sfile}")
        machine.succeed(f"jq -e '.sessions.worker.profile == \"triage\"' {sfile}")
        # The profile's env reaches the session's process environment at spawn
        # — through the env-exec wrapper, keyed on the recorded profile name,
        # so a later edit applies on the next restart. It is convenience and
        # not isolation (issue #135): a sibling session reads it out of
        # /proc/<pid>/environ, which is exactly how this assertion reads it.
        machine.wait_until_succeeds(tmux("has-session -t =worker"), timeout=60)
        # Same race as the pemsess assertion above: has-session answers before
        # the pane's `<env-exec wrapper> <shell>` has necessarily finished
        # exec'ing, so the loaded environment may still be sitting on a child
        # of the pane pid rather than the pane pid itself. Poll both, exactly
        # like pemsess does, instead of reading pane_pid once.
        wpids = (
            tmux('display -p -t "=worker:" "#{pane_pid}"')
            + " | xargs -I{} sh -c 'echo {}; pgrep -P {} || true'"
        )
        machine.wait_until_succeeds(
            wpids
            + " | xargs -I{} sh -c \"tr '\\0' '\\n' < /proc/{}/environ\""
            + " | grep -x 'PROFILE_TOKEN=sekret' >/dev/null",
            timeout=60,
        )
        examined = machine.succeed(wpids).split()
        environ = ""
        for pid in examined:
            candidate = machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ || true")
            if "PROFILE_TOKEN=sekret" in candidate:
                environ = candidate
                break
        assert "PROFILE_TOKEN=sekret" in environ, (examined, environ)
        assert "AGENT_BOX_PROFILE=triage" in environ, environ
        # The reserved LAUNCH keys are NOT environment: a system prompt in the
        # environment of everything the agent shells out to is not what a
        # profile promised.
        assert "MODEL=sonnet" not in environ, environ

        # A claude profile maps the same keys onto claude's own flags, and the
        # override order holds: --harness wins over the profile's harness, and
        # the args follow the harness that actually runs.
        machine.succeed(
            as_agent(
                "agent-box-profile set reviewer HARNESS=claude MODEL=sonnet "
                "EFFORT=low SYSTEM_PROMPT='You review PRs.'"
            )
        )
        launch = json.loads(machine.succeed(as_agent("agent-box-profile launch reviewer")))
        assert launch["harness"] == "claude", launch
        # A SYSTEM_PROMPT is prose: it may span lines and it may end on a
        # blank one, and the harness must be handed exactly what the store
        # holds (issue #212). Its own profile, because `reviewer` is asserted
        # on below and must keep the prompt it was created with.
        machine.succeed(
            as_agent(
                "agent-box-profile set prosaic HARNESS=claude "
                + shlex.quote("SYSTEM_PROMPT=Review PRs.\n\nBe terse.\n\n")
            )
        )
        prose = json.loads(machine.succeed(as_agent("agent-box-profile launch prosaic")))
        assert prose["args"][-1] == "Review PRs.\n\nBe terse.\n\n", prose
        # And it survives `add --profile`, which stores the resolved argument
        # vector: a newline-separated decode turned one two-paragraph prompt
        # into three separate flags to the agent CLI.
        machine.succeed(as_agent("agent-box-session add proser --profile prosaic"))
        stored = json.loads(machine.succeed(f"cat {sfile}"))["sessions"]["proser"]
        assert stored["extraArgs"] == [
            "--append-system-prompt",
            "Review PRs.\n\nBe terse.\n\n",
        ], stored
        machine.succeed(as_agent("agent-box-session rm proser"))
        machine.succeed(as_agent("agent-box-profile rm prosaic"))
        assert launch["args"] == [
            "--model", "sonnet", "--effort", "low",
            "--append-system-prompt", "You review PRs.",
        ], launch
        codex_launch = json.loads(
            machine.succeed(as_agent("agent-box-profile launch reviewer codex"))
        )
        assert codex_launch["args"][:2] == ["-m", "sonnet"], codex_launch
        assert "-c" in codex_launch["args"], codex_launch
        assert codex_launch["warnings"], codex_launch
        # And what the session actually stores: profile args first, the
        # caller's `-- EXTRA_ARGS` tail after them (both harness CLIs take the
        # last occurrence of a flag, so the tail is what wins).
        machine.succeed(
            as_agent("agent-box-session add tailtest --profile reviewer -- --model opus")
        )
        stored = json.loads(machine.succeed(f"jq -c '.sessions.tailtest' {sfile}"))
        assert stored["agent"] == "claude", stored
        assert stored["extraArgs"] == launch["args"] + ["--model", "opus"], stored
        machine.succeed(as_agent("agent-box-session rm tailtest"))

        # rm KEY drops one key; rm with no key drops the profile, and a
        # session already running with it is unaffected.
        machine.succeed(as_agent("agent-box-profile rm triage PROFILE_TOKEN"))
        machine.fail(f"grep -q PROFILE_TOKEN {pfile}")
        machine.succeed(as_agent("agent-box-profile rm triage"))
        machine.fail(f"test -e {pfile}")
        machine.succeed(tmux("has-session -t =worker"))
        machine.fail(as_agent("agent-box-session add orphan --profile triage"))
        machine.succeed(as_agent("agent-box-session rm worker"))
        machine.succeed(as_agent("agent-box-profile rm reviewer"))

    # --- restart --all bounces every listed session -----------------------
    with subtest("restart --all"):
        old_all = machine.succeed(tmux('display -p -t "=main:" "#{pane_pid}"')).strip()
        machine.succeed("su -s /bin/sh agent -c 'agent-box-session restart --all'")
        machine.wait_until_succeeds(
            tmux('display -p -t "=main:" "#{pane_pid}"') + f" | grep . | grep -vx '{old_all}'",
            timeout=60,
        )

    # --- stop semantics (issue 167): parked, not respawned ----------------
    with subtest("clean agent exit parks the session instead of respawning"):
        # `claude --help` exits 0 — the same clean exit /quit produces, minus
        # the TUI. The pane epilogue must record stopped=true and the
        # reconcile loop must then leave the session down (before #167 it
        # respawned-and-resumed every clean exit within ~2s).
        machine.succeed(as_agent("agent-box-session add quitter -- --help"))
        machine.wait_until_succeeds(
            f"jq -e '.sessions.quitter.stopped == true' {sfile}", timeout=120
        )
        machine.wait_until_fails(tmux("has-session -t =quitter"), timeout=60)
        settle()
        machine.fail(tmux("has-session -t =quitter"))
        ls_out = machine.succeed(as_agent("agent-box-session ls"))
        assert re.search(r"^quitter\s+claude\s+stopped", ls_out, re.M), ls_out
        machine.succeed(as_agent("agent-box-session rm quitter"))

    # --- crash semantics (issue #516): a live pane with no agent in it ----
    with subtest("a crashed agent is reported as died, never as running"):
        # `claude --<unknown flag>` exits 1 — the same non-zero exit a crash
        # produces, minus the crash. What the epilogue records then is the
        # STATUS, and it must not park the session: `stopped` is the flag
        # that means somebody asked for this, and nobody did.
        machine.succeed(
            as_agent("agent-box-session add crasher -- --not-a-real-flag"))
        machine.wait_until_succeeds(
            f"jq -e '.sessions.crasher.died == 1' {sfile}", timeout=120
        )
        machine.succeed(f"jq -e '.sessions.crasher | has(\"stopped\") | not' {sfile}")
        # The pane survives, and keeping it is deliberate (#167): it is the
        # post-mortem shell. That is also exactly why every reader needed
        # telling — tmux reports this session as live, so `ls` called a dead
        # agent `live` and the settings page called it Running, which is how
        # a codex Remote Control daemon died on the deployed box with no
        # surface anywhere saying so.
        settle()
        machine.succeed(tmux("has-session -t =crasher"))
        ls_out = machine.succeed(as_agent("agent-box-session ls"))
        assert re.search(r"^crasher\s+claude\s+died\(1\)", ls_out, re.M), ls_out
        # `peers` is the surface a dispatched session reads before deciding
        # whether the work is taken, and a corpse must not read as an owner.
        peers = machine.succeed(as_agent("agent-box-session peers"))
        assert "crasher" in peers, peers
        assert "DIED (exit 1)" in peers, peers
        machine.succeed(as_agent("agent-box-session rm crasher"))

    with subtest("a spawn clears a stale died flag"):
        # The flag describes the pane that is up NOW, so a spawn must drop
        # the one it is answering — a stale flag would call a healthy session
        # dead, which is #516 with the sign flipped. Seeded by hand on a
        # session that is running fine, because an agent that keeps crashing
        # can never show the clear.
        machine.succeed(as_agent("agent-box-session add revived"))
        machine.wait_until_succeeds(tmux("has-session -t =revived"), timeout=120)
        # Under the registry lock, the same one every writer in the box
        # takes: has-session only proves tmux created the session, and the
        # supervisor can still be inside its own mark_started read-modify-
        # write. An unlocked jq-and-mv here would publish a document read
        # before that update and lose one of the two writes -- either the
        # seeded died=99 or the supervisor's (CodeRabbit on PR #522).
        machine.succeed(as_agent(
            f"flock {sfile}.lock sh -c "
            + shlex.quote(
                f"jq '.sessions.revived.died = 99' {sfile} > {sfile}.t "
                f"&& mv {sfile}.t {sfile}"
            )
        ))
        machine.succeed(f"jq -e '.sessions.revived.died == 99' {sfile}")
        machine.succeed(as_agent("agent-box-session restart revived"))
        machine.wait_until_succeeds(
            f"jq -e '.sessions.revived | has(\"died\") | not' {sfile}",
            timeout=120,
        )
        machine.wait_until_succeeds(tmux("has-session -t =revived"), timeout=60)
        ls_out = machine.succeed(as_agent("agent-box-session ls"))
        assert re.search(r"^revived\s+claude\s+live", ls_out, re.M), ls_out
        machine.succeed(as_agent("agent-box-session rm revived"))

    with subtest("a one-shot session is delisted, not parked"):
        # A hook-* session's shape: spawned --ephemeral because nobody will
        # ever resume it. Both ways of parking one must end in a DELIST, so
        # the entry cannot outlive the work (before this, a hook agent that
        # answered its event and quit stayed listed for good — the preamble
        # asking it to `rm` itself is a request to a model, not a guarantee).
        #
        # First the clean-exit path, the one a finished hook agent takes.
        # `claude --help` exits 0, exactly as /quit does minus the TUI. No
        # assertion on the intermediate stopped=true: the reap is meant to
        # follow it within one 2s tick, so only the END state is stable.
        machine.succeed(as_agent("agent-box-session add gone --ephemeral -- --help"))
        machine.wait_until_succeeds(
            f"jq -e '.sessions | has(\"gone\") | not' {sfile}", timeout=120
        )
        settle()
        machine.fail(tmux("has-session -t =gone"))
        ls_out = machine.succeed(as_agent("agent-box-session ls"))
        assert not re.search(r"^gone\s", ls_out, re.M), ls_out
        # The supervisor's per-session state went with it (issue #282), so the
        # next holder of this name cannot inherit its launch id, and with it
        # its transcript.
        machine.succeed(
            as_agent("test ! -e ~/.local/state/agent-box/session/gone.json")
        )

        # Then the `stop` path, on a session that is up rather than exiting —
        # which also pins the flag itself, racelessly.
        machine.succeed(as_agent("agent-box-session add oneshot --ephemeral"))
        machine.wait_until_succeeds(tmux("has-session -t =oneshot"), timeout=60)
        machine.succeed(f"jq -e '.sessions.oneshot.ephemeral == true' {sfile}")
        stop_out = machine.succeed(as_agent("agent-box-session stop oneshot"))
        # The message must not promise a `restart` that will never come.
        assert "one-shot" in stop_out, stop_out
        machine.wait_until_succeeds(
            f"jq -e '.sessions | has(\"oneshot\") | not' {sfile}", timeout=120
        )

    with subtest("a plain session is still parked, not reaped"):
        # The reap must key on --ephemeral alone. A named session parked by
        # the very same flag keeps its entry, because its boxSessionId is the
        # only record mapping the name to a conversation `restart` can resume.
        machine.succeed(as_agent("agent-box-session add keeper -- --help"))
        machine.wait_until_succeeds(
            f"jq -e '.sessions.keeper.stopped == true' {sfile}", timeout=120
        )
        settle()
        machine.succeed(f"jq -e '.sessions | has(\"keeper\")' {sfile}")
        machine.succeed(as_agent("agent-box-session rm keeper"))

    with subtest("stop parks a listed session; restart revives it"):
        machine.succeed(as_agent("agent-box-session stop main"))
        machine.succeed(f"jq -e '.sessions.main.stopped == true' {sfile}")
        machine.wait_until_fails(tmux("has-session -t =main"), timeout=60)
        settle()
        machine.fail(tmux("has-session -t =main"))
        # restart clears the flag and the supervisor brings it back.
        machine.succeed(as_agent("agent-box-session restart main"))
        machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=60)
        machine.succeed(
            f"jq -e '.sessions.main | has(\"stopped\") | not' {sfile}"
        )


    # --- two writers of sessions.json (issue #254, tests from #285) --------
    # Every writer of the registry rewrites the WHOLE document through
    # tmp+rename. That makes a reader safe and says nothing about the interval
    # between a writer's read and its rename, so before the sidecar lock a
    # second writer's edit was reverted wholesale. The suite had no test with
    # two writers at all (#285), which is why #254 lived on master.
    #
    # Both cases below run their timing-critical steps INSIDE the VM, in one
    # shell: a driver round trip per step is tens of milliseconds, and the
    # windows being raced are single-digit milliseconds wide.
    with subtest("a delete racing the first spawn is never resurrected"):
        # `rm` delists and kills; the supervisor is meanwhile finishing that
        # session's first spawn and rewrites the entry to record hasRun, the
        # box session id and the consumed prompt. Landing the delete inside
        # that read-modify-write used to republish the deleted entry (or, with
        # the delete already applied, CREATE it again as a stub — jq's `|=`
        # assigns through a missing key). Either way the name is listed with
        # nothing live, so the reconcile loop starts it again, forever, and no
        # delete path knows it exists.
        #
        # shell sessions, not claude: the bookkeeping being raced is
        # agent-independent, and 20 claude starts would only add memory
        # pressure to a 2 GB VM. --prompt stays because the consumed prompt is
        # one of the fields the racing write publishes.
        race_script = r"""
        set -u
        sfile=/home/agent/.config/agent-box/sessions.json
        export TMUX_TMPDIR=/run/agent-box-agent
        for i in $(seq 1 20); do
          agent-box-session add racer --harness shell --prompt 'race me' >/dev/null \
            || { echo "iteration $i: add failed (a lost update dropped it?)" >&2; exit 1; }
          # Busy-wait, no sleep: the window opens when the pane appears and is
          # a few milliseconds wide, so the delete has to leave immediately.
          # Bounded only so a session that never starts reports itself instead
          # of spinning until the driver's own timeout.
          spins=0
          until tmux -L agent-box has-session -t =racer 2>/dev/null; do
            spins=$((spins + 1))
            [ $spins -lt 4000 ] \
              || { echo "iteration $i: racer never started" >&2; exit 1; }
          done
          # Sweep the offset instead of hoping one lands right: the supervisor
          # re-checks the file, then rewrites the entry, and which of those two
          # the delete falls into decides WHICH bug it hits (a republished stale
          # document, or an entry created from nothing by jq's assignment).
          # 5 ms steps out to 95 ms cross both on some iteration.
          sleep $(printf '0.%03d' $(( (i - 1) * 5 )))
          agent-box-session rm racer >/dev/null || exit 1
          # Long enough for a racing rewrite to land (milliseconds) — a
          # resurrected entry then stays, because the reconcile loop only ever
          # adds sessions back.
          sleep 1
          if jq -e '.sessions | has("racer")' "$sfile" >/dev/null; then
            echo "iteration $i: racer came back after rm:" >&2
            jq -c '.sessions.racer' "$sfile" >&2
            exit 1
          fi
        done
        # A resurrected entry is respawned within ~2s, so settle past a few
        # ticks and check the tmux side too.
        sleep 6
        if jq -e '.sessions | has("racer")' "$sfile" >/dev/null; then
          echo "racer is listed again after settling" >&2
          exit 1
        fi
        if tmux -L agent-box has-session -t =racer 2>/dev/null; then
          echo "racer is live again after settling" >&2
          exit 1
        fi
        echo "20 add/rm races, no resurrect"
        """
        assert "no resurrect" in machine.succeed(as_agent(race_script))

    with subtest("Start on one session cannot revert another's first spawn"):
        # The worse consequence of a lost update: the reverted document is a
        # whole document. Pressing Start (POST /sessions/restart) clears one
        # flag on one entry, but republishing it over a supervisor rewrite put
        # back another session's hasRun=false and its already-consumed
        # initialPrompt — while that session was RUNNING. Nothing looks wrong
        # until it next dies, when the supervisor treats it as a first spawn:
        # new id, kickoff prompt fired a second time, previous transcript
        # orphaned.
        #
        # Six sessions are first-spawning per round (two rounds) so there are
        # twelve victim rewrites to hit, and the Start storm runs at the same
        # time — one victim's odds are not a test, twelve of them are. Each POST
        # needs the stopped flag present to write at all (the route is a no-op
        # without it), so every worker re-parks the session first — which makes
        # the storm the two real user actions it imitates: Start in the browser
        # and `agent-box-session stop` in a terminal.
        machine.succeed(as_agent("agent-box-session add parked --harness shell"))
        machine.wait_until_succeeds(tmux("has-session -t =parked"), timeout=60)
        machine.succeed(as_agent("agent-box-session stop parked"))
        lost_update_script = r"""
        set -u
        sfile=/home/agent/.config/agent-box/sessions.json
        sock=/run/agent-box-settings/agent.sock
        export TMUX_TMPDIR=/run/agent-box-agent
        names="lu1 lu2 lu3 lu4 lu5 lu6"
        for round in 1 2; do
          # The daemon is a ThreadingHTTPServer, so concurrent POSTs overlap
          # each other as well as the supervisor.
          for w in 1 2 3; do
            ( r=0
              while [ $r -lt 20 ]; do
                r=$((r + 1))
                agent-box-session stop parked >/dev/null 2>&1 || true
                curl -s -o /dev/null --max-time 30 --unix-socket "$sock" \
                  -d name=parked http://localhost/sessions/restart || true
              done ) &
          done
          for n in $names; do
            agent-box-session add $n --harness shell --prompt "kickoff $n" >/dev/null \
              || { echo "round $round: add $n failed (a lost update dropped it?)" >&2; exit 1; }
          done
          for n in $names; do
            waits=0
            until tmux -L agent-box has-session -t "=$n" 2>/dev/null; do
              waits=$((waits + 1))
              [ $waits -lt 300 ] \
                || { echo "round $round: $n never started" >&2; exit 1; }
              sleep 0.2
            done
          done
          wait
          # The supervisor records the spawn right after creating the pane;
          # settle past that before reading the result.
          sleep 3
          for n in $names; do
            if ! jq -e --arg n "$n" \
                 '.sessions[$n] | .hasRun == true and .initialPrompt == null' \
                 "$sfile" >/dev/null; then
              echo "round $round: $n lost its first-spawn record:" >&2
              jq -c --arg n "$n" '.sessions[$n]' "$sfile" >&2
              exit 1
            fi
          done
          for n in $names; do
            agent-box-session rm $n >/dev/null || exit 1
          done
        done
        echo "2 rounds of 6 first spawns under a Start storm, no lost update"
        """
        assert "no lost update" in machine.succeed(as_agent(lost_update_script))
        machine.succeed(as_agent("agent-box-session rm parked"))

    # --- a registry that does not parse heals itself (issue #279) ----------
    # Both halves of the box used to read "cannot parse" as "there are no
    # sessions". The reconcile loop sent jq's error to /dev/null, so it
    # iterated over nothing, logged nothing and left the unit `active
    # (running)`: no session started and the box looked idle. The seed could
    # not repair it either, because it re-seeds only a MISSING OR EMPTY file.
    #
    # LAST in this file, deliberately: healing re-seeds the DECLARED set, so
    # anything added at runtime above would go with it.
    with subtest("an unreadable sessions.json is moved aside and rebuilt"):
        since = machine.succeed("date '+%Y-%m-%d %H:%M:%S'").strip()
        # Corrupt it and end the pane in one step, with the unit left
        # RUNNING: what is under test is the reconcile loop, not the startup
        # path, because corruption arrives while a box is up.
        machine.succeed(as_agent(
            f"printf 'not json\\n' > {sfile}; "
            "env TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box "
            # `|| true`: main may be the only session left by this point in
            # the file, and killing the last one ends the tmux server, which
            # tmux can report back as a failure. What matters is that the
            # pane is gone.
            "kill-session -t =main || true"
        ))
        # The box comes back on its own, with no operator and no restart.
        machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=120)
        machine.succeed(f"jq -e '.sessions.main' {sfile} >/dev/null")
        # The bad file is KEPT, never deleted: it is the only record of what
        # the operator had asked this box to run.
        kept = machine.succeed(as_agent(f"ls {sfile}.corrupt-*")).split()[0]
        assert "not json" in machine.succeed(as_agent(f"cat {kept}")), kept
        # And the journal names it, which is all an operator staring at an
        # idle box has to go on. NOT called `log`: the test driver already
        # binds that name to its own AbstractLogger, and the driver's type
        # check refuses the shadowing at BUILD time, before any VM boots.
        journal = machine.succeed(
            "journalctl -u agent-box@agent.service --no-pager "
            f"--since '{since}'"
        )
        assert sfile in journal, journal
        assert "does not parse" in journal, journal
        machine.succeed(as_agent(f"rm -f {sfile}.corrupt-*"))

  '';
}
