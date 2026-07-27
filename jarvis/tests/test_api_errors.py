"""Tenant error capture: scrubbing, fingerprinting, the report endpoint, the
jarvis-only Error Log reader, and the self-gating push."""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api_errors, error_push

DT = api_errors.DT
USER = "apierr-user@example.com"


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


class ApiErrorsBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": USER,
					"first_name": "apierr",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			).insert(ignore_permissions=True)

	def setUp(self):
		super().setUp()
		frappe.db.delete(DT)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(DT)
		frappe.db.commit()
		super().tearDown()


# --------------------------------------------------------------------------- #
# Scrubbing
# --------------------------------------------------------------------------- #
class TestScrub(ApiErrorsBase):
	def test_redacts_data_keeps_taxonomy(self):
		text = (
			"PermissionDeniedError: user other@corp.com cannot read invoice 'ACME Corp Pvt Ltd' "
			"balance 42.50 token=sk-abcdef1234567890abcdef hash 0123456789abcdef0123456789abcdef"
		)
		out = api_errors._scrub_error_text(text, keep_email=USER)
		# taxonomy / structure survives
		self.assertIn("PermissionDeniedError", out)
		# values are gone
		self.assertNotIn("other@corp.com", out)
		self.assertNotIn("42.50", out)
		self.assertNotIn("sk-abcdef1234567890abcdef", out)
		self.assertNotIn("0123456789abcdef0123456789abcdef", out)

	def test_keeps_the_reporting_users_own_email(self):
		out = api_errors._scrub_error_text(f"failed for {USER}", keep_email=USER)
		self.assertIn(USER, out)

	def test_empty_is_safe(self):
		self.assertEqual(api_errors._scrub_error_text(None), "")


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #
class TestFingerprint(ApiErrorsBase):
	def test_digits_normalized_so_similar_errors_group(self):
		a = api_errors._fingerprint("run_error", "RunError", "42 units missing", "spa_chat")
		b = api_errors._fingerprint("run_error", "RunError", "7 units missing", "spa_chat")
		self.assertEqual(a, b)

	def test_different_class_or_surface_splits(self):
		a = api_errors._fingerprint("run_error", "RunError", "boom", "spa_chat")
		b = api_errors._fingerprint("run_error", "RunError", "boom", "onboarding")
		self.assertNotEqual(a, b)


# --------------------------------------------------------------------------- #
# report_client_errors
# --------------------------------------------------------------------------- #
class TestReportEndpoint(ApiErrorsBase):
	def test_inserts_and_dedupes(self):
		# Messages differing only by digits normalize to one fingerprint (the same
		# error recurring with a different number), so they fold into one row.
		row = {
			"surface": "spa_chat",
			"error_code": "run_error",
			"error_class": "RunError",
			"message": "the run failed after 3 tries",
			"route": "/c/abc?x=1",
		}
		with _as(USER):
			r1 = api_errors.report_client_errors([row])
			r2 = api_errors.report_client_errors([dict(row, message="the run failed after 8 tries")])
		self.assertEqual(r1["accepted"], 1)
		self.assertEqual(r2["accepted"], 1)
		rows = frappe.get_all(DT, filters={"user": USER}, fields=["name", "count", "route", "message"])
		self.assertEqual(len(rows), 1, "same fingerprint must fold into one row")
		self.assertEqual(rows[0].count, 2)
		self.assertEqual(rows[0].route, "/c/abc", "query string stripped")
		self.assertIn("8 tries", rows[0].message, "the newest sample overwrites")

	def test_different_word_is_a_different_group(self):
		with _as(USER):
			api_errors.report_client_errors([{"surface": "spa", "error_code": "e", "message": "disk full"}])
			api_errors.report_client_errors(
				[{"surface": "spa", "error_code": "e", "message": "network down"}]
			)
		self.assertEqual(frappe.db.count(DT, {"user": USER}), 2)

	def test_scrubs_before_store(self):
		with _as(USER):
			api_errors.report_client_errors(
				[{"surface": "spa_chat", "error_code": "x", "message": "amount 99.99 for other@corp.com"}]
			)
		msg = frappe.get_all(DT, filters={"user": USER}, pluck="message")[0]
		self.assertNotIn("99.99", msg)
		self.assertNotIn("other@corp.com", msg)

	def test_guest_refused(self):
		with _as("Guest"), self.assertRaises(frappe.AuthenticationError):
			api_errors.report_client_errors([{"message": "x"}])

	def test_json_string_body_accepted(self):
		with _as(USER):
			r = api_errors.report_client_errors('[{"surface":"pwa","error_code":"c","message":"m"}]')
		self.assertEqual(r["accepted"], 1)

	def test_bad_rows_do_not_sink_the_batch(self):
		with _as(USER):
			r = api_errors.report_client_errors([None, "nope", {"surface": "spa", "message": "ok"}])
		self.assertEqual(r["accepted"], 1)


