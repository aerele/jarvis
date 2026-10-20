"""Bench mirror of the operator's connector catalogue policy (jarvis.connector_catalogue).

The control plane owns the list of switched-off presets; the bench mirrors it via the
marker-aware persist_from_connection. KEEP on any doubt (old CP / no marker, absent key,
malformed payload), authoritative on a well-formed list including ``[]``. Unknown names
are stored but never reach the overlay. CP-side coverage lives in the jarvis_admin_v2 suite.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import connector_catalogue
from jarvis.connectors import catalog

SETTINGS = "Jarvis Settings"
FIELD = "connector_catalogue_disabled"
SYNCED_AT = "connector_catalogue_synced_at"
MARKER = "connector_catalogue_supported"
PRESET = "PayPal"
OTHER = "Stripe"


def _conn(**extra) -> dict:
	return {MARKER: True, **extra}


def _stored() -> list | None:
	raw = frappe.db.get_single_value(SETTINGS, FIELD, cache=False)
	return json.loads(raw) if raw else None


class TestConnectorCatalogueMirror(FrappeTestCase):
	def setUp(self):
		self._saved = {f: frappe.db.get_single_value(SETTINGS, f, cache=False) for f in (FIELD, SYNCED_AT)}
		frappe.db.set_single_value(SETTINGS, {FIELD: "", SYNCED_AT: None}, update_modified=False)

	def tearDown(self):
		frappe.db.set_single_value(SETTINGS, self._saved, update_modified=False)

	def _seed(self, names: list) -> None:
		connector_catalogue.persist_from_connection(_conn(connector_catalogue_disabled=names))
		self.assertEqual(_stored(), sorted(names))

	def test_the_presets_used_here_ship_enabled(self):
		for name in (PRESET, OTHER):
			self.assertTrue(catalog.by_name(name).enabled, name)

	def test_well_formed_list_is_authoritative(self):
		connector_catalogue.persist_from_connection(_conn(connector_catalogue_disabled=[PRESET]))
		self.assertEqual(_stored(), [PRESET])
		self.assertEqual(connector_catalogue.disabled_names(), {PRESET})
		self.assertTrue(frappe.db.get_single_value(SETTINGS, SYNCED_AT, cache=False))

	def test_empty_list_is_authoritative_and_re_enables(self):
		self._seed([PRESET])
		connector_catalogue.persist_from_connection(_conn(connector_catalogue_disabled=[]))
		self.assertEqual(_stored(), [])
		self.assertEqual(connector_catalogue.disabled_names(), set())

	def test_names_are_stripped_deduped_and_sorted(self):
		connector_catalogue.persist_from_connection(
			_conn(connector_catalogue_disabled=[f" {PRESET} ", OTHER, PRESET])
		)
		self.assertEqual(_stored(), sorted([PRESET, OTHER]))

	def test_marker_absent_keeps_last_applied(self):
		self._seed([PRESET])
		connector_catalogue.persist_from_connection({FIELD: []})
		self.assertEqual(_stored(), [PRESET])

	def test_key_absent_keeps_last_applied(self):
		self._seed([PRESET])
		connector_catalogue.persist_from_connection(_conn())
		self.assertEqual(_stored(), [PRESET])

	def test_malformed_payload_keeps_last_applied_and_logs(self):
		self._seed([PRESET])
		for bad in ({"PayPal": True}, "PayPal", [PRESET, ""], [PRESET, 3], [None], None):
			with self.subTest(payload=bad), patch("frappe.log_error") as log:
				connector_catalogue.persist_from_connection(_conn(connector_catalogue_disabled=bad))
				self.assertEqual(_stored(), [PRESET])
				log.assert_called_once()
				self.assertIn("malformed", log.call_args.kwargs["title"])

	def test_never_raises(self):
		with (
			patch("frappe.db.set_single_value", side_effect=RuntimeError("db down")),
			patch("frappe.log_error") as log,
		):
			connector_catalogue.persist_from_connection(_conn(connector_catalogue_disabled=[PRESET]))
		log.assert_called_once()

	def test_providers_disables_the_stored_presets_only(self):
		self._seed([PRESET])
		by_name = {p.name: p for p in connector_catalogue.providers()}
		self.assertFalse(by_name[PRESET].enabled)
		self.assertTrue(by_name[OTHER].enabled)
		self.assertEqual(len(by_name), len(catalog.PROVIDERS), "an overlay never adds or drops entries")

	def test_unknown_names_are_stored_but_ignored(self):
		self._seed(["No Such App", catalog.CUSTOM_URL, PRESET])
		self.assertIn("No Such App", connector_catalogue.disabled_names())
		names = {p.name for p in connector_catalogue.providers()}
		self.assertNotIn("No Such App", names)
		self.assertFalse(connector_catalogue.is_disabled("No Such App"))
		self.assertFalse(connector_catalogue.is_disabled(catalog.CUSTOM_URL), "Custom URL is never governed")
		self.assertTrue(connector_catalogue.is_disabled(PRESET))

	def test_providers_follows_a_policy_change(self):
		self._seed([PRESET])
		self.assertFalse(catalog.by_name(PRESET, providers=connector_catalogue.providers()).enabled)
		self._seed([])
		self.assertTrue(catalog.by_name(PRESET, providers=connector_catalogue.providers()).enabled)

	def test_never_pulled_means_the_shipped_catalog(self):
		self.assertEqual(connector_catalogue.disabled_names(), set())
		self.assertEqual(connector_catalogue.providers(), catalog.PROVIDERS)

	def test_corrupt_stored_value_fails_open_and_logs(self):
		frappe.db.set_single_value(SETTINGS, FIELD, "{not json", update_modified=False)
		with patch("frappe.log_error") as log:
			self.assertEqual(connector_catalogue.disabled_names(), set())
		log.assert_called_once()
