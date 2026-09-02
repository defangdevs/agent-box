## Your host: a distro box with agent-box in a Nix profile

- This box is NOT NixOS. The base OS is whatever distro image the
  deployment started from (Ubuntu, on the ones agent-box ships) and the
  distro's own package manager still owns it, so its security updates keep
  arriving the usual way. agent-box never touches apt: everything it
  installs lives in a Nix profile at /nix/var/nix/profiles/agent-box, and
  `agentbox apply` renders the users, units, sudoers and Caddyfile from
  the box's declared configuration (by default /etc/agent-box/config.yaml;
  `systemctl cat agent-box-update.service` names the file this box was
  actually applied from).
- Those base-OS patches install and take effect UNATTENDED, with no
  reboot: `agentbox apply` writes the policy (see
  /etc/apt/apt.conf.d/52-agent-box-unattended and
  /etc/needrestart/conf.d/50-agent-box.conf), so unattended-upgrades
  patches and needrestart restarts the affected daemons in place. Your own
  session is excluded from those restarts by name, so an apt run cannot
  kill it. By DEFAULT the box also never reboots itself, but a deployment
  can opt in to a nightly reboot, so read the
  `Unattended-Upgrade::Automatic-Reboot` line in that apt file rather than
  assuming either way.
  A KERNEL patch is the exception - it only takes effect at a boot, and
  the box tells you one is waiting by creating /var/run/reboot-required
  (`cat /var/run/reboot-required.pkgs` names what asked). Say so to the
  person you are working with when you see it. There IS a way to finish
  it - "Restart agent-box" in the settings page's Maintenance section, and the same
  command as a sudo grant if `sudo -ln` lists `systemctl reboot` for you
  - but a reboot kills every session on this box, yours included, so it
  is a thing to be asked for rather than a thing to decide. Ask, unless
  you were already told to.
- Two layers, and it pays to know which one you are looking at.
  `apt list --installed` is the distro's; `nix profile list --profile
  /nix/var/nix/profiles/agent-box` is agent-box's. Tools YOU want go in
  your own profile with `nix profile add` (below) - you cannot apt-install
  anything anyway, since that needs a root you do not have.
- `agentbox` is on your PATH. `agentbox --help` lists what it does, and
  `agentbox apply --dry-run` prints what the box's declared configuration
  would change without changing it - the fastest way to see how this host
  is actually put together.
- SSH works the way the distro image left it, including a provider's
  browser-console SSH if it has one, and first-boot output is in
  /var/log/agent-box-bootstrap.log.
- **Do not identify this machine from the cloud metadata service.** Where
  a provider builds its managed product on top of a lower-level one, IMDS
  describes the machine UNDERNEATH that abstraction: an instance type, a
  lifecycle, a disk size belonging to the layer you are not billed for.
  Reasoning "IMDS says instance-type X, therefore I am on the raw
  service" gets the product, the pricing and the operational model wrong.
  The notes below this guide say what this deployment is; treat them and
  the stack that created the box as the authority, and IMDS as a detail
  about the hardware underneath.
