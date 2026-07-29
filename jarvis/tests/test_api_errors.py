"""Tenant error capture: scrubbing, fingerprinting, the report endpoint, the
jarvis-only Error Log reader, and the self-gating push."""

from __future__ import annotations

import contextlib
import time
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import admin_client, api_errors, error_push, selfhost

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
		# a short, alphabetic *quoted* entity name is gone too (the old length/
		# digit heuristic left these behind)
		self.assertNotIn("ACME", out)

	def test_removes_bare_unquoted_entity_names(self):
		# The blocking finding: ERP messages interpolate entity names bare
		# (frappe.throw(_("...{0}...").format(name))), so they are neither quoted
		# nor numeric and no value regex catches them. A capitalised multi-word run
		# must be redacted.
		for text, leaked in (
			("Could not find Customer: 'Tata Steel Ltd'", "Tata Steel"),
			("Employee Priya Sharma has base salary 85000", "Priya Sharma"),
			("Item Titanium Rod not available for Supplier Reliance Industries", "Reliance Industries"),
			('customer_name "Bharat Petroleum"', "Bharat Petroleum"),
		):
			out = api_errors._scrub_error_text(text)
			self.assertNotIn(leaked, out, f"entity name leaked: {out!r}")
		# a single-token CamelCase exception class is NOT a run, so it survives
		self.assertIn("ValidationError", api_errors._scrub_error_text("ValidationError: rejected"))

	def test_redacts_id_shaped_tokens(self):
		# Fixed-width alphanumeric IDs (GSTIN/PAN/serials) mixing letters and digits
		# are ERP identifiers, not taxonomy - they must not ride along.
		out = api_errors._scrub_error_text("filing for GSTIN 27AAPFU0939F1ZV was rejected")
		self.assertNotIn("27AAPFU0939F1ZV", out)
		# a pure-letter token (an exception class) has no digit and survives
		self.assertIn("PermissionDeniedError", api_errors._scrub_error_text("PermissionDeniedError: no"))

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

	def test_fingerprint_uses_raw_message_not_scrubbed(self):
		# Two different entity names scrub to the SAME text ("Missing [NAME]") but
		# are different errors. Fingerprinting the RAW message keeps them distinct
		# AND decouples grouping from redaction policy, so a future scrubber tweak
		# never re-keys the admin feed (N6).
		with _as(USER):
			api_errors.report_client_errors(
				[{"surface": "spa", "error_code": "e", "message": "Missing Tata Steel Ltd"}]
			)
			api_errors.report_client_errors(
				[{"surface": "spa", "error_code": "e", "message": "Missing Reliance Industries"}]
			)
		self.assertEqual(frappe.db.count(DT, {"user": USER}), 2, "distinct raw messages stay distinct groups")
		# ...and both stored copies are scrubbed (the names never land on disk).
		for msg in frappe.get_all(DT, filters={"user": USER}, pluck="message"):
			self.assertNotIn("Tata Steel", msg)
			self.assertNotIn("Reliance Industries", msg)

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

	def test_drops_non_jarvis_origin_desk_error(self):
		# A stray ERPNext Desk error (positive non-jarvis asset marker in the raw
		# stack, no jarvis marker) is filtered server-side, not stored/forwarded.
		with _as(USER):
			r = api_errors.report_client_errors(
				[
					{
						"surface": "desk",
						"error_code": "uncaught",
						"message": "boom",
						"stack": "at handler (/assets/erpnext/js/erpnext.bundle.js:1:2)",
					}
				]
			)
		self.assertEqual(r["accepted"], 0)
		self.assertEqual(frappe.db.count(DT, {"user": USER}), 0)

	def test_keeps_jarvis_origin_desk_error(self):
		# Same shape, but the stack points at a jarvis asset -> kept.
		with _as(USER):
			r = api_errors.report_client_errors(
				[
					{
						"surface": "desk",
						"error_code": "uncaught",
						"message": "boom",
						"stack": "at handler (/assets/jarvis/js/jarvis_widget.bundle.js:1:2)",
					}
				]
			)
		self.assertEqual(r["accepted"], 1)
		self.assertEqual(frappe.db.count(DT, {"user": USER}), 1)

	def test_rate_limit_throttles_past_the_cap(self):
		# The endpoint short-circuits the limiter under frappe.flags.in_test, so
		# exercise the bucket directly with the flag flipped off. The clock is
		# FROZEN so the calendar-minute bucket cannot roll mid-loop (otherwise the
		# (N+1)th call would land in a fresh bucket and spuriously pass).
		probe = "ratelimit-probe@example.com"
		frozen = 1_800_000_000  # fixed epoch
		bucket = frozen // 60
		# incrby uses the raw redis key, site-prefixed; clear with delete().
		key = f"{frappe.local.site}:jarvis.client_error_report.{probe}.{bucket}"
		frappe.cache.delete(key)
		orig = frappe.flags.in_test
		frappe.flags.in_test = False
		try:
			with patch("jarvis.api_errors.time.time", return_value=frozen):
				for _ in range(api_errors.REPORT_RATE_PER_MIN):
					self.assertFalse(api_errors._over_report_rate_limit(probe))
				self.assertTrue(api_errors._over_report_rate_limit(probe), "the (N+1)th call is throttled")
		finally:
			frappe.flags.in_test = orig
			frappe.cache.delete(key)


