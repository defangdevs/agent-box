# The same mitigation is activated by native apply and a NixOS switch.
# Exercise the real kernel filter, reload, and persistence across a reboot.
{ agent-box }:
{
  name = "agent-box-ipv4-tcp-mitigation";
  node.pkgsReadOnly = false;
  nodes.machine = { pkgs, lib, ... }: {
    imports = [ agent-box ];
    services.agent-box = {
      enable = true;
      installAgents = [ "claude" ];
      protectMemory = false;
      containers.enable = false;
      users.agent = { };
    };
    networking.nftables.enable = true;
    networking.nftables.flushRuleset = true;
    environment.systemPackages = [
      pkgs.caddy
      (lib.hiPrio (pkgs.python3.withPackages (ps: [ ps.scapy ])))
    ];
    environment.etc."ipv4-tcp-mitigation-test.py".source = ./ipv4-tcp-mitigation.py;
    system.stateVersion = "25.05";
  };
  testScript = ''
    machine.start(allow_reboot=True)
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("agent-box-cve-2026-98432.service")
    machine.succeed("python3 /etc/ipv4-tcp-mitigation-test.py")
    # A live MPTCP listener survives the sysctl change until it is restarted.
    machine.succeed("sysctl -w net.mptcp.enabled=1")
    machine.succeed("printf '{\\n admin off\\n}\\n:18081 {\\n respond ok\\n}\\n' > /tmp/caddy-probe.conf")
    machine.succeed("systemd-run --unit=mptcp-caddy-probe --property=Environment=GODEBUG=multipathtcp=1 /run/current-system/sw/bin/caddy run --config /tmp/caddy-probe.conf --adapter caddyfile")
    machine.wait_until_succeeds("ss --mptcp --listening --numeric --no-header | grep ':18081' >/dev/null", timeout=30)
    machine.succeed("systemctl reload agent-box-cve-2026-98432.service")
    machine.succeed("ss --mptcp --listening --numeric --no-header | grep ':18081' >/dev/null")
    machine.succeed("systemctl restart mptcp-caddy-probe.service")
    machine.wait_until_succeeds("curl --fail http://127.0.0.1:18081", timeout=30)
    machine.fail("ss --mptcp --listening --numeric --no-header | grep ':18081' >/dev/null")
    machine.succeed("systemctl stop mptcp-caddy-probe.service")
    machine.succeed("systemctl reload nftables.service")
    machine.wait_until_succeeds("nft list chain ip cve_2026_98432 prerouting")
    machine.succeed("python3 /etc/ipv4-tcp-mitigation-test.py")
    machine.succeed("systemctl restart nftables.service")
    machine.wait_for_unit("agent-box-cve-2026-98432.service")
    machine.succeed("python3 /etc/ipv4-tcp-mitigation-test.py")
    machine.succeed("systemctl stop nftables.service")
    machine.succeed("systemctl start nftables.service")
    machine.wait_for_unit("agent-box-cve-2026-98432.service")
    machine.succeed("python3 /etc/ipv4-tcp-mitigation-test.py")
    machine.reboot()
    machine.wait_for_unit("agent-box-cve-2026-98432.service")
    machine.succeed("systemctl is-enabled agent-box-cve-2026-98432.service")
    machine.succeed("python3 /etc/ipv4-tcp-mitigation-test.py")
  '';
}