# --------------------------------------------------------------------------- #
# Error Log reader — jarvis-only
# --------------------------------------------------------------------------- #
class TestErrorLogReader(ApiErrorsBase):
	def test_is_jarvis_error(self):
		self.assertTrue(api_errors.is_jarvis_error("jarvis.chat.api.send_message", ""))
		self.assertTrue(api_errors.is_jarvis_error("", 'File "/x/apps/jarvis/jarvis/api.py", line 3, in f'))
		self.assertFalse(api_errors.is_jarvis_error("erpnext.stock.get_stock", ""))
		self.assertFalse(api_errors.is_jarvis_error("frappe.model.document.save", "no app path"))

	def test_collect_filters_to_jarvis_and_advances_watermark(self):
		jtb = (
			"Traceback (most recent call last):\n"
			'  File "/home/x/apps/jarvis/jarvis/chat/api.py", line 10, in send_message\n'
			"    boom()\n"
			"ValueError: bad thing 4242"
		)
		etb = (
			"Traceback (most recent call last):\n"
			'  File "/home/x/apps/erpnext/erpnext/stock/x.py", line 5, in get\n'
			"    boom()\n"
			"KeyError: nope"
		)
		j = frappe.get_doc({"doctype": "Error Log", "method": "jarvis.chat.api.send_message", "error": jtb})
		j.insert(ignore_permissions=True)
		e = frappe.get_doc({"doctype": "Error Log", "method": "erpnext.stock.x.get", "error": etb})
		e.insert(ignore_permissions=True)
		frappe.db.commit()

		result = api_errors.collect_error_log(since=None, limit=500)
		classes = [r["error_class"] for r in result["rows"]]
		self.assertIn("ValueError", classes)
		self.assertNotIn("KeyError", classes, "an ERPNext exception must never be forwarded")
		# a forwarded jarvis row is scrubbed (the digits in the message are gone)
		vrow = next(r for r in result["rows"] if r["error_class"] == "ValueError")
		self.assertNotIn("4242", vrow["message"])
		self.assertEqual(vrow["kind"], "exception")
		self.assertIsNotNone(result["watermark"])


# --------------------------------------------------------------------------- #
# Push job — self-gating + never-raise
# --------------------------------------------------------------------------- #
class TestPushSelfGates(ApiErrorsBase):
	def test_skips_when_self_hosted(self):
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=True),
			patch("jarvis.admin_client.push_error_rollup") as push,
		):
			error_push.push_error_rollup()
		push.assert_not_called()

	def test_skips_when_admin_unconfigured(self):
		with (
			patch("jarvis.selfhost.is_self_hosted", return_value=False),
			patch("jarvis.error_push._admin_configured", return_value=False),
			patch("jarvis.admin_client.push_error_rollup") as push,
		):
			error_push.push_error_rollup()
		push.assert_not_called()

	def test_never_raises(self):
		with (
			patch("jarvis.selfhost.is_self_hosted", side_effect=RuntimeError("boom")),
		):
			# Must swallow and log, not propagate.
			error_push.push_error_rollup()
