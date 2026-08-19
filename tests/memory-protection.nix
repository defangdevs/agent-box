# VM test for protectMemory (issue 62): zram swap is active, earlyoom
# runs, the agent unit carries the raised OOMScoreAdjust — and, the actual
# incident scenario, a runaway memory hog gets killed by earlyoom while
# the box stays responsive, instead of the no-swap refault livelock that
# froze a 2 GB CFN box (Caddy, sshd and SSM included) for six hours.
#
# Pass to pkgs.testers.runNixOSTest.
{ agent-box }:
{
  name = "agent-box-memory-protection";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    services.agent-box = {
      enable = true;
      agent = "claude";
      users.agent = { };
    };
    system.stateVersion = "25.05";
  };

  testScript = ''
    machine.wait_for_unit("multi-user.target")
    # TEMPORARY DIAGNOSTIC (agent-box#154 Phase 3 CI investigation) — remove
    # before merge. Printing the fully-merged unit systemd actually loaded,
    # since eval/local-build both show the correct ExecStart but the real
    # boot keeps reporting "Unable to locate executable 'agent-box-supervisor'".
    machine.sleep(5)
    # systemctl cat proved the template-level "agent-box@.service.d/" drop-in
    # never gets merged (only the base unit + the per-instance drop-in show
    # up), which contradicts systemd.unit(5)'s documented "instance .d/ THEN
    # template .d/" lookup — check the filesystem directly to see whether
    # the directory is actually missing (a render/link bug on our side) or
    # present-but-ignored (a systemd-side surprise), and look for any
    # drop-in load warning in the journal we might have missed.
    print(machine.succeed("ls -la /etc/systemd/system/ | grep 'agent-box@' || echo NO_AGENT_BOX_AT_ENTRIES"))
    print(machine.succeed("ls -la /etc/systemd/system/agent-box@.service.d/ 2>&1 || echo TEMPLATE_DROPIN_DIR_MISSING"))
    print(machine.succeed("cat /etc/systemd/system/agent-box@.service.d/overrides.conf 2>&1 || echo TEMPLATE_DROPIN_FILE_MISSING"))
    print(machine.succeed("readlink -f /etc/systemd/system/agent-box@.service.d/overrides.conf 2>&1 || echo NO_REAL_TARGET"))
    print(machine.succeed("journalctl -b --no-pager 2>&1 | grep -i 'agent-box@' || echo NO_JOURNAL_MENTIONS"))
    print(machine.succeed("systemctl cat agent-box@agent.service 2>&1 || true"))
    # The on-disk drop-in is provably correct (confirmed above) yet the unit
    # keeps resolving the bare name from the very first restart at boot —
    # testing whether systemd loaded the unit BEFORE indexing this drop-in
    # and never re-merged it since (Restart=always reuses the SAME loaded
    # config, it doesn't re-read files per restart).
    print(machine.succeed("systemctl daemon-reload 2>&1 || true"))
    print(machine.succeed("systemctl show agent-box@agent.service --no-pager -p ExecStart 2>&1 || true"))
    machine.succeed("systemctl reset-failed agent-box@agent.service || true")
    machine.succeed("systemctl restart agent-box@agent.service || true")
    machine.sleep(2)
    print(machine.succeed("systemctl show agent-box@agent.service --no-pager -p ExecStart -p ActiveState -p SubState 2>&1 || true"))
    print(machine.succeed("systemctl show agent-box@agent.service --no-pager -p ExecStart -p ExecStop -p Environment -p LoadState -p LoadError 2>&1 || true"))
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_for_unit("earlyoom.service")

    # This is the only test that runs with web.enable off (the default), so it
    # is also the guard for the ~/sites ReadWritePaths entry: the snippet dir is
    # tmpfiles-created only when web is on, and listing it unconditionally made
    # namespace setup fail with 226/NAMESPACE — the unit above never started.
    machine.fail(
        "systemctl show agent-box@agent --property=ReadWritePaths --value "
        "| grep agent-box-sites >/dev/null"
    )

    # zram swap is active and sized to RAM (memoryPercent = 100)
    print(machine.succeed("swapon --show"))
    machine.succeed("swapon --show=NAME --noheadings | grep zram0 >/dev/null")

    # the zram sysctl tuning landed
    assert machine.succeed("sysctl -n vm.swappiness").strip() == "180"
    assert machine.succeed("sysctl -n vm.page-cluster").strip() == "0"

    # the agent unit's main process runs with the raised OOM score
    main_pid = machine.succeed(
        "systemctl show -p MainPID --value agent-box@agent.service"
    ).strip()
    assert main_pid != "0", "agent unit has no main PID"
    adj = machine.succeed(f"cat /proc/{main_pid}/oom_score_adj").strip()
    assert adj == "500", f"agent oom_score_adj = {adj}, want 500"

    # Provoke the incident: an unbounded allocator that on a swapless box
    # would livelock the whole VM. tail buffers all of /dev/zero in RAM;
    # transient unit so the hog is not a child of the test's own shell.
    # Absolute path: transient units get systemd's default PATH
    # (/usr/bin:/bin), which doesn't exist on NixOS.
    machine.execute(
        "systemd-run --unit=memhog sh -c "
        "'/run/current-system/sw/bin/tail /dev/zero > /dev/null'"
    )

    # earlyoom notices the pressure and SIGTERM/SIGKILLs the hog...
    machine.wait_until_succeeds(
        "journalctl -u earlyoom.service | grep -E 'sending SIG(TERM|KILL) to process' | grep tail >/dev/null",
        timeout=180,
    )
    machine.wait_until_fails("pgrep -x tail", timeout=60)

    # ...and the box came through responsive, management plane intact.
    machine.succeed("systemctl is-active earlyoom.service")
    machine.succeed("systemctl is-active agent-box@agent.service")
    print(machine.succeed("journalctl -u earlyoom.service | tail -20"))
  '';
}
