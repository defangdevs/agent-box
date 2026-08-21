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
              tmpfiles = builtins.filter (r: lib.hasInfix "agent-box" r)
                sys.config.systemd.tmpfiles.rules;
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

          # The runtime profile a non-NixOS host installs instead of a system
          # closure (issue #154 Phase 4):
          #   nix profile install github:defangdevs/agent-box/<rev>#runtime
          # Built from modules/src/*, the same payloads the NixOS module
          # embeds — see nix/runtime.nix.
          runtime = import ./nix/runtime.nix {
            pkgs = nixpkgs.legacyPackages.${system};
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
        in
        {
          # Eval-level assertion; cheap.
          multi-user = assert missing == [ ];
            pkgs.runCommand "agent-box-multi-user-ok" { } ''
              printf 'generated services: %s\n' ${nixpkgs.lib.escapeShellArg (toString wanted)} > "$out"
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
                nativeBuildInputs = [ pkgs.bash ];
                profile = self.packages.${system}.runtime;
                srcDir = ./modules/src;
              } ''
              fail=0
              # bin/<name> must be src/<file>, modulo the added shebang.
              check_payload() {
                bin="$profile/bin/$1"
                src="$srcDir/$2"
                if [ ! -x "$bin" ]; then
                  echo "MISSING: bin/$1"; fail=1; return
                fi
                # writeShellScriptBin prepends a shebang and ends the file
                # with a newline; the body between must match byte for byte.
                if ! diff -u <(tail -n +2 "$bin") <(cat "$src"; printf '\n') >/dev/null \
                  && ! diff -u <(tail -n +2 "$bin") "$src" >/dev/null; then
                  echo "DRIFT: bin/$1 is not modules/src/$2 verbatim"
                  diff -u <(tail -n +2 "$bin") "$src" | head -20 || true
                  fail=1
                  return
                fi
                bash -n "$bin" || { echo "SYNTAX: bin/$1"; fail=1; }
                echo "ok: bin/$1 == src/$2"
              }
              check_payload agent-box-supervisor supervisor.sh
              check_payload agent-box-attach attach.sh
              check_payload agent-box-mark-stopped mark-stopped.sh
              check_payload agent-box-spot-monitor spot-monitor.sh
              check_payload agent-box-update update.sh
              check_payload agent-box-codex-remote-control codex-remote-control.sh
              check_payload agent-box-claude-session-start-hook claude-session-start-hook.sh
              check_payload agent-box-env-exec env-exec.sh
              check_payload agent-box-session-bare session-cli.sh

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
              for a in default-agents.md default-agents-webhook.md; do
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
              for b in agent-box-settings tmux ttyd caddy jq gh git python3 \
                       bwrap flock claude codex; do
                [ -x "$profile/bin/$b" ] || { echo "MISSING: bin/$b"; fail=1; }
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
              python3 tests/test_agentbox.py -v 2>&1 | tail -20

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
              printf 'agentbox render matches tests/native/expected\n' > "$out"
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
              } ''
              install -d repo/bin repo/modules repo/docs
              cp "$assembler" repo/bin/assemble-module.py
              cp "$template" repo/modules/agent-box.nix.in
              cp "$committed" repo/modules/agent-box.nix
              cp -r "$srcDir" repo/modules/src
              cp "$mark" repo/docs/potato.svg
              python3 repo/bin/assemble-module.py --check --repo repo
              touch "$out"
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

          # Interactive VM test: wrong-password basic-auth attempts on the web
          # terminal get the client IP banned. Needs KVM (or slow TCG); CI
          # enables /dev/kvm before building this.
          web-fail2ban = pkgs.testers.runNixOSTest
            (import ./tests/web-fail2ban.nix { agent-box = self.nixosModules.agent-box; });

          # Interactive VM test: an agent user drops a snippet into ~/sites/
          # and reloads caddy via the sudoAllowlist rule; the new vhost
          # serves without any nixos-rebuild.
          self-serve-domain = pkgs.testers.runNixOSTest
            (import ./tests/self-serve-domain.nix { agent-box = self.nixosModules.agent-box; });

          # Interactive VM test: the per-user settings page (issue #36) adds a
          # secret through the browser (behind basic auth), writes the
          # user-owned 0600 env file, lists key names only, and the agent unit
          # picks the file up as an optional EnvironmentFile — no rebuild.
          settings-page = pkgs.testers.runNixOSTest
            (import ./tests/settings-page.nix { agent-box = self.nixosModules.agent-box; });

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
          # settings daemon serves the root session manager page plus its
          # CRUD routes, all behind the web auth gate.
          sessions = pkgs.testers.runNixOSTest
            (import ./tests/sessions.nix { agent-box = self.nixosModules.agent-box; });

          # Interactive VM test (issue #132): each web user's ~/downloads
          # file-drop dir is served behind the terminal's basic auth at
          # /<user>/downloads/, so an agent can hand a produced file to the
          # user as a URL — perms/symlink, caddy reachability, and the auth
          # gate.
          download-files = pkgs.testers.runNixOSTest
            (import ./tests/download-files.nix { agent-box = self.nixosModules.agent-box; });

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
        });
    };
}
