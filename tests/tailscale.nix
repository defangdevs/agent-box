# VM test for the settings page's Tailscale card (PR #1069): the root
# kernel-mode tailscaled the box owner starts through a polkit grant, joined
# to a self-hosted Headscale, and the Caddy site for the box's tailnet name.
#
# Three guests: headscale (the coordination server, TLS from a test CA every
# node trusts), box (agent-box, `agent` is the root web user) and client (a
# plain tailscale node). The VM has no internet, so the box carries the
# pinned tailscale path in its store and `install-client` realises it locally.
#
# What this covers that the CLI tests with fakes cannot: the polkit grant,
# `set --operator` before login, `up --reset --operator=<self>` run by the
# owner, Caddy serving the tailnet name, that a loopback-only service stays
# private (the reason for kernel mode), Disconnect wiping the node state, and
# the unit coming back after a reboot while the marker is there.
{ agent-box }:
{ pkgs, lib, ... }:
let
  # One self-signed certificate, which is also the CA every node trusts.
  tls = pkgs.runCommand "headscale-test-tls" { nativeBuildInputs = [ pkgs.openssl ]; } ''
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -out cert.pem -keyout key.pem \
      -subj '/CN=headscale' -addext "subjectAltName=DNS:headscale"
    install -d $out
    cp cert.pem key.pem $out
  '';
  headscalePort = 8080;
  stunPort = 3478;
  baseDomain = "tail.test";
