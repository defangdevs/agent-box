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

The box is a **verifier**, never a client of the portal's API: it holds no
portal credential and the portal never calls in. It does fetch the portal's
**published signing keys** — see §2 — which is what lets the portal rotate
them without redeploying a single box.

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
- `kid` is OPTIONAL but **recommended**. With one, the box verifies against
  exactly that key from your JWKS, and a `kid` it has not seen makes it
  refetch — so a key you published seconds ago works immediately. Without
  one it tries every Ed25519 key in the set, which also works but gets
  slower as you keep old keys around.

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

### 2.1 Publishing the keys (`.well-known/jwks.json`)

The portal MUST serve its signing keys as a JWK Set at

```
https://<issuer>/.well-known/jwks.json
```

over **HTTPS**. The box derives that URL from the issuer it was configured
with, and **never from the token** — a `jku`-style header that could point
the box at a key server of the caller's choosing would make the signature
check meaningless. A portal that publishes elsewhere is accommodated by box
configuration (`web.portalJwksUrl`), not by a claim.

Each signing key is an OKP/Ed25519 JWK:

```json
{
  "keys": [
    {
      "kty": "OKP",
      "crv": "Ed25519",
      "use": "sig",
      "kid": "portal-2026-09",
      "x": "vC22x8FyVqzCuadrQ-HJkZdqjeZ9S-yrrD_ereHRoRo"
    }
  ]
}
```

- `kty` MUST be `OKP` and `crv` MUST be `Ed25519`. Any other key type in the
  set is **skipped**, not guessed at.
- `use`, if present, MUST be `sig`. A key published for encryption is not
  accepted for signatures.
- `x` is the 32-byte public key, base64url, unpadded.
- `kid` is what a token's `kid` selects.

In Node, `crypto.createPublicKey(pem).export({format: 'jwk'})` produces
exactly this for an Ed25519 key.

**How the box treats the set.** It caches it on disk for an hour. A `kid` it
does not hold triggers **one** refetch, rate-limited to once a minute — so a
newly published key is picked up at once, and a caller minting tokens with
random `kid`s cannot turn the handover route into a way to hammer the portal.

**Rotation** therefore needs nothing from the box: publish the new key
alongside the old one, start signing with the new `kid`, and retire the old
entry once no token bearing it can still be valid (its `exp` plus the box's
skew allowance — so a minute after you stop using it).

**An outage is survivable, but not indefinitely.** If the endpoint is
unreachable the box keeps using the keys it already cached, so a brief blip
does not lock anyone out. The cost is that a **withdrawn** key stays usable
for up to the cache hour. If you need a key dead sooner than that, the
revocation to reach for is per-account (§6), not the key set.

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
2. **Verify the signature** over `header.payload` against the keys the
   portal publishes (§2.1), selected by `kid` when the token carries one.
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
```

- **`portalUser` is the mapping.** A token whose `sub` matches it is
  authorized for this linux user. Without it, no handover route is served at
  all.
- `portalIssuer` is the `iss` the box demands. Whatever Station uses — the
  box trusts exactly this one string.
- **No key is configured.** The box fetches them from
  `<portalIssuer>/.well-known/jwks.json` (§2.1), so a rotation never touches
  a box and no public key is committed to a template.
  `web.portalJwksUrl` overrides that path for a portal that publishes
  somewhere else.
- **`portalProject` is optional**, and narrows `portalUser` to one project
  within that account:

  ```yaml
      portalUser: usr_2Nk9x…
      portalProject: acme-prod      # now a matching `project` claim is REQUIRED
  ```

  Set it when one portal account owns several projects and a token for one
  must not open another. Setting it *without* `portalUser` does nothing — it
  narrows a mapping rather than creating one.

**From a 1-click deployment** (issue #593), both values are template
parameters, alongside the web password:

| flavor | parameters |
|---|---|
| AWS CloudFormation (`deploy/aws/template.yaml`) | `PortalUser`, `PortalIssuer` |
| AWS Lightsail (`deploy/aws/lightsail-template.yaml`) | `PortalUser`, `PortalIssuer` |
| Azure Bicep (`deploy/azure/agent-box.bicep`) | `portalUser`, `portalIssuer` |

Both are optional and both are needed: leave either blank and the box serves
no handover route at all, which is what a hand-launched box gets. **There is
no key parameter on any flavor** — that is the point of §2.1.

`portalUser` is the per-box value, and it has to arrive this way rather than
over the wire: the handover endpoint is unauthenticated by construction, so a
token must never be able to tell a box whose it is.

A project is a linux user
([#352](https://github.com/defangdevs/agent-box/issues/352)), so this is
declared where every other per-project setting is — one `users:` entry in
`/etc/agent-box/config.yaml`, reconciled by `agentbox apply`. There is no
second spec file.

Generate the portal's keypair once (the box never sees the private half,
and never needs the public half configured either — it fetches it):

```
openssl genpkey -algorithm ed25519 -out portal-key.pem
```

In Node, `crypto.createPrivateKey` reads that PEM and
`crypto.sign(null, data, key)` produces the 64-byte Ed25519 signature to
base64url — `alg: "EdDSA"` needs no digest argument. The matching JWK for
§2.1 is `crypto.createPublicKey(privateKey).export({format: 'jwk'})` plus a
`kid` and `use: "sig"` of your choosing.

## 6. Revocation

Two independent axes.

1. **Per account, immediate.** Remove `portalUser` from the box's `users:`
   entry and apply. That both stops serving the handover route and makes
   every later handover fail. Existing cookies still run until they expire.
2. **Per session, ≤ 1 day.** A session record expires on its own, and the
   browser must return through the portal — which is a live portal decision.
   The interval *is* the granularity.

Deleting the session record file revokes one browser at once.

A third axis exists without touching host configuration at all: §9's
compare-and-swap revokes every session for the OLD identity as part of
changing the mapping, immediately and without a reboot or `agentbox apply`.

## 7. Driving the box after a handover

The cookie §4 mints is not only for a browser. A portal that holds it can render the
box's **guided sign-in cards** and its **environment store** in its own UI and call
here for what is behind them — which is what Defang Station does
([#642](https://github.com/defangdevs/agent-box/issues/642),
`DefangLabs/station#80`). Everything below is under `/<user>/settings`, inside the same
auth block as the page, and needs no CORS: the portal calls from its own server with the
cookie it was handed, and the browser never talks to the box.

