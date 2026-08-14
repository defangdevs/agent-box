# agent-box

Reproducible, multi-user coding-agent sandboxes - one click on AWS, on bare
metal, or as a VM image, from one declarative config. (Built on NixOS.)

Each agent is an **unprivileged user** running a supported agent CLI inside a
persistent `tmux` session. The only elevated power an agent gets is a tight,
explicit passwordless-`sudo` allowlist. Custom tokens (e.g. `GH_TOKEN`) are
injected via drop-in `EnvironmentFile`s that never enter the world-readable Nix
store.

Supported agents:

| Agent | Package | Autonomy flag used by `skipPermissions = true` | Notes |
| --- | --- | --- | --- |
| Claude Code | `pkgs.claude-code` | `--dangerously-skip-permissions` | Supports Claude Remote Control. |
| Codex | `pkgs.codex` | `--dangerously-bypass-approvals-and-sandbox` | Per session, *either* the TUI in the browser terminal *or* Remote Control via the `codex remote-control` daemon (`remoteControl = true`) - not both. |

## 1-click AWS launch

Provisions one AWS Lightsail instance running NixOS with the module + a
browser terminal (Caddy -> ttyd) already wired up, priced as **one flat
monthly bundle** (compute + SSD + static IPv4 + a multi-TB transfer
allowance). Lightsail has no NixOS blueprint, so the box boots the Ubuntu
24.04 blueprint and converts itself to NixOS in-place on first boot with
[nixos-infect](https://github.com/elitak/nixos-infect); expect the first
launch to take ~10-20 minutes while the conversion and the first
`nixos-rebuild switch` run and Caddy issues a Let's Encrypt cert against
`<static-ip>.sslip.io`.

| Region | Launch |
| --- | --- |
| us-east-1 (N. Virginia) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Flightsail-template.yaml) |
| us-west-2 (Oregon) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Flightsail-template.yaml) |
| eu-central-1 (Frankfurt) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=eu-central-1#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Flightsail-template.yaml) |
| eu-west-1 (Ireland) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=eu-west-1#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Flightsail-template.yaml) |

Choose `Agent` (`claude` or `codex`), set a `WebPassword` (any 16&ndash;64
characters, including password-manager symbols), pick a bundle size,
launch. The stack reports
CREATE_COMPLETE only after the box phones home from its first successful
rebuild — a first boot that goes wrong rolls the
stack back visibly instead of leaving a green stack with a dead URL. The agent runs as the
`UserName` linux user (default `agent`). Lightsail manages the networking, so
nothing on the account has to be pre-configured. The
stack Outputs show `https://<ip>.sslip.io/<UserName>/` - open it, sign in
as the `UserName` with your `WebPassword`, complete the selected agent's
one-time sign-in, done. `<UserName>-main@<host>.sslip.io` is used as the Claude
Remote Control session name - the box's public address, so the entry in the
Claude apps doubles as the address you reach it at.

