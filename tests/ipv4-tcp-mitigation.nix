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
    machine.succeed("systemctl reload nftables.service")
    machine.wait_until_succeeds("nft list chain ip cve_2026_98432 prerouting")
    machine.succeed("python3 /etc/ipv4-tcp-mitigation-test.py")
    machine.reboot()
    machine.wait_for_unit("agent-box-cve-2026-98432.service")
    machine.succeed("systemctl is-enabled agent-box-cve-2026-98432.service")
    machine.succeed("python3 /etc/ipv4-tcp-mitigation-test.py")
  '';
}
