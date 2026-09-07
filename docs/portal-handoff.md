# Portal → box session handover

This is the wire contract between a portal and an agent-box. It lets a
browser with a live portal session land in the box's web UI without typing
the box's HTTP Basic credentials.

The portal this was written for is **Defang Station** (`DefangLabs/station`),
which is its **own service** — it is *not* `portal.defang.io`, and nothing
here assumes any particular hostname. The box is told the issuer it must
trust (`web.portalIssuer`), so any portal that signs the shape below works,
and every example issuer in this file is a placeholder.

Issue [#541](https://github.com/defangdevs/agent-box/issues/541). The
alternative-auth parent is [#201](https://github.com/defangdevs/agent-box/issues/201).

Everything below is normative for the portal. The box rejects anything it
does not state.

## 1. Shape of the flow

```
browser        portal                                  box
   |  GET /boxes/42/open                                |
   |------------------->|                               |
   |                    | mint handover token (Ed25519) |
   |  200 auto-submit form                              |
   |<-------------------|                               |
   |  POST /<user>/auth/handoff   token=<jwt>           |
   |--------------------------------------------------->|
   |                                     verify + map + mint session
   |  303 -> /<user>/   Set-Cookie: __Host-agent_box_session_<user>
   |<---------------------------------------------------|
   |  GET /<user>/      (cookie)                        |
   |--------------------------------------------------->|
```

The box is a **verifier**, never a client. It makes no call back to the
portal, so it needs no portal credential, no egress and no JWKS fetch.

## 2. The token

A compact JWS. Three segments, `base64url` without padding.

### Header

```json
{ "alg": "EdDSA", "typ": "JWT", "kid": "portal-2026-09" }
```

- `alg` MUST be `EdDSA` over curve Ed25519. The box accepts **no other
  value** — not `none`, not `HS256`, not `RS256`. This is a fixed
  single-algorithm check, not a lookup driven by the header, so the usual
  algorithm-confusion substitution has nothing to select.
- `kid` is OPTIONAL and advisory. The box tries every configured public key
  and does not require a match, so key rotation never depends on the portal
  and the box agreeing on a name.

### Claims

```json
{
  "iss": "https://station.example.com",
  "aud": "agent-box",
  "sub": "usr_2Nk9x…",
  "project": "acme-prod",
  "iat": 1788700000,
  "exp": 1788700060,
  "jti": "01JZ8Q0T5S6P7R8V9W0X1Y2Z3A"
}
```

| claim | required | rule the box enforces |
|---|---|---|
| `iss` | yes | Exact string match against the box's configured issuer. |
| `aud` | yes | MUST be the literal `agent-box`. May also be a 1-element array containing it. |
| `sub` | yes | The portal's user id. 1–256 chars. |
| `project` | **no** | A project within that account. 1–256 chars if present. Required **only** if the box declares `portalProject` — see §5. |
| `iat` | yes | Must not be more than 60 s in the future (clock skew). |
| `exp` | yes | Must be in the future. `exp - iat` MUST be ≤ 300 s. |
| `jti` | yes | Unique per token. 1–256 chars. Replay is refused. |

Any other claim is ignored.

**`sub` is the whole of what the box needs.** The endpoint is
`/<user>/auth/handoff`, and each linux user's daemon serves its own — so the
**URL has already named the linux user** before any claim is read. The token
therefore *authorizes* the user being addressed; it never has to *select*
one. That is why `project` can be omitted without becoming ambiguous, and it
is what the MVP does.

**Why `aud` is a constant and not the box's hostname.** The token is scoped to
a portal account, deliberately not to a box (#541), so one token works at
each box that account owns. A hostname audience would put box identity back
into the claims, and would mean the portal has to know which box it is
sending the browser to before it can sign. The constant audience does a
different and still necessary job: it separates a **handover** token from
every other token the portal signs with the same key. A portal API token
replayed at the box's handoff endpoint fails on `aud`.

**Recommended lifetime: 60 s.** The token is a redirect carrier, not a
session. The box's hard ceiling is 300 s.

## 3. Delivery: POST, never a URL

The portal MUST deliver the token as an `application/x-www-form-urlencoded`
POST body to:

```
POST https://<box-domain>/<user>/auth/handoff
Content-Type: application/x-www-form-urlencoded

token=<jwt>
```

The route accepts **POST only**. `GET` returns 405, and the box never reads a
token from a query string.

This is the SAML/OIDC form-POST binding, and the reason is that a `?token=`
query string is written to Caddy's access log, to the browser's history, and
to the `Referer` of anything the landing page loads. The token is short-lived
and single-use, so a leak is survivable — but it is free not to leak it.

A portal serves it as a self-submitting form:

```html
<form id="f" method="POST" action="https://box.example.com/agent/auth/handoff">
  <input type="hidden" name="token" value="…">
  <noscript><button type="submit">Open your agent-box</button></noscript>
</form>
<script>document.getElementById('f').submit()</script>
```

`<user>` is the box's linux user for that project. The portal learns it from
the box's own provisioning record — see §5.

## 4. What the box does, in order

1. **Parse.** Three dot-separated segments, header `alg` exactly `EdDSA`.
2. **Verify the signature** over `header.payload` against each configured
   public key.
3. **Check the claims** per the table above.
4. **Authorize the linux user the URL named.** `sub` must match that
   user's declared `portalUser`; if the box also declares `portalProject`,
   the token must carry a matching `project`. No match → **403**.
   **A handover never creates a project** (decided on #541): the endpoint is
   unauthenticated by construction, so minting a linux account from a claim
   made on it is not a privilege the flow gets to have.
5. **Refuse a replay.** `jti` already spent → **401**. Otherwise record it
   as spent until `exp` plus the skew allowance, so the record outlives
   every moment the token itself is still acceptable.

**Steps 1–3 and 5 all answer a single `401`**, with one message. The caller
here is unauthenticated, so naming the field that failed would let it tune a
token against the box one check at a time. Only the mapping answers
differently, and only because 403 tells an operator something they can act
on: the token was genuine, this box just does not host that project.

**The mapping is checked BEFORE the replay record is written**, so a token
this box will refuse anyway does not burn its `jti`. Otherwise a caller could
fill the spent-id store with ids that never had a session coming.
6. **Mint a box session.** A 256-bit random value; the box stores only its
   SHA-256, so the session store cannot be read back into a live cookie.
7. **Answer** `303 See Other` to `/<user>/` with:

```
Set-Cookie: __Host-agent_box_session_<user>=<value>;
            Path=/; Max-Age=86400; HttpOnly; Secure; SameSite=Lax
```

**Lifetime: 1 day** (decided on #541). Expiry is enforced by the box against
the stored record, not by the browser's `Max-Age`.

**`SameSite=Lax`, where the Basic-auth cookie uses `Strict`.** The
navigation that follows the POST is initiated cross-site, from the portal. A
`Strict` cookie is withheld on exactly that navigation, so the user would
land back on a Basic-auth prompt and the handover would appear to do nothing.
`Lax` is sent on top-level navigations, which is what this flow is.

## 5. Provisioning: what the box needs before any of this works

The MVP shape — one portal account per box, no project claim:

```yaml
users:
  agent:
    root: true
    portalUser: usr_2Nk9x…          # the `sub` this box admits
web:
  portalIssuer: https://station.example.com   # Station's own URL
  portalKeyFiles:
    - /etc/agent-box/portal-key.pub
```

- **`portalUser` is the mapping.** A token whose `sub` matches it is
  authorized for this linux user. Without it, no handover route is served at
  all.
- `portalIssuer` is the `iss` the box demands. Whatever Station uses — the
  box trusts exactly this one string.
- `portalKeyFiles` are PEM `PUBLIC KEY` files holding Ed25519 keys. List two
  during a rotation: the box tries all of them.
- **`portalProject` is optional**, and narrows `portalUser` to one project
  within that account:

  ```yaml
      portalUser: usr_2Nk9x…
      portalProject: acme-prod      # now a matching `project` claim is REQUIRED
  ```

  Set it when one portal account owns several projects and a token for one
  must not open another. Setting it *without* `portalUser` does nothing — it
  narrows a mapping rather than creating one.

A project is a linux user
([#352](https://github.com/defangdevs/agent-box/issues/352)), so this is
declared where every other per-project setting is — one `users:` entry in
`/etc/agent-box/config.yaml`, reconciled by `agentbox apply`. There is no
second spec file.

Generate a keypair with the openssl the box already ships:

```
openssl genpkey -algorithm ed25519 -out portal-key.pem
openssl pkey -in portal-key.pem -pubout -out portal-key.pub
```

In Node, `crypto.createPrivateKey` reads that PEM and
`crypto.sign(null, data, key)` produces the 64-byte Ed25519 signature to
base64url — `alg: "EdDSA"` needs no digest argument.

## 6. Revocation

Two independent axes.

1. **Per account, immediate.** Remove `portalUser` from the box's `users:`
   entry and apply. That both stops serving the handover route and makes
   every later handover fail. Existing cookies still run until they expire.
2. **Per session, ≤ 1 day.** A session record expires on its own, and the
   browser must return through the portal — which is a live portal decision.
   The interval *is* the granularity.

Deleting the session record file revokes one browser at once.

## 7. What this does not do

- **No continuous check.** Between handovers the box does not ask the portal
  whether access still holds. A session outlives a portal-side revocation by
  up to its remaining lifetime. Shorten the lifetime if that window matters
  more than the number of redirects.
- **No identity beyond the mapping.** The box records `sub` and `project` on
  the session, but every session for a project has the same linux user and
  the same filesystem. Users are the isolation boundary here; a project is a
  user ([#127](https://github.com/defangdevs/agent-box/issues/127)).
- **Basic auth still works.** It is an independent path that never consults
  the portal or the settings daemon, so a portal outage — or a daemon crash
  loop — can never lock the box owner out of their own machine.
