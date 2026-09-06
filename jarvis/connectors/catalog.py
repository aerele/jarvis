"""In-app connector provider catalog (frappe-free): the single source of truth
for MCP connector presets, their names, pinned endpoints, auth flow, logo and
credential-entry help copy.

Design:

  * This is a baseline catalog. It ships INSIDE the app, in code, reviewed
    like any other change. It is public data (no secrets ever live here) but
    it is also an ALLOWLIST: it decides which endpoint a saved connector's
    credential is ever allowed to be sent to. That is why it belongs behind
    code review rather than a database row an admin edits unsupervised.
  * A `base_url` is a fixed vendor endpoint, `auth` picks one of the four
    connection flows the discovery engine supports, and `logo` / `help_url` /
    `hint` are display copy. None of that is secret. What IS secret, a static
    preset's registered client id/secret, or a user's own bearer token, never
    lives in this module or its overlay; those are stored per tenant on the
    connector's own sign-in client or the connector row's own credential field.
  * `apply_overlay` is the seam for a later admin-pushed extension of this
    list. An overlay may only disable a shipped entry or add a brand-new one;
    it can never change what an existing name already means (its key,
    base_url, auth or category), because an admin push that silently
    redirected an existing preset's traffic would defeat the allowlist.
    Implemented and tested here; nothing calls it yet.

Catalog order: the four presets already shipping (GitHub, Atlassian, Linear,
Stripe) come first in their existing order, so an already-familiar dropdown
does not reshuffle under an existing user. The remaining entries are grouped
by category in the order payments, work, files, design, support, data, web,
automation, docs, matching how they were picked for ERPNext relevance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

AUTH_DCR = "dcr"
AUTH_STATIC = "static"
AUTH_TOKEN = "token"
AUTH_OPEN = "open"

_ALLOWED_AUTH = frozenset({AUTH_DCR, AUTH_STATIC, AUTH_TOKEN, AUTH_OPEN})

#: The pinned sign-in endpoints a metadata-less static provider declares: a
#: provider whose sign-in service publishes no discovery document names its own
#: issuer / authorization_endpoint / token_endpoint here so a client can be
#: seeded from the reviewed catalog instead of running discovery. Only a
#: `static` provider may carry them, and each must be https - a preset that could
#: redirect a sign-in anywhere is exactly what the catalog allowlist exists to
#: prevent.
_ENDPOINT_FIELDS = ("issuer", "authorization_endpoint", "token_endpoint")
_ENDPOINT_AUTHS = frozenset({AUTH_STATIC})

#: `scopes` names the permissions a sign-in asks for, so it only means anything on
#: a provider that HAS a sign-in: a `static` preset (its own default) or a `dcr`
#: preset (passed through to registration). A `token` / `open` preset has no
#: sign-in to scope, so a scope on one is a catalog mistake the allowlist rejects.
_SCOPES_AUTHS = frozenset({AUTH_STATIC, AUTH_DCR})

_ALLOWED_CATEGORIES = frozenset(
	{
		"payments",
		"work",
		"files",
		"design",
		"support",
		"data",
		"web",
		"automation",
		"docs",
		"dev",
	}
)

_KEY_RE = re.compile(r"^[a-z0-9_-]+$")

# The free-form path: a caller's own base_url, gated separately by the
# `allow_custom_urls` site setting. Not a Provider, so it can never end up in
# PROVIDERS or leak through `by_name`.
CUSTOM_URL = "Custom URL"


@dataclass(frozen=True)
class Provider:
	"""One connector preset. `name` is the display name and IS the `preset`
	value a saved `Jarvis Connector` row stores; `key` is the short slug the
	agent addresses it by.

	The `issuer` / `authorization_endpoint` / `token_endpoint` / `scopes` fields are
	the metadata-less-static seam: a provider whose sign-in service publishes no
	discovery document declares its endpoints (and a default `scopes`) here, so
	`_setup_mcp_oauth_client` can seed the client from the catalog instead of
	running discovery. They are OPTIONAL and only ever set on a `static` provider
	(`validate` enforces that, all three endpoints together or none), so every
	self-registering (`dcr`) preset and every `static` preset that DOES publish
	metadata (Slack, Box, ...) leaves them None and keeps the discovery path
	unchanged.

	`token_hint` / `token_help_url` are the paste-a-token guidance shown when a user
	picks "use a token instead" of a sign-in. They are display copy allowed on ANY
	auth class (`validate` only requires `token_help_url` be https), kept separate
	from `hint` / `help_url` because on a `static` (bring-your-own-app) preset those
	now carry the register-your-own-app guide, not token guidance. A `dcr` preset
	(Atlassian, Linear) self-registers its app, so its `hint` / `help_url` are
	unaffected and still carry token guidance.

	`description` is one short plain line naming what the app is for (its data, not
	a protocol), shown under the name in the SPA's preset picker. `validate` requires
	it non-empty, and `to_public` ships it. It has a `""` default only so `replace`
	and an overlay's new-entry branch have a value to fall back to; a shipped entry
	that left it blank would fail `validate` at import."""

	name: str
	key: str
	base_url: str
	auth: str
	category: str
	logo: str | None
	help_url: str | None
	hint: str | None
	description: str = ""
	enabled: bool = True
	issuer: str | None = None
	authorization_endpoint: str | None = None
	token_endpoint: str | None = None
	scopes: str | None = None
	token_hint: str | None = None
	token_help_url: str | None = None


def validate(providers: tuple[Provider, ...]) -> None:
	"""Raise ``ValueError`` on the first invariant a catalog violates: a
	duplicate name, a duplicate or malformed key, a non-https base_url, or an
	auth/category outside the allowed sets. Called at import time on
	``PROVIDERS`` (a bad shipped catalog must fail loudly in CI, not at
	runtime) and again inside ``apply_overlay`` on its result."""
	seen_names: set[str] = set()
	seen_keys: set[str] = set()
	for provider in providers:
		if provider.name in seen_names:
			raise ValueError(f"duplicate provider name: {provider.name!r}")
		seen_names.add(provider.name)

		if provider.key in seen_keys:
			raise ValueError(f"duplicate provider key: {provider.key!r}")
		seen_keys.add(provider.key)

		if not _KEY_RE.fullmatch(provider.key):
			raise ValueError(f"invalid key slug for {provider.name!r}: {provider.key!r}")

		if not provider.base_url.startswith("https://"):
			raise ValueError(f"base_url must be https for {provider.name!r}: {provider.base_url!r}")

		if provider.auth not in _ALLOWED_AUTH:
			raise ValueError(f"invalid auth class for {provider.name!r}: {provider.auth!r}")

		if provider.category not in _ALLOWED_CATEGORIES:
			raise ValueError(f"invalid category for {provider.name!r}: {provider.category!r}")

		if not (provider.description or "").strip():
			raise ValueError(f"description is required for {provider.name!r}")

		declared_endpoints = [f for f in _ENDPOINT_FIELDS if getattr(provider, f)]
		if declared_endpoints and provider.auth not in _ENDPOINT_AUTHS:
			raise ValueError(
				f"sign-in endpoints are only allowed on a static provider for "
				f"{provider.name!r}: {provider.auth!r}"
			)
		if declared_endpoints and len(declared_endpoints) != len(_ENDPOINT_FIELDS):
			# ALL three or NONE: a client seeded from a half-declared provider would
			# have a hole where discovery would otherwise fill one, so a partial
			# declaration is a catalog bug, not a seedable provider.
			raise ValueError(
				f"a static provider must declare all sign-in endpoints or none for {provider.name!r}"
			)
		for field in _ENDPOINT_FIELDS:
			value = getattr(provider, field)
			if value and not value.startswith("https://"):
				raise ValueError(f"{field} must be https for {provider.name!r}: {value!r}")

		if provider.scopes and provider.auth not in _SCOPES_AUTHS:
			raise ValueError(
				f"scopes are only allowed on a static or self-registering provider for "
				f"{provider.name!r}: {provider.auth!r}"
			)
		if provider.token_help_url and not provider.token_help_url.startswith("https://"):
			raise ValueError(
				f"token_help_url must be https for {provider.name!r}: {provider.token_help_url!r}"
			)


PROVIDERS: tuple[Provider, ...] = (
	# --- already shipping (order preserved) --------------------------------
	Provider(
		name="GitHub",
		key="github",
		description="Repositories, issues and pull requests",
		base_url="https://api.githubcopilot.com/mcp/",
		auth=AUTH_STATIC,
		category="dev",
		logo="github",
		help_url="https://github.com/settings/applications/new",
		hint="Register your own GitHub app, then paste its details here.",
		# GitHub's sign-in service publishes no discovery document, so its
		# endpoints are pinned here for the metadata-less static seam:
		# `_setup_mcp_oauth_client` seeds the client straight from these reviewed
		# constants and makes no outbound request.
		issuer="https://github.com/login/oauth",
		authorization_endpoint="https://github.com/login/oauth/authorize",
		token_endpoint="https://github.com/login/oauth/access_token",
		scopes="repo read:org",
		# `hint`/`help_url` above now guide registering your own GitHub app, so the
		# older paste-a-token guidance lives on its own fields for the "use a token
		# instead" fallback, restored verbatim from the pre-sign-in catalog.
		token_hint="Needs a fine-grained token scoped to the repos you want connected, with Contents (read) and Pull requests (read and write) permissions.",
		token_help_url="https://github.com/settings/personal-access-tokens/new",
	),
	Provider(
		name="Atlassian",
		key="atlassian",
		description="Jira issues and Confluence pages",
		base_url="https://mcp.atlassian.com/v2/mcp",
		auth=AUTH_DCR,
		category="work",
		logo="atlassian",
		help_url="https://id.atlassian.com/manage-profile/security/api-tokens",
		hint="Needs an Atlassian API token for your account. Your organization admin may need to enable API tokens for Jira and Confluence first.",
	),
	Provider(
		name="Linear",
		key="linear",
		description="Issues, projects and cycles",
		base_url="https://mcp.linear.app/mcp",
		auth=AUTH_DCR,
		category="work",
		logo="linear",
		help_url="https://linear.app/settings/account/security",
		hint="Needs a personal API key from your Linear workspace's Security & access settings.",
	),
	Provider(
		name="Stripe",
		key="stripe",
		description="Payments, customers and invoices",
		base_url="https://mcp.stripe.com/",
		auth=AUTH_TOKEN,
		category="payments",
		logo="stripe",
		help_url="https://dashboard.stripe.com/apikeys",
		hint="Needs a restricted API key with only the permissions this connector should use, not a full secret key.",
	),
	# --- payments ------------------------------------------------------------
	Provider(
		name="Razorpay",
		key="razorpay",
		description="Payments, orders and settlements",
		base_url="https://mcp.razorpay.com/mcp",
		auth=AUTH_DCR,
		category="payments",
		logo="razorpay",
		help_url=None,
		hint=None,
	),
	Provider(
		name="PayPal",
		key="paypal",
		description="Payments and invoices",
		base_url="https://mcp.paypal.com/mcp",
		auth=AUTH_DCR,
		category="payments",
		logo="paypal",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Square",
		key="square",
		description="Payments, orders and catalog",
		base_url="https://mcp.squareup.com/mcp",
		auth=AUTH_DCR,
		category="payments",
		logo="square",
		help_url=None,
		hint=None,
		# Its sign-in service only accepts callback addresses it already
		# knows (localhost and a few hosted assistants); every self-hosted
		# tenant address is refused with invalid_redirect_uri (probed
		# 2026-09-06). Off until Square opens registration.
		enabled=False,
	),
	# --- work ------------------------------------------------------------
	Provider(
		name="Asana",
		key="asana",
		description="Tasks and projects",
		base_url="https://mcp.asana.com/mcp",
		auth=AUTH_DCR,
		category="work",
		logo="asana",
		help_url=None,
		hint=None,
		# Its sign-in service only accepts callback addresses it already
		# knows: localhost registers, every public tenant host is refused
		# with invalid_redirect_uri (probed 2026-09-06). Off until Asana
		# opens registration.
		enabled=False,
	),
	Provider(
		name="Notion",
		key="notion",
		description="Pages and databases",
		base_url="https://mcp.notion.com/mcp",
		auth=AUTH_DCR,
		category="work",
		logo="notion",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Monday.com",
		key="monday",
		description="Boards and items",
		base_url="https://mcp.monday.com/mcp",
		auth=AUTH_STATIC,
		category="work",
		logo=None,
		help_url="https://developer.monday.com/apps/docs/oauth",
		hint="Register your own Monday.com app, then paste its details here.",
	),
	Provider(
		name="Slack",
		key="slack",
		description="Channels and messages",
		base_url="https://mcp.slack.com/mcp",
		auth=AUTH_STATIC,
		category="work",
		logo="slack",
		help_url="https://api.slack.com/apps",
		hint="Register your own Slack app, then paste its details here.",
	),
	# --- files ------------------------------------------------------------
	Provider(
		name="Dropbox",
		key="dropbox",
		description="Files and folders",
		base_url="https://mcp.dropbox.com/mcp",
		auth=AUTH_DCR,
		category="files",
		logo="dropbox",
		help_url=None,
		hint=None,
		# Its sign-in service answers registration_not_supported: "only
		# pre-registered MCP trusted partners" (probed 2026-09-06). Off until
		# Dropbox opens registration.
		enabled=False,
	),
	Provider(
		name="Box",
		key="box",
		description="Files and folders",
		base_url="https://mcp.box.com/mcp",
		auth=AUTH_STATIC,
		category="files",
		logo="box",
		help_url="https://app.box.com/developers/console",
		hint="Register your own Box app, then paste its details here.",
	),
	# --- design ------------------------------------------------------------
	Provider(
		name="Canva",
		key="canva",
		description="Designs and templates",
		base_url="https://mcp.canva.com/mcp",
		auth=AUTH_DCR,
		category="design",
		logo="canva",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Figma",
		key="figma",
		description="Files and comments",
		base_url="https://mcp.figma.com/mcp",
		auth=AUTH_DCR,
		category="design",
		logo="figma",
		help_url=None,
		hint=None,
		# Its registration endpoint answers 403 to every request shape
		# (probed 2026-09-06); registration is limited to approved partners.
		# Off until Figma opens it.
		enabled=False,
	),
	# --- support ------------------------------------------------------------
	Provider(
		name="Intercom",
		key="intercom",
		description="Conversations and tickets",
		base_url="https://mcp.intercom.com/mcp",
		auth=AUTH_TOKEN,
		category="support",
		logo="intercom",
		help_url="https://developers.intercom.com/docs/build-an-integration/learn-more/authentication",
		hint="Paste an access token from your Intercom developer hub.",
	),
	Provider(
		name="Zendesk",
		key="zendesk",
		description="Conversations and tickets",
		base_url="https://mcp.zendesk.com/mcp",
		auth=AUTH_TOKEN,
		category="support",
		logo="zendesk",
		help_url="https://support.zendesk.com/hc/en-us/articles/4408889192858-Managing-API-token-access-to-the-Zendesk-API",
		hint="Paste an API token generated in your Zendesk admin settings.",
	),
	# --- data ------------------------------------------------------------
	Provider(
		name="Supabase",
		key="supabase",
		description="Databases and projects",
		base_url="https://mcp.supabase.com/mcp",
		auth=AUTH_DCR,
		category="data",
		logo="supabase",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Neon",
		key="neon",
		description="Databases and projects",
		base_url="https://mcp.neon.tech/mcp",
		auth=AUTH_DCR,
		category="data",
		logo="neon",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Airtable",
		key="airtable",
		description="Bases and records",
		base_url="https://mcp.airtable.com/mcp",
		auth=AUTH_STATIC,
		category="data",
		logo="airtable",
		help_url="https://airtable.com/create/oauth",
		hint="Register your own Airtable app, then paste its details here.",
	),
	Provider(
		name="Sentry",
		key="sentry",
		description="Errors and releases",
		base_url="https://mcp.sentry.dev/mcp",
		auth=AUTH_DCR,
		category="data",
		logo="sentry",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Cloudflare",
		key="cloudflare",
		description="Workers, KV and DNS",
		base_url="https://bindings.mcp.cloudflare.com/mcp",
		auth=AUTH_DCR,
		category="data",
		logo="cloudflare",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Vercel",
		key="vercel",
		description="Sites and deployments",
		base_url="https://mcp.vercel.com/",
		auth=AUTH_DCR,
		category="data",
		logo="vercel",
		help_url=None,
		hint=None,
		# Its sign-in service only approves callback addresses of known
		# assistants (invalid_redirect_uri, probed 2026-09-06). Off until
		# Vercel opens registration.
		enabled=False,
	),
	Provider(
		name="Netlify",
		key="netlify",
		description="Sites and deployments",
		base_url="https://netlify-mcp.netlify.app/mcp",
		auth=AUTH_DCR,
		category="data",
		logo="netlify",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Plaid",
		key="plaid",
		description="Bank accounts and transactions",
		base_url="https://api.dashboard.plaid.com/mcp/sse",
		auth=AUTH_TOKEN,
		category="data",
		logo=None,
		help_url="https://dashboard.plaid.com/developers/keys",
		hint="Paste the secret key from your Plaid developer dashboard.",
		# The sweep's own endpoint is an SSE-suffixed URL, the older
		# separate-stream MCP transport; jarvis.connectors.mcp_client only
		# speaks Streamable HTTP (a single POST endpoint). Listed for
		# completeness but off until someone confirms the server also
		# answers Streamable HTTP on this address.
		enabled=False,
	),
	# --- web ------------------------------------------------------------
	Provider(
		name="Webflow",
		key="webflow",
		description="Sites, pages and content",
		base_url="https://mcp.webflow.com/mcp",
		auth=AUTH_DCR,
		category="web",
		logo="webflow",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Wix",
		key="wix",
		description="Sites, pages and content",
		base_url="https://mcp.wix.com/mcp",
		auth=AUTH_DCR,
		category="web",
		logo="wix",
		help_url=None,
		hint=None,
	),
	# --- automation ------------------------------------------------------------
	Provider(
		name="Zapier",
		key="zapier",
		description="Your Zaps and automations",
		base_url="https://mcp.zapier.com/api/mcp/mcp",
		auth=AUTH_TOKEN,
		category="automation",
		logo="zapier",
		help_url="https://mcp.zapier.com/",
		hint="Paste the key from your Zapier connection settings.",
	),
	# --- docs ------------------------------------------------------------
	Provider(
		name="Microsoft Learn",
		key="microsoft_learn",
		description="Official documentation",
		base_url="https://learn.microsoft.com/api/mcp",
		auth=AUTH_OPEN,
		category="docs",
		logo="microsoft",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Cloudflare Docs",
		key="cloudflare_docs",
		description="Official documentation",
		base_url="https://docs.mcp.cloudflare.com/mcp",
		auth=AUTH_OPEN,
		category="docs",
		logo="cloudflare",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Hugging Face",
		key="huggingface",
		description="Models and datasets",
		base_url="https://huggingface.co/mcp",
		auth=AUTH_OPEN,
		category="docs",
		logo="huggingface",
		help_url=None,
		hint=None,
	),
)

validate(PROVIDERS)


def by_name(name: str, *, providers: tuple[Provider, ...] = PROVIDERS) -> Provider | None:
	"""The provider whose `name` (the stored `preset` value) matches, disabled
	entries included, so a create/update path can look one up and decide for
	itself whether `.enabled` still allows it."""
	for provider in providers:
		if provider.name == name:
			return provider
	return None


def preset_names(*, providers: tuple[Provider, ...] = PROVIDERS) -> tuple[str, ...]:
	"""The presets a NEW connector may be created against: catalog order,
	enabled entries only. A disabled entry never appears here, that is the
	point of disabling it."""
	return tuple(provider.name for provider in providers if provider.enabled)


def all_names(*, providers: tuple[Provider, ...] = PROVIDERS) -> tuple[str, ...]:
	"""Every preset name, catalog order, DISABLED ENTRIES INCLUDED.

	This is what the `Jarvis Connector.preset` Select options are generated from,
	and the reason they are not `preset_names()`: Frappe validates a Select value
	on every save, so dropping a name from the options makes every existing row
	on it unsaveable, and disabling an entry would silently freeze rows that are
	already saved (no relabel, no disable, not even a credential change). What
	disabling does is keep the name out of the picker (`to_public`) and out of
	the create allowlist (`preset_names`), which is where it belongs."""
	return tuple(provider.name for provider in providers)


def base_urls(*, providers: tuple[Provider, ...] = PROVIDERS) -> dict[str, str]:
	"""`name -> base_url` for every entry, unfiltered by `enabled`: an already
	saved connector row must keep resolving its endpoint even after its
	preset stops being offered for new connectors. A create path must check
	`by_name(name).enabled` before resolving through this map, or a disabled
	preset is still creatable."""
	return {provider.name: provider.base_url for provider in providers}


def keys(*, providers: tuple[Provider, ...] = PROVIDERS) -> dict[str, str]:
	"""`name -> key`, unfiltered for the same reason as `base_urls`."""
	return {provider.name: provider.key for provider in providers}


def auth_of(name: str, *, providers: tuple[Provider, ...] = PROVIDERS) -> str | None:
	"""The auth class for `name`, or `None` if it names no known preset
	(disabled entries still resolve, matching `by_name`)."""
	provider = by_name(name, providers=providers)
	return provider.auth if provider else None


def to_public(*, providers: tuple[Provider, ...] = PROVIDERS) -> list[dict]:
	"""The fields the SPA may see, enabled entries only, catalog order: name,
	key, auth, category, logo, help_url, hint, description, token_hint,
	token_help_url. Never `base_url`, the endpoint is server-pinned and never client
	input, and never `enabled` (a disabled entry is simply absent instead).
	`token_hint` / `token_help_url` are public strings (paste-a-token guidance) the
	SPA shows on the "use a token instead" fallback. `description` is the one-line
	summary of what the app is for, shown under its name in the picker."""
	return [
		{
			"name": provider.name,
			"key": provider.key,
			"auth": provider.auth,
			"category": provider.category,
			"logo": provider.logo,
			"help_url": provider.help_url,
			"hint": provider.hint,
			"description": provider.description,
			"token_hint": provider.token_hint,
			"token_help_url": provider.token_help_url,
		}
		for provider in providers
		if provider.enabled
	]


def apply_overlay(
	overlay: list[dict], *, providers: tuple[Provider, ...] = PROVIDERS
) -> tuple[Provider, ...]:
	"""Return `providers` (default `PROVIDERS`) with each `overlay` entry
	applied, then re-validated with `validate()` before it is returned, so a
	bad overlay fails loudly instead of shipping a broken catalog.

	Each entry is a dict identified by `name`:

	  * a `name` that already exists may only carry `enabled` (True or
	    False); any other field present in the entry that differs from the
	    shipped value (`key`, `base_url`, `auth`, `category`, `logo`,
	    `help_url`, `hint`) raises `ValueError`, an overlay disables, it does
	    not redefine.
	  * a `name` that does not yet exist is added as a brand-new `Provider`;
	    the entry must then carry `key`, `base_url`, `auth` and `category`
	    (`logo`/`help_url`/`hint`/`enabled` are optional, defaulting like the
	    dataclass does).

	Implemented and tested; nothing calls this yet, a later phase wires it to
	an admin-editable Settings row."""
	by_existing = {provider.name: provider for provider in providers}
	result = list(providers)
	fixed_fields = (
		"key",
		"base_url",
		"auth",
		"category",
		"logo",
		"help_url",
		"hint",
		"description",
		"issuer",
		"authorization_endpoint",
		"token_endpoint",
		"scopes",
		"token_hint",
		"token_help_url",
	)

	for entry in overlay:
		name = entry.get("name")
		if not name:
			raise ValueError("overlay entry missing 'name'")

		existing = by_existing.get(name)
		if existing is not None:
			for field in fixed_fields:
				if field in entry and entry[field] != getattr(existing, field):
					raise ValueError(f"overlay may not change {field!r} for {name!r}")
			updated = replace(existing, enabled=entry.get("enabled", existing.enabled))
			result[result.index(existing)] = updated
			by_existing[name] = updated
		else:
			provider = Provider(
				name=name,
				key=entry.get("key", ""),
				base_url=entry.get("base_url", ""),
				auth=entry.get("auth", ""),
				category=entry.get("category", ""),
				logo=entry.get("logo"),
				help_url=entry.get("help_url"),
				hint=entry.get("hint"),
				description=entry.get("description", ""),
				enabled=entry.get("enabled", True),
				issuer=entry.get("issuer"),
				authorization_endpoint=entry.get("authorization_endpoint"),
				token_endpoint=entry.get("token_endpoint"),
				scopes=entry.get("scopes"),
				token_hint=entry.get("token_hint"),
				token_help_url=entry.get("token_help_url"),
			)
			result.append(provider)
			by_existing[name] = provider

	final = tuple(result)
	validate(final)
	return final