| method | path | answer |
|---|---|---|
| `GET` | `/connect` | `{"ok": true, "flows": [...]}` — every card this box offers |
| `GET` | `/connect?flow=<id>` | `{"ok": true, "flow": {...}}` — one card |
| `GET` | `/env` | `{"ok": true, "keys": [...]}` — env key **names** |
| `GET` | `/status` | `{"rev", "sessions", "connect_fp"}` — a change fingerprint |
| `POST` | `/connect/start` | `flow=<id>` → `303`, or `409` + a page it could not begin |
| `POST` | `/connect/code` | `flow=<id>&code=<code>` → `303` |
| `POST` | `/connect/cancel` | `flow=<id>` → `303` |
| `POST` | `/set` | `key=&value=` → `303` |
| `POST` | `/delete` | `key=` → `303` |

A card's `state` is one of `checking`, `idle`, `starting`, `waiting`, `exchanging`,
`connected`, `failed`, `expired`, derived from the CLI's own status command and the tmux
pane the flow runs in — never from anything this daemon stored, which is why a card
survives a reload, a daemon restart or a second reader. `installed: false` means the
button offers to fetch the CLI first.

**No route returns a stored secret.** `/env` answers names, `/set` is where a value
travels, and it travels in a body. The one piece of pane text that can come back is a
failed flow's last line, redacted first.

Four things a portal has to get right, each of which has bitten one:

- **`{BASE}` is `/<user>/settings`, and an unknown path under it answers `200` with the
  settings page** — not `404`. So "does this box have that route" is decided by the
  **content type**, not the status.
- **The card set varies per box.** A flow whose CLI the box can neither find nor fetch is
  not served, and `?flow=<id>` answers `404`. That means "not on this box", never
  "broken" — and a `404` from `/auth/handoff` (§4) is a third thing again: no handover
  route at all.
- **The POSTs answer `303`, or `409` and a rendered page.** Do not parse the page; act,
  then re-read the card. A JSON answer for these is ask 3 of #642 and not shipped.
- **Poll `/status` first when polling.** `connect_fp` changes only when a card does, so a
  1–3 s poll costs one small request until something moves.

The CSRF guard admits a request that carries neither `Sec-Fetch-Site` nor `Origin`, which
is what a non-browser client sends; a browser posting cross-site is still refused.

## 8. What this does not do

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

## 9. PortalUser transfer (compare-and-swap)

