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
#   - browser tmux clients advertise OSC 8 support, preserving a long hidden
#     hyperlink target when its visible URL wraps across terminal rows (#18),
#   - the root tabbed terminal workspace (the settings daemon in
#     AGENT_BOX_HOME mode for the primary web user; issue 119) — one tab per
#     session, panes iframing the per-session ttyd URLs, server-side ?tab=
#     selection — and its /sessions/* CRUD routes, all behind auth — nothing
#     on the vhost is served unauthenticated anymore (the old picker and its
#     public sessions.json are gone),
#   - session CRUD on the settings page (back=settings redirects there),
#   - the live session feed both pages follow (Server-Sent Events through
#     Caddy, plus the one-shot fingerprint its no-stream fallback polls),
#   - ttyd running with --url-arg so /<user>/?arg=<session> deep links work.
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
        # Once the supervisor spawns it, the prompt is consumed and hasRun set,
        # so a later respawn resumes instead of re-running the task.
        machine.wait_until_succeeds(tmux("has-session -t =task1"), timeout=60)
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
        machine.succeed("su -s /bin/sh agent -c 'agent-box-session rm task1'")

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

    # --- /clear rotation: respawn follows the recorded live id ------------
    with subtest("/clear rotation: respawn resumes the recorded live id"):
        # /clear rotates claude's live session id mid-process (issue #223):
        # the process keeps running (and keeps its Remote Control name) but
        # writes a NEW transcript under a new uuid, while sessions.json
        # still holds the launch id. The SessionStart hook records
        # live-id-by-launch-id; a respawn must resume the RECORDED id — not
        # resurrect the pre-/clear transcript — and persist the adopted id.
        machine.succeed(
            as_agent(
                "agent-box-session add rot --agent claude --prompt 'do the rotation thing'"
            )
        )
        machine.wait_until_succeeds(tmux("has-session -t =rot"), timeout=60)
        machine.wait_until_succeeds(
            f"jq -e '.sessions.rot.hasRun == true' {sfile}", timeout=60
        )
        rot_bid = machine.succeed(
            f"jq -r '.sessions.rot.boxSessionId' {sfile}"
        ).strip()
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
        # Drive the REAL hook exactly as claude does on /clear: SessionStart
        # payload on stdin, launch id in the environment.
        rotated = "12345678-9abc-4def-8123-456789abcdef"
        payload = json.dumps(
            {"session_id": rotated, "hook_event_name": "SessionStart", "source": "clear"}
        )
        machine.succeed(
            as_agent(
                f"printf %s {shlex.quote(payload)} | "
                f"AGENT_BOX_SESSION_ID={rot_bid} {hook}"
            )
        )
        machine.succeed(
            f"grep -qx {rotated} "
            f"/home/agent/.local/state/agent-box/live-session-id/{rot_bid}"
        )
        # The rotated transcript must exist (claude_has_transcript gates the
        # switch) — a real /clear always leaves one behind.
        machine.succeed(
            "install -D -o agent /dev/null "
            f"/home/agent/.claude/projects/-home-agent/{rotated}.jsonl"
        )
        machine.succeed(tmux("kill-session -t =rot"))
        # As in the codex subtest, read what the supervisor BUILT from the
        # recorded pane start command — it must not depend on claude
        # surviving its (unauthenticated) resume attempt.
        rot_start_cmd = machine.wait_until_succeeds(
            tmux('list-panes -t "=rot" -F "#{pane_start_command}"'), timeout=60
        )
        assert f"--resume {rotated}" in rot_start_cmd.replace("\\", ""), rot_start_cmd
        # The adopted id is persisted, so the NEXT respawn needs no record.
        machine.wait_until_succeeds(
            f"jq -e '.sessions.rot.boxSessionId == \"{rotated}\"' {sfile}",
            timeout=60,
        )
        machine.succeed(as_agent("agent-box-session rm rot"))

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

    # The root page (behind auth) is the tabbed terminal workspace (issue
    # 119): a tab per session, the selected (live) session's pane iframing
    # its ttyd deep link. Never a session's argv/cwd/env (may hold secrets).
    root_page = client.succeed(f"{curl} -u agent:testpassword https://box.test/")
    assert 'id="tab-bar"' in root_page, root_page
    assert 'data-tab="main" href="/?tab=main" aria-current="page"' in root_page, root_page
    assert 'src="/agent/?arg=main"' in root_page, root_page
    assert "workingDirectory" not in root_page, root_page

    # The add/delete banner is page-level feedback, so it renders ABOVE the
    # tab bar (issue 188) — below the tabs it read as a message from the
    # terminal in the pane underneath it. An empty slot when there is no
    # message, so the tabs stay flush with the top of the viewport.
    assert '<div id="msg-slot"></div>' in root_page, root_page
    ok_page = client.succeed(
        f"{curl} -u agent:testpassword 'https://box.test/?ok=session_added'"
    )
    assert "Session added" in ok_page, ok_page
    assert ok_page.index('class="msg"') < ok_page.index('id="tab-bar"'), ok_page

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
    # the bare-agent-name tab. Assert the raw Location header (h2 lowercases
    # it, CRLF line ends — no grep -x): what the daemon EMITS is the contract,
    # curl's %{redirect_url} resolution is not. Any submitted "name" field is
    # ignored, so passing a bogus one changes nothing.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -D - "
        "-d 'name=ignored&agent=claude' "
        "https://box.test/sessions/add "
        "| grep -i '^location: /?ok=session_added&tab=claude'"
    )
    machine.wait_until_succeeds(tmux("has-session -t =claude"), timeout=60)
    machine.succeed(
        "jq -e '.sessions.claude.agent == \"claude\"' "
        "/home/agent/.config/agent-box/sessions.json"
    )

    # ?tab= selects a tab server-side (the no-JS switching path): the new
    # tab is current and its live pane iframes its ttyd URL.
    tab_page = client.succeed(f"{curl} -u agent:testpassword 'https://box.test/?tab=claude'")
    assert 'data-tab="claude" href="/?tab=claude" aria-current="page"' in tab_page, tab_page
    assert 'src="/agent/?arg=claude"' in tab_page, tab_page
    # main is still a tab, just not the current one.
    assert 'data-tab="main" href="/?tab=main" title="main">' in tab_page, tab_page

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
    long_page = client.succeed(f"{curl} -u agent:testpassword https://box.test/")
    assert f'data-tab="{long_name}" href="/?tab={long_name}"' in long_page, long_page
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
    # Read initialPrompt right after the write, before the supervisor's next
    # ~2s tick spawns the session and consumes the prompt.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'agent=claude' --data-urlencode 'prompt=hello there' "
        "https://box.test/sessions/add | grep -x 303"
    )
    machine.succeed(
        "jq -e '.sessions.claude.initialPrompt == \"hello there\"' "
        "/home/agent/.config/agent-box/sessions.json"
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
        for page_url in ["https://box.test/", "https://box.test/agent/settings/"]:
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

  '';
}
