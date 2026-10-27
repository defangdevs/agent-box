# Reject invalid recipient configuration before the SMTP service can restart-loop.
{ nixpkgs, system, agent-box, pkgs }:
let
  failed = extra:
    builtins.filter (a: !a.assertion && nixpkgs.lib.hasInfix "agent-box email" a.message)
      (nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          agent-box
          {
            services.agent-box = nixpkgs.lib.recursiveUpdate {
              enable = true;
              web = { enable = true; domain = "box.test"; };
              users.agent = { email = true; web.passwordHashFile = "/etc/agent.hash"; };
              email = {
                enable = true;
                domains = [ "box.test" ];
                certificateFile = "/etc/mail/cert.pem";
                keyFile = "/etc/mail/key.pem";
              };
            } extra;
            system.stateVersion = "25.05";
          }
        ];
      }).config.assertions;
  rejects = label: extra: builtins.any (a: nixpkgs.lib.hasInfix label a.message) (failed extra);
in
assert failed { } == [ ];
assert failed { email.domains = [ "a" "1-2-3-4.defangstation.com" ]; } == [ ];
assert failed { users._worker = { email = true; web.passwordHashFile = "/etc/worker.hash"; }; } == [ ];
assert rejects "email.domains" { email.domains = [ "box.test." ]; };
assert rejects "email.domains" { email.domains = [ "box.test-" ]; };
assert rejects "email.domains" { email.domains = [ (builtins.concatStringsSep "" (builtins.genList (_: "a") 254)) ]; };
assert rejects "email user Alice" { users.Alice = { email = true; web.passwordHashFile = "/etc/alice.hash"; }; };
assert rejects "email user agent" { users.agent.web.passwordHashFile = null; };
pkgs.runCommand "agent-box-email-options-ok" { } ''
  touch "$out"
''
