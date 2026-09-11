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
      eagerAgents = [ "claude" ];
      package = pkgs.writeShellScriptBin "claude" "exec sleep infinity";
      webhook.enable = false;
      selfUpdate.checkout.enable = false;
      users.agent = {
        seedMainSession = false;
        sessions = {
          a.agent = "claude";
          b.agent = "claude";
          c.agent = "claude";
        };
      };
    };
    system.stateVersion = "25.05";
  };
  testScript = ''
    machine.wait_for_unit("agent-box@agent.service")
    cli = "sudo -u agent env HOME=/home/agent agent-box-session"
    panes = "sudo -u agent env TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box"
    machine.wait_until_succeeds(f"{panes} has-session -t a")
    machine.wait_until_succeeds(f"{panes} has-session -t b")
    machine.fail(f"{panes} has-session -t c")
    rc, message = machine.execute(f"{cli} add extra --harness shell 2>&1")
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
