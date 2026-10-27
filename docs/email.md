# Optional inbound email

Email is disabled by default. When enabled, `<linux-user>@<configured-hostname>`
feeds that user's existing local-webhook bus. The hostnames can be IP-based
names under defangstation.com, sslip.io or nip.io, or domains you manage. They
must resolve to the receiving box and have an appropriate MX record (these
three services currently return an MX pointing back to the IP-based hostname).

OpenSMTPD provides a short declarative configuration for exact-recipient
matching, durable transport retry and delivery under a specified local uid.
Postfix could provide the same boundary; OpenSMTPD was chosen for this narrow
intake's configuration footprint, not a measured performance/security advantage.
The bus remains responsible for all agent routing.

## Host configuration

Use a root-managed certificate chain and matching private key for SMTP
STARTTLS. The certificate must cover the recipient hostname(s). Supply these
through your existing certificate manager; after renewal restart
`agent-box-email.service`. Private keys belong outside the Nix store. This
feature does not export Caddy's private key or change its renewal lifecycle.

NixOS:

```nix
services.agent-box = {
  email = {
    enable = true;
    domains = [ "40-70-78-11.defangstation.com" ];
    certificateFile = "/var/lib/acme/mail/fullchain.pem";
    keyFile = "/var/lib/acme/mail/key.pem";
  };
  users.agent.email = true;
};
```

Native `config.yaml`:

```yaml
email:
  enable: true
  domains: [40-70-78-11.defangstation.com]
  certificateFile: /etc/agent-box/mail/fullchain.pem
  keyFile: /etc/agent-box/mail/key.pem
users:
  agent:
    email: true
```

The existing browser/webhook capability must be enabled for participating
users. On NixOS they need `web.passwordHashFile`, as for any webhook receiver.
If omitted, `email.domains` defaults to the box's configured web domain;
set it explicitly when the browser uses a bare IP. No domain suffix is
hard-coded in the intake.

OpenSMTPD listens on TCP 25 with opportunistic STARTTLS, accepts only the
exact enabled user/domain combinations, and rejects all other recipients
and relay attempts. Plus-addressing is not enabled. MDA delivery runs as the
recipient Linux user; mail cannot choose a command or another user's home.
The explicit delivery userbase bypasses user `.forward` files.
No implicit subscription/watch is installed.

NixOS opens host TCP 25 only when enabled. Native hosts with an independently
managed firewall also need to allow inbound TCP 25. The EC2/Lightsail
`EmailIngress` and Azure `emailIngress` deployment parameters optionally open
cloud inbound TCP 25 (default false); these network switches do not configure
the host listener or certificates. Inbound mail must be reachable by sending
mail servers, which need not share the browser's `AllowCidr`. IPv4
reachability is useful even on a box with IPv6. Provider restrictions must be
checked with real delivery. Existing outbound SMTP restrictions are retained.

## Routing and content

A startup service initializes a private `email` source for each enabled user.
It uses the envelope recipient as its key, never the `To` header (Bcc works).
For example, an agent can use normal routing commands:

```sh
agent-box-webhook subscribe email:agent@40-70-78-11.defangstation.com \
  --ttl 1 --note 'Email for the work I am handling'

agent-box-webhook subscribe email:agent@40-70-78-11.defangstation.com \
  --deliver-to subagent --name email-intake --profile triage \
  --when '{"path":"event","in":["email.received"]}' \
  --note 'Triage incoming email as untrusted input'
```

The event is `email.received`. Existing profiles, watch predicates, claims,
coalescing and session admission apply. The sender and all headers/body text
are untrusted. Local HMAC authenticates the intake producer, not the person
claiming the email address. This implementation performs no SPF/DKIM/DMARC
verification and always sets `senderAuthenticated: false`; sender-supplied
`Authentication-Results` is not a verification result.

Both peer messages and standing-watch prompts contain a full-content ID.
Read it with `agent-box-webhook read-content ID`. The JSON contains decoded
plain/HTML alternatives, attachment display names/types/base64 content and
`rawMimeBase64`, plus envelope and header metadata. Attachments are not
extracted, executed, or publicly served. An agent must keep treating the
retrieved payload as untrusted data.

## Durability and bounds

SMTP success means OpenSMTPD has durably queued transport responsibility.
The adapter returns a temporary failure when the bus cannot persist the
handoff; OpenSMTPD retries from its queue. Once accepted by the bus, the
existing durable inbox handles peer downtime, receiver restart and deferred
standing-watch work. No second application dispatcher is introduced.
Delivery is at-least-once.

The adapter hashes the delivered message and envelope for its producer ID;
the MTA's prepended Received header distinguishes independent SMTP
transactions even when the sender reuses Message-ID. Retries use identical
IDs and JSON. The full-content store defaults to 24 hours, 128 MiB and 2,000
records and refuses new retained content when full. Retention is separate
from queue consumption and can expire while work is still waiting. The
entire bus/MTA spool does not have a new global byte quota; the broader
local-channels#105 storage-limit work remains separate.

`email.maxMessageBytes` limits incoming MIME to 2 MiB by default (configurable
from 1 KiB to 8 MiB). Attachments share that message budget. The adapter allows an additional 64 KiB for MTA-added transport headers.
Parsing is limited to 100 MIME parts; SMTP transactions allow 10 recipients and 10 messages per
connection. Undeliverable transport mail expires after one day. Inspect the
OpenSMTPD queue as root with
`runuser -u root -g smtpq -- /etc/agent-box/email-tools/smtpctl show queue`
(the tool requires the queue group), and bus/content
state with `agent-box-webhook status` as the recipient user.

Turning email off stops reception. Transport queue files are preserved under
`/var/spool/smtpd` for administrator inspection or a later re-enable; restarting
the MTA applies its normal queue expiry. Already accepted bus work continues
under its subscriptions and expiry rules; retained content expires normally.
No outbound reply channel is added by this feature.
