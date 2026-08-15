from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.exceptions import AdminUnreachableError


class TestGetPresetCatalog(FrappeTestCase):
	def setUp(self):
		frappe.cache().delete_value(admin_client._PRESET_CATALOG_CACHE_KEY)

	def test_fetches_and_caches_admin_catalog(self):
		payload = [
			{
				"key": "openai-resilient",
				"label": "OpenAI — resilient",
				"kind": "single_vendor",
				"blurb": "",
				"enabled": True,
				"models": [{"provider": "openai", "model": "gpt-5.5", "order": 0}],
				"vendors": ["openai"],
			}
		]
		with patch.object(admin_client, "_post_guest", return_value=payload) as gp:
			out = admin_client.get_preset_catalog()
		self.assertEqual(out, payload)
		gp.assert_called_once()
		self.assertIn("get_preset_catalog", gp.call_args.kwargs.get("path", ""))
		with patch.object(admin_client, "_post_guest", side_effect=AssertionError("must use cache")):
			self.assertEqual(admin_client.get_preset_catalog(), payload)

	def test_cache_hit_short_circuits_network(self):
		cached = [
			{
				"key": "cost-saver",
				"label": "Cost-saver",
				"kind": "cross_vendor",
				"blurb": "",
				"enabled": True,
				"models": [],
				"vendors": [],
			}
		]
		frappe.cache().set_value(
			admin_client._PRESET_CATALOG_CACHE_KEY, cached, expires_in_sec=admin_client._PRESET_CATALOG_TTL_S
		)
		with patch.object(admin_client, "_post_guest") as m:
			result = admin_client.get_preset_catalog()
		self.assertEqual(result, cached)
		m.assert_not_called()

	def test_falls_back_to_bundled_when_admin_down_and_cache_empty(self):
		from jarvis._preset_catalog import BUNDLED_PRESET_CATALOG

		with patch.object(admin_client, "_post_guest", side_effect=AdminUnreachableError("down")):
			out = admin_client.get_preset_catalog()
		self.assertEqual(out, BUNDLED_PRESET_CATALOG)
		self.assertTrue(all("key" in e and "models" in e for e in out))

	def test_wrapper_delegates_to_admin_client(self):
		from jarvis import onboarding

		with patch.object(admin_client, "get_preset_catalog", return_value=[{"key": "k"}]) as m:
			self.assertEqual(onboarding.get_preset_catalog(), [{"key": "k"}])
		m.assert_called_once()
