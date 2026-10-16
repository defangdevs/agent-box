# Admission during boot recovery, not just when a registry entry is added.
{ agent-box }:
{
  name = "agent-box-session-limit";
  node.pkgsReadOnly = false;
  nodes.machine = { pkgs, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 1024;
    services.agent-box = {
      enable = true;
      sessionLimit = 2;
      sessionHibernationGraceSeconds = 0;
      eagerAgents = [ "claude" "codex" ];
      package = pkgs.writeShellScriptBin "claude" "exec sleep infinity";
      webhook.enable = false;
      selfUpdate.checkout.enable = false;
      users.agent = {
        seedMainSession = false;
        sessions = {
          a.agent = "claude";
          b.agent = "codex";
          c.agent = "claude";
        };
      };
    };
    system.stateVersion = "25.05";
  };
  testScript = ''
    import json

    machine.start(allow_reboot=True)
    machine.wait_for_unit("agent-box@agent.service")
    cli = "sudo -u agent env HOME=/home/agent agent-box-session"
    panes = "sudo -u agent env TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box"
    machine.wait_until_succeeds(f"{panes} has-session -t a")
    machine.wait_until_succeeds(f"{panes} has-session -t b")
    machine.fail(f"{panes} has-session -t c")
    # The VM has no network and this test exercises target activation, not
    # plugin installation. Seed the minimal installed record so selecting a
    # WhatsApp target cannot spend two 90-second retries in the supervisor.
    machine.succeed(
        "install -d -o agent -g users /home/agent/.claude/plugins && "
        "printf '%s\\n' '{\"plugins\":{\"local-whatsapp@local-channels\":[{}]}}' "
        "> /home/agent/.claude/plugins/installed_plugins.json && "
        "chown agent:users /home/agent/.claude/plugins/installed_plugins.json"
    )

    # A completed Claude turn is hibernated only because c is waiting at the
    # full limit. Freeze a rotated native UUID, then delete the live hook
    # record before activation: the cold sidecar mapping alone must resume
    # that exact transcript.
    claude_native = "11111111-1111-4111-8111-111111111111"
    claude_launch = machine.succeed(
        "jq -r .launchSessionId /home/agent/.local/state/agent-box/session/a.json"
    ).strip()
    machine.succeed(
        "install -d -o agent -g users /home/agent/.claude/projects/-home-agent "
        "/home/agent/.local/state/agent-box/live-session-id"
    )
    machine.succeed(
        f"printf '%s\\n' '{{\"type\":\"assistant\",\"message\":"
        f"{{\"stop_reason\":\"end_turn\"}}}}' > "
        f"/home/agent/.claude/projects/-home-agent/{claude_native}.jsonl"
    )
    machine.succeed(
        f"printf '%s\\n' {claude_native} > "
        f"/home/agent/.local/state/agent-box/live-session-id/{claude_launch}"
    )
    machine.succeed(
        "chown -R agent:users /home/agent/.claude "
        "/home/agent/.local/state/agent-box/live-session-id"
    )
    machine.succeed(f"{cli} whatsapp select a")
    hibernated = machine.succeed(f"{cli} hibernate")
    assert json.loads(hibernated)["name"] == "a", hibernated
    machine.wait_until_fails(f"{panes} has-session -t a")
    machine.wait_until_succeeds(f"{panes} has-session -t c")
    machine.succeed(
        "test $(jq -r .nativeSessionId "
        "/home/agent/.local/state/agent-box/session/a.json) = " + claude_native
    )
    machine.succeed(
        f"rm /home/agent/.local/state/agent-box/live-session-id/{claude_launch}"
    )
    machine.succeed(f"{cli} stop c")
    machine.succeed(
        "install -d -o agent -g users /home/agent/.local/state/local-whatsapp && "
        "printf '%s\\n' '{\"session\":\"a\"}' > "
        "/home/agent/.local/state/local-whatsapp/target.json && "
        "printf '%s\\n' '{\"messages\":{\"m\":{\"reply\":null,"
        "\"control\":false}}}' > "
        "/home/agent/.local/state/local-whatsapp/messages.json && "
        "chown -R agent:users /home/agent/.local/state/local-whatsapp"
    )
    activated = json.loads(machine.succeed(f"{cli} whatsapp activate a"))
    assert activated == {"name": "a", "harness": "claude", "activated": True}, activated
    machine.wait_until_succeeds(f"{panes} has-session -t a")
    machine.wait_until_succeeds(
        f"{panes} display-message -pt a '#{{pane_start_command}}' "
        f"| grep -- '--resume {claude_native}' >/dev/null"
    )

    # Codex has an explicit task_complete terminal event. Its rollout UUID is
    # distinct from the Agentbox launch UUID and must survive the same cold
    # WhatsApp activation without `resume --last` or a replacement thread.
    codex_native = "22222222-2222-4222-8222-222222222222"
    codex_launch = machine.succeed(
        "jq -r .launchSessionId /home/agent/.local/state/agent-box/session/b.json"
    ).strip()
    codex_rollout = (
        "/home/agent/.codex/sessions/2026/10/16/"
        f"rollout-2026-10-16T00-00-00-{codex_native}.jsonl"
    )
    machine.succeed("install -d -o agent -g users /home/agent/.codex/sessions/2026/10/16")
    machine.succeed(
        f"printf '%s\\n' '{{\"message\":\"[agent-box session {codex_launch}]\"}}' "
        "'{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\"}}' "
        "'{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\"}}' "
        f"> {codex_rollout} && chown agent:users {codex_rollout}"
    )
    machine.succeed(f"{cli} whatsapp select b")
    hibernated = machine.succeed(f"{cli} hibernate --exclude a")
    assert json.loads(hibernated)["name"] == "b", hibernated
    machine.wait_until_fails(f"{panes} has-session -t b")
    machine.succeed(
        "test $(jq -r .nativeSessionId "
        "/home/agent/.local/state/agent-box/session/b.json) = " + codex_native
    )
    machine.succeed(
        "printf '%s\\n' '{\"session\":\"b\"}' > "
        "/home/agent/.local/state/local-whatsapp/target.json"
    )
    activated = json.loads(machine.succeed(f"{cli} whatsapp activate b"))
    assert activated == {"name": "b", "harness": "codex", "activated": True}, activated
    machine.wait_until_succeeds(f"{panes} has-session -t b")
    machine.wait_until_succeeds(
        f"{panes} display-message -pt b '#{{pane_start_command}}' "
        f"| grep -E -- ' resume .* -- {codex_native}' >/dev/null"
    )

    machine.succeed(f"{cli} add terminal --harness shell")
    machine.wait_until_succeeds(f"{panes} has-session -t terminal")
    rc, message = machine.execute(f"{cli} add extra --harness claude 2>&1")
    assert rc == 75 and "Session limit reached" in message, (rc, message)
    machine.succeed(f"{cli} stop a")
    machine.wait_until_succeeds(f"{panes} has-session -t c")
    rc, message = machine.execute(f"{cli} restart a")
    assert rc == 75, (rc, message)
    rc, message = machine.execute(f"{cli} restart --all")
    assert rc == 75, (rc, message)
    machine.succeed(f"{cli} restart b")
    machine.wait_until_succeeds(f"{panes} has-session -t b")
    machine.fail(f"{panes} has-session -t a")
    machine.reboot()
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_until_succeeds(f"{panes} has-session -t b")
    machine.wait_until_succeeds(f"{panes} has-session -t c")
    machine.fail(f"{panes} has-session -t a")
  '';
}
