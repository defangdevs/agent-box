# Optional inbound email

Email is disabled by default. When enabled, explicitly registered purpose
addresses feed the owning Linux user's existing local-webhook bus. The hostnames can be IP-based
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

OpenSMTPD requires both certificate and key files to be owned by root. If
your certificate manager runs as another user (such as Caddy), use a
root-managed renewal hook to validate and copy the matching certificate/key
pair into a private mail directory with mode `0644` for the certificate and
`0600` for the key, then restart the mail service. Pointing intake directly
at Caddy-owned certificate files fails OpenSMTPD validation.

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

On a native host, an enabled user's configured home must match the existing
Linux account's passwd home; apply rejects a mismatch before writing changes.

The existing browser/webhook capability must be enabled for participating
users. On NixOS they need `web.passwordHashFile`, as for any webhook receiver.
If omitted, `email.domains` defaults to the box's configured web domain;
set it explicitly when the browser uses a bare IP. No domain suffix is
hard-coded in the intake.

OpenSMTPD listens on TCP 25 with opportunistic STARTTLS, accepts only the
registered, enabled addresses for configured Linux users and domains.
Unregistered addresses, including arbitrary `+tags`, and relay attempts are
rejected at RCPT before any message is queued. Tags use unquoted ASCII
local-part characters; spaces, quotes and backslashes are not accepted.
MDA delivery runs as the
recipient Linux user; mail cannot choose a command or another user's home.
The explicit delivery userbase bypasses user `.forward` files.
No catch-all address or implicit triage watch is installed.

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
Tagged addresses retain the full recipient in the topic: mail for
`agent+github@box.example` routes to `email:agent+github@box.example`,
under the same Linux user as `agent@box.example`. The payload contains
`recipient` (full address), `baseRecipient` (`agent@box.example`) and
`recipientTag` (`github`, or an empty string for untagged mail). The first
`+` separates the tag; `agent+build+linux@box.example` has tag `build+linux`.
The adapter normalizes the base user and domain to lowercase and keeps the
tag as supplied by OpenSMTPD. Tags are sender-controlled routing data.

## Runtime purpose addresses

In Settings, open **Automations -> Email addresses**. Add a complete address,
a short purpose, an existing worker profile and a batching window. Each
address has its own exact-topic standing watch. For example, triage, GitHub
notifications and Linear notifications can use different profiles and batch
sizes. Creating and changing addresses needs no rebuild or MTA restart.

The CLI provides the same operations:

```sh
agent-box-email addresses set agent+triage@box.example \
  --purpose 'Triage incoming correspondence' --profile triage --batch-seconds 30
agent-box-email addresses set agent+github@box.example \
  --purpose 'Triage GitHub notifications' --profile github --batch-seconds 120
agent-box-email addresses set agent+linear@box.example \
  --purpose 'Triage Linear notifications' --profile linear --batch-seconds 120
agent-box-email addresses list
agent-box-email addresses disable agent+github@box.example
```

Create purpose-specific worker profiles first. The purpose is included in
that address's watch briefing; the profile supplies the worker configuration
and instructions. Register an untagged address explicitly if desired. An
address must belong to the calling Linux user and a host-configured domain.
Domain aliases are separate addresses and must each be registered.
Registry addresses are lowercase, matching the bus's case-insensitive topic
identity; differently cased spellings cannot acquire separate handlers.

A handler is installed before reception is enabled. Updating an existing
address first disables reception; if saving its handler fails, it remains
disabled until a successful save. Disable stops new SMTP recipients while
preserving the watch for previously accepted mail. Saving again enables it.
Another automatic rule for the same address must be removed first, preventing
an earlier rule from silently taking its notifications. Managed address
watches are edited through Email addresses, not the generic rule editor.

The event is `email.received`. Existing durable dispatch, coalescing, claims
and hook-session admission apply. Batching groups a burst into a session;
there is no separate agent or dispatcher per idle address. Existing-session
subscriptions can still observe an explicitly registered address, for example:

