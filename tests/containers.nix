# VM test for containers.enable (issue 600): the HOST half of rootless
# docker, which is the half an unprivileged session cannot arrange for
# itself and therefore the half agent-box owns.
#
# Deliberately NOT "a container runs". Docker is not shipped - its closure
# is 972 MiB, larger than the whole runtime profile - so it comes from the
# user's own nix profile, and there is no network in a VM test to fetch it
# from. What IS asserted here is everything the daemon needs the box to
# have already done, plus the behaviour the design leans on hardest: with
# no docker installed the unit must be a clean no-op rather than a restart
# loop, and it must pick one up without a rebuild.
#
# The last of those is proved with a FAKE dockerd-rootless in the user's
# profile path. That is not a stand-in for the real daemon (a real one
# needs the whole 972 MiB and a kernel this VM would have to be given
# nested cgroup delegation for); it is a stand-in for "the user ran
# `nix profile add nixpkgs#docker`", which is the only thing the unit's
# condition can actually observe.
#
# Pass to pkgs.testers.runNixOSTest.
{ agent-box }:
{
  name = "agent-box-containers";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    services.agent-box = {
      enable = true;
      agent = "claude";
      eagerAgents = [ "claude" ];
      containers.enable = true;
      users.agent = { };
    };
    system.stateVersion = "25.05";
  };

  testScript = ''
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("agent-box@agent.service")

    # 1. The subuid range rootlesskit maps a container's users into.
    #    users-groups.nix defaults autoSubUidGidRange on for a normal user,
    #    and the module asserts that rather than trusting it - so if this
    #    line ever fails, the assertion should have fired at eval and did
    #    not.
    machine.succeed("grep -E '^agent:[0-9]+:[0-9]+$' /etc/subuid >/dev/null")
    machine.succeed("grep -E '^agent:[0-9]+:[0-9]+$' /etc/subgid >/dev/null")

    # 2. newuidmap/newgidmap with the FILE capabilities that apply it. A
    #    read-only nix store path cannot carry one, which is the whole
    #    reason /run/wrappers exists here and the reason the native backend
    #    has to make capped copies of its own.
    caps = machine.succeed("getcap /run/wrappers/bin/newuidmap")
    assert "cap_setuid" in caps, f"newuidmap has no cap_setuid: {caps}"
    caps = machine.succeed("getcap /run/wrappers/bin/newgidmap")
    assert "cap_setgid" in caps, f"newgidmap has no cap_setgid: {caps}"

    # 3. NixOS does not restrict unprivileged user namespaces, so the agent
    #    can make one - the thing an Ubuntu box refuses until the AppArmor
    #    profile the native renderer writes is loaded. Proving it HERE is
    #    what says the two backends reach the same place by different
    #    routes.
    machine.succeed("su -s /bin/sh agent -c 'unshare -Ur id' >/dev/null")

    # 4. XDG_RUNTIME_DIR for the daemon: 0700 agent:agent, because the
    #    socket inside it is the whole of that daemon's authority over the
    #    user's containers and home. NOT a RuntimeDirectory= - it has to
    #    outlive a stopped daemon, so it must be here with the unit down.
    machine.succeed("test -d /run/agent-box-docker/agent")
    mode = machine.succeed(
        "stat -c '%a %U %G' /run/agent-box-docker/agent").strip()
    assert mode == "700 agent agent", f"runtime dir is {mode}"

    # 5. No docker installed, so the unit is inactive with its CONDITION
    #    failed - not failed, not restarting. The distinction is the whole
    #    reason ConditionPathIsExecutable is there: Restart=always over a
    #    missing binary would burn the start limit on every box that never
    #    installs docker, which is every box on its first boot.
    machine.succeed("systemctl start agent-box-docker@agent.service")
    state = machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=ActiveState --value").strip()
    assert state == "inactive", f"unit is {state}, want inactive"
    result = machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=ConditionResult --value").strip()
    assert result == "no", f"ConditionResult is {result}, want no"

    # 6. The session's DOCKER_HOST, so `docker compose up` needs no flag
    #    and no per-agent setup. Read off the unit rather than out of the
    #    env file, because the file only matters if the unit loads it.
    env = machine.succeed(
        "systemctl show agent-box@agent --property=Environment --value")
    want = "DOCKER_HOST=unix:///run/agent-box-docker/agent/docker.sock"
    assert want in env, f"agent unit has no {want}: {env}"

    # 7. The grant, byte for byte. sudoers matches argv exactly, so a rule
    #    that differs from the command the shipped guide prints asks for a
    #    password instead of running (#353) - and the guide is where the
    #    agent reads that command.
    restart = ("/run/current-system/sw/bin/systemctl restart "
               "agent-box-docker@agent.service")
    machine.succeed(
        f"su -s /bin/sh agent -c 'sudo -n -l {restart}' >/dev/null")
    guide = machine.succeed("cat /etc/agent-box-guides/AGENTS.agent.md")
    assert f"sudo -n {restart.replace('@agent.', '@$(whoami).')}" in guide, \
        "the shipped guide does not print the command that is granted"

    # ...and it stops at the user boundary. A rootless daemon is root over
    # its user's containers and home, so this must stay one user's power
    # over its own work only.
    machine.fail(
        "su -s /bin/sh agent -c 'sudo -n -l "
        "/run/current-system/sw/bin/systemctl restart "
        "agent-box-docker@root.service' >/dev/null")

    # 8. A newly installed docker is picked up by the granted command
    #    alone - no rebuild, no root beyond that one grant. The condition
    #    is re-evaluated at every start, which is what makes `restart` the
    #    right verb to grant and the right one to document.
    machine.succeed(
        "install -d -o agent -g agent /home/agent/.nix-profile/bin")
    machine.succeed(
        "printf '#!/bin/sh\\nsleep infinity\\n' "
        "> /home/agent/.nix-profile/bin/dockerd-rootless")
    machine.succeed("chmod 755 /home/agent/.nix-profile/bin/dockerd-rootless")
    machine.succeed(f"su -s /bin/sh agent -c 'sudo -n {restart}' || true")
    # Type=notify and the fake never notifies, so it stays "activating" -
    # which is exactly what proves the condition passed and the unit is
    # running the binary. Anything that ran nothing would be inactive.
    machine.wait_until_succeeds(
        "systemctl show agent-box-docker@agent.service "
        "--property=ConditionResult --value | grep '^yes$' >/dev/null")
    machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=ActiveState --value | grep -E '^(active|activating)$' "
        ">/dev/null")
  '';
}
