{
  description = "agent-box: reproducible multi-user coding agent hosts (bare-metal NixOS + VM images)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, ... }:
    let
      # The module itself is arch-agnostic, and the deployed fleet is aarch64
      # (aws/template.yaml offers Graviton instances only), so the cheap
      # eval-level checks and the assemble app are exposed for both — a
      # maintainer on an ARM box can run them natively.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      eachSystem = nixpkgs.lib.genAttrs systems;

      # The qcow2 image (BIOS/GRUB partitioning) and the interactive
      # runNixOSTest checks stay pinned here: a NixOS test needs a same-arch
      # guest to get KVM, so cross-arch it is unbuildable without emulation.
      # To offer them elsewhere, add the system to `vmSystems`.
      imageSystem = "x86_64-linux";
      vmSystems = [ imageSystem ];

      # Golden behavior snapshot (issue #154, Phase 0): every module-generated
      # systemd unit, published /etc file, tmpfiles rule and script payload
      # from the two golden configurations, with store hashes normalized to a
      # fixed placeholder. The committed copy lives at tests/golden/; the
      # golden-snapshot check below diffs the two, so the portability refactor
      # (phases 1-3) provably keeps the rendered configuration byte-stable —
      # an intentional change is made visible by regenerating the fixture
      # (`nix run .#update-golden`) and reviewing that diff in the PR.
      goldenSnapshotFor = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          lib = nixpkgs.lib;
          # Evaluated for the CHECK's system (not pinned to imageSystem like
          # nixosConfigurations.vm) so the snapshot builds natively on both CI
          # (x86_64) and the deployed aarch64 fleet; hash normalization makes
          # the rendered fixture identical across systems, and on
          # x86_64-linux the vm entry IS nixosConfigurations.vm's eval.
          configs = {
            vm = [ self.nixosModules.agent-box ./hosts/vm.nix ];
            # hosts/vm.nix never enables web/selfUpdate; the overlay pins the
            # whole Caddy/ttyd/settings/webhook/self-update surface too.
            web = [ self.nixosModules.agent-box ./hosts/vm.nix ./tests/golden-web.nix ];
          };
          # multi-user.target/sockets.target (issue #154 Phase 3): the module
          # doesn't own these units, but it drops a `Wants=` override onto
          # each one to enable a per-user %i template instance (see the
          # agent-box@/agent-box-settings@/agent-box-webhook@/
          # agent-web-terminal@ instances below) — a mechanism the earlier
          # per-instance `wantedBy` attempt got wrong in a way no eval-level
          # check caught (only a real VM boot did, unit stayed inactive).
          # Capturing the override text here is what would have caught it.
          unitFilter = n:
            builtins.match
              "(agent-box|agent-web|caddy|fail2ban|earlyoom).*|multi-user\\.target|sockets\\.target"
              n != null;
          # /etc content the module owns or materially shapes. The fail2ban
          # dir entries (filter.d/, action.d/) are upstream package trees and
          # deliberately excluded; the module's own filter and the jail
          # settings land in the files below.
          etcFilter = n:
            builtins.match
              # agent-box/units/*.env (issue #154 Phase 3): the per-user
              # generated env files the "%i" template units' EnvironmentFile=
              # reads — the payload capture below only scans Nix-visible
              # `environment` attrs, so these plain-text files are the review
              # surface for what moved out of that attrset.
              "agent-box-guides/.*|agent-box/units/.*|caddy/caddy_config|codex/config\\.toml|fail2ban/(fail2ban|jail)\\.local|fail2ban/filter\\.d/agent-web-auth\\.conf|sudoers"
              n != null;
          manifestOf = modules:
            let sys = nixpkgs.lib.nixosSystem { inherit system modules; }; in
            {
              # Unit text is an eval-time string; wantedBy/requiredBy are
              # realized as .wants/.requires symlinks and would be invisible
              # in it, so they ride along explicitly.
              units = lib.mapAttrs
                (n: u: {
                  inherit (u) text;
                  wantedBy = lib.naturalSort u.wantedBy;
                  requiredBy = lib.naturalSort u.requiredBy;
                })
                (lib.filterAttrs (n: u: unitFilter n && (u.text or null) != null)
                  sys.config.systemd.units);
              etc = lib.mapAttrs (n: e: "${e.source}")
                (lib.filterAttrs (n: e: etcFilter n) sys.config.environment.etc);
              # The module's OWN rules, stated by the module. This was a
              # filter over the whole system's rules for the substring
              # "agent-box", which is a predicate that can miss: #370's two
              # `d /home/<user>/.config` rules are named after the user, so
              # the lock never captured them and `one-spec-both-backends`
              # kept reporting a native-vs-NixOS divergence that had been
              # fixed a week earlier. An inventory has to come from the thing
              # being inventoried.
              tmpfiles = sys.config.services.agent-box.internal.tmpfilesRules;
            };
          # The manifest string keeps its Nix string context, so building it
          # realizes everything the captured texts reference — which is what
          # lets the builder dereference the generated payload scripts
          # (supervisor, session CLI, attach script, ...) inside the sandbox.
          manifest = pkgs.writeText "agent-box-golden-manifest.json"
            (builtins.toJSON (lib.mapAttrs (_: manifestOf) configs));
        in
        pkgs.runCommand "agent-box-golden-snapshot"
          { nativeBuildInputs = [ pkgs.python3 ]; inherit manifest; } ''
          python3 ${./bin/golden-snapshot.py} "$manifest" "$out"
        '';

      # One spec, two backends (issue #451). The native renderer's config
      # schema was a third hand-mirrored copy of the option tree, and the two
      # test configs that drove the two fixtures had drifted into describing
      # different boxes — so nothing could compare what the backends produced
      # for the SAME box. tests/spec.nix evaluates the golden web
      # configuration's own options into that JSON, `nix run
      # .#update-native-config` commits it, and the one-spec-both-backends
      # check below fails when the committed copy has drifted.
      #
      # Same eval as goldenSnapshotFor's `web` entry, deliberately: the point
      # is that both fixtures come from one configuration.
      nativeConfigFor = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          sys = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [ self.nixosModules.agent-box ./hosts/vm.nix ./tests/golden-web.nix ];
          };
        in
        pkgs.writeText "agent-box-native-config.json"
          (builtins.toJSON
            (import ./tests/spec.nix { lib = nixpkgs.lib; } sys.config));
    in
    {
      # The portable module. Import into any NixOS host:
      #   imports = [ inputs.agent-box.nixosModules.agent-box ];
      nixosModules.agent-box = import ./modules/agent-box.nix;
      nixosModules.default = self.nixosModules.agent-box;

      # Bootable VM config used both by the qcow2 generator (below) and by
      #   nixos-rebuild build-vm --flake .#vm
      # (build-vm injects boot/filesystem, so this stays hardware-free).
      nixosConfigurations.vm = nixpkgs.lib.nixosSystem {
        system = imageSystem;
        modules = [ self.nixosModules.agent-box ./hosts/vm.nix ];
      };

      # Standalone qcow2 image (BIOS boot), built via the image API upstreamed
      # into nixpkgs (NixOS 25.05+): nix build .#vm  ->  result/*.qcow2
      # The `qemu` variant extends the fs/bootloader-free vm config with its own
      # partition table + GRUB, so the base config stays usable for build-vm.
      packages = eachSystem (system:
        {
          # Rendered golden snapshot (issue #154) — input of the
          # golden-snapshot check, materialized into tests/golden by
          # `nix run .#update-golden`.
          golden-snapshot = goldenSnapshotFor system;

          # The native renderer's config, evaluated from the golden web
          # configuration's own options (issue #451) — input of the
          # one-spec-both-backends check, materialized into
          # tests/native/config.json by `nix run .#update-native-config`.
          native-config = nativeConfigFor system;

          # The runtime profile a non-NixOS host installs instead of a system
          # closure (issue #154 Phase 4):
          #   nix profile install github:defangdevs/agent-box/<rev>#runtime
          # Built from modules/src/*, the same payloads the NixOS module
          # embeds — see nix/runtime.nix.
          runtime = import ./nix/runtime.nix {
            pkgs = nixpkgs.legacyPackages.${system};
            # The pinned webhook.py, from the same file the module's
            # services.agent-box.webhook.{repo,rev,sha256} defaults read
            # (nix/webhook-pin.nix). Without it runtime.nix has no pin to
            # wrap, so the profile shipped NONE of the webhook payloads and
            # every native box rendered a receiver unit, a CLI wrapper and a
            # spawn command pointing at binaries that were not there — issue
            # #425. builtins.fetchurl, as the module does it: a hash is given,
            # so it is pure, and no build step stands between the pin and the
            # profile.
            localWebhookScript =
              let pin = import ./nix/webhook-pin.nix; in
              builtins.fetchurl {
                url = "https://raw.githubusercontent.com/${pin.repo}/${pin.rev}"
                      + "/local-webhook/webhook.py";
                sha256 = pin.sha256;
              };
            # The bundled agent CLIs are unfree, and a flake package has no
            # host configuration.nix to carry an allowUnfreePredicate — so
            # allow exactly those two here, the same set (and the same
            # reasoning) as the module's agentPkgs import.
            agentPkgs = import nixpkgs {
              inherit system;
              config.allowUnfreePredicate = pkg:
                builtins.elem (nixpkgs.lib.getName pkg) [ "claude-code" "codex" ];
            };
          };
        }
        // nixpkgs.lib.optionalAttrs (system == imageSystem) (
          let
            image = self.nixosConfigurations.vm.config.system.build.images.qemu;
          in
          {
            vm = image;
            default = image;
          }
        ));

      # `nix run .#assemble` — regenerate the committed modules/agent-box.nix
      # from modules/agent-box.nix.in + modules/src/*. Run from the repo root;
      # edits the working tree in place.
      apps = eachSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          assemble = {
            type = "app";
            program = "${pkgs.writeShellScript "agent-box-assemble" ''
              exec ${pkgs.python3}/bin/python3 "$PWD/bin/assemble-module.py" "$@"
            ''}";
          };

          # `nix run .#update-native-config` — regenerate the committed
          # tests/native/config.json from the golden web configuration's own
          # options (issue #451). Run it after a module option change that
          # the native schema should carry, then regenerate the native
          # fixture with `python3 tests/test_agentbox.py --update`; both
          # diffs belong in the same pull request.
          update-native-config = {
            type = "app";
            program = "${pkgs.writeShellScript "agent-box-update-native-config" ''
              set -euo pipefail
              out=$(nix build --no-link --print-out-paths "$PWD#native-config")
              # BOTH dialects, from the one spec. config.yaml is the shape
              # cloud-init writes and tests/test_agentbox.py renders it
              # alongside the JSON to prove the two describe the same box —
              # so a hand-maintained copy is a third mirror, and it had
              # already drifted (its hostLabel and sudoAllowlist were the old
              # hand-written ones). Generated in canonical block style: what
              # the test needs is that agentbox's YAML path parses to the same
              # box, not that this file uses flow lists.
              ${pkgs.python3.withPackages (ps: [ ps.pyyaml ])}/bin/python3 -c 'import json,sys,yaml
data = json.load(open(sys.argv[1]))
header = "# Generated by `nix run .#update-native-config` from the golden web\n" \
         "# configuration (tests/spec.nix) — do not edit. Same box as\n" \
         "# config.json, spelled in YAML: the shape cloud-init writes.\n"
open(sys.argv[2], "w").write(json.dumps(data, indent=2, sort_keys=True) + "\n")
open(sys.argv[3], "w").write(header + yaml.safe_dump(data, sort_keys=True))' \
                "$out" "$PWD/tests/native/config.json" "$PWD/tests/native/config.yaml"
              echo "wrote tests/native/config.{json,yaml} from $out — review the diff and commit"
            ''}";
          };

          # `nix run .#update-golden` — regenerate the committed golden
          # fixture (tests/golden) after an INTENTIONAL behavior change; the
          # resulting diff is the reviewable statement of what changed. Run
          # from the repo root; edits the working tree in place.
          update-golden = {
            type = "app";
            program = "${pkgs.writeShellScript "agent-box-update-golden" ''
              set -euo pipefail
              out=$(nix build --no-link --print-out-paths "$PWD#golden-snapshot")
              rm -rf "$PWD/tests/golden"
              cp -rT --no-preserve=mode,ownership,timestamps "$out" "$PWD/tests/golden"
              echo "wrote tests/golden from $out — review the diff and commit"
            ''}";
          };

          # `nix run .#compile-azure-bicep` — regenerate the committed
          # azure/agent-box.json from azure/agent-box.bicep. Run from the
          # repo root; edits the working tree in place. Uses nixpkgs' own
          # `bicep` rather than the CI-pinned `az bicep` (azure/.bicep-version):
          # check_azure_template.py strips the version-derived `_generator`
          # block before diffing, so any Bicep CLI produces an equivalent
          # build and nothing outside the flake needs installing.
          compile-azure-bicep = {
            type = "app";
            program = "${pkgs.writeShellScript "agent-box-compile-azure-bicep" ''
              set -euo pipefail
              ${pkgs.bicep}/bin/bicep build "$PWD/azure/agent-box.bicep" \
                --outfile "$PWD/azure/agent-box.json"
              echo "wrote azure/agent-box.json from azure/agent-box.bicep — review the diff and commit"
            ''}";
          };
        });

      # CI validation entrypoints (`nix build .#checks.<system>.<name>`).
      # NOTE: prefer these over `nix flake check` — the VM nixosConfiguration is
      # intentionally bootloader/filesystem-free (the generator supplies them),
      # so its `toplevel` (which flake check builds) does not evaluate.
      checks = eachSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # A full, bootable multi-user system (qemu-vm supplies boot/fs) built
          # from the published bare-metal example — proves the module evaluates
          # and generates a per-user service for every configured agent.
          multiUser = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.agent-box
              ./hosts/bare-metal.nix
              ({ modulesPath, ... }: { imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ]; })
            ];
          };
          services = multiUser.config.systemd.services;
          # issue #154 Phase 3: "agent-box@" is the systemd %i template unit,
          # shipped verbatim via systemd.packages (not a systemd.services
          # Nix declaration — a template-level drop-in there was found to
          # silently never merge into any real instance, so all host-level
          # content moved onto the per-instance declaration below). Each
          # configured user gets its own "agent-box@<user>" drop-in instead
          # of a flat "agent-box-<user>" unit.
          wanted = [ "agent-box@alice" "agent-box@bob" "agent-box@coder" "agent-box@ci" ];
          missing = builtins.filter (n: ! builtins.hasAttr n services) wanted;

          # A fully-featured eval — web on, so every per-user unit the module
          # can render (terminal, settings, webhook) is rendered, and
          # `config.systemd.services` is the ground truth for "what did the
          # module actually define". Shared by the two checks below that both
          # need that ground truth rather than a hand-maintained name list.
          webBaseline = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.agent-box
              ({ modulesPath, ... }: { imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ]; })
              {
                services.agent-box = {
                  enable = true;
                  agent = "claude";
                  users.agent.web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
                  web = { enable = true; domain = "phantom-unit.test"; user = "agent"; };
                };
                system.stateVersion = "25.05";
              }
            ];
          };
        in
        {
          # Eval-level assertion; cheap.
          multi-user = assert missing == [ ];
            pkgs.runCommand "agent-box-multi-user-ok" { } ''
              printf 'generated services: %s\n' ${nixpkgs.lib.escapeShellArg (toString wanted)} > "$out"
            '';

          # Guard (issue #362): a test or host example that overrides a unit
          # by an old/misspelled name — e.g. the flat "agent-box-settings-agent"
          # a rename left behind, instead of the real per-instance
          # "agent-box-settings@agent" — does not fail at eval. NixOS happily
          # defines a brand-new, never-started unit under that name, and the
          # override silently never reaches the real daemon. A VM test then
          # fails three steps removed from the cause (a timeout, not a missing
          # override), and a host example just ships a no-op.
          #
          # `phantomBaseline` below is a fully-featured eval (web on, so the
          # settings/webhook/terminal per-user units all render) — the ground
          # truth for "which units does the module actually define". Scanning
          # a fixed list of names (the grep stopgap from the issue) has the
          # same blind spot as the sweep that missed this in the first place:
          # it can only reject names it already knows about. Eval knows what
          # actually got rendered, so it catches a name nobody anticipated.
          # Guard (this file's `webBaseline`, agent-box#386 + this change): a
          # socket-activated unit MUST set stopIfChanged = false. NixOS's
          # default two-step restart stops such a unit in the OLD
          # configuration, before the new one's daemon-reload, while its
          # .socket keeps listening — so a client (or a webhook delivery)
          # arriving in that window re-activates the daemon from systemd's
          # CACHED old unit definition, and switch-to-configuration's later
          # start step then finds it already running and leaves the stale
          # survivor in place until the next reboot. It cost the settings
          # page a wrong rev (#386) and the webhook receiver a spawn command
          # pointing into a garbage-collected generation.
          #
          # The socket-activation relation is declared in the VERBATIM unit
          # text shipped via systemd.packages, not in the Nix attrs, so it is
          # read from that text — which is also what makes this catch a unit
          # nobody thought to list here. `%i` templates only: every
          # socket-activated unit the module has is per-user.
          socket-activated-restart =
            let
              unitDir = ./modules/src/units;
              templates = builtins.filter (n: nixpkgs.lib.hasSuffix "@.service" n)
                (builtins.attrNames (builtins.readDir unitDir));
              # Either direction counts: Requires= is what makes the socket
              # start the daemon, After= alone still means the socket outlives
              # the service's stop and can re-activate it.
              activated = builtins.filter
                (n: nixpkgs.lib.hasInfix ".socket" (builtins.readFile (unitDir + "/${n}")))
                templates;
              svc = webBaseline.config.systemd.services;
              # "agent-box-settings@.service" -> the baseline's own instance.
              instanceOf = n: (nixpkgs.lib.removeSuffix "@.service" n) + "@agent";
              offenders = builtins.filter
                (n:
                  let i = instanceOf n; in
                  builtins.hasAttr i svc && svc.${i}.stopIfChanged != false)
                activated;
            in
            if offenders != [ ]
            then
              throw (
                "socket-activated unit(s) without stopIfChanged = false — the " +
                "two-step restart lets the .socket re-activate the daemon from " +
                "systemd's cached OLD unit definition, and the stale survivor " +
                "is never replaced (agent-box#386):\n" +
                nixpkgs.lib.concatMapStringsSep "\n"
                  (n: "  ${n} -> systemd.services.\"${instanceOf n}\"")
                  offenders
              )
            else
              pkgs.runCommand "agent-box-socket-activated-restart-ok" { } ''
                printf 'stopIfChanged = false on: %s\n' \
                  ${nixpkgs.lib.escapeShellArg (toString activated)} > "$out"
              '';

          phantom-unit-overrides =
            let
              phantomBaseline = webBaseline;
              hasAt = n: nixpkgs.lib.hasInfix "@" n;
              # "agent-box-settings@agent" -> "agent-box-settings@": the
              # per-instance override target is expected to differ from the
              # baseline's own username, so only the template half is checked.
              templateOf = n: nixpkgs.lib.head (nixpkgs.lib.splitString "@" n) + "@";
              # services and sockets are DIFFERENT unit types — a name that
              # exists only as a socket must still fail a
              # systemd.services.NAME override targeting it (and vice versa),
              # so each class gets its own flat/template sets rather than one
              # merged pool.
              knownByClass = {
                services = builtins.attrNames phantomBaseline.config.systemd.services;
                sockets = builtins.attrNames phantomBaseline.config.systemd.sockets;
              };
              setsFor = class:
                let known = knownByClass.${class}; in
                {
                  flatNames = builtins.filter (n: ! hasAt n) known;
                  templatePrefixes =
                    nixpkgs.lib.unique (map templateOf (builtins.filter hasAt known));
                };
              isKnownUnit = class: name:
                let sets = setsFor class; in
                if hasAt name
                then builtins.elem (templateOf name) sets.templatePrefixes
                else builtins.elem name sets.flatNames;

              # `systemd.services.NAME` / `systemd.sockets.NAME` at attribute-path
              # position, NAME bare ("caddy") or quoted with an "@" instance
              # ("agent-box-settings@agent"). No "." in the class: it must stop
              # before a chained ".environment" / ".serviceConfig" continuation
              # rather than swallowing it into the captured name.
              pattern = ''systemd\.(services|sockets)\."?([A-Za-z0-9_@-]+)"?'';
              namesInFile = file:
                let
                  matches = builtins.filter builtins.isList
                    (builtins.split pattern (builtins.readFile file));
                in
                map (g: { class = builtins.elemAt g 0; name = builtins.elemAt g 1; inherit file; })
                  matches;

              # Every test node config and published host example — the two
              # places an override can name a unit that no longer exists.
              nixFilesIn = dir:
                map (n: dir + "/${n}")
                  (builtins.filter (n: nixpkgs.lib.hasSuffix ".nix" n)
                    (builtins.attrNames (builtins.readDir dir)));
              scanFiles = nixFilesIn ./tests ++ nixFilesIn ./hosts;

              overrides = builtins.concatMap namesInFile scanFiles;
              phantoms = builtins.filter (o: ! isKnownUnit o.class o.name) overrides;
            in
            if phantoms != [ ]
            then
              throw (
                "phantom systemd unit override(s) — name not found among the " +
                "module's own rendered units of that class (renamed unit? " +
                "wrong class? missing \"@\"?):\n" +
                nixpkgs.lib.concatMapStringsSep "\n"
                  (o: "  ${toString o.file}: systemd.${o.class}.${o.name}")
                  phantoms
              )
            else
              pkgs.runCommand "agent-box-phantom-unit-overrides-ok" { } ''
                printf 'no phantom systemd unit overrides in tests/ or hosts/\n' > "$out"
              '';

          # Guard (issue #132): the module's REAL generated Caddyfile (the VM
          # test below swaps in a `tls internal` stand-in) must carry the
          # authenticated downloads route for a web user — the handle, the
          # strip_prefix, and file_server rooted at the caddy-readable backing
          # dir. Cheap: realises only the tiny rendered config + a grep.
          download-route =
            let
              sys = nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  self.nixosModules.agent-box
                  ({ modulesPath, ... }: { imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ]; })
                  {
                    services.agent-box = {
                      enable = true;
                      agent = "claude";
                      users.agent.web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
                      web = {
                        enable = true;
                        domain = "downloads.test";
                        user = "agent";
                      };
                    };
                    system.stateVersion = "25.05";
                  }
                ];
              };
            in
            pkgs.runCommand "agent-box-download-route-ok"
              { caddyfile = sys.config.services.caddy.configFile; } ''
              grep -qF 'handle /agent/downloads/*' "$caddyfile"
              grep -qF 'uri strip_prefix /agent/downloads' "$caddyfile"
              grep -qF 'root * /var/lib/agent-box-downloads/agent' "$caddyfile"
              grep -qF 'file_server browse' "$caddyfile"
              printf 'downloads route present in generated Caddyfile\n' > "$out"
            '';

          # Guard: the module's REAL generated Caddyfile (every VM test swaps
          # in a `tls internal` stand-in) must give each session a path of
          # its own — /<user>/<session>/ rewritten onto ttyd's own path with
          # the session in ?arg= — and must keep the bare /<user>/ landing
          # page pointed at the settings daemon. It also has to PARSE: a
          # typo in a matcher here takes the whole web UI down at once, and
          # nothing else in CI adapts this file. Cheap: realises only the
          # tiny rendered config, then adapts it with stand-in secrets.
          session-route =
            let
              sys = nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  self.nixosModules.agent-box
                  ({ modulesPath, ... }: { imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ]; })
                  {
                    services.agent-box = {
                      enable = true;
                      agent = "claude";
                      users.agent.web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
                      # A SECOND user, because the point of the scheme is
                      # hosting several on one Caddy (issue #221): every user
                      # must get their own landing page and session paths, not
                      # just the one whose daemon serves the vhost root.
                      users.bob.web.passwordHashFile = "/var/lib/agent-box-web/bob-hash";
                      web = {
                        enable = true;
                        domain = "sessions.test";
                        user = "agent";
                      };
                    };
                    system.stateVersion = "25.05";
                  }
                ];
              };
            in
            pkgs.runCommand "agent-box-session-route-ok"
              { caddyfile = sys.config.services.caddy.configFile;
                nativeBuildInputs = [ pkgs.caddy ]; } ''
              # The landing page is the daemon's, and only without an arg=:
              # the session routes rewrite onto this same path WITH one, and
              # ttyd has to keep receiving those.
              grep -qF 'path /agent/' "$caddyfile"
              grep -qF 'not query arg=*' "$caddyfile"
              # One path per session, plus the canonical trailing slash.
              grep -qF 'path_regexp sess_agent ^/agent/([^/]+)/(.*)$' "$caddyfile"
              grep -qF 'rewrite @sess_agent /agent/{re.sess_agent.2}?arg={re.sess_agent.1}&{query}' "$caddyfile"
              grep -qF 'redir @sess_bare_agent /agent/{re.bare_agent.1}/' "$caddyfile"
              # The names a session may not take are excluded from both, so
              # the pages that own them keep answering.
              grep -qF 'not path /agent/settings* /agent/downloads/* /agent/webhook*' "$caddyfile"
              # Ordering: the rewrite must be emitted before the catch-all it
              # feeds, and the landing page before it too.
              rw=$(grep -n 'rewrite @sess_agent' "$caddyfile" | cut -d: -f1)
              home=$(grep -n 'handle @home_agent {' "$caddyfile" | cut -d: -f1)
              catchall=$(grep -n 'handle /agent/\* {' "$caddyfile" | cut -d: -f1)
              [ "$home" -lt "$catchall" ]
              [ "$rw" -lt "$catchall" ]
              # And it all parses. The secrets are Caddy env placeholders,
              # absent in a build sandbox: stand-ins keep basic_auth happy.
              # One set PER USER — an unset algorithm placeholder collapses to
              # nothing and Caddy then reads the login name as the algorithm,
              # which is its own reason to adapt this file in CI.
              WEB_PASSWORD_ALGORITHM_AGENT=bcrypt \
              WEB_PASSWORD_HASH_AGENT='$2a$14$ptCNRCTOMkoUnEXBv0kPWuOJHhYtnpBWQZbLFXW/Ehg5AGKQMoS/W' \
              WEB_COOKIE_SECRET_AGENT=0123456789abcdef \
              WEB_PASSWORD_ALGORITHM_BOB=bcrypt \
              WEB_PASSWORD_HASH_BOB='$2a$14$ptCNRCTOMkoUnEXBv0kPWuOJHhYtnpBWQZbLFXW/Ehg5AGKQMoS/W' \
              WEB_COOKIE_SECRET_BOB=fedcba9876543210 \
                caddy validate --config "$caddyfile" --adapter caddyfile
              # The second user gets the same shape, on their own path.
              grep -qF 'handle @home_bob {' "$caddyfile"
              grep -qF 'path_regexp sess_bob ^/bob/([^/]+)/(.*)$' "$caddyfile"
              grep -qF 'rewrite @sess_bob /bob/{re.sess_bob.2}?arg={re.sess_bob.1}&{query}' "$caddyfile"
              bobrw=$(grep -n 'rewrite @sess_bob' "$caddyfile" | cut -d: -f1)
              bobcatch=$(grep -n 'handle /bob/\* {' "$caddyfile" | cut -d: -f1)
              [ "$bobrw" -lt "$bobcatch" ]
              printf 'per-session routes present for every user, and the Caddyfile adapts\n' > "$out"
            '';

          # Guard (issue #101): the module's REAL generated Caddyfile (the VM
          # test below swaps in a `tls internal` stand-in) must route the
          # webhook path to the user's ingress socket, BEFORE the /<user>/*
          # catch-all and with no basic_auth in that handle — GitHub cannot
          # authenticate, so an auth gate here means silently dropped
          # deliveries. Cheap: realises only the tiny rendered config.
          webhook-route =
            let
              sys = nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  self.nixosModules.agent-box
                  ({ modulesPath, ... }: { imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ]; })
                  {
                    services.agent-box = {
                      enable = true;
                      agent = "claude";
                      users.agent.web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
                      web = {
                        enable = true;
                        domain = "webhook.test";
                        user = "agent";
                      };
                    };
                    system.stateVersion = "25.05";
                  }
                ];
              };
            in
            pkgs.runCommand "agent-box-webhook-route-ok"
              { caddyfile = sys.config.services.caddy.configFile; } ''
              # The handle and its ENTIRE body — three lines, then the closing
              # brace at the handle's own indentation. Asserting the whole body
              # is what makes "no basic_auth in here" meaningful: grepping the
              # file at large would hit the terminal/settings blocks, which are
              # supposed to have one.
              grep -A3 -F 'handle /agent/webhook* {' "$caddyfile" > block
              grep -qF 'uri strip_prefix /agent/webhook' block
              grep -qF 'reverse_proxy unix//run/agent-box-webhook/agent.sock' block
              grep -qxF '  }' block
              ! grep -q basic_auth block
              # Ordering: Caddy prefers the more specific matcher, but keep the
              # emitted order honest too — the webhook handle must come first.
              wh=$(grep -n 'handle /agent/webhook\*' "$caddyfile" | cut -d: -f1)
              catchall=$(grep -n 'handle /agent/\* {' "$caddyfile" | cut -d: -f1)
              [ "$wh" -lt "$catchall" ]
              printf 'unauthenticated webhook route present in generated Caddyfile\n' > "$out"
            '';

          # Regression guard (issue #51): deployed boxes fetch
          # modules/agent-box.nix as a SINGLE file — the CFN user-data and
          # agent-box-update.service both fetchurl just that path — so the
          # module must never reference a ./sibling. builtins.path snapshots
          # the lone file into the store exactly like fetchurl does; forcing
          # the toplevel drvPath then proves a web-enabled system still
          # evaluates from the bare file. Eval-only, nothing is built.
          module-single-file =
            let
              moduleAlone = builtins.path {
                path = ./modules/agent-box.nix;
                name = "agent-box-module-alone.nix";
              };
              sys = nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  moduleAlone
                  ({ modulesPath, ... }: { imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ]; })
                  {
                    services.agent-box = {
                      enable = true;
                      agent = "claude";
                      users.agent.web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
                      web = {
                        enable = true;
                        domain = "single-file.test";
                        user = "agent";
                      };
                    };
                    system.stateVersion = "25.05";
                  }
                ];
              };
            in
            pkgs.runCommand "agent-box-module-single-file-ok" {
              # Forcing drvPath instantiates the full system eval without
              # building it; the context is discarded so this check itself
              # stays a trivial build.
              evaluated = builtins.unsafeDiscardStringContext
                sys.config.system.build.toplevel.drvPath;
            } ''
              printf 'single-file eval OK: %s\n' "$evaluated" > "$out"
            '';

          # modules/agent-box.nix is GENERATED from modules/agent-box.nix.in +
          # modules/src/* by bin/assemble-module.py (issue #140). Re-run the
          # generator in --check mode and fail (printing a diff) if the
          # committed file is stale — this is what lets us split the source for
          # tooling while still shipping the one self-contained fetched file.
          # Behavior lock for the portability refactor (issue #154, Phase 0):
          # the committed tests/golden fixture must byte-match the freshly
          # rendered snapshot of every module-generated unit/etc/tmpfiles/
          # script payload (store hashes normalized). Fails on ANY change to
          # rendered output — including a nixpkgs bump. When the change is
          # intended, regenerate with `nix run .#update-golden` and commit
          # the reviewed diff.
          golden-snapshot =
            pkgs.runCommand "agent-box-golden-snapshot-ok"
              {
                fixture = builtins.path {
                  path = ./tests/golden;
                  name = "agent-box-golden-fixture";
                };
                snapshot = self.packages.${system}.golden-snapshot;
              } ''
              if diff -ru "$fixture" "$snapshot"; then
                printf 'golden snapshot matches tests/golden\n' > "$out"
              else
                echo
                echo "rendered configuration diverged from the committed golden fixture."
                echo "If this change is INTENDED: nix run .#update-golden, review the"
                echo "tests/golden diff, and commit it with the change."
                exit 1
              fi
            '';

          # Issue #154 Phase 4: the runtime profile is how a non-NixOS host
          # gets its software, so the one thing that must never drift is the
          # payload bytes — a native box running a different supervisor than
          # the NixOS golden fixture locks would be a silent fork of the
          # product. Assert every shipped payload is its modules/src file
          # verbatim (the shebang writeShellScriptBin adds, then the body),
          # that the shared assets are byte-identical too, and that each
          # script parses.
          runtime-profile =
            pkgs.runCommand "agent-box-runtime-profile-ok"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.python3 ];
                profile = self.packages.${system}.runtime;
                srcDir = ./modules/src;
              } ''
              fail=0

              # The tree the assembler resolves markers in: an @@include@@
              # path is relative to the including file, and
              # settings-daemon.py's reach out to docs/. Built once, used by
              # every check below.
              mkdir -p tree/modules tree/docs
              cp -r "$srcDir" tree/modules/src
              cp ${./docs/potato.svg} tree/docs/potato.svg

              # bin/<name> must be RESOLVE(src/<file>), modulo the added
              # shebang — not src/<file> raw. supervisor.sh, mark-stopped.sh,
              # session-cli.sh and webhook-spawn.sh each carry an
              # @@include:lib/registry.sh@@ marker, so demanding the raw
              # source here would demand exactly the bug this check exists to
              # catch: the marker text shipped to a box, where it runs as a
              # command that does not exist and every registry helper below
              # it is undefined. resolve() is the identity on a file with no
              # markers, so one rule covers both kinds.
              check_payload() {
                bin="$profile/bin/$1"
                if [ ! -x "$bin" ]; then
                  echo "MISSING: bin/$1"; fail=1; return
                fi
                python3 ${./bin/assemble-module.py} \
                  --resolve "tree/modules/src/$2" > want
                # The shebang is line 1; the body after it ends with the
                # source's own trailing newline and must match byte for byte.
                if ! diff -u want <(tail -n +2 "$bin") >/dev/null; then
                  echo "DRIFT: bin/$1 is not resolve(modules/src/$2)"
                  diff -u want <(tail -n +2 "$bin") | head -20 || true
                  fail=1
                  return
                fi
                bash -n "$bin" || { echo "SYNTAX: bin/$1"; fail=1; }
                if grep -q '@@include' "$bin"; then
                  echo "MARKER: bin/$1 still has an @@include@@ marker"; fail=1
                fi
                echo "ok: bin/$1 == resolve(src/$2)"
              }
              check_payload agent-box-supervisor supervisor.sh
              check_payload agent-box-attach attach.sh
              check_payload agent-box-mark-stopped mark-stopped.sh
              check_payload agent-box-spot-monitor spot-monitor.sh
              check_payload agent-box-update update.sh
              check_payload agent-box-source source-tree.sh
              check_payload agent-box-codex-remote-control codex-remote-control.sh
              check_payload agent-box-claude-session-start-hook claude-session-start-hook.sh
              check_payload agent-box-session-bare session-cli.sh
              check_payload agent-box-upload upload-cli.sh
              # Built only when the webhook script is pinned, which the
              # default runtime is; both are marker payloads.
              if [ -x "$profile/bin/agent-box-webhook-spawn" ]; then
                check_payload agent-box-webhook-spawn webhook-spawn.sh
                check_payload agent-box-webhook-bare webhook-cli.sh
              fi

              # agent-box-env-exec is Python, not shell (issue #212), and it
              # is not src/env-exec.py verbatim: src/lib/envstore.py is
              # spliced in above it, exactly as the module's envExecWrapper
              # does, so what it must equal is that concatenation — checked
              # with py_compile rather than bash -n.
              cat "$srcDir/lib/envstore.py" > want_env_exec.py
              printf '\n\n' >> want_env_exec.py
              cat "$srcDir/env-exec.py" >> want_env_exec.py
              if ! diff -u want_env_exec.py \
                   <(tail -n +2 "$profile/bin/agent-box-env-exec") >/dev/null; then
                echo "DRIFT: bin/agent-box-env-exec is not src/lib/envstore.py + src/env-exec.py"
                diff -u want_env_exec.py \
                  <(tail -n +2 "$profile/bin/agent-box-env-exec") | head -20 || true
                fail=1
              else
                echo "ok: bin/agent-box-env-exec == src/lib/envstore.py + src/env-exec.py"
              fi
              # ast.parse, not `python3 -m py_compile`: py_compile writes a
              # __pycache__ next to its input, and the input here lives in
              # the store. It failed with "[Errno 13] Permission denied:
              # .../bin/__pycache__" and reported it as a SYNTAX error in a
              # file that is perfectly good Python.
              python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' \
                "$profile/bin/agent-box-env-exec" \
                || { echo "SYNTAX: bin/agent-box-env-exec"; fail=1; }
              if grep -q '@@include' "$profile/bin/agent-box-env-exec"; then
                echo "MARKER: bin/agent-box-env-exec still has an @@include@@ marker"
                fail=1
              fi

              # The settings daemon is the one asset in modules/src that is
              # NOT a finished file: it carries @@include@@ markers for
              # settings.css, settings.js and docs/potato.svg. Shipping it
              # verbatim put those markers in the served page — no stylesheet
              # and "Uncaught SyntaxError: Invalid or unexpected token" — so
              # what it must equal is the ASSEMBLER's output, not the source.
              python3 ${./bin/assemble-module.py} \
                --resolve tree/modules/src/settings-daemon.py > want.py
              if grep -q '@@include' want.py; then
                echo "MARKER: resolve() left a marker in settings-daemon.py"
                fail=1
              fi
              # ...prepended by the env-store library, exactly as
              # agent-box-env-exec above and the module's own settingsDaemon
              # are (issue #212): the page calls load()/keys()/update()/
              # as_dict() and ENV_HEADER, none of which it defines.
              {
                cat "$srcDir/lib/envstore.py"
                printf '\n\n'
                cat want.py
              } > want-settings.py
              if ! diff -u want-settings.py <(tail -n +2 "$profile/bin/agent-box-settings") \
                   >/dev/null; then
                echo "DRIFT: bin/agent-box-settings is not resolve(src/settings-daemon.py)"
                diff -u want-settings.py \
                  <(tail -n +2 "$profile/bin/agent-box-settings") | head -20 || true
                fail=1
              else
                echo "ok: bin/agent-box-settings == src/lib/envstore.py + resolve(src/settings-daemon.py)"
              fi

              # Shared assets: the units both backends install verbatim, the
              # Caddyfile fragments and guides the native renderer binds, the
              # settings page assets, and the password-helper template.
              for u in "$srcDir"/units/*; do
                got="$profile/share/agent-box/units/$(basename "$u")"
                if ! diff -u "$u" "$got" >/dev/null 2>&1; then
                  echo "DRIFT: share/agent-box/units/$(basename "$u")"; fail=1
                else
                  echo "ok: units/$(basename "$u")"
                fi
              done
              for c in "$srcDir"/caddyfile-*.caddy; do
                diff -u "$c" "$profile/share/agent-box/caddy/$(basename "$c")" >/dev/null \
                  || { echo "DRIFT: caddy/$(basename "$c")"; fail=1; }
              done
              for a in default-agents.md default-agents-webhook.md \
                       default-agents-host-native.md; do
                diff -u "$srcDir/$a" "$profile/share/agent-box/guides/$a" >/dev/null \
                  || { echo "DRIFT: guides/$a"; fail=1; }
              done
              for a in settings.css settings.js; do
                diff -u "$srcDir/$a" "$profile/share/agent-box/web/$a" >/dev/null \
                  || { echo "DRIFT: web/$a"; fail=1; }
              done
              diff -u "$srcDir/password-helper.py" \
                "$profile/libexec/agent-box/password-helper.py" >/dev/null \
                || { echo "DRIFT: libexec/agent-box/password-helper.py"; fail=1; }

              # The tools a session's PATH is expected to have, and the
              # services the units drive. A missing one is a native box that
              # boots and then cannot serve, so name them explicitly.
              #
              # The agent harnesses are NOT in this list (issue #416): the
              # profile ships none by default, and a session fetches the one
              # it names into the user's own profile on first use. Their
              # absence is the feature — 1.4 GiB of profile closure down to
              # 800 MiB, measured on aarch64 — so assert it, rather than
              # leaving a check that would pass again the day somebody put
              # them back by accident.
              for b in agent-box-settings tmux ttyd caddy jq gh git python3 \
                       bwrap flock; do
                [ -x "$profile/bin/$b" ] || { echo "MISSING: bin/$b"; fail=1; }
              done
              for b in claude codex; do
                if [ -e "$profile/bin/$b" ]; then
                  echo "EAGER: bin/$b"; fail=1
                fi
              done

              [ "$fail" -eq 0 ] || exit 1
              printf 'runtime profile payloads match modules/src\n' > "$out"
            '';

          # Issue #154 Phase 4: the native backend's render is reviewable the
          # same way the NixOS one is. tests/test_agentbox.py renders
          # tests/native/config.{json,yaml} into a tree and diffs it against
          # the committed tests/native/expected fixture, so a change to what
          # a native box gets shows up in the pull request. The render is
          # then handed to `caddy validate`, which is the check the golden
          # fixture cannot do: a Caddyfile that adapts and provisions, not
          # just one whose bytes are expected.
          agentbox-render =
            pkgs.runCommand "agent-box-agentbox-render-ok"
              {
                nativeBuildInputs = [
                  (pkgs.python3.withPackages (ps: [ ps.pyyaml ]))
                  pkgs.caddy
                  # visudo, for the sudoers half of what `agentbox apply`
                  # now validates before it commits a render (issue #526).
                  # Without it here the test that puts the rendered
                  # drop-in through visudo skips, and a sudoers file that
                  # can lock an administrator out of sudo reaches a box
                  # unchecked.
                  pkgs.sudo
                  # The two config formats this render does NOT own: apt
                  # reads back the unattended-upgrade policy (dpkg only so
                  # apt agrees it has a packaging system at all), perl
                  # parses the needrestart snippet.
                  pkgs.apt
                  pkgs.dpkg
                  pkgs.perl
                ];
                repo = builtins.path {
                  path = ./.;
                  name = "agent-box-src";
                  filter = path: type:
                    let base = builtins.baseNameOf path; in
                    base != "result" && base != ".git";
                };
              } ''
              cp -rT --no-preserve=mode,ownership "$repo" repo
              cd repo
              # To a file, not a pipe: `... | tail -20` hides everything the
              # last 20 lines are not, and a failing assertion is usually
              # further up than the progress output that follows it. It also
              # stops depending on the builder's shell having pipefail for
              # the exit status to survive at all.
              if ! python3 tests/test_agentbox.py -v > render.log 2>&1; then
                tail -80 render.log
                exit 1
              fi
              tail -5 render.log

              # The rendered Caddyfile must be a config caddy accepts. Env
              # placeholders are resolved from a throwaway env file with a
              # real argon2id hash — an invalid hash fails provisioning, so
              # this also proves the auth block is wired the way caddy wants.
              hash=$(printf 'test-password-1234\n' \
                | caddy hash-password --algorithm argon2id)
              for u in AGENT ROBOT; do
                printf 'WEB_PASSWORD_HASH_%s=%s\n' "$u" "$hash"
                printf 'WEB_PASSWORD_ALGORITHM_%s=argon2id\n' "$u"
                printf 'WEB_COOKIE_SECRET_%s=%s\n' "$u" deadbeef
              done > caddy.env
              caddy validate --envfile caddy.env --adapter caddyfile \
                --config tests/native/expected/etc/agent-box/Caddyfile

              # Same idea for the base-OS patch policy, and here the stakes
              # are higher than a service that fails to start: a syntax
              # error in ANY file under /etc/apt/apt.conf.d breaks every
              # apt invocation on the box, so the render would take the
              # distro's own security patching down with it. apt itself is
              # the only honest judge of that, and it also proves the keys
              # land where they are meant to (`Dir::Bin::dpkg`, because a
              # Nix builder has no /usr/bin/dpkg and apt refuses to run
              # without one).
              apt=tests/native/expected/etc/apt/apt.conf.d/52-agent-box-unattended
              apt-config -c "$apt" -o "Dir::Bin::dpkg=$(command -v dpkg)" \
                dump > apt.dump
              for key in \
                'APT::Periodic::Unattended-Upgrade "1"' \
                'Unattended-Upgrade::Automatic-Reboot "false"' ; do
                grep -F "$key;" apt.dump > /dev/null \
                  || { echo "apt did not read back: $key"; cat apt.dump; \
                       exit 1; }
              done
              # Negative control: apt-config exits 0 on a file it could not
              # parse when the parse error is only a warning, so prove the
              # check above can actually fail.
              sed 's/"1";/"1"/' "$apt" > broken.conf
              if apt-config -c broken.conf -o \
                  "Dir::Bin::dpkg=$(command -v dpkg)" dump > /dev/null 2>&1
              then
                echo "FAIL: apt-config accepted a config with a syntax error"
                exit 1
              fi

              # needrestart's snippets are Perl, eval'd into the daemon's
              # own namespace: a snippet that does not parse is a `die` in
              # needrestart, which is a patch run that restarts nothing.
              perl -c tests/native/expected/etc/needrestart/conf.d/50-agent-box.conf
              printf 'agentbox render matches tests/native/expected\n' > "$out"
            '';

          # Issue #451: the parity check below guards the CONFIGURATION each
          # backend hands the shared payloads, keyed on AGENT_BOX_* names. It
          # is blind to everything with no such name in it — tmpfiles rules,
          # sudoers grants, the seed JSON two hand-written producers emit,
          # which are #356's actual bugs. Nothing could compare those,
          # because the two fixtures were rendered from two hand-mirrored
          # configurations that had drifted into describing different boxes.
          # So: tests/native/config.json is now GENERATED from the golden web
          # configuration's own options (tests/spec.nix), this check fails if
          # the committed copy has drifted from it, and only then compares
          # what the two backends made of that one box.
          one-spec-both-backends =
            pkgs.runCommand "agent-box-one-spec-both-backends"
              {
                # pyyaml: config.yaml is the second dialect of the one spec,
                # and without it that half of the check silently skips.
                nativeBuildInputs = [
                  (pkgs.python3.withPackages (ps: [ ps.pyyaml ]))
                ];
                spec = nativeConfigFor system;
                repo = builtins.path {
                  path = ./.;
                  name = "agent-box-src";
                  filter = path: type:
                    let base = builtins.baseNameOf path; in
                    base != "result" && base != ".git";
                };
              } ''
              cp -rT --no-preserve=mode,ownership "$repo" repo
              cd repo
              # To a file, then copied — NOT `| tee "$out"`, which returns
              # tee's status: the builder's shell has no pipefail, so a
              # detected divergence would have written $out and exited 0, and
              # this check would have gone green on exactly the failure it
              # exists to report (CodeRabbit, PR #455). Same shape as
              # agentbox-render above, for the same reason.
              if ! python3 scripts/check_one_spec.py --spec "$spec" > report; then
                cat report
                exit 1
              fi
              cat report
              cp report "$out"
            '';

          # Issue #394: the two renderers configure the SAME payloads, and
          # nothing made them agree about how. #392 is what that cost — the
          # settings daemon's whole Connections section, missing from every
          # native box because only the module set AGENT_BOX_CONNECT_BINS,
          # with the payload byte-identical on both. This reads the contract
          # out of the committed fixtures (no VM, no build) and fails on any
          # divergence not declared with a reason in the script's tables.
          backend-parity =
            pkgs.runCommand "agent-box-backend-parity-ok"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                repo = builtins.path {
                  path = ./.;
                  name = "agent-box-src";
                  filter = path: type:
                    let base = builtins.baseNameOf path; in
                    base != "result" && base != ".git";
                };
              } ''
              cp -rT --no-preserve=mode,ownership "$repo" repo
              cd repo
              # To a file for the same reason agentbox-render does it: the
              # declared-divergence table prints above the failure, and a
              # pipe would cost the exit status.
              if ! python3 scripts/check_backend_parity.py > parity.log 2>&1
              then
                cat parity.log
                exit 1
              fi
              cat parity.log

              # Negative control, and not a formality: #426 is a variable
              # BOTH backends supply, in two different units, which the
              # box-wide comparison above cannot see by construction. So
              # put that exact bug back — drop AGENT_BOX_WEBHOOK_SCRIPT
              # from the native settings unit, leaving the copy the
              # agent-box-webhook CLI wrapper exports — and require a
              # failure. A check that passes on a tree with #426 in it is
              # the check we already had.
              units=tests/native/expected/etc/systemd/system
              # Both instances: the comparison folds agent-box-settings@agent
              # and @robot into one family, so dropping the variable from one
              # user's drop-in leaves the other user still supplying it.
              sed -i '/AGENT_BOX_WEBHOOK_SCRIPT/d' \
                "$units"/agent-box-settings@*.service.d/10-host.conf
              if python3 scripts/check_backend_parity.py > reintroduced.log 2>&1
              then
                echo "FAIL: the check passed with issue #426 put back."
                cat reintroduced.log
                exit 1
              fi
              grep -q "agent-box-settings@.service AGENT_BOX_WEBHOOK_SCRIPT" \
                reintroduced.log
              echo "negative control ok: #426 reintroduced, check failed."
              cp parity.log "$out"
            '';

          # Issue #394: the browser terminal is one password on the open
          # internet, and the jail in front of it is only as good as a
          # config nobody has ever run. `fail2ban-server -t` alone is not
          # that proof — it accepts a jail with no filter and no action,
          # warns, and exits 0 — so this asserts the warnings are absent
          # AND runs the shipped failregex against a real Caddy 401 line.
          # A jail that parses but matches nothing is the failure worth
          # testing for: nobody notices a lock that never clicks.
          fail2ban-jail =
            pkgs.runCommand "agent-box-fail2ban-jail-ok"
              {
                nativeBuildInputs = [ pkgs.fail2ban ];
                conf = ./tests/native/expected/etc/agent-box/fail2ban;
              } ''
              cp -rT --no-preserve=mode,ownership "$conf" jail
              # The rendered tree symlinks action.d into the runtime
              # profile; here the same directory comes from the package the
              # profile installs.
              rm -f jail/action.d
              ln -s ${pkgs.fail2ban}/etc/fail2ban/action.d jail/action.d

              fail2ban-server -t -c jail > test.log 2>&1 || {
                cat test.log; exit 1; }
              cat test.log
              if grep -E "No filter set|No actions were defined" test.log \
                   > /dev/null; then
                echo "the jail parsed but is inert — see the warnings above" >&2
                exit 1
              fi

              # A Caddy JSON access-log line for a request that carried an
              # Authorization header and was refused: what a brute-force
              # attempt against /agent/ actually leaves in the journal.
              cat > sample.log <<'LINE'
              {"level":"error","ts":1756315000.1,"logger":"http.log.access","msg":"handled request","request":{"remote_ip":"203.0.113.77","client_ip":"203.0.113.77","proto":"HTTP/2.0","method":"GET","host":"box.example.com","uri":"/agent/","headers":{"Authorization":["REDACTED"],"User-Agent":["curl/8.5.0"]}},"status":401,"size":0}
              LINE
              sed -i 's/^ *//' sample.log
              fail2ban-regex sample.log jail/filter.d/agent-web-auth.conf \
                > regex.log 2>&1 || { cat regex.log; exit 1; }
              cat regex.log
              grep -E "1 matched" regex.log > /dev/null || {
                echo "the shipped failregex does not match a real Caddy 401 line" >&2
                exit 1
              }
              printf 'fail2ban jail parses, is armed, and matches\n' > "$out"
            '';

          module-generated-up-to-date =
            pkgs.runCommand "agent-box-module-generated-up-to-date"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                assembler = ./bin/assemble-module.py;
                template = ./modules/agent-box.nix.in;
                committed = ./modules/agent-box.nix;
                srcDir = ./modules/src;
                # The mascot mark (issue #185) lives with the website it also
                # serves as a favicon, and the settings daemon embeds those
                # same bytes — so this one asset outside modules/src is part
                # of the module's source set too.
                mark = ./docs/potato.svg;
                # Same reasoning as the mark: the webhook pin is one file
                # outside modules/src that the template splices, because both
                # backends have to read the pin from ONE place (issue #425).
                pin = ./nix/webhook-pin.nix;
              } ''
              install -d repo/bin repo/modules repo/docs repo/nix
              cp "$assembler" repo/bin/assemble-module.py
              cp "$template" repo/modules/agent-box.nix.in
              cp "$committed" repo/modules/agent-box.nix
              cp -r "$srcDir" repo/modules/src
              cp "$mark" repo/docs/potato.svg
              cp "$pin" repo/nix/webhook-pin.nix
              python3 repo/bin/assemble-module.py --check --repo repo
              touch "$out"
            '';

          # Unit test for the assembler's Nix escaping (issue #244).
          # module-generated-up-to-date cannot catch an escaping bug — it
          # regenerates the file with the same assembler, so the check and the
          # bug agree on the wrong bytes. This one decodes the escaped text the
          # way Nix's lexer does and round-trips a corpus through it.
          # Issue #51 makes the repo the distribution channel for the one
          # third-party asset the settings page loads: a deployed box fetches
          # modules/agent-box.nix as a single file and the pages pull nothing
          # from a CDN, so a vendored copy is the only shape available. This
          # verifies each copy still hashes to the pin that records where it
          # came from — an edited, truncated or swapped file fails here rather
          # than shipping to every box — and that no vendored file is
          # unpinned, unused, or undeclared.
          #
          # The upstream half (`--upstream`, "is there a newer release?") is
          # deliberately NOT here: it needs network, which a Nix build does not
          # get. .github/workflows/vendor-updates.yml runs it weekly instead.
          vendor-integrity =
            pkgs.runCommand "agent-box-vendor-integrity"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                checker = ./scripts/check_vendor.py;
                vendor = ./modules/src/vendor;
                daemon = ./modules/src/settings-daemon.py;
              } ''
              install -d repo/scripts repo/modules/src
              cp "$checker" repo/scripts/check_vendor.py
              cp -r "$vendor" repo/modules/src/vendor
              cp "$daemon" repo/modules/src/settings-daemon.py
              cd repo
              python3 scripts/check_vendor.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          assemble-module-escaping =
            pkgs.runCommand "agent-box-assemble-module-escaping"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                assembler = ./bin/assemble-module.py;
                tests = ./tests/test-assemble-module.py;
                # The tests round-trip the REAL vendored assets through the
                # Python-host dialect, not just a synthetic corpus: a bump
                # replaces one of those files wholesale, so the new bytes have
                # to survive the escaper before they can land.
                vendor = ./modules/src/vendor;
              } ''
              install -d repo/bin repo/tests repo/modules/src
              cp "$assembler" repo/bin/assemble-module.py
              cp "$tests" repo/tests/test-assemble-module.py
              cp -r "$vendor" repo/modules/src/vendor
              # Not piped into tee: the log has to reach the build output
              # whether the tests pass or fail, and the exit status has to be
              # python's own.
              python3 repo/tests/test-assemble-module.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Unit test for agent-box-upload (issue #368). The three things it
          # gets right were shipped WRONG first, as a curl recipe in the guide
          # (PR #367 review): the token in argv, where every other Linux user
          # can read it out of ps, and a plain `curl -sS` that exits 0 on a
          # 404 and hands back an error body where a URL was expected. Prose
          # cannot be tested; this can, and it fails on both if either
          # regresses. gh and curl are shimmed, so it needs no network and no
          # token — the real endpoint has no sandbox, and a check that depends
          # on GitHub being up is a check that goes red for someone else's
          # outage.
          upload-cli =
            pkgs.runCommand "agent-box-upload-cli"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.coreutils pkgs.jq ];
                script = ./modules/src/upload-cli.sh;
                tests = ./tests/test-upload-cli.sh;
              } ''
              bash "$tests" "$script" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Unit test for lazy harness installation (issue #416). The VM
          # tests cannot cover this: an install reaches the network, and a
          # check that goes red when channels.nixos.org is down is a check
          # that goes red for someone else's outage. nix and flock are
          # shimmed instead, so every branch — the table still winning, the
          # refusal to turn a registry name into a path, and the rate limit
          # that keeps an offline box from making one network call per
          # respawn — is pinned natively on every architecture.
          jit-agents =
            pkgs.runCommand "agent-box-jit-agents"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.coreutils ];
                lib = ./modules/src/lib/agents.sh;
                tests = ./tests/test-jit-agents.sh;
              } ''
              bash "$tests" "$lib" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Unit test for `subscribe --claim` (issues #419, #420). A claim
          # is what stops a standing watch spawning a second session onto
          # work already in hand, and its failure mode is SILENT — an
          # incomplete claim warns about nothing, a sibling just turns up.
          # So which payload paths a claim covers is pinned here byte for
          # byte, with webhook.py shimmed to print its argv: no daemon, no
          # network, and it runs natively on every architecture.
          webhook-claim =
            pkgs.runCommand "agent-box-webhook-claim"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.coreutils pkgs.jq pkgs.gawk ];
                script = ./modules/src/webhook-cli.sh;
                tests = ./tests/test-webhook-claim.sh;
              } ''
              bash "$tests" "$script" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Unit test for the claim a DISPATCHED session is seeded with
          # (issues #251, #510, #511). Same reasoning as the check above —
          # a claim's failure mode is silent, so its payload paths are
          # pinned byte for byte — but for the other half of the mechanism:
          # webhook-cli.sh writes the claim a session asks for, and
          # webhook-spawn.sh writes the one a hook session is BORN with.
          #
          # It drives the spawn wrapper with a shimmed session CLI and env
          # store, then hands the claim it wrote to the REAL pinned
          # webhook.py: shape is half the promise, and the matcher agreeing
          # is the other half. These assertions used to live in the webhook
          # VM test, where a pure function of LOCAL_WEBHOOK_SPAWN_META cost
          # a VM boot — and where they eventually cost the test itself,
          # since nixpkgs passes testScript to the driver build as one
          # environment variable and Linux caps that at 128 KiB
          # (MAX_ARG_STRLEN): the driver stopped building with "Argument
          # list too long" before any VM booted.
          webhook-spawn-claim =
            pkgs.runCommand "agent-box-webhook-spawn-claim"
              {
                nativeBuildInputs = [
                  pkgs.bash
                  pkgs.coreutils
                  pkgs.jq
                  pkgs.python3
                ];
                # The whole directory, not the one file: the source form
                # carries @@include markers and the test resolves them
                # against its siblings, exactly as the assembler does.
                src = ./modules/src;
                tests = ./tests/test-webhook-spawn-claim.sh;
                # The same pin the module and #runtime read, fetched the
                # same way (nix/webhook-pin.nix).
                webhookPy =
                  let pin = import ./nix/webhook-pin.nix; in
                  builtins.fetchurl {
                    url = "https://raw.githubusercontent.com/${pin.repo}/${pin.rev}"
                          + "/local-webhook/webhook.py";
                    sha256 = pin.sha256;
                  };
              } ''
              bash "$tests" "$src/webhook-spawn.sh" "$webhookPy" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Unit tests for the durable per-session lease (issue #535):
          # outcome precedence (first ending wins, never the most recent),
          # clear's delete-not-blank resolution, and the read-only accessor
          # ls/peers call. Pure functions of a JSON file on disk, so this
          # runs in about a second natively rather than costing a VM boot —
          # what needs the VM instead is the WIRING (mark-stopped.sh and
          # supervisor.sh actually calling these at the right moment),
          # which tests/sessions.nix covers with the fake-agent harness.
          lease-protocol =
            pkgs.runCommand "agent-box-lease-protocol"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.coreutils pkgs.jq ];
                lease = ./modules/src/lib/lease.sh;
                tests = ./tests/test-lease.sh;
              } ''
              bash "$tests" "$lease" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Eval regression for selfUpdate.checkout's two assertions
          # (issue #242, PR #478 review). Both guard a value whose only
          # other feedback is a background job failing with EROFS in a
          # journal nobody reads, so what matters is that the REFUSAL
          # happens at eval — and `..`, `.` and the empty component are
          # exactly the inputs a first pass at "must be relative" lets
          # through.
          #
          # It reads config.assertions rather than forcing toplevel:
          # inspecting the list is what says WHICH assertion fired, where
          # a failed build would only say that something did.
          checkout-options =
            let
              evalWith = extra: (nixpkgs.lib.nixosSystem {
                inherit system;
                modules = [
                  self.nixosModules.agent-box
                  ({ modulesPath, ... }: {
                    imports = [ (modulesPath + "/virtualisation/qemu-vm.nix") ];
                  })
                  {
                    services.agent-box = nixpkgs.lib.recursiveUpdate {
                      enable = true;
                      users.agent = { };
                      selfUpdate = {
                        enable = true;
                        rev = "0000000000000000000000000000000000000000";
                      };
                    } extra;
                    system.stateVersion = "25.05";
                  }
                ];
              }).config.assertions;
              failed = extra:
                builtins.filter (a: !a.assertion) (evalWith extra);
              # Does SOME assertion fire, and does its message name the
              # option under test? Keyed on the option path, so a config
              # rejected for an unrelated reason cannot pass for a hit.
              rejects = option: extra:
                builtins.any
                  (a: nixpkgs.lib.hasInfix option a.message)
                  (failed extra);
              path = p: { selfUpdate.checkout.path = p; };
              srcDir = d: { selfUpdate.srcDir = d; };
              cases =
                # srcDir decides what ROOT builds, so its assertion is the
                # trust boundary and not a convenience: accepted cases must
                # keep working, and every path with an agent-writable
                # ancestor must be refused.
                map (d: { label = "accepts srcDir ${builtins.toJSON d}"; ok = !(rejects "selfUpdate.srcDir" (srcDir d)); })
                  [ "/var/lib/agent-box/src" "/var/lib/agent-box-src" "/var/lib/a/b/c" ]
                ++ map (d: { label = "refuses srcDir ${builtins.toJSON d}"; ok = rejects "selfUpdate.srcDir" (srcDir d); })
                  [ "/home/agent/src" "/home/agent/agent-box" "/tmp/src" "/var/tmp/src"
                    "/var/lib" "/var/libel/src" "relative/src" ""
                    "/var/lib/../../home/agent/src" "/var/lib//src" "/var/lib/./src" ]
                ++
                # Accepted: a plain name and a subdirectory.
                map (p: { label = "accepts ${p}"; ok = !(rejects "checkout.path" (path p)); })
                  [ "agent-box" "src/agent-box" ]
                # Refused: every way out of /home/<maintainer>.
                ++ map (p: { label = "refuses ${builtins.toJSON p}"; ok = rejects "checkout.path" (path p); })
                  [ "/srv/agent-box" "../agent-box" "src/../../agent-box" "." "" "a//b" "agent-box/" ]
                ++ [
                  { label = "accepts a maintainer that exists";
                    ok = !(rejects "checkout.maintainer"
                      { selfUpdate.checkout.maintainer = "agent"; }); }
                  { label = "refuses a maintainer that does not";
                    ok = rejects "checkout.maintainer"
                      { selfUpdate.checkout.maintainer = "nobody"; }; }
                  { label = "accepts a null maintainer";
                    ok = !(rejects "checkout.maintainer"
                      { selfUpdate.checkout.maintainer = null; }); }
                ];
              bad = builtins.filter (c: !c.ok) cases;
            in
            assert bad == [ ] || throw ("agent-box: checkout option assertions "
              + "did not behave as expected: "
              + builtins.concatStringsSep ", " (map (c: c.label) bad));
            pkgs.runCommand "agent-box-checkout-options-ok" { } ''
              printf '%s\n' ${nixpkgs.lib.escapeShellArg
                (builtins.concatStringsSep "\n" (map (c: "ok   " + c.label) cases))} \
                | tee "$out"
            '';

          # Unit test for the shipped source checkout (issue #242). Its
          # dangerous branches are the ones that do NOTHING: this runs
          # unattended at every supervisor start, in a tree sibling
          # sessions work in, so a realign that moved somebody's branch
          # pointer would destroy work at boot on a box nobody is
          # watching. `origin` is a local repository and `gh` is a shim,
          # so there is no network here and it runs natively on every
          # architecture — which is where the VM tests cannot go.
          # The box's own source tree (issue #242) — the thing "update the
          # box" now moves, and the fast-forward guard that replaced a
          # GitHub API compare. Root runs this unattended and the tree it
          # leaves behind is what the next rebuild BUILDS, so the refusals
          # (a rewritten history, a downgrade, a baseline the tree has never
          # heard of) are worth more assertions than the happy path.
          # `origin` is a local repository, so there is no network here and
          # it runs natively on every architecture.
          source-tree =
            pkgs.runCommand "agent-box-source-tree"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.coreutils pkgs.git pkgs.gnugrep ];
                script = ./modules/src/source-tree.sh;
                tests = ./tests/test-source-tree.sh;
              } ''
              bash "$tests" "$script" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          checkout-bootstrap =
            pkgs.runCommand "agent-box-checkout-bootstrap"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.coreutils pkgs.git pkgs.gnugrep ];
                script = ./modules/src/checkout-cli.sh;
                tests = ./tests/test-checkout-bootstrap.sh;
              } ''
              bash "$tests" "$script" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # agent-box-candidate (this box before the fleet): the validator
          # standing between an agent's sudo grant and what root builds.
          # Same shape and same reasoning as source-tree above -- weighted
          # at the refusals, `origin` a local repository so there is no
          # network, and natively runnable on every architecture.
          candidate =
            pkgs.runCommand "agent-box-candidate-check"
              {
                nativeBuildInputs = [ pkgs.bash pkgs.coreutils pkgs.git pkgs.gnugrep pkgs.gnused ];
                script = ./modules/src/candidate.sh;
                tests = ./tests/test-candidate.sh;
              } ''
              bash "$tests" "$script" > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Issue #425: a box with no webhook panel used to render an
          # empty string, so its operator could not tell a feature that is
          # off from one that is wired up wrong — which is how #425 was
          # reported in the first place. The subject is the golden payload
          # (the daemon as it actually ships, env-store library and all),
          # imported under one environment per state.
          webhook-panel-state =
            pkgs.runCommand "agent-box-webhook-panel-state"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                daemon = ./tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings;
                tests = ./tests/test-webhook-panel-state.py;
              } ''
              install -d repo/tests/golden/web/payloads/agent-box-settings/bin
              cp "$daemon" \
                repo/tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings
              cp "$tests" repo/tests/test-webhook-panel-state.py
              python3 repo/tests/test-webhook-panel-state.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # The settings page's agent-profile panel (issue #321, step 5).
          # Same shape and same subject as webhook-panel-state above: the
          # GOLDEN PAYLOAD, which is the daemon as it actually ships with
          # the env-store library prepended. Natively runnable on every
          # architecture, in seconds — the VM tests cannot pin "a value is
          # never rendered" or "the profile NAME is recorded on the
          # session" at anything like this price.
          profile-panel =
            pkgs.runCommand "agent-box-profile-panel"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                daemon = ./tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings;
                tests = ./tests/test-profile-panel.py;
              } ''
              install -d repo/tests/golden/web/payloads/agent-box-settings/bin
              cp "$daemon" \
                repo/tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings
              cp "$tests" repo/tests/test-profile-panel.py
              python3 repo/tests/test-profile-panel.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # render_connect_card()'s "checking" window: the status probe
          # never blocks a render, so every card starts "checking" on a
          # cold cache and the Sign-in button must exist there — while a
          # destructive flow's confirmation must stay armed until the
          # probe actually clears it. Same shape and same subject as the
          # two above: the GOLDEN PAYLOAD, the daemon as it actually ships.
          connect-card =
            pkgs.runCommand "agent-box-connect-card"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                daemon = ./tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings;
                tests = ./tests/test-connect-card.py;
              } ''
              install -d repo/tests/golden/web/payloads/agent-box-settings/bin
              cp "$daemon" \
                repo/tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings
              cp "$tests" repo/tests/test-connect-card.py
              python3 repo/tests/test-connect-card.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # The settings daemon's session routes against a registry that
          # does not parse (issue #279). Same shape and same subject as the
          # two above: the GOLDEN PAYLOAD, the daemon as it actually ships.
          # The VM half of #279 (the supervisor moving the bad file aside)
          # rides in tests/sessions.nix and the registry-protocol check
          # below; what only this can price is the three mutation ROUTES,
          # each against five ways the file can be broken, in seconds.
          sessions-registry =
            pkgs.runCommand "agent-box-sessions-registry"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                daemon = ./tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings;
                tests = ./tests/test-sessions-registry.py;
              } ''
              install -d repo/tests/golden/web/payloads/agent-box-settings/bin
              cp "$daemon" \
                repo/tests/golden/web/payloads/agent-box-settings/bin/agent-box-settings
              cp "$tests" repo/tests/test-sessions-registry.py
              python3 repo/tests/test-sessions-registry.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # Unit test for the env store's format (issue #212). The VM tests
          # prove one PEM survives one caller at 300+ seconds a run; this
          # pins the format itself — round trip, no key injection, and the
          # legacy readings a deployed box's file relies on — in a second,
          # natively, on every architecture. It also composes and runs the
          # CLI the way the generated module does, so the library and its
          # front end cannot drift apart unnoticed.
          envstore-format =
            pkgs.runCommand "agent-box-envstore-format"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                envstoreLib = ./modules/src/lib/envstore.py;
                envstoreCli = ./modules/src/envstore-cli.py;
                tests = ./tests/test-envstore.py;
              } ''
              install -d repo/modules/src/lib repo/tests
              cp "$envstoreLib" repo/modules/src/lib/envstore.py
              cp "$envstoreCli" repo/modules/src/envstore-cli.py
              cp "$tests" repo/tests/test-envstore.py
              # Not piped into tee: the log has to reach the build output
              # whether the tests pass or fail, and the exit status has to be
              # python's own.
              python3 repo/tests/test-envstore.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';

          # The session registry's write protocol (issues #254, #289). The VM
          # tests deliberately avoid concurrency — they stop the supervisor, or
          # write once so the first spawn sees the final config — so a lock
          # that stopped working would cost 300s a run and still go unnoticed
          # (issue #285). This runs the real library, composed the way the
          # generated module composes it, against real concurrent writers, in
          # about ten seconds on every architecture. It also holds the settings
          # daemon's fcntl side and the shell side to the same sidecar file,
          # which is the one agreement nothing else checks.
          registry-protocol =
            pkgs.runCommand "agent-box-registry-protocol"
              {
                nativeBuildInputs = [ pkgs.python3 pkgs.bash pkgs.jq pkgs.util-linux pkgs.coreutils ];
                registryLib = ./modules/src/lib/registry.sh;
                tests = ./tests/test-registry.py;
              } ''
              install -d repo/modules/src/lib repo/tests
              cp "$registryLib" repo/modules/src/lib/registry.sh
              cp "$tests" repo/tests/test-registry.py
              # Not piped into tee: the log has to reach the build output
              # whether the tests pass or fail, and the exit status has to be
              # python's own.
              python3 repo/tests/test-registry.py > log 2>&1 || {
                cat log
                exit 1
              }
              cat log
              cp log "$out"
            '';
        }
        # Everything below boots a guest, so it only exists for `vmSystems`:
        # runNixOSTest wants a same-arch KVM guest (cross-arch falls back to
        # TCG, which is too slow to be useful), and vm-closure builds the
        # imageSystem-pinned nixosConfigurations.vm.
        // nixpkgs.lib.optionalAttrs (builtins.elem system vmSystems) {
          # Full closure build of the VM config — the "is it actually usable"
          # proof (compiles the system agents would run in).
          vm-closure = self.nixosConfigurations.vm.config.system.build.vm;

          # Interactive VM test for the whole user-facing web surface, in one
          # guest (issue #312 — this was three tests with the same node
          # definition): the per-user ~/downloads file drop served behind the
          # auth gate (issue #132), an agent adding a vhost by writing ~/sites/
          # and reloading caddy via the sudoAllowlist rule with no
          # nixos-rebuild (issue #40), and wrong-password basic-auth attempts
          # getting the client IP banned by the fail2ban jail. Needs KVM (or
          # slow TCG); CI enables /dev/kvm before building this.
          web-surface = pkgs.testers.runNixOSTest
            (import ./tests/web-surface.nix { agent-box = self.nixosModules.agent-box; });

          # Interactive VM test: the per-user settings page (issue #36) adds a
          # secret through the browser (behind basic auth), writes the
          # user-owned 0600 env file, lists key names only, and the agent unit
          # picks the file up as an optional EnvironmentFile — no rebuild.
          settings-page = pkgs.testers.runNixOSTest
            (import ./tests/settings-page.nix { agent-box = self.nixosModules.agent-box; });

          connect = pkgs.testers.runNixOSTest
            (import ./tests/connect.nix { agent-box = self.nixosModules.agent-box; });

          # Interactive VM test (issue 62): protectMemory defaults — zram
          # swap active, agent unit's OOMScoreAdjust applied, and earlyoom
          # kills a runaway memory hog while the box stays responsive
          # (instead of the swapless refault livelock that froze a deployed
          # 2 GB box for hours).
          memory-protection = pkgs.testers.runNixOSTest
            (import ./tests/memory-protection.nix { agent-box = self.nixosModules.agent-box; });

          # Interactive VM test (issue #59): sessions are runtime data — the
          # seeded "main" session starts, `agent-box-session add/rm` brings a
          # second agent up and down as the user (no sudo, no rebuild), the
          # runtime session lives inside the hardened unit's cgroup, and the
          # supervisor's own bookkeeping survives two writers racing it.
          sessions = pkgs.testers.runNixOSTest
            (import ./tests/sessions.nix { agent-box = self.nixosModules.agent-box; });

          # The browser half of the same box (issue #312 — this and `sessions`
          # were one test that ran for 325s, more than the other five checks
          # put together, so no amount of --max-jobs could shorten the wave):
          # the tabbed workspace at /<user>/, the settings page's session
          # manager, the /sessions/* CRUD routes, the live feed and the
          # transcript download, all behind the web auth gate. Shares
          # tests/sessions-common.nix with `sessions`, so both halves drive
          # the same box.
          sessions-web = pkgs.testers.runNixOSTest
            (import ./tests/sessions-web.nix { agent-box = self.nixosModules.agent-box; });

          # Interactive VM test (issue #101): the per-user webhook receiver, ON
          # BY DEFAULT. Socket-activated 0660 <user>:caddy ingress, the
          # unauthenticated public path next to a still-401ing vhost, HMAC
          # accept/reject (and 404 before any secret exists — why default-on is
          # safe), IPC fan-out into a stand-in session peer applying its own
          # filter, and the discovery surface (CLI on PATH,
          # AGENT_BOX_WEBHOOK_URL, per-session LOCAL_WEBHOOK_SESSION, seeded
          # claude plugin settings).
          webhook = pkgs.testers.runNixOSTest
            (import ./tests/webhook.nix { agent-box = self.nixosModules.agent-box; });

          # The cliff every VM test in this directory is walking toward, and
          # the one failure that says nothing useful when you reach it.
          #
          # nixpkgs hands the driver build its whole test script in ONE
          # environment variable (`testScript = config.testScriptString` in
          # nixos/lib/testing/driver.nix). Linux caps a single environment
          # string at MAX_ARG_STRLEN — 32 pages, 128 KiB — so the FIRST thing
          # that happens past that is execve failing for the builder's shell:
          #
          #   nixos-test-driver-agent-box-webhook> error: executing
          #     '…/bin/bash': Argument list too long
          #
          # No VM boots, nothing names the test script, and the size of a
          # comment is not where anyone looks. tests/webhook.nix hit it in
          # PR #518 at 133 KiB, after a 155-line addition.
          #
          # So the limit is asserted with a page to spare and a cure attached.
          # The cure is not "write less": it is to move assertions that do not
          # need a VM into a native check (`webhook-spawn-claim` is one such
          # move), or to split the test the way tests/sessions-common.nix
          # split the session tests in issue #312.
          testscript-fits =
            let
              # 128 KiB is the kernel's; one page under it is ours, so the
              # guard fires before the unreadable failure does.
              limit = 131072 - 4096;
              # Every VM test in the check set, found by the passthru only
              # runNixOSTest has. removeAttrs first: filterAttrs would force
              # this attribute's own value and recurse forever.
              vmTests = nixpkgs.lib.filterAttrs (_: t: t ? driver)
                (builtins.removeAttrs self.checks.${system} [ "testscript-fits" ]);
              sizes = nixpkgs.lib.mapAttrs
                (_: t: builtins.stringLength t.driver.drvAttrs.testScript) vmTests;
              over = nixpkgs.lib.filterAttrs (_: n: n > limit) sizes;
              report = nixpkgs.lib.concatStringsSep "\n" (nixpkgs.lib.mapAttrsToList
                (name: n: "  ${name}: ${toString n} bytes (${toString (n - limit)} over)")
                over);
            in
            if over == { } then
              pkgs.runCommand "agent-box-testscript-fits" { } ''
                ${nixpkgs.lib.concatStringsSep "\n" (nixpkgs.lib.mapAttrsToList
                  (name: n: "echo '${name}: ${toString n} bytes'") sizes)}
                touch "$out"
              ''
            else
              throw ''
                VM test script over the ${toString limit}-byte limit:
                ${report}

                nixpkgs passes testScript to the driver build as one environment
                variable, and Linux caps that at 128 KiB (MAX_ARG_STRLEN). Past
                it the driver fails to build with "Argument list too long" and
                no VM boots.

                Move assertions that do not need a VM into a native check (see
                `webhook-spawn-claim`), or split the test the way
                tests/sessions-common.nix split the session tests (issue #312).
              '';
        });
    };
}
