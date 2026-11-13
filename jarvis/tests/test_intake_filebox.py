"""File Box intake list and retry: who sees which rows, and when a row may run again."""

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import filebox
from jarvis.intake import capture, worker

TRIGGER = "Jarvis Trigger"
ACTIVITY = "Jarvis Trigger Activity"
UPLOADER = "jarvis-fb-uploader@example.com"
OTHER = "jarvis-fb-other@example.com"
MANAGER = "jarvis-fb-manager@example.com"


def _ensure_user(email: str, roles: tuple = ()) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Fb",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.add_roles(*roles) if roles else None


class TestIntakeFilebox(FrappeTestCase):
	def setUp(self):
		_ensure_user(UPLOADER)
		_ensure_user(OTHER)
		_ensure_user(MANAGER, ("System Manager",))
		self._orig = frappe.session.user
		self._names: list[str] = []
		self._triggers: list[str] = []
		# The gate is a separate concern (test_chat_endpoint_gating covers it).
		p = mock.patch("jarvis.permissions.require_jarvis_access")
		p.start()
		self.addCleanup(p.stop)
		self._gate = p
		frappe.db.delete(ACTIVITY, {"action_type": "Intake"})

	def tearDown(self):
		frappe.set_user(self._orig)
		for name in self._names:
			frappe.db.delete(ACTIVITY, {"name": name})
		for name in self._triggers:
			frappe.delete_doc(TRIGGER, name, ignore_permissions=True, force=True)

	def _rule(self, **over) -> str:
		doc = {
			"doctype": TRIGGER,
			"trigger_name": f"fb-{frappe.generate_hash(length=8)}",
			"action_type": "Intake",
			"intake_source": "Folder",
			"intake_folder": "Home/Attachments",
			"intake_target_doctype": "ToDo",
			"intake_spec": json.dumps({"fields": ["description"]}),
			"enabled": 1,
		}
		doc.update(over)
		name = frappe.get_doc(doc).insert(ignore_permissions=True).name
		self._triggers.append(name)
		return name

	def _row(self, owner=UPLOADER, trigger=None, **fields) -> str:
		# A draft that was deleted since is a dangling link: set it after the insert.
		draft = {k: fields.pop(k) for k in ("draft_doctype", "draft_name") if k in fields}
		doc = frappe.get_doc(
			{
				"doctype": ACTIVITY,
				"trigger": trigger,
				"trigger_label": "fb",
				"action_type": "Intake",
				"status": "Failed",
				"event_user": owner,
				"summary": "Reading the file failed.",
				**fields,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(ACTIVITY, doc.name, {"owner": owner, **draft}, update_modified=False)
		self._names.append(doc.name)
		return doc.name

	def _list(self, user, **kw):
		frappe.set_user(user)
		return filebox.list_intake_page(**kw)

	def test_mine_lists_only_own_rows(self):
		mine, theirs = self._row(UPLOADER), self._row(OTHER)
		rows = {r["name"] for r in self._list(UPLOADER)["rows"]}
		self.assertIn(mine, rows)
		self.assertNotIn(theirs, rows)

	def test_all_scope_needs_trigger_manager(self):
		mine, theirs = self._row(UPLOADER), self._row(OTHER)
		with self.assertRaises(frappe.PermissionError):
			self._list(UPLOADER, scope="all")
		rows = {r["name"] for r in self._list(MANAGER, scope="all")["rows"]}
		self.assertTrue({mine, theirs} <= rows)
		# "mine" for a manager stays their own rows.
		self.assertFalse({mine, theirs} & {r["name"] for r in self._list(MANAGER)["rows"]})

	def test_row_shape_and_source_label(self):
		folder = self._rule(intake_folder="Home/Attachments")
		record = self._rule(intake_source="Record", intake_record_doctype="Customer", intake_folder="")
		a = self._row(trigger=folder, status="Success", summary="Draft ready")
		b = self._row(trigger=record, target_docname="CUST-1")
		rows = {r["name"]: r for r in self._list(UPLOADER)["rows"]}
		self.assertEqual(rows[a]["source_label"], "Folder: Attachments")
		self.assertEqual(rows[b]["source_label"], "Record: Customer CUST-1")
		self.assertNotIn("detail", rows[a])
		self.assertEqual(
			set(rows[a]),
			{
				"name",
				"creation",
				"file_name",
				"file_url",
				"source_label",
				"status",
				"summary",
				"draft_doctype",
				"draft_name",
				"approval_request",
				"can_retry",
			},
		)

	def test_filters_search_and_paging(self):
		self._row(status="Failed", summary="alpha_one")
		self._row(status="Success", summary="beta")
		res = self._list(UPLOADER, filters={"status": "Failed"})
		self.assertEqual({r["status"] for r in res["rows"]}, {"Failed"})
		self.assertEqual(len(self._list(UPLOADER, search="alpha_one")["rows"]), 1)
		# "_" is a literal, not a wildcard
		self.assertEqual(len(self._list(UPLOADER, search="alpha_x")["rows"]), 0)
		page = self._list(UPLOADER, page_length=1)
		self.assertEqual((len(page["rows"]), page["total"], page["has_more"]), (1, 2, True))
		with self.assertRaises(frappe.ValidationError):
			self._list(UPLOADER, filters={"owner": "x"})
		with self.assertRaises(frappe.ValidationError):
			self._list(UPLOADER, filters={"status": "Bogus"})
		with self.assertRaises(frappe.ValidationError):
			self._list(UPLOADER, filters={"from_date": "nope"})

	def test_can_retry_rules(self):
		failed = self._row(UPLOADER, status="Failed")
		running = self._row(UPLOADER, status="Running")
		drafted = self._row(UPLOADER, status="Failed", draft_doctype="User", draft_name=OTHER)
		gone = self._row(UPLOADER, status="Failed", draft_doctype="User", draft_name="nobody@x.com")
		dup = self._row(UPLOADER, status="Skipped", duplicate_of=failed)
		rows = {r["name"]: r["can_retry"] for r in self._list(UPLOADER)["rows"]}
		self.assertEqual(rows, {failed: True, running: False, drafted: False, gone: True, dup: False})
		# a manager viewing another person's row may retry it
		self.assertTrue({r["name"]: r for r in self._list(MANAGER, scope="all")["rows"]}[failed]["can_retry"])

	def test_retry_resets_and_enqueues(self):
		name = self._row(UPLOADER, status="Failed", reading_json='{"description": "x"}')
		frappe.set_user(UPLOADER)
		with mock.patch("frappe.enqueue") as enq:
			res = filebox.retry_intake(name)
		self.assertEqual(res, {"ok": True, "status": "Queued"})
		row = frappe.db.get_value(ACTIVITY, name, ["status", "summary", "reading_json"], as_dict=True)
		self.assertEqual((row.status, row.summary), ("Queued", "Queued again"))
		self.assertTrue(row.reading_json)  # draft-only retry costs no AI call
		kw = enq.call_args.kwargs
		self.assertEqual(enq.call_args.args[0], "jarvis.intake.worker.run_job")
		self.assertEqual((kw["activity"], kw["job_id"]), (name, f"intake:{name}"))
		# a second click finds the row already Queued
		with mock.patch("frappe.enqueue") as enq2, self.assertRaises(frappe.ValidationError):
			filebox.retry_intake(name)
		enq2.assert_not_called()

	def test_uploader_without_jarvis_access_can_retry_their_own_row(self):
		"""The uploader persona has no Jarvis role, so the Jarvis gate must not apply."""
		self._gate.stop()
		own = self._row(UPLOADER, status="Failed")
		theirs = self._row(OTHER, status="Failed")
		frappe.set_user(UPLOADER)
		with mock.patch("frappe.enqueue") as enq:
			self.assertEqual(filebox.retry_intake(own)["status"], "Queued")
			with self.assertRaises(frappe.PermissionError):
				filebox.retry_intake(theirs)
		enq.assert_called_once()

	def test_retry_clears_reading_after_no_image(self):
		name = self._row(UPLOADER, status="Failed", summary=worker.NO_IMAGE_SUMMARY, reading_json='{"a": 1}')
		frappe.set_user(UPLOADER)
		with mock.patch("frappe.enqueue"):
			filebox.retry_intake(name)
		self.assertFalse(frappe.db.get_value(ACTIVITY, name, "reading_json"))

	def test_retry_refused_when_draft_exists(self):
		name = self._row(UPLOADER, status="Failed", draft_doctype="User", draft_name=OTHER)
		frappe.set_user(UPLOADER)
		with mock.patch("frappe.enqueue") as enq, self.assertRaises(frappe.ValidationError):
			filebox.retry_intake(name)
		enq.assert_not_called()
		self.assertEqual(frappe.db.get_value(ACTIVITY, name, "status"), "Failed")

	def test_retry_refused_for_strangers_duplicates_and_other_statuses(self):
		mine = self._row(UPLOADER, status="Failed")
		dup = self._row(UPLOADER, status="Skipped", duplicate_of=mine)
		running = self._row(UPLOADER, status="Running")
		with mock.patch("frappe.enqueue") as enq:
			frappe.set_user(OTHER)
			with self.assertRaises(frappe.PermissionError):
				filebox.retry_intake(mine)
			frappe.set_user(UPLOADER)
			for bad in (dup, running):
				with self.assertRaises(frappe.ValidationError):
					filebox.retry_intake(bad)
			frappe.set_user(MANAGER)
			filebox.retry_intake(mine)  # a manager may retry another person's row
		self.assertEqual(enq.call_count, 1)

	def test_removed_file_failure_is_permanent(self):
		gone = self._row(UPLOADER, status="Failed", summary=capture.FILE_REMOVED_SUMMARY)
		rows = {r["name"]: r["can_retry"] for r in self._list(UPLOADER)["rows"]}
		self.assertEqual(rows, {gone: False})
		frappe.set_user(UPLOADER)
		with mock.patch("frappe.enqueue") as enq, self.assertRaises(frappe.ValidationError):
			filebox.retry_intake(gone)
		enq.assert_not_called()

	def test_unreadable_type_skip_is_permanent_but_limit_skips_retry(self):
		perm = self._row(UPLOADER, status="Skipped", summary=capture.UNREADABLE_TYPE_SUMMARY)
		limit = self._row(UPLOADER, status="Skipped", summary="daily limit reached")
		off = self._row(UPLOADER, status="Skipped", summary="rule turned off")
		rows = {r["name"]: r["can_retry"] for r in self._list(UPLOADER)["rows"]}
		self.assertEqual(rows, {perm: False, limit: True, off: True})
		frappe.set_user(UPLOADER)
		with mock.patch("frappe.enqueue") as enq:
			with self.assertRaises(frappe.ValidationError):
				filebox.retry_intake(perm)
			filebox.retry_intake(limit)
		self.assertEqual(enq.call_count, 1)
