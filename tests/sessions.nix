# VM test for issue #59: sessions are runtime data, decoupled from linux
# users. One hardened unit per USER supervises tmux sessions declared in the
# user-owned ~/.config/agent-box/sessions.json. Exercises:
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
#   - both agent CLIs installed regardless of what sessions run
#     (installAgents default),
#   - the instruction files each harness reads (issue #305): the editable
#     AGENTS.md plus, for a claude session only, the project-scope and
#     user-scope CLAUDE.md pointers — seeded IFF absent, never clobbering,
#     and skipped for codex (reads AGENTS.md natively) and shell sessions,
#   - browser tmux clients advertise OSC 8 support, preserving a long hidden
#     hyperlink target when its visible URL wraps across terminal rows (#18),
#   - the tabbed terminal workspace at /<user>/ (the settings daemon in
#     AGENT_BOX_HOME mode for the primary web user; issue 119) — one tab per
#     session, panes iframing each session's own path, server-side ?tab=
#     selection — the vhost root redirecting into it, and its /sessions/*
#     CRUD routes, all behind auth — nothing on the vhost is served
#     unauthenticated anymore (the old picker and its public sessions.json
#     are gone),
#   - session CRUD on the settings page (back=settings redirects there),
#   - the live session feed both pages follow (Server-Sent Events through
#     Caddy, plus the one-shot fingerprint its no-stream fallback polls),
#   - ttyd running with --url-arg, which is what /<user>/<session>/ is
#     rewritten onto (this test swaps the Caddyfile, so the rewrite itself is
#     the session-route eval check's job; here it is the LINKS that matter).
#
# Like the other tests, lib.mkForce-swaps the module Caddyfile for a minimal
# `tls internal` one (no ACME in the sandbox) that keeps the same routing
# shape: /agent/settings* and the root catch-all both inside the auth gate,
# proxied to the settings daemon's unix socket.
{ agent-box }:
{
  name = "agent-box-sessions";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, lib, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    environment.systemPackages = [ pkgs.curl pkgs.jq ];
    services.agent-box = {
      enable = true;
      agent = "claude";
      # Leave the host label unset so auto-derived Remote Control names fall
      # back to the public web.domain rather than the internal kernel
      # hostname (issue: derived names showed the internal EC2 fqdn).
      remoteControlHost = "";
      users.agent = {
        web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
      };
      web = {
        enable = true;
        domain = "box.test";
        user = "agent";
        fail2ban = false;
      };
    };
    system.stateVersion = "25.05";

    system.activationScripts.agent-web-password-hash.text = ''
      install -d -m 0700 /var/lib/agent-box-web
      if [ ! -s /var/lib/agent-box-web/password-hash ]; then
        (
          umask 077
          ${pkgs.caddy}/bin/caddy hash-password --plaintext testpassword \
            > /var/lib/agent-box-web/password-hash
        )
        chmod 0600 /var/lib/agent-box-web/password-hash
      fi
    '';

    services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
      box.test {
        log
        tls internal
        handle /agent/settings* {
          route {
            basic_auth bcrypt agent {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            reverse_proxy unix//run/agent-box-settings/agent.sock
          }
        }
        handle {
          route {
            basic_auth bcrypt agent {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            reverse_proxy unix//run/agent-box-settings/agent.sock
          }
        }
      }
    '');
  };

  nodes.client = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.curl ];
  };

  testScript = ''
    import base64
    import json
    import re
    import shlex

    start_all()
    machine.wait_for_unit("agent-box-agent.service")
    machine.wait_for_unit("agent-box-settings-agent.service")
    machine.wait_for_unit("caddy.service")
    client.wait_for_unit("multi-user.target")

    def as_agent(cmd):
        return "su -s /bin/sh agent -c " + shlex.quote(cmd)

    def tmux(cmd):
        # Run a tmux command as the agent user against its own server (the
        # socket lives under the agent unit's RuntimeDirectory, not /tmp).
        return as_agent(
            "env TMUX_TMPDIR=/run/agent-box-agent "
            "tmux -L agent-box " + cmd
        )

    # --- first boot: legacy options seeded a "main" session --------------
    machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=120)
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
    # (the CI hang on this PR's first three runs).
    start_script = machine.succeed(
        "systemctl show agent-box-agent --property=ExecStart --value "
        "| grep -o '/nix/store/[^ ;]*-agent-box-supervisor' | head -n1"
    ).strip()
    machine.succeed(
        "systemctl show agent-box-agent -p Environment --value "
        "| grep -F 'AGENT_BOX_HOST_LABEL=box.test' >/dev/null"
    )
    machine.succeed(f"grep -qF 'rcname=$USER-$sname' {start_script}")

    # Both agent CLIs are installed even though no session uses codex yet
    # (installAgents defaults to all supported agents).
    machine.succeed("test -x /run/current-system/sw/bin/claude")
    machine.succeed("test -x /run/current-system/sw/bin/codex")
    machine.succeed("su -s /bin/sh agent -c 'test -x /home/agent/.codex/packages/standalone/current/codex'")
    machine.succeed("test -x /run/current-system/sw/bin/bwrap")
    # Tools agents assume exist resolve by bare name in agent tool shells.
    # Assert against the UNIT's PATH, not via as_agent(): that runs under su,
    # which gets the full system path and would pass even when the unit PATH
    # is missing them (the bug this guards against).
    unit_path = machine.succeed(
        "systemctl show agent-box-agent -p Environment --value"
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
    machine.succeed("systemctl cat agent-box-agent | grep -- '-gh-' >/dev/null")

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
    with subtest("seeded AGENTS.md and the two claude CLAUDE.md pointers"):
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
            "agent-box-session add keeper --agent claude --cwd /home/agent/keep"
        ))
        machine.wait_until_succeeds(tmux("has-session -t =keeper"), timeout=60)
        machine.succeed("grep -Fx MINE-AGENTS /home/agent/keep/AGENTS.md >/dev/null")
        machine.succeed("grep -Fx MINE-CLAUDE /home/agent/keep/CLAUDE.md >/dev/null")
        machine.succeed(as_agent("agent-box-session rm keeper"))

        # A codex session gets AGENTS.md alone — codex reads that name
        # natively, so a CLAUDE.md beside it would be dead weight.
        machine.succeed(as_agent("mkdir -p /home/agent/cxdir"))
        machine.succeed(as_agent(
            "agent-box-session add cx --agent codex --cwd /home/agent/cxdir"
        ))
        machine.wait_until_succeeds("test -f /home/agent/cxdir/AGENTS.md", timeout=60)
        machine.fail("test -e /home/agent/cxdir/CLAUDE.md")
        machine.succeed(as_agent("agent-box-session rm cx"))

        # A shell session gets neither: no agent there reads them.
        machine.succeed(as_agent("mkdir -p /home/agent/shdir"))
        machine.succeed(as_agent(
            "agent-box-session add sh --agent shell --cwd /home/agent/shdir"
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

        # The theme seed never clobbers a hand-picked one: /theme writes the
        # same key, and the seeder re-runs on every session start.
        machine.succeed(as_agent(
            "jq '.theme = \"light\"' /home/agent/.claude/settings.json"
            " > /home/agent/s.tmp && mv /home/agent/s.tmp"
            " /home/agent/.claude/settings.json"
        ))
        machine.succeed(as_agent("agent-box-session add themed --agent claude"))
        machine.wait_until_succeeds(tmux("has-session -t =themed"), timeout=60)
        machine.succeed(
            "jq -e '.theme == \"light\"' /home/agent/.claude/settings.json"
        )
        machine.succeed(as_agent("agent-box-session rm themed"))

    # --- runtime add: no sudo, no rebuild ---------------------------------
    machine.succeed(
        "su -s /bin/sh agent -c 'agent-box-session add helper --agent codex'"
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
    # the sandbox. The sleeps hold stdin open while the server answers; feeding
    # both lines at once EOFs it before it replies.
    init = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"clientInfo": {"name": "agent-box-test", "version": "0"}},
    })
    start = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "thread/start",
        "params": {"ephemeral": True, "cwd": "/home/agent"},
    })
    thread = machine.succeed(as_agent(
        "{ printf '%s\\n' " + shlex.quote(init)
        + "; sleep 3; printf '%s\\n' " + shlex.quote(start)
        + "; sleep 10; } | codex app-server --listen stdio:// 2>/dev/null"
    ))
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
    assert "--agent shell" not in full_pane, full_pane
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
        "su -s /bin/sh agent -c 'agent-box-session add helper --agent claude'"
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
    machine.succeed(f"grep -q agent-box-agent.service /proc/{server_pid}/cgroup")

    # ls shows both sessions with their agents.
    listing = machine.succeed("su -s /bin/sh agent -c 'agent-box-session ls'")
    assert "main" in listing and "helper" in listing, listing
    assert "codex" in listing, listing

    # --- auto-named add: no NAME → derived from the agent -----------------
    # First codex-derived name is the bare agent name (no session is literally
    # "codex" yet — "helper" runs codex but under its own name).
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session add --agent codex'")
    machine.wait_until_succeeds(tmux("has-session -t =codex"), timeout=60)
    machine.succeed(
        "jq -e '.sessions.codex.agent == \"codex\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )
    # A second codex-derived name collides with "codex", so it gets a short
    # random suffix ("codex-XXXX") — a distinct, valid session name.
    machine.succeed("su -s /bin/sh agent -c 'agent-box-session add --agent codex'")
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
        "su -s /bin/sh agent -c 'agent-box-session add scratch --agent shell'"
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
    machine.succeed("sleep 6")  # a few supervisor ticks
    machine.fail(tmux("has-session -t =helper"))
    machine.succeed(
        "jq -e '.sessions | has(\"helper\") | not' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # --- kickoff prompt: fires once, then resumes -------------------------
    sfile = "/home/agent/.config/agent-box/sessions.json"

    def state_file(name):
        """The supervisor's own record for one session (issue #282):
        sessions.json is intent, this is what the supervisor observed. Spelled
        here the way modules/src/supervisor.sh's session_state_file spells
        it — one accessor per program, so re-keying it later (issue #284)
        stays a small change."""
        return f"/home/agent/.local/state/agent-box/session/{name}.json"
    with subtest("kickoff prompt is delivered once and consumed"):
        machine.succeed(
            "su -s /bin/sh agent -c "
            + shlex.quote("agent-box-session add task1 --agent codex --prompt 'do the thing'")
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
        machine.succeed("sleep 6")
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
        machine.succeed(as_agent("agent-box-session add kept --agent shell"))
        machine.wait_until_succeeds(tmux("has-session -t =kept"), timeout=60)
        machine.wait_until_succeeds(f"test -e {state_file('kept')}", timeout=60)
        machine.succeed(as_agent("agent-box-session stop kept"))
        machine.succeed("sleep 6")
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
                "agent-box-session add variadic --agent claude "
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
                "agent-box-session add rot --agent claude --prompt 'do the rotation thing'"
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
            + f" | grep -qx AGENT_BOX_SESSION_ID={rot_bid}"
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
            + f" | grep -qx AGENT_BOX_SESSION_ID={first}"
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
        machine.succeed(as_agent("agent-box-session add pemsess --agent shell"))
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

    # --- agent profiles (issue #321) --------------------------------------
    with subtest("a profile resolves a worker: harness, args and session env"):
        # A profile is the WORKER (harness + model + effort + appended system
        # prompt + env), as opposed to the harness `--agent` picks. Runtime
        # data like the env store: written by the CLI, no rebuild, no root.
        machine.succeed(
            as_agent(
                "agent-box-profile set triage HARNESS=shell MODEL=sonnet "
                "EFFORT=low PROFILE_TOKEN=sekret"
            )
        )
        pfile = "/home/agent/.config/agent-box/profiles/triage.env"
        machine.succeed(f"stat -c '%a' {pfile} | grep -x 600")
        prof_ls = machine.succeed(as_agent("agent-box-profile ls"))
        assert re.search(r"^triage\s+shell\s+sonnet\s+low", prof_ls, re.M), prof_ls
        # show prints the launch config, and the env KEY without its value —
        # the same rule `agent-box-session env ls` holds (a profile is exactly
        # where a token ends up).
        prof_show = machine.succeed(as_agent("agent-box-profile show triage"))
        assert "PROFILE_TOKEN" in prof_show, prof_show
        assert "sekret" not in prof_show, prof_show
        # A harness this box does not install is refused at write time, so a
        # profile can never resolve to a session that cannot start.
        machine.fail(as_agent("agent-box-profile set triage HARNESS=nosuch"))
        # MODEL/EFFORT map to nothing on a shell session, and the CLI says so
        # rather than inventing flags for it.
        assert "ignored for the 'shell' harness" in prof_show, prof_show

        # add --profile: the harness comes from the profile, and the caller's
        # own `--` tail is appended AFTER the profile's args, so an explicit
        # flag still has the last word.
        machine.succeed(as_agent("agent-box-session add worker --profile triage"))
        machine.succeed(f"jq -e '.sessions.worker.agent == \"shell\"' {sfile}")
        machine.succeed(f"jq -e '.sessions.worker.profile == \"triage\"' {sfile}")
        # The profile's env reaches the session's process environment at spawn
        # — through the env-exec wrapper, keyed on the recorded profile name,
        # so a later edit applies on the next restart. It is convenience and
        # not isolation (issue #135): a sibling session reads it out of
        # /proc/<pid>/environ, which is exactly how this assertion reads it.
        machine.wait_until_succeeds(tmux("has-session -t =worker"), timeout=60)
        wpid = machine.wait_until_succeeds(
            tmux('display -p -t "=worker:" "#{pane_pid}"') + " | grep .", timeout=60
        ).strip()
        environ = machine.succeed(f"tr '\\0' '\\n' < /proc/{wpid}/environ")
        assert "PROFILE_TOKEN=sekret" in environ, environ
        assert "AGENT_BOX_PROFILE=triage" in environ, environ
        # The reserved LAUNCH keys are NOT environment: a system prompt in the
        # environment of everything the agent shells out to is not what a
        # profile promised.
        assert "MODEL=sonnet" not in environ, environ

        # A claude profile maps the same keys onto claude's own flags, and the
        # override order holds: --agent wins over the profile's harness, and
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
        machine.succeed("sleep 6")  # a few supervisor ticks
        machine.fail(tmux("has-session -t =quitter"))
        ls_out = machine.succeed(as_agent("agent-box-session ls"))
        assert re.search(r"^quitter\s+claude\s+stopped", ls_out, re.M), ls_out
        machine.succeed(as_agent("agent-box-session rm quitter"))

    with subtest("stop parks a listed session; restart revives it"):
        machine.succeed(as_agent("agent-box-session stop main"))
        machine.succeed(f"jq -e '.sessions.main.stopped == true' {sfile}")
        machine.wait_until_fails(tmux("has-session -t =main"), timeout=60)
        machine.succeed("sleep 6")  # a few supervisor ticks
        machine.fail(tmux("has-session -t =main"))
        # restart clears the flag and the supervisor brings it back.
        machine.succeed(as_agent("agent-box-session restart main"))
        machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=60)
        machine.succeed(
            f"jq -e '.sessions.main | has(\"stopped\") | not' {sfile}"
        )

    # --- web surface -------------------------------------------------------
    machine_ip = machine.succeed("ip -4 -o addr show eth1 | head -1").split()[3].split("/")[0]
    curl = f"curl -sk --resolve box.test:443:{machine_ip}"

    # NOTHING on the vhost is public anymore: the root session manager
    # challenges for auth, and the old public sessions.json is gone (its
    # path now falls into the auth-gated catch-all).
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' https://box.test/ | grep -x 401"
    )
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/sessions.json | grep -x 401"
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
        "https://box.test/sessions/delete | grep -x 303"
    )
    machine.succeed("sleep 6")
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
        "https://box.test/sessions/add | grep -x 303"
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
        "-d 'name=claude' https://box.test/sessions/delete | grep -x 303"
    )
    machine.succeed("sleep 6")
    machine.fail(tmux("has-session -t =claude"))

    # Session CRUD is rejected without credentials.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "-d 'agent=claude' "
        "https://box.test/sessions/add | grep -x 401"
    )

    # The session CRUD routes stay at the root for the primary user (the
    # old settings-path routes remain gone)...
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'agent=claude' "
        "https://box.test/agent/settings/sessions/add | grep -x 404"
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

        # The terminal dead end names the verb that actually revives it.
        # Print the wrapper's path (see the grep -o note below) — running
        # the substitution as the command would run the WRAPPER instead.
        attach = machine.succeed(
            "{ systemctl show agent-web-terminal-agent --property=ExecStart "
            "--value | grep -o '/nix/store/[^ ]*-agent-box-attach' "
            "|| echo /missing; } | head -n1"
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

    # ttyd serves per-session deep links: the unit runs with --url-arg.
    machine.succeed("systemctl cat agent-web-terminal-agent | grep -- --url-arg >/dev/null")
    # The attach script is the shared agent-box-attach since issue #154
    # Phase 2. `grep -o ... || echo missing`: an empty substitution would
    # leave `grep -q` reading stdin — the backdoor shell then hangs the whole
    # test until the CI timeout (exactly how the rename was first caught).
    machine.succeed(
        "grep -q -- '-T hyperlinks' "
        "$({ systemctl show agent-web-terminal-agent --property=ExecStart --value "
        "| grep -o '/nix/store/[^ ]*-agent-box-attach' || echo /missing; } | head -n1)"
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
                "https://box.test/sessions/add | grep -x 400"
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
            "https://box.test/sessions/add | grep -x 303"
        )
        machine.succeed(
            "jq -e '.sessions.claude.workingDirectory == \"/home/agent/work/repo\"' "
            "/home/agent/.config/agent-box/sessions.json"
        )
        machine.wait_until_succeeds(tmux("has-session -t =claude"), timeout=60)
        machine.wait_until_succeeds(
            tmux('display -p -t "=claude:" "#{pane_current_path}"')
            + " | grep -x /home/agent/work/repo",
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
            "https://box.test/sessions/add | grep -x 303"
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

        # Clean up so the migration subtest starts from a known session set.
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null "
            "-d 'name=claude' https://box.test/sessions/delete"
        )
        machine.succeed("sleep 6")
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
            "https://box.test/sessions/events | grep -x 401"
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
            the agent uid — the rotation subtest above seeded this same tree
            with `install -D`, which leaves the parent dirs root-owned, so a
            `su agent` redirect into them is EACCES. Only readability by the
            settings daemon (which runs as the user) matters here."""
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
                f"'https://box.test/sessions/transcript?name={bad}' | grep -x 404"
            )
        client.succeed(
            f"{curl} -o /dev/null -w '%{{http_code}}' "
            "'https://box.test/sessions/transcript?name=main' | grep -x 401"
        )

        machine.succeed(as_agent("agent-box-session rm shelltest"))

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
          agent-box-session add racer --agent shell --prompt 'race me' >/dev/null \
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
        machine.succeed(as_agent("agent-box-session add parked --agent shell"))
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
            agent-box-session add $n --agent shell --prompt "kickoff $n" >/dev/null \
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

  '';
}
