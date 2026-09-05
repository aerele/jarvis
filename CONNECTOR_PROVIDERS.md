# Connector providers — live probe sweep (2026-09-05)

Each candidate remote MCP server was probed exactly the way the discovery engine
does it: unauthenticated `initialize` → `401 WWW-Authenticate resource_metadata` →
RFC 9728 protected-resource metadata → RFC 8414 / OIDC authorization-server metadata
→ `registration_endpoint` present or not. The probe is the truth; re-run
`probe_sweep.py` (job tmp) to refresh. Classes map 1:1 onto the connection flows the
engine supports.

## Sign-in, zero setup (DCR — the auth server self-registers) — 18

| Provider | MCP endpoint | Auth server | PKCE |
|---|---|---|---|
| Asana | `https://mcp.asana.com/mcp` | mcp.asana.com | S256 |
| Atlassian (Jira, Confluence) | `https://mcp.atlassian.com/v2/mcp` | auth.atlassian.com | S256 |
| Canva | `https://mcp.canva.com/mcp` | mcp.canva.com | S256 |
| Cloudflare (bindings) | `https://bindings.mcp.cloudflare.com/mcp` | bindings.mcp.cloudflare.com | S256 |
| Dropbox | `https://mcp.dropbox.com/mcp` | www.dropbox.com | S256 |
| Figma | `https://mcp.figma.com/mcp` | api.figma.com | S256 |
| Linear | `https://mcp.linear.app/mcp` | mcp.linear.app | S256 |
| Neon | `https://mcp.neon.tech/mcp` | mcp.neon.tech | S256 |
| Netlify | `https://netlify-mcp.netlify.app/mcp` | netlify-mcp.netlify.app | S256 |
| Notion | `https://mcp.notion.com/mcp` | mcp.notion.com | S256 |
| PayPal | `https://mcp.paypal.com/mcp` | mcp.paypal.com | S256 |
| Razorpay | `https://mcp.razorpay.com/mcp` | mcp.razorpay.com | S256 |
| Sentry | `https://mcp.sentry.dev/mcp` | mcp.sentry.dev | S256 |
| Square | `https://mcp.squareup.com/mcp` | mcp.squareup.com | S256 |
| Supabase | `https://mcp.supabase.com/mcp` | api.supabase.com | S256 |
| Vercel | `https://mcp.vercel.com/` | vercel.com | S256 |
| Webflow | `https://mcp.webflow.com/mcp` | mcp.webflow.com | S256 |
| Wix | `https://mcp.wix.com/mcp` | mcp.wix.com | S256 |

Several also advertise `plain` PKCE; the engine always sends S256 (spec-mandated).

## Sign-in, one-time app registration (static — auth server has no self-registration) — 5

| Provider | MCP endpoint | Auth server | Note |
|---|---|---|---|
| GitHub | `https://api.githubcopilot.com/mcp/` | github.com | v1 path (Connected App). No AS metadata doc. |
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
- ERPNext-relevant picks by category: payments (Razorpay, PayPal, Square, Stripe), work
  (Atlassian, Linear, Asana, Notion, Monday.com, Slack), files (Dropbox, Box), design
  (Canva, Figma), support (Intercom, Zendesk), data/infra (Supabase, Neon, Airtable,
  Sentry, Cloudflare, Vercel, Netlify), web (Webflow, Wix), automation (Zapier), docs
  (Microsoft Learn, Cloudflare Docs, Hugging Face).
- India-specific gaps with no public MCP today (GST/IRP, Tally, Shiprocket, WhatsApp
  Business) are candidates for our own MCP servers — a separate project.
