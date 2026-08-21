# VM test for the guided sign-in cards (issues #207, #208, #313): the
# settings page runs each tool's OWN sign-in command in its own tmux
# session, reads the URL off that pane, types the code back in, and asks
# the CLI itself whether it worked.
#
# The CLIs are STUBBED here. What is under test is the driver — pane
# lifecycle, URL host-anchoring, code forwarding, the keypress gh's flow
# waits for, success detection, cancel, the manual-env-key warning, and
# the refusal to start a tmux server — none of which needs a real OAuth
# round trip, and all of which a real CLI would make untestable offline.
#
# Requests go over the daemon's unix socket as the agent user: the Caddy
# route and its auth gate are settings-page.nix's job, not this test's.
{ agent-box }:
let
  # Where both units can see the stubs' bookkeeping: /tmp is private to
  # each unit (PrivateTmp), and the pane runs under the AGENT unit while
  # the status calls run under the SETTINGS daemon.
  stateDir = "/home/agent/.connect-stub";
in
{
  name = "agent-box-connect";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, lib, ... }:
    let
      # `claude auth login`: prints a decoy URL on an untrusted host
      # BEFORE the real one (the card must still link claude.com), waits
      # for a pasted code, and only then reports itself signed in.
      stubClaude = pkgs.writeShellScriptBin "claude" ''
        set -u
        case "$1 $2" in
          "auth login")
            mkdir -p ${stateDir}
            echo "Opening browser to sign in..."
            echo "Decoy: https://evil.example.com/claude.com/oauth/authorize"
            echo "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=stub&state=abc"
            printf "Paste code here if prompted > "
            # A cancelled flow closes the pane, so `read` sees EOF: exit
            # without marking success, the way a real CLI killed mid-flow
            # would.
            read -r code || exit 1
            [ -n "$code" ] || exit 1
            printf '%s' "$code" > ${stateDir}/claude-code
            if [ "$code" = "bad" ]; then
              echo "OAuth error: Request failed with status code 400"
              exit 1
            fi
            : > ${stateDir}/claude-in
            echo "Login successful."
            ;;
          "auth status")
            if [ -e ${stateDir}/claude-in ]; then
              echo '{"loggedIn":true,"authMethod":"claude.ai","email":"stub@example.com","subscriptionType":"max"}'
            else
              echo '{"loggedIn":false,"authMethod":"none"}'
            fi
            ;;
          *) echo "unexpected: $*" >&2; exit 64 ;;
        esac
      '';
      # `gh auth login --web`: prints the one-time code, then blocks on the
      # keypress the real gh waits for before it starts polling.
      stubGh = pkgs.writeShellScriptBin "gh" ''
        set -u
        case "$1 $2" in
          "auth login")
            mkdir -p ${stateDir}
            echo "! First copy your one-time code: EE45-B423"
            printf "Press Enter to open https://github.com/login/device in your browser... "
            read -r _ignored || exit 1
            : > ${stateDir}/gh-enter
            : > ${stateDir}/gh-in
            echo "! Failed opening a web browser at https://github.com/login/device"
            ;;
          "auth status")
            # Same precedence the real gh reports: an environment token
            # wins over the stored credential, and the source is named.
            if [ -n "''${GH_TOKEN:-}" ]; then
              echo "✓ Logged in to github.com account envuser (GH_TOKEN)"
            elif [ -e ${stateDir}/gh-in ]; then
              echo "✓ Logged in to github.com account stubuser (keyring)"
            else
              echo "You are not logged into any GitHub hosts." >&2
              exit 1
            fi
            ;;
          *) echo "unexpected: $*" >&2; exit 64 ;;
        esac
      '';
    in
    {
      imports = [ agent-box ];
      virtualisation.memorySize = 2048;
      environment.systemPackages = [ pkgs.curl ];
      services.agent-box = {
        enable = true;
        agent = "claude";
        installAgents = [ "claude" ];
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

      # Point the cards at the stubs instead of the real CLIs. Exactly the
      # variable the module computes, so the daemon cannot tell the
      # difference — which is the point of naming binaries rather than
      # relying on PATH.
      systemd.services.agent-box-settings-agent.environment.AGENT_BOX_CONNECT_BINS =
        lib.mkForce "claude=${stubClaude}/bin/claude github=${stubGh}/bin/gh";

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

      # No ACME in the sandbox: a minimal vhost keeps caddy healthy while
      # the test itself talks to the daemon's socket directly.
      services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
        box.test {
          tls internal
          respond "ok"
        }
      '');
    };

  testScript = ''
    import json
    import shlex

    start_all()
    machine.wait_for_unit("agent-box-agent.service")
    machine.wait_for_unit("agent-box-settings-agent.service")

    sock = "curl -s --max-time 20 --unix-socket /run/agent-box-settings/agent.sock"
    page = "http://localhost/agent/settings/"


    def as_agent(cmd):
        # shlex.quote, not repr: these commands carry single quotes of
        # their own (curl's -w format), which repr would not escape.
        return "su -s /bin/sh agent -c " + shlex.quote(cmd)


    def get(path):
        return machine.succeed(as_agent(f"{sock} http://localhost{path}"))


    def post(path, data=""):
        arg = f"-d {data!r} " if data else "-X POST "
        return machine.succeed(
            as_agent(f"{sock} {arg}-o /dev/null -w '%{{http_code}}' http://localhost{path}")
        ).strip()


    def state(flow):
        raw = get(f"/agent/settings/connect?flow={flow}")
        return json.loads(raw)["flow"]


    def tmux(cmd):
        # Route through as_agent's shlex.quote rather than splicing cmd into
        # a hand-written single-quoted string: cmd itself carries quotes
        # (e.g. "list-sessions -F '#S'"), and naive splicing closes the
        # outer quote early, leaving a bare # that swallows the rest as a
        # shell comment (agent-box#317's CI failure).
        return as_agent(
            "env TMUX_TMPDIR=/run/agent-box-agent "
            "tmux -L agent-box " + cmd
        )


    def wait_detail(flow, want, tries=20):
        for _ in range(tries):
            got = state(flow)
            if want in got["detail"]:
                return got
            machine.sleep(1)
        raise Exception(f"{flow} detail {state(flow)['detail']!r} never held {want!r}")


    def wait_state(flow, want, tries=40):
        for _ in range(tries):
            got = state(flow)
            if got["state"] == want:
                return got
            machine.sleep(1)
        raise Exception(f"{flow} stuck in {state(flow)['state']}, wanted {want}")


    with subtest("cards render for the installed CLIs only"):
        body = get("/agent/settings/")
        assert "Connections" in body, body[:400]
        assert "Claude Code" in body
        assert "GitHub" in body
        # codex is not in installAgents, so it has no card.
        assert ">Codex<" not in body
        assert state("claude")["state"] == "idle"
        assert state("github")["state"] == "idle"

    with subtest("start opens one pane and the card links the trusted URL"):
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        machine.succeed(tmux("list-sessions -F '#S'") + " | grep _connect-claude >/dev/null")
        waiting = wait_state("claude", "waiting")
        # Host-anchored: the decoy the stub printed FIRST is not the link.
        assert waiting["url"].startswith("https://claude.com/cai/oauth/authorize"), waiting
        assert "evil.example.com" not in get("/agent/settings/")
        # A second start is idempotent — still one pane.
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        panes = machine.succeed(tmux("list-sessions -F '#S'") + " | grep -c _connect-claude")
        assert panes.strip() == "1", panes

    with subtest("the pasted code reaches the CLI and success is the CLI's own answer"):
        assert post("/agent/settings/connect/code", "flow=claude&code=abc-1234567890") == "303"
        got = wait_state("claude", "connected")
        assert got["detail"] == "stub@example.com (max)", got
        assert machine.succeed("cat ${stateDir}/claude-code").strip() == "abc-1234567890"
        # Signed in: the pane is reaped rather than left holding a stale code.
        machine.wait_until_fails(tmux("has-session -t =_connect-claude"))

    with subtest("a code with whitespace is refused before it reaches the pane"):
        assert post("/agent/settings/connect/code", "flow=claude&code=with space") == "400"

    with subtest("gh's keypress prompt is answered for the user"):
        assert post("/agent/settings/connect/start", "flow=github") == "303"
        wait_state("github", "connected")
        machine.succeed("test -e ${stateDir}/gh-enter")
        assert "stubuser" in state("github")["detail"]

    with subtest("cancel kills the pane"):
        machine.succeed("rm -f ${stateDir}/claude-in")
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        wait_state("claude", "waiting")
        assert post("/agent/settings/connect/cancel", "flow=claude") == "303"
        machine.wait_until_fails(tmux("has-session -t =_connect-claude"))
        assert state("claude")["state"] == "idle"

    with subtest("a hand-set env key still wins, and the card says so"):
        assert post("/agent/settings/set", "key=GH_TOKEN&value=ghp_manual") == "303"
        body = get("/agent/settings/")
        assert "is set under Environment secrets" in body
        assert "ghp_manual" not in body
        # The status probe must see the env STORE, not just the daemon's own
        # environment: that unit never loads the store (the supervisor's
        # spawn wrapper does, per session), so a probe with our bare
        # environment would report "not signed in" for a box whose sessions
        # authenticate perfectly through this key. The stub reports the
        # source, so the card can be checked for it.
        wait_detail("github", "envuser (GH_TOKEN)")

    with subtest("an unknown flow is a 404, not a started pane"):
        assert post("/agent/settings/connect/start", "flow=nope") == "404"
        assert post("/agent/settings/connect/bogus", "flow=claude") == "404"

    with subtest("the daemon never starts a tmux server of its own"):
        # A server started here would parent every session the supervisor
        # later spawns, moving the agents out of their hardened unit's
        # namespace and into the settings daemon's.
        machine.succeed("systemctl stop agent-box-agent.service")
        machine.wait_until_fails(tmux("list-sessions"))
        assert post("/agent/settings/connect/start", "flow=claude") == "409"
        machine.fail(tmux("list-sessions"))
        assert "nowhere to sign in from" in get("/agent/settings/")
        assert state("claude")["blocked"] is True
  '';
}
