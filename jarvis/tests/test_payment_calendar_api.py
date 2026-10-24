"""Real permission-checked delivery; projections never bypass all-row visibility."""

import json
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.utils import add_days

from jarvis.payment_calendar import access, api, store
from jarvis.payment_calendar.projection import project_snapshot
from jarvis.payment_calendar.scope import normalize_scope
from jarvis.payment_calendar.settings import get_settings, update_safety_floor
from jarvis.payment_calendar.sources import read_obligations
from jarvis.tests.payment_calendar_fixtures import CalendarFixtures, require_calendar_test_site


class TestPaymentCalendarAPI(TestCase):
	def setUp(self):
		require_calendar_test_site()
		frappe.set_user("Administrator")
		frappe.db.savepoint("calendar_api")
		self.fx = CalendarFixtures()
		self.invoice = self.fx.invoice((40, 60))
		self.fx.invoice((90,), side="Receivable")
		update_safety_floor(self.fx.company, "20", 0)
		self.user = self.fx.user()
		frappe.get_doc("User", self.user).add_roles("Jarvis User")
		frappe.set_user(self.user)
		self.scope = normalize_scope(self.fx.company, [])
		proof = access.authorize_scope(self.scope)
		sources = {
			side: read_obligations(
				self.fx.company, side, self.fx.today, self.scope["window_start"], self.scope["window_end"]
			)
			for side in ("Payable", "Receivable")
		}
		self.result = {
			"sources": sources,
			"projection": project_snapshot(
				sources["Payable"],
				sources["Receivable"],
				None,
				"20",
				self.fx.today,
				self.scope["window_start"],
				self.scope["window_end"],
			),
			"bank": None,
			"bank_code": None,
			"floor": get_settings(self.fx.company),
			"currency": "INR",
			"generated_at": "2026-10-24T10:00:00+00:00",
			"unavailable": {},
		}
		self.sid, _ = store.admit(self.user, "b" * 64, self.scope, proof["fingerprint"])
		store.mark_running(self.user, self.sid)
		store.publish(self.user, self.sid, self.result)

	def tearDown(self):
		store.purge_actor(self.user)
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="calendar_api")
		frappe.clear_cache(doctype="Purchase Invoice")

	def test_status_delivers_aggregates_without_private_permission_proof_or_source_rows(self):
		api._load(self.sid)
		result = api.get_snapshot_status(self.sid)
		self.assertTrue(result["ok"], result)
		self.assertEqual(result["data"]["currency"], "INR")
		self.assertNotIn("_permission", json.dumps(result))
		self.assertNotIn("permission_columns", json.dumps(result))
		self.assertNotIn(self.invoice, json.dumps(result))

	def test_detail_and_upcoming_use_the_same_snapshot_without_running_sources(self):
		selected = add_days(self.fx.today, 10)
		with patch("jarvis.payment_calendar.snapshot.read_obligations") as source:
			detail = api.get_day_detail(self.sid, selected, "Payable", 1, 1)
			upcoming = api.get_upcoming(self.sid, self.fx.today)
			source.assert_not_called()
		self.assertTrue(detail["ok"], detail)
		self.assertEqual(detail["data"]["rows"][0]["remaining"], "40.00")
		self.assertEqual(detail["data"]["parties"][0]["remaining"], "40.00")
		self.assertNotIn("_permission", json.dumps(detail))
		self.assertEqual([r["remaining"] for r in upcoming["data"]["rows"]], ["40.00", "60.00"])

	def test_cross_user_snapshot_is_indistinguishable_from_expiry(self):
		frappe.set_user("Administrator")
		other = self.fx.user()
		frappe.get_doc("User", other).add_roles("Jarvis User")
		frappe.set_user(other)
		self.assertEqual(api.get_snapshot_status(self.sid), api.get_snapshot_status("0" * 32))

	def test_floor_change_blocks_all_old_aggregate_and_detail_delivery(self):
		frappe.set_user("Administrator")
		update_safety_floor(self.fx.company, "30", 1)
		frappe.set_user(self.user)
		result = api.get_snapshot_status(self.sid)
		self.assertFalse(result["ok"])
		self.assertEqual(result["code"], "scope_changed")
		self.assertNotIn("data", result)
		self.assertIsNone(store.load_result(self.user, self.sid))

	def test_floor_identity_uses_the_locked_row_even_if_a_plain_read_would_be_stale(self):
		with patch.object(api, "get_settings", return_value={"configuration_id": "old-deleted-row"}):
			result = api.configure_floor(self.fx.company, "99", 1, "old-deleted-row")
		self.assertFalse(result["ok"])
		self.assertEqual(result["code"], "settings_changed")
		self.assertEqual(get_settings(self.fx.company)["safety_floor"], "20.00")

	def test_a_failed_floor_request_rolls_back_its_partial_write(self):
		current = get_settings(self.fx.company)

		def fail(*args):
			frappe.db.set_value(
				"Jarvis Payment Calendar Settings", current["configuration_id"], "safety_floor", 99
			)
			raise RuntimeError("PRIVATE_FINANCIAL_EXCEPTION")

		with patch.object(api, "update_safety_floor", side_effect=fail), patch.object(frappe, "log_error"):
			result = api.configure_floor(self.fx.company, "99", 1, current["configuration_id"])
		self.assertFalse(result["ok"])
		self.assertEqual(get_settings(self.fx.company)["safety_floor"], "20.00")

	def test_live_invoice_query_change_blocks_aggregates_even_with_the_same_hook_identity(self):
		from jarvis.tests import test_payment_calendar_party_permissions as party_tests

		# Introduce a stateful hook before binding its identity, then change its
		# result without changing any policy metadata or the callback's path.
		original = frappe.get_hooks

		def hooks(key=None, *args, **kwargs):
			if key == "permission_query_conditions":
				value = original(key, *args, **kwargs).copy()
				value["Supplier"] = [
					"jarvis.tests.test_payment_calendar_party_permissions.supplier_query_condition"
				]
				return value
			return original(key, *args, **kwargs)

		with patch.object(frappe, "get_hooks", side_effect=hooks):
			party_tests._selected_supplier = self.fx.supplier
			proof = access.authorize_scope(self.scope)
			sid, _ = store.admit(self.user, "c" * 64, self.scope, proof["fingerprint"])
			store.mark_running(self.user, sid)
			store.publish(self.user, sid, self.result)
			party_tests._selected_supplier = "NO_SUCH_SUPPLIER"
			result = api.get_snapshot_status(sid)
			self.assertFalse(result["ok"], result)
			self.assertNotIn("data", result)
		party_tests._selected_supplier = None

	def test_invalid_dates_pages_and_sides_return_safe_envelopes(self):
		for date, side, page, size in (
			("2026-02-30", "Payable", 1, 25),
			(self.fx.today, "Unknown", 1, 25),
			(self.fx.today, "Payable", -1, 25),
			(self.fx.today, "Payable", 1, 101),
		):
			with self.subTest(date=date, side=side, page=page, size=size):
				result = api.get_day_detail(self.sid, date, side, page, size)
				self.assertFalse(result["ok"])
				self.assertNotIn("data", result)

	def test_error_envelopes_never_echo_exception_text_or_server_messages(self):
		with (
			patch.object(api, "_load", side_effect=ValueError("PRIVATE_SUPPLIER_AMOUNT")),
			patch.object(frappe, "log_error") as log,
		):
			frappe.local.message_log = ["PRIVATE_SUPPLIER_AMOUNT"]
			result = api.get_snapshot_status(self.sid)
			self.assertNotIn("PRIVATE_SUPPLIER_AMOUNT", json.dumps(result))
			self.assertFalse(frappe.local.message_log)
			self.assertNotIn("PRIVATE_SUPPLIER_AMOUNT", str(log.call_args))

	def test_failed_payable_source_cannot_be_reported_as_an_empty_upcoming_list(self):
		proof = access.authorize_scope(self.scope)
		result = {
			**self.result,
			"sources": {**self.result["sources"], "Payable": None},
			"unavailable": {"Payable": "too_large"},
		}
		result["projection"] = project_snapshot(
			None,
			result["sources"]["Receivable"],
			None,
			"20",
			self.fx.today,
			self.scope["window_start"],
			self.scope["window_end"],
		)
		sid, _ = store.admit(self.user, "f" * 64, self.scope, proof["fingerprint"])
		store.mark_running(self.user, sid)
		store.publish(self.user, sid, result)
		response = api.get_upcoming(sid, self.fx.today)
		self.assertFalse(response["ok"], response)
		self.assertEqual(response["state"], "unavailable")
		self.assertEqual(response["code"], "too_large")
		self.assertNotIn("data", response)

	def test_active_job_cap_refusal_uses_the_failure_envelope(self):
		with patch.object(api.jobs, "request_snapshot", side_effect=store.ActorBusy):
			response = api.request_snapshot(self.fx.company, [])
		self.assertFalse(response["ok"])
		self.assertEqual(response["code"], "worker_busy")
		self.assertEqual(response["state"], "unavailable")

	def test_forbidden_receivables_are_not_reported_as_a_retryable_outage(self):
		result = {
			**self.result,
			"sources": {**self.result["sources"], "Receivable": None},
			"unavailable": {"Receivable": "forbidden"},
		}
		with patch.object(api, "_complete", return_value=(store.load_status(self.user, self.sid), result)):
			response = api.get_day_detail(self.sid, self.fx.today, "Receivable")
		self.assertFalse(response["ok"])
		self.assertEqual(response["state"], "forbidden")
		self.assertFalse(response["retryable"])
