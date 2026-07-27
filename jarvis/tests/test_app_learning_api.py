"""API-layer tests for jarvis.chat.app_learning_api.

The whole surface is manage-gated (System Manager / Jarvis Admin): consent is
mandatory to schedule, apps are validated against the installed non-core set,
an app with an in-progress run can't be re-queued, schedule times are validated,
cancel only works from Queued/Zipping/Analyzing, and the runs list whitelists
its filters/sort. The pipeline itself is covered by test_app_learning.py; here
we mock the app source so nothing is actually zipped or enqueued.
"""

from __future__ import annotations

from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from jarvis.chat import app_learning_api
from jarvis.learning import app_analysis
from jarvis.permissions import JARVIS_USER_ROLE

RUN = "Jarvis App Learning Run"

ADMIN_USER = "app-learn-api-admin@example.com"
PLAIN_USER = "app-learn-api-user@example.com"


def _ensure_user(email: str, roles: list[str]) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "AL",
				"last_name": "Test",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	u = frappe.get_doc("User", email)
	# strip any drifted roles, then set exactly the ones asked for
	u.set("roles", [])
	for r in roles:
		u.append("roles", {"role": r})
	u.save(ignore_permissions=True)
	frappe.db.commit()


class _ApiTestCase(FrappeTestCase):
	def setUp(self):
		_ensure_user(ADMIN_USER, ["System Manager"])
		_ensure_user(PLAIN_USER, [JARVIS_USER_ROLE])
		self._orig_user = frappe.session.user
		frappe.db.delete(RUN)
		frappe.db.commit()
		# Two fake installed custom apps; never actually zipped/enqueued.
		self._patches = [
			mock.patch.object(app_analysis, "_installed_custom_apps", return_value=["fakeapp", "otherapp"]),
			mock.patch.object(
				app_analysis,
				"list_custom_apps_data",
				return_value=[
					{
						"app": "fakeapp",
						"title": "Fake App",
						"installed_version": "1.0",
						"path_ok": True,
						"approx_files": 3,
						"approx_kb": 4,
						"last_run": None,
					},
					{
						"app": "otherapp",
						"title": "Other App",
						"installed_version": "2.0",
						"path_ok": True,
						"approx_files": 5,
						"approx_kb": 9,
						"last_run": None,
					},
				],
			),
			mock.patch.object(app_analysis, "_enqueue_tick", return_value=None),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()
		frappe.set_user(self._orig_user)
		frappe.db.delete(RUN)
		frappe.db.commit()

	def _schedule(self, apps, when="", consent=1):
		return app_learning_api.schedule_app_learning(frappe.as_json(apps), when, consent)


class TestGating(_ApiTestCase):
	def test_plain_user_is_denied_everywhere(self):
		frappe.set_user(PLAIN_USER)
		self.assertRaises(frappe.PermissionError, app_learning_api.list_custom_apps)
		self.assertRaises(frappe.PermissionError, app_learning_api.get_app_learning_overview)
		self.assertRaises(frappe.PermissionError, app_learning_api.list_app_learning_runs_page)
		self.assertRaises(
			frappe.PermissionError,
			app_learning_api.schedule_app_learning,
			frappe.as_json(["fakeapp"]),
			"",
			1,
		)

	def test_admin_can_read(self):
		frappe.set_user(ADMIN_USER)
		self.assertTrue(app_learning_api.list_custom_apps()["ok"])
		ov = app_learning_api.get_app_learning_overview()["data"]
		self.assertIn("active_run", ov)
		self.assertIn("apps", ov)
		self.assertEqual(len(ov["apps"]), 2)


class TestSchedule(_ApiTestCase):
	def setUp(self):
		super().setUp()
		# CA3-5: ``schedule_app_learning`` refuses ONLY while the legacy pipeline is retired
		# (the default). These tests exercise the RETAINED scheduling body, which is reachable
		# only when the pipeline is re-enabled for rollback — so run them un-retired. (The
		# refuse-while-retired config is covered by test_app_learning_agent's
		# TestLegacyScheduleRetired.)
		p = mock.patch.object(app_analysis, "_legacy_retired", return_value=False)
		p.start()
		self.addCleanup(p.stop)

	def test_consent_is_mandatory(self):
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			self._schedule(["fakeapp"], consent=0)
		self.assertEqual(frappe.db.count(RUN), 0)

	def test_unknown_app_is_rejected(self):
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			self._schedule(["frappe"])  # a core/excluded app, not in the installed set
		self.assertEqual(frappe.db.count(RUN), 0)

	def test_empty_selection_is_rejected(self):
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			self._schedule([])

	def test_run_now_creates_queued_rows_per_app(self):
		frappe.set_user(ADMIN_USER)
		r = self._schedule(["fakeapp", "otherapp"])
		self.assertEqual(len(r["data"]["runs"]), 2)
		rows = frappe.get_all(RUN, fields=["app", "status", "requested_by", "consent_at"])
		self.assertEqual({x.app for x in rows}, {"fakeapp", "otherapp"})
		self.assertTrue(all(x.status == "Queued" for x in rows))
		self.assertTrue(all(x.requested_by == ADMIN_USER for x in rows))
		self.assertTrue(all(x.consent_at for x in rows))

	def test_duplicate_non_terminal_run_is_rejected_atomically(self):
		frappe.set_user(ADMIN_USER)
		frappe.get_doc(
			{
				"doctype": RUN,
				"app": "fakeapp",
				"status": "Analyzing",
				"requested_by": ADMIN_USER,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		# scheduling fakeapp + otherapp must reject BEFORE inserting either
		with self.assertRaises(frappe.ValidationError):
			self._schedule(["otherapp", "fakeapp"])
		self.assertEqual(frappe.db.count(RUN, {"app": "otherapp"}), 0)

	def test_terminal_run_does_not_block_rescheduling(self):
		frappe.set_user(ADMIN_USER)
		frappe.get_doc(
			{
				"doctype": RUN,
				"app": "fakeapp",
				"status": "Completed",
				"requested_by": ADMIN_USER,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		r = self._schedule(["fakeapp"])
		self.assertEqual(len(r["data"]["runs"]), 1)

	def test_past_schedule_time_is_rejected(self):
		frappe.set_user(ADMIN_USER)
		past = str(add_to_date(now_datetime(), hours=-1))
		with self.assertRaises(frappe.ValidationError):
			self._schedule(["fakeapp"], when=past)

	def test_too_far_out_schedule_is_rejected(self):
		frappe.set_user(ADMIN_USER)
		far = str(add_to_date(now_datetime(), days=app_learning_api.MAX_SCHEDULE_DAYS_OUT + 2))
		with self.assertRaises(frappe.ValidationError):
			self._schedule(["fakeapp"], when=far)

	def test_future_schedule_is_accepted_and_not_ticked(self):
		frappe.set_user(ADMIN_USER)
		soon = str(add_to_date(now_datetime(), days=1))
		r = self._schedule(["fakeapp"], when=soon)
		self.assertEqual(len(r["data"]["runs"]), 1)
		# a future run must not kick the tick (only run-now does)
		app_analysis._enqueue_tick.assert_not_called()


class TestCancel(_ApiTestCase):
	def _mk(self, status: str) -> str:
		doc = frappe.get_doc(
			{
				"doctype": RUN,
				"app": "fakeapp",
				"status": status,
				"requested_by": ADMIN_USER,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def test_cancel_queued_run(self):
		frappe.set_user(ADMIN_USER)
		name = self._mk("Queued")
		self.assertTrue(app_learning_api.cancel_app_learning_run(name)["ok"])
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "Cancelled")

	def test_cannot_cancel_ingesting_or_terminal(self):
		frappe.set_user(ADMIN_USER)
		for status in ("Ingesting", "Completed", "Failed", "Cancelled"):
			name = self._mk(status)
			with self.assertRaises(frappe.ValidationError):
				app_learning_api.cancel_app_learning_run(name)


class TestRunsList(_ApiTestCase):
	def _seed(self, n: int):
		for i in range(n):
			frappe.get_doc(
				{
					"doctype": RUN,
					"app": "fakeapp" if i % 2 else "otherapp",
					"status": "Completed" if i % 2 else "Failed",
					"requested_by": ADMIN_USER,
					"error": f"boom-{i}",
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_envelope_and_pagination(self):
		frappe.set_user(ADMIN_USER)
		self._seed(5)
		page = app_learning_api.list_app_learning_runs_page(page_length=2)["data"]
		self.assertEqual(len(page["rows"]), 2)
		self.assertEqual(page["total"], 5)
		self.assertTrue(page["has_more"])

	def test_status_and_app_filters(self):
		frappe.set_user(ADMIN_USER)
		self._seed(6)
		page = app_learning_api.list_app_learning_runs_page(filters=frappe.as_json({"status": "Failed"}))[
			"data"
		]
		self.assertTrue(all(r["status"] == "Failed" for r in page["rows"]))

	def test_search_matches_error(self):
		frappe.set_user(ADMIN_USER)
		self._seed(4)
		page = app_learning_api.list_app_learning_runs_page(search="boom-1")["data"]
		self.assertTrue(any("boom-1" in (r["error"] or "") for r in page["rows"]))

	def test_unknown_filter_and_sort_throw(self):
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			app_learning_api.list_app_learning_runs_page(filters=frappe.as_json({"requested_by": ADMIN_USER}))
		with self.assertRaises(frappe.ValidationError):
			app_learning_api.list_app_learning_runs_page(sort_field="requested_by")

	def test_invalid_status_filter_value_throws(self):
		frappe.set_user(ADMIN_USER)
		with self.assertRaises(frappe.ValidationError):
			app_learning_api.list_app_learning_runs_page(filters=frappe.as_json({"status": "Bogus"}))


class TestRetireLegacyRunsMigration(_ApiTestCase):
	"""CA-5: the upgrade migration must not strand a run that was Queued (or
	mid-flight) before the chat-batch pipeline was retired — nothing advances it
	and the frontend surfaces were removed. It is terminalized to Cancelled with a
	retirement reason; historical terminal rows are untouched; re-run is a no-op."""

	def _mk(self, status: str, error: str = "") -> str:
		doc = frappe.get_doc(
			{"doctype": RUN, "app": "fakeapp", "status": status, "requested_by": ADMIN_USER, "error": error}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name

	def test_non_terminal_rows_are_cancelled_with_reason(self):
		from jarvis.patches.v2_06_retire_app_learning_runs import execute

		non_terminal = {s: self._mk(s) for s in ("Queued", "Zipping", "Analyzing", "Ingesting")}
		done = self._mk("Completed")
		failed = self._mk("Failed", error="real failure")

		execute()
		frappe.db.commit()

		for status, name in non_terminal.items():
			row = frappe.db.get_value(RUN, name, ["status", "error", "finished_at"], as_dict=True)
			self.assertEqual(row.status, "Cancelled", f"{status} row must be terminalized")
			self.assertIn("retired", (row.error or "").lower())
			self.assertTrue(row.finished_at)
		# historical terminal rows are untouched (status + a real failure reason)
		self.assertEqual(frappe.db.get_value(RUN, done, "status"), "Completed")
		self.assertEqual(frappe.db.get_value(RUN, failed, "status"), "Failed")
		self.assertEqual(frappe.db.get_value(RUN, failed, "error"), "real failure")

		# idempotent: a second run leaves everything terminal, nothing to change
		execute()
		frappe.db.commit()
		self.assertEqual(
			frappe.db.count(RUN, {"status": ["in", ("Queued", "Zipping", "Analyzing", "Ingesting")]}), 0
		)
