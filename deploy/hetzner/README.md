# Hetzner Cloud deployment (`hetzner/`)

One native Ubuntu 24.04 VM running the same agent-box runtime and systemd
configuration as AWS Lightsail and Azure. Hetzner is only the provisioning
layer: the VM stays Ubuntu, Nix supplies a pinned runtime profile, and
`agentbox apply --first-boot` renders the users, units, sudoers, Caddyfile,
password hash, and settings surface.

The reasons this is the default while NixOS remains a supported appliance
backend are recorded in the shared
[deployment-model decision](../../docs/deployment-model.md).

This supersedes the original NixOS/`nixos-anywhere` proposal in
[issue #7](https://github.com/defangdevs/agent-box/issues/7). The closed
[PR #839](https://github.com/defangdevs/agent-box/pull/839) preserves that
experiment and its live-deployment findings.

## Files

| File | Role |
| --- | --- |
| `deploy.sh` | Creates the firewall and Ubuntu server, supplies user data, then waits for valid HTTPS. |
| `render-user-data.sh` | Validates deployment inputs and substitutes only base64 data into the bootstrap. |
| `bootstrap.sh.in` | Runs once through Hetzner cloud-init; installs or reuses Nix and the runtime, writes config, and applies it. |
| `AGENTS.md` | Hetzner-specific guidance appended to the shared platform guide on the box. |

## Launch

The deployment requires an API token, an SSH key already uploaded to the
Hetzner project, and an Argon2id hash of the browser password. The plaintext
password never enters Hetzner user data.

```bash
export HCLOUD_TOKEN='<project API token>'
export HCLOUD_SSH_KEY='<Hetzner SSH key name or ID>'

web_password="$(openssl rand -base64 24)"
printf '%s\n' "$web_password"
export AGENT_BOX_WEB_PASSWORD_HASH="$(
  nix run nixpkgs#caddy -- hash-password --algorithm argon2id \
    --plaintext "$web_password"
)"

nix shell nixpkgs#hcloud nixpkgs#jq --command \
  ./deploy/hetzner/deploy.sh agent-box
```

The checkout's current commit is the default `AGENT_BOX_FLAKE_REF`, making
the installed runtime reproducible. That commit must exist on GitHub. Override
the variable to deploy a different published revision.

Defaults are a `cx23` in `nbg1`, user `workspace`, stock `ubuntu-24.04`, and
key-only root SSH. The created Hetzner firewall admits TCP 443 and, by default,
TCP 22 from IPv4 and IPv6. Relevant overrides are listed by
`deploy.sh --help`; use `HCLOUD_ALLOW_CIDRS_JSON` to narrow ingress and
`HCLOUD_DEBUG_SSH=false` to omit SSH after the bootstrap is proven.

Hetzner's create API completes before cloud-init does. `deploy.sh` therefore
waits for the terminal's real TLS endpoint to answer rather than treating VM
allocation as deployment success. It retains a failed VM for diagnosis and
prints the SSH command and bootstrap-log location.

## Shared native contract

The provider implementations deliberately converge after VM allocation:

| Stage | Lightsail | Azure | Hetzner |
| --- | --- | --- | --- |
| Base OS | Ubuntu 24.04 | Ubuntu 24.04 | Ubuntu 24.04 |
| Software | pinned `#runtime` system profile | pinned `#runtime` system profile | pinned `#runtime` system profile |
| Declared state | `/etc/agent-box/config.yaml` | `/etc/agent-box/config.yaml` | `/etc/agent-box/config.yaml` |
| Activation | `agentbox apply --first-boot` | `agentbox apply --first-boot` | `agentbox apply --first-boot` |
| Completion gate | CloudFormation wait condition | VM extension status | HTTPS readiness poll |

Hetzner-specific code is limited to `hcloud` resource creation, its firewall,
and cloud-init transport. Behavior changes belong in the shared runtime and
native renderer, not in this directory.

One security difference is forced by the providers: Azure protects its
bootstrap in extension `protectedSettings`; Hetzner user data is deployment
metadata. Like Lightsail, the Hetzner path therefore accepts only the password
hash. A project principal that can read user data gains no reusable plaintext
credential.

## Baked Ubuntu image

The fast path is a Hetzner snapshot of a clean Ubuntu image with both Nix and
`/nix/var/nix/profiles/agent-box` preinstalled. It is the Hetzner equivalent of
Azure's `imageIncludesRuntime=true`: software substitution happens in the
image pipeline, while the same bootstrap still writes instance configuration
and runs `agentbox apply --first-boot`.

Launch such an image with:

```bash
export HCLOUD_IMAGE='<snapshot ID>'
export AGENT_BOX_IMAGE_INCLUDES_RUNTIME=true
./deploy/hetzner/deploy.sh agent-box
```

The bootstrap refuses the flag when the expected runtime executable is absent,
so a wrong or stale image fails visibly instead of silently becoming a mutable
stock-image deployment.

Build the snapshot before `agentbox apply`, or sanitize the builder before
shutdown. Never bake any of these into it:

- `/etc/machine-id` or SSH host keys;
- web password hashes, cookie secrets, Caddy ACME account/certificates, or a
  public hostname;
- Hetzner/project credentials, API tokens, agent logins, webhook secrets, or
  session state;
- a deployer's authorized SSH key unless that key is deliberately shared by
  every future instance.

Power the builder off before snapshotting for filesystem consistency. A
snapshot is architecture-specific and incurs image-storage charges; label it
with the agent-box revision and architecture, retain the stock-image path as
the image builder/recovery path, and delete superseded snapshots explicitly.

## Rebuilding an existing test server

Hetzner's rebuild operation now accepts the same user data as server creation.
Rendering it separately makes a controlled destructive rebuild possible:

```bash
deploy/hetzner/render-user-data.sh > /tmp/agent-box-user-data
hcloud server rebuild --image ubuntu-24.04 \
  --user-data-from-file /tmp/agent-box-user-data <server>
```

Rebuild erases the server's disk. It preserves the server resource and Primary
IPs, so the public address remains stable, but it should only be used for a
known-disposable host.

## Failure diagnosis

SSH as root with the Hetzner key and inspect:

```bash
cloud-init status --long
tail -n 200 /var/log/agent-box-bootstrap.log
journalctl -u agent-box@workspace.service \
  -u agent-box-settings@workspace.service \
  -u caddy.service
```

The runtime profile is independently reversible with `nix profile rollback`;
normal deployed-box updates should use the settings page or
`agentbox update`, which re-applies configuration and rolls back a failed
release automatically.
