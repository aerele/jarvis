"""In-app connector provider catalog (frappe-free): the single source of truth
for MCP connector presets, their names, pinned endpoints, auth flow, logo and
credential-entry help copy.

Design:

  * This is a baseline catalog. It ships INSIDE the app, in code, reviewed
    like any other change. It is public data (no secrets ever live here) but
    it is also an ALLOWLIST: it decides which endpoint a saved connector's
    credential is ever allowed to be sent to. That is why it belongs behind
    code review rather than a database row an admin edits unsupervised.
  * A `base_url` is a fixed vendor endpoint, `auth` picks one of the five
    connection flows the discovery engine supports, and `logo` / `help_url` /
    `hint` are display copy. None of that is secret. What IS secret, a static
    preset's registered client id/secret, or a user's own bearer token, never
    lives in this module or its overlay; those are stored per tenant on the
    Connected App or the connector row's own credential field.
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
AUTH_CONNECTED_APP = "connected_app"

_ALLOWED_AUTH = frozenset({AUTH_DCR, AUTH_STATIC, AUTH_TOKEN, AUTH_OPEN, AUTH_CONNECTED_APP})

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
	agent addresses it by."""

	name: str
	key: str
	base_url: str
	auth: str
	category: str
	logo: str | None
	help_url: str | None
	hint: str | None
	enabled: bool = True


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


PROVIDERS: tuple[Provider, ...] = (
	# --- already shipping (order preserved) --------------------------------
	Provider(
		name="GitHub",
		key="github",
		base_url="https://api.githubcopilot.com/mcp/",
		auth=AUTH_CONNECTED_APP,
		category="dev",
		logo="github",
		help_url="https://github.com/settings/personal-access-tokens/new",
		hint="Needs a fine-grained token scoped to the repos you want connected, with Contents (read) and Pull requests (read and write) permissions.",
	),
	Provider(
		name="Atlassian",
		key="atlassian",
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
		base_url="https://mcp.squareup.com/mcp",
		auth=AUTH_DCR,
		category="payments",
		logo="square",
		help_url=None,
		hint=None,
	),
	# --- work ------------------------------------------------------------
	Provider(
		name="Asana",
		key="asana",
		base_url="https://mcp.asana.com/mcp",
		auth=AUTH_DCR,
		category="work",
		logo="asana",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Notion",
		key="notion",
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
		base_url="https://mcp.monday.com/mcp",
		auth=AUTH_STATIC,
		category="work",
		logo=None,
		help_url=None,
		hint=None,
	),
	Provider(
		name="Slack",
		key="slack",
		base_url="https://mcp.slack.com/mcp",
		auth=AUTH_STATIC,
		category="work",
		logo="slack",
		help_url=None,
		hint=None,
	),
	# --- files ------------------------------------------------------------
	Provider(
		name="Dropbox",
		key="dropbox",
		base_url="https://mcp.dropbox.com/mcp",
		auth=AUTH_DCR,
		category="files",
		logo="dropbox",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Box",
		key="box",
		base_url="https://mcp.box.com/mcp",
		auth=AUTH_STATIC,
		category="files",
		logo="box",
		help_url=None,
		hint=None,
	),
	# --- design ------------------------------------------------------------
	Provider(
		name="Canva",
		key="canva",
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
		base_url="https://mcp.figma.com/mcp",
		auth=AUTH_DCR,
		category="design",
		logo="figma",
		help_url=None,
		hint=None,
	),
	# --- support ------------------------------------------------------------
	Provider(
		name="Intercom",
		key="intercom",
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
		base_url="https://mcp.airtable.com/mcp",
		auth=AUTH_STATIC,
		category="data",
		logo="airtable",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Sentry",
		key="sentry",
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
		base_url="https://mcp.vercel.com/",
		auth=AUTH_DCR,
		category="data",
		logo="vercel",
		help_url=None,
		hint=None,
	),
	Provider(
		name="Netlify",
		key="netlify",
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
	key, auth, category, logo, help_url, hint. Never `base_url`, the endpoint
	is server-pinned and never client input, and never `enabled` (a disabled
	entry is simply absent instead)."""
	return [
		{
			"name": provider.name,
			"key": provider.key,
			"auth": provider.auth,
			"category": provider.category,
			"logo": provider.logo,
			"help_url": provider.help_url,
			"hint": provider.hint,
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
	fixed_fields = ("key", "base_url", "auth", "category", "logo", "help_url", "hint")

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
				enabled=entry.get("enabled", True),
			)
			result.append(provider)
			by_existing[name] = provider

	final = tuple(result)
	validate(final)
	return final