# --------------------------------------------------------------------------- #
# Error Log reader — jarvis-only
# --------------------------------------------------------------------------- #
class TestErrorLogReader(ApiErrorsBase):
	def test_is_jarvis_error(self):
		self.assertTrue(api_errors.is_jarvis_error("jarvis.chat.api.send_message", ""))
		self.assertTrue(api_errors.is_jarvis_error("", 'File "/x/apps/jarvis/jarvis/api.py", line 3, in f'))
		self.assertFalse(api_errors.is_jarvis_error("erpnext.stock.get_stock", ""))
		self.assertFalse(api_errors.is_jarvis_error("frappe.model.document.save", "no app path"))

	def test_reporter_own_failures_are_not_forwarded(self):
		# The push job / endpoint failing must NOT feed back to admin about its own
		# outage - otherwise a */5 admin outage floods the feed once admin returns.
		push_tb = 'File "/x/apps/jarvis/jarvis/error_push.py", line 60, in push_error_rollup'
		self.assertFalse(api_errors.is_jarvis_error("jarvis errors: rollup push failed", push_tb))
		endpoint_tb = 'File "/x/apps/jarvis/jarvis/api_errors.py", line 40, in report_client_errors'
		self.assertFalse(api_errors.is_jarvis_error("", endpoint_tb))

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
			patch.object(selfhost, "is_self_hosted", return_value=True),
			patch.object(admin_client, "push_error_rollup") as push,
		):
			error_push.push_error_rollup()
		push.assert_not_called()

	def test_skips_when_admin_unconfigured(self):
		with (
			patch.object(selfhost, "is_self_hosted", return_value=False),
			patch.object(error_push, "_admin_configured", return_value=False),
			patch.object(admin_client, "push_error_rollup") as push,
		):
			error_push.push_error_rollup()
		push.assert_not_called()

	def test_never_raises(self):
		with (
			patch.object(selfhost, "is_self_hosted", side_effect=RuntimeError("boom")),
		):
			# Must swallow and log, not propagate.
			error_push.push_error_rollup()


# --------------------------------------------------------------------------- #
# Push job — claim / confirm / revert (occurrences never lost, outage safe)
# --------------------------------------------------------------------------- #
class TestPushClaimConfirm(ApiErrorsBase):
	def _seed(self, message="boom"):
		with _as(USER):
			api_errors.report_client_errors([{"surface": "spa", "error_code": "e", "message": message}])

	def _run_push(self, push_impl):
		"""Run push_error_rollup with the admin push replaced by ``push_impl``
		(a Mock or a plain callable)."""
		with (
			patch.object(selfhost, "is_self_hosted", return_value=False),
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(admin_client, "push_error_rollup", push_impl),
		):
			error_push.push_error_rollup()

	def test_success_marks_pushed(self):
		self._seed()
		push = Mock()
		self._run_push(push)
		push.assert_called_once()
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 0}), 0, "claimed + sent")
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 1}), 1)

	def test_failed_push_reverts_claim(self):
		from jarvis.exceptions import AdminUnreachableError

		self._seed()
		# Must not raise; the claimed row is reverted for the next cycle.
		self._run_push(Mock(side_effect=AdminUnreachableError("down")))
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 0}), 1)
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 1}), 0)

	def test_occurrence_arriving_after_claim_is_not_lost(self):
		self._seed("window boom")

		def _during_push(errors):
			# Same error reported again mid-flight: it must land in a FRESH pushed=0
			# row (it can't fold into the already-claimed one), so its count lives.
			with _as(USER):
				api_errors.report_client_errors(
					[{"surface": "spa", "error_code": "e", "message": "window boom"}]
				)

		self._run_push(_during_push)
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 1}), 1, "original sent once")
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 0}), 1, "mid-flight occurrence kept")