```sh
agent-box-webhook subscribe email:agent+github@box.example --ttl 1 \
  --note 'Observe GitHub mail for this task'
```

Registries are stored under `/var/lib/agent-box-email/addresses/<user>/`.
The root-owned parent separates users. Each per-user directory is setgid
`<user>:smtpd` with mode `2750`; atomically published registry files are
`0640`. The filter reads bounded, validated metadata without access to the
user's private webhook state or credentials. Missing or disabled addresses
receive `550 5.1.1`; an unreadable or corrupt registry receives `451 4.3.0`
so senders can retry. There are at most 128 entries and 128 KiB per registry.
Keep these registries with the host's persistent email state.

### Upgrade from unrestricted tags

This changes reception to deny by default. Existing email watches do not
implicitly authorize addresses. After deploying this release, register every
intended address in Settings or with the CLI, including any untagged triage
address. Remove a prior automatic rule for the same exact address before
registering its replacement. Until registered, the address is rejected at
SMTP; senders receiving a permanent refusal may need to resend after setup.
Already queued mail keeps its transport responsibility and can drain through
its previous bus handler. No previously accepted email is replayed by adding
an address.

## Mandatory sender verification

The IPv4, IPv6 and local control-socket listeners run the same OpenSMTPD
filter using dkimpy and pyspf. Before
accepting a message, the filter requires exactly one valid `From` mailbox
and at least one of:

- SPF passes for the connecting IP and envelope sender, whose domain exactly
  matches the `From` domain.
- A DKIM signature passes with a signing domain that exactly matches the
  `From` domain. RSA-SHA256 and Ed25519-SHA256 are supported; SHA-1 and
  signatures with a body-length (`l=`) limit do not qualify.

This rule is unconditional. Unsigned mail without aligned SPF, missing DNS
records, failed verification, and unrelated signing domains receive SMTP
`550 5.7.1`. Subdomain alignment is deliberately strict: `mail.example.org`
does not authenticate `example.org`. A null envelope sender needs aligned
DKIM. Local `sendmail` submissions also require aligned DKIM because they
have no remote IP for SPF. A passing aligned DKIM signature can authenticate forwarded mail when
SPF fails. Temporary DNS/verifier errors and verifier saturation receive
`451 4.7.1`, leaving retry responsibility with the sender. Verification uses
the actual SMTP connection and envelope, never client-supplied transport or
`Authentication-Results` headers. No localhost bypass is provided.

This is a local authentication acceptance rule, not a full DMARC policy or
reporting implementation. It does not consult `p=none`/`quarantine`/`reject`
or relax the rule for domains without DMARC records. DNS work, message size,
signature count, active connections and concurrent verifications are bounded.
Headers are limited to 64 KiB before parsing. RSA keys must have a modulus
between 1024 and 4096 bits and an odd exponent between 3 and 2^32-1;
these bounds are checked before the RSA operation.
DATA lines above 64,000 bytes are rejected before forwarding them through
OpenSMTPD's bounded filter-response buffer.

The filter replaces all inbound copies of its verification marker with a
server-generated value persisted under `/var/lib/agent-box-email`. The value
survives restarts; preserve this state alongside the SMTP spool.
Only messages passing the SMTP commit gate are queued for delivery. The
adapter refuses unmarked messages, including mail queued before upgrading;
inspect or redeliver that legacy queue explicitly. Accepted events carry
`senderAuthenticated: true` and `authenticationPolicy: "exact-domain-v1"`.
The receiving agent does not decide whether authentication passed.

Authentication establishes the author's domain, not an individual mailbox's
owner or permission to execute a request. Mail text, attachments, display
names and other headers remain untrusted input. Local HMAC authenticates
the intake producer. Authorization for agent actions remains separate from
email authentication.

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

Message-size, MIME-part and recipient refusals are permanent delivery errors;
configuration, identity and bus-handoff failures remain temporary so the MTA
can retry after the service is repaired.

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
