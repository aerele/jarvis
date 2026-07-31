"""Tenant error capture: scrubbing, fingerprinting, the report endpoint, the
jarvis-only Error Log reader, and the self-gating push."""

from __future__ import annotations

import contextlib
import time
from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api_errors, error_push

# NB: self-hosted mode was removed (commit 9f8d984a deleted ``jarvis/selfhost.py``),
# so error_push no longer imports it and push_error_rollup gates only on
# ``_admin_configured()``. ``test_pushes_when_admin_configured`` drives the real
# entry point - the earlier push tests only exercise ``_do_push``, which is how a
# stale ``import jarvis.selfhost`` shipped a silently-dead push job with green tests.

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
		# Fixed-width UPPERCASE alphanumeric IDs (GSTIN/PAN/serials) are ERP
		# identifiers, not taxonomy - they must not ride along.
		out = api_errors._scrub_error_text("filing for GSTIN 27AAPFU0939F1ZV was rejected")
		self.assertNotIn("27AAPFU0939F1ZV", out)
		# a pure-letter token (an exception class) has no digit and survives
		self.assertIn("PermissionDeniedError", api_errors._scrub_error_text("PermissionDeniedError: no"))
		# a CamelCase class name that HAPPENS to carry a digit has a lowercase run,
		# so it is not ID-shaped and survives (R2 - taxonomy must not be eaten).
		for cls in ("OAuth2Error", "Base64DecodeError", "Http404Error", "S3UploadError"):
			self.assertIn(cls, api_errors._scrub_error_text(f"raised {cls} in handler"), cls)

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

	def test_fingerprint_groups_by_scrubbed_message(self):
		# Different entity names in the SAME error shape scrub to the same text
		# ("Missing [NAME]") and MUST fold into one group - otherwise one bug across
		# N customers becomes N rows at count=1 (R1). Grouping keys on the scrubbed
		# message; _SCRUB_VERSION makes a future scrubber change a deliberate re-key.
		with _as(USER):
			api_errors.report_client_errors(
				[{"surface": "spa", "error_code": "e", "message": "Missing Tata Steel Ltd"}]
			)
			api_errors.report_client_errors(
				[{"surface": "spa", "error_code": "e", "message": "Missing Reliance Industries"}]
			)
		rows = frappe.get_all(DT, filters={"user": USER}, fields=["count", "message"])
		self.assertEqual(len(rows), 1, "same error shape, different names -> one group")
		self.assertEqual(rows[0].count, 2, "occurrences accumulate")
		# ...and the stored copy is scrubbed (the names never land on disk).
		self.assertNotIn("Tata Steel", rows[0].message)
		self.assertNotIn("Reliance Industries", rows[0].message)

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
		# GAP 1 heartbeat: its own push-path errors must not self-forward either (guards
		# the _REPORTER_SELF_MARKERS entry against a future drop/typo).
		heartbeat_tb = 'File "/x/apps/jarvis/jarvis/chat/heartbeat.py", line 60, in push_bench_heartbeat'
		self.assertFalse(api_errors.is_jarvis_error("jarvis.chat.heartbeat push failed", heartbeat_tb))

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
	# push_error_rollup gates only on _admin_configured() now (self-hosted mode
	# removed). Patch only the importable error_push / admin_client seams.
	def test_pushes_when_admin_configured(self):
		# Regression: the real */5 entry point must REACH the push when admin is
		# configured. A stale `from jarvis import selfhost` (module deleted in
		# 9f8d984a) made every tick raise ModuleNotFoundError, swallowed by the
		# never-raise guard, so tenant errors were silently never forwarded - and
		# the suite stayed green because the other push tests drive _do_push
		# directly and never push_error_rollup itself.
		with (
			patch.object(error_push, "_admin_configured", return_value=True),
			patch.object(error_push, "_do_push") as do_push,
			patch.object(error_push, "_log_push_failure_throttled") as logged,
		):
			error_push.push_error_rollup()
		do_push.assert_called_once()
		logged.assert_not_called()  # no swallowed import/other error

	def test_skips_when_admin_unconfigured(self):
		from jarvis import admin_client

		with (
			patch.object(error_push, "_admin_configured", return_value=False),
			patch.object(admin_client, "push_error_rollup") as push,
		):
			error_push.push_error_rollup()
		push.assert_not_called()

	def test_never_raises(self):
		# Any exception in the push path must be swallowed, not propagated. Force
		# one via the importable _admin_configured seam.
		with patch.object(error_push, "_admin_configured", side_effect=RuntimeError("boom")):
			error_push.push_error_rollup()


# --------------------------------------------------------------------------- #
# Push job — claim / confirm / revert (occurrences never lost, outage safe)
# --------------------------------------------------------------------------- #
class TestPushClaimConfirm(ApiErrorsBase):
	def _seed(self, message="boom"):
		with _as(USER):
			api_errors.report_client_errors([{"surface": "spa", "error_code": "e", "message": message}])

	def _run_do_push(self, push_impl):
		"""Drive the claim/confirm/revert core directly with the admin push replaced
		by ``push_impl``. Goes through ``_do_push`` (not ``push_error_rollup``) to
		isolate the claim/confirm/revert logic; the entry-point gate is covered by
		TestPushSelfGates. ``admin_client`` imports cleanly."""
		from jarvis import admin_client

		with patch.object(admin_client, "push_error_rollup", push_impl):
			error_push._do_push()

	def test_success_marks_pushed(self):
		self._seed()
		push = Mock()
		self._run_do_push(push)
		push.assert_called_once()
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 0}), 0, "claimed + sent")
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 1}), 1)

	def test_failed_push_reverts_claim(self):
		from jarvis.exceptions import AdminUnreachableError

		self._seed()
		# _do_push reverts the claim, then re-raises for push_error_rollup to
		# classify (silent for transient admin errors). We assert both here.
		with self.assertRaises(AdminUnreachableError):
			self._run_do_push(Mock(side_effect=AdminUnreachableError("down")))
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

		self._run_do_push(_during_push)
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 1}), 1, "original sent once")
		self.assertEqual(frappe.db.count(DT, {"user": USER, "pushed": 0}), 1, "mid-flight occurrence kept")
