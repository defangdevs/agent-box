# VM test for issue #628: the browser terminal's transport is private to its
# own user and the proxy in front of it.
#
# ttyd runs --writable and has no credential of its own -- Caddy authenticates
# the PUBLIC URL, which says nothing about who else on the box can open the
# thing behind it. While that thing was `-i 127.0.0.1 -p 7681`, any local user
# could: connect, upgrade, and be typing into another user's tmux under that
# user's identity. Two agent users on one box is the ordinary case here (issue
# #352: a project IS a linux user), and the wiki's Users-vs-Sessions page is
# explicit that the user boundary is the only one this deployment has -- so
# this is the boundary, and it needs a test with two real users in it.
#
# What is proven here, with `mallory` standing in for any second local user:
#   - nothing listens on TCP for a terminal at all; the transport is a unix
#     socket, 0660 <user>:caddy inside a 2750 <user>:caddy directory,
#   - mallory cannot connect(2) to agent's socket, and agent can,
#   - mallory's own valid web login does not reach agent's terminal either,
#   - the authenticated owner still attaches, TYPES (a --writable proof that
#     survives the permission change), reconnects, and starts a stopped
#     session through the attach wrapper,
#   - a cross-origin WebSocket is refused by ttyd's --check-origin, as is one
#     with no Origin at all.
#
# The Caddyfile is swapped for a `tls internal` one (no ACME in the sandbox),
# as in every other web VM test here, but its terminal routes are the module's
# own shape: the per-session rewrite onto ttyd's path with ?arg=, the auth
# gate, and the unix-socket upstream. That the module's REAL Caddyfile emits
# that upstream is the `session-route` eval check's job.
{ agent-box }:
let
  probe = pkgs: pkgs.writers.writePython3Bin "ttyd-probe" {
    libraries = [ pkgs.python3Packages.websocket-client ];
    # A list REPLACES pycodestyle's defaults, so W503/W504 have to be named
    # here or they become errors (see AGENTS.md).
    flakeIgnore = [ "E501" "W503" "W504" "E226" ];
  } (builtins.readFile ./ttyd-probe.py);
