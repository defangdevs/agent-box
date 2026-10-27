# Real OpenSMTPD -> recipient uid -> signed bus -> existing durable inbox.
{ agent-box }:
{
  name = "agent-box-email";
  node.pkgsReadOnly = false;
  nodes.machine = { pkgs, ... }: {
    imports = [ agent-box ];
    virtualisation.memorySize = 2048;
    services.agent-box = {
      enable = true;
      web = { enable = true; domain = "box.test"; user = "agent"; fail2ban = false; };
      users = {
        agent = {
          email = true;
          web.passwordHashFile = pkgs.writeText "web-password-hash" "$2b$12$wW7Z8XbEnENqhTB8sFDhkeKCStpPocw7E4acMC4hN0ex2H5zP.vxK";
        };
        other = { seedMainSession = false; };
      };
      email = {
        enable = true;
        domains = [ "box.test" "1-2-3-4.defangstation.com" ];
        certificateFile = "/etc/agent-box-email-cert.pem";
        keyFile = "/etc/agent-box-email-key.pem";
      };
    };
    system.activationScripts.emailCertificate.text = ''
      ${pkgs.openssl}/bin/openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout /etc/agent-box-email-key.pem -out /etc/agent-box-email-cert.pem \
        -days 2 -subj /CN=box.test
      chmod 600 /etc/agent-box-email-key.pem
    '';
    environment.systemPackages = [ pkgs.python3 pkgs.util-linux ];
    environment.etc."email-probe.py".text = ''
      import smtplib, ssl
      body = b"From: claimed@example.org\r\nTo: other@elsewhere.example\r\nSubject: Test mail\r\nMessage-ID: <reused@example.org>\r\n\r\nComplete private body\r\n"
      with smtplib.SMTP("localhost", 25, timeout=20) as client:
          client.ehlo()
          assert client.has_extn("starttls")
          client.starttls(context=ssl._create_unverified_context())
          client.ehlo()
          for address in ("other@box.test", "unknown@box.test", "agent@elsewhere.example", "agent+stale@box.test"):
              client.mail("sender@example.org")
              code, _ = client.rcpt(address)
              assert code >= 500, (address, code)
              client.rset()
          for address in ("agent@box.test", "agent@1-2-3-4.defangstation.com"):
              assert client.sendmail("sender@example.org", [address], body) == {}
    '';
    system.stateVersion = "25.05";
  };
  testScript = ''
    import json
    try:
        machine.wait_for_unit("agent-box-email.service")
    except Exception:
        print(machine.succeed("journalctl -u agent-box-email-source@agent.service -u agent-box-email.service --no-pager"))
        raise
    machine.wait_for_unit("agent-box-webhook@agent.socket")
    machine.wait_for_open_port(25)
    source = json.loads(machine.succeed("cat /home/agent/.local/state/local-webhook/sources.json"))
    assert source["defaultSource"] == "github"
    assert source["sources"]["email"]["retainPayload"] is True
    machine.succeed("su - agent -c 'LOCAL_WEBHOOK_SESSION=emailwait agent-box-webhook subscribe email:agent@box.test --note mail-test'")
    machine.succeed("su - agent -c 'LOCAL_WEBHOOK_SESSION=emailwait agent-box-webhook subscribe email:agent@1-2-3-4.defangstation.com --note alias-test'")
    machine.succeed("su - agent -c \"echo '|touch /home/agent/forward-ran' > /home/agent/.forward\"")
    # SMTP 250 means the MTA owns a durable copy, even while the bus is down.
    machine.succeed("systemctl stop agent-box-webhook@agent.socket agent-box-webhook@agent.service")
    machine.succeed("python3 /etc/email-probe.py")
    machine.fail("test -d /home/agent/.local/state/local-webhook/spool/content/new")
    machine.succeed("systemctl start agent-box-webhook@agent.socket")
    # Force the MTA retry now instead of waiting for its normal backoff.
    machine.succeed("runuser -u root -g smtpq -- /etc/agent-box/email-tools/smtpctl schedule all")
    machine.wait_until_succeeds("test $(find /home/agent/.local/state/local-webhook/spool/content/new -type f | wc -l) -eq 2")
    machine.wait_until_succeeds("test $(find /home/agent/.local/state/local-webhook/spool/peers/emailwait/new -type f | wc -l) -eq 2")
    machine.succeed("find /home/agent/.local/state/local-webhook/spool/content/new -type f -exec stat -c '%U %a' {} + | grep 'agent 600' >/dev/null")
    machine.fail("su - other -c 'ls /home/agent/.local/state/local-webhook/spool/content/new'")
    records = json.loads(machine.succeed("python3 -c 'import pathlib,json; print(json.dumps([json.loads(p.read_text()) for p in pathlib.Path(\"/home/agent/.local/state/local-webhook/spool/content/new\").iterdir()]))'"))
    assert len({r["id"] for r in records}) == 2
    for record in records:
        assert "Complete private body" in record["payload"]["text"]
        assert record["payload"]["senderAuthenticated"] is False
        assert record["payload"]["headers"]["to"] == ["other@elsewhere.example"]
        assert record["payload"]["rawMimeBase64"]
    machine.succeed("systemctl restart agent-box-webhook@agent.service agent-box-email.service")
    machine.wait_for_unit("agent-box-email.service")
    machine.fail("test -e /home/agent/forward-ran")
    ident = records[0]["id"]
    machine.succeed(f"su - agent -c 'agent-box-webhook read-content {ident}'")
  '';
}
