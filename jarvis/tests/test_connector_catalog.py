"""Unit tests for ``jarvis.connectors.catalog``, the connector provider data
module. Plain ``unittest``; the module is frappe-free, so this runs with no
bench: ``python3 -m unittest jarvis.tests.test_connector_catalog``.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from jarvis.connectors import catalog

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
_LEGACY_HELP_URLS = {
	"GitHub": "https://github.com/settings/personal-access-tokens/new",
	"Atlassian": "https://id.atlassian.com/manage-profile/security/api-tokens",
	"Linear": "https://linear.app/settings/account/security",
	"Stripe": "https://dashboard.stripe.com/apikeys",
}
_STRIPE_HINT = (
	"Needs a restricted API key with only the permissions this connector should use, not a full secret key."
)


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
				catalog.AUTH_CONNECTED_APP: 1,
				catalog.AUTH_STATIC: 4,
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

	def test_github_is_connected_app(self):
		self.assertEqual(catalog.by_name("GitHub").auth, catalog.AUTH_CONNECTED_APP)

	def test_atlassian_and_linear_are_dcr(self):
		self.assertEqual(catalog.by_name("Atlassian").auth, catalog.AUTH_DCR)
		self.assertEqual(catalog.by_name("Linear").auth, catalog.AUTH_DCR)

	def test_stripe_is_token_with_its_existing_hint(self):
		stripe = catalog.by_name("Stripe")
		self.assertEqual(stripe.auth, catalog.AUTH_TOKEN)
		self.assertEqual(stripe.hint, _STRIPE_HINT)

	def test_non_token_legacy_presets_have_no_hint(self):
		for name in ("GitHub", "Atlassian", "Linear"):
			self.assertIsNone(catalog.by_name(name).hint, name)


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

	def test_auth_of_known_and_unknown(self):
		self.assertEqual(catalog.auth_of("Stripe"), catalog.AUTH_TOKEN)
		self.assertIsNone(catalog.auth_of("Nonexistent Provider"))


class TestToPublic(unittest.TestCase):
	def test_only_allowed_fields_exposed(self):
		allowed = {"name", "key", "auth", "category", "logo", "help_url", "hint"}
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


if __name__ == "__main__":
	unittest.main()
