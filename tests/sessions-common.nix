# The box both session VM tests drive, and the python they both open with.
#
# tests/sessions.nix and tests/sessions-web.nix were ONE test until issue
# #312: at 1870 lines it ran for 325s, more than twice the next slowest check
# and longer than every other check put together, so `--max-jobs 3` could
# never finish the wave before it. Splitting it in two only helps if both
# halves keep testing the same box, so the node definition lives here once
# rather than being copied and drifting.
#
# Both tests declare the SAME node set — machine plus client — even though
# only the web half talks to the client. That is deliberate: the test
# framework writes every node's address into every node's /etc/hosts, so a
# machine with a client beside it and a machine without one are different
# systems, and CI would build (and cache) the guest twice. With the sets
# matching, the two tests name one nixos-system derivation. The CLI half
# starts only the machine, so the client costs nothing at run time.
{ agent-box }:
{
  machineNode = { pkgs, lib, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    environment.systemPackages = [ pkgs.curl pkgs.jq ];
    services.agent-box = {
      enable = true;
      agent = "claude";
      # Leave the host label unset so auto-derived Remote Control names fall
      # back to the public web.domain rather than the internal kernel
      # hostname (issue: derived names showed the internal EC2 fqdn).
      remoteControlHost = "";
      users.agent = {
        web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
        # This test predates the front door (issue #416) and its subject is
        # what happens WITH a session running, so it opts back in to the
        # seeded "main" a web box no longer gets by default. Without this
        # the supervisor starts no tmux server at all and every assertion
        # below fails on "error connecting to .../tmux-1000/agent-box".
        seedMainSession = true;
      };
      web = {
        enable = true;
        domain = "box.test";
        user = "agent";
        fail2ban = false;
      };
    };
    system.stateVersion = "25.05";

    system.activationScripts.agent-web-password-hash.text = ''
      install -d -m 0700 /var/lib/agent-box-web
      if [ ! -s /var/lib/agent-box-web/password-hash ]; then
        (
          umask 077
          ${pkgs.caddy}/bin/caddy hash-password --plaintext testpassword \
            > /var/lib/agent-box-web/password-hash
        )
        chmod 0600 /var/lib/agent-box-web/password-hash
      fi
    '';

    services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
      box.test {
        log
        tls internal
        handle /agent/settings* {
          route {
            basic_auth bcrypt agent {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            reverse_proxy unix//run/agent-box-settings/agent.sock
          }
        }
        handle {
          route {
            basic_auth bcrypt agent {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            reverse_proxy unix//run/agent-box-settings/agent.sock
          }
        }
      }
    '');
  };

  clientNode = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.curl ];
  };

  # The python both testScripts open with: the two ways every step talks to
  # the box, where the two files that hold session state live, and settle().
  prelude = ''
    import json
    import re
    import shlex

    def as_agent(cmd):
        return "su -s /bin/sh agent -c " + shlex.quote(cmd)

    def tmux(cmd):
        # Run a tmux command as the agent user against its own server (the
        # socket lives under the agent unit's RuntimeDirectory, not /tmp).
        return as_agent(
            "env TMUX_TMPDIR=/run/agent-box-agent "
            "tmux -L agent-box " + cmd
        )

    # The registry: what the user asked for.
    sfile = "/home/agent/.config/agent-box/sessions.json"

    def state_file(name):
        """The supervisor's own record for one session (issue #282):
        sessions.json is intent, this is what the supervisor observed. Spelled
        here the way modules/src/supervisor.sh's session_state_file spells
        it — one accessor per program, so re-keying it later (issue #284)
        stays a small change."""
        return f"/home/agent/.local/state/agent-box/session/{name}.json"

    # Every assertion that something did NOT come back has to outlast a whole
    # pass of the supervisor's reconcile loop, which sweeps the state files of
    # delisted sessions and then starts every listed session that has no tmux
    # session. Each of those points used to be a flat `sleep 6` commented "a
    # few supervisor ticks" — 48s of this suite's wall time, and a guess in
    # both directions (too long when the loop is prompt, too short on a
    # loaded runner).
    #
    # The sweep is observable, so wait on it instead: a state file whose name
    # is not in the registry is gone by the end of the next sweep. Two
    # canaries bracket one COMPLETE pass — the first proves a sweep ran, and
    # the second cannot vanish until the sweep AFTER it, so the start phase
    # between them has run too. It costs what the loop actually takes.
    _canaries = 0

    def settle(passes=2):
        """Block until the supervisor has completed `passes` reconcile passes."""
        global _canaries
        for _ in range(passes):
            _canaries += 1
            canary = state_file(f"settle-canary-{_canaries}")
            machine.succeed(as_agent("printf '{}' > " + canary))
            machine.wait_until_fails(f"test -e {canary}", timeout=60)
  '';
}
