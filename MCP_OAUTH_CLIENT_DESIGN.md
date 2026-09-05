# Spec-compliant MCP OAuth client (bench) — design

Status: **design, awaiting Kavin's nod on scope + one small decision (§9).**
Supersedes the "PKCE add-on" sketch in `OAUTH_CONNECTORS_DESIGN.md` §5. This is the
engine for connecting to **arbitrary remote MCP servers** (the Connector Directory,
task #5), built to the MCP Authorization spec. GitHub v1 (already built, reviewed) is a
special case of it and is **not** reworked here.

Grounded in the **2026-07-28** MCP Authorization spec (not 2025-06-18 — the registration
story changed) and a live probe of GitHub's remote MCP (2026-09-05, results in §4).

---

## 1. Who is the "client"? The bench, not the container.

In our architecture the **bench broker** is the MCP client (it makes the real MCP call
under the impersonated user); the agent container only holds `call_connector`. The
2026-07-28 client-best-practices page blesses exactly this: *"the host acts as a broker…
Authorization tokens and credentials are held by the host and never exposed to the
generated code."* So the OAuth client logic lives in the bench, alongside `mcp_client`.

## 2. What the 2026-07-28 spec mandates (the delta that matters)

The registration story **changed** from 2025-06-18:

| Mechanism | 2025-06-18 | 2026-07-28 |
|---|---|---|
| **Client ID Metadata Documents (CIMD)** | — | **SHOULD support — the preferred path** |
| Pre-registered (static) client | fallback | supported |
| **Dynamic Client Registration (RFC 7591)** | SHOULD | **MAY, "deprecated, retained for backwards compatibility"** |

So the instinct to "implement DCR" resolves to **implement CIMD first**, keep static for
providers like GitHub, and treat DCR as a deprecated fallback. Priority order per the spec:
**CIMD → pre-registered → DCR.**

Everything else the client MUST do (unchanged or new in 2026-07-28):
- **OAuth 2.1 + PKCE** — mandatory for every client (`S256`).
- **Discovery** — on 401, parse `WWW-Authenticate` → fetch RFC 9728 protected-resource
  metadata → read `authorization_servers` → fetch AS metadata via **RFC 8414 *or* OpenID
  Connect Discovery** (client MUST support both).
- **Resource Indicators (RFC 8707)** — `resource` param on auth + token requests, set to
  the MCP server's canonical URI (no trailing slash), always sent.
- **RFC 9207 issuer validation (NEW)** — record the AS `issuer` from validated metadata;
  on the auth-code callback, validate the `iss` param against it *before* the token
  exchange (mix-up-attack defense). On mismatch, MUST NOT act on or display the response.
- **Scope from the challenge** — use `scope` in the 401 `WWW-Authenticate`, else
  `scopes_supported`; least privilege; step-up on `insufficient_scope` (403) with scope
  union on re-auth.
- **Refresh** — keep confidential; MAY request `offline_access`; MUST NOT assume a refresh
  token is issued; public clients get rotating refresh tokens.
- **No token passthrough** — the token we send to an MCP server MUST be one issued by *that
  server's* AS; never forward a token across resources.

## 3. Client ID Metadata Documents — why this is the unlock

CIMD lets us **use an HTTPS URL as our `client_id`**. Jarvis hosts **one** client-metadata
JSON at a stable URL (e.g. `https://jarvis.aerele.in/.well-known/mcp-client`); any
CIMD-supporting AS fetches + validates it (redirect_uris included) at authorize time. That
**collapses the multi-tenant registration problem** from `OAUTH_CONNECTORS_DESIGN.md` §7:
no per-tenant DCR, no per-tenant redirect registration — every tenant presents the same
client-id URL, and the metadata doc declares the one central callback. For AS that don't
do CIMD, we fall back to static (admin-entered) or, last, DCR.

## 4. GitHub, grounded by a live probe (2026-09-05)

`POST https://api.githubcopilot.com/mcp/` unauth returns:
```
401  WWW-Authenticate: Bearer error="invalid_request",
     resource_metadata="https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"
```
That metadata document:
```json
{ "resource": "https://api.githubcopilot.com/mcp/",
  "authorization_servers": ["https://github.com/login/oauth"],
  "scopes_supported": ["repo","read:org","read:user",...],
  "bearer_methods_supported": ["header"] }
```

What this settles:
- GitHub's MCP **is spec-compliant on discovery** (RFC 9728). Its AS is
  **`https://github.com/login/oauth`** — GitHub's own OAuth-App system, the exact
  `authorization_uri`/`token_uri` our v1 already uses.
- Therefore a `gho_` token from our classic OAuth App **is a token issued by the MCP
  server's own AS** — v1 is the spec's **pre-registered (static) client** path, *not* a
  foreign/passthrough token. **v1 stands, spec-aligned.**
- GitHub's AS serves **no** RFC 8414 metadata document and **no** registration endpoint
  (probed: the well-known AS URL returns nothing). So GitHub is **static-client only** —
  CIMD/DCR do not apply to it.
- One improvement v1 should still take: **add PKCE** to the GitHub authorize/token calls.
  GitHub OAuth Apps accept `code_challenge`; the spec requires the client to send it. That
  is the "PKCE for more secure" the owner asked for, applied to v1.

## 5. Data model — own it; never auto-create Connected App from untrusted discovery

Dynamic per-server registration does not fit an admin-created-per-provider Connected App.
Two new DocTypes:

- **`MCP OAuth Client`** — one per `(connector, tenant)`. Holds: registration mode
  (`cimd` | `static` | `dcr`), the issued/declared `client_id` (a URL for CIMD), encrypted
  `client_secret` (static/DCR only), a DCR registration access token, the **validated**
  discovered endpoints + AS metadata snapshot (`issuer`, authorize/token/registration
  endpoints, `scopes_supported`), and the PKCE method. Server-controlled; never populated
  from client input.
- **`MCP OAuth Token`** — one per `(connector, user)`. Encrypted access + refresh token,
  expiry, granted scopes, the bound `resource`. Per-user isolation, exactly like the v1
  Token Cache model.

Frappe's Connected App path (v1 GitHub) is **left untouched**; unifying the two behind one
broker seam is a follow-up, not this project.

## 6. Security — the new attack surface, closed

Discovery means an **untrusted, user-added server URL now tells us where its auth server
is**. Every hop is egress to an attacker-influenced host, so:

- **Every HTTP hop through the SSRF-guarded, IP-pinned client** (`mcp_client`/`ssrf`): the
  9728 GET, the 8414/OIDC GET, the CIMD fetch is the AS's problem but our `/register` POST,
  token POST, and refresh POST are ours. Do **not** use `requests_oauthlib` for the network
  (it is plain `requests` and bypasses the pin). PKCE is `secrets.token_urlsafe(64)` +
  `S256 = base64url(sha256(verifier))` — three lines; no oauthlib pulled in for transport.
- **Validation gates:**
  - protected-resource `resource` **MUST equal** the connector's canonical `base_url`
    (RFC 9728 §3.3);
  - AS metadata `issuer` **MUST equal** the URL it was fetched from (RFC 8414 §3.3), and is
    recorded for the RFC 9207 `iss` check on callback;
  - every AS endpoint **MUST be HTTPS**; redirect URIs HTTPS or localhost, exact-match;
  - `state` single-use, short TTL, bound server-side to `(tenant, user, connector,
    code_verifier, issuer)`;
  - **`resource` on the wire vs. the pin**: the gate and the token pin compare **canonical**
    forms (lowercase scheme/host, no trailing slash), but the value **sent** in authorize /
    token / refresh is the server's **own declared** `resource` string from its metadata
    (RFC 9728 §3.3 is what the resource server validates against) — so a server that
    declares `.../mcp/` gets `.../mcp/`.
- **Show the user the AS host before redirecting** — "This app signs you in at
  *github.com*" — because the server they pasted chose it. Confused-deputy defense.
- **No passthrough**, **redacted logging** (Fable's v1 finding — a client_secret must never
  reach the Error Log — now applies to *our* code, since refresh is ours).

## 7. Broker seam + refresh

`broker._credential` gains one more branch (or `auth_method` a third value): resolve the
`MCP OAuth Token` for the current user, refresh proactively **before** the concurrency slot
if expired and a refresh token exists, through the pinned client within the time budget; a
failure is `connector_not_ready` (re-consent), never a breaker signal. Allowed-actions gate,
confirm-on-writes, breaker, cap, audit — all untouched.

## 8. UX (frappe-ui, quiet copy, no protocol words)

Paste a server URL → we probe (401 → discovery) → "This app needs sign-in at *<host>*" →
**Connect** (browser) → callback → **Test**. Same design language as the shipped
ConnectorsPane; one heading + one line per section.

## 9. Scope + the one decision

- **In scope:** the CIMD-first spec-compliant client (discovery, PKCE, resource, 9207,
  scope/step-up, refresh), the two DocTypes, the broker seam, the SSRF-wrapped transport,
  the Connect/Disconnect UX for a **custom server URL**. This is the Connector Directory's
  auth engine.
- **v1 GitHub:** untouched here, except a small, safe **PKCE add-on** to its existing
  authorize/token calls (owner's "OAuth 2.1 + PKCE" ask, applied where it ships first).
- **Decision (settled 2026-09-05): Static + DCR now, CIMD later.** The engine leads with
  **DCR** (RFC 7591 self-registration) for arbitrary servers whose AS supports it, and
  **static** (admin-entered client_id/secret) for GitHub-like AS. **CIMD** is built as a
  clean third registration mode **later**, once there is a public Jarvis host to serve the
  `/.well-known/mcp-client` metadata document — no rework, the registration layer is
  designed for three modes from the start. Rationale: CIMD is draft-00 with almost no AS
  adoption today, while DCR (though "deprecated") is what real MCP servers actually support
  now; static-first + DCR lands a working, spec-compliant engine without a hosting
  dependency.

## 10. Estimate + sequence

Multi-day security subsystem (discovery + PKCE + 9207 + three registration modes + two
DocTypes + SSRF-wrapped transport + refresh + UX). Rough order:
1. Kavin's nod on scope + §9 (host CIMD now vs static-first).
2. Spec-review this doc with a fresh Fable pass before building.
3. Separate branch/PR from v1 (v1 is mergeable on its own).
4. Build with subagents; **two** fresh-context Fable passes, same rigor as v1.
5. e2e against a real CIMD/static server + GitHub (with PKCE) on e2e.localhost.
