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
  # bin/agentbox lives outside modules/src (it is a tool, not a payload).
  readRepo = file: builtins.readFile (../. + "/${file}");
  # writePython3Bin supplies its own shebang, so the file's own would land on
  # line 2 as a stray block comment (and fail the flake8 gate).
  stripShebang = text:
    let lines = lib.splitString "\n" text;
    in if lib.hasPrefix "#!" (lib.head lines)
       then lib.concatStringsSep "\n" (lib.tail lines)
       else text;

  # settings-daemon.py is the ONE asset in modules/src that is not a finished
  # file: it carries three `@@include:` markers (settings.css, settings.js and
  # docs/potato.svg) that only bin/assemble-module.py expands. readFile'ing it
  # like every other payload shipped those markers to the box verbatim, so the
  # workspace page served `@@include:settings.css@@` inside its <style> and
  # `@@include:settings.js@@` inside its <script> — no stylesheet, and
  # "Uncaught SyntaxError: Invalid or unexpected token" killing every bit of
  # the page's behavior. Seen on a live Lightsail box; the NixOS backend was
  # never affected, because assembling the module IS the expansion.
  #
  # Resolved through the assembler's own resolve(), not a second
  # implementation of the marker syntax: a `.py` host gets the identity
  # escaper, so what comes out is the same plain Python the module embeds.
  # Built as a derivation rather than fed through writers.writePython3Bin,
  # because resolving the markers means RUNNING the assembler: handing
  # `builtins.readFile <derivation>` to a writer would be
  # import-from-derivation, and then `packages.x86_64-linux.runtime` could not
  # even be EVALUATED from an aarch64 box — no more `nix build --dry-run`
  # against the deploy target. The two things writePython3Bin adds are
  # reproduced explicitly: the interpreter shebang and the flake8 gate, with
  # the same ignore list the other python payloads here use.
  settingsDaemon = pkgs.runCommand "agent-box-settings"
    {
      nativeBuildInputs = [ pkgs.python3 pkgs.python3Packages.flake8 ];
    } ''
      # The marker paths are relative to the including file, and one of them
      # reaches out to docs/, so the layout has to be rebuilt around it.
      mkdir -p tree/modules tree/docs
      cp -r ${src} tree/modules/src
      cp ${../docs/potato.svg} tree/docs/potato.svg
      python3 ${../bin/assemble-module.py} \
        --resolve tree/modules/src/settings-daemon.py > daemon.py
      if grep -q '@@include' daemon.py; then
        echo "an @@include@@ marker survived resolve() — the page would" >&2
        echo "serve the marker text as CSS and as JS" >&2
        exit 1
      fi
      flake8 --show-source --ignore E501,E302,E305,W503,E226 daemon.py
      mkdir -p $out/bin
      {
        printf '#!%s\n' ${pkgs.python3}/bin/python3
        cat daemon.py
      } > $out/bin/agent-box-settings
      chmod +x $out/bin/agent-box-settings
    '';

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
    (payload "agent-box-env-exec" "env-exec.py")
    settingsDaemon
  ] ++ lib.optionals webhookEnabled [
    (payload "agent-box-webhook-spawn" "webhook-spawn.sh")
    (pkgs.writeShellScriptBin "agent-box-webhook-receiver" ''
      exec ${pkgs.python3}/bin/python3 ${localWebhookScript} "$@"
    '')
  ];

  # The native configuration layer itself (bin/agentbox). It ships in the
  # profile so `agentbox` can find the profile from its own location, and so
  # an update of the profile updates the renderer with the payloads it
  # renders for.
  agentboxCli = pkgs.writers.writePython3Bin "agentbox" {
    libraries = [ pkgs.python3Packages.pyyaml ];
    # Same gate as the settings daemon: compile-check, skip the style rules
    # the script is deliberately not written to.
    flakeIgnore = [ "E501" "E302" "E305" "W503" "E226" "E741" ];
  } (stripShebang (readRepo "bin/agentbox"));

  # The interpreter the generated per-user password helper runs under. It
  # needs argon2-cffi and bcrypt (verify an old hash, write a new one), which
  # a bare python3 does not have — so the profile names one that does,
  # instead of the helper's shebang hoping for a distro-provided module.
  pythonForHelper = pkgs.runCommand "agent-box-python" { } ''
    install -d $out/bin
    ln -s ${pkgs.python3.withPackages (ps: with ps; [
      argon2-cffi bcrypt pyyaml
    ])}/bin/python3 $out/bin/agent-box-python
  '';

  # Interactive CLIs. These run from every PATH there is — an SSH login
  # shell, `su -c`, an agent tool call, a unit — so they cannot rely on a
  # unit's env file. They ship bare here and `agentbox apply` generates the
  # env-setting wrapper, which is the native counterpart of the module's
  # generated wrapper prelude.
  cliPayloads = [
    (payload "agent-box-session-bare" "session-cli.sh")
    # Needs no env wrapper: it resolves its token from gh at runtime.
    (payload "agent-box-upload" "upload-cli.sh")
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
          ++ [ agentboxCli pythonForHelper ]
          ++ map agentPackage installAgents
          ++ [ assets ];
  # A profile is a user-visible PATH: keep man pages, drop nothing silently.
  extraOutputsToInstall = [ "man" ];
  meta = {
    description = "agent-box runtime software for a non-NixOS host (issue #154 Phase 4)";
    platforms = lib.platforms.linux;
  };
}
