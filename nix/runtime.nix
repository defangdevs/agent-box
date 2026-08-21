# The runtime profile — issue #154 Phase 4.
#
# On NixOS the box gets its software from a system closure. On a stock
# Ubuntu/RHEL host there is no closure and no nixos-rebuild, so the same
# software arrives as a plain Nix profile:
#
#   nix profile install github:defangdevs/agent-box/<rev>#runtime
#
# atomically switchable, `nix profile rollback`-able, and orthogonal to the
# distro's package manager (which stays responsible only for base-OS
# security patching — `agentbox apply` never calls apt or dnf).
#
# Everything here is built from modules/src/*, the SAME assets the NixOS
# module embeds by way of bin/assemble-module.py. That is the whole point of
# Phase 2: the payloads take their configuration from AGENT_BOX_* env and
# resolve binaries from PATH, so neither backend owns them. This file reads
# them with builtins.readFile instead of the @@include assembler, so no
# Nix-string escaping applies and a payload cannot drift between backends by
# way of a quoting difference.
#
# What this profile deliberately does NOT contain:
#   - agent-box-password-<user>: the sudo-crossing password helper keeps its
#     per-user paths compiled in rather than taking them from env (a root
#     write-redirect primitive otherwise). `agentbox apply` generates one per
#     user, the same way the module does.
#   - The AGENT_BOX_* values themselves. Bare payloads go here; the host
#     layer (`agentbox apply`) renders /etc/agent-box/units/<user>.env and
#     the interactive wrappers.
{ pkgs
, lib ? pkgs.lib
  # Which agent CLIs to bundle. Mirrors services.agent-box.installAgents.
, installAgents ? [ "claude" "codex" ]
  # Resolve the fast-moving agent CLIs from a pinned nixos-unstable, exactly
  # as the module's selfUpdate.agentNixpkgs wiring does. Null = host pkgs.
, agentPkgs ? pkgs
  # The fetched local-webhook script (webhook.py). The receiver is a two-line
  # wrapper around it; without a pin there is nothing to wrap, so the webhook
  # payloads are simply left out rather than shipped broken.
, localWebhookScript ? null
}:
let
  src = ../modules/src;
  readSrc = file: builtins.readFile (src + "/${file}");

  # writeShellScriptBin, not writeShellApplication: the module builds these
  # the same way, and writeShellApplication would prepend `set -o errexit`
  # (etc.) and silently change the semantics of every payload.
  payload = name: file: pkgs.writeShellScriptBin name (readSrc file);

  agentPackage = agent:
    if agent == "claude" then agentPkgs.claude-code
    else if agent == "codex" then agentPkgs.codex
    else throw "nix/runtime.nix: unknown agent \"${agent}\" in installAgents";

  webhookEnabled = localWebhookScript != null;

  # Unit-driven payloads: a systemd unit (or ttyd) execs these by bare name,
  # with every value they need supplied by the generated env file.
  unitPayloads = [
    (payload "agent-box-supervisor" "supervisor.sh")
    (payload "agent-box-attach" "attach.sh")
    (payload "agent-box-mark-stopped" "mark-stopped.sh")
    (payload "agent-box-spot-monitor" "spot-monitor.sh")
    (payload "agent-box-update" "update.sh")
    (payload "agent-box-codex-remote-control" "codex-remote-control.sh")
    (payload "agent-box-claude-session-start-hook" "claude-session-start-hook.sh")
    (payload "agent-box-env-exec" "env-exec.sh")
    (pkgs.writers.writePython3Bin "agent-box-settings" {
      # Same gate as the module: compile-check the script, skip the style
      # rules it is deliberately not written to.
      flakeIgnore = [ "E501" "E302" "E305" "W503" "E226" ];
    } (readSrc "settings-daemon.py"))
  ] ++ lib.optionals webhookEnabled [
    (payload "agent-box-webhook-spawn" "webhook-spawn.sh")
    (pkgs.writeShellScriptBin "agent-box-webhook-receiver" ''
      exec ${pkgs.python3}/bin/python3 ${localWebhookScript} "$@"
    '')
  ];

  # Interactive CLIs. These run from every PATH there is — an SSH login
  # shell, `su -c`, an agent tool call, a unit — so they cannot rely on a
  # unit's env file. They ship bare here and `agentbox apply` generates the
  # env-setting wrapper, which is the native counterpart of the module's
  # generated wrapper prelude.
  cliPayloads = [
    (payload "agent-box-session-bare" "session-cli.sh")
  ] ++ lib.optional webhookEnabled (payload "agent-box-webhook-bare" "webhook-cli.sh");

  # Tools agents assume exist, kept in step with the module's
  # agentBaseTools. On a distro host most of these are already present as
  # distro packages; shipping them anyway keeps a session's PATH identical
  # on both backends, which is what the golden/spec convergence in Phase 5
  # will compare.
  baseTools = with pkgs; [
    gawk diffutils gnupatch less file jq ripgrep
    gnutar gzip bzip2 xz zip unzip
    curl wget openssh rsync iputils iproute2 dnsutils netcat-gnu openssl gnupg
    procps psmisc util-linux
    python3 nano
    coreutils findutils gnugrep gnused bash
    git gh
  ];

  # The service side: what the units and the web front door need.
  serviceTools = with pkgs; [ tmux ttyd caddy bubblewrap which fail2ban earlyoom ];

  # Verbatim shared assets. Both backends install these byte-for-byte; the
  # native `agentbox apply` copies them out of the profile.
  assets = pkgs.runCommand "agent-box-assets" { } ''
    install -d $out/share/agent-box/units $out/share/agent-box/caddy \
               $out/share/agent-box/guides $out/share/agent-box/web \
               $out/libexec/agent-box
    for u in ${src}/units/*; do install -m444 "$u" $out/share/agent-box/units/; done
    for c in ${src}/caddyfile-*.caddy; do install -m444 "$c" $out/share/agent-box/caddy/; done
    install -m444 ${src}/default-agents.md ${src}/default-agents-webhook.md \
      $out/share/agent-box/guides/
    install -m444 ${src}/settings.css ${src}/settings.js $out/share/agent-box/web/
    # The password helper is generated per user by `agentbox apply` (it
    # crosses sudo, so its paths must not come from env); the template it
    # renders from lives here.
    install -m444 ${src}/password-helper.py $out/libexec/agent-box/
  '';
in
pkgs.buildEnv {
  name = "agent-box-runtime";
  paths = unitPayloads ++ cliPayloads ++ baseTools ++ serviceTools
          ++ map agentPackage installAgents
          ++ [ assets ];
  # A profile is a user-visible PATH: keep man pages, drop nothing silently.
  extraOutputsToInstall = [ "man" ];
  meta = {
    description = "agent-box runtime software for a non-NixOS host (issue #154 Phase 4)";
    platforms = lib.platforms.linux;
  };
}
