# NixOS host profile for Hetzner Cloud, installed with nixos-anywhere.
#
# The flake supplies the deployment SSH key and, when requested, the public
# IPv4/sslip.io name plus its Caddy password hash through impure environment
# variables.  Keeping those values outside the repository makes the profile
# reusable and avoids baking an operator's key into a published flake.
{ config, lib, modulesPath, pkgs, sshPublicKey ? "", webDomain ? "", webPasswordHash ? "", ... }:
let
  webEnabled = webDomain != "" && webPasswordHash != "";
  passwordFile = "/etc/agent-box-web/password-hash";
in
{
  imports = [
    (modulesPath + "/profiles/qemu-guest.nix")
    (import ../deploy/hetzner/disk-config.nix { })
  ];

  boot.loader.grub = {
    enable = true;
    # qemu-guest.nix supplies the singular device form; use the list form
    # explicitly so the generated mirroredBoots contains the disk once.
    device = lib.mkForce "";
    devices = lib.mkForce [ "/dev/sda" ];
  };

  # DHCP is the stable network contract for Hetzner Cloud's public interface.
  networking.useDHCP = lib.mkDefault true;
  networking.firewall.allowedTCPPorts = [ 22 ] ++ lib.optional webEnabled 80 ++ lib.optional webEnabled 443;

  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = false;
  };

  # The deployment key is required for both nixos-anywhere and post-install
  # verification.  An empty value keeps pure evaluation useful for CI, but a
  # real deployment must provide it.
  users.users.root.openssh.authorizedKeys.keys = lib.optional (sshPublicKey != "") sshPublicKey;

  services.agent-box = {
    enable = true;
    agent = "claude";
    eagerAgents = [ "claude" ];
    users.agent = {
      environment.TERM = "xterm-256color";
      web.passwordHashFile = lib.mkIf webEnabled passwordFile;
    };
    web = {
      enable = webEnabled;
      domain = lib.mkIf webEnabled webDomain;
      user = "agent";
    };
  };

  users.users.agent.openssh.authorizedKeys.keys = lib.optional (sshPublicKey != "") sshPublicKey;

  # The hash is not a plaintext credential, but keep the file root-readable
  # only.  This block is active only for an explicitly requested web terminal.
  environment.etc."agent-box-web/password-hash" = lib.mkIf webEnabled {
    text = webPasswordHash + "\n";
    mode = "0400";
    user = "root";
    group = "root";
  };

  system.stateVersion = "25.05";
}
