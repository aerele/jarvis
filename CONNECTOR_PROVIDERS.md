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

## Corrected on 2026-09-07 (the 09-05 sweep had the wrong URL for these)

| Vendor | Real server | Now |
|---|---|---|
| HubSpot | `https://mcp.hubspot.com` (the ORIGIN; `/mcp` is 404) | listed, static |
| Docusign | `https://mcp.docusign.com/mcp` | listed, static, pinned endpoints |
| Shopify | `https://setup.shopify.com/mcp` | listed, static, pinned endpoints, OFF |
| Twilio | `https://mcp.twilio.com/docs` is a public-docs server only (no account data) | not listed |

## Notes for the preset catalog (Phase D)

- The preset's `auth` class drives the SPA: `dcr` → Connect directly (no Check step
  needed); `static` → admin app-credentials block then Connect; `token` → token field;
  `open` → no credential field.
- ERPNext-relevant picks by category (offered today; the closed sign-in vendors and
  Shopify are listed but off): payments (Razorpay, PayPal, Stripe, Cashfree), accounting
  (Xero), crm (HubSpot, Pipedrive), work (Atlassian, Linear, Notion, Monday.com, Slack,
  ClickUp, Docusign), communication (Gmail, Google Calendar, Calendly, Zoom, Mailchimp
  Transactional), files (Box, Google Drive, Google Sheets, Google Docs), design (Canva),
  support (Intercom, Zendesk), data/infra (Supabase, Neon, Airtable, Sentry, Cloudflare,
  Netlify), web (Webflow, Wix), automation (Zapier), docs (Microsoft Learn, Cloudflare
  Docs, Hugging Face), dev (GitHub, GitLab).
- India-specific gaps with no public MCP today (GST/IRP, Tally, Shiprocket, WhatsApp
  Business) are candidates for our own MCP servers, a separate project. Cashfree closed
  its gap on its own (self-registering server, listed 2026-09-07).

# Gap analysis additions (2026-09-07)

Read-only probe of the vendors the 09-05 sweep missed, same hops as the engine
(unauthenticated `initialize`, RFC 9728 resource document, RFC 8414/OIDC metadata, is
there a `registration_endpoint`). Classes below are what the metadata SAYS; only the
09-06 rows above have been proven by a real registration from a tenant host.

## Sign-in, self-registering per the metadata: 5 (tenant-host registration NOT yet proven)

The 09-06 lesson applies: Asana, Square and Vercel advertised a registration endpoint
too and refused every public callback. These five ship enabled because a refusal
surfaces as an inline `registration_failed` on Connect (the row is discarded), never
as a broken row; run the throwaway-registration sweep from a tenant host before
relying on them, and switch off any that refuse.

| Provider | MCP endpoint | Auth server | Note |
|---|---|---|---|
| Pipedrive | `https://mcp.pipedrive.ai/mcp` | oauth.pipedrive.com | every plan |
| ClickUp | `https://mcp.clickup.com/mcp` | mcp.clickup.com | OAuth only, public beta, per-plan daily call caps |
| Calendly | `https://mcp.calendly.com/` | calendly.com | server is the origin; `/mcp` is 404 |
| GitLab | `https://gitlab.com/api/v4/mcp` | gitlab.com | gitlab.com only; a self-hosted instance is a Custom URL on the same path |
| Cashfree | `https://mcp.cashfree.com/mcp` | mcp.cashfree.com | 401 with an EMPTY `WWW-Authenticate`; the path-derived well-known document exists |

Monday.com (listed as static on 09-05) now ALSO advertises
`https://auth.monday.com/oauth_ms/oauth/register`. Its class is left as shipped;
discovery already self-registers there at connect time whatever the class says, and
flipping the class only changes the dialog's copy. Flip it once a tenant-host
registration has been seen to succeed.

## Sign-in, one-time app registration (static): 10

