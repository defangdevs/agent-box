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
  # Which agent CLIs to BUNDLE. Mirrors services.agent-box.eagerAgents, and
  # like it, empty by default since issue #416: claude-code and codex were
  # measured at 472 and 565 MiB, they barely overlap, and a session fetches
  # the one it needs into the user's own profile on first use. Which agents
  # a box may RUN is a host-config question (`agents:` in the config the
  # renderer reads), not a profile one — so this list only decides what the
  # profile pays for.
, eagerAgents ? [ ]
  # Resolve the fast-moving agent CLIs from a pinned nixos-unstable, exactly
  # as the module's selfUpdate.agentNixpkgs wiring does. Null = host pkgs.
, agentPkgs ? pkgs
  # The fetched local-webhook script (webhook.py). The receiver is a two-line
  # wrapper around it; without a pin there is nothing to wrap, so the webhook
  # payloads are simply left out rather than shipped broken.
, localWebhookScript ? null
  # Where that script came from, shipped into the profile as data so the native
  # renderer can read the marketplace repo without evaluating Nix. Defaults to
  # the same file the module's option defaults read.
, webhookPin ? import ./webhook-pin.nix
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
      ${resolveTree}
      python3 ${../bin/assemble-module.py} \
        --resolve tree/modules/src/settings-daemon.py > daemon.py
      if grep -q '@@include' daemon.py; then
        echo "an @@include@@ marker survived resolve() — the page would" >&2
        echo "serve the marker text as CSS and as JS" >&2
        exit 1
      fi
      # Like env-exec, the daemon is not self-contained: it calls the env
      # store's load()/keys()/update()/as_dict() and ENV_HEADER, which the
      # module prepends as envStoreLib (issue #212). Without it the flake8
      # gate below reports F821 on every one of them — which is what a
      # native box would have hit at runtime on the settings page's env
      # endpoints, had the profile ever built.
      {
        cat ${src}/lib/envstore.py
        printf '\n\n'
        cat daemon.py
      } > page.py
      # E402/F811 as well, and only here, for the same reason the module
      # gives: the daemon's own imports follow the library's code, and four
      # of them re-bind names the library already imported.
      flake8 --show-source \
        --ignore E501,E302,E305,W503,E226,E402,F811 page.py
      mkdir -p $out/bin
      {
        printf '#!%s\n' ${pkgs.python3}/bin/python3
        cat page.py
      } > $out/bin/agent-box-settings
      chmod +x $out/bin/agent-box-settings
    '';

  # The tree the assembler resolves markers in: the paths in an @@include@@
  # are relative to the including file, and settings-daemon.py's reach out
  # to docs/, so the repo layout has to be rebuilt around modules/src.
  resolveTree = ''
    mkdir -p tree/modules tree/docs
    cp -r ${src} tree/modules/src
    cp ${../docs/potato.svg} tree/docs/potato.svg
  '';

  # Shell payloads are NOT all finished files (issue #374): supervisor.sh,
  # mark-stopped.sh, session-cli.sh and webhook-spawn.sh each carry an
  # @@include:lib/registry.sh@@ marker, the one parser of the session
  # registry's write protocol. `writeShellScriptBin (readFile ...)` shipped
  # that marker to the box verbatim — the settings daemon's bug (below) with
  # a worse ending, since every registry helper a session command calls
  # would simply not exist.
  #
  # So the same treatment: resolve through the assembler's own resolve(),
  # not a second implementation of the marker syntax, and build a derivation
  # rather than `readFile` its output into a writer, which would be
  # import-from-derivation and make `packages.<system>.runtime`
  # un-evaluatable from another architecture.
  #
  # What writeShellScriptBin did is reproduced exactly (runtimeShell shebang,
  # body verbatim, no `set -o errexit` — writeShellApplication would have
  # changed the semantics of every payload).
  payload = name: file: pkgs.runCommand name
    {
      nativeBuildInputs = [ pkgs.python3 pkgs.bash ];
    } ''
      ${resolveTree}
      python3 ${../bin/assemble-module.py} \
        --resolve tree/modules/src/${file} > body
      if grep -q '@@include' body; then
        echo "an @@include@@ marker survived resolve() in ${file}" >&2
        exit 1
      fi
      install -d $out/bin
      { printf '#!%s\n' ${pkgs.runtimeShell}; cat body; } > $out/bin/${name}
      chmod +x $out/bin/${name}
      bash -n $out/bin/${name}
    '';

  # env-exec.py is Python, not shell (issue #212), and it is not
  # self-contained: it calls load_into() and uses os without importing either,
  # relying on src/lib/envstore.py being spliced in above it — exactly as
  # the module's envExecWrapper does. `payload` above would wrap it in a
  # bash shebang and hand bash a python file to parse.
  envStoreLib = readSrc "lib/envstore.py";
  envExecWrapper = pkgs.writers.writePython3Bin "agent-box-env-exec" {
    flakeIgnore = [ "E402" "E501" ];
  } (envStoreLib + "\n\n" + readSrc "env-exec.py");

  # The env store's one WRITER (issue #212). Spliced the same way env-exec
  # is, and for the same reason: envstore-cli.py calls into lib/envstore.py
  # without importing it, exactly as the module's generated wrapper does.
  # Without this the box could READ the store at session spawn and had no
  # way to write it — `agent-box-session env set`, which the shipped guide
  # tells every agent to use for secrets, died on a ${VAR:?} (issue #394).
  envStoreCli = pkgs.writers.writePython3Bin "agent-box-envstore" {
    flakeIgnore = [ "E402" "E501" ];
  } (envStoreLib + "\n\n" + readSrc "envstore-cli.py");

  agentPackage = agent:
    if agent == "claude" then agentPkgs.claude-code
    else if agent == "codex" then agentPkgs.codex
    else throw "nix/runtime.nix: unknown agent \"${agent}\" in eagerAgents";

  webhookEnabled = localWebhookScript != null;

  # The pinned webhook.py, installed into the profile beside the other shared
  # assets (guides, units, caddy snippets). The native renderer points the CLI
  # and the settings panel at THIS path, and the receiver wrapper below execs
  # the same file, so a native box has one answer to "which webhook.py do we
  # run" instead of a store path for the daemon and a hand-declared path for
  # everything else (issue #425).
  webhookScriptAsset = pkgs.runCommand "agent-box-local-webhook" { } ''
    install -Dm444 ${localWebhookScript} \
      $out/share/agent-box/local-webhook/webhook.py
    install -Dm444 ${webhookPinFile} \
      $out/share/agent-box/local-webhook/pin.json
  '';
  # The pin as data, for `agentbox apply`: the supervisor needs the MARKETPLACE
  # repo (AGENT_BOX_WEBHOOK_REPO, which doubles as its enable flag) to pin
  # claude's local-webhook plugin, and the renderer is a python script with no
  # way to evaluate Nix. Shipping the pin beside the script keeps
  # nix/webhook-pin.nix the single home for it (issue #425).
  webhookPinFile = pkgs.writeText "agent-box-webhook-pin.json"
    (builtins.toJSON webhookPin);
  webhookScriptFile =
    "${webhookScriptAsset}/share/agent-box/local-webhook/webhook.py";

  # Native has no webhook.watchPolicy OPTION to render (that is a NixOS-only
  # module setting) — ship an empty policy so agent-box-webhook-policy-apply
  # (below) is a real, runnable binary that is simply always idle, rather
  # than the receiver unit's ExecStartPre pointing at a name the profile
  # never shipped at all (issue #457).
  emptyWebhookWatchPolicyFile =
    pkgs.writeText "agent-box-webhook-watch-policy.json" (builtins.toJSON { });

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
    envExecWrapper
    envStoreCli
    settingsDaemon
  ] ++ lib.optionals webhookEnabled [
    (payload "agent-box-webhook-spawn" "webhook-spawn.sh")
    (pkgs.writeShellScriptBin "agent-box-webhook-receiver" ''
      exec ${pkgs.python3}/bin/python3 ${webhookScriptFile} "$@"
    '')
    # The body (src/webhook-policy-apply.sh) is the SAME file the module
    # includes for this binary — bare jq/mv/rm resolved from the unit's PATH,
    # config taken from AGENT_BOX_WEBHOOK_POLICY_FILE — so this is the one
    # place the two backends could still diverge: which file that variable
    # points at.
    (pkgs.writeShellScriptBin "agent-box-webhook-policy-apply" (''
      export AGENT_BOX_WEBHOOK_POLICY_FILE=${emptyWebhookWatchPolicyFile}
    '' + readSrc "webhook-policy-apply.sh"))
    webhookScriptAsset
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
    # Agent profiles (issue #321): the worker `--profile` selects. Bare, so
    # `agentbox apply` can pin the env store and the harness list into the
    # wrapper — this CLI runs from PATHs that carry almost nothing.
    (payload "agent-box-profile-bare" "profile-cli.sh")
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
  # nftables because the fail2ban jail bans with nftables-multiport:
  # a ban action whose binary comes from the distro is a lock that
  # depends on the image having shipped one.
  serviceTools = with pkgs; [ tmux ttyd caddy bubblewrap which fail2ban
                              nftables earlyoom ];

  # Verbatim shared assets. Both backends install these byte-for-byte; the
  # native `agentbox apply` copies them out of the profile.
  assets = pkgs.runCommand "agent-box-assets" { } ''
    install -d $out/share/agent-box/units $out/share/agent-box/caddy \
               $out/share/agent-box/guides $out/share/agent-box/web \
               $out/share/agent-box/contract $out/libexec/agent-box
    for u in ${src}/units/*; do install -m444 "$u" $out/share/agent-box/units/; done
    for c in ${src}/caddyfile-*.caddy; do install -m444 "$c" $out/share/agent-box/caddy/; done
    # The binding contract (issue #451): the SAME manifest the module
    # embeds via @@include + builtins.fromJSON, installed verbatim so
    # bin/agentbox's Renderer reads the identical bytes at render time.
    for j in ${src}/contract/*.json; do install -m444 "$j" $out/share/agent-box/contract/; done
    install -m444 ${src}/default-agents.md ${src}/default-agents-webhook.md \
      ${src}/default-agents-host-native.md \
      $out/share/agent-box/guides/
    install -m444 ${src}/settings.css ${src}/settings.js $out/share/agent-box/web/
    # The pinned Defang CLI expression (issue #461). Shipped as a FILE, not
    # as the built closure: the card fetches it on demand, so a box that
    # never presses it never pays the 105 MiB. It is the same file the
    # module splices into agent-box-defang-cli-expr.nix, so both backends
    # resolve to one output path and share one binary cache hit.
    install -m444 ${src}/defang-cli.nix $out/share/agent-box/
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
          ++ map agentPackage eagerAgents
          ++ [ assets ];
  # A profile is a user-visible PATH: keep man pages, drop nothing silently.
  extraOutputsToInstall = [ "man" ];
  meta = {
    description = "agent-box runtime software for a non-NixOS host (issue #154 Phase 4)";
    platforms = lib.platforms.linux;
  };
}