in
{
  name = "agent-box-ttyd-isolation";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, lib, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    environment.systemPackages = [ pkgs.curl (probe pkgs) ];
    # The probes talk to the vhost by name, from the box itself.
    networking.hosts."127.0.0.1" = [ "box.test" ];

    services.agent-box = {
      enable = true;
      agent = "claude";
      # No session is declared here, and "main" is added by the CLI in the
      # test script instead. That is not a preference: `sessions.<n>.agent =
      # "shell"` fails an eval assertion, because it checks the session's
      # agent against installAgents, whose enum is the two agent CLIs and
      # cannot contain the always-available "shell" pseudo-agent -- so the
      # module's own `sessions` example (`scratch = { agent = "shell"; }`)
      # does not evaluate. Worth a fix of its own; not this PR's, which is a
      # security fix to the terminal transport.
      users.agent.web.passwordHashFile =
        "/var/lib/agent-box-web/password-hash";
      # The second local user. Nothing about her is unusual -- she has her own
      # terminal, her own password and no privilege over agent -- which is the
      # point: the old loopback port gave her one anyway. She needs no session
      # of her own: a ttyd (and its socket) is per USER, not per session.
      users.mallory.web.passwordHashFile =
        "/var/lib/agent-box-web/mallory-hash";
      web = {
        enable = true;
        domain = "box.test";
        user = "agent";
        # The probes below deliberately make unauthenticated and
        # wrong-user requests; a jail would ban the box from itself.
        fail2ban = false;
      };
    };
    system.stateVersion = "25.05";

    system.activationScripts.agent-web-password-hash.text = ''
      install -d -m 0700 /var/lib/agent-box-web
      for pair in password-hash:testpassword mallory-hash:mallorypassword; do
        file="/var/lib/agent-box-web/''${pair%%:*}"
        if [ ! -s "$file" ]; then
          (
            umask 077
            ${pkgs.caddy}/bin/caddy hash-password \
              --plaintext "''${pair##*:}" > "$file"
          )
          chmod 0600 "$file"
        fi
      done
    '';

    services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
      box.test {
        log
        tls internal
        @sess_agent {
          path_regexp sess_agent ^/agent/([^/]+)/(.*)$
          not path /agent/settings* /agent/downloads/* /agent/webhook*
        }
        rewrite @sess_agent /agent/{re.sess_agent.2}?arg={re.sess_agent.1}&{query}
        handle /agent/* {
          route {
            basic_auth bcrypt agent {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            reverse_proxy unix//run/agent-box-ttyd/agent/ttyd.sock
          }
        }
        @sess_mallory {
          path_regexp sess_mallory ^/mallory/([^/]+)/(.*)$
          not path /mallory/settings* /mallory/downloads/* /mallory/webhook*
        }
        rewrite @sess_mallory /mallory/{re.sess_mallory.2}?arg={re.sess_mallory.1}&{query}
        handle /mallory/* {
          route {
            basic_auth bcrypt mallory {
              mallory {$WEB_PASSWORD_HASH_MALLORY}
            }
            reverse_proxy unix//run/agent-box-ttyd/mallory/ttyd.sock
          }
        }
      }
    '');
  };

  testScript = ''
    import shlex


    def as_user(user, cmd):
        return "su -s /bin/sh " + user + " -c " + shlex.quote(cmd)


    def tmux(cmd):
        return as_user(
            "agent",
            "env TMUX_TMPDIR=/run/agent-box-agent tmux -L agent-box " + cmd
        )


    sock = "/run/agent-box-ttyd/agent/ttyd.sock"
    curl = "curl -sk"

    start_all()
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_for_unit("agent-web-terminal@agent.service")
    machine.wait_for_unit("agent-web-terminal@mallory.service")
    machine.wait_for_unit("caddy.service")
    machine.wait_for_file(sock)

    # A plain shell for "main", so that typing into the terminal later has an
    # unambiguous effect to assert -- a file appears in the agent's home. A
    # harness TUI would answer keystrokes with a redraw and nothing testable.
    machine.succeed(as_user("agent", "agent-box-session add main --harness shell"))
    machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=120)

    # 1. There is no terminal on TCP at all. Not "not on a well-known port":
    # the whole class is gone, so a future change that reintroduces a
    # listener on any port fails here rather than only on the two this test
    # would have thought to name.
    listeners = machine.succeed("ss -H -ltnp || true")
    for line in listeners.splitlines():
        assert "ttyd" not in line, listeners

    # 2. The shape the permissions rest on: a 2750 <user>:caddy directory
    # (SETGID, which is what hands the socket group caddy -- ttyd runs
    # unprivileged and cannot chown one) holding a 0660 <user>:caddy socket.
    for user in ("agent", "mallory"):
        got = machine.succeed(
            f"stat -c '%a %U %G' /run/agent-box-ttyd/{user}"
        ).strip()
        assert got == f"2750 {user} caddy", got
        got = machine.succeed(
            f"stat -c '%a %U %G' /run/agent-box-ttyd/{user}/ttyd.sock"
        ).strip()
        assert got == f"660 {user} caddy", got

    # 3. The vulnerability, in one syscall. connect(2) on a unix socket needs
    # write permission on the socket AND search permission on every directory
    # above it; mallory has neither, and the owner has both.
    refused = machine.succeed(
        "if " + as_user("mallory", f"ttyd-probe connect {sock}")
        + "; then echo rc=0; else echo rc=$?; fi"
    )
    assert "REFUSED" in refused, refused
    assert "Permission denied" in refused, refused
    assert "rc=3" in refused, refused
    machine.succeed(as_user("agent", f"ttyd-probe connect {sock}"))
    # The directory alone stops her: she cannot even look at the socket.
    machine.fail(as_user("mallory", f"stat {sock}"))

    # 4. Nor does her own valid login get her there. Same box, same vhost,
    # different user: the auth gate in front of agent's terminal only knows
    # agent's password.
    for cmd in (
        f"{curl} -o /dev/null -w '%{{http_code}}' https://box.test/agent/main/",
        f"{curl} -u mallory:mallorypassword -o /dev/null -w '%{{http_code}}'"
        " https://box.test/agent/main/",
    ):
        assert machine.succeed(cmd).strip() == "401", cmd

    # 5. The owner's browser still gets the terminal itself.
    page = machine.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/main/"
    )
    assert "ttyd" in page.lower(), page[:400]

    # 6. ... and it is still WRITABLE through the socket. The keystrokes go
    # the whole way: caddy -> unix socket -> ttyd -> agent-box-attach -> that
    # session's tmux pane -> a shell that creates the file.
    out = machine.succeed(
        as_user("agent",
                "ttyd-probe attach wss://box.test/agent/main/ws"
                " --user agent --password testpassword"
                " --origin https://box.test"
                " --send 'touch /home/agent/typed-through-the-socket'")
    )
    assert "ATTACHED" in out, out
    machine.wait_for_file("/home/agent/typed-through-the-socket")

    # 7. A second connection attaches just as well (the socket outlives a
    # client, which is the reconnect a browser does on every page load).
    out = machine.succeed(
        as_user("agent",
                "ttyd-probe attach wss://box.test/agent/main/ws"
                " --user agent --password testpassword"
                " --origin https://box.test --read 4")
    )
    assert "ATTACHED" in out, out

    # 8. Cross-origin, and origin-less, WebSockets are refused -- ttyd's
    # --check-origin, the browser half of the fix. It is defence in depth and
    # not a substitute for 3: this same request from a local process would
    # simply forge the header, which is why the socket is what carries the
    # boundary.
    for extra in ("--origin https://evil.example", ""):
        got = machine.succeed(
            "if " + as_user(
                "agent",
                "ttyd-probe attach wss://box.test/agent/main/ws"
                " --user agent --password testpassword " + extra
            ) + "; then echo rc=0; else echo rc=$?; fi"
        )
        assert "REFUSED" in got, got
        assert "rc=3" in got, got

    # 9. A stopped session still starts from the browser. The pane offers
    # "press Enter to start it here" (modules/src/attach.sh), so the probe
    # sends a bare Enter and stays connected while the wrapper clears the
    # stopped flag through the session CLI and the supervisor brings the
    # tmux session back. Nothing about that path went through the port, but
    # it is the one behaviour a permission mistake on the socket would break
    # silently -- the page would look fine and the session would never come
    # up. It also proves the offer is reachable at all: it only appears for
    # a pane on a real pty, which is ttyd's end of this socket.
    machine.succeed(as_user("agent", "agent-box-session stop main"))
    machine.wait_until_fails(tmux("has-session -t =main"), timeout=60)
    out = machine.succeed(
        as_user("agent",
                "ttyd-probe attach wss://box.test/agent/main/ws"
                " --user agent --password testpassword"
                " --origin https://box.test --enter --read 12")
    )
    assert "is stopped" in out, out
    machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=120)
  '';
}
