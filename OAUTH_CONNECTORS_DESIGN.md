# Connector OAuth tier — design (v1)

Status: **design, awaiting one decision + one credential handover** (see §8).
Scope: adds a browser "Connect" flow to the connectors feature that already ships in
this branch (paste-a-token). This doc folds in the **OAuth-first, key-fallback**
principle the owner approved and cuts a v1 that can actually ship on *our* timeline.

Companion: `MCP_CONNECTORS_PLAN.md` (the paste-token feature, already built + live on
e2e.localhost). This doc only covers the OAuth addition.

---

## 1. Principle: OAuth-first, key-fallback

For every connector, the connection method is chosen in this order:

1. **OAuth 2.0 / 2.1 authorization-code flow** when the provider offers one. The user
   clicks **Connect**, authorises in the provider's own browser page, and returns; we
   store an access+refresh token pair, never a long-lived secret the user pasted.
2. **API key / PAT** (the shipped path) only when the provider has no OAuth app model,
   or for **service accounts** (a shared, non-human connector a tenant runs on a bot
   identity) and **key-only providers**.

Why OAuth is the default in the agentic era, stated plainly so the doc carries the
rationale:

- **Least privilege** — the consent screen scopes the grant; a PAT is all-or-nothing
  unless the user hand-crafts fine-grained scopes (and most won't).
- **Short-lived credentials** — access tokens expire in minutes/hours and refresh
  silently; a leaked access token is a small blast radius, a leaked PAT is a large one.
- **Central revocation** — the user (or their org admin) revokes our grant from the
  provider's own connected-apps screen, killing every token at once.
- **The raw secret never touches our UI** — with paste-a-token the user copies a
  credential through their clipboard into our form; with OAuth they never do.

This is **complementary** to, not a replacement for, the layers already in the branch:
the bench-broker (token never enters the agent container), the allowed-actions gate, and
confirm-on-writes all still apply to an OAuth connector exactly as to a token one. OAuth
improves *how the credential is obtained and held*; the broker governs *how it is used*.

---

## 2. The engine already exists: Frappe Connected App

Frappe ships a **Connected App** DocType that is a complete OAuth 2.0 authorization-code
client:

- Stores `client_id`, `client_secret`, `authorization_uri`, `token_uri`, `scopes`,
  `redirect_uri`.
- `initiate_web_application_flow(user, ...)` builds the authorize URL.
- A whitelisted **callback** exchanges the `code`, and a **Token Cache** child DocType
  stores the per-user access+refresh token.
- `get_active_token(user)` returns a live token, **auto-refreshing** under a worker lock
  when it is near expiry.

So the OAuth *machinery* is not something we build. What we build is the **wiring**
between a `Jarvis Connector` row and a Connected App, the **tenant-aware callback**, the
**SPA Connect/Reconnect/Revoke UX**, and — only where a provider demands it — a **PKCE
add-on** (see §5).

---

## 3. v1 cut: GitHub as the flagship, via a classic OAuth App

The honest v1 is **the OAuth tier built end-to-end with one flagship provider**, not "all
providers at once." GitHub is the right flagship, but the *path* matters — there are two
GitHub OAuth stories and only one is v1-viable:

| Path | What it is | PKCE? | Extra requirement | v1? |
|---|---|---|---|---|
| **A — OAuth App, token-forward** | Classic GitHub **OAuth App** (authorization-code + `client_secret`). We obtain a user token and forward it in the `Authorization` header to `https://api.githubcopilot.com/mcp/`, exactly as the shipped PAT path does. | **No** | none | **Yes** |
| B — MCP-native OAuth (DCR) | The remote MCP server acts as its own OAuth resource server per the MCP Authorization spec; client does dynamic registration + PKCE. | **Yes (OAuth 2.1)** | **per-user GitHub Copilot license** | No |

Path B needs the PKCE subclass **and** a Copilot license per user — most of our tenants
won't have Copilot, so it's a bad flagship. Path A rides the **existing** paste-token
transport unchanged (the MCP server accepts a forwarded credential in the `Authorization`
header) and the **existing** Frappe Connected App unchanged (classic OAuth App =
`client_secret`, no PKCE). That is the v1.

> **OPEN RISK to close during build (not an assumption to ship on):** confirm the remote
> GitHub MCP server accepts a **GitHub OAuth-App user token** (`gho_…`) forwarded in the
> `Authorization` header the same way it accepts a PAT, *without* a Copilot license. The
> PAT path is documented; the OAuth-App-token-as-bearer path is the specific thing to
> verify with one live `initialize` call before we commit GitHub as the flagship. If it
> turns out the remote server only honours PATs or Copilot-OAuth, the flagship moves to a
> provider whose MCP server takes a plain OAuth bearer (candidates in §7).

Verified facts behind this table (2026-09, GitHub changelog + MCP auth docs):
- Remote GitHub MCP server went GA 2025-09-04; supports **OAuth 2.1 + PKCE** *and*
  **PAT in the `Authorization` header**.
- The **hosted** remote server's *OAuth* path (Path B) requires a **Copilot license**.
- Endpoint: `https://api.githubcopilot.com/mcp/`.

---

## 4. Where OAuth plugs into the built code

One seam. In `jarvis/connectors/broker.py`, `_credential(row)` (≈L181) currently decrypts
the stored credential and hands it to `mcp_client.run_tool(...)` as the bearer. OAuth
changes only *how that string is produced*:

```
_credential(row):
    if row.auth_method == "oauth":
        # resolve the Connected App token for the impersonated user,
        # refreshing PROACTIVELY if it expires within the call budget (see §6)
        return connected_app.get_active_token(frappe.session.user).access_token
    else:
        return row.get_password("credential")   # shipped path, unchanged
```

Everything downstream — SSRF pin, breaker, concurrency cap, 20s budget, audit redaction,
the allowed-actions gate — is untouched. The broker already runs under the impersonated
user (`frappe.session.user` is the real end user), which is exactly the identity the
per-user Token Cache is keyed on, so token resolution is automatically the right user's.

Data-model additions (new tab in `Jarvis Connector`, mirroring the plan's "settings
doctype, SPA-configured" rule):
- `auth_method` — Select `api_key` | `oauth` (default `api_key`).
- `connected_app` — Link to Connected App (set when `auth_method == oauth`).
- The paste-`credential` field stays for the key path.

---

## 5. PKCE add-on (only if/when Path B or a PKCE-only provider is in scope)

Frappe's Connected App does **plain** authorization-code with `client_secret` and **no**
PKCE and no auto-discovery. For v1 (Path A, classic OAuth App) this is sufficient and we
add nothing. When we later add a provider whose MCP server mandates OAuth 2.1 (PKCE
required — e.g. Path B, or a spec-compliant third-party MCP server), we add a **jarvis-app
subclass** of Connected App that:
- generates a `code_verifier`/`code_challenge` pair, stashes the verifier in the flow
  state, and sends `code_challenge` on authorize + `code_verifier` on token exchange;
- (optionally) does the MCP `.well-known/oauth-protected-resource` discovery + dynamic
  client registration the Authorization spec describes.

This is scoped **out of v1** deliberately. Calling it out here so the estimate is honest:
it is real work, gated on a real provider that needs it.

---

## 6. Token refresh inside the sync, deadline-bounded broker

The broker is synchronous (`requests`), IP-pinned, and hard-capped at ~20s per call under
a concurrency slot. `get_active_token` refreshes under a lock and makes a **new egress
call** to the provider's token endpoint. Two rules so refresh doesn't blow the budget or
the SSRF guard:

1. **Refresh proactively, before the slot, not mid-call.** Resolve the token in
   `_credential` *before* entering the concurrency `cap.slot()` and before the MCP call
   starts, and refresh if it expires within a margin (say < 120s). This keeps the
   refresh round-trip out of the 20s tool budget and out of the breaker's failure
   accounting — a refresh failure is an **auth** problem (`connector_not_ready` /
   re-consent), never an endpoint-health signal that should open the circuit.
2. **Route the token-endpoint call through the same egress/SSRF guard.** The refresh POST
   to the provider's `token_uri` is outbound egress like any other; it must not bypass
   `_egress_allowed` + the IP-pin. (Provider token hosts are public and will pass, but
   the guard must see them — no silent side channel.)

A reactive fallback (call → 401 → refresh once → retry) may be added, but only if the
single retry + refresh still fits a *fresh* 20s budget; proactive refresh is the primary
path precisely so we rarely hit it.

---

## 7. Tenants are host-routed → one central callback that bounces

Sites are Host-header-routed on a single server (`site.jarvis`, `jarvis.proxy`, per-tenant
hosts). A provider OAuth app registers a **fixed** set of redirect URIs; we can't register
one per tenant. So:

- Register **one** central callback URL (on a stable host we control) with each provider.
- Carry the tenant identity + a CSRF nonce in the OAuth **`state`** parameter.
- The central callback validates `state`, then **bounces** the `code` to the originating
  tenant's own whitelisted callback (or completes the exchange centrally and writes the
  Token Cache into the right tenant DB — decide during build; the bounce keeps token
  storage tenant-local, which is cleaner for isolation).

This is the piece with no existing implementation and the main integration risk after the
Path-A token-forward question. It is bounded and ours to build (no external dependency).

---

## 8. What v1 needs from the owner (blocking)

1. **Decision — flagship confirmation.** Ship v1 OAuth with **GitHub via Path A**
   (recommended), pending the §3 open-risk check? If the check fails, I fall back to a
   provider from §7's candidate list rather than widening scope.
2. **Credential handover — I cannot register OAuth apps.** For GitHub Path A you (owner)
   create a **GitHub OAuth App** and give me: `client_id`, `client_secret`, and you
   register the **redirect URI** I specify (the central callback from §7). Same shape for
   any later provider.

Explicitly **out of v1**, and why: **Gmail / Google Workspace** connectors. Google
restricted-scope OAuth requires **CASA security assessment + app verification**, a
weeks-long process on **Google's** clock, not ours; and Gmail's remote MCP is still draft
with no send tool. OAuth-first means Gmail is OAuth *when it lands* — it just can't be in
this v1.

---

## 9. Estimate (honest)

- Data-model + `_credential` OAuth branch + proactive refresh wiring: ~1 day.
- Central callback + `state`-bounce + tenant routing: ~1.5–2 days (the unknown).
- SPA Connect / Reconnect / Revoke UX (frappe-ui, matching the shipped ConnectorsPane):
  ~1 day.
- The §3 open-risk live check + GitHub OAuth App setup + e2e on e2e.localhost: ~0.5 day.

**~4 developer-days**, contingent on the §8 handover and the §3 check passing. This is a
multi-day feature, not a tweak — flagged so it isn't mistaken for one. The PKCE subclass
(§5) and additional providers are **not** in this estimate.

---

## 10. Sequence

1. Owner answers §8 (decision + credential handover start).
2. Close the §3 open risk with one live `initialize` call.
3. Build data-model + broker seam + refresh (§4, §6).
4. Build central callback + bounce (§7).
5. Build SPA Connect/Reconnect/Revoke (§8 shape).
6. e2e on e2e.localhost against the real GitHub OAuth App; then PR.
