# VM test for issue #101: per-user webhook receiver, on by default.
#
# The point of the feature is that a session gets TOLD when CI finishes or a
# review lands, instead of polling GitHub for it. That only works if every hop
# holds, so this walks the whole chain in the shape a real box runs it:
#
#   - the socket unit pre-binds /run/agent-box-webhook/<user>.sock as
#     0660 <user>:caddy (the settings-socket model, issue #49) and the
#     receiver-only daemon adopts it by socket activation — no port, and no
#     "whichever session won the race" ownership;
#   - the public path /<user>/webhook is served WITHOUT basic auth (GitHub
#     cannot authenticate) — so a POST must reach the daemon while everything
#     else on the vhost still 401s;
#   - a delivery is accepted only if signed: unsigned/bad-signature -> 401,
#     unknown source -> 404, correct HMAC -> 200. Nothing is reachable at all
#     until `agent-box-webhook setup` mints a secret, which is what makes
#     default-on safe;
#   - the daemon then fans the verified event out over IPC to that user's
#     session peers, each applying its OWN subscription filter — asserted with
#     a stand-in peer (a plain webhook.py on stdio, which is exactly what
#     claude runs as the plugin's MCP server) that must print the channel
#     notification for a subscribed topic and stay silent for one nobody
#     subscribed to;
#   - discovery: agent-box-webhook is on the agent's PATH,
#     AGENT_BOX_WEBHOOK_URL is in its environment, the supervisor puts
#     LOCAL_WEBHOOK_SESSION in each tmux session's environment (so the CLI
#     needs no arguments), and the claude settings seed enables the plugin in
#     the exact shape claude itself writes.
#
# Like the fail2ban/downloads tests, this lib.mkForce-swaps the module's ACME
# Caddyfile for a `tls internal` one — the sandbox has no ACME. The swapped-in
# vhost reproduces the module's two relevant handles (unauthenticated
# /agent/webhook*, authenticated catch-all); the flake's `webhook-route` eval
# check separately asserts the module's real Caddyfile emits that same block.
{ agent-box }:
{
  name = "agent-box-webhook";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, lib, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    environment.systemPackages = [ pkgs.jq pkgs.openssl pkgs.curl ];
    services.agent-box = {
      enable = true;
      agent = "claude";
      users.agent = {
        web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
      };
      web = {
        enable = true;
        domain = "box.test";
        user = "agent";
        fail2ban = false;
      };
      # webhook.enable is deliberately NOT set: this test asserts the DEFAULT.
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

    # Minimal `tls internal` vhost reproducing the module's routing shape: the
    # webhook handle FIRST and with no basic_auth, then an authenticated
    # catch-all — so a 200 on /agent/webhook next to a 401 on /agent/ proves
    # the ingress really is outside the auth gate.
    services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
      box.test {
        log
        tls internal
        handle /agent/webhook* {
          uri strip_prefix /agent/webhook
          reverse_proxy unix//run/agent-box-webhook/agent.sock
        }
        handle {
          route {
            basic_auth {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            respond "terminal" 200
          }
        }
      }
    '');
  };

  nodes.client = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.curl pkgs.openssl ];
  };

  testScript = ''
    start_all()
    machine.wait_for_unit("caddy.service")
    machine.wait_for_unit("agent-box-agent.service")
    client.wait_for_unit("multi-user.target")
    machine_ip = machine.succeed("ip -4 -o addr show eth1 | head -1").split()[3].split("/")[0]

    # --- default-on: the units exist without anyone setting webhook.enable ---
    machine.wait_for_unit("agent-box-webhook-agent.socket")
    machine.wait_for_unit("agent-box-webhook-agent.service")
    # Socket ownership is the isolation boundary: the user and caddy, nobody
    # else. systemd (root) binds it before the daemon starts, and the daemon
    # adopts that fd rather than binding a path or a port itself.
    machine.succeed(
        "stat -c '%U:%G %a' /run/agent-box-webhook/agent.sock | grep -x 'agent:caddy 660'"
    )
    machine.succeed("systemctl show -p User --value agent-box-webhook-agent.service | grep -x agent")

    # --- discovery surface -------------------------------------------------
    # The CLI is on the agent's PATH and the endpoint URL is in its
    # environment, so an agent can find both without being told a hostname.
    machine.succeed("test -x /run/current-system/sw/bin/agent-box-webhook")
    machine.succeed(
        "systemctl show -p Environment agent-box-agent.service | grep -q agent-box-webhook/bin"
    )
    machine.succeed(
        "systemctl show -p Environment agent-box-agent.service"
        " | grep -q 'AGENT_BOX_WEBHOOK_URL=https://box.test/agent/webhook'"
    )
    # The supervisor gives each tmux session its own subscription scope, so a
    # bare `agent-box-webhook subscribe` in that session cannot leak into a
    # sibling. Session env comes from `tmux new-session -e`.
    machine.wait_until_succeeds(
        "sudo -u agent env TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box"
        " show-environment -t main | grep -x 'LOCAL_WEBHOOK_SESSION=agent-main'",
        timeout=60,
    )
    machine.succeed(
        "sudo -u agent env TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box"
        " show-environment -t main | grep -x 'LOCAL_WEBHOOK_PORT=0'"
    )
    # Claude loads the plugin from the seeded settings — in the object shape
    # `claude plugin install` writes, not a list of records.
    machine.wait_until_succeeds(
        "jq -e '.enabledPlugins[\"local-webhook@local-channels\"] == true"
        " and .extraKnownMarketplaces[\"local-channels\"].source.repo"
        " == \"defangdevs/local-channels\"' /home/agent/.claude/settings.json",
        timeout=60,
    )

    curl = f"curl -sk --resolve box.test:443:{machine_ip}"

    # --- fails closed before setup -----------------------------------------
    # No sources.json yet: every delivery is rejected, which is what makes
    # shipping this on by default safe. (Also proves caddy reaches the socket:
    # a 404 is the daemon's answer, not caddy's — a dead upstream would 502.)
    client.wait_until_succeeds(
        f"{curl} -o /dev/null -w '%{{http_code}}' -X POST -d '{{}}'"
        f" https://box.test/agent/webhook/github | grep -x 404",
        timeout=60,
    )
    # ... and the rest of the vhost is still behind the terminal's auth.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' https://box.test/agent/ | grep -x 401"
    )

    # --- the user turns it on ----------------------------------------------
    setup = machine.succeed(
        "sudo -u agent env HOME=/home/agent"
        " LOCAL_WEBHOOK_STATE_DIR=/home/agent/.local/state/local-webhook"
        " AGENT_BOX_WEBHOOK_URL=https://box.test/agent/webhook"
        " agent-box-webhook setup"
    )
    assert "https://box.test/agent/webhook/github" in setup, setup
    machine.succeed(
        "stat -c '%U %a' /home/agent/.local/state/local-webhook/github.secret"
        " | grep -x 'agent 600'"
    )
    secret = machine.succeed(
        "cat /home/agent/.local/state/local-webhook/github.secret"
    ).strip()
    assert len(secret) == 32, secret

    # A subscription for THIS session, written through the CLI with no
    # arguments beyond the topic — the session scope comes from the env.
    machine.succeed(
        "sudo -u agent env HOME=/home/agent"
        " LOCAL_WEBHOOK_STATE_DIR=/home/agent/.local/state/local-webhook"
        " LOCAL_WEBHOOK_SESSION=agent-main"
        " agent-box-webhook subscribe defangdevs/agent-box --note 'testing #101' --ttl 0"
    )
    machine.succeed(
        "jq -e '.topics[0].topic == \"github:defangdevs/agent-box\""
        " and .topics[0].note == \"testing #101\"'"
        " /home/agent/.local/state/local-webhook/filter.agent-main.json"
    )

    # --- a stand-in session peer ------------------------------------------
    # Exactly what claude runs as the plugin's MCP server: the same webhook.py
    # on stdio, PORT=0 so it never takes the ingress. Its stdout is the channel
    # stream. Both the interpreter and the script come from the daemon unit's
    # own ExecStart, so the test cannot drift from the pinned pair.
    exec_start = machine.succeed(
        "systemctl show -p ExecStart --value agent-box-webhook-agent.service"
    )
    python = machine.succeed(
        "systemctl show -p ExecStart --value agent-box-webhook-agent.service"
        " | grep -o '/nix/store/[^ ;]*/bin/python3' | head -1"
    ).strip()
    script = machine.succeed(
        "systemctl show -p ExecStart --value agent-box-webhook-agent.service"
        " | grep -o '/nix/store/[^ ;]*webhook.py' | head -1"
    ).strip()
    assert python and script, exec_start
    # systemd-run so the driver isn't left waiting on a backgrounded shell.
    # `sleep | python3` keeps stdin OPEN: webhook.py treats stdin EOF as its
    # session closing and exits, which is right for claude and wrong here.
    # Absolute paths throughout — a transient unit gets systemd's stock PATH
    # (/usr/bin:/bin:...), where NixOS has no `sleep`; an unfound sleep closes
    # the pipe at once and the peer exits before it can register.
    sw = "/run/current-system/sw/bin"
    machine.succeed(
        "systemd-run --unit=webhook-peer --uid=agent --setenv=HOME=/home/agent"
        " --setenv=LOCAL_WEBHOOK_STATE_DIR=/home/agent/.local/state/local-webhook"
        " --setenv=LOCAL_WEBHOOK_SESSION=agent-main --setenv=LOCAL_WEBHOOK_PORT=0"
        f" {sw}/sh -c '{sw}/sleep 600 | {python} {script} > /tmp/peer.log 2>&1'"
    )
    # Fail loudly if the peer died on startup instead of silently timing out on
    # the socket wait below.
    machine.succeed("systemctl is-active webhook-peer.service")
    # The peer registers a per-PID IPC socket; that is what the daemon fans to.
    machine.wait_until_succeeds(
        "ls /home/agent/.local/state/local-webhook/instances/*.sock", timeout=30
    )

    # --- deliveries --------------------------------------------------------
    # A GitHub-shaped workflow_run on the subscribed repo, signed correctly.
    client.succeed(
        "cat > /tmp/body.json <<'EOF'\n"
        '{"action":"completed","workflow_run":{"name":"CI","conclusion":"failure",'
        '"head_branch":"feat/issue-101","html_url":"https://box.test/run/1"},'
        '"repository":{"full_name":"defangdevs/agent-box"},"sender":{"login":"someone"}}\n'
        "EOF"
    )
    sig = client.succeed(
        f"openssl dgst -sha256 -hmac {secret} -r /tmp/body.json | cut -d' ' -f1"
    ).strip()

    post = (
        f"{curl} -o /dev/null -w '%{{http_code}}' -X POST"
        " -H 'content-type: application/json' -H 'x-github-event: workflow_run'"
        " -H 'x-github-delivery: test-1' --data-binary @/tmp/body.json"
    )
    # Wrong signature and no signature are both refused, before any parsing.
    client.succeed(
        f"{post} -H 'x-hub-signature-256: sha256=00' "
        f"https://box.test/agent/webhook/github | grep -x 401"
    )
    client.succeed(f"{post} https://box.test/agent/webhook/github | grep -x 401")
    # An unconfigured source is refused even with a valid signature for another.
    client.succeed(
        f"{post} -H 'x-hub-signature-256: sha256={sig}' "
        f"https://box.test/agent/webhook/stripe | grep -x 404"
    )
    # The real thing: signed, through the unauthenticated public path.
    client.succeed(
        f"{post} -H 'x-hub-signature-256: sha256={sig}' "
        f"https://box.test/agent/webhook/github | grep -x 200"
    )
    # A bare POST maps to defaultSource, so a pre-existing hook URL with no
    # source path keeps working.
    client.succeed(
        f"{post} -H 'x-hub-signature-256: sha256={sig}' "
        f"https://box.test/agent/webhook | grep -x 200"
    )

    # The daemon fanned it out and the peer applied its own filter: a channel
    # notification naming the repo and the failing run.
    machine.wait_until_succeeds("grep -q 'notifications/claude/channel' /tmp/peer.log", timeout=30)
    machine.succeed("grep -q 'defangdevs/agent-box' /tmp/peer.log")
    machine.succeed("grep -q 'UNTRUSTED webhook:github' /tmp/peer.log")
    # The note is echoed under the delivery, so a fresh-context session still
    # knows why it is being told.
    machine.succeed("grep -q 'testing #101' /tmp/peer.log")

    # An event on a repo nobody subscribed to is accepted (HMAC is valid) but
    # filtered out per session — the filter is what keeps a session quiet.
    client.succeed(
        "cat > /tmp/other.json <<'EOF'\n"
        '{"action":"opened","pull_request":{"number":9,"title":"unrelated",'
        '"html_url":"https://box.test/pr/9"},'
        '"repository":{"full_name":"someone/unrelated"},"sender":{"login":"x"}}\n'
        "EOF"
    )
    sig2 = client.succeed(
        f"openssl dgst -sha256 -hmac {secret} -r /tmp/other.json | cut -d' ' -f1"
    ).strip()
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' -X POST"
        " -H 'content-type: application/json' -H 'x-github-event: pull_request'"
        f" -H 'x-hub-signature-256: sha256={sig2}' --data-binary @/tmp/other.json"
        f" https://box.test/agent/webhook/github | grep -x 200"
    )
    machine.sleep(3)
    machine.fail("grep -q 'someone/unrelated' /tmp/peer.log")

    # --- dispatch: a standing watch spawns a fresh session (0.9.0, #1) ------
    # A deliver_to:"subagent" subscription goes to the SHARED dispatch file,
    # pinned by default, and does not touch the session's own filter.
    machine.succeed(
        "sudo -u agent env HOME=/home/agent"
        " LOCAL_WEBHOOK_STATE_DIR=/home/agent/.local/state/local-webhook"
        " LOCAL_WEBHOOK_SESSION=agent-main"
        " agent-box-webhook subscribe defangdevs/agent-box --deliver-to subagent"
        " --note 'standing watch: triage'"
    )
    machine.succeed(
        "jq -e '.topics[0].topic == \"github:defangdevs/agent-box\""
        " and .topics[0].ttlHours == 0'"
        " /home/agent/.local/state/local-webhook/filter.dispatch.json"
    )
    # The daemon advertises the spawn wiring, so subscribe could warn if the
    # unit ever lost LOCAL_WEBHOOK_SPAWN_CMD.
    machine.succeed(
        "systemctl show -p Environment agent-box-webhook-agent.service"
        " | grep -q 'LOCAL_WEBHOOK_SPAWN_CMD=/nix/store/'"
    )
    machine.succeed("jq -e '.spawn == true' /home/agent/.local/state/local-webhook/receiver.json")

    # A signed delivery on the watched repo → a fresh hook-* session appears in
    # sessions.json, primed with the framed event text plus the trusted
    # preamble, and the supervisor starts it as a real tmux session.
    client.succeed(
        f"{post} -H 'x-hub-signature-256: sha256={sig}' "
        f"https://box.test/agent/webhook/github | grep -x 200"
    )
    machine.wait_until_succeeds(
        "jq -e '.sessions | keys | map(select(startswith(\"hook-\"))) | length == 1'"
        " /home/agent/.config/agent-box/sessions.json",
        timeout=60,
    )
    hook_prompt = machine.succeed(
        "jq -r '.sessions | to_entries[] | select(.key | startswith(\"hook-\"))"
        " | .value.initialPrompt' /home/agent/.config/agent-box/sessions.json"
    )
    assert "UNTRUSTED webhook:github" in hook_prompt, hook_prompt
    assert "defangdevs/agent-box" in hook_prompt, hook_prompt
    assert "standing watch: triage" in hook_prompt, hook_prompt
    assert "webhook dispatcher" in hook_prompt, hook_prompt   # trusted preamble
    assert "agent-box-session rm hook-" in hook_prompt, hook_prompt  # cleanup duty
    # The key names the repo, so the tab is readable: hook-defangdevs-agent-box-XXXX.
    machine.succeed(
        "jq -e '.sessions | keys[] | select(startswith(\"hook-defangdevs-agent-box-\"))'"
        " /home/agent/.config/agent-box/sessions.json"
    )
    machine.wait_until_succeeds(
        "sudo -u agent env TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box"
        " list-sessions -F '#S' | grep -q '^hook-'",
        timeout=60,
    )

    # The event that spawned the watch session was NOT also a session delivery
    # for the peer (its filter has someone else's repo) — dispatch and session
    # routing stay independent.
    machine.fail("grep -q 'standing watch: triage' /tmp/peer.log")

    # The daemon is the ingress owner and survives every delivery — the box's
    # endpoint must not depend on which sessions happen to be alive.
    machine.succeed("systemctl is-active agent-box-webhook-agent.service")
  '';
}
