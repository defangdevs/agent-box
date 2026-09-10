# VM test for the web surface a controlling user reaches: the per-user
# ~/downloads file drop (issue #132), operator-declared virtual hosts serving
# an agent's own files out of ~/sites (issues #40, #629), and the fail2ban
# jail on the terminal's basic auth.
#
# One VM, one client, three subtests (issue #312). These were three separate
# tests whose node definitions were the same 40 lines three times over — same
# agent-box config, same password-hash activation script, same curl-only client
# — differing only in the Caddyfile they lib.mkForce-swapped in and the
# assertions they then ran. Merging them keeps every assertion, drops two VM
# boots from CI, and gives the web surface one obvious place to grow.
#
# The sandbox has no ACME, so the module-managed Caddyfile is replaced with a
# `tls internal` one that reproduces the routing shapes the module emits: an
# operator-declared web.sites vhost, the authenticated /<user>/downloads/
# handle (basic_auth -> reverse_proxy to that user's settings daemon, which is
# what serves the drop since issue #630, plus the issue #631 attachment and
# sandbox headers), and the authenticated catch-all standing in for the
# terminal. That the REAL Caddyfile emits those blocks — and, since issue
# #629, that it imports nothing an agent can write — is asserted where it
# belongs, in the `download-route`, `webhook-route` and `site-route` eval
# checks; what needs a booted VM is whether caddy, fail2ban, tmpfiles and the
# agent unit's namespace agree with each other, which is what this test covers.
#
# Ordering matters: the fail2ban subtest ends with the client banned at the
# firewall, so it runs last. Running the reload-driven ~/sites subtest before
# it also means the final "correct password still works" check proves the
# `{$WEB_PASSWORD_HASH_AGENT}` placeholder survives a `systemctl reload
# caddy.service` — the exact sequence a real box goes through, which neither of
# the split tests could see (fail2ban never reloaded, and the self-serve
# Caddyfile carried no placeholder).
{ agent-box }:
{
  name = "agent-box-web-surface";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, lib, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    services.agent-box = {
      sessionLimit = 64;
      enable = true;
      agent = "claude";
      users.agent = {
        web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
      };
      web = {
        enable = true;
        domain = "box.test";
        user = "agent";
      };
    };
    system.stateVersion = "25.05";

    # curl on the SERVER too, not only on the client: the ~/sites subtest
    # asks caddy's admin socket what is actually loaded, which is a
    # unix-socket request only root can make. python3 is what the agent
    # serves its own site with, started as a transient unit in the script
    # below rather than declared here — `phantom-unit-overrides` (issue
    # #362) scans these files for systemd.services.<name> and cannot tell
    # a test's own new unit from a drop-in naming one the module never
    # renders.
    environment.systemPackages = [ pkgs.curl pkgs.python3 ];

    # Materialize the password hash (subshell so the umask doesn't leak).
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

    # Same $WEB_PASSWORD_HASH_AGENT placeholder the module wires up, so the
    # agent-web-auth-secrets prep unit still feeds these vhosts. `log` is what
    # the fail2ban filter reads.
    #
    # mysite.test is the shape web.sites renders (issue #629): a vhost the
    # BOX's configuration declares, reverse-proxying to a port the AGENT
    # listens on. A reverse proxy is the only shape there is — caddy serves
    # no files here, because a `root` over an agent-writable directory is
    # the symlink escape issue #630 already took out of the file drop
    # ("a site root is not a filesystem sandbox", caddyfile-terminal.caddy).
    #
    # There is deliberately no `import /var/lib/agent-box-sites/agent/*.caddy`
    # here any more — that import is what let an agent write a site block
    # into the instance holding every user's WEB_COOKIE_SECRET_*, and the
    # subtest below proves a snippet left in ~/sites reaches nothing.
    services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
      {
        admin unix//run/caddy/admin.sock
      }

      mysite.test {
        log
        tls internal
        reverse_proxy 127.0.0.1:3000
      }

      box.test {
        log
        tls internal
        header {
          Cache-Control "no-store"
          X-Content-Type-Options "nosniff"
          Content-Security-Policy "frame-ancestors 'self'"
        }
        @dl_file_agent {
          path /agent/downloads/*
          not path */
        }
        header @dl_file_agent {
          Content-Disposition "attachment"
          Content-Security-Policy "sandbox; frame-ancestors 'none'"
          defer
        }
        handle /agent/downloads/* {
          route {
            basic_auth {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            reverse_proxy unix//run/agent-box-settings/agent.sock
          }
        }
        handle {
          route {
            basic_auth {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            respond "ok" 200
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
    machine.wait_for_unit("fail2ban.service")
    machine.wait_for_unit("agent-box@agent.service")
    # The drop is the settings daemon's now (issue #630), reached over this
    # socket -- socket-activated, so caddy's first request starts the service.
    machine.wait_for_unit("agent-box-settings@agent.socket")
    client.wait_for_unit("multi-user.target")

    machine_ip = machine.succeed("ip -4 -o addr show eth1 | head -1").split()[3].split("/")[0]
    client_ip = client.succeed("ip -4 -o addr show eth1 | head -1").split()[3].split("/")[0]
    curl = f"curl -sk --resolve box.test:443:{machine_ip}"

    # Writes that the guide tells an agent to make (~/downloads, ~/sites) must be
    # exercised INSIDE the agent unit's mount namespace. Writing as the agent uid
    # from the driver's root shell skips ProtectSystem entirely, which is how a
    # read-only ~/downloads shipped under a green test (issue #316).
    agent_pid = machine.succeed(
        "systemctl show -p MainPID --value agent-box@agent.service"
    ).strip()
    assert agent_pid not in ("", "0"), "agent unit has no main PID"
    in_session = f"nsenter -t {agent_pid} -m -- runuser -u agent --"

    with subtest("~/downloads is a per-user file drop served behind the auth gate"):
        # The tmpfiles-created symlink from ~agent/downloads into the backing dir.
        machine.succeed("test -L /home/agent/downloads")
        machine.succeed(
            '[ "$(readlink /home/agent/downloads)" = /var/lib/agent-box-downloads/agent ]'
        )

        # Perms: 0700, where the ~/sites snippet dir beside it is 0750. The
        # drop was caddy-readable until issue #630 moved the route to this
        # user's own settings daemon; nothing behind the web server opens
        # files under here any more, so the group bits came off and a caddy
        # compromise no longer reads every user's drop. The group is still
        # `caddy` and grants nothing at 0700 — both backends emit this rule
        # identically and neither can portably name a per-user group, so the
        # MODE is the boundary (issues #604, #630).
        machine.succeed(
            "stat -c '%U:%G %a' /var/lib/agent-box-downloads/agent | grep -x 'agent:caddy 700'"
        )

        # ~/downloads resolves to /var/lib/agent-box-downloads/agent, outside
        # /home — so ProtectSystem=strict denies it with EROFS unless the target
        # is named in ReadWritePaths, exactly as for ~/sites below (issue #316).
        machine.succeed(
            "systemctl show agent-box@agent --property=ReadWritePaths --value "
            "| grep /var/lib/agent-box-downloads/agent >/dev/null"
        )

        # The agent drops a file through the ~/downloads symlink (never touches
        # /var/lib directly), exactly as AGENTS.md instructs — and from inside
        # the unit's namespace, which is the only place that proves it.
        machine.succeed(
            f"{in_session} tee /home/agent/downloads/report.txt > /dev/null <<'EOF'\n"
            "hello from the box\n"
            "EOF"
        )
        # Default umask still leaves it 0644, and that no longer matters to
        # anyone: the 0700 directory above it is what decides who gets in,
        # and the only reader is the daemon running as this same user.
        machine.succeed(
            "stat -c '%U %a' /var/lib/agent-box-downloads/agent/report.txt | grep -x 'agent 644'"
        )

        # A credential-less request is refused (401) — nothing is served anonymously.
        client.succeed(
            f"{curl} -o /dev/null -w '%{{http_code}}' https://box.test/agent/downloads/report.txt | grep -x 401"
        )

        # With the right password the file downloads intact.
        client.wait_until_succeeds(
            f"{curl} -u agent:testpassword https://box.test/agent/downloads/report.txt | grep 'hello from the box' >/dev/null",
            timeout=30,
        )

        # The bare directory is a browsable index listing the dropped file.
        # Capture the (multi-KB) listing to a file before grepping: piping a large
        # body into `grep -q` makes grep close the pipe on first match, and the
        # resulting curl write-error (exit 23) trips the driver's pipefail even
        # though the match succeeded. Same reason every piped grep in tests/ drops
        # `-q` for `>/dev/null` — see AGENTS.md, Testing Guidelines.
        client.succeed(
            f"{curl} -u agent:testpassword https://box.test/agent/downloads/ -o /tmp/index.html"
        )
        client.succeed("grep -q report.txt /tmp/index.html")

        # Issue #630, end to end, in two layers that hold independently.
        #
        # Layer one is the RESOLVER, and this case isolates it: the target is
        # a file the daemon's own user wrote and can read perfectly well, in
        # its home, outside the drop. Nothing about permissions refuses this
        # request — only the confinement does.
        machine.succeed(
            f"{in_session} tee /home/agent/private-note.txt > /dev/null <<'EOF'\n"
            "agent-home-marker\n"
            "EOF"
        )
        machine.succeed(
            f"{in_session} cat /home/agent/private-note.txt "
            "| grep agent-home-marker >/dev/null"
        )
        machine.succeed(
            f"{in_session} ln -sfn /home/agent/private-note.txt "
            "/home/agent/downloads/note.txt"
        )
        client.succeed(
            f"{curl} -u agent:testpassword -o /tmp/note.html "
            "-w '%{http_code}' "
            "https://box.test/agent/downloads/note.txt | grep -x 404"
        )
        client.fail("grep -q agent-home-marker /tmp/note.html")

        # An absolute link to a system file the agent can also read is
        # refused the same way, for the same one reason.
        machine.succeed(
            f"{in_session} ln -sfn /etc/hostname "
            "/home/agent/downloads/host.txt"
        )
        client.succeed(
            f"{curl} -u agent:testpassword -o /dev/null -w '%{{http_code}}' "
            "https://box.test/agent/downloads/host.txt | grep -x 404"
        )

        # Layer two is the MODE. A synthetic sibling drop — this VM has one
        # terminal user — created exactly as the tmpfiles rule creates a real
        # one. Before #630's follow-up it was 0750 <user>:caddy, and caddy
        # could read it: that shared identity is what made the symlink escape
        # reach another user's files at all. At 0700 neither caddy nor this
        # agent can open it, whatever any route asks for.
        machine.succeed(
            "install -d -o root -g caddy -m 0700 "
            "/var/lib/agent-box-downloads/bob"
        )
        machine.succeed(
            "install -m 0644 /dev/stdin "
            "/var/lib/agent-box-downloads/bob/report.txt "
            "<<'EOF'\nbob-private-marker\nEOF"
        )
        machine.fail(
            "runuser -u caddy -- cat "
            "/var/lib/agent-box-downloads/bob/report.txt"
        )
        machine.fail(
            f"{in_session} cat /var/lib/agent-box-downloads/bob/report.txt"
        )
        # And the link to it is still refused by the resolver, which is the
        # layer that would hold even if the mode were loosened again. Before
        # #630 this request answered 200 with bob's file in it.
        machine.succeed(
            f"{in_session} ln -sfn /var/lib/agent-box-downloads/bob/report.txt "
            "/home/agent/downloads/sibling.txt"
        )
        client.succeed(
            f"{curl} -u agent:testpassword -o /tmp/sibling.html "
            "-w '%{http_code}' "
            "https://box.test/agent/downloads/sibling.txt | grep -x 404"
        )
        client.fail("grep -q bob-private-marker /tmp/sibling.html")

        # A link that stays INSIDE the drop still resolves: the rule is
        # confinement, not a ban on symlinks, so `ln -s` next to a file the
        # agent already dropped keeps working.
        machine.succeed(
            f"{in_session} ln -sfn report.txt "
            "/home/agent/downloads/latest.txt"
        )
        client.succeed(
            f"{curl} -u agent:testpassword "
            "https://box.test/agent/downloads/latest.txt "
            "| grep 'hello from the box' >/dev/null"
        )

    with subtest("a hostile artifact is handed over as an inert attachment"):
        # Issue #631: ~/downloads is served from the SAME origin as the
        # settings page and the terminals, so an artifact rendered INLINE is
        # same-origin privileged JavaScript running with the operator's
        # ambient auth -- HttpOnly stops a script reading the auth cookie but
        # not sending it. The route has to hand every file to the browser as
        # a download instead. (What only a browser can settle -- that Chromium
        # then really refuses to execute it -- is
        # tests/e2e/download-isolation.spec.ts.)
        machine.succeed(
            f"{in_session} tee /home/agent/downloads/evil.html > /dev/null <<'EOF'\n"
            "<script>fetch('/agent/settings')</script>\n"
            "EOF"
        )
        # An index.html in the drop directory must NOT stand in for the
        # listing: that path is the one the attachment matcher exempts, so
        # serving it would put attacker HTML back on this origin inline.
        # Since issue #630 the listing is the daemon's own generated page
        # and nothing looks for an index.html at all -- this asserts that
        # rather than the `index off` it used to need.
        machine.succeed(
            f"{in_session} tee /home/agent/downloads/index.html > /dev/null <<'EOF'\n"
            "<h1>ATTACKER INDEX</h1>\n"
            "EOF"
        )
        client.wait_until_succeeds(
            f"{curl} -u agent:testpassword -D /tmp/evil.head -o /dev/null "
            "https://box.test/agent/downloads/evil.html",
            timeout=30,
        )
        client.succeed("grep -i '^content-disposition: attachment' /tmp/evil.head >/dev/null")
        client.succeed(
            "grep -i \"^content-security-policy: sandbox; frame-ancestors 'none'\" "
            "/tmp/evil.head >/dev/null"
        )
        client.succeed("grep -i '^x-content-type-options: nosniff' /tmp/evil.head >/dev/null")

        # The listing itself stays a listing: no attachment disposition (it
        # would download instead of render), and it is the daemon's own page,
        # not the index.html sitting next to it.
        client.succeed(
            f"{curl} -u agent:testpassword -D /tmp/list.head "
            "https://box.test/agent/downloads/ -o /tmp/list.html"
        )
        client.fail("grep -i '^content-disposition' /tmp/list.head >/dev/null")
        client.fail("grep -F 'ATTACKER INDEX' /tmp/list.html >/dev/null")
        client.succeed("grep -F evil.html /tmp/list.html >/dev/null")

        # The exempt shape is the LISTING and nothing else. A file asked for
        # with a trailing slash matches `not path */` too, so if the drop
        # served it there it would arrive without these headers -- the
        # daemon answers 404 instead (issues #630, #631).
        client.succeed(
            f"{curl} -u agent:testpassword -o /tmp/slashed.html "
            "-w '%{http_code}' "
            "https://box.test/agent/downloads/evil.html/ | grep -x 404"
        )
        client.fail("grep -F 'fetch(' /tmp/slashed.html >/dev/null")

        # And a management response may be framed only by this box itself --
        # the workspace iframes each session's terminal from this very origin.
        client.succeed(
            f"{curl} -u agent:testpassword -D /tmp/mgmt.head -o /dev/null https://box.test/"
        )
        client.succeed(
            "grep -i \"^content-security-policy: frame-ancestors 'self'\" "
            "/tmp/mgmt.head >/dev/null"
        )

    with subtest("~/sites holds an operator-declared site's files, not its config"):
        # Issue #629. ~/sites used to be an extension point for
        # CONFIGURATION: a *.caddy snippet there was imported into the front
        # door, which is the one Caddy instance holding every user's
        # WEB_PASSWORD_HASH_* and WEB_COOKIE_SECRET_* and able to reach every
        # user's settings socket. So a snippet could print a SIBLING's cookie
        # secret through Caddy's own `{$ENV}` substitution -- and since the
        # normal routes admit an exact cookie match, that value is a session
        # -- or reverse_proxy straight to a sibling's settings socket with no
        # auth gate.
        #
        # What is left is the useful half: the directory is still the
        # agent's to write, and an operator declares a hostname that
        # reaches the server the agent runs over those files (web.sites,
        # standing in above as mysite.test -> 127.0.0.1:3000). caddy opens
        # none of them itself.

        # The tmpfiles-created symlink from ~agent/sites into the caddy-readable dir.
        machine.succeed("test -L /home/agent/sites")
        machine.succeed(
            '[ "$(readlink /home/agent/sites)" = /var/lib/agent-box-sites/agent ]'
        )

        # Perms: 0750 agent:caddy. The user writes; the `caddy` group is
        # vestigial since the site became a proxy, and the mode is what
        # keeps other agent users out.
        machine.succeed(
            "stat -c '%U:%G %a' /var/lib/agent-box-sites/agent | grep -x 'agent:caddy 750'"
        )

        # The dir must be writable in the AGENT UNIT's mount namespace, not
        # just to the agent uid. ~/sites resolves to /var/lib/agent-box-sites/agent,
        # outside the ReadWritePaths of ProtectSystem=strict — so the documented
        # flow returned EROFS for every real agent while this test (which used to
        # write as plain `sudo -u agent` from the driver's root namespace) passed.
        machine.succeed(
            "systemctl show agent-box@agent --property=ReadWritePaths --value "
            "| grep /var/lib/agent-box-sites/agent >/dev/null"
        )

        # The agent writes its app's CONTENT through the ~/sites symlink —
        # never touches /var/lib directly — and its own server on
        # 127.0.0.1:3000 is what reads it back. nsenter joins the running
        # unit's mount namespace so the write is subject to the same
        # read-only remount a tool shell inside the session gets; runuser
        # then drops to the agent uid for the ownership check below.
        machine.succeed(f"{in_session} mkdir -p /home/agent/sites/public")
        machine.succeed(
            f"{in_session} "
            "tee /home/agent/sites/public/index.html > /dev/null <<'HTML'\n"
            "hello from mysite\n"
            "HTML"
        )
        machine.succeed(
            "stat -c '%U' /var/lib/agent-box-sites/agent/public/index.html "
            "| grep -x agent"
        )

        # ...and serves it ITSELF, as itself, on loopback. This is the whole
        # replacement for the old `root` + `file_server`: caddy proxies to
        # this instead of opening the directory, so a symlink left in here
        # is resolved by a process that is already the agent and reaches
        # nothing the agent could not read anyway. A transient unit, so the
        # server is supervised without this test declaring one.
        machine.succeed(
            "systemd-run --unit=agent-static-site --uid=agent "
            "--collect python3 -m http.server 3000 --bind 127.0.0.1 "
            "--directory /var/lib/agent-box-sites/agent/public"
        )
        machine.wait_for_open_port(3000, addr="127.0.0.1")

        # /run/wrappers must be on the agent unit's PATH — it holds the setuid
        # sudo wrapper, without which shells started by the agent CLI can't
        # invoke sudo even though the sudoers rule permits the command.
        machine.succeed(
            "systemctl show agent-box@agent --property=Environment "
            "| grep '/run/wrappers/bin' >/dev/null"
        )

        # No caddy reload grant (issue #629). This configuration sets no
        # sudoAllowlist, and web.enable no longer implies one, so the command
        # the guide used to hand every agent is now refused. `sudo -n` fails
        # rather than prompting, which is what makes this assertable at all.
        machine.fail(
            "sudo -u agent -H bash -lc "
            "'sudo -n systemctl reload caddy.service'"
        )

        # The reload that DOES happen is an operator's, as root. Kept here
        # because the fail2ban subtest below depends on having reloaded once:
        # it is what proves {$WEB_PASSWORD_HASH_AGENT} survives a reload.
        machine.succeed("systemctl reload caddy.service")
        machine.wait_until_succeeds("systemctl is-active caddy.service", timeout=20)

        # The declared vhost serves the file the agent wrote.
        site = f"curl -sk --resolve mysite.test:443:{machine_ip}"
        client.wait_until_succeeds(
            f"{site} https://mysite.test/ | grep 'hello from mysite' >/dev/null",
            timeout=30,
        )

    with subtest("a *.caddy snippet left in ~/sites reaches nothing"):
        # The attack the old import allowed, run for real. The secret is
        # genuinely in caddy's environment -- assert that first, or the
        # negative below proves nothing -- and the snippet asks Caddy to
        # print it on a vhost of the agent's own choosing.
        secret = machine.succeed(
            "grep -o 'WEB_COOKIE_SECRET_AGENT=.*' /run/agent-box-web/env "
            "| cut -d= -f2"
        ).strip()
        assert len(secret) > 8, f"no cookie secret to exfiltrate: {secret!r}"

        machine.succeed(
            f"{in_session} "
            "tee /home/agent/sites/evil.caddy > /dev/null <<'CFG'\n"
            "evil.test {\n"
            "  tls internal\n"
            "  respond \"{$WEB_COOKIE_SECRET_AGENT}\" 200\n"
            "}\n"
            "CFG"
        )
        # It landed -- the write is not what is blocked here, the READING of
        # it as configuration is.
        machine.succeed("test -s /var/lib/agent-box-sites/agent/evil.caddy")

        # An operator's reload does not pick it up, because nothing imports
        # it. caddy stays up (a snippet that WAS imported and was malformed
        # would fail the reload instead).
        machine.succeed("systemctl reload caddy.service")
        machine.wait_until_succeeds("systemctl is-active caddy.service", timeout=20)

        # No such vhost exists, so nothing answers for it. Whatever comes
        # back -- a TLS failure, a 404, the default vhost -- must not
        # contain the secret. From the CLIENT, which is where a visitor the
        # agent pointed at its hostname would be, and the node that has
        # curl.
        evil = client.succeed(
            f"curl -sk --max-time 10 --resolve evil.test:443:{machine_ip} "
            "https://evil.test/ || true"
        )
        assert secret not in evil, (
            "a ~/sites snippet exfiltrated the cookie secret"
        )
        # And the vhost the agent tried to declare is not in the running
        # config at all. Asked through the admin socket, which only root can
        # reach (see the subtest below). `caddy adapt` is not the question
        # here -- what is loaded is.
        cfg = machine.succeed(
            "curl -s --unix-socket /run/caddy/admin.sock "
            "http://localhost/config/ || true"
        )
        assert "evil.test" not in cfg, "the snippet reached the live config"
        # The site the OPERATOR declared is unaffected by any of this.
        client.wait_until_succeeds(
            f"curl -sk --resolve mysite.test:443:{machine_ip} "
            "https://mysite.test/ | grep 'hello from mysite' >/dev/null",
            timeout=30,
        )

    with subtest("the caddy admin API is off TCP and on a root-only socket"):
        # Issue #605. Caddy's default admin endpoint is 127.0.0.1:2019 and it
        # takes NO credentials, so any process on the box -- every agent
        # session included -- could read the live config, replace it, or stop
        # the server. On 2026-09-03 a bare `caddy stop` meant for a local
        # preview found this endpoint instead and took the box's front door
        # down for 47 minutes, losing about 44 webhook deliveries.
        #
        # The reloads in the subtests above are the other half of this and
        # have already run: they went through `systemctl reload
        # caddy.service`, whose ExecReload passes no --address, and the
        # declared vhost served afterwards. So the endpoint moving to a
        # socket did not cost the reload path an operator's `apply` needs.
        machine.succeed("test -S /run/caddy/admin.sock")
        machine.succeed("stat -c '%U' /run/caddy/admin.sock | grep -x caddy")
        # No group or other bits: root and caddy, nobody else.
        machine.succeed(
            "stat -c '%a' /run/caddy/admin.sock | grep -Ex '[0-7]?[0-7]00'"
        )
        machine.fail("sudo -u agent test -w /run/caddy/admin.sock")
        # And nothing is listening on the old TCP port at all. /dev/tcp rather
        # than curl or ss, so this asserts nothing about what the image ships.
        machine.fail("timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/2019'")

    with subtest("caddy comes back from a clean stop on its own"):
        # The second half of #605. A stop through the admin API is a CLEAN
        # exit, so Restart=on-failure saw success and left the front door
        # dead -- and the agent cannot start it again, because the sudo
        # allowlist has `reload`, not `start`. The only way back was a reboot,
        # which destroys every session on the box. Restart=always is what
        # makes that recovery need no privilege and no human.
        machine.succeed("systemctl show -p Restart caddy.service "
                        "| grep -x 'Restart=always'")
        # Drive it for real: a clean stop of the MAIN process, not
        # `systemctl stop` (which would tell systemd the unit is meant to be
        # down and is not what happened here).
        main = machine.succeed(
            "systemctl show -p MainPID --value caddy.service").strip()
        machine.succeed(f"kill -TERM {main}")
        machine.wait_until_succeeds(
            f"test \"$(systemctl show -p MainPID --value caddy.service)\" "
            f"!= {main}", timeout=60)
        machine.wait_for_unit("caddy.service")
        # Serving again, with no operator action of any kind.
        client.wait_until_succeeds(
            f"curl -sk --resolve mysite.test:443:{machine_ip} "
            "https://mysite.test/ | grep 'hello from mysite' >/dev/null",
            timeout=60,
        )

    with subtest("repeated basic-auth failures get the client banned"):
        # Correct password works — after the reload above, so this also proves
        # the reload re-expanded $WEB_PASSWORD_HASH_AGENT — and doesn't score
        # against the jail.
        client.wait_until_succeeds(
            f"{curl} -u agent:testpassword https://box.test/ | grep ok >/dev/null",
            timeout=30,
        )

        # For the record: nothing so far has scored, including the
        # credential-less 401 the downloads subtest took (what a browser gets
        # before it shows the password prompt). Only a SUPPLIED wrong credential
        # counts, which is what the loop below spends.
        print(machine.succeed("fail2ban-client status agent-web-auth"))

        # Five wrong-password attempts trip maxretry
        for i in range(5):
            client.succeed(f"{curl} -o /dev/null -u agent:wrong{i} https://box.test/")

        machine.wait_until_succeeds(
            f"fail2ban-client status agent-web-auth | grep '{client_ip}' >/dev/null",
            timeout=60,
        )

        # Banned: connection no longer completes. Retry-until-refused rather than
        # a single fail(): the status listing above appears BEFORE fail2ban's ban
        # action has inserted the firewall rule, so one immediate curl can still
        # slip through that gap (seen under CI load in PR #152).
        client.wait_until_fails(
            f"{curl} -m 5 -o /dev/null -u agent:testpassword https://box.test/",
            timeout=60,
        )

        print(machine.succeed("fail2ban-client status agent-web-auth"))
  '';
}
