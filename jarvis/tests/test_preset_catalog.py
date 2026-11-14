import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client
from jarvis.catalog_store import PRESETS, SNAPSHOT_DT
from jarvis.exceptions import AdminUnreachableError

_PAYLOAD = [
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


def _snapshot_presets() -> list:
	return json.loads(frappe.db.get_single_value(SNAPSHOT_DT, "preset_catalog") or "[]")


class TestPresetCatalog(FrappeTestCase):
	def setUp(self):
		frappe.cache().delete_value(PRESETS.cache_key)
		self.addCleanup(frappe.cache().delete_value, PRESETS.cache_key)
		frappe.db.savepoint("preset_catalog_test")
		self.addCleanup(frappe.db.rollback, save_point="preset_catalog_test")

	def test_read_never_calls_the_admin(self):
		with patch.object(admin_client, "_post_guest", side_effect=AssertionError("no admin")):
			out = admin_client.get_preset_catalog()
		self.assertTrue(out)
		self.assertEqual(out, _snapshot_presets())

	def test_refresh_fetches_and_saves(self):
		with patch.object(admin_client, "_post_guest", return_value=_PAYLOAD) as gp:
			self.assertEqual(PRESETS.refresh(), _PAYLOAD)
		self.assertIn("get_preset_catalog", gp.call_args.kwargs["path"])
		self.assertEqual(_snapshot_presets(), _PAYLOAD)

	def test_refresh_with_admin_down_serves_the_snapshot(self):
		before = _snapshot_presets()
		with patch.object(admin_client, "_post_guest", side_effect=AdminUnreachableError("down")):
			self.assertEqual(PRESETS.refresh(), before)

	def test_onboarding_endpoint_fetches_live(self):
		# Onboarding reads the admin directly (not Redis) so a new customer sees
		# today's presets.
		from jarvis import onboarding

		with patch.object(PRESETS, "refresh", return_value=[{"key": "k"}]) as refresh:
			self.assertEqual(onboarding.get_preset_catalog(), [{"key": "k"}])
		refresh.assert_called_once()