A second, independent capability on the same daemon: changing WHICH portal
user this linux user answers to, without rewriting host configuration or
running `agentbox apply`. Issue
[#774](https://github.com/defangdevs/agent-box/issues/774). This is what
lets a control plane provision a box once, under an internal pool identity,
and later assign it to the first paying customer by flipping the mapping
alone.

This is not a handover token used differently -- it is a **different
credential for a different caller**. A handover token authorizes a browser
to establish a session for the `sub` it already names; this authorizes
changing what `sub` means on this box at all. Reusing the handover
audience for both would mean a token good for one is good for the other.

### 9.1 The token

Same shape as §2 -- a compact `EdDSA` JWS, verified against the same
`portalIssuer` and the same published key set -- with a claim set that marks
it for this operation and no other:

```json
{
  "iss": "https://station.example.com",
  "aud": "agent-box-portal-user",
  "act": "portal-user-transfer",
  "sub": "usr_customer…",
  "from": "usr_internal_pool…",
  "iat": 1788700000,
  "exp": 1788700060,
  "jti": "01JZ8Q0T5S6P7R8V9W0X1Y2Z3A"
}
```

| claim | required | rule the box enforces |
|---|---|---|
| `iss` | yes | Exact string match against the box's configured issuer -- same check as §2. |
| `aud` | yes | MUST be the literal `agent-box-portal-user`. A handover token's `agent-box` is refused here, and this token is refused at `/auth/handoff`. |
| `act` | yes | MUST be the literal `portal-user-transfer`, so a second kind of token the portal might one day sign under this same audience does not land here by accident. |
| `sub` | yes | The portal user to transfer **to**. 1–256 chars. |
| `from` | yes | The portal user this box must **currently** answer to for the swap to apply. 1–256 chars. |
| `iat`, `exp`, `jti` | yes | Same rules as §2: ≤60 s clock skew, `exp - iat` ≤ 300 s, unique per logical request -- see idempotency below for what "unique" means here. |

Any other claim, including `project`, is ignored: this operation changes an
identity mapping, not a session, and it is not scoped to a project.

### 9.2 Delivery and authentication

```
POST https://<box-domain>/<user>/auth/portal-user
Authorization: Bearer <jwt>
```

**The request body carries no authority and is never parsed** -- it is read
only to keep the connection well-behaved, then discarded. Every fact this
operation acts on (`from`, `to`, its own idempotency key) comes out of the
verified token. A control plane posts no form and no JSON; the bearer token
is the whole request.

Authenticated **only** by that token. Explicitly NOT by any of: the session
cookie a browser holds, this box's HTTP Basic web password, or a handover
token -- even a genuine, current, correctly-signed one, which a different
`aud` makes meaningless here. Like §3, this route sits ahead of the
same-origin/CSRF guard the rest of `/<user>/*` enforces: the caller is a
control plane presenting its own credential, never a browser riding an
ambient one.

### 9.3 Semantics: compare-and-swap, not a write

The box holds one persisted mapping (its current `sub`) plus the `jti` of
the request that last set it. A request is answered from exactly one of
three outcomes, decided while holding an internal lock so two concurrent
requests cannot both believe they won:

| current mapping | this request's `from` | this request's `sub` + `jti` | answer |
|---|---|---|---|
| equals `from` | — | — | **200.** Swap to `sub`, revoke every session minted under `from` (§9.4), record this `jti`. |
| equals `sub` already, same `jti` as the recorded one | — | matches the recorded transfer | **200, no change.** A safe retry of a request already completed -- including one retried because the caller never saw the first answer. |
| anything else | — | — | **409.** The box is not in the state this request assumes. Nothing changes. |

`from == sub` in the same request is refused with **400** before any of the
above: it is not a meaningful no-op swap, and the box does not guess which
case was meant.

Unlike a handover token's `jti` (§4, step 5), this request's `jti` is
**never** burned as spent on its own. Replay protection here is the
idempotency rule above, which deliberately lets the identical logical
request succeed twice -- a control plane that times out waiting for an
answer must be able to retry safely rather than land on a 401 it cannot
distinguish from a real replay.

### 9.4 What a completed transfer does, atomically

1. **Revoke.** Every session record naming the OLD `sub` is deleted, before
   the new mapping is published -- so a crash between the two leaves the
   mapping still on the old identity, and a retry of the same request
   finishes the revoke rather than risk the reverse: a crash AFTER
   publishing that leaves some of the old identity's sessions live under a
   mapping that can no longer mint new ones but never had its existing ones
   cut off.
2. **Publish.** The new mapping is written to disk under the same session
   state directory as §4's sessions and §2.1's key cache -- never to the
   process environment -- and replaces the old one atomically. It is read
   fresh on every request rather than cached at process start, which is
   what makes it effective immediately: no unit restart, no `agentbox
   apply`, and it is exactly as durable as every other record in that
   directory, so it survives both a daemon restart and a host reboot.
3. **Defense in depth.** `portal_session_ok` (§4's session check) also
   verifies a session's `sub` against the CURRENT mapping on every use, not
   only at mint time -- so a session that somehow survived step 1 (a
   handoff racing the transfer in the instant between the revoke scan and
   the publish) is still refused the next time it is presented, rather than
   remaining valid until its natural expiry.

### 9.5 Provisioning

Nothing new to configure. This route exists whenever `portalIssuer` and the
derived or configured JWKS URL are set (§5) -- independent of whether
`portalUser` itself is declared, since bootstrapping the FIRST identity on a
box provisioned for handover but not yet assigned one (`from` the empty
string) is a legitimate use of this same operation. A box with no portal
integration at all serves no `/auth/portal-user` route, exactly as it
serves no `/auth/handoff` route.
