## This box (Hetzner Cloud)

- This is a Hetzner Cloud VM running Ubuntu with agent-box installed through
  Nix. Ubuntu remains the base operating system: apt and unattended-upgrades
  own it, while agent-box lives in `/nix/var/nix/profiles/agent-box` and
  renders its services as native systemd units.
- The local disk persists but RAM does not. A reboot loses live tmux sessions,
  so save working context under your home directory.
- The public IPv4 and the sslip.io URL remain attached to this server across
  reboots and power cycles. Always read `$AGENT_BOX_URL` rather than hard-code
  either value.
- Hetzner Cloud has no managed serial shell equivalent to Azure's. Key-only
  root SSH is the recovery path when enabled at deployment time, and
  `/var/log/agent-box-bootstrap.log` records first-boot progress.
- Scheduled Nix garbage collection reclaims store space. A larger server type
  can expand the local disk, but shrinking it requires a fresh deployment.
