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
      packages.${imageSystem} =
        let
          image = self.nixosConfigurations.vm.config.system.build.images.qemu;
        in
        {
          vm = image;
          default = image;
        };

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
          wanted = [ "agent-box-alice" "agent-box-bob" "agent-box-coder" "agent-box-ci" ];
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
          module-generated-up-to-date =
            pkgs.runCommand "agent-box-module-generated-up-to-date"
              {
                nativeBuildInputs = [ pkgs.python3 ];
                assembler = ./bin/assemble-module.py;
                template = ./modules/agent-box.nix.in;
                committed = ./modules/agent-box.nix;
                srcDir = ./modules/src;
              } ''
              install -d repo/bin repo/modules
              cp "$assembler" repo/bin/assemble-module.py
              cp "$template" repo/modules/agent-box.nix.in
              cp "$committed" repo/modules/agent-box.nix
              cp -r "$srcDir" repo/modules/src
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