in
{
  name = "agent-box-tailscale";
  node.pkgsReadOnly = false;

  nodes.headscale = {
    virtualisation.memorySize = 1024;
    security.pki.certificateFiles = [ "${tls}/cert.pem" ];
    services.headscale = {
      enable = true;
      port = headscalePort;
      settings = {
        server_url = "https://headscale";
        prefixes.v4 = "100.64.0.0/10";
        derp = {
          server = {
            enabled = true;
            region_id = 999;
            stun_listen_addr = "0.0.0.0:${toString stunPort}";
          };
          urls = [ ];
        };
        dns = {
          magic_dns = true;
          base_domain = baseDomain;
          override_local_dns = false;
        };
      };
    };
    services.nginx = {
      enable = true;
      virtualHosts.headscale = {
        addSSL = true;
        sslCertificate = "${tls}/cert.pem";
        sslCertificateKey = "${tls}/key.pem";
        locations."/" = {
          proxyPass = "http://127.0.0.1:${toString headscalePort}";
          proxyWebsockets = true;
        };
      };
    };
    networking.firewall = {
      allowedTCPPorts = [ 80 443 9997 ];
      allowedUDPPorts = [ stunPort ];
    };
    environment.systemPackages = [ pkgs.headscale ];
  };

  nodes.box = { pkgs, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    # The unit's install-client realises this exact path (the module embeds
    # it without a dependency); with no binary cache, it must already be here.
    virtualisation.additionalPaths = [ pkgs.tailscale ];
    security.pki.certificateFiles = [ "${tls}/cert.pem" ];
    services.agent-box = {
      sessionLimit = 64;
      enable = true;
      agent = "claude";
      users.agent.web.passwordHashFile = "/var/lib/agent-box-web/password-hash";
      web = {
        enable = true;
        domain = "box.test";
        user = "agent";
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
  };

  nodes.client = {
    virtualisation.memorySize = 1024;
    security.pki.certificateFiles = [ "${tls}/cert.pem" ];
    services.tailscale.enable = true;
    environment.systemPackages = [ pkgs.curl ];
  };

  testScript = ''
    import json
    import shlex

    MARKER = "/home/agent/.local/state/agent-box-tailscale/enabled"
    SITE = "/home/agent/sites/agent-box-tailnet.caddy"
    UNIT = "agent-box-tailscale.service"


    def as_agent(command):
        """Run as the box owner inside the agent unit's mount namespace,
        with that unit's PATH: what the settings daemon's call amounts to."""
        pid = box.succeed(
            "systemctl show -p MainPID --value agent-box@agent.service").strip()
        path = box.succeed(
            "systemctl show -p Environment --value agent-box@agent.service"
            " | tr ' ' '\\n' | sed -n 's/^PATH=//p'").strip()
        return (f"nsenter -t {pid} -m -- runuser -u agent -- env HOME=/home/agent"
                f" USER=agent LOGNAME=agent PATH={shlex.quote(path)}"
                f" bash -c {shlex.quote(command)}")


    def caddy_reloads():
        return int(box.succeed(
            "journalctl -u caddy.service --no-pager -o cat"
            " | grep -c '^Reloaded ' || true").strip())


    # The client install-client realised: the plain CLI, not on the box's PATH.
    TS = ("$(readlink /nix/var/nix/gcroots/agent-box-tailscale)/bin/tailscale"
          " --socket /run/tailscale/tailscaled.sock")


    def box_status():
        return json.loads(box.succeed(f"{TS} status --json --peers=false"))


    def connect():
        before = caddy_reloads()
        box.succeed(as_agent(
            f"echo {shlex.quote(authkey)} | agent-box-tailscale configure"
            " --login-server https://headscale --auth-key-stdin"))
        box.succeed(as_agent("agent-box-tailscale connect >&2"))
        return before


    start_all()
    headscale.wait_for_unit("headscale.service")
    headscale.wait_for_open_port(443)
    headscale.succeed("headscale users create test")
    authkey = headscale.succeed(
        "headscale preauthkeys -u 1 create --reusable").strip().splitlines()[-1]

    client.wait_for_unit("tailscaled.service")
    client.succeed(
        f"tailscale up --login-server https://headscale --auth-key {authkey}")

    box.wait_for_unit("multi-user.target")
    box.wait_for_unit("agent-box@agent.service")
    box.wait_for_unit("caddy.service")

    with subtest("a box nobody connected never starts tailscaled"):
        box.succeed(f"test ! -e {MARKER}")
        box.fail(f"systemctl is-active {UNIT}")
        box.succeed("test ! -e /nix/var/nix/gcroots/agent-box-tailscale")

    with subtest("1. connect as agent: unit, Running, operator, site, reload"):
        before = connect()
        box.succeed(f"systemctl is-active {UNIT}")
        status = box_status()
        assert status["BackendState"] == "Running", status["BackendState"]
        prefs = json.loads(box.succeed(f"{TS} debug prefs"))
        assert prefs.get("OperatorUser") == "agent", prefs.get("OperatorUser")
        box.succeed(f"test -f {SITE}")
        site = box.succeed(f"cat {SITE}")
        dns = status["Self"]["DNSName"].rstrip(".")
        assert dns == "box.${baseDomain}", dns
        assert f"https://{dns} {{" in site and "tls internal" in site, site
        after = caddy_reloads()
        assert after > before, f"caddy reloads {before} -> {after}"
        card = json.loads(box.succeed(as_agent("agent-box-tailscale status")))
        assert card["enabled"] and card["backendState"] == "Running", card
        assert card["url"] == f"https://{dns}/agent/", card
        # The owner's key file is gone once the node joined.
        box.succeed("test ! -e /home/agent/.local/state/agent-box-tailscale/authkey")
        box.succeed("test -L /nix/var/nix/gcroots/agent-box-tailscale")

    box_ip = status["TailscaleIPs"][0]
    ca = box.succeed(
        "cat $(find /var/lib/caddy -path '*pki/authorities/local/root.crt' | head -1)")
    client.succeed(f"cat > /tmp/caddy-root.crt <<'EOF'\n{ca}EOF")
    curl = (f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 10"
            f" --cacert /tmp/caddy-root.crt --resolve {dns}:443:{box_ip}"
            f" https://{dns}/agent/")

    with subtest("2. client reaches the box's pages over the tailnet"):
        client.wait_until_succeeds(f"tailscale ping -c 1 {box_ip}", timeout=120)
        code = client.wait_until_succeeds(f"{curl} | grep -x 401", timeout=60)
        # Signed in, /agent/ answers (a 303 on to the settings page while the
        # box has no session); follow it to the page itself.
        code = client.succeed(f"{curl} -L -u agent:testpassword").strip()
        assert code == "200", code

    with subtest("3. a loopback-only service on the box stays private"):
        box.succeed("systemd-run --unit loopback-9999 -p DynamicUser=yes"
                    " ${pkgs.python3}/bin/python3 -m http.server 9999 --bind 127.0.0.1")
        box.wait_for_open_port(9999)
        box.succeed("curl -sf http://127.0.0.1:9999/ >/dev/null")
        client.fail(f"curl -sS --max-time 5 http://{box_ip}:9999/")
        box.succeed("systemctl stop loopback-9999")

    with subtest("3b. a port the box's firewall closes stays closed to the tailnet"):
        box.succeed("systemd-run --unit wildcard-9998 -p DynamicUser=yes"
                    " ${pkgs.python3}/bin/python3 -m http.server 9998 --bind 0.0.0.0")
        box.wait_for_open_port(9998)
        client.fail(f"curl -sS --max-time 5 http://{box_ip}:9998/")
        box.succeed("systemctl stop wildcard-9998")

    with subtest("3c. the tailnet reaches only the front door, even past an open port"):
        # A native box usually has no host firewall at all, so the unit's own
        # nftables table is what keeps 0.0.0.0 services off the tailnet. Here
        # the NixOS firewall OPENS 9997, so only that table can refuse it.
        box.succeed("${pkgs.nftables}/bin/nft list table inet agent_box_tailnet")
        box.succeed("systemd-run --unit wildcard-9997 -p DynamicUser=yes"
                    " ${pkgs.python3}/bin/python3 -m http.server 9997 --bind 0.0.0.0")
        box.wait_for_open_port(9997)
        lan_ip = box.succeed(
            "ip -4 -o addr show eth1 | awk '{print $4}' | cut -d/ -f1").strip()
        client.succeed(f"curl -sf --max-time 5 http://{lan_ip}:9997/ >/dev/null")
        client.fail(f"curl -sS --max-time 5 http://{box_ip}:9997/")
        box.succeed("systemctl stop wildcard-9997")

    with subtest("4. disconnect: marker, unit, node state, site all gone"):
        box.succeed(as_agent("agent-box-tailscale disconnect >&2"))
        # Stop removes the tailnet-ingress table with the daemon.
        box.fail("${pkgs.nftables}/bin/nft list table inet agent_box_tailnet")
        box.succeed(f"test ! -e {MARKER}")
        box.fail(f"systemctl is-active {UNIT}")
        box.succeed("test -z \"$(ls -A /var/lib/agent-box-tailscale)\"")
        box.succeed(f"test ! -e {SITE}")
        client.fail(f"{curl}")

    with subtest("5. connect again, reboot: the unit comes back"):
        connect()
        box.succeed(f"systemctl is-active {UNIT}")
        box.shutdown()
        box.start()
        box.wait_for_unit(UNIT)
        box.wait_until_succeeds(
            f"{TS} status --json --peers=false | grep '\"BackendState\": \"Running\"'",
            timeout=120)
        box.succeed(f"test -e {MARKER}")
        box.wait_for_unit("caddy.service")
        # A new node: Headscale still holds the logged-out one, so the name
        # can come back with a suffix (box-1). The site follows the new name.
        status = box_status()
        box_ip = status["TailscaleIPs"][0]
        dns = status["Self"]["DNSName"].rstrip(".")
        box.succeed(f"grep -F 'https://{dns} {{' {SITE}")
        client.wait_until_succeeds(
            f"curl -sS -o /dev/null -w '%{{http_code}}' --max-time 10"
            f" --cacert /tmp/caddy-root.crt --resolve {dns}:443:{box_ip}"
            f" -L -u agent:testpassword https://{dns}/agent/ | grep -x 200", timeout=120)
  '';
}
