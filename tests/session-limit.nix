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
          "hook-a".agent = "claude";
          "hook-b".agent = "codex";
          "hook-c".agent = "claude";
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
    machine.wait_until_succeeds(f"{panes} has-session -t hook-a")
    machine.wait_until_succeeds(f"{panes} has-session -t hook-b")
    machine.fail(f"{panes} has-session -t hook-c")
    # The VM has no network and this test exercises target activation, not
    # plugin installation. Seed the minimal installed record so selecting a
    # WhatsApp target cannot spend two 90-second retries in the supervisor.
    machine.succeed(
        "install -d -o agent -g users /home/agent/.claude/plugins && "
        "printf '%s\\n' '{\"plugins\":{\"local-whatsapp@local-channels\":[{}]}}' "
        "> /home/agent/.claude/plugins/installed_plugins.json && "
        "chown agent:users /home/agent/.claude/plugins/installed_plugins.json"
    )

    # A completed Claude hook is hibernated only because hook-c is waiting at the
    # full limit. Freeze a rotated native UUID, then delete the live hook
    # record before activation: the cold sidecar mapping alone must resume
    # that exact transcript.
    claude_native = "11111111-1111-4111-8111-111111111111"
    claude_launch = machine.succeed(
        "jq -r .launchSessionId /home/agent/.local/state/agent-box/session/hook-a.json"
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
    machine.succeed(f"{cli} whatsapp select hook-a")
    hibernated = machine.succeed(f"{cli} hibernate")
    assert json.loads(hibernated)["name"] == "hook-a", hibernated
    machine.wait_until_fails(f"{panes} has-session -t hook-a")
    machine.wait_until_succeeds(f"{panes} has-session -t hook-c")
    machine.succeed(
        "test $(jq -r .nativeSessionId "
        "/home/agent/.local/state/agent-box/session/hook-a.json) = " + claude_native
    )
    machine.succeed(
        f"rm /home/agent/.local/state/agent-box/live-session-id/{claude_launch}"
    )
    machine.succeed(f"{cli} stop hook-c")
    machine.succeed(
        "install -d -o agent -g users /home/agent/.local/state/local-whatsapp && "
        "printf '%s\\n' '{\"session\":\"hook-a\"}' > "
        "/home/agent/.local/state/local-whatsapp/target.json && "
        "printf '%s\\n' '{\"messages\":{\"m\":{\"reply\":null,"
        "\"control\":false}}}' > "
        "/home/agent/.local/state/local-whatsapp/messages.json && "
        "chown -R agent:users /home/agent/.local/state/local-whatsapp"
    )
    activated = json.loads(machine.succeed(f"{cli} whatsapp activate hook-a"))
    assert activated == {"name": "hook-a", "harness": "claude", "activated": True}, activated
    machine.wait_until_succeeds(f"{panes} has-session -t hook-a")
    machine.wait_until_succeeds(
        f"{panes} display-message -pt hook-a '#{{pane_start_command}}' "
        f"| grep -- '--resume {claude_native}' >/dev/null"
    )

    # Codex has an explicit task_complete terminal event. Its rollout UUID is
    # distinct from the Agentbox launch UUID and must survive the same cold
    # WhatsApp activation without `resume --last` or a replacement thread.
    codex_native = "22222222-2222-4222-8222-222222222222"
    codex_launch = machine.succeed(
        "jq -r .launchSessionId /home/agent/.local/state/agent-box/session/hook-b.json"
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
    machine.succeed(f"{cli} whatsapp select hook-b")
    hibernated = machine.succeed(f"{cli} hibernate --exclude hook-a")
    assert json.loads(hibernated)["name"] == "hook-b", hibernated
    machine.wait_until_fails(f"{panes} has-session -t hook-b")
    machine.succeed(
        "test $(jq -r .nativeSessionId "
        "/home/agent/.local/state/agent-box/session/hook-b.json) = " + codex_native
    )
    machine.succeed(
        "printf '%s\\n' '{\"session\":\"hook-b\"}' > "
        "/home/agent/.local/state/local-whatsapp/target.json"
    )
    activated = json.loads(machine.succeed(f"{cli} whatsapp activate hook-b"))
    assert activated == {"name": "hook-b", "harness": "codex", "activated": True}, activated
    machine.wait_until_succeeds(f"{panes} has-session -t hook-b")
    machine.wait_until_succeeds(
        f"{panes} display-message -pt hook-b '#{{pane_start_command}}' "
        f"| grep -E -- ' resume .* -- {codex_native}' >/dev/null"
    )

    # Manual sessions start and restart while all hook slots are occupied.
    machine.succeed(f"{cli} add manual --harness claude")
    machine.wait_until_succeeds(f"{panes} has-session -t manual")
    machine.succeed(f"{cli} stop manual")
    machine.succeed(f"{cli} restart manual")
    machine.wait_until_succeeds(f"{panes} has-session -t manual")
    machine.succeed(f"{cli} add terminal --harness shell")
    machine.wait_until_succeeds(f"{panes} has-session -t terminal")
    rc, message = machine.execute(f"{cli} add extra --harness claude --origin webhook 2>&1")
    assert rc == 75 and "Session limit reached" in message, (rc, message)
    machine.succeed(f"{cli} stop hook-a")
    machine.succeed(f"{cli} restart hook-c")
    machine.wait_until_succeeds(f"{panes} has-session -t hook-c")
    rc, message = machine.execute(f"{cli} restart hook-a")
    assert rc == 75, (rc, message)
    rc, message = machine.execute(f"{cli} restart --all")
    assert rc == 75, (rc, message)
    machine.succeed(f"{cli} restart hook-b")
    machine.wait_until_succeeds(f"{panes} has-session -t hook-b")
    machine.fail(f"{panes} has-session -t hook-a")
    machine.reboot()
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_until_succeeds(f"{panes} has-session -t manual")
    machine.wait_until_succeeds(f"{panes} has-session -t hook-b")
    machine.wait_until_succeeds(f"{panes} has-session -t hook-c")
    machine.fail(f"{panes} has-session -t hook-a")
  '';
}
