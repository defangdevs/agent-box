# VM test for issues #36 and #91: the per-user settings page lets an end user
# manage agent secrets and change the web password through the browser (behind
# the same basic-auth as the terminal) without a nixos-rebuild. Exercises:
#   - the agent-box-settings-<user> daemon unit (runs as the agent user),
#   - the Caddy /<user>/settings* route inside the basic-auth block,
#   - writing ~/.config/agent-box/env atomically at 0600,
#   - the page listing key NAMES only (never values),
#   - the agent unit's optional EnvironmentFile picking the file up on restart,
#   - previous/new/confirm password validation, root-owned atomic hash rotation,
#     cookie invalidation, and live Caddy reload.
#
# Like the other tests, lib.mkForce-swaps the module Caddyfile for a minimal
# `tls internal` one (no ACME in the sandbox) that keeps the same
# cookie-or-basic-auth gate and reverse-proxies /agent/settings* to the
# settings daemon's unix socket (issue #49). Also asserts the socket's
# permission story: 0660 agent:caddy, other local users get EACCES, and
# nothing listens on TCP anymore.
{ agent-box }:
{
  name = "agent-box-settings-page";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, lib, ... }: let
    # Portal handover (issue #541) now verifies against the key set the
    # portal PUBLISHES, so the test has to publish one. Built here rather
    # than generated in an activation script: the private half has to be
    # readable by the minter too, and a fixture in the store is stable
    # across both.
    portalKeys = pkgs.runCommand "portal-test-keys" {
      nativeBuildInputs = [ pkgs.openssl pkgs.python3 ];
    } ''
      mkdir -p $out
      openssl genpkey -algorithm ed25519 -out $out/key.pem
      openssl pkey -in $out/key.pem -pubout -outform DER -out pub.der
      # A SECOND keypair the JWK Set deliberately does NOT carry, for the
      # "correctly formed, signed by a key nobody published" case.
      openssl genpkey -algorithm ed25519 -out $out/other.pem
      # An Ed25519 SubjectPublicKeyInfo is a fixed 12-byte prefix plus the
      # 32-byte key, so the JWK is a slice — no encoder, no crypto.
      python3 -c '
import base64, json, sys
der = open("pub.der", "rb").read()
assert len(der) == 44, len(der)
x = base64.urlsafe_b64encode(der[12:]).rstrip(b"=").decode()
json.dump({"keys": [{"kty": "OKP", "crv": "Ed25519", "use": "sig",
                     "kid": "test-key-1", "x": x}]}, sys.stdout)