| Provider | MCP endpoint | Auth server | Note |
|---|---|---|---|
| HubSpot | `https://mcp.hubspot.com` | mcp.hubspot.com | discovery works; HubSpot's guide has each customer create an "MCP auth app"; PKCE mandatory |
| Xero | `https://mcp.xero.com/mcp` | identity.xero.com | discovery works; the resource document names the accounting scopes and discovery asks for exactly those |
| Zoom | `https://mcp.zoom.us/mcp/zoom/streamable` | zoom.us | discovery works; Zoom's docs: manual app registration only, no DCR |
| Docusign | `https://mcp.docusign.com/mcp` | account.docusign.com | unauth initialize is 403 (no 401 gate), so endpoints are PINNED; only the `signature` scope is requested (the resource also advertises Navigator `adm_store_unified_repo_read` and Maestro `aow_manage`, which may fail consent on accounts without them); developer accounts use `mcp-d` / `account-d`, not covered |
| Shopify | `https://setup.shopify.com/mcp` | setup.shopify.com/auth | unauth initialize is 403, endpoints PINNED, **OFF**: Shopify documents no third-party client path for this server (its agent docs cover only the buyer-facing Cart / Checkout / Order servers), so whether a merchant's own Dev Dashboard app may sign in is unproven |
| Gmail | `https://gmailmcp.googleapis.com/mcp/v1` | accounts.google.com | see Google note |
| Google Calendar | `https://calendarmcp.googleapis.com/mcp/v1` | accounts.google.com | see Google note |
| Google Drive | `https://drivemcp.googleapis.com/mcp/v1` | accounts.google.com | see Google note |
| Google Sheets | `https://sheetsmcp.googleapis.com/mcp/v1` | accounts.google.com | see Google note |
| Google Docs | `https://docsmcp.googleapis.com/mcp/v1` | accounts.google.com | see Google note |

**Google note.** Every Workspace MCP server answers an unauthenticated `initialize` with
HTTP 200 (no 401 challenge), so the engine's first discovery gate refuses it; each
Google preset therefore PINS `accounts.google.com` endpoints and seeds its client like
GitHub. Google issues NO refresh token unless the authorize request carries
`access_type=offline` and `prompt=consent`, so those ride on the new catalog field
`authorize_params` (reviewed constants, appended by `flow.build_authorize_url`, never a
reserved parameter). Scopes follow Google's own per-server guide, plus
`calendar.events` on Calendar so the agent can create events. The servers are part of
the Google Workspace Developer Preview: the customer registers an OAuth client in their
own Cloud project (an External app in Testing mode works for its listed test users
without the CASA verification a published app with Gmail scopes needs). Slides, Chat and
People servers exist at the same pattern and are not listed.

## Token only: 1

| Provider | MCP endpoint | Note |
|---|---|---|
| Mailchimp Transactional | `https://mandrillapp.com/mcp` | Mailchimp's only official server; the marketing API (audiences, campaigns) has none |

## Looked at and not listed

| Vendor | Why |
|---|---|
| LinkedIn | no first-party server (partner-gated API, nothing announced as of 2026-07); Zapier (listed) exposes two write actions; Taplio hosts a third-party DCR server, a data processor over customer data, so it is Custom URL only if the admin allows |
| QuickBooks | `https://ai-inc.quickbooks.intuit.com/v1/mcp` answered an Akamai 403 from every egress tried (two user agents); auth class unknown, re-probe from staging before listing |
| Twilio | hosted server is public docs only |
| Zoho (Books, CRM, Mail) | per-organisation URL generated in the Zoho MCP console, OAuth 2.1: Custom URL + sign-in |
| Salesforce | `https://api.salesforce.com/platform/mcp/v1/{server}` per org, External Client App consumer key, Enterprise+: Custom URL + sign-in (a public client, blank secret) |
| Freshdesk | `https://<domain>/mcp`, API key, Enterprise early access: Custom URL + token |
| Xero self-hosted, Cashfree npm, Tally community servers | stdio; out of scope by design |
| WhatsApp Business, Amazon Seller, Tally, Shiprocket, GST | no first-party or hosted server |
