# Golden-snapshot overlay (issue #154, Phase 0). nixosConfigurations.vm alone
# never enables the web stack, so a snapshot of just that config would miss
# every Caddy/ttyd/settings/webhook/self-update artifact — exactly the parts
# Phases 1-3 must not change. This overlay turns them all on, on top of
# hosts/vm.nix:
#   - web.enable with TWO terminal users, so the per-user ttyd socket paths
#     (issue #628, which replaced a sorted-order port assignment) and the
#     rootUser selection are pinned;
#   - a codex user, so the supervisor's codex branch and the codex
#     remote-control wrapper land in the snapshot;
#   - selfUpdate.enable, so the update unit and the settings daemon's update
#     wiring are pinned (rev is a fixed dummy — only the rendered text
#     matters, nothing here ever runs).
# Values are frozen: changing any of them rewrites tests/golden/web and
# defeats the point of the fixture.
#
# Issue #451 (PR 1) added the sessions and the per-user environment below. They
# are not new coverage for the module: tests/native/config.json already
# declared exactly this box, by hand, and the two hand-mirrored configs had
# drifted into describing DIFFERENT boxes (no sessions here, two per user
# there), which is what made every cross-backend comparison of the two
# fixtures apples-to-oranges. tests/native/config.json is now generated from
# THIS file (tests/spec.nix), so the shape lives here once and both fixtures
# describe one box — including the seed JSON, whose two producers are #356's
# twin-schema bug.
{ ... }:
{
  services.agent-box = {
    users.agent = {
      web.passwordHashFile = "/var/lib/agent-box-web/password-hash-agent";
      sessions = {
        main = { };
        review = { agent = "codex"; workingDirectory = "/home/agent/agent-box"; };
      };
    };
    users.robot = {
      agent = "codex";
      web.passwordHashFile = "/var/lib/agent-box-web/password-hash-robot";
      sessions.main = { agent = "codex"; };
      environment.AGENT_BOX_EXTRA = "1";
      environmentFiles = [ "/etc/agent-box/robot.extra.env" ];
    };
    web = {
      enable = true;
      domain = "golden.example.org";
      user = "agent";
      # One operator-declared vhost (issue #629), so the site fragment is
      # in the fixture and one-spec-both-backends compares the native
      # renderer's half of it against this one. It replaces the old
      # `import ~/sites/*.caddy` lines the fixture used to carry: extra
      # vhosts are declared in configuration now, and nothing
      # agent-writable is imported into the gateway.
      sites."app.golden.example.org".upstream = "127.0.0.1:3000";
    };
    selfUpdate = {
      enable = true;
      rev = "0000000000000000000000000000000000000000";
    };
    # Rootless containers on (issue 600), so the shared docker unit, the
    # two per-user sudo grants, the runtime-dir tmpfiles rules and the
    # DOCKER_HOST env line are all in the fixture - and so that the native
    # renderer's own half of them is compared against this one by
    # one-spec-both-backends rather than only against itself.
    containers.enable = true;
  };
}
