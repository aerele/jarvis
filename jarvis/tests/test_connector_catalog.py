"""Unit tests for ``jarvis.connectors.catalog``, the connector provider data
module. Plain ``unittest``; the module is frappe-free, so this runs with no
bench: ``python3 -m unittest jarvis.tests.test_connector_catalog``.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from jarvis.connectors import catalog

# The Jarvis Connector DocType's own JSON. Read as a file rather than through
# frappe.get_meta so this stays bench-free.
_CONNECTOR_JSON = (
	Path(catalog.__file__).resolve().parents[1]
	/ "jarvis"
	/ "doctype"
	/ "jarvis_connector"
	/ "jarvis_connector.json"
)

# The four presets already shipping in ``jarvis.chat.connectors_api`` before
# this catalog existed. These literals are copied from
# ``_PRESET_BASE_URLS``/``_PRESET_KEYS`` there and from
# ``frontend/src/components/settings/connectorHelp.js``'s ``CONNECTOR_HELP``,
# not imported (``connectors_api`` pulls in frappe, which this test suite must
# not need).
_LEGACY_BASE_URLS = {
	"GitHub": "https://api.githubcopilot.com/mcp/",
	"Atlassian": "https://mcp.atlassian.com/v2/mcp",
	"Linear": "https://mcp.linear.app/mcp",
	"Stripe": "https://mcp.stripe.com/",
}
_LEGACY_KEYS = {
	"GitHub": "github",
	"Atlassian": "atlassian",
	"Linear": "linear",
	"Stripe": "stripe",
}
# GitHub is deliberately absent: it moved onto the sign-in engine as a
# bring-your-own-app static provider, so its help_url now points at GitHub's
# app-creation page, not the old personal-access-token page. Its new copy is
# asserted in TestCatalogEndpointFields instead. The other three are unchanged.
_LEGACY_HELP_URLS = {
	"Atlassian": "https://id.atlassian.com/manage-profile/security/api-tokens",
	"Linear": "https://linear.app/settings/account/security",
	"Stripe": "https://dashboard.stripe.com/apikeys",
}
_STRIPE_HINT = (
	"Needs a restricted API key with only the permissions this connector should use, not a full secret key."
)
# The token guidance the SPA shipped before this catalog existed, copied from the
# same CONNECTOR_HELP map. A sign-in preset still shows it: the "Use a token
# instead" fallback is exactly the case where a user needs to be told WHICH
# token, so losing these strings would leave that path unexplained.
_LEGACY_HINTS = {
	"Atlassian": (
		"Needs an Atlassian API token for your account. Your organization admin may "
		"need to enable API tokens for Jira and Confluence first."
	),
	"Linear": "Needs a personal API key from your Linear workspace's Security & access settings.",
	"Stripe": _STRIPE_HINT,
}


class TestCatalogShape(unittest.TestCase):
	def test_thirty_one_providers(self):
		self.assertEqual(len(catalog.PROVIDERS), 31)

	def test_names_unique(self):
		names = [p.name for p in catalog.PROVIDERS]
		self.assertEqual(len(names), len(set(names)))

	def test_keys_unique(self):
		keys = [p.key for p in catalog.PROVIDERS]
		self.assertEqual(len(keys), len(set(keys)))

	def test_all_base_urls_https(self):
		for provider in catalog.PROVIDERS:
			self.assertTrue(provider.base_url.startswith("https://"), provider.name)

	def test_all_keys_are_lowercase_slugs(self):
		for provider in catalog.PROVIDERS:
			self.assertRegex(provider.key, r"^[a-z0-9_-]+$", provider.name)

	def test_auth_class_counts_match_probe_sweep(self):
		counts = {}
		for provider in catalog.PROVIDERS:
			counts[provider.auth] = counts.get(provider.auth, 0) + 1
		self.assertEqual(
			counts,
			{
				catalog.AUTH_DCR: 18,
				catalog.AUTH_STATIC: 5,
				catalog.AUTH_TOKEN: 5,
				catalog.AUTH_OPEN: 3,
			},
		)

	def test_unreachable_providers_excluded(self):
		names = {p.name for p in catalog.PROVIDERS}
		for excluded in ("DocuSign", "HubSpot", "Shopify", "Twilio"):
			self.assertNotIn(excluded, names)

	def test_custom_url_constant_is_not_a_provider(self):
		names = {p.name for p in catalog.PROVIDERS}
		self.assertNotIn(catalog.CUSTOM_URL, names)
		self.assertEqual(catalog.CUSTOM_URL, "Custom URL")


class TestLegacyPresetsUnchanged(unittest.TestCase):
	def test_base_urls_and_keys_exact(self):
		for name, base_url in _LEGACY_BASE_URLS.items():
			provider = catalog.by_name(name)
			self.assertIsNotNone(provider, name)
			self.assertEqual(provider.base_url, base_url, name)
			self.assertEqual(provider.key, _LEGACY_KEYS[name], name)

	def test_help_urls_carried_over(self):
		for name, help_url in _LEGACY_HELP_URLS.items():
			self.assertEqual(catalog.by_name(name).help_url, help_url, name)

	def test_github_is_static_byoa(self):
		# GitHub moved onto the sign-in engine as a bring-your-own-app static
		# provider; the legacy per-app sign-in path it used to take was removed.
		self.assertEqual(catalog.by_name("GitHub").auth, catalog.AUTH_STATIC)

	def test_atlassian_and_linear_are_dcr(self):
		self.assertEqual(catalog.by_name("Atlassian").auth, catalog.AUTH_DCR)
		self.assertEqual(catalog.by_name("Linear").auth, catalog.AUTH_DCR)

	def test_stripe_is_token_with_its_existing_hint(self):
		stripe = catalog.by_name("Stripe")
		self.assertEqual(stripe.auth, catalog.AUTH_TOKEN)
		self.assertEqual(stripe.hint, _STRIPE_HINT)

	def test_legacy_token_hints_are_carried_over_verbatim(self):
		for name, hint in _LEGACY_HINTS.items():
			self.assertEqual(catalog.by_name(name).hint, hint, name)


class TestAccessors(unittest.TestCase):
	def test_by_name_unknown_returns_none(self):
		self.assertIsNone(catalog.by_name("Nonexistent Provider"))

	def test_preset_names_excludes_disabled(self):
		names = catalog.preset_names()
		self.assertIn("Razorpay", names)
		self.assertNotIn("Plaid", names)  # shipped disabled, see catalog.py

	def test_preset_names_is_catalog_order_and_matches_enabled_count(self):
		enabled = [p.name for p in catalog.PROVIDERS if p.enabled]
		self.assertEqual(list(catalog.preset_names()), enabled)

	def test_base_urls_and_keys_include_disabled_entries(self):
		# Unfiltered: an existing saved connector row must still resolve its
		# endpoint even for a preset no longer offered to new connectors.
		self.assertIn("Plaid", catalog.base_urls())
		self.assertIn("Plaid", catalog.keys())

	def test_all_names_is_catalog_order_including_disabled(self):
		self.assertEqual(list(catalog.all_names()), [p.name for p in catalog.PROVIDERS])
		self.assertIn("Plaid", catalog.all_names())

	def test_auth_of_known_and_unknown(self):
		self.assertEqual(catalog.auth_of("Stripe"), catalog.AUTH_TOKEN)
		self.assertIsNone(catalog.auth_of("Nonexistent Provider"))


class TestToPublic(unittest.TestCase):
	def test_only_allowed_fields_exposed(self):
		allowed = {
			"name",
			"key",
			"auth",
			"category",
			"logo",
			"help_url",
			"hint",
			"description",
			"token_hint",
			"token_help_url",
		}
		for row in catalog.to_public():
			self.assertEqual(set(row), allowed)
			self.assertNotIn("base_url", row)
			self.assertNotIn("enabled", row)

	def test_excludes_disabled_entries(self):
		public_names = {row["name"] for row in catalog.to_public()}
		self.assertNotIn("Plaid", public_names)
		self.assertIn("Razorpay", public_names)

	def test_count_matches_enabled_providers(self):
		enabled_count = sum(1 for p in catalog.PROVIDERS if p.enabled)
		self.assertEqual(len(catalog.to_public()), enabled_count)

	def test_every_public_entry_ships_a_non_empty_description(self):
		# The SPA renders one line under each name; a blank one would look broken.
		for row in catalog.to_public():
			self.assertTrue((row["description"] or "").strip(), row["name"])


class TestDocTypeSelectOptionsDrift(unittest.TestCase):
	"""The ``preset`` Select on Jarvis Connector is a COPY of the catalog, and
	Frappe validates a Select value against those options server-side. A catalog
	entry added without regenerating the JSON would therefore be offered by the
	SPA and then rejected on save, so the drift has to fail here instead."""

	def _preset_field(self) -> dict:
		fields = json.loads(_CONNECTOR_JSON.read_text())["fields"]
		for field in fields:
			if field.get("fieldname") == "preset":
				return field
		raise AssertionError("Jarvis Connector has no 'preset' field")

	def test_options_match_the_catalog_exactly(self):
		expected = "\n".join([*catalog.all_names(), "Custom URL"])
		self.assertEqual(
			self._preset_field()["options"],
			expected,
			"regenerate jarvis_connector.json's preset options from the catalog",
		)

	def test_disabled_entries_stay_in_the_options(self):
		# Frappe validates this Select on EVERY save, so a name dropped from the
		# options freezes every row already on it - no relabel, no disable, not
		# even a credential change. Disabling belongs in the picker
		# (``to_public``) and the create allowlist (``preset_names``), not here.
		options = self._preset_field()["options"].split("\n")
		disabled = [p.name for p in catalog.PROVIDERS if not p.enabled]
		self.assertTrue(disabled, "this test needs at least one disabled entry to mean anything")
		for name in disabled:
			self.assertIn(name, options, name)
			self.assertNotIn(name, catalog.preset_names(), name)
			self.assertNotIn(name, {row["name"] for row in catalog.to_public()}, name)


class TestValidate(unittest.TestCase):
	def test_shipped_catalog_passes(self):
		catalog.validate(catalog.PROVIDERS)  # must not raise

	def test_duplicate_key_raises(self):
		dup = replace(catalog.PROVIDERS[0], name="A Different Name")
		with self.assertRaises(ValueError):
			catalog.validate((*catalog.PROVIDERS, dup))

	def test_duplicate_name_raises(self):
		dup = replace(catalog.PROVIDERS[0], key="a-different-key")
		with self.assertRaises(ValueError):
			catalog.validate((*catalog.PROVIDERS, dup))

	def test_non_https_base_url_raises(self):
		bad = replace(catalog.PROVIDERS[0], name="Bad", key="bad", base_url="http://example.com/mcp")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_bad_auth_raises(self):
		bad = replace(catalog.PROVIDERS[0], name="Bad", key="bad", auth="magic")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_bad_category_raises(self):
		bad = replace(catalog.PROVIDERS[0], name="Bad", key="bad", category="nope")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_bad_key_slug_raises(self):
		bad = replace(catalog.PROVIDERS[0], name="Bad", key="Not A Slug!")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_blank_description_raises(self):
		bad = replace(catalog.PROVIDERS[0], name="Bad", key="bad", description="  ")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))


class TestApplyOverlay(unittest.TestCase):
	def test_disable_existing_entry(self):
		result = catalog.apply_overlay([{"name": "Razorpay", "enabled": False}])
		self.assertEqual(len(result), len(catalog.PROVIDERS))
		self.assertNotIn("Razorpay", catalog.preset_names(providers=result))
		# The base catalog itself is untouched.
		self.assertIn("Razorpay", catalog.preset_names())

	def test_add_new_entry(self):
		overlay = [
			{
				"name": "Acme",
				"key": "acme",
				"base_url": "https://mcp.acme.example/mcp",
				"auth": catalog.AUTH_TOKEN,
				"category": "data",
				"hint": "Paste an Acme API key.",
				"description": "Widgets and orders",
			}
		]
		result = catalog.apply_overlay(overlay)
		self.assertEqual(len(result), len(catalog.PROVIDERS) + 1)
		added = catalog.by_name("Acme", providers=result)
		self.assertIsNotNone(added)
		self.assertEqual(added.base_url, "https://mcp.acme.example/mcp")
		self.assertTrue(added.enabled)
		# Not leaked into the unmodified base catalog.
		self.assertIsNone(catalog.by_name("Acme"))

	def test_rejects_base_url_change_on_existing_entry(self):
		overlay = [{"name": "Razorpay", "base_url": "https://evil.example/mcp"}]
		with self.assertRaises(ValueError):
			catalog.apply_overlay(overlay)

	def test_rejects_auth_change_on_existing_entry(self):
		overlay = [{"name": "Razorpay", "auth": catalog.AUTH_TOKEN}]
		with self.assertRaises(ValueError):
			catalog.apply_overlay(overlay)

	def test_missing_name_raises(self):
		with self.assertRaises(ValueError):
			catalog.apply_overlay([{"enabled": False}])

	def test_result_is_revalidated(self):
		# A "new" entry that collides on key with an existing one must fail
		# the same way a bad shipped catalog would.
		overlay = [
			{
				"name": "Acme",
				"key": "razorpay",
				"base_url": "https://mcp.acme.example/mcp",
				"auth": catalog.AUTH_TOKEN,
				"category": "data",
			}
		]
		with self.assertRaises(ValueError):
			catalog.apply_overlay(overlay)


class TestCatalogEndpointFields(unittest.TestCase):
	"""The metadata-less-static seam: a static provider whose sign-in service
	publishes no discovery document may pin its own sign-in endpoints so its
	client can be seeded without discovery."""

	def test_github_declares_its_pinned_endpoints_and_scopes(self):
		github = catalog.by_name("GitHub")
		self.assertEqual(github.auth, catalog.AUTH_STATIC)
		self.assertEqual(github.issuer, "https://github.com/login/oauth")
		self.assertEqual(github.authorization_endpoint, "https://github.com/login/oauth/authorize")
		self.assertEqual(github.token_endpoint, "https://github.com/login/oauth/access_token")
		self.assertEqual(github.scopes, "repo read:org")

	def test_github_ships_bring_your_own_app_help_and_hint(self):
		github = catalog.by_name("GitHub")
		self.assertEqual(github.help_url, "https://github.com/settings/applications/new")
		self.assertEqual(github.hint, "Register your own GitHub app, then paste its details here.")

	def test_every_static_provider_guides_the_customer_to_register_its_own_app(self):
		# Every static provider is bring-your-own-app: the customer registers their
		# own vendor app, so each must ship a help link and a one-line hint.
		for provider in catalog.PROVIDERS:
			if provider.auth == catalog.AUTH_STATIC:
				self.assertTrue(provider.help_url, provider.name)
				self.assertTrue(provider.hint, provider.name)

	def test_only_github_seeds_from_pinned_endpoints(self):
		# The other static providers (Slack, Box, ...) publish metadata and keep
		# the discovery path, so they declare no endpoints.
		for provider in catalog.PROVIDERS:
			if provider.name != "GitHub":
				self.assertIsNone(provider.issuer, provider.name)
				self.assertIsNone(provider.authorization_endpoint, provider.name)
				self.assertIsNone(provider.token_endpoint, provider.name)

	def test_endpoints_rejected_on_a_dcr_provider(self):
		# A self-registering provider gets its endpoints from discovery, never from
		# the catalog: declaring them here is a category error the allowlist blocks.
		bad = replace(catalog.by_name("Linear"), issuer="https://as.linear.example")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_non_https_endpoint_rejected(self):
		bad = replace(catalog.by_name("GitHub"), token_endpoint="http://insecure.example/token")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_partial_endpoint_declaration_rejected(self):
		# All three endpoints together or none: a client seeded from a
		# half-declared provider would have a hole discovery would otherwise fill.
		bad = replace(catalog.by_name("GitHub"), token_endpoint=None)
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_scopes_rejected_on_a_non_signin_provider(self):
		# A token preset has no sign-in to scope, so a `scopes` on one is a mistake.
		bad = replace(catalog.by_name("Stripe"), scopes="read")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_scopes_allowed_on_a_dcr_provider(self):
		# A self-registering preset passes its scope through to registration, so a
		# scope on one is legitimate and must NOT be rejected.
		ok = replace(catalog.by_name("Linear"), scopes="read")
		catalog.validate((ok,))  # must not raise


class TestTokenGuidanceFields(unittest.TestCase):
	"""`token_hint` / `token_help_url` carry the paste-a-token guidance for the
	"use a token instead" fallback, kept separate from `hint` / `help_url` because
	those now guide registering your own app on a sign-in preset."""

	def test_github_ships_paste_a_token_guidance_verbatim(self):
		github = catalog.by_name("GitHub")
		self.assertEqual(
			github.token_hint,
			"Needs a fine-grained token scoped to the repos you want connected, with "
			"Contents (read) and Pull requests (read and write) permissions.",
		)
		self.assertEqual(github.token_help_url, "https://github.com/settings/personal-access-tokens/new")

	def test_to_public_exposes_both_token_fields_for_github(self):
		github_row = next(row for row in catalog.to_public() if row["name"] == "GitHub")
		self.assertEqual(
			github_row["token_hint"],
			"Needs a fine-grained token scoped to the repos you want connected, with "
			"Contents (read) and Pull requests (read and write) permissions.",
		)
		self.assertEqual(
			github_row["token_help_url"], "https://github.com/settings/personal-access-tokens/new"
		)

	def test_token_help_url_must_be_https(self):
		bad = replace(catalog.by_name("GitHub"), token_help_url="http://insecure.example/token")
		with self.assertRaises(ValueError):
			catalog.validate((bad,))

	def test_token_hint_is_allowed_on_any_auth_class(self):
		# A plain hint string carries no endpoint, so it is display copy allowed on a
		# token / open preset the same as a static one.
		ok = replace(catalog.by_name("Microsoft Learn"), token_hint="Paste a token.")
		catalog.validate((ok,))  # must not raise

	def test_overlay_adds_a_metadata_less_static_provider(self):
		overlay = [
			{
				"name": "Acme SSO",
				"key": "acme_sso",
				"base_url": "https://mcp.acme.example/mcp",
				"auth": catalog.AUTH_STATIC,
				"category": "data",
				"issuer": "https://auth.acme.example",
				"authorization_endpoint": "https://auth.acme.example/authorize",
				"token_endpoint": "https://auth.acme.example/token",
				"scopes": "read write",
				"description": "Widgets and orders",
			}
		]
		result = catalog.apply_overlay(overlay)
		added = catalog.by_name("Acme SSO", providers=result)
		self.assertIsNotNone(added)
		self.assertEqual(added.issuer, "https://auth.acme.example")
		self.assertEqual(added.authorization_endpoint, "https://auth.acme.example/authorize")
		self.assertEqual(added.token_endpoint, "https://auth.acme.example/token")
		self.assertEqual(added.scopes, "read write")
		# Not leaked into the unmodified base catalog.
		self.assertIsNone(catalog.by_name("Acme SSO"))

	def test_overlay_rejects_changing_an_endpoint_on_an_existing_entry(self):
		overlay = [{"name": "GitHub", "issuer": "https://evil.example/login/oauth"}]
		with self.assertRaises(ValueError):
			catalog.apply_overlay(overlay)


if __name__ == "__main__":
	unittest.main()
