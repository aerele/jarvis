# Connector providers — live probe sweep (2026-09-05)

Each candidate remote MCP server was probed exactly the way the discovery engine
does it: unauthenticated `initialize` → `401 WWW-Authenticate resource_metadata` →
RFC 9728 protected-resource metadata → RFC 8414 / OIDC authorization-server metadata
→ `registration_endpoint` present or not. The probe is the truth; re-run
`probe_sweep.py` (job tmp) to refresh. Classes map 1:1 onto the connection flows the
engine supports.

## Sign-in, zero setup (DCR: the auth server self-registers): 13

Re-verified 2026-09-06 with a REAL registration from an e2e tenant (client name
`Jarvis (<site host>)`, callback on the tenant's own host): every row below issued a
client id.

| Provider | MCP endpoint | Auth server | PKCE |
|---|---|---|---|
| Atlassian (Jira, Confluence) | `https://mcp.atlassian.com/v2/mcp` | auth.atlassian.com | S256 |
| Canva | `https://mcp.canva.com/mcp` | mcp.canva.com | S256 |
| Cloudflare (bindings) | `https://bindings.mcp.cloudflare.com/mcp` | bindings.mcp.cloudflare.com | S256 |
| Linear | `https://mcp.linear.app/mcp` | mcp.linear.app | S256 |
| Neon | `https://mcp.neon.tech/mcp` | mcp.neon.tech | S256 |
| Netlify | `https://netlify-mcp.netlify.app/mcp` | netlify-mcp.netlify.app | S256 |
| Notion | `https://mcp.notion.com/mcp` | mcp.notion.com | S256 |
| PayPal | `https://mcp.paypal.com/mcp` | mcp.paypal.com | S256 |
| Razorpay | `https://mcp.razorpay.com/mcp` | mcp.razorpay.com | S256 |
| Sentry | `https://mcp.sentry.dev/mcp` | mcp.sentry.dev | S256 |
| Supabase | `https://mcp.supabase.com/mcp` | api.supabase.com | S256 |
| Webflow | `https://mcp.webflow.com/mcp` | mcp.webflow.com | S256 |
| Wix | `https://mcp.wix.com/mcp` | mcp.wix.com | S256 |

Several also advertise `plain` PKCE; the engine always sends S256 (spec-mandated).

Two things the live registration taught the engine (both fixed 2026-09-06):
- **Atlassian** answers a bare HTTP 400 to a registration with no `client_name`; the
  engine now always sends one (`<brand> (<site host>)`) plus `client_uri` when https.
- **Razorpay and Asana** declare `resource` at ORIGIN level (`https://mcp.razorpay.com`)
  for an endpoint at `/mcp`; the RFC 9728 gate now accepts a same-origin path-prefix
  declaration (`canonical.resource_covers`), as the reference client SDK does. Asana's
  401 also points at the ROOT well-known document, not `/.well-known/.../mcp`.

## Sign-in advertised, but closed to third-party apps: 5 (listed, `enabled=False`)

Each advertises a registration endpoint, and each refuses a self-hosted tenant:

| Provider | MCP endpoint | What it answers |
|---|---|---|
| Asana | `https://mcp.asana.com/mcp` | `invalid_redirect_uri` ("not allowed") for any public host, port or not; `localhost` callbacks register, so only local assistants and its approved hosted ones can sign in |
| Square | `https://mcp.squareup.com/mcp` | `invalid_redirect_uri` for any public host; `localhost` callbacks are accepted, so only local assistants and its approved hosted ones can sign in |
| Figma | `https://mcp.figma.com/mcp` | `403 Forbidden` from `api.figma.com/v1/oauth/mcp/register` for every request shape (public or confidential, any user agent) |
| Dropbox | `https://mcp.dropbox.com/mcp` | `registration_not_supported`: "only pre-registered MCP trusted partners are allowed" |
| Vercel | `https://mcp.vercel.com/` | `invalid_redirect_uri`: "redirect URIs are not approved for use by this authorization server" |

They stay in the catalog (logos, endpoints) switched off, like Plaid, so turning one
on when its vendor opens registration is a one-line change.

## Sign-in, one-time app registration (static — auth server has no self-registration) — 5

| Provider | MCP endpoint | Auth server | Note |
|---|---|---|---|
| GitHub | `https://api.githubcopilot.com/mcp/` | github.com | static / bring-your-own-app; endpoints pinned in catalog, client seeded without discovery. No AS metadata doc. |
| Slack | `https://mcp.slack.com/mcp` | mcp.slack.com | S256; has metadata, no registration endpoint |
| Box | `https://mcp.box.com/mcp` | api.box.com | S256; no registration endpoint |
| Airtable | `https://mcp.airtable.com/mcp` | airtable.com | no AS metadata doc |
| Monday.com | `https://mcp.monday.com/mcp` | auth.monday.com | no AS metadata doc |

An admin registers one app per provider and pastes client id/secret into Jarvis
(`set_oauth_client_credentials`); the engine handles the rest.

## Token only (401, no spec discovery) — 5

| Provider | MCP endpoint |
|---|---|
| Stripe | `https://mcp.stripe.com/` (restricted API key; Stripe's recommended server-side model) |
| Intercom | `https://mcp.intercom.com/mcp` |
| Zapier | `https://mcp.zapier.com/api/mcp/mcp` (per-user server URL/key) |
| Zendesk | `https://mcp.zendesk.com/mcp` |
| Plaid | `https://api.dashboard.plaid.com/mcp/sse` (SSE transport; verify Streamable HTTP support before listing) |

## Open (no credential needed) — 3

| Provider | MCP endpoint | What it is |
|---|---|---|
| Microsoft Learn | `https://learn.microsoft.com/api/mcp` | public docs |
| Cloudflare Docs | `https://docs.mcp.cloudflare.com/mcp` | public docs |
| Hugging Face | `https://huggingface.co/mcp` | public hub |

Open servers need a **no-credential** path (the broker already sends no
`Authorization` header when the credential is empty; the SPA must skip the token field).

## Not reachable at the probed URL (wrong URL or no public server) — 4

DocuSign (403), HubSpot (404), Shopify Dev (404), Twilio (404). Re-probe with vendor-confirmed
URLs before listing; do not guess.

## Notes for the preset catalog (Phase D)

- The preset's `auth` class drives the SPA: `dcr` → Connect directly (no Check step
  needed); `static` → admin app-credentials block then Connect; `token` → token field;
  `open` → no credential field.
- ERPNext-relevant picks by category (offered today; the five closed sign-in vendors
  above are listed but off): payments (Razorpay, PayPal, Stripe), work (Atlassian,
  Linear, Notion, Monday.com, Slack), files (Box), design (Canva), support (Intercom,
  Zendesk), data/infra (Supabase, Neon, Airtable, Sentry, Cloudflare, Netlify), web
  (Webflow, Wix), automation (Zapier), docs (Microsoft Learn, Cloudflare Docs, Hugging
  Face).
- India-specific gaps with no public MCP today (GST/IRP, Tally, Shiprocket, WhatsApp
  Business) are candidates for our own MCP servers — a separate project.
