# AWS deployment (`aws/`)

CloudFormation templates that provision a single-agent agent-box host with a
browser terminal (Caddy + ttyd). The deployment form lets the user choose
Claude Code or Codex.

- `lightsail-template.yaml` - the **default** template (the README's Launch
  buttons point here): agent-box on **AWS Lightsail** for one flat monthly
  bundle price, installed natively on the stock Ubuntu blueprint with Nix.
  See ["Lightsail variant"](#lightsail-variant-lightsail-templateyaml)
  below. There is exactly one Lightsail template; the `nixos-infect` one
  that used to carry this filename is gone (issue #390).
- `template.yaml` - the EC2 alternative (on-demand with a Spot opt-in, IPv4
  by default with an IPv6-only opt-out, SSM root access, resizable EBS).
  Most of this document describes it; the Lightsail section covers what
  differs.

## What the template does

- Provisions its own VPC (10.42.0.0/16) with an Amazon-provided IPv6 CIDR,
  a single public subnet with a /64 IPv6 range, IGW + routes for v4 and
  v6. First-boot dependencies are fetched from dual-stack hosts, so the
  IPv6-only launch does not require NAT64/DNS64.
- Launches one EC2 instance from the latest NixOS 26.05 AMI for the region.
- Uses EC2 user-data as a NixOS configuration: imports the pinned
  `agent-box` module, sets `services.agent-box.agent` from the `Agent`
  parameter, and enables the module's web terminal (Caddy, TLS-ALPN-01 only,
  plus a per-user `ttyd` on `127.0.0.1:7681` that attaches to `agent`'s tmux
  session; `TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box -t main` - the
  socket lives under `/run` because the agent runs with `PrivateTmp`).
- **Basic-auth-to-cookie web auth**. The terminal lives at `/<UserName>/`
  (default `/agent/`); Caddy prompts for the `UserName` (the linux user name
  selects the terminal) and the `WebPassword`, sets an
  `HttpOnly; Secure; SameSite=Strict` cookie, then lets browser WebSocket
  upgrades authenticate with that cookie. ttyd still binds only to localhost.
  The site root serves an unauthenticated index page listing the configured
  terminals (just the one `UserName` on this template).
- The stack output URL is `https://<host>.sslip.io/<UserName>/`; sign in as
  the `UserName` with the `WebPassword`. The URL deliberately carries no
  `user@` userinfo: Chrome answers the auth challenge with URL userinfo plus
  an empty password, and credentials typed into the prompt cannot override
  the URL-embedded identity (issue 56).
- `<UserName>-main@<host>.sslip.io` becomes the Claude Remote Control session
  name (default user: `agent`), where `<host>.sslip.io` is the box's public
  address — so the box is identifiable and reachable in the Claude apps.
  Sessions added at runtime derive the same way (`<UserName>-<session>@...`).
  Override per user via `remoteControlName`, or box-wide via
  `services.agent-box.remoteControlHost`, in the NixOS config post-deploy.
- The hostname `<addr>.sslip.io` is derived at CFN time via `Fn::Split` +
  `Fn::Join '-'`: on the NetworkInterface's PrimaryIpv6Address, split on
  `:` (IPv6 mode), or on the EIP address, split on `.` (IPv4 mode) - both
  address families end up as the same dashed shape, e.g.
  `63-182-190-210.sslip.io` rather than `63.182.190.210.sslip.io` (issue
  359; only the derived shape gets a Let's Encrypt cert, so the "other"
  spelling fails TLS instead of just not existing). Consecutive `::` in an
  IPv6 address becomes an empty split element that re-joins as `--` -
  matches sslip.io's encoding exactly. Existing stacks keep whatever
  shape they baked at first boot: the hostname is written into the NixOS
  config and the issued certificate, so this only changes newly deployed
  EC2 stacks, not ones already running.
- Requires IMDSv2 (`HttpTokens: required`).
- Disables `amazon-init` after the first successful apply so local edits to
  `/etc/nixos/configuration.nix` survive reboots.
- Attaches an IAM instance profile with `AmazonSSMManagedInstanceCore` so the
  deployer has a root path onto the box via SSM Session Manager. See
  ["Root access via SSM Session Manager"](#root-access-via-ssm-session-manager)
  below. Opt out with `EnableSsm=false` to skip the IAM resources and the
  launch console's CAPABILITY_IAM acknowledgment.
- Sizes the root volume via `RootVolumeSize` (default 30 GiB gp3) and keeps
  it from filling up. See ["Disk headroom"](#disk-headroom) below.

### Disk headroom

The NixOS AMI's own snapshot is only a few GiB, and the nix store grows with
every `nixos-rebuild` — including agent-triggered self-updates — so an
unsized root volume eventually wedges the whole box: a full root means no
journal, no rebuilds, and usually a stuck agent, on a box where nobody is
around to run garbage collection by hand.

Three layers keep that from happening:

- **`RootVolumeSize` parameter** (default 30 GiB, gp3, ~$0.08/GiB-month).
  The `BlockDeviceMappings` device name must match the AMI's
  `RootDeviceName` (`/dev/xvda` on official NixOS AMIs); a mismatch would
  silently attach a second volume instead of sizing the root.
- **Automatic nix GC**: `nix.gc.automatic` prunes generations older than 7
  days on a timer, and `nix.settings.min-free`/`max-free` trigger GC
  mid-build whenever free space dips below 1 GiB (freeing up to 5 GiB) —
  the case that matters, since rebuilds are what eat the disk. The journal
  is capped at 200M (`SystemMaxUse`), as it otherwise claims 10% of the fs.
- **Grow-on-boot**: NixOS's amazon image expands the partition and
  filesystem to fill the volume on every boot, not just the first. So a
  running box that still fills up needs no in-instance tooling: enlarge the
  volume in the EC2 console (Volumes -> Modify), then reboot the instance.
  EBS cannot shrink volumes, only grow them.

### Root access via SSM Session Manager

The template ships no SSH `KeyName`, so on default settings SSM is the only
privileged path onto the box. This matters most when first boot fails:
`amazon-init` writes to the systemd journal only, and `ec2:GetConsoleOutput`
does not capture it — without SSM there is nothing to look at.

The `amazon-ssm-agent` NixOS service is enabled unconditionally by
`virtualisation/amazon-image.nix` on the AMIs we use, so the only piece the
template adds is an IAM instance profile carrying the
`AmazonSSMManagedInstanceCore` managed policy (attached when `EnableSsm=true`,
the default). Once the instance registers with SSM (usually within a minute
of `nixos-rebuild switch` completing), open a root shell either from the AWS
console (Systems Manager -> Session Manager -> Start session -> pick the
instance -> Start session) or from the CLI:

```bash
aws ssm start-session --target <InstanceId> --region <region>
```

`<InstanceId>` is in the stack Outputs. The session lands as the `ssm-user`
account with passwordless sudo; `sudo -i` gets you a root shell for
`journalctl -u amazon-init`, `nixos-rebuild switch`, etc. No SSH key, no
inbound port opened - Session Manager tunnels via the ssm-agent's outbound
connection to the SSM service.

To skip the IAM resources entirely - and avoid the launch console's
CAPABILITY_IAM acknowledgment checkbox - set `EnableSsm=false` at launch.
The box then has no privileged access path; only choose this if you are
comfortable tearing the stack down and redeploying to recover from a broken
first boot.

### Updating a deployed box (user- or agent-triggered)

The launch-time `AgentBoxRev`/`AgentBoxSha256` parameters pin the module for
the FIRST boot only. The generated `/etc/nixos/configuration.nix` prefers
`/etc/nixos/agent-box-pin.nix` when that file exists, and
`agent-box-update.service` — a root oneshot enabled via the module's
`selfUpdate` option — owns that file: it resolves upstream master's HEAD,
verifies it is strictly ahead of the running revision (history rewrites and
downgrade replays are refused), hash-pins the fetched module, rewrites the pin
file atomically, and runs `nixos-rebuild switch`. On rebuild failure the pins
roll back and the running system is unchanged.

The same run also advances `/etc/nixos/agent-box-agent-pin.nix` — a second
pin, holding the latest nixos-unstable channel-release tarball — from which
only the agent CLI packages (`claude-code`, `codex`) are resolved. The box
itself stays on its release channel; the fast-moving agent CLIs track
nixos-unstable, closing most of the version gap to upstream releases without
giving up reproducibility (the pin is URL + hash, and Hydra has the binaries).

Two triggers, both privilege-checked the same way: the "Update box" button on
each user's settings page, and the agent running
`sudo systemctl start agent-box-update.service` in its terminal — the
sudoers entries match those literal commands, so no arguments, environment
or paths cross the privilege boundary; the caller can only say "go". Save
any working context first: the rebuild restarts changed agent services,
killing their sessions mid-update.

The updater trusts the pinned GitHub repo as published (TLS + hash-pinning of
what it fetched). Signature verification against an offline key is tracked in
[issue 46](https://github.com/defangdevs/agent-box/issues/46).

### WebPassword storage

`WebPassword` is required (16-64 chars). Even Claude Code stacks that intend to
drive the box from the Claude apps via Remote Control need it: the first
`claude login` still runs in the browser terminal, and Remote Control only
takes over after that credential lands on disk. AWS masks the field once
entered and it isn't emitted in stack outputs, so callers must save it out of
band — the launch page copy leads with this.

`WebPassword` is marked `NoEcho` and is not emitted in stack Outputs. It still
exists as plaintext in the substituted EC2 user-data, and the current
implementation interpolates a reversible base64 projection into first-boot
Nix/systemd material, then decodes it in the activation script so Caddy can
derive its Basic Auth hash. Treat principals that can read instance user-data
or the instance's local system configuration as inside the web terminal trust
boundary.

Caddy does not compare the plaintext password at request time. On first boot an
activation script runs `caddy hash-password --algorithm argon2id` and stores
only the Argon2id hash at
`/var/lib/agent-box-web/password-hash` (the file the module's
`users.agent.web.passwordHashFile` points at). On every boot
`agent-web-auth-secrets.service` writes that hash and its detected algorithm
(`WEB_PASSWORD_HASH_AGENT` / `WEB_PASSWORD_ALGORITHM_AGENT`) plus a random
cookie secret (`WEB_COOKIE_SECRET_AGENT`) to
`/run/agent-box-web/env` (`0600`), and Caddy reads that environment file. The
cookie secret is generated on the instance and stored separately at
`/var/lib/agent-box-web/cookie-secret-agent` (`0700` parent directory).

## Design decisions & gotchas

### Why Basic Auth mints a cookie

The obvious "username + password prompt" model (ttyd `-c user:pass`, or
Caddy `basic_auth`) breaks the terminal in every browser: **Chrome,
Firefox, and Safari all refuse to attach cached Basic Auth credentials
to the WebSocket `Upgrade` request**. The HTML loads fine; the WS
handshake gets rejected with 401; the terminal shows "disconnected."
Confirmed via [Bugzilla 1229443](https://bugzilla.mozilla.org/show_bug.cgi?id=1229443),
[Chromium 40193544](https://issues.chromium.org/issues/40193544), and
[ttyd #1437](https://github.com/tsl0922/ttyd/issues/1437).

Fix: Caddy uses Basic Auth only for the initial page load, then sets a
host-scoped `__Host-agent_box_auth_agent` cookie. Later ttyd WebSocket requests
carry
that cookie, not an `Authorization` header, so the terminal works without
putting the secret in browser history, `Referer` headers, or CloudFormation
Outputs. The password is still a shared secret: anyone with enough AWS access to
read EC2 user-data should be treated as inside the deployment's trust boundary.

### Why `sslip.io` and not the EC2 public DNS

Every EC2 instance gets an `ec2-<ip>.compute-1.amazonaws.com` hostname
for free, but **Let's Encrypt hard-refuses to issue certs under
`*.compute.amazonaws.com`** by policy - see [LE community post #12692](https://community.letsencrypt.org/t/policy-forbids-issuing-for-name-on-amazon-ec2-domain/12692).
So we need any other name that resolves to our public IP.

`sslip.io` returns whatever IP is encoded in the label - no DNS setup,
no signup, no dep. Third-party service risk: if they disappear, existing
certs keep serving but new stacks can't ACME. Threat model: they only
provide DNS, so they can't MITM active sessions (TLS cert is ours);
worst case is DoS of new issuance or user redirection to a decoy site
that immediately fails cert validation.

### CloudFormation quick-create requires an S3 template URL

`templateURL` in the `/stacks/quickcreate` URL **must be an S3 URL** -
GitHub Pages or `raw.githubusercontent.com` are rejected with
"TemplateURL must be a supported URL." That's why the publish-template
workflow syncs to S3, and the README's Launch Stack links point at
`https://<bucket>.s3.amazonaws.com/lightsail-template.yaml` (default) and
`https://<bucket>.s3.amazonaws.com/template.yaml` (EC2 alternative).

### Why the template creates its own VPC

Older AWS accounts' default VPCs never had IPv6 CIDR blocks
retroactively added. Making the template self-provisioning (VPC + IPv6
CIDR + IGW + IPv6 subnet) means:
- No "please select a subnet from the dropdown" step at launch (truer
  1-click).
- Works in any account regardless of default-VPC state.
- IPv6 default is reachable outbound to the dual-stack hosts required for
  first boot, without paying for NAT Gateway infrastructure.

All the extra resources are free (VPC, subnet, route table, IGW, EIP-
while-attached historical wisdom no longer applies - see the IPv4 note
below).

### IPv4 by default for reachability, IPv6-only opt-out to save cost

Since **Feb 2024, AWS charges $0.005/hr for every public IPv4 address**
regardless of attach state (EIP or ephemeral, running or stopped
instance) - ~$3.60/mo per address. IPv6 is free. The template still
defaults to `PublicIpv4: true` (an EIP, ~$3.60/mo) so a freshly launched
box is reachable regardless of the client's own connectivity - corporate
nets and coffee-shop WiFi often lack real IPv6. Users who know their
client has IPv6 connectivity can set `PublicIpv4: false` at launch to go
IPv6-only and drop that charge.

### On-demand by default, Spot opt-in to save cost

`UseSpot` defaults to `false` (on-demand) so a freshly launched box never
gets reclaimed mid-session - the same reachability-first reasoning as the
`PublicIpv4` default above. Users who accept the interruption risk for the
lower price set `UseSpot: true`. The spot options can't sit on
`AWS::EC2::Instance` (it has no `InstanceMarketOptions`), so they ride on a
conditional `AWS::EC2::LaunchTemplate` that the instance references only
when `UseSpot=true`. We use a **persistent** request with
`InstanceInterruptionBehavior: stop`: on interruption AWS stops (not
terminates) the instance and restarts the *same* instance in the *same AZ*
when capacity returns, so the root EBS, the ENI's IPv6, and the on-disk TLS
cert all survive. What does not survive is the live tmux session (RAM is
lost on any stop). Risk: if that one AZ+type pool stays capacity-starved,
the box stays stopped until it frees up - pick a deep pool. No `MaxPrice` is
set, so the cap is the on-demand rate. The E2E deploy-test's `ipv4-full` leg
runs on-demand by default too (`UseSpot=false`) so CI doesn't depend on spot
capacity; a `use_spot` dispatch input can opt that leg into Spot instead.

### Race condition: EIP association vs boot

In our custom subnet, `MapPublicIpOnLaunch` is false, so the instance
has **no public IPv4 until `EIPAssoc` completes**. Without that,
amazon-init's `fetchTarball` from github.com (IPv4-only host) fails on
first boot and Caddy never comes up. Fix: `DependsOn: EIPAssoc` on the
Instance. CFN handles this correctly even when the referenced resource
has a Condition that's false (skips the wait); cfn-lint's E3005 is
over-eager here and is suppressed on that resource.

### CFN can't `GetAtt` an Instance's IPv6

Long-standing gap ([issue #916](https://github.com/aws-cloudformation/cloudformation-coverage-roadmap/issues/916)):
`AWS::EC2::Instance` exposes `PublicIp`, `PublicDnsName`, etc. but no
IPv6 attribute. Work-around used here: create an explicit
`AWS::EC2::NetworkInterface` (which does return `PrimaryIpv6Address`)
and attach it to the instance via `NetworkInterfaces:
[NetworkInterfaceId: !Ref NetworkInterface]`.

### cfn-lint gap: SG per-rule descriptions

`SecurityGroupIngress` rule descriptions have a stricter regex than
`GroupDescription` - Unicode punctuation like `-` (U+002D hyphen) is safe, but
Unicode punctuation like an em dash is
rejected by EC2's API but cfn-lint only checks the group description.
The template avoids em-dashes anywhere that becomes a rule description
to sidestep this.

## Lightsail variant (`lightsail-template.yaml`)

A separate template (not a toggle on the EC2 one) that runs agent-box on
**AWS Lightsail** instead of EC2 — the one the README's 1-click Launch buttons
point at. The draw is billing shape: Lightsail is one flat monthly bundle that
folds compute, the SSD, the attached static IPv4, and a multi-TB transfer
allowance into a single price, with **no separate EBS or public-IPv4 line
items**. At the small tier the two come out within a few percent of each
other:

| | EC2 `t4g.small` (this repo's default region set) | Lightsail `small_3_0` |
| --- | --- | --- |
| vCPU / RAM | 2 / 2 GiB | 2 / 2 GiB |
| Disk | 30 GiB gp3 (billed separately) | 60 GiB SSD (in bundle) |
| Public IPv4 | ~$3.60/mo (billed separately) | included |
| Transfer | 100 GB free, then $0.09/GB | multi-TB included |
| Price | ~$13.4/mo on-demand-equivalent (~$12.6 measured on Spot) | **$12.0/mo flat** |

So Lightsail slightly undercuts the EC2 on-demand-equivalent and roughly ties
Spot, while bundling 2x the disk and a large transfer allowance and removing
Spot's interruption risk. EC2 keeps the edge on flexibility (arbitrary instance
types, deep Spot discounts, IaC-native networking).

`small_3_0` is both the default and the **smallest bundle offered**: 2 GiB is
the floor across both templates. The sub-2-GiB bundles do complete a launch on
the swap file the bootstrap creates, but they leave nothing for the work the
box exists to do — an agent CLI plus a language server plus a build, not the
bootstrap.

### How it works (Ubuntu + Nix, no NixOS)

Lightsail has **no NixOS blueprint** and CloudFormation's
`AWS::Lightsail::Instance` cannot boot a custom image — it only takes a public
`BlueprintId`. So the box launches the stock **Ubuntu 24.04** blueprint and
**stays Ubuntu** (issue #154 Phase 4): Nix is installed as a plain package
manager (Determinate installer), the pinned runtime profile
(`nix profile install <ref>#runtime`) brings the payloads, tools and agent
CLIs, and `agentbox apply --first-boot` renders the users, units, sudoers,
tmpfiles, Caddyfile and first-boot secrets. `AgentBoxFlakeRef` is what pins a
box to a revision; `publish-template.yml` injects the commit into the S3 copy
the Launch buttons use.

### Why there is no `nixos-infect` template any more

This filename used to hold a template that converted the instance to NixOS
in place with [`nixos-infect`](https://github.com/elitak/nixos-infect). It is
gone, for two reasons (issue #390):

- **It could not boot.** Lightsail prepends its own `#!/bin/sh` preamble to
  the launch script, so the template's shebang was only a comment and the
  script ran under dash — where its first executable line, `set -euxo
  pipefail`, died 19 s into first boot. Every launch it ever had failed that
  way, and `scripts/check_lightsail_userdata.py`, written to catch exactly
  this, had a hand-maintained template list that omitted it.
- **The native path is better on every axis that mattered:** first boot in
  minutes instead of tens of minutes (no closure build, no relabel, no
  reboot), the Lightsail console's browser SSH keeps working because nothing
  lustrates `/home`, and the base OS keeps getting apt security updates.

Existing infect-based stacks are unaffected — they are NixOS boxes running
`services.agent-box` and self-updating through `nixos-rebuild` — but there is
no template to create another. The EC2 template (`template.yaml`) remains the
NixOS-native deployment.

### Differences from the EC2 template

- **No VPC/subnet/IGW/SG/EIP/IAM** resources — Lightsail manages networking;
  the per-instance firewall is the `Networking.Ports` block (443 always, 22
  when `DebugSsh=true`). No `CAPABILITY_IAM` acknowledgment either.
- **IPv4-native**, so no `PublicIpv4`/`Nat64` parameters and no NAT64 plumbing.
- **No Spot** and no `RootVolumeSize` — the SSD is fixed by the bundle; grow
  by snapshot-and-restore onto a larger bundle.
- **A static IP is always attached** (free on Lightsail while attached, and
  stable across a stop/start), so the `sslip.io` URL keeps working. Because the
  attach happens after instance creation, the launch script can't reference the
  static IP without a dependency cycle, so the box settles its own public IPv4
  at runtime (`agentbox apply --first-boot --settle-delay 60`) and bakes
  `<ip>.sslip.io`. The stack `Outputs` report the same address via
  `GetAtt StaticIp.IpAddress`.
- **Debug access is the Lightsail default key over SSH as `ubuntu`**
  (`DebugSsh=true` opens 22), not SSM — Lightsail has no Session Manager. The
  console's browser SSH works too. Bootstrap output lands in
  `/var/log/agent-box-bootstrap.log`.

### Updating a native box

Issue #358: `sudo -n /usr/bin/systemctl start --no-block
agent-box-update.service` — the full path, because sudo matches the grant on
the exact command line and a bare `systemctl` resolves through PATH (#353).
Same grant the agent holds on the EC2 template, different mechanism behind it,
because there is no closure to rebuild. The unit runs `agentbox update`, which
reads the rev the profile records for itself, asks GitHub for the repo's
current HEAD, refuses anything that is not strictly ahead of what is running
(rewritten history, or a replay of an older rev), and then swaps the profile
with a **remove-then-install**: `nix profile install` over an existing entry
fails on a file conflict, leaves the old profile in place, and the `apply` that
follows honestly reports "0 change(s)" — a box that looks updated and is not.
The new rev is verified out of the profile afterwards for the same reason.

The apply and the restarts are then handed to the **newly installed**
`agentbox` as a fresh process, so the release being installed renders its own
host configuration rather than the outgoing one's renderer doing it. Units
name `/nix/var/nix/profiles/agent-box/bin/...`, a path that does not move when
the profile does, so nothing looks changed to systemd and the update restarts
the services itself; agent sessions go last, since the agent that triggered
the update is sitting in one. A failure at any step rolls the profile back to
the generation it started from and re-applies with that older code.

### Base-OS patching (unattended, and reboot-free)

That update path moves agent-box's own software. The Ubuntu underneath it is
still apt's, and `agentbox apply` still never calls apt — but it does write
down the two answers an unattended patch run needs from a machine nobody is
sitting at:

- **`/etc/apt/apt.conf.d/52-agent-box-unattended`** keeps the periodic jobs
  on (a box whose `20auto-upgrades` never got written patches nothing and
  looks exactly like one that is current), cleans up old kernels and stale
  `.deb`s so patching cannot fill the root disk, and says
  `Unattended-Upgrade::Automatic-Reboot "false"`. It does **not** touch
  `Allowed-Origins`: which pockets are security pockets is Ubuntu's call, and
  an apt list assignment appends rather than replaces, so restating it could
  only duplicate the distro's list or — with a `#clear` — narrow it.
- **`/etc/needrestart/conf.d/50-agent-box.conf`** sets `$nrconf{restart} =
  'a'`, which is the half that makes "no reboot" honest: needrestart's
  default is interactive, and an interactive needrestart run
  non-interactively (exactly how unattended-upgrades runs it) falls back to
  **list-only**, so the patch lands and the vulnerable process keeps running
  until a reboot. It then excludes one unit family from those automatic
  restarts — `agent-box@`, whose `ExecStop` is `tmux -L agent-box
  kill-server`, so restarting it because libc moved is every session on the
  box dying mid-task. The exclusion is *merged* into needrestart's own hash
  (`conf.d` is eval'd after the shipped config), never assigned over it,
  which would drop its exclusions for dbus and the network stack on the way
  past.

A **kernel** patch is the one thing this cannot finish: it installs on disk
and takes effect at a boot. The box says so in `/var/run/reboot-required`, and
the shipped guide tells the agent to report that rather than go looking for a
reboot it has no grant for. Two ways to close that gap when a deployment cares
more about a current kernel than about long-lived sessions:

- `osUpdates: {automaticReboot: "03:00"}` in `/etc/agent-box/config.yaml` —
  renders `Automatic-Reboot "true"` at that hour, with
  `Automatic-Reboot-WithUsers "true"`, because an agent session counts as a
  logged-in user and without it the reboot is skipped forever. A bare `true`
  is rejected: unattended-upgrades reads that as "now", i.e. a reboot the
  moment a kernel patch lands.
- `sudoAllowlist: ["/usr/bin/systemctl reboot"]`, which hands the agent the
  reboot it otherwise has no way to perform, to take when its own work is at
  a safe point.

`osUpdates: {enable: false}` renders neither file (and removes ours if a
previous apply wrote them) — the setting for a host whose package manager is
not apt, which should be patching through `dnf-automatic` instead.

### The launch script must stay bash-guarded

Lightsail prepends its own `#!/bin/sh` preamble to an instance's launch
script, so the template's shebang is only a comment in the middle of the file
cloud-init runs and the payload executes under dash. That is exactly how the
infect template failed, so the script re-execs itself under bash on its first
line and `scripts/check_lightsail_userdata.py` fails CI if that guard goes
missing (or if a bashism appears ahead of it, or if the script sources the Nix
profile before exporting `HOME` — cloud-init provides none, and that aborted a
live launch 40 s in). The check **discovers** its targets with
`aws/lightsail*template.yaml` rather than carrying a list, because a list is
how it came to skip the one template that needed it.

It is also why the payload is a shell script rather than the `#cloud-config`
the Phase 4 design sketched: on Lightsail the launch script is concatenated
*into* a shell script, so a YAML cloud-config document could never be parsed
as one. A native **EC2** template can use `#cloud-config` — nothing is
prepended there.

### Deploying (CLI)

The 1-click Launch buttons use the S3-published copy of this template (pinned
`AgentBoxFlakeRef` injected by `publish-template.yml`, see
["Publishing to S3"](#publishing-to-s3)). To deploy a working-tree copy
directly:

```bash
aws cloudformation deploy \
  --region eu-central-1 \
  --stack-name agentbox-lightsail \
  --template-file aws/lightsail-template.yaml \
  --parameter-overrides \
      WebPassword='<16-64 chars>' \
      AgentBoxFlakeRef="github:defangdevs/agent-box/$(git rev-parse HEAD)"
```

No `--capabilities` needed: the stack creates no IAM. It blocks on the
first-boot `WaitCondition` (timeout 1200 s) and then emits `WebURL`,
`PublicAddress`, `RemoteControlSession`, an `SshCommand` hint, and the
bootstrap log path to read if it times out.

### Validation status

`cfn-lint`, the launch-script dialect check, and `tests/test_agentbox.py`
(which renders `tests/native/config.{json,yaml}` and diffs it against
`tests/native/expected`, then hands the rendered Caddyfile to `caddy
validate`) all run in PR CI.

The end-to-end launch has also been exercised on live hardware: two Lightsail
stacks in a lab account (2026-08-25 and 2026-08-27), the second unattended
from a `master` commit, both reaching a working terminal. Verify a deploy with
`scripts/ws_smoke.py <WebURL> <password>` — it proves a live tmux session
rather than a merely reachable ttyd. Still outstanding: a Lightsail leg in
`deploy-test.yml` (create stack, assert `WebURL` reachable over IPv4, tear
down). GitHub runners are IPv4-only and Lightsail is IPv4-native, so unlike
the EC2 IPv6-only leg this can smoke-test the live URL.

## Refreshing the AMI map

NixOS publishes AMI ids at <https://nixos.github.io/amis/images.json> (no
auth). AMIs are garbage-collected ~90d after publication, so the template's
`Mappings.RegionMap` block needs to be refreshed periodically.

`scripts/refresh_amis.py` regenerates the block between the `BEGIN AMI MAP`
/ `END AMI MAP` markers in `template.yaml`. It targets the NixOS 26.05
channel, `aarch64-linux` (Graviton), and only the 4 regions we support
today (us-east-1, us-west-2, eu-central-1, eu-west-1).

CI runs it weekly via `.github/workflows/refresh-amis.yml` and pushes a
commit if anything changed.

## Publishing to S3

CloudFormation's `templateURL` accepts only S3 URLs, so the templates live
at `s3://defang-agent-box/lightsail-template.yaml` (the default Launch
buttons) and `s3://defang-agent-box/template.yaml` (the EC2 alternative).
`.github/workflows/publish-template.yml` uploads both on every push to
`master` via GitHub OIDC (no static AWS keys).

### Prerequisites (forking this repo)

The workflow is self-bootstrapping - it upserts the bucket, its
public-access configuration, and an `s3:GetObject` policy scoped to the two
template objects (the only objects in the bucket) every run. The module itself
is fetched by the box direct from `raw.githubusercontent.com` at first boot;
that host is dual-stack, so an IPv6-only box needs no NAT64. It reads all
deploy config from **repo-level Actions variables** (Settings > Secrets and
variables > Actions > Variables). None are secrets; they're just fork-specific.

| Variable | Required | Purpose |
| --- | --- | --- |
| `AWS_ROLE_ARN` | yes | IAM role assumed via OIDC. Trust policy must allow the GitHub environment named in `AGENT_BOX_ENVIRONMENT`. |
| `AGENT_BOX_BUCKET` | yes | S3 bucket name to publish the templates into. Global namespace. |
| `AGENT_BOX_ENVIRONMENT` | no | GitHub Actions environment name. Defaults to `defang-agent-box` (must be repo-scoped since env-scoped is a chicken-and-egg). Set to any name your role's trust policy accepts. |
| `AWS_REGION` | no | Region for the bucket + AWS API calls. Defaults to `us-east-1`. |

Role permissions needed: `s3:CreateBucket`, `s3:PutPublicAccessBlock`,
`s3:PutBucketPolicy`, `s3:PutObject`, and `cloudformation:ValidateTemplate`.
The E2E deploy-test workflow uses the same role but doesn't touch S3; it
needs `cloudformation:CreateStack`, `cloudformation:DeleteStack`,
`cloudformation:DescribeStacks`, `ec2:GetConsoleOutput` (to assert amazon-init
provisioned the box - this is how the IPv6-only leg verifies success without
connecting), and the EC2 create/delete permissions used by the template -
including `ec2:CreateLaunchTemplate` / `ec2:DeleteLaunchTemplate` (Spot options)
and `ec2:DescribeSpotInstanceRequests` / `ec2:CancelSpotInstanceRequests` (so
teardown can cancel the persistent Spot request before deleting the stack).

The GitHub environment listed in `AGENT_BOX_ENVIRONMENT` must exist - create it
via `gh api --method PUT repos/<owner>/<repo>/environments/<name>` or the
repo settings UI. No secrets attached; it's just the deployment gate.

Verify with a `workflow_dispatch` run of `Publish CFN template to S3`, then:

```bash
curl -I "https://${AGENT_BOX_BUCKET}.s3.amazonaws.com/lightsail-template.yaml"
curl -I "https://${AGENT_BOX_BUCKET}.s3.amazonaws.com/template.yaml"
```

## Pull request validation

`.github/workflows/aws-ci.yml` runs on pull requests that touch the AWS
templates, launch page, browser-terminal smoke helper, or related workflows. It
does not create AWS resources; it runs
`cfn-lint aws/template.yaml aws/lightsail-template.yaml` and compiles
`scripts/ws_smoke.py` so template/auth-helper changes get fast PR feedback.

## End-to-end deploy test

`.github/workflows/deploy-test.yml` (on push to the template or WebSocket smoke
helper + manual trigger) creates real CloudFormation stacks and deletes them at
the end. It runs two legs in parallel:

- **ipv4-full** - forces `PublicIpv4=true` (+ Spot); GitHub runners are
  IPv4-only, so this is the only leg that can actually reach the box. It runs
  the full connectivity smoke tests (`/agent/` serves ttyd over HTTPS after
  Basic auth, unauthenticated requests get 401, the site root serves the
  session manager behind the same auth, the WebSocket upgrade returns 101
  with the auth cookie).
- **ipv6-outputs** - exercises the IPv6-only path (`PublicIpv4=false`, an
  opt-out from the template's default). The runner can't
  connect over IPv6, so it asserts the stack reaches `CREATE_COMPLETE` and its
  outputs are populated (catches blank `PrimaryIpv6Address` bugs).

Both legs then assert, from the **serial console** (`ec2:GetConsoleOutput`),
that `amazon-init` finished - i.e. the box actually provisioned from user-data.
`CREATE_COMPLETE` fires before amazon-init runs, so this is the only signal
that the IPv6-only module fetch + `nixos-rebuild` succeeded. Toggle
`destroy: false` on the dispatch inputs to keep a stack up for debugging.
