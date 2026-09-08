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
    # getcap, for the capability assertion below. NixOS puts no libcap
    # binary on the system path by default, so without this the test fails
    # at "getcap: command not found" and reads as a missing capability.
    environment.systemPackages = [ pkgs.libcap ];
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

    # 4. No docker installed, so the unit is inactive with its CONDITION
    #    failed - not failed, not restarting. This is the assertion that
    #    matters most in the file: the key is ConditionFileIsExecutable,
    #    and the plausible-looking ConditionPathIsExecutable is not a
    #    systemd key at all - systemd logs "Unknown key ... ignoring" and
    #    runs the unit, so the no-op becomes a 203/EXEC restart loop
    #    against a binary that is not there. That is what shipped until
    #    this test first ran.
    machine.succeed("systemctl start agent-box-docker@agent.service")
    state = machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=ActiveState --value").strip()
    assert state == "inactive", f"unit is {state}, want inactive"
    result = machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=ConditionResult --value").strip()
    assert result == "no", f"ConditionResult is {result}, want no"
    # The generic guard for the same class of mistake anywhere in the unit:
    # systemd only WARNS about a key it does not know, so a typo is a
    # silent behaviour change. Nothing else in the repo would catch it.
    machine.fail(
        "journalctl -b --grep 'Unknown key' "
        "| grep agent-box-docker >/dev/null")

    # 5. The daemon's XDG_RUNTIME_DIR does NOT exist yet, and should not:
    #    it is the unit's own RuntimeDirectory=, so it appears when the
    #    daemon first runs and (RuntimeDirectoryPreserve=yes) stays after
    #    it stops. An earlier draft made it with tmpfiles and got
    #    "Failed to resolve group 'agent': Unknown group" on a fresh boot,
    #    because that rule races user creation and /run gets no second
    #    chance. Asserted as absent so a silent return to tmpfiles shows up
    #    here rather than as a race nobody reproduces.
    machine.fail("test -e /run/agent-box-docker/agent")

    # 6. The session's DOCKER_HOST, so `docker compose up` needs no flag
    #    and no per-agent setup. Read it out of the RUNNING supervisor,
    #    not off the unit: it arrives through EnvironmentFile=, which
    #    `systemctl show --property=Environment` does not report, and the
    #    question that matters is whether it reaches the process that
    #    starts the tmux server every pane inherits from.
    want = "DOCKER_HOST=unix:///run/agent-box-docker/agent/docker.sock"
    pid = machine.succeed(
        "systemctl show agent-box@agent --property=MainPID --value").strip()
    env = machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ")
    assert want in env.splitlines(), \
        f"the supervisor's environment has no {want}"
    # And it is in the file the unit reads, which is what survives a
    # restart of that process.
    machine.succeed(
        f"grep -Fx '{want}' /etc/agent-box/units/agent.env >/dev/null")

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
    # The fake sends the readiness notification a real dockerd-rootless
    # sends, because the unit is Type=notify: without it the granted
    # `restart` blocks for the whole TimeoutStartSec and then fails, which
    # would be three wasted minutes and a masked assertion rather than a
    # test. Resolved from the DRIVER's shell - the unit's own PATH is
    # deliberately short, and hard-coding a store path here would rot.
    notify = machine.succeed("command -v systemd-notify").strip()
    fake = "/home/agent/.nix-profile/bin/dockerd-rootless"
    machine.succeed(
        f"printf '#!/bin/sh\\n{notify} --ready\\nexec sleep infinity\\n' "
        f"> {fake}")
    machine.succeed(f"chmod 755 {fake}")
    machine.succeed(f"su -s /bin/sh agent -c 'sudo -n {restart}'")
    # The condition passed this time, and the unit is actually running the
    # binary - a start that ran nothing would be inactive, as it was above.
    result = machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=ConditionResult --value").strip()
    assert result == "yes", f"ConditionResult is {result} after installing"
    machine.wait_for_unit("agent-box-docker@agent.service")
    # NOW the runtime dir exists, 0700 agent:agent - the socket inside it
    # is the whole of that daemon's authority over the user's containers
    # and home, so nothing outside that user and root may reach it.
    mode = machine.succeed(
        "stat -c '%a %U %G' /run/agent-box-docker/agent").strip()
    assert mode == "700 agent agent", f"runtime dir is {mode}"
    # As the right user, in its own delegated cgroup, with the runtime
    # dir and the PATH it was given - the four things the unit is for.
    who = machine.succeed(
        "ps -o user= -p $(systemctl show agent-box-docker@agent.service "
        "--property=MainPID --value)").strip()
    assert who == "agent", f"the daemon runs as {who}, want agent"
    delegate = machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=Delegate --value").strip()
    assert delegate == "yes", f"Delegate is {delegate}"
    pid = machine.succeed(
        "systemctl show agent-box-docker@agent.service "
        "--property=MainPID --value").strip()
    env = machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ")
    assert "XDG_RUNTIME_DIR=/run/agent-box-docker/agent" in env.splitlines(), \
        ("the daemon has no XDG_RUNTIME_DIR, so it would put its socket "
         "somewhere no session's DOCKER_HOST points at")
    # And the capped uidmap binaries are reachable from its PATH, which is
    # the one thing rootlesskit cannot do without.
    path = [line for line in env.splitlines() if line.startswith("PATH=")]
    assert path and "/run/wrappers/bin" in path[0], \
        f"the daemon's PATH has no /run/wrappers/bin: {path}"

    # 9. And the runtime dir outlives a stopped daemon, which is what
    #    RuntimeDirectoryPreserve=yes buys and what a session's
    #    DOCKER_HOST needs: the CLI should meet a refused connection, not
    #    a path whose parent has gone.
    machine.succeed(
        "su -s /bin/sh agent -c 'sudo -n "
        "/run/current-system/sw/bin/systemctl stop "
        "agent-box-docker@agent.service'")
    machine.succeed("test -d /run/agent-box-docker/agent")
  '';
}
