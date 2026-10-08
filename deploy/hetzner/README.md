# Hetzner Cloud deployment (`hetzner/`)

The Hetzner path installs a native NixOS agent-box on a Cloud VM. Hetzner
does not provide a provider-specific NixOS image hook, so the bootstrap uses
an ordinary temporary Linux image and `nixos-anywhere`: it kexecs the NixOS
installer, applies the Disko layout, copies the flake closure, installs GRUB,
and reboots into the resulting system.

This is the implementation for [issue #7](https://github.com/defangdevs/agent-box/issues/7).

## Files

- [`../../hosts/hetzner.nix`](../../hosts/hetzner.nix) is the NixOS host
  profile. It enables SSH, the agent-box service, and the browser terminal.
- [`disk-config.nix`](./disk-config.nix) describes Hetzner's `/dev/sda`: a
  1 MiB BIOS partition and an ext4 root partition using the remaining disk.
- [`../../flake.nix`](../../flake.nix) exposes the `nixosConfigurations.hetzner`
  target and pins Disko in `flake.lock`.

## Deployment flow

Create a temporary `ubuntu-24.04` server with an SSH key, then run
`nixos-anywhere` against `.#hetzner`. The deployment environment supplies the
operator's public key and, when the web terminal is wanted immediately, the
`sslip.io` hostname and Caddy password hash through `--impure` environment
variables:

```bash
export AGENT_BOX_HETZNER_SSH_KEY="$(cat ~/.ssh/agent-box-hetzner.pub)"
export AGENT_BOX_HETZNER_WEB_DOMAIN="<dashed-ip>.sslip.io"
export AGENT_BOX_HETZNER_WEB_PASSWORD_HASH="<caddy argon2id hash>"

nix run --impure github:nix-community/nixos-anywhere -- \
  --flake .#hetzner \
  --target-host root@<server-ip> \
  -i ~/.ssh/agent-box-hetzner \
  --option pure-eval false \
  --build-on local
```

The supplied key is installed for both `root` and `agent`, while password SSH
authentication remains disabled. The web terminal is enabled only when both
web variables are present; leaving them empty produces an SSH-only NixOS host
that can be configured later.

The current end-to-end test used a `cx23` in `nbg1`, installed NixOS 26.11,
and verified the agent-box supervisor, settings service, Caddy, ttyd, SSH,
and Let's Encrypt certificate after reboot. The server remains available as
the live test instance; it is labelled `purpose=agent-box-issue-7`.

## Baked image investigation

The practical Hetzner baked image is a **snapshot**, not a qcow2 artifact.
After a clean NixOS install, power the source server off and create a Hetzner
snapshot. New servers can then be created directly from that snapshot. This
removes the kexec/Disko install from the steady-state launch path, at the
cost of snapshot storage and an image refresh whenever the agent-box runtime
changes. Snapshots are architecture-specific and do not include attached
Volumes.

The current live test server is not a golden-image source yet. It contains
instance-specific SSH host keys, machine identity, Caddy ACME state, and the
web password/domain. A production image pipeline should first build a generic
profile with the runtime installed, then inject these values on first boot:

- SSH credentials and the per-instance web password;
- the dashed public-IP hostname and Caddy certificate state;
- machine-id and SSH host keys;
- agent sessions, webhook subscriptions, tokens, and other user state.

Hetzner's own application-image model is a useful precedent: bake software
with Packer, then generate dynamic credentials and instance data at deploy
time with cloud-init. For agent-box, a NixOS first-boot systemd unit is a
better fit than baking secrets into the flake or snapshot. It should consume
short-lived deployment metadata, write root-only runtime secrets, and only
then enable the public web service.

The snapshot should be versioned with labels such as `agent-box`, `git-rev`,
`architecture`, and `created-at`. Keep the current nixos-anywhere path as the
recovery and image-builder path; it is also the fallback when a snapshot is
stale or unavailable.

Do not create a snapshot automatically from a running production host. Hetzner
recommends powering the source off for filesystem consistency, and the
snapshot consumes billed image storage. The implementation should add an
explicit, disposable image-build step once the first-boot secret injection
contract is settled.

## Failure diagnosis

If the kexec install fails, the temporary Linux server is still available for
SSH diagnostics. If the installed NixOS host fails after reboot, use the
Hetzner rescue system or rebuild a temporary server and rerun the same target;
the flake and Disko layout are deliberately self-contained.