' > $out/jwks.json
    '';
    # A cert for box.test that the MACHINE trusts, so the settings daemon's
    # own outbound fetch of the key set verifies. `tls internal` cannot do
    # that job: its CA is minted at runtime, and the daemon would have to be
    # told about it after caddy first ran.
    portalTls = pkgs.runCommand "box-test-tls" {
      nativeBuildInputs = [ pkgs.openssl ];
    } ''
      mkdir -p $out
      openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout $out/key.pem -out $out/cert.pem \
        -subj "/CN=box.test" -days 36500 \
        -addext "subjectAltName=DNS:box.test"
      chmod 0644 $out/key.pem
    '';
  in {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    # So the settings daemon's OWN fetch of the key set verifies. Python
    # reads /etc/ssl/certs/ca-certificates.crt by default, which is exactly
    # what this option builds — no env var on the unit, and none needed on a
    # real box either, where the portal has a public cert.
    security.pki.certificateFiles = [ "${portalTls}/cert.pem" ];
    # testScript is outside this function's scope, so the fixture is exposed
    # at a fixed path rather than interpolated into the script.
    environment.etc."agent-box-portal".source = portalKeys;
    # ...and so it can RESOLVE the issuer. On a real box that is public DNS;
    # here the box is its own portal.
    networking.hosts."127.0.0.1" = [ "box.test" ];
    # `agent-box-mint` stands in for the PORTAL (issue #541): it signs a
    # handover token exactly as docs/portal-handoff.md says one is signed,
    # so the subtests below exercise the daemon's real verification rather
    # than a fixture the daemon and the test could drift apart on.
    # Overrides let each subtest bend ONE field and watch it be refused.
    environment.systemPackages = [ pkgs.curl pkgs.openssl (pkgs.writers.writePython3Bin
      "agent-box-mint" { flakeIgnore = [ "E501" ]; } ''
      import base64
      import json
      import os
      import subprocess
      import sys
      import tempfile
      import time


      def b64(raw):
          return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


      def main():
          over = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
          now = int(time.time())
          # `kid` selects among the keys the portal published. Overridable,
          # so a token naming a key nobody published can be tested.
          kid = over.pop("kid", "test-key-1")
          header = {"alg": over.pop("alg", "EdDSA"), "typ": "JWT"}
          if kid:
              header["kid"] = kid
          claims = {"iss": "https://box.test", "aud": "agent-box",
                    "sub": "usr_2Nk9x", "project": "acme-prod",
                    "iat": now, "exp": now + 60,
                    "jti": over.pop("jti", "jti-%d" % now)}
          for key, value in over.items():
              if value is None:
                  claims.pop(key, None)
              else:
                  claims[key] = value
          head = b64(json.dumps(header).encode())
          body = b64(json.dumps(claims).encode())
          signing = (head + "." + body).encode("ascii")
          if header["alg"] != "EdDSA":
              # alg:none carries no signature at all, which is the classic
              # forgery the daemon must refuse on the header alone.
              print(signing.decode() + ".")
              return
          key_path = os.environ.get("MINT_KEY",
                                    "/etc/agent-box-portal/key.pem")
          with tempfile.TemporaryDirectory() as work:
              msg = os.path.join(work, "msg")
              with open(msg, "wb") as handle:
                  handle.write(signing)
              sig = subprocess.run(
                  ["openssl", "pkeyutl", "-sign", "-inkey", key_path,
                   "-rawin", "-in", msg],
                  capture_output=True, check=True).stdout
          print(signing.decode() + "." + b64(sig))


      main()
    '') ];
    # A second, unrelated local user: must NOT be able to reach agent's
    # settings daemon (issue #49).
    users.users.mallory = {
      isNormalUser = true;
    };
    services.agent-box = {
      enable = true;
      agent = "claude";
      # Its seeded "main" below runs the real claude, and a VM test cannot
      # fetch one (issue #416).
      eagerAgents = [ "claude" ];
      users.agent = {
        web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
        # This test predates the front door (issue #416) and its subject is
        # what happens WITH a session running, so it opts back in to the
        # seeded "main" a web box no longer gets by default. Without this
        # the supervisor starts no tmux server at all and every assertion
        # below fails on "error connecting to .../tmux-1000/agent-box".
        seedMainSession = true;
      };
      users.agent.web.portalUser = "usr_2Nk9x";
      users.agent.web.portalProject = "acme-prod";
      web = {
        enable = true;
        domain = "box.test";
        user = "agent";
        fail2ban = false;
        # Portal handover (issue #541). The key is PUBLIC, so unlike the
        # password hash it is world-readable — the settings daemon runs as
        # `agent` and could not read it out of the 0700 root directory the
        # hash lives in.
        # The box serves the key set itself (see the Caddyfile below), so
        # the derived well-known path resolves to its own vhost.
        portalIssuer = "https://box.test";
      };
      # Issue 54: the settings page grows an "Update agent-box" button that triggers
      # agent-box-update.service through the allowlisted sudo rule. The VM has
      # no network, so the unit itself will fail after activating — the test
      # only proves the trigger plumbing (button -> daemon -> sudo -> unit).
      selfUpdate = {
        enable = true;
        rev = "0000000000000000000000000000000000000000";
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

    # Minimal `tls internal` Caddyfile that keeps the settings route's auth
    # gate but proxies to the settings daemon's unix socket. Same env
    # placeholder the module wires up, so agent-web-auth-secrets still feeds
    # this vhost.
    services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
      box.test {
        log
        tls ${portalTls}/cert.pem ${portalTls}/key.pem
        # The portal's published key set. Unauthenticated on purpose --
        # these are PUBLIC keys, and the daemon fetching them holds no
        # credential for this vhost.
        handle /.well-known/jwks.json {
          root * ${portalKeys}
          rewrite * /jwks.json
          file_server
        }
        # Portal handover (issue #541): the one unauthenticated route, and
        # more specific than the catch-all so caddy reaches it first.
        handle /agent/auth/* {
          reverse_proxy unix//run/agent-box-settings/agent.sock
        }
        handle /agent/settings* {
          @cookie_settings header_regexp Cookie "(^|; )__Host-agent_box_auth_agent={$WEB_COOKIE_SECRET_AGENT}(;|$)"
          handle @cookie_settings {
            reverse_proxy unix//run/agent-box-settings/agent.sock
          }
          handle {
            route {
              basic_auth {$WEB_PASSWORD_ALGORITHM_AGENT} agent {
                agent {$WEB_PASSWORD_HASH_AGENT}
              }
              header >Set-Cookie "__Host-agent_box_auth_agent={$WEB_COOKIE_SECRET_AGENT}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Strict"
              reverse_proxy unix//run/agent-box-settings/agent.sock
            }
          }
        }
        # Root catch-all: the session manager and its /sessions/* CRUD
        # routes (the daemon runs in AGENT_BOX_HOME mode for web.user) —
        # same auth gate, same upstream, mirroring the module Caddyfile.
        handle {
          # A live portal session reaches the box exactly as a basic-auth
          # login does, and caddy asks the daemon rather than matching a
          # static string. forward_auth is stock caddy, no plugin.
          @portal_root header_regexp Cookie "(^|; )__Host-agent_box_session_agent=([A-Za-z0-9_-]+)(;|$)"
          handle @portal_root {
            route {
              forward_auth unix//run/agent-box-settings/agent.sock {
                uri /agent/auth/verify
              }
              reverse_proxy unix//run/agent-box-settings/agent.sock
            }
          }
          @cookie_root header_regexp Cookie "(^|; )__Host-agent_box_auth_agent={$WEB_COOKIE_SECRET_AGENT}(;|$)"
          handle @cookie_root {
            reverse_proxy unix//run/agent-box-settings/agent.sock
          }
          handle {
            route {
              basic_auth {$WEB_PASSWORD_ALGORITHM_AGENT} agent {
                agent {$WEB_PASSWORD_HASH_AGENT}
              }
              header >Set-Cookie "__Host-agent_box_auth_agent={$WEB_COOKIE_SECRET_AGENT}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Strict"
              reverse_proxy unix//run/agent-box-settings/agent.sock
            }
          }
        }
      }
    '');
  };

  nodes.client = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.curl ];
  };

  testScript = ''
    start_all()
    machine.wait_for_unit("caddy.service")
    machine.wait_for_unit("agent-box@agent.service")
    machine.wait_for_unit("agent-box-settings@agent.service")
    client.wait_for_unit("multi-user.target")

    def tmux(cmd):
        # Run a tmux command as the agent user against its own server (the
        # socket lives under the agent unit's RuntimeDirectory, not /tmp).
        return (
            "su -s /bin/sh agent -c 'env TMUX_TMPDIR=/run/agent-box-agent "
            "tmux -L agent-box " + cmd + "'"
        )

    machine_ip = machine.succeed("ip -4 -o addr show eth1 | head -1").split()[3].split("/")[0]
    curl = f"curl -sk --resolve box.test:443:{machine_ip}"

    # Daemon runs AS the agent user (no root).
    machine.succeed(
        "systemctl show agent-box-settings@agent --property=User | grep -x 'User=agent'"
    )

    # The daemon lists /run/agent-box-agent in ReadWritePaths, and nothing but
    # agent-box@agent's own RuntimeDirectory creates it — so it must be
    # ORDERED after that unit. Both are wanted by multi-user.target, so
    # without the ordering they raced on every boot, and a boot this daemon
    # won failed the whole namespace setup with 226/NAMESPACE (the same class
    # of bug as the ~/sites entry guarded in tests/memory-protection.nix).
    # Asserted here rather than in tests/golden/: the ordering lives in the
    # verbatim unit text shipped via systemd.packages, whose raw bytes the
    # golden snapshot cannot see (issue #299).
    machine.succeed(
        "systemctl show agent-box-settings@agent --property=After --value "
        "| tr ' ' '\\n' | grep -x 'agent-box@agent.service' >/dev/null"
    )
    machine.succeed(
        "systemctl show agent-box-settings@agent --property=ReadWritePaths "
        "--value | grep /run/agent-box-agent >/dev/null"
    )

    # Issue #49: the daemon listens ONLY on the systemd-owned unix socket —
    # 0660 agent:caddy, no TCP listener for other local users to reach.
    machine.succeed(
        "stat -c '%U %G %a' /run/agent-box-settings/agent.sock | grep -x 'agent caddy 660'"
    )
    machine.fail("ss -tln | grep ':7781' >/dev/null")

    sock_curl = "curl -s --max-time 10 --unix-socket /run/agent-box-settings/agent.sock"

    # The owning user can talk to its own daemon over the socket, and gets
    # the rendered page back (heading plus its icon span; a single-line
    # regex can't pin both at once since {mark} is a multi-line inline SVG).
    sock_page = machine.succeed(
        f"su -s /bin/sh agent -c '{sock_curl} http://localhost/agent/settings/'"
    )
    assert '<span class="mark">' in sock_page
    assert "Settings</h1>" in sock_page
    # ...another local user gets permission denied — cannot list, write, or
    # restart. (Before the fix, all three worked over 127.0.0.1:7781.)
    machine.fail(
        f"su -s /bin/sh mallory -c '{sock_curl} http://localhost/agent/settings/'"
    )
    machine.fail(
        f"su -s /bin/sh mallory -c '{sock_curl} -d key=PWNED -d value=x "
        "http://localhost/agent/settings/set'"
    )
    machine.fail(
        f"su -s /bin/sh mallory -c '{sock_curl} -X POST http://localhost/agent/settings/restart'"
    )

    # Unauthenticated request to the settings path is rejected (401).
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' https://box.test/agent/settings/ | grep -x 401"
    )

    # Authenticated GET renders the page.
    auth_page = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/settings/"
    )
    assert '<span class="mark">' in auth_page
    assert "Settings</h1>" in auth_page

    # No env file exists yet.
    machine.fail("test -e /home/agent/.config/agent-box/env")

    # Issue 117: a browser cross-site POST (valid basic auth, but the browser
    # marks the request cross-site / carries a foreign Origin) is refused 403,
    # so CSRF cannot inject a secret even though auth succeeds.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-H 'Sec-Fetch-Site: cross-site' -d 'key=PWNED&value=x' "
        "https://box.test/agent/settings/set | grep -x 403"
    )
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-H 'Origin: https://evil.example' -d 'key=PWNED&value=x' "
        "https://box.test/agent/settings/set | grep -x 403"
    )
    # A same-origin marker with matching Origin is accepted (the real page).
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-H 'Sec-Fetch-Site: same-origin' -H 'Origin: https://box.test' "
        "-d 'key=OKKEY&value=x' https://box.test/agent/settings/set | grep -x 303"
    )
    # The blocked posts wrote nothing; the accepted one did.
    machine.fail("grep -q PWNED /home/agent/.config/agent-box/env")
    machine.succeed("grep -q '^OKKEY=x$' /home/agent/.config/agent-box/env")
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null "
        "-d 'key=OKKEY' https://box.test/agent/settings/delete"
    )

    # POST a secret through the page (never touches the terminal/chat).
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'key=GH_TOKEN&value=ghp_supersecret' "
        "https://box.test/agent/settings/set | grep -x 303"
    )

    # File written, owned by agent, mode 0600, with the value.
    machine.succeed(
        "stat -c '%U %a' /home/agent/.config/agent-box/env | grep -x 'agent 600'"
    )
    machine.succeed("grep -q '^GH_TOKEN=ghp_supersecret$' /home/agent/.config/agent-box/env")

    # The page lists the key NAME but NEVER the value.
    page = client.succeed(f"{curl} -u agent:testpassword https://box.test/agent/settings/")
    assert "GH_TOKEN" in page, "key name should be listed"
    assert "ghp_supersecret" not in page, "value must never be rendered"
    assert 'action="/agent/settings/password"' in page, "password form should be rendered"
    for field in ["previous_password", "new_password", "confirm_password"]:
        assert f'name="{field}"' in page, f"password form should contain {field}"

    # Delete the key.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'key=GH_TOKEN' https://box.test/agent/settings/delete | grep -x 303"
    )
    machine.fail("grep -q GH_TOKEN /home/agent/.config/agent-box/env")

    # Issue 89: the user env file must NOT be a unit-level EnvironmentFile —
    # that's a snapshot from unit start, and sessions are respawned by the
    # long-lived supervisor, so UI-added secrets never reached restarted
    # sessions (and deleted keys never left). The spawn wrapper below is
    # the live source instead.
    machine.fail(
        "systemctl show agent-box@agent --property=EnvironmentFiles "
        "| grep '/home/agent/.config/agent-box/env' >/dev/null"
    )

    # Dump the environment of the main session's AGENT process. The pane
    # pid is the `sh -c "wrapper cmd || exec bash"` shell; the env-exec
    # wrapper (and the agent it execs, same pid) is that shell's CHILD —
    # /proc environ is an exec-time snapshot, so the exports only show up
    # there. Empty output (no child yet) just fails the grep and retries.
    # Grep this dump with `grep PATTERN >/dev/null`, never `grep -q`: -q closes
    # the pipe on the first match, `tr` dies of EPIPE mid-dump, and pipefail
    # reports exit 123 for an assertion that actually held (CI run 31745418148).
    agent_env = (
        tmux('display -p -t "=main:" "#{pane_pid}"')
        + " | xargs -I{} sh -c 'pgrep -P {} | head -1'"
        + " | xargs -I{} sh -c \"tr '\\0' '\\n' < /proc/{}/environ\""
    )

    def wait_new_pane(restart_cmd):
        old_pane = machine.succeed(tmux('display -p -t "=main:" "#{pane_pid}"')).strip()
        client.succeed(restart_cmd)
        machine.wait_until_succeeds(
            tmux('display -p -t "=main:" "#{pane_pid}"')
            + f" | grep . | grep -vx '{old_pane}'",
            timeout=90,
        )

    # Issue 89 regression test: secrets saved through the page reach the
    # SESSION's process environment after a PER-SESSION restart (the spawn
    # wrapper re-reads the env file; the old unit-level EnvironmentFile
    # was a stale snapshot from unit start)...
    with subtest("UI-added env reaches a restarted session (spawn wrapper)"):
        machine.wait_until_succeeds(tmux("has-session -t =main"), timeout=120)
        for kv in ["key=UI_SECRET&value=from-the-ui", "key=UI_KEEP&value=stays"]:
            client.succeed(
                f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
                f"-d '{kv}' https://box.test/agent/settings/set | grep -x 303"
            )
        wait_new_pane(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-d 'name=main' https://box.test/sessions/restart | grep -x 303"
        )
        machine.wait_until_succeeds(
            agent_env + " | grep -x 'UI_SECRET=from-the-ui' >/dev/null", timeout=30
        )
        machine.succeed(agent_env + " | grep -x 'UI_KEEP=stays' >/dev/null")

    # A PEM is the value people actually need this form for (issue #212), and
    # a browser textarea posts its newlines as CRLF. What the session reads
    # back must be the text that was pasted: one key, LF line ends, whole.
    with subtest("a multi-line secret posted from the page reaches a session"):
        client.succeed(
            "printf '%s\\r\\n' '-----BEGIN PRIVATE KEY-----' 'MIIBVgIBADANBg==' "
            "> /tmp/pem && printf '%s' '-----END PRIVATE KEY-----' >> /tmp/pem"
        )
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-d 'key=UI_PEM' --data-urlencode 'value@/tmp/pem' "
            "https://box.test/agent/settings/set | grep -x 303"
        )
        # ONE quoted entry in the file, still 0600.
        machine.succeed(
            "grep -qx 'UI_PEM=\"-----BEGIN PRIVATE KEY-----' "
            "/home/agent/.config/agent-box/env"
        )
        machine.succeed(
            "stat -c '%U %a' /home/agent/.config/agent-box/env | grep -x 'agent 600'"
        )
        # The page still lists the NAME and never a line of the value.
        page = client.succeed(f"{curl} -u agent:testpassword https://box.test/agent/settings/")
        assert "UI_PEM" in page, page
        assert "BEGIN PRIVATE KEY" not in page, "value must never be rendered"
        wait_new_pane(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-d 'name=main' https://box.test/sessions/restart | grep -x 303"
        )
        # First and last line both arrive, so the middle did too...
        machine.wait_until_succeeds(
            agent_env + " | grep -x 'UI_PEM=-----BEGIN PRIVATE KEY-----' >/dev/null",
            timeout=30,
        )
        machine.succeed(agent_env + " | grep -x -- '-----END PRIVATE KEY-----' >/dev/null")
        # ...and the CRs the form posted are not in it.
        machine.fail(agent_env + " | grep \"$(printf '\\r')\" >/dev/null")
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-d 'key=UI_PEM' https://box.test/agent/settings/delete | grep -x 303"
        )

    # ...and "Restart all" bounces the WHOLE unit (the daemon SIGTERMs the
    # supervisor — no sudo), so unit-level EnvironmentFiles (the host's
    # environmentFiles) are re-read, and a DELETED key is gone from the next
    # spawn. UI_KEEP
    # doubles as the exec sentinel: the same read that proves it arrived
    # must show UI_SECRET absent.
    with subtest("restart-all bounces the unit; deleted env leaves"):
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-d 'key=UI_SECRET' https://box.test/agent/settings/delete | grep -x 303"
        )
        old_main = machine.succeed(
            "systemctl show agent-box@agent --property=MainPID --value"
        ).strip()
        wait_new_pane(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "-X POST https://box.test/agent/settings/restart | grep -x 303"
        )
        machine.wait_until_succeeds(
            "p=$(systemctl show agent-box@agent --property=MainPID --value); "
            f"[ -n \"$p\" ] && [ \"$p\" != 0 ] && [ \"$p\" != {old_main} ]",
            timeout=60,
        )
        machine.wait_until_succeeds(
            agent_env + " > /tmp/agent-env && grep -qx 'UI_KEEP=stays' /tmp/agent-env",
            timeout=30,
        )
        machine.fail("grep -q '^UI_SECRET=' /tmp/agent-env")

    # Issue 54: with selfUpdate enabled the page shows the Update card...
    assert "Update agent-box" in page, "Update card should be rendered when selfUpdate is on"
    # ...including the running rev as a GitHub commit link (selfUpdate.rev +
    # the default repo), so the page answers "what version is this box on".
    assert (
        "github.com/defangdevs/agent-box/commit/"
        "0000000000000000000000000000000000000000" in page
    ), "Update card should link the running rev to its GitHub commit"
    assert "<code>000000000000</code>" in page, "Update card should show the short rev"
    # Without JavaScript the card still links to GitHub's comparison. In a
    # browser the progressive update check replaces this fallback with either
    # a current or an "update available" status linking the same changes.
    assert 'id="update-status"' in page, "Update card should include its status target"
    assert (
        "github.com/defangdevs/agent-box/compare/"
        "0000000000000000000000000000000000000000...HEAD" in page
    ), "Update status should fall back to a GitHub changes link"
    assert "Check GitHub for changes" in page, "Update status should have a no-JS fallback"

    # ...and POSTing to /update triggers agent-box-update.service through the
    # daemon's sudo -n systemctl start --no-block. The unit was inactive
    # before; activating it in this offline VM makes it fail (curl can't reach
    # GitHub), which is exactly the observable we want: the trigger worked.
    machine.fail("systemctl is-active --quiet agent-box-update.service")
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-X POST https://box.test/agent/settings/update | grep -x 303"
    )
    machine.wait_until_succeeds(
        "systemctl is-failed --quiet agent-box-update.service", timeout=60
    )
    # ...and it must have failed for the RIGHT reason. "failed" alone cannot
    # tell an offline git fetch from a unit that never reached ExecStart at
    # all, and that gap let a release ship in which the update unit died at
    # 209/STDOUT on every box: StandardOutput= named a file inside a
    # LogsDirectory= that systemd creates only AFTER it opens that file. It
    # left boxes unable to update at all, recoverable only by root, which is
    # the one thing an agent triggering an update cannot call on.
    status = machine.succeed(
        "systemctl show agent-box-update.service "
        "--property=ExecMainStatus --value").strip()
    assert status != "209", (
        "the update unit died at 209/STDOUT - systemd could not open "
        "StandardOutput=, so ExecStart never ran and no box can update")
    # The positive half: the redirection worked and the process did run, so
    # its own account of the failure is on disk where an agent can read it -
    # which is the whole point of the file.
    machine.succeed("test -s /var/log/agent-box-update.log")
    machine.succeed("test 644 = $(stat -c %a /var/log/agent-box-update.log)")

    # The settings page long-polls {base}/status for restart/update progress.
    # It is read-only JSON behind the same auth gate as the page (401 without
    # credentials), reports the running rev and a live session count, and —
    # because selfUpdate is on — an `update` block read straight from the
    # update unit via an unprivileged `systemctl show` (no sudo). Having just
    # driven that unit to `failed`, the endpoint must reflect it: this proves
    # the AGENT_BOX_UPDATE_UNIT/SYSTEMCTL wiring and that the daemon's hardening
    # still permits the world-readable unit-state query.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/status | grep -x 401"
    )
    status = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/settings/status"
    )
    assert (
        '"rev": "0000000000000000000000000000000000000000"' in status
    ), f"status should report the running rev: {status}"
    assert '"sessions"' in status and '"configured"' in status, (
        f"status should report a session count: {status}"
    )
    assert '"update"' in status and '"active": "failed"' in status, (
        f"status should surface the failed update run: {status}"
    )

    # {base}/env and {base}/connect are the two machine-readable READS a
    # portal drives this daemon through (issue #642): Defang Station renders
    # the Environment panel and the connect cards in its own UI and asks here
    # for what is behind them. Both sit inside the same auth block as the page
    # -- which for a route that lists SECRET NAMES is the property worth
    # asserting in a VM, not just in a unit test -- and neither may ever
    # answer with a value. UI_KEEP survived the delete subtest above, so
    # there is a real stored secret to withhold.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/env | grep -x 401"
    )
    env_json = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/settings/env"
    )
    assert '"ok": true' in env_json and '"keys"' in env_json, (
        f"env should list key names: {env_json}"
    )
    assert "UI_KEEP" in env_json, (
        f"env should name the key the page saved: {env_json}"
    )
    assert "stays" not in env_json, (
        f"env must never serve a stored value: {env_json}"
    )
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/connect | grep -x 401"
    )
    cards = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/settings/connect"
    )
    assert '"flows"' in cards and '"id": "claude"' in cards, (
        f"connect should list every card in one answer: {cards}"
    )

    # Maintenance's third row restarts agent-box. A kernel or libc patch
    # installs on disk and only takes effect at a boot, unattended patching
    # deliberately never reboots, and nothing inside the box could - so
    # without this card such an update never finishes at all.
    assert "Restart agent-box" in page, "Maintenance should offer a restart"
    assert 'id="reboot-status"' in page, "Reboot card should have a status target"
    # It stays quiet until the distro says a reboot would apply something.
    # NixOS never writes that marker, so this box says nothing until the
    # test writes one itself.
    assert "A restart is required" not in page, (
        "the reboot card should be silent with no marker file"
    )
    machine.succeed(
        "touch /run/reboot-required; "
        "printf 'linux-image-6.8.0-test\\nlibc6\\n' > /run/reboot-required.pkgs"
    )
    pending = client.succeed(
        f"{curl} -u agent:testpassword https://box.test/agent/settings/"
    )
    assert "A restart is required" in pending, (
        "the card should report the distro's pending-reboot marker"
    )
    assert "linux-image-6.8.0-test" in pending, (
        "the card should name what asked for the reboot"
    )
    machine.succeed("rm -f /run/reboot-required /run/reboot-required.pkgs")

    # The grant behind that button, read back from sudo itself rather than
    # from the file we wrote: sudoers matches on the exact command line, so
    # a rule that differs from AGENT_BOX_REBOOT_CMD by one flag does not
    # error — it asks for a password no agent can answer (issue #353).
    # Listing it is as far as this test goes on purpose: actually running it
    # would reboot the machine the rest of the suite is talking to.
    machine.succeed(
        "su -s /bin/sh agent -c '/run/wrappers/bin/sudo -n -l' > /tmp/sudo-l"
    )
    machine.succeed(
        "grep -F '/run/current-system/sw/bin/systemctl reboot --no-block' "
        "/tmp/sudo-l > /dev/null"
    )
    # mallory (not an agent-box user) has no such grant.
    machine.fail(
        "su -s /bin/sh mallory -c '/run/wrappers/bin/sudo -n "
        "/run/current-system/sw/bin/systemctl reboot --no-block'"
    )

    # mallory (not an agent-box user) must not be able to trigger an update.
    machine.fail(
        "su -s /bin/sh mallory -c '/run/wrappers/bin/sudo -n "
        "/run/current-system/sw/bin/systemctl start --no-block agent-box-update.service'"
    )

    # Issue 91: validation failures must leave the old credential untouched.
    # Include symbols outside the old URL-safe allowlist: password-manager
    # output should work unchanged.
    new_password = "new!test@password#123"
    client.succeed(
        f"{curl} -u agent:testpassword -o /tmp/mismatch -w '%{{http_code}}' "
        "-d 'previous_password=testpassword' "
        f"-d 'new_password={new_password}' -d 'confirm_password=doesnotmatch123' "
        "https://box.test/agent/settings/password | grep -x 400"
    )
    client.succeed("grep -q 'do not match' /tmp/mismatch")
    client.succeed(
        f"{curl} -u agent:testpassword -o /tmp/wrong -w '%{{http_code}}' "
        "-d 'previous_password=wrongpassword' "
        f"-d 'new_password={new_password}' -d 'confirm_password={new_password}' "
        "https://box.test/agent/settings/password | grep -x 403"
    )
    client.succeed("grep -q 'Current password is incorrect' /tmp/wrong")
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/ | grep -x 200"
    )

    # -- portal session handover (issue #541) --------------------------------
    #
    # The daemon is the trust boundary here: caddy has no JWT module in this
    # build, so it forwards the token and the ANSWER decides. These subtests
    # therefore drive the real daemon and the real caddy together, one bent
    # field at a time. They run BEFORE the password change below, so the
    # basic-auth fallback can still be proved with the original password.
    mint = "MINT_KEY=/etc/agent-box-portal/key.pem agent-box-mint"

    def handoff(token_cmd, expect):
        """POST a minted token through caddy and assert the status."""
        token = machine.succeed(token_cmd).strip()
        return client.succeed(
            f"{curl} -sS -o /tmp/hand -D /tmp/handh -w '%{{http_code}}' "
            f"--data-urlencode 'token={token}' "
            f"https://box.test/agent/auth/handoff | grep -x {expect}"
        )

    # The route is served without any box credential at all -- that is the
    # whole point of it, and the one place on this vhost where that is true.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' -X POST "
        "https://box.test/agent/auth/handoff | grep -x 401"
    )
    # ...but only for POST. A token in a query string would be written to
    # caddy's access log, the browser history and any outbound Referer.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "'https://box.test/agent/auth/handoff?token=x' | grep -x 405"
    )

    # The body is BOUNDED before it is read. This is the one route an
    # anonymous caller can reach, and _read_form allocates whatever
    # Content-Length claims, so without a cap the caller sizes the daemon's
    # memory (CodeRabbit on #588).
    client.succeed(
        "head -c 200000 /dev/zero | tr '\\0' 'a' > /tmp/big")
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' -X POST "
        "--data-binary @/tmp/big -H 'Content-Type: application/x-www-form-urlencoded' "
        "https://box.test/agent/auth/handoff | grep -x 413"
    )

    # A real handover: a valid token becomes a box session.
    handoff(f"{mint}", "303")
    client.succeed("grep -qi 'Location: /agent/' /tmp/handh")
    client.succeed(
        "grep -qi 'Set-Cookie: __Host-agent_box_session_agent=' /tmp/handh")
    # 1 day, and SameSite=Lax -- Strict would be withheld on the very
    # navigation the portal's POST leads to, so the user would land back on
    # a basic-auth prompt and the handover would look like it did nothing.
    client.succeed("grep -qi 'Max-Age=86400' /tmp/handh")
    client.succeed("grep -qi 'SameSite=Lax' /tmp/handh")
    client.succeed("grep -qi 'HttpOnly' /tmp/handh")

    # That cookie now reaches the box, through forward_auth, with no password.
    session = client.succeed(
        "sed -n 's/.*__Host-agent_box_session_agent=\\([A-Za-z0-9_-]*\\).*/\\1/p' "
        "/tmp/handh | head -1"
    ).strip()
    assert session, "handover set no session cookie"
    # The daemon runs in HOME mode with one web user, so the vhost root
    # REDIRECTS into that user's space. A 303 already proves the cookie was
    # accepted rather than challenged -- a refusal is the 401 asserted
    # below...
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        f"-H 'Cookie: __Host-agent_box_session_agent={session}' "
        "https://box.test/ | grep -x 303"
    )
    # ...and following it proves the cookie reaches real content, not just
    # the redirect.
    client.succeed(
        f"{curl} -L -o /dev/null -w '%{{http_code}}' "
        f"-H 'Cookie: __Host-agent_box_session_agent={session}' "
        "https://box.test/ | grep -x 200"
    )

    # Single use. The same token again is refused, so a token captured from
    # a log or a Referer buys nothing.
    handoff(f"{mint} '{{\"jti\": \"replay-me\"}}'", "303")
    handoff(f"{mint} '{{\"jti\": \"replay-me\"}}'", "401")

    # Everything the signature must not survive.
    handoff("MINT_KEY=/etc/agent-box-portal/other.pem agent-box-mint",
            "401")
    handoff(f"{mint} '{{\"alg\": \"none\"}}'", "401")
    handoff(f"{mint} '{{\"iss\": \"https://evil.test\"}}'", "401")
    handoff(f"{mint} '{{\"aud\": \"portal-api\"}}'", "401")
    handoff(f"{mint} '{{\"exp\": 1700000000, \"iat\": 1699999999}}'", "401")
    # A token whose own lifetime is over the box's ceiling, however valid
    # its signature: a long-lived handover token is a bearer credential.
    handoff(f"{mint} '{{\"exp\": 2000000000}}'", "401")

    # A VALID signature is not authorization. Station signs EVERY
    # account's tokens with the same key, so a signature alone would mean
    # anybody's token opens anybody's box. What stops that is this box's
    # own declared portalUser: another account's token is genuine, and
    # refused anyway.
    handoff(f"{mint} '{{\"sub\": \"usr_somebody_else\"}}'", "403")
    # This box also declares a portalProject, which NARROWS that account
    # to one project -- so a token for a different project, and a token
    # carrying no project at all, are both refused. A box that declares
    # no project ignores the claim instead; that shape is locked by the
    # portal-route eval check and the native renderer tests rather than
    # by a second VM boot.
    handoff(f"{mint} '{{\"project\": \"someone-elses\"}}'", "403")
    handoff(f"{mint} '{{\"project\": null}}'", "403")

    # The keys came from the portal's own published set, over HTTPS, and are
    # cached on disk -- so a rotation is the portal's business and a restart
    # does not refetch on the one route an anonymous caller can reach.
    machine.succeed(
        "test -s /home/agent/.config/agent-box/web-sessions/jwks/cache.json")
    machine.succeed(
        "grep -q test-key-1 "
        "/home/agent/.config/agent-box/web-sessions/jwks/cache.json")
    # The key set is public and served unauthenticated, because the daemon
    # fetching it holds no credential for this vhost.
    client.succeed(
        f"{curl} -o /dev/null -w '%{{http_code}}' "
        "https://box.test/.well-known/jwks.json | grep -x 200")
    # A `kid` the portal never published is refused. The daemon refetches
    # once on a miss (a just-published key is the ordinary reason for one),
    # and the key still is not there.
    handoff(f"{mint} '{{\"kid\": \"never-published\"}}'", "401")
    # No kid at all still works: the contract makes it advisory, and the box
    # then tries every Ed25519 key in the set.
    handoff(f"{mint} '{{\"kid\": null, \"jti\": \"nokid\"}}'", "303")

    # A forged session cookie is refused -- and the refusal CLEARS it and
    # asks for a password, so an expired session degrades to the normal
    # login instead of wedging the browser on a bare 401 it cannot act on.
    client.succeed(
        f"{curl} -o /dev/null -D /tmp/refused -w '%{{http_code}}' "
        "-H 'Cookie: __Host-agent_box_session_agent=notarealsession00000000' "
        "https://box.test/ | grep -x 401"
    )
    client.succeed(
        "grep -qi 'Set-Cookie: __Host-agent_box_session_agent=;' /tmp/refused")
    client.succeed("grep -qi 'WWW-Authenticate: Basic' /tmp/refused")

    # The session store keeps no live cookie: records are named by the
    # SHA-256 of the secret, so reading the store back yields nothing that
    # can be replayed as one.
    machine.succeed(
        "test -d /home/agent/.config/agent-box/web-sessions/sessions")
    machine.fail(
        f"grep -rq '{session}' /home/agent/.config/agent-box/web-sessions/")
    # Every record, not merely one of them: `grep -qx 600` over a multi-line
    # stat would pass on the first record and never look at the second.
    machine.succeed(
        "stat -c %a /home/agent/.config/agent-box/web-sessions/sessions/*.json "
        "> /tmp/modes"
    )
    machine.succeed("test -s /tmp/modes")
    machine.fail("grep -qvx 600 /tmp/modes")

    # And basic auth is untouched by any of it.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/ | grep -x 200"
    )

    old_hash = machine.succeed("cat /var/lib/agent-box-web/password-hash")
    old_cookie = machine.succeed("cat /var/lib/agent-box-web/cookie-secret-agent")
    # Capture a real cookie authenticated under the old secret; it must stop
    # working even though cookies normally bypass basic auth for WebSockets.
    client.succeed(
        f"{curl} -u agent:testpassword -c /tmp/old-cookie -o /dev/null "
        "https://box.test/agent/settings/"
    )
    client.succeed(
        f"{curl} -b /tmp/old-cookie -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/ | grep -x 200"
    )
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "-d 'previous_password=testpassword' "
        f"-d 'new_password={new_password}' -d 'confirm_password={new_password}' "
        "https://box.test/agent/settings/password | grep -x 303"
    )
    machine.wait_for_unit("caddy.service")
    new_hash = machine.succeed("cat /var/lib/agent-box-web/password-hash")
    assert new_hash != old_hash
    assert new_hash.startswith("$argon2id$"), "new hashes should use Argon2id"
    assert machine.succeed("cat /var/lib/agent-box-web/cookie-secret-agent") != old_cookie
    machine.succeed(
        "stat -c '%U %G %a' /var/lib/agent-box-web/password-hash "
        "| grep -x 'root root 600'"
    )

    # The live Caddy config must accept only the new password; no rebuild or
    # service restart by the test is allowed to paper over a stale env snapshot.
    client.succeed(
        f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/ | grep -x 401"
    )
    client.succeed(
        f"{curl} -b /tmp/old-cookie -o /dev/null -w '%{{http_code}}' "
        "https://box.test/agent/settings/ | grep -x 401"
    )
    client.succeed(
        f"{curl} -u 'agent:{new_password}' -o /tmp/changed -w '%{{http_code}}' "
        "https://box.test/agent/settings/?ok=password_changed | grep -x 200"
    )
    client.succeed("grep -q 'Password changed' /tmp/changed")
  '';
}
