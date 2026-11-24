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


KEY_FIELD = "connector_key_overrides"
KEY_SYNCED_AT = "connector_key_overrides_synced_at"
KEY_MARKER = "connector_key_policy_supported"
SIGN_IN_ONLY = "Sentry"  # dcr, ships accepts_key=False
KEY_OK = "Linear"  # dcr, ships accepts_key=True
KEY_ONLY = "Stripe"  # token class: always takes a key


def _key_conn(**extra) -> dict:
	return {KEY_MARKER: True, **extra}


def _stored_overrides() -> dict | None:
	raw = frappe.db.get_single_value(SETTINGS, KEY_FIELD, cache=False)
	return json.loads(raw) if raw else None


class TestConnectorKeyOverridesMirror(FrappeTestCase):
	def setUp(self):
		fields = (KEY_FIELD, KEY_SYNCED_AT)
		self._saved = {f: frappe.db.get_single_value(SETTINGS, f, cache=False) for f in fields}
		frappe.db.set_single_value(SETTINGS, {KEY_FIELD: "", KEY_SYNCED_AT: None}, update_modified=False)

	def tearDown(self):
		frappe.db.set_single_value(SETTINGS, self._saved, update_modified=False)

	def _seed(self, overrides: dict) -> None:
		connector_catalogue.persist_key_overrides_from_connection(
			_key_conn(connector_key_overrides=overrides)
		)
		self.assertEqual(_stored_overrides(), overrides)

	def test_the_presets_used_here_ship_as_described(self):
		self.assertFalse(catalog.accepts_key_of(SIGN_IN_ONLY))
		self.assertTrue(catalog.accepts_key_of(KEY_OK))
		self.assertEqual(catalog.auth_of(SIGN_IN_ONLY), catalog.AUTH_DCR)
		self.assertEqual(catalog.auth_of(KEY_ONLY), catalog.AUTH_TOKEN)

	def test_well_formed_map_is_authoritative(self):
		self._seed({SIGN_IN_ONLY: True, KEY_OK: False})
		self.assertEqual(connector_catalogue.key_overrides(), {SIGN_IN_ONLY: True, KEY_OK: False})
		self.assertTrue(frappe.db.get_single_value(SETTINGS, KEY_SYNCED_AT, cache=False))

	def test_empty_map_is_authoritative_and_clears(self):
		self._seed({SIGN_IN_ONLY: True})
		connector_catalogue.persist_key_overrides_from_connection(_key_conn(connector_key_overrides={}))
		self.assertEqual(_stored_overrides(), {})
		self.assertEqual(connector_catalogue.key_overrides(), {})

	def test_names_are_stripped(self):
		connector_catalogue.persist_key_overrides_from_connection(
			_key_conn(connector_key_overrides={f" {KEY_OK} ": False})
		)
		self.assertEqual(_stored_overrides(), {KEY_OK: False})

	def test_marker_absent_keeps_last_applied(self):
		self._seed({SIGN_IN_ONLY: True})
		connector_catalogue.persist_key_overrides_from_connection({KEY_FIELD: {}})
		self.assertEqual(_stored_overrides(), {SIGN_IN_ONLY: True})

	def test_key_absent_keeps_last_applied(self):
		self._seed({SIGN_IN_ONLY: True})
		connector_catalogue.persist_key_overrides_from_connection(_key_conn())
		self.assertEqual(_stored_overrides(), {SIGN_IN_ONLY: True})

	def test_malformed_payload_keeps_last_applied_and_logs(self):
		self._seed({SIGN_IN_ONLY: True})
		bad_payloads = (
			[SIGN_IN_ONLY],
			"Sentry",
			None,
			{SIGN_IN_ONLY: "true"},
			{SIGN_IN_ONLY: 1},
			{SIGN_IN_ONLY: None},
			{"": True},
			{"  ": True},
			{SIGN_IN_ONLY: True, f" {SIGN_IN_ONLY}": False},
		)
		for bad in bad_payloads:
			with self.subTest(payload=bad), patch("frappe.log_error") as log:
				connector_catalogue.persist_key_overrides_from_connection(
					_key_conn(connector_key_overrides=bad)
				)
				self.assertEqual(_stored_overrides(), {SIGN_IN_ONLY: True})
				log.assert_called_once()
				self.assertIn("malformed", log.call_args.kwargs["title"])

	def test_never_raises(self):
		with (
			patch("frappe.db.set_single_value", side_effect=RuntimeError("db down")),
			patch("frappe.log_error") as log,
		):
			connector_catalogue.persist_key_overrides_from_connection(
				_key_conn(connector_key_overrides={KEY_OK: False})
			)
		log.assert_called_once()

	def test_never_pulled_means_the_code_defaults(self):
		self.assertEqual(connector_catalogue.key_overrides(), {})
		self.assertFalse(connector_catalogue.accepts_key(SIGN_IN_ONLY))
		self.assertTrue(connector_catalogue.accepts_key(KEY_OK))

	def test_corrupt_stored_value_falls_back_and_logs(self):
		frappe.db.set_single_value(SETTINGS, KEY_FIELD, "{not json", update_modified=False)
		with patch("frappe.log_error") as log:
			self.assertEqual(connector_catalogue.key_overrides(), {})
		log.assert_called_once()

	def test_override_turns_a_key_on_and_off_for_a_switchable_preset(self):
		self._seed({SIGN_IN_ONLY: True, KEY_OK: False})
		self.assertTrue(connector_catalogue.accepts_key(SIGN_IN_ONLY))
		self.assertFalse(connector_catalogue.accepts_key(KEY_OK))

	def test_override_is_ignored_off_a_switchable_preset(self):
		self._seed({KEY_ONLY: False, catalog.CUSTOM_URL: False, "No Such App": False})
		self.assertTrue(connector_catalogue.accepts_key(KEY_ONLY), "token class always takes a key")
		self.assertTrue(connector_catalogue.accepts_key(catalog.CUSTOM_URL))
		self.assertTrue(connector_catalogue.accepts_key("No Such App"))

	def test_public_catalog_shows_the_effective_value(self):
		self._seed({SIGN_IN_ONLY: True, KEY_OK: False, KEY_ONLY: False})
		by_name = {row["name"]: row for row in connector_catalogue.public_catalog()}
		self.assertTrue(by_name[SIGN_IN_ONLY]["accepts_key"])
		self.assertFalse(by_name[KEY_OK]["accepts_key"])
		self.assertTrue(by_name[KEY_ONLY]["accepts_key"])

	def test_public_catalog_without_overrides_matches_the_shipped_one(self):
		self.assertEqual(
			connector_catalogue.public_catalog(), catalog.to_public(providers=connector_catalogue.providers())
		)
