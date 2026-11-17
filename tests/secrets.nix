# VM test for the secret-custody broker (issue #922) - the half of the
# feature the unit tests cannot reach.
#
# tests/test-secret-custody.py proves the protocol, the encryption, the job
# engine and the redaction with DirectLauncher: every process in it runs as
# ONE uid, so it says nothing about the thing the feature exists for - a
# root-owned custodian that another Linux user cannot read around. That
# needs a real second user, a real root-owned unit and a real
# `systemd-run User=` transient job, which is what this boots.
#
# One node, two users (agent, robot), secrets on by default (this test does
# not set the option, so it also pins the default). Assertions read OUTPUT,
# not exit codes alone: a boundary that "fails" for the wrong reason (a typo
# in a path) looks identical to one that holds.
#
# Pass to pkgs.testers.runNixOSTest.
{ agent-box }:
{
  name = "agent-box-secrets";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    services.agent-box = {
      enable = true;
      agent = "claude";
      eagerAgents = [ "claude" ];
      users.agent = { };
      users.robot = { };
    };
    system.stateVersion = "25.05";
  };

  testScript = ''
    import json
    import shlex

    SOCK = "/run/agent-box-secrets/agent.sock"
    STORE = "/var/lib/agent-box-secrets"
    VALUE = "s3cr3t-VALUE-0123456789"
    # Jobs start with no PATH (minimal ambient env); give the shell one.
    SYSPATH = "PATH=/run/current-system/sw/bin; "

    def as_user(user, cmd, sock=True):
        """Run cmd as `user` with that user's own broker socket exported,
        the way a session sees it. Returns (status, output)."""
        env = f"export AGENT_BOX_SECRET_SOCKET=/run/agent-box-secrets/{user}.sock; " if sock else ""
        return machine.execute(f"su -s /bin/sh {user} -c {shlex.quote(env + cmd)} 2>&1")

    def agent(cmd):
        status, out = as_user("agent", cmd)
        assert status == 0, f"`{cmd}` failed ({status}): {out}"
        return out

    def run_job(args, wait=40, user="agent"):
        """Start a job, wait for it to leave pending/running, return the
        parsed final status."""
        status, out = as_user(user, f"agent-box-secret --profile t exec {args}")
        assert status == 0, f"exec failed: {out}"
        job = out.strip().splitlines()[-1]
        for _ in range(wait):
            status, out = as_user(user, f"agent-box-secret --profile t --json status {job}")
            assert status == 0, f"status failed: {out}"
            reply = json.loads(out.strip().splitlines()[-1])
            if reply["state"] not in ("pending", "running"):
                return job, reply
            machine.sleep(1)
        raise Exception(f"job {job} still running after {wait}s: {out}")

    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_for_unit("agent-box@robot.service")
    machine.wait_for_unit("agent-box-secret-broker@agent.socket")
    machine.wait_for_unit("agent-box-secret-broker@robot.socket")

    with subtest("units, sockets and directories"):
        # Socket: agent-only. A mode wider than 0600 or the wrong owner is
        # the first thing that would quietly widen access.
        assert machine.succeed(f"stat -c '%a %U' {SOCK}").strip() == "600 agent"
        assert machine.succeed("stat -c '%a %U' /run/agent-box-secrets/robot.sock").strip() == "600 robot"
        # The store parent is root-only; the socket dir is traversable (a
        # socket's NAME is not a secret, its mode is what gates connect()).
        assert machine.succeed(f"stat -c '%a %U' {STORE}").strip() == "700 root"
        assert machine.succeed("stat -c '%a %U' /run/agent-box-secrets").strip() == "755 root"
        # The broker itself is root: the whole point.
        machine.succeed("systemctl start agent-box-secret-broker@agent.service")
        pid = machine.succeed(
            "systemctl show agent-box-secret-broker@agent --property=MainPID --value").strip()
        assert machine.succeed(f"stat -c %U /proc/{pid}").strip() == "root"
        machine.fail("journalctl -b --grep 'Unknown key' | grep agent-box-secret >/dev/null")

    with subtest("session environment reaches the supervisor"):
        want = f"AGENT_BOX_SECRET_SOCKET={SOCK}"
        pid = machine.succeed("systemctl show agent-box@agent --property=MainPID --value").strip()
        env = machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ")
        assert want in env.splitlines(), f"supervisor environment has no {want}"
        # robot's own socket, not agent's.
        pid = machine.succeed("systemctl show agent-box@robot --property=MainPID --value").strip()
        env = machine.succeed(f"tr '\\0' '\\n' < /proc/{pid}/environ")
        assert "AGENT_BOX_SECRET_SOCKET=/run/agent-box-secrets/robot.sock" in env.splitlines()

    with subtest("set / list / at-rest encryption / root key"):
        agent(f"printf %s '{VALUE}' | agent-box-secret --profile t set K --stdin")
        out = agent("agent-box-secret --profile t list")
        assert "K\tv1\tconfigured" in out, out
        # The value is nowhere on disk, in any form the broker wrote.
        machine.fail(f"grep -rF '{VALUE}' {STORE}")
        recs = machine.succeed(f"find {STORE} -name 'K*' -type f").strip()
        assert recs, "no record file was written for K"
        # Root key: created by the broker, root-only.
        key = machine.succeed(f"find {STORE} -maxdepth 1 -name '.rootkey' -type f").strip()
        assert key, f"no root key under {STORE}: " + machine.succeed(f"ls -la {STORE}")
        assert machine.succeed(f"stat -c '%a %U' {key}").strip() == "600 root"
        # The agent user can neither list nor read any of it.
        status, out = as_user("agent", f"ls {STORE}", sock=False)
        assert status != 0, f"agent could list the store: {out}"
        status, out = as_user("agent", f"cat {key}", sock=False)
        assert status != 0 and "Permission denied" in out, out

    with subtest("another user cannot reach agent's broker"):
        # File mode: robot cannot even connect.
        status, out = machine.execute(
            "su -s /bin/sh robot -c "
            + shlex.quote(f"export AGENT_BOX_SECRET_SOCKET={SOCK}; agent-box-secret --profile t list")
            + " 2>&1")
        assert status != 0, f"robot reached agent's broker: {out}"
        assert "K" not in out.split(), out
        # robot's own broker has its own empty store.
        _, out = as_user("robot", "agent-box-secret --profile t list")
        assert "configured" not in out, out
        # Root bypasses file modes, so ONLY the SO_PEERCRED check can refuse
        # this one. It must: root is not the user this broker serves.
        status, out = machine.execute(
            f"export AGENT_BOX_SECRET_SOCKET={SOCK}; agent-box-secret --profile t list 2>&1")
        assert status != 0, f"root was served by agent's broker: {out}"
        assert "configured" not in out, out

    with subtest("exec: uid drop, scoped injection, redaction"):
        _, reply = run_job(
            "--secret K --command "
            "'"+SYSPATH+"echo uid=$(id -u) user=$(id -un); echo val=$K; env | grep -c ^K=; "
            "cat " + key + " 2>&1; echo END'")
        out = reply.get("stdout", "")
        agent_uid = machine.succeed("id -u agent").strip()
        assert f"uid={agent_uid} user=agent" in out, out
        assert "val=***redacted***" in out and VALUE not in out, out
        assert "\n1\n" in out, out                      # exactly K, once
        assert "Permission denied" in out, out          # not root: cannot read the key
        assert reply["exitCode"] == 0, reply
        # Nothing but what was asked for, plus the minimal ambient set.
        _, reply = run_job("--command '" + SYSPATH + "env | sort | cut -d= -f1 | tr \"\\n\" \" \"'")
        names = reply.get("stdout", "").split()
        assert "K" not in names and not any("SECRET" in n for n in names), names

    with subtest("scope: a name from another profile is not injectable"):
        status, out = as_user("agent", "agent-box-secret --profile other exec --secret K --command true")
        job = out.strip().splitlines()[-1]
        machine.sleep(2)
        _, out = as_user("agent", f"agent-box-secret --profile other --json status {job}")
        assert "no such secret" in out or "error" in out, out

    with subtest("cancel and timeout reap the whole control group"):
        # The job sleeps for an odd duration and is found by its exact
        # command line: a bare `pgrep -u agent -x sleep` also matches the
        # box's own agent-owned loops (supervisor, attach), which keep a
        # short `sleep` alive almost all the time, so "no sleep left"
        # raced them and timed out.
        JOB_SLEEP = "sleep 3017"
        JOB_PGREP = f"pgrep -u agent -xf '{JOB_SLEEP}'"
        JOB_CMD = "'" + SYSPATH + f"{JOB_SLEEP} & {JOB_SLEEP}'"

        def leftovers():
            units = machine.succeed(
                "systemctl list-units --all --plain --no-legend 'agent-box-secret-job-*' || true").strip()
            procs = machine.execute(JOB_PGREP)[1].strip()
            return units, procs

        # cancel: a shell that backgrounds a second sleep, so killing only
        # the leader would leave an orphan behind.
        status, out = as_user("agent",
            "agent-box-secret --profile t exec --command " + JOB_CMD)
        job = out.strip().splitlines()[-1]
        machine.wait_until_succeeds(JOB_PGREP + " >/dev/null", timeout=20)
        agent(f"agent-box-secret --profile t cancel {job}")
        machine.wait_until_succeeds("! " + JOB_PGREP, timeout=20)
        units, procs = leftovers()
        assert not units and not procs, f"cancel left units={units!r} procs={procs!r}"

        # timeout: same shape, reaped by the deadline instead of the caller.
        _, reply = run_job("--timeout-ms 2000 --command " + JOB_CMD, wait=30)
        assert reply["state"] == "timeout", reply
        machine.wait_until_succeeds("! " + JOB_PGREP, timeout=20)
        units, procs = leftovers()
        assert not units and not procs, f"timeout left units={units!r} procs={procs!r}"

    with subtest("the broker restarts without losing the store"):
        machine.succeed("systemctl restart agent-box-secret-broker@agent.service")
        machine.wait_for_unit("agent-box-secret-broker@agent.socket")
        out = agent("agent-box-secret --profile t list")
        assert "K\tv1\tconfigured" in out, out
        _, reply = run_job("--secret K --command 'echo val=$K'")
        assert "val=***redacted***" in reply.get("stdout", ""), reply

    with subtest("a corrupt record is refused, not served, and does not wedge the broker"):
        rec = machine.succeed(f"find {STORE}/agent -name 'K*' -type f | head -1").strip()
        machine.succeed(f"cp {rec} /tmp/K.rec.bak && printf 'garbage' > {rec}")
        # The job must end in "error" naming the corrupt record: a job that
        # ran anyway (state "done", exit 0) would also print no VALUE, so
        # checking the output alone cannot tell refusal from execution.
        _, reply = run_job("--secret K --command 'echo ran'")
        assert reply["state"] == "error", reply
        assert reply.get("exitCode") is None, reply
        assert "corrupt" in reply.get("stderr", ""), reply
        assert "ran" not in reply.get("stdout", ""), reply
        assert VALUE not in json.dumps(reply), reply
        # Still answering other requests while the corrupt record is present:
        # a different profile's list must SUCCEED, not merely not hang.
        agent("agent-box-secret --profile unaffected list")
        machine.succeed(f"cp /tmp/K.rec.bak {rec}")

    with subtest("env-exec strips protected names, tombstones included"):
        # Any worker harness: the profile only names the default namespace
        # (master refuses HARNESS=shell profiles, which are not workers).
        agent("agent-box-profile set t HARNESS=claude")
        agent("agent-box-profile default t")
        agent("printf %s other | agent-box-secret --profile t set T --stdin")
        agent("agent-box-secret --profile t remove T")
        agent("mkdir -p ~/.config/agent-box && "
              "printf 'K=legacy-plain\\nT=legacy-tomb\\nKEEP=ok\\n' >> ~/.config/agent-box/env")
        exec_bin = machine.succeed(
            "nix-store -qR /run/current-system | grep 'agent-box-env-exec$' | head -1").strip()
        assert exec_bin, "no agent-box-env-exec in the system closure"
        out = agent(f"{exec_bin} env")
        names = [line.split("=", 1)[0] for line in out.splitlines() if "=" in line]
        assert "K" not in names, "configured name leaked into the session environment"
        assert "T" not in names, "tombstoned name leaked into the session environment"
        assert "KEEP=ok" in out.splitlines(), "an unrelated name must pass through"
        assert f"AGENT_BOX_SECRET_SOCKET={SOCK}" in out.splitlines(), out
  '';
}
