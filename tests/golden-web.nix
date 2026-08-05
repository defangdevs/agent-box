# Golden-snapshot overlay (issue #154, Phase 0). nixosConfigurations.vm alone
# never enables the web stack, so a snapshot of just that config would miss
# every Caddy/ttyd/settings/webhook/self-update artifact — exactly the parts
# Phases 1-3 must not change. This overlay turns them all on, on top of
# hosts/vm.nix:
#   - web.enable with TWO terminal users, so the sorted-order ttyd port
#     assignment (7681, 7682) and the rootUser selection are pinned;
#   - a codex user, so the supervisor's codex branch and the codex
#     remote-control wrapper land in the snapshot;
#   - selfUpdate.enable, so the update unit and the settings daemon's update
#     wiring are pinned (rev is a fixed dummy — only the rendered text
#     matters, nothing here ever runs).
# Values are frozen: changing any of them rewrites tests/golden/web and
# defeats the point of the fixture.
{ ... }:
{
  services.agent-box = {
    users.agent.web.passwordHashFile = "/var/lib/agent-box-web/password-hash-agent";
    users.robot = {
      agent = "codex";
      web.passwordHashFile = "/var/lib/agent-box-web/password-hash-robot";
    };
    web = {
      enable = true;
      domain = "golden.example.org";
      user = "agent";
    };
    selfUpdate = {
      enable = true;
      rev = "0000000000000000000000000000000000000000";
    };
  };
}