**Cost.** The bundle price is the whole bill — no separate EBS, transfer, or
public-IPv4 line items. The default `small_3_0` (2 vCPU / 2 GiB / 60 GiB SSD)
is **$12/mo flat**; bundles range from `micro_3_0` (1 GiB, $7/mo) to
`xlarge_3_0` (16 GiB, $84/mo). 2 GiB is tight while `nixos-rebuild` evaluates
a self-update — the template adds a 3 GiB swap file to carry it, so updates
are slow rather than fatal; pick `medium_3_0` (4 GiB, $24/mo) for comfortable
rebuild headroom. The attached static IPv4 is included, and the URL survives
a stop/start (a live tmux session doesn't; RAM is lost on any stop). Delete
the stack to stop billing.

Out of disk? Lightsail bundles have a fixed SSD — snapshot the instance and
restore onto a larger bundle to grow. The box also garbage-collects the nix
store automatically.

**Root shell for debugging.** The browser terminal is an unprivileged `agent`
user. For a root path onto the box (e.g. to inspect a failed first-boot
conversion — logs land in `/var/log/agent-box-infect.log`), the template
leaves port 22 open by default (`DebugSsh`) for key-only root SSH with the
Lightsail default key: download it from the Lightsail console (Account ->
SSH keys), then `ssh -i <key> root@<static-ip>`. Password auth stays off;
set `DebugSsh=false` at launch to keep 22 closed.

**Changing the web password.** Open the settings page (the gear icon next to
the terminal), choose **Change password**, and enter the current password plus
the new password twice. The new password follows the launch-time 16&ndash;64
character policy. Saving replaces the root-owned password hash using Caddy's
recommended Argon2id algorithm, reloads Caddy,
and signs out every browser by rotating the authentication-cookie secret.

**Updating the box.** Click "Update box" on the settings page (the gear icon
next to your terminal; the card also shows the running agent-box rev, linked
to its GitHub commit), or ask the agent in its terminal to run
`sudo systemctl start agent-box-update.service` — a root oneshot (alongside
the caddy reload, the only sudo the agent holds) that fast-forwards the box
to this repo's latest master, advances the agent-CLI pin to the newest
nixos-unstable channel release (so `claude` / `codex` stay current even
though the box itself tracks a stable NixOS release), and runs
`nixos-rebuild switch`. Have the agent save its working context first: the
rebuild restarts changed agent services, which kills their running sessions.
Anything that is not a fast-forward of the running revision is refused.
Verifying releases against an offline signing key is tracked in
[issue 46](https://github.com/defangdevs/agent-box/issues/46).

Template source: [`aws/lightsail-template.yaml`](./aws/lightsail-template.yaml).
See [`aws/README.md`](./aws/README.md) for design notes and the S3-hosting
setup.

### Alternative: EC2 template (Spot, IPv6-only)

The original EC2 template is still published, for accounts that want EC2's
flexibility: arbitrary instance types, Spot pricing, IaC-native VPC
networking, root access via SSM Session Manager, and an EBS root volume that
can grow in place.

| Region | Launch |
| --- | --- |
| us-east-1 (N. Virginia) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Ftemplate.yaml) |
| us-west-2 (Oregon) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Ftemplate.yaml) |
| eu-central-1 (Frankfurt) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=eu-central-1#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Ftemplate.yaml) |
| eu-west-1 (Ireland) | [Launch stack →](https://console.aws.amazon.com/cloudformation/home?region=eu-west-1#/stacks/quickcreate?stackName=agent-box&templateURL=https%3A%2F%2Fdefang-agent-box.s3.us-west-2.amazonaws.com%2Ftemplate.yaml) |

It boots a NixOS 26.05 AMI directly (no conversion step; first load ~2-3
minutes) and defaults to a **persistent Spot** `t4g.medium` on an
**IPv6-only** network — ~$16-20/mo all-in, dodging AWS's ~$3.60/mo
public-IPv4 charge. Set `PublicIpv4: true` at launch if your client has no
IPv6 connectivity (corporate/coffee-shop networks often don't; adds the
$3.60/mo EIP), and `UseSpot: false` for on-demand (~$27/mo all-in, no
interruption risk). IPv6-only boxes reach IPv4-only hosts through a free
public DNS64/NAT64 service ([nat64.net](https://nat64.net)); set
`Nat64: false` to opt out. The full cost breakdown, the Spot
stop-not-terminate behavior, SSM root access, and the other design notes
live in [aws/README.md](./aws/README.md); template source:
[`aws/template.yaml`](./aws/template.yaml).

## Why

Turns a hand-tuned, single-user, bare-metal agent setup into something others
can stand up identically - either as per-person accounts on a shared host or as
disposable, snapshot-able KVM guests.

**Why not just a container?** Three properties containers don't give you:

- **Blast radius.** The 1-click path puts each team on a *real VM in their
  own AWS account* — a hypervisor boundary rather than a shared kernel, and
  no code, tokens, or transcripts leave their org. The whole box is
  disposable from the CloudFormation console.
- **Persistence and multi-tenancy.** This is a long-lived box, not an
  ephemeral sandbox: agents keep working in persistent tmux sessions while
  you're away, state survives reconnects and reboots, and several people (or
  several agents per person) share one host under separate unprivileged
  accounts with systemd-hardened services — see the security model below.
- **One config, three targets.** The same declarative NixOS module produces
  the cloud box, the bare-metal multi-user host, and the qcow2 VM image,
  and a deployed box can fast-forward itself to this repo's latest release
  on request — no image rebuild pipeline.

## Quick start (bare metal, multiple users)

Add the flake as an input and import the module:

```nix
# flake.nix (your host)
{
  inputs.agent-box.url = "github:defangdevs/agent-box";
  # ...
}
```

```nix
# configuration.nix
{ pkgs, ... }:
{
  imports = [ inputs.agent-box.nixosModules.agent-box ];

  services.agent-box = {
    enable = true;
    agent = "claude"; # or "codex"
    users = {
      # One account, several agents: sessions seed on FIRST BOOT only —
      # afterwards add/remove them at runtime (see "Sessions" below).
      alice = {
        sessions = {
          main   = { };                    # box default agent
          review = { agent = "codex"; };
        };
      };
      bob   = { remoteControlName = "bob-box"; };
      coder = { agent = "codex"; };
      ci    = { skipPermissions = false; };   # keep approval prompts on
    };
    # The ONLY elevated powers the agents get - keep it tight.
    sudoAllowlist = [ "/run/current-system/sw/bin/systemctl reload caddy.service" ];
    extraPackages = with pkgs; [ git ripgrep jq ];
  };
}
```

Then `sudo nixos-rebuild switch`. Each user gets an `agent-box-<name>.service`.

**First login (per user):** attach to the session and complete the one-time
agent sign-in:

```bash
sudo -u alice env TMUX_TMPDIR=/run/agent-box-alice tmux -L agent-box attach -t main
```

`TMUX_TMPDIR` is required: the agent service runs with `PrivateTmp`, so its
tmux control socket lives under `/run/agent-box-<user>` rather than `/tmp`.

Credentials live in that user's home directory (`~/.claude` for Claude Code,
`~/.codex` for Codex) - per-user runtime state, never baked into the config.

Sign-in is the *only* interactive step: the module pre-accepts Claude Code's
other first-run dialogs (the folder-trust prompt for the agent's working
directory, and the Bypass Permissions warning when `skipPermissions` is on)
by seeding the acceptance flags into `~/.claude.json` and
`~/.claude/settings.json` before each start. Without that, a fresh box parks
the session on a dialog that Remote Control can't answer.

**Claude Code first login in the browser terminal:** Claude emits its OAuth URL
as an OSC 8 hyperlink with the complete URL in a hidden target. The terminal's
`xterm-256color` terminfo cannot advertise OSC 8, so agent-box explicitly
enables tmux's `hyperlinks` client feature. Without it tmux redraws only the
visible text; Safari then detects just the first wrapped row as a plain URL and
opens that truncated fragment. With the native OSC 8 target forwarded, clicking
the first row opens the complete URL even when the visible text wraps.

Claude Code 2.1.126 and later accepts the returned OAuth code in the terminal
when the browser cannot reach a local callback (see the
[Claude Code changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)),
so no callback relay is needed. If an older deployed box still shows the
legacy flow, update it from the settings page or run
`sudo systemctl start agent-box-update.service`, then retry
`claude auth login`.

The auth code input is hidden like a password, so pasting gives no visible
feedback. Paste the code, press Enter, and Claude Code should print
`Login successful.`.

**Codex first login and pairing the Codex apps:** a Codex session with
`remoteControl = true` runs the app-server daemon, *not* a TUI - but its tab
drives the whole onboarding, so there is nothing to run by hand and no second
session to open. On a logged-out box the pane starts the device-code sign-in
itself and prints the URL plus the one-time code (valid 15 min); open the link
on any device, enter the code, and the pane goes straight on to print the
pairing code for the Codex desktop/mobile app. Pairing codes are short-lived -
**press Enter in the pane for a fresh one**. Enter also retries sign-in if it
was abandoned. Those two keys are all the pane's keyboard does; it is not a
codex prompt.

Credentials the ChatGPT backend has invalidated (password change, revoked
session, expired refresh token) need no keys at all: `codex login status` is a
local check and keeps reporting "logged in", so pairing is what discovers the
`401 token_invalidated`, and the pane then drops the dead credentials and re-runs
the device flow by itself - printing the server's own explanation, not the HTTP
transcript. That happens once per cycle; if a *fresh* sign-in is rejected too,
the account is the problem (wrong account, no Codex access) and the pane says so
instead of looping. Typing `login` + Enter forces the same logout-and-sign-in by
hand - still worth having, because signing in as the wrong account produces no
error to detect. Pairing failures that are *not* about auth (an enrollment race
just after boot, no network) are retried and never drop working credentials.

Sign-in has to be device auth: plain `codex login` starts a callback server on
`localhost:1455`, which the browser on your laptop cannot reach. Until sign-in
completes, pairing fails with `remote control pairing is unavailable until
enrollment completes` - that error means "not signed in". If you do sign in
from elsewhere (a shell session, a seeded token), the pane notices within ~5s
and pairs.

`codex login --device-auth` deletes any stored credentials as it starts and
does not restore them if the flow is abandoned, so the pane only ever runs it
when the box is *not* signed in - which is also why you should not run it by
hand on a working box.

The Codex apps label the box with the name the daemon reports, which Codex
takes from `gethostname(2)` with no env var, config key or flag to override it.
agent-box therefore runs the daemon in a private UTS namespace whose hostname
is the box's public address, so it appears as e.g. `1-2-3-4.sslip.io` rather
than an internal cloud hostname. `remoteControlName` remains Claude-only.

## Sessions (any user can run any agent — no rebuild)

A linux user account and an agent CLI are decoupled: each user runs one or
more **sessions**, and each session is one agent (Claude Code or Codex) in
its own tmux session, all supervised by that user's single hardened
`agent-box-<name>.service`. All supported agent CLIs are installed
regardless of what any session runs (`installAgents`). The pseudo-agent
`shell` runs the user's login shell instead — a supervised plain terminal
for manual investigation or clean-up that respawns on exit and gets a
terminal tab in the web workspace like any other session.

Sessions are **runtime data**. The Nix config above only seeds
`~/.config/agent-box/sessions.json` on first boot; after that the file is
authoritative and a rebuild never clobbers runtime changes. Create and
destroy sessions as the user — no sudo, no `nixos-rebuild`:

```bash
agent-box-session ls                        # NAME AGENT STATE
agent-box-session add --agent codex         # auto-named "codex" (or "codex-XXXX")
agent-box-session add review --agent codex  # or name it yourself; starts within ~2s
agent-box-session add scratch --cwd ~/proj -- --model opus
agent-box-session add tidy --agent shell    # plain login shell, no agent
agent-box-session restart review
agent-box-session rm review                 # delist + kill
```

The site root (web setups) is the terminal itself — `https://<domain>/` is
a tabbed workspace, one tab per session, behind the same login as the
terminal (add and close sessions from the tab bar — the tab's `×` arms on
the first click and only closes on the second; restart/delete also on the
settings page) — and agents can spawn sibling sessions themselves (it's just
a file edit on their own account — handy for "have Codex cross-check this").
Open pages follow along live: a session added or removed anywhere — the CLI,
an agent, a second browser tab — appears or disappears in the tab bar (and in
the settings list) within a second, without a reload.

![Tabbed terminal workspace: one tab per session](docs/workspace-tabs.png)

![Closing a session from its tab: the × arms on the first click and only
closes on the second](docs/workspace-tab-close.png)

Attach locally with `tmux -L agent-box attach -t <session>` (see
`TMUX_TMPDIR` note above). In the browser, every tab is also a
deep-linkable standalone terminal at
`https://<domain>/<user>/?arg=<session>`. Killed-on-error sessions keep a
post-mortem shell open instead of being respawned over; delisted sessions
stay gone.

**New user or new session?** A user is the trust boundary; a session is a
unit of work, and sessions of one user are *not* isolated from each other.
The decision rule, the measurements behind it, and what "1 user = 1
project" is worth are in
[Users vs sessions](https://github.com/defangdevs/agent-box/wiki/Users-vs-Sessions)
in the wiki.

## Downloading files the agent produced (web setups)

Files an agent writes live on its own disk, which isn't trivially reachable
from a browser. Each web user gets a file-drop directory, `~/downloads`,
served — behind the **same login as the terminal** — at
`https://<domain>/<user>/downloads/` as a browsable index. The agent moves or
copies a file there and hands you the full URL:

```bash
mv ./report.pdf ~/downloads/    # -> https://<domain>/<user>/downloads/report.pdf
```

Only `~/downloads` is exposed this way — nothing else in the agent's home is
reachable over the web. (`caddy.service` runs with `ProtectHome=true` and
can't read `/home` at all; the directory is backed by a caddy-readable path
under `/var/lib` and symlinked in as `~/downloads`.) The seeded `AGENTS.md`
tells the agent about this route, so "send me that file" just works. For
unauthenticated sharing, an agent can instead run its own web service and
expose it via `~/sites` (see the seeded `AGENTS.md`).

## Webhooks: the agent gets told, instead of polling (web setups)

An agent waiting on CI, a review, or a teammate's push otherwise loops on
`gh pr checks` and `sleep` — slow, and it burns context re-reading state it
already had. Each web user instead gets a webhook endpoint whose deliveries
land **in the session** as messages:

```bash
agent-box-webhook setup                     # mint an HMAC secret, print the URL
# register that URL + secret in the repo (Settings -> Webhooks -> Add webhook)
agent-box-webhook subscribe OWNER/REPO --note "PR 42: waiting on CI"
agent-box-webhook ls                        # what this session listens to
```

On by default (`webhook.enable`), needs `web.enable`. How it fits together:

- A **receiver-only daemon** per web user (`agent-box-webhook-<user>`) owns a
  socket-activated UNIX ingress at `/run/agent-box-webhook/<user>.sock`, bound
  `0660 <user>:caddy` by systemd — the same isolation model as the settings
  socket. It keeps the box's one endpoint up regardless of which sessions exist.
- Caddy reverse-proxies `https://<domain>/<user>/webhook` there **without basic
  auth**: GitHub can't authenticate, so the per-source **HMAC-SHA256** check is
  the trust boundary. Nothing is reachable until a user runs
  `agent-box-webhook setup` — with no source configured every POST is answered
  `404`, and a configured source rejects anything unsigned (`401`).
- The daemon verifies once, then fans each event out over IPC to that user's
  sessions. Subscriptions are **per session** and expire (1h by default), so a
  straggler can't wake a session whose context is long gone. Payload text is
  truncated and marked `⟪UNTRUSTED⟫` — the agent is told to read it as data.
- **Standing watches** (`--deliver-to subagent`): for events no session owns —
  new issues, new PRs, CI on a repo nobody is working on — a shared, pinned
  subscription makes the daemon spawn a **fresh `hook-*` session** primed with
  the event text instead of interrupting whichever session is active. Bursts
  coalesce into one session; concurrent spawns are capped, and the wrapper
  refuses to accumulate more than a handful of live `hook-*` sessions. The
  spawned session's prompt tells it to remove itself when done.
- Claude Code sessions additionally get `webhook_subscribe` /
  `webhook_unsubscribe` / `webhook_subscriptions` as MCP tools, from the
  [local-channels](https://github.com/defangdevs/local-channels) `local-webhook`
  plugin (enabled non-interactively via seeded `~/.claude/settings.json`). The
  CLI and the tools share one subscription list, so Codex and shell sessions are
  not second-class.

Any sender that HMAC-signs its raw body works, not just GitHub —
`agent-box-webhook setup stripe` adds a second source at
`/<user>/webhook/stripe`. Set `services.agent-box.webhook.enable = false` to
omit the daemon, socket, and public path entirely.

## VM image

Build from the same config:

```bash
nix build github:defangdevs/agent-box#vm   # -> qcow2 disk image
# or run a throwaway QEMU VM locally:
nixos-rebuild build-vm --flake github:defangdevs/agent-box#vm && ./result/bin/run-*-vm
```

The bundled `hosts/vm.nix` provisions a single `agent` user with console
autologin (change the initial password on first boot).

## Adding custom tokens (no rebuild)

Tokens live in the user-owned `~/.config/agent-box/env` (0600), managed
through the settings page's Secrets card or from a shell:

```bash
sudo -u alice agent-box-session env set GH_TOKEN ghp_xxx
```

The supervisor's spawn wrapper re-reads the file at every session (re)start,
so a new token applies on the next respawn — no unit restart, no rebuild, and
secrets stay out of the Nix store. For Nix-managed secret paths (agenix,
sops-nix, etc.) use `environmentFiles` instead.

## Options

All under `services.agent-box`:

| Option | Default | Description |
| --- | --- | --- |
| `enable` | `false` | Turn the module on. |
| `agent` | `"claude"` | Default agent CLI: `"claude"` or `"codex"`. |
| `package` | selected agent default | Override package to run for every agent user. |
| `installAgents` | all supported | Agent CLIs installed on the box (independent of what sessions run). |
| `codexFullAccess` | `true` | Run codex with no approval prompts and no sandbox, box-wide, via `/etc/codex/config.toml`. The box is the sandbox. That file is codex's *system* config layer, so a user's own `~/.codex/config.toml` still overrides it — and it is the only path that reaches the app-server daemon behind a remote-controlled codex session. |
| `remoteControlHost` | `fqdnOrHostName` | Host label for the `@<host>` suffix of auto-derived Remote Control names. Empty -> falls back to the public `web.domain`, then the live kernel hostname. The AWS image sets it to the box's public sslip.io host. |
| `users.<name>.sessions.<s>.*` | `{}` | Seed sessions (first boot only): per session `agent`, `skipPermissions`, `remoteControl`, `remoteControlName`, `workingDirectory`, `extraArgs`. Empty = the legacy per-user options below seed a session named `main`. |
| `users.<name>.agent` | `null` | Agent for the default `main` session; null uses `services.agent-box.agent`. |
| `users.<name>.skipPermissions` | `true` | Pass the selected agent's autonomy flag. |
| `users.<name>.remoteControl` | `true` | Make the session drivable from the agent's apps: claude gets `--remote-control`; codex runs the `codex remote-control` daemon instead of its TUI. |
| `users.<name>.remoteControlName` | `<name>-main@<host>` | Claude Remote Control session name (null -> `<user>-<session>@<host>`, where `<host>` is `remoteControlHost`). Ignored for Codex (its daemon names itself from the hostname). |
| `users.<name>.workingDirectory` | `/home/<name>` | Agent startup directory. |
| `users.<name>.extraGroups` | `[]` | Extra groups for the user. |
| `users.<name>.extraArgs` | `[]` | Extra args appended to the selected agent CLI. |
| `users.<name>.environmentFiles` | `[]` | Extra `EnvironmentFile` paths for this agent. |
| `users.<name>.environment` | `{}` | Extra (non-secret) env vars for this agent's service. |
| `sudoAllowlist` | `[]` | Passwordless sudo commands granted to every agent. |
| `extraPackages` | `[]` | Packages placed on each agent's PATH. |
| `environmentFiles` | `[]` | Extra `EnvironmentFile` paths applied to every agent. |
| `webhook.enable` | `true` | Per-user webhook receiver (see above): an unauthenticated `/<user>/webhook` ingress whose HMAC-verified deliveries reach that user's agent sessions, plus the `agent-box-webhook` CLI. No-op without `web.enable`; inert until a user runs `agent-box-webhook setup`. |
| `webhook.repo` / `.rev` / `.sha256` | pinned `defangdevs/local-channels` | Source and pin of the `local-webhook` script the daemon and CLI run. |
| `protectMemory` | `true` | zram swap (zstd, sized to RAM), earlyoom, and `OOMScoreAdjust=500` on agent units, so runaway agent memory gets its process killed (and auto-restarted) instead of livelocking the whole box. All knobs are `mkDefault` - tune or disable pieces from the host config. |

## Security model

The module treats each agent as an untrusted process running inside its own
unprivileged user account, on a machine the operator already treats as a
sandbox host (VM, throwaway EC2 box, etc.). The OS layer is what contains a
compromised agent - the agent CLI's in-tool approval prompts are *deliberately*
off by default (`skipPermissions = true`), so nothing in the agent itself gates
arbitrary command execution as the agent user.

**What the module gives you:**

- **Unprivileged agent user.** Not root. Agent autonomy is intentionally scoped
  to that user.
- **Systemd hardening on every agent service:** `PrivateTmp`,
  `PrivateDevices` (keeps pty, blocks `/dev/mem` and friends),
  `ProtectSystem=strict` (root filesystem read-only, only `/home/<name>`
  writable via `ReadWritePaths`), `ProtectKernelTunables/Modules/`
  `ControlGroups/Clock`, `RestrictSUIDSGID`, `RestrictRealtime`,
  `LockPersonality`. `NoNewPrivileges=true` is applied automatically when
  `sudoAllowlist` is empty; a non-empty allowlist keeps NNP off (sudo is
  setuid and needs the euid transition) - a deliberate trade of a bit of
  containment for scoped elevation.
- **Tight sudo:** whatever's in `sudoAllowlist` is the entire root-capable
  surface. `NOPASSWD` only - no `SETENV`, no blanket sudo, no ALL.
- **Login on everything a human reaches, brute-force damping (web
  deployments):** the terminal workspace, per-session terminals, settings, and
  the `/<user>/downloads/` file drop all sit behind the login (the CI tests
  assert the 401s), new password hashes use Caddy's recommended Argon2id
  algorithm (legacy bcrypt hashes remain accepted until the password changes),
  and a fail2ban jail bans IPs that repeatedly fail it (default on,
  `web.fail2ban`; it only counts requests that actually carried credentials).
  Discovery isn't assumed to be hard — the ACME cert for `<ip>.sslip.io` lands
  in public CT logs minutes after launch — so auth is required, not relied on
  being unguessable.
- **One deliberate exception: `/<user>/webhook`.** Webhook senders can't do
  basic auth, so that path is served without it and a per-source
  **HMAC-SHA256** signature over the raw body is the trust boundary instead
  (see [Webhooks](#webhooks-the-agent-gets-told-instead-of-polling-web-setups)).
  It fails closed in both directions: with no source configured every POST gets
  `404`, and a configured source rejects anything whose signature doesn't
  verify with `401` — so a box where nobody ran `agent-box-webhook setup`
  accepts nothing at all. A bad-signature `401` also can't feed the fail2ban
  jail into banning a real sender: the jail's regex requires an `Authorization`
  header on the request, and webhook senders don't send one. Set
  `webhook.enable = false` to remove the path.
- **User-scoped secrets file:** each user's tokens live in their own
  `~/.config/agent-box/env` (0600, inside the 0700 `~/.config/agent-box`),
  written only by that user's settings daemon / `agent-box-session env` —
  other agent users on the box can't read it, and values are exported into
  sessions at spawn time rather than stored anywhere world-readable.

**Deliberate defaults that stay ON:**

- `skipPermissions = true` - a headless agent runner with per-tool
  approval prompts and no human to answer them is useless. Flip to
  `false` per-user if you actually have a human at the terminal.
- `remoteControl = true` - the "drive it from your phone" feature. For Claude
  Code it adds `--remote-control`; for Codex it starts the local app-server
  daemon, enables Remote Control on it, and lets the relay connect once login
  and network are available instead of requiring either at daemon startup
  (the session pane then signs the box in and prints its pairing code — see
  the Codex first-login section). Flip to `false` per-user if
  you don't want the session reachable from the agent's apps — then Codex runs
  its normal TUI, reachable via the browser terminal.

**Tradeoffs the module can't fully paper over:**

- **Persistent `/home/<name>` across sessions.** SSH keys, git creds,
  dotfiles, session state - anything the agent writes accumulates.
  Treat each agent home as untrusted; back up or wipe with intent.
- **Secrets as env vars.** Anything in `<user>.env` becomes an env
  var in the agent's process and its children. Env vars can leak via
  `/proc/<pid>/environ`, coredumps, or child-process inheritance.
  Systemd's `LoadCredential=` (files under `$CREDENTIALS_DIRECTORY`)
  is a possible future improvement if the tools running under the
  agent actually read from there.
- **A prompt-injected agent can spend whatever you gave it.** Everything
  the agent user can read — env tokens, `~/.ssh`, logged-in CLIs — it can
  also use or exfiltrate if hostile content it processes (a web page, an
  issue, a dependency README) hijacks it. No sandbox changes that; it only
  contains the damage to what those credentials allow. Scope tokens to the
  repos and roles the agent actually needs, and size each key by what it
  could do in an attacker's hands.
- **Agent autonomy flags** grant full autonomy inside the agent CLI. Prefer the
  VM target for anything you'd not lose sleep over an attacker doing as the
  agent user - a KVM guest is a
  much stronger blast-radius boundary than a container.

## Notes

- `claude-code` is unfree; the module allows just the supported agent packages
  (overridable).
- The qcow2 image uses the native nixpkgs image API (`system.build.images`,
  upstreamed in NixOS 25.05) - no extra flake inputs.
- Future work: switching the agent on a running instance should likely be a
  small NixOS config change (`services.agent-box.agent = ...`) plus
  `nixos-rebuild switch` and a service restart, but the UX still needs a tidy
  operator command because credentials and live tmux state are agent-specific.

## Docs

Maintainer and continuity notes live in the
[project wiki](https://github.com/defangdevs/agent-box/wiki).

## License

MIT - see [LICENSE](./LICENSE). Note this license covers the flake/module only;
agent CLIs ship under their own terms.
