# VM test for the web surface a controlling user reaches: the per-user
# ~/downloads file drop (issue #132), self-serve virtual hosts written into
# ~/sites (issue #40), and the fail2ban jail on the terminal's basic auth.
#
# One VM, one client, three subtests (issue #312). These were three separate
# tests whose node definitions were the same 40 lines three times over — same
# agent-box config, same password-hash activation script, same curl-only client
# — differing only in the Caddyfile they lib.mkForce-swapped in and the
# assertions they then ran. Merging them keeps every assertion, drops two VM
# boots from CI, and gives the web surface one obvious place to grow.
#
# The sandbox has no ACME, so the module-managed Caddyfile is replaced with a
# `tls internal` one that reproduces the three routing shapes the module emits:
# the per-user snippet `import`, the authenticated /<user>/downloads/ handle
# (basic_auth -> strip_prefix -> file_server), and the authenticated catch-all
# standing in for the terminal. The flake's `download-route` and `webhook-route`
# eval checks separately assert the module's REAL Caddyfile emits those blocks;
# what needs a booted VM is whether caddy, fail2ban, tmpfiles and the agent
# unit's namespace agree with each other, which is what this test covers.
#
# Ordering matters: the fail2ban subtest ends with the client banned at the
# firewall, so it runs last. Running the reload-driven self-serve subtest before
# it also means the final "correct password still works" check proves the
# `{$WEB_PASSWORD_HASH_AGENT}` placeholder survives a `systemctl reload
# caddy.service` — the exact sequence a real agent puts a box through, which
# neither of the split tests could see (fail2ban never reloaded, and the
# self-serve Caddyfile carried no placeholder).
{ agent-box }:
{
  name = "agent-box-web-surface";
  node.pkgsReadOnly = false;

  nodes.machine = { pkgs, lib, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    services.agent-box = {
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
    # agent-web-auth-secrets prep unit still feeds these vhosts. Caddyfile
    # globs cap at ONE `*`, so the snippet import stays per-user (as in the
    # module). `log` is what the fail2ban filter reads.
    services.caddy.configFile = lib.mkForce (pkgs.writeText "Caddyfile" ''
      import /var/lib/agent-box-sites/agent/*.caddy

      box.test {
        log
        tls internal
        handle /agent/downloads/* {
          route {
            basic_auth {
              agent {$WEB_PASSWORD_HASH_AGENT}
            }
            uri strip_prefix /agent/downloads
            root * /var/lib/agent-box-downloads/agent
            file_server browse
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

        # Perms: 0750 agent:caddy, same as the ~/sites snippet dir — the user
        # writes, caddy reaches files by its group + their world-read bit. This
        # is why caddy's ProtectHome=true is a non-issue.
        machine.succeed(
            "stat -c '%U:%G %a' /var/lib/agent-box-downloads/agent | grep -x 'agent:caddy 750'"
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
        # Default umask leaves it world-readable, which is what lets caddy read it.
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

    with subtest("an agent adds a vhost by writing ~/sites and reloading caddy"):
        # The tmpfiles-created symlink from ~agent/sites into the caddy-readable dir.
        machine.succeed("test -L /home/agent/sites")
        machine.succeed(
            '[ "$(readlink /home/agent/sites)" = /var/lib/agent-box-sites/agent ]'
        )

        # Perms: 0750 agent:caddy. The user writes; caddy reads by group.
        machine.succeed(
            "stat -c '%U:%G %a' /var/lib/agent-box-sites/agent | grep -x 'agent:caddy 750'"
        )

        # The snippet dir must be writable in the AGENT UNIT's mount namespace, not
        # just to the agent uid. ~/sites resolves to /var/lib/agent-box-sites/agent,
        # outside the ReadWritePaths of ProtectSystem=strict — so the documented
        # flow returned EROFS for every real agent while this test (which used to
        # write as plain `sudo -u agent` from the driver's root namespace) passed.
        machine.succeed(
            "systemctl show agent-box@agent --property=ReadWritePaths --value "
            "| grep /var/lib/agent-box-sites/agent >/dev/null"
        )

        # The agent writes a new vhost snippet through the ~/sites symlink — never
        # touches /var/lib directly. `tls internal` sidesteps ACME in the sandbox.
        # nsenter joins the running unit's mount namespace so the write is subject
        # to the same read-only remount a tool shell inside the session gets;
        # runuser then drops to the agent uid for the ownership check below.
        machine.succeed(
            f"{in_session} "
            "tee /home/agent/sites/mysite.caddy > /dev/null <<'CFG'\n"
            "mysite.test {\n"
            "  tls internal\n"
            "  respond \"hello from mysite\" 200\n"
            "}\n"
            "CFG"
        )

        # File landed inside the caddy-readable dir (symlink target), owned by agent.
        machine.succeed(
            "stat -c '%U' /var/lib/agent-box-sites/agent/mysite.caddy | grep -x agent"
        )

        # /run/wrappers must be on the agent unit's PATH — it holds the setuid
        # sudo wrapper, without which shells started by the agent CLI can't
        # invoke sudo even though the sudoers rule permits the command.
        machine.succeed(
            "systemctl show agent-box@agent --property=Environment "
            "| grep '/run/wrappers/bin' >/dev/null"
        )

        # Reload caddy via the sudo rule (NOPASSWD).
        machine.succeed(
            "sudo -u agent -H bash -lc "
            "'sudo -n systemctl reload caddy.service'"
        )
        machine.wait_until_succeeds("systemctl is-active caddy.service", timeout=20)

        # New vhost actually serves.
        site = f"curl -sk --resolve mysite.test:443:{machine_ip}"
        client.wait_until_succeeds(
            f"{site} https://mysite.test/ | grep 'hello from mysite' >/dev/null",
            timeout=30,
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
