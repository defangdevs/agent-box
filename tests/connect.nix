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
            # Prefix, not equality: the daemon refuses a code under four
            # characters before it reaches this pane, so the test cannot
            # send a bare "bad".
            case "$code" in
              bad*)
                echo "OAuth error: Request failed with status code 400"
                exit 1
                ;;
            esac
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
            # Whether the pane can run git at all. The real gh shells out
            # to it to read the credential helper /etc/gitconfig sets for
            # github.com, and a pane that inherited the settings daemon's
            # PATH instead of the agent unit's cannot: gh then opens with
            # an interactive "Authenticate Git with your GitHub
            # credentials?" that nothing answers, and the card never gets
            # a URL or a code.
            command -v git > ${stateDir}/gh-git 2>/dev/null || true
            # Real gh, with --git-protocol https, asks this FIRST — before
            # it contacts GitHub — and blocks on the answer whether or not
            # git is on PATH. The daemon must recognise and answer this
            # keypress, or gh never requests a device code and the card
            # hangs at "Starting the sign-in…" (agent-box#400).
            printf "? Authenticate Git with your GitHub credentials? (Y/n) "
            read -r _gitans || exit 1
            : > ${stateDir}/gh-gitans
            # The shape the REAL device-flow CLIs print: prose that
            # contains the word "code", and the code itself on a line of
            # its own further down. Anchoring the code on "code" rendered
            # "code authorization" as the code AUTHORIZATION and never
            # found the real one (codex, agent-box#317 follow-up).
            echo "Follow these steps to sign in using device code authorization:"
            echo "! First copy your one-time code"
            echo "   EE45-B423"
            echo "Open this URL to continue in your web browser: https://github.com/login/device"
            # LATE, on purpose: the keypress used to be answered by a
            # bounded poll at start-up only, so a CLI slower than that
            # window (one slow device-code request) was left holding a
            # prompt nobody would ever answer.
            sleep 8
            printf "Press Enter to open github.com in your browser... "
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
      # `defang login --non-interactive=false`: prints only a URL — the real
      # CLI polls the auth server itself (auth.go's StartAuthCodeFlow) and
      # never shows a code to paste back — so the stub blocks on a marker
      # file standing in for that poll succeeding, instead of a real OAuth
      # round trip.
      stubDefang = pkgs.writeShellScriptBin "defang" ''
        set -u
        case "$1 $2" in
          "login --non-interactive=false")
            mkdir -p ${stateDir}
            echo "Please visit the following URL to log in: (Right click the URL or press ENTER to open browser)"
            echo "  https://auth.defang.io/cli/stubstate/stubchallenge"
            for _ in $(seq 1 30); do
              [ -e ${stateDir}/defang-approved ] && break
              sleep 1
            done
            [ -e ${stateDir}/defang-approved ] || { echo "Error: login timed out" >&2; exit 1; }
            : > ${stateDir}/defang-in
            ;;
          "whoami --json")
            if [ -e ${stateDir}/defang-in ]; then
              echo '{"workspace":"stub-ws","subscriberTier":"pro","provider":"aws","region":"us-east-1"}'
            else
              echo "Error: missing bearer token" >&2
              exit 16
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
          # This test predates the front door (issue #416): its sign-in
          # panes are children of the agent unit's tmux server, and
          # connect_start REFUSES to create that server itself (it would
          # reparent every later session into the settings daemon's
          # cgroup). So it needs the seeded "main" a web box no longer
          # gets by default — including for the subtest below that KILLS
          # every session to prove the card reports "blocked" rather than
          # offering a dead button.
          seedMainSession = true;
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
      systemd.services."agent-box-settings@agent".environment.AGENT_BOX_CONNECT_BINS =
        lib.mkForce "claude=${stubClaude}/bin/claude github=${stubGh}/bin/gh defang=${stubDefang}/bin/defang";

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
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_for_unit("agent-box-settings@agent.service")

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


    with subtest("every card renders, installed or not"):
        body = get("/agent/settings/")
        assert "Connections" in body, body[:400]
        assert "Claude Code" in body
        assert "GitHub" in body
        assert "Defang" in body
        # codex is NOT in installAgents and has no stub binary, and it
        # still gets a card (issue #416). That is the whole point of the
        # lazy box: with the CLIs out of the closure, a card that only
        # existed where its binary did was a chicken-and-egg — no card to
        # press, and pressing it is how you get the CLI.
        assert ">Codex<" in body, body[:400]
        wait_state("claude", "idle")
        wait_state("github", "idle")
        wait_state("defang", "idle")

    with subtest("an uninstalled CLI says so, and offers to fetch it"):
        # The distinction is not cosmetic: "Not signed in" would send the
        # user looking for an OAuth flow, when what is missing is a
        # download.
        assert state("codex")["installed"] is False
        assert state("codex")["installable"] is True
        assert state("claude")["installed"] is True
        body = get("/agent/settings/")
        assert "Not installed" in body, body[:400]
        assert "Install &amp; sign in" in body or "Install & sign in" in body

    with subtest("defang is never offered as an install"):
        # It is not in nixpkgs and comes from its own background unit, so
        # a card must not offer to fetch it from a package source that has
        # never heard of it.
        assert state("defang")["installable"] is False

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

    with subtest("the card shows the code the CLI printed, not its prose"):
        assert post("/agent/settings/connect/start", "flow=github") == "303"
        waiting = wait_state("github", "waiting")
        assert waiting["code"] == "EE45-B423", waiting
        assert waiting["url"] == "https://github.com/login/device", waiting
        body = get("/agent/settings/")
        assert "EE45-B423" in body
        assert "AUTHORIZATION" not in body

    with subtest("the pane can run git, and gh's Git-credential prompt is auto-answered"):
        # Empty gh-git means the pane inherited this daemon's PATH instead
        # of the agent unit's, so gh setup-git could not run git at all.
        machine.succeed("test -s ${stateDir}/gh-git")
        # gh asks "Authenticate Git with your GitHub credentials?" before
        # it contacts GitHub, even with git present; reaching the code
        # above at all proves the daemon answered that keypress, and the
        # marker confirms gh got past it rather than the code being a
        # decoy (agent-box#400).
        machine.succeed("test -e ${stateDir}/gh-gitans")

    with subtest("gh's keypress prompt is answered however late it arrives"):
        wait_state("github", "connected")
        machine.succeed("test -e ${stateDir}/gh-enter")
        assert "stubuser" in state("github")["detail"]

    with subtest("a flow with no code at all just waits on the URL, then polls itself"):
        assert post("/agent/settings/connect/start", "flow=defang") == "303"
        waiting = wait_state("defang", "waiting")
        assert waiting["url"] == "https://auth.defang.io/cli/stubstate/stubchallenge", waiting
        assert waiting["code"] is None, waiting
        assert waiting["needs_code"] is False, waiting
        machine.succeed("touch ${stateDir}/defang-approved")
        got = wait_state("defang", "connected")
        assert got["detail"] == "stub-ws (pro)", got

    with subtest("sign-in again works on a card that reports signed in"):
        # The card says "Signed in", which is WHY the button is pressed. A
        # cached "connected" must not reap the pane the press just started.
        assert state("claude")["state"] == "connected"
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        wait_state("claude", "waiting")
        machine.succeed(tmux("has-session -t =_connect-claude"))
        assert post("/agent/settings/connect/cancel", "flow=claude") == "303"
        machine.wait_until_fails(tmux("has-session -t =_connect-claude"))
        wait_state("claude", "connected")

    with subtest("a rejected code reports the CLI's complaint, not the transcript"):
        machine.succeed("rm -f ${stateDir}/claude-in")
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        wait_state("claude", "waiting")
        assert post("/agent/settings/connect/code", "flow=claude&code=badcode1") == "303"
        failed = wait_state("claude", "failed")
        # The CLI's own line, and ONLY that: not the instructions, not the
        # URL, and not the echo of the prompt the code was typed into (which
        # carries the code itself).
        assert "status code 400" in failed["error"], failed
        assert "badcode1" not in failed["error"], failed
        assert "claude.com" not in failed["error"], failed
        assert post("/agent/settings/connect/cancel", "flow=claude") == "303"

    with subtest("a failed sign-in can be retried while its pane still lingers"):
        # A finished pane lingers in CONNECT_LINGER's sleep so the last
        # screen stays readable — a FAILED one the same way. Start must
        # reap that corpse and begin again, not mistake it for a sign-in
        # already in flight and no-op until the linger elapses, which left
        # a Codex 500 with no way to retry (agent-box#400).
        machine.succeed("rm -f ${stateDir}/claude-in")
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        wait_state("claude", "waiting")
        assert post("/agent/settings/connect/code", "flow=claude&code=badcode2") == "303"
        wait_state("claude", "failed")
        # The failed pane is still listed, lingering in the wrapper's sleep.
        machine.succeed(tmux("has-session -t =_connect-claude"))
        # Pressing Sign in again reaps it and starts a fresh sign-in.
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        wait_state("claude", "waiting")
        assert post("/agent/settings/connect/cancel", "flow=claude") == "303"
        machine.wait_until_fails(tmux("has-session -t =_connect-claude"))

    with subtest("cancel kills the pane"):
        machine.succeed("rm -f ${stateDir}/claude-in")
        assert post("/agent/settings/connect/start", "flow=claude") == "303"
        wait_state("claude", "waiting")
        assert post("/agent/settings/connect/cancel", "flow=claude") == "303"
        machine.wait_until_fails(tmux("has-session -t =_connect-claude"))
        wait_state("claude", "idle")

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

    with subtest("rendering the page never waits on a CLI"):
        # The probes run in a background thread; a render that forked three
        # real CLIs held the settings page past its client timeout.
        machine.succeed(
            as_agent(f"{sock} --max-time 5 -o /dev/null -w '%{{http_code}}' {page}")
            + " | grep -x 200 >/dev/null"
        )

    with subtest("an unknown flow is a 404, not a started pane"):
        assert post("/agent/settings/connect/start", "flow=nope") == "404"
        assert post("/agent/settings/connect/bogus", "flow=claude") == "404"

    with subtest("the daemon never starts a tmux server of its own"):
        # A server started here would parent every session the supervisor
        # later spawns, moving the agents out of their hardened unit's
        # namespace and into the settings daemon's.
        machine.succeed("systemctl stop agent-box@agent.service")
        machine.wait_until_fails(tmux("list-sessions"))
        assert post("/agent/settings/connect/start", "flow=claude") == "409"
        machine.fail(tmux("list-sessions"))
        assert "nowhere to sign in from" in get("/agent/settings/")
        assert state("claude")["blocked"] is True
  '';
}
