"""Intake strict mode: the Approval Board review (raise, answer, approve, reject)."""

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approvals_api
from jarvis.intake import draft, review
from jarvis.tests.test_intake_draft import _field, _make_doctype, _perm, _user

PARENT = "Intake Rev Order"
CHILD = "Intake Rev Order Item"
CUSTOMER = "Intake Rev Customer"
RECORD = "Intake Rev Sheet"
ACTIVITY = "Jarvis Trigger Activity"
TRIGGER = "Jarvis Trigger"
AR = "Jarvis Approval Request"
ROLE = "Intake Rev Maker"
UPLOADER = "intake-rev-up@example.com"
OWNER = "intake-rev-owner@example.com"
STRANGER = "intake-rev-other@example.com"


class TestIntakeReview(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Role", ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert()
		_make_doctype(
			CUSTOMER,
			[_field("code", "Data", unique=1)],
			[_perm("System Manager", read=1, create=1, write=1), _perm(ROLE, read=1)],
			autoname="field:code",
		)
		_make_doctype(
			CHILD,
			[
				_field("item_name", "Data", reqd=1, in_list_view=1),
				_field("qty", "Float", reqd=1, in_list_view=1),
				_field("supplier", "Link", options=CUSTOMER),
			],
			[],
			istable=1,
		)
		_make_doctype(
			PARENT,
			[
				_field("title", "Data", reqd=1),
				_field("customer", "Link", options=CUSTOMER, reqd=1),
				_field("items", "Table", options=CHILD),
			],
			[_perm("System Manager", read=1, create=1, write=1), _perm(ROLE, read=1, create=1, write=1)],
		)
		_make_doctype(
			RECORD,
			[_field("progress", "Data")],
			[_perm("System Manager", read=1, create=1, write=1)],
		)
		_user(UPLOADER, [ROLE, "Jarvis User"])
		_user(OWNER, ["Jarvis User"])
		_user(STRANGER, ["Jarvis User"])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt in (PARENT, CHILD, CUSTOMER, RECORD):
			frappe.db.delete(dt)
			frappe.delete_doc("DocType", dt, force=True, ignore_permissions=True)
		for u in (UPLOADER, OWNER, STRANGER):
			frappe.delete_doc("User", u, force=True, ignore_permissions=True)
		frappe.delete_doc("Role", ROLE, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete(PARENT)
		for code in ("ACME", "BRK-1"):
			if not frappe.db.exists(CUSTOMER, code):
				frappe.get_doc({"doctype": CUSTOMER, "code": code}).insert()
		p = mock.patch("jarvis.permissions.is_valid_unattended_owner", return_value=True)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.set_user, "Administrator")
		self._rules: list[str] = []
		self.addCleanup(self._drop_rules)

	def _drop_rules(self):
		frappe.set_user("Administrator")
		for name in self._rules:
			for ar in frappe.get_all(AR, filters={"source": "Intake"}, pluck="name"):
				frappe.delete_doc(AR, ar, force=True, ignore_permissions=True)
			frappe.db.delete(ACTIVITY, {"trigger": name})
			frappe.delete_doc(TRIGGER, name, force=True, ignore_permissions=True)

	# ---- helpers --------------------------------------------------------------
	def _rule(self, **over):
		doc = {
			"doctype": TRIGGER,
			"trigger_name": f"intake-{frappe.generate_hash(length=8)}",
			"action_type": "Intake",
			"intake_source": "Folder",
			"intake_folder": "Home/Attachments",
			"intake_target_doctype": PARENT,
			"intake_spec": json.dumps(
				{
					"fields": ["title", "customer"],
					"table": {"fieldname": "items", "columns": ["item_name", "qty", "supplier"]},
					"match": ["customer"],
				}
			),
			"enabled": 1,
		}
		doc.update(over)
		rule = frappe.get_doc(doc).insert(ignore_permissions=True)
		frappe.db.set_value(TRIGGER, rule.name, "owner", OWNER)
		self._rules.append(rule.name)
		return rule

	def _file(self):
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": "sheet.png", "content": b"hello", "is_private": 1}
		).insert(ignore_permissions=True)
		frappe.db.set_value("File", doc.name, "owner", UPLOADER)
		self.addCleanup(frappe.delete_doc, "File", doc.name, force=True, ignore_permissions=True)
		return doc

	def _row(self, rule, **over):
		values = {
			"doctype": ACTIVITY,
			"trigger": rule.name,
			"trigger_label": rule.trigger_name,
			"action_type": "Intake",
			"status": "Running",
			"source_file": self._file().name,
			"target_doctype": PARENT,
			"event_user": UPLOADER,
			"summary": "Reading",
		}
		values.update(over)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def _pending(self, reading=None, **rule_over):
		"""A strict-mode row that went to review: (rule, activity row, request name)."""
		rule = self._rule(**rule_over)
		row = self._row(rule)
		reading = reading or {
			"title": "Sheet 1",
			"customer": "ACME",
			"items": [
				{"item_name": "bolt", "qty": 2, "supplier": None},
				{"item_name": "nut", "qty": None, "supplier": None},
			],
		}
		draft.finish(row, reading)
		row.reload()
		self.assertEqual(row.status, "Needs Review")
		return rule, row, row.approval_request

	def _decide(self, user, request, answers, approve=1):
		frappe.set_user(user)
		return approvals_api.decide(request, json.dumps(answers), approve)

	def _request(self, name):
		return frappe.db.get_value(AR, name, ["status", "decision", "decided_by"], as_dict=True)

	# ---- raise ----------------------------------------------------------------
	def test_raise_request_lists_missing(self):
		_rule, row, name = self._pending()
		ar = frappe.get_doc(AR, name)
		self.assertEqual((ar.source, ar.status), ("Intake", "Pending"))
		self.assertEqual(ar.title, "Review sheet.png")
		self.assertIn("Qty, row 2: unreadable", ar.question)
		self.assertEqual((ar.ref_doctype, ar.ref_name, ar.intake_activity), (ACTIVITY, row.name, row.name))
		payload = frappe.parse_json(ar.intake_missing)
		(item,) = payload["missing"]
		self.assertEqual(
			(item["key"], item["label"], item["fieldtype"], item["row"]), ("items.2.qty", "Qty", "Float", 2)
		)
		self.assertEqual(payload["values"]["title"], "Sheet 1")
		self.assertEqual(len(payload["values"]["items"]), 2)
		# The rule owner can see it on their board.
		self.assertTrue(
			frappe.db.exists("DocShare", {"share_doctype": AR, "share_name": name, "user": OWNER})
		)

	def test_request_owner_is_the_rule_owner_whoever_runs_the_job(self):
		rule = self._rule()
		row = self._row(rule)
		frappe.set_user(STRANGER)  # a background job inherits whoever enqueued it
		draft.finish(row, {"title": "Sheet 1", "customer": "ACME", "items": [{"item_name": "bolt"}]})
		frappe.set_user("Administrator")
		row.reload()
		self.assertEqual(frappe.db.get_value(AR, row.approval_request, "owner"), OWNER)

	def test_intake_fields_are_server_managed(self):
		_rule, row, _name = self._pending()
		frappe.set_user(STRANGER)
		doc = frappe.get_doc({"doctype": AR, "title": "x", "question": "q", "intake_activity": row.name})
		with self.assertRaises(frappe.PermissionError):
			doc.insert(ignore_permissions=True)

	# ---- approve --------------------------------------------------------------
	def test_approve_creates_draft_as_uploader_without_ai_call(self):
		rule, row, name = self._pending()
		with (
			mock.patch("jarvis.intake.client.vision_task_complete") as ai,
			mock.patch("jarvis.chat.approvals_api._resume_conversation") as resume,
		):
			res = self._decide(OWNER, name, {"items.2.qty": "7"})
		ai.assert_not_called()
		resume.assert_not_called()
		self.assertEqual(res["status"], "Approved")
		frappe.set_user("Administrator")
		row.reload()
		self.assertEqual(row.status, "Success")
		doc = frappe.get_doc(PARENT, row.draft_name)
		self.assertEqual(doc.owner, UPLOADER)
		self.assertEqual([(r.item_name, r.qty) for r in doc.items], [("bolt", 2), ("nut", 7)])
		self.assertEqual(doc.customer, "ACME")
		state = self._request(name)
		self.assertEqual((state.status, state.decided_by), ("Approved", OWNER))
		self.assertIn(f"Draft ready: {PARENT} {doc.name}", state.decision)

	def test_system_manager_may_approve(self):
		_rule, row, name = self._pending()
		res = self._decide("Administrator", name, {"items.2.qty": 3})
		self.assertEqual(res["status"], "Approved")

	def test_answers_still_missing_rejected(self):
		_rule, row, name = self._pending()
		for answers in ({}, {"items.2.qty": "  "}, {"items.2.qty": "lots"}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._decide(OWNER, name, answers)
			self.assertIn("Qty, row 2", str(ctx.exception))
		frappe.set_user("Administrator")
		self.assertEqual(self._request(name).status, "Pending")
		row.reload()
		self.assertEqual((row.status, row.draft_name), ("Needs Review", None))
		self.assertFalse(frappe.db.count(PARENT))

	def test_unlisted_keys_are_ignored(self):
		_rule, row, name = self._pending()
		self._decide(OWNER, name, {"items.2.qty": 4, "title": "Hijacked", "items.1.qty": 99, "notes": "x"})
		frappe.set_user("Administrator")
		row.reload()
		doc = frappe.get_doc(PARENT, row.draft_name)
		self.assertEqual(doc.title, "Sheet 1")
		self.assertEqual([r.qty for r in doc.items], [2, 4])

	def test_link_answer_needs_an_exact_name(self):
		_rule, row, name = self._pending(
			{"title": "S", "customer": "Nobody", "items": [{"item_name": "a", "qty": 1}]}
		)
		(item,) = frappe.parse_json(frappe.db.get_value(AR, name, "intake_missing"))["missing"]
		self.assertEqual((item["key"], item["fieldtype"], item["options"]), ("customer", "Link", CUSTOMER))
		for wrong in ("acme", "Nobody Else"):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._decide(OWNER, name, {"customer": wrong})
			self.assertIn("Customer (not found)", str(ctx.exception))
		self._decide(OWNER, name, {"customer": "ACME"})
		frappe.set_user("Administrator")
		row.reload()
		self.assertEqual(frappe.db.get_value(PARENT, row.draft_name, "customer"), "ACME")

	def test_cannot_decide_without_being_owner_or_manager(self):
		_rule, row, name = self._pending()
		for approve in (1, 0):
			with self.assertRaises(frappe.PermissionError):
				self._decide(STRANGER, name, {"items.2.qty": 3}, approve)
			with self.assertRaises(frappe.PermissionError):
				self._decide(UPLOADER, name, {"items.2.qty": 3}, approve)
		frappe.set_user("Administrator")
		self.assertEqual(self._request(name).status, "Pending")

	def test_second_approve_never_makes_a_second_draft(self):
		_rule, row, name = self._pending()
		self._decide(OWNER, name, {"items.2.qty": 3})
		with self.assertRaises(frappe.ValidationError):
			self._decide(OWNER, name, {"items.2.qty": 3})
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.count(PARENT), 1)

	def test_existing_draft_closes_the_request_only(self):
		_rule, row, name = self._pending()
		frappe.set_user("Administrator")
		made = frappe.get_doc({"doctype": PARENT, "title": "Earlier", "customer": "ACME"}).insert()
		row.db_set({"draft_doctype": PARENT, "draft_name": made.name})
		with mock.patch("jarvis.intake.draft.create") as create:
			res = self._decide(OWNER, name, {"items.2.qty": 3})
		create.assert_not_called()
		self.assertEqual(res["status"], "Approved")
		self.assertEqual(frappe.db.count(PARENT), 1)

	def test_refused_draft_marks_the_request_answered(self):
		_rule, row, name = self._pending()
		# The uploader loses the right to create between the raise and the answer.
		frappe.set_user("Administrator")
		frappe.db.delete("Has Role", {"parent": UPLOADER, "role": ROLE})
		frappe.clear_cache(user=UPLOADER)
		# The class shares one transaction: give the role back for the next test.
		self.addCleanup(
			lambda: (frappe.get_doc("User", UPLOADER).add_roles(ROLE), frappe.clear_cache(user=UPLOADER))
		)
		res = self._decide(OWNER, name, {"items.2.qty": 3})
		self.assertEqual(res["status"], "Answered")
		frappe.set_user("Administrator")
		row.reload()
		self.assertEqual(row.status, "Failed")
		self.assertIn("Draft not created", self._request(name).decision)
		self.assertFalse(frappe.db.count(PARENT))

	# ---- reject ---------------------------------------------------------------
	def test_reject_marks_failed(self):
		_rule, row, name = self._pending()
		res = self._decide(OWNER, name, {}, approve=0)
		self.assertEqual(res["status"], "Rejected")
		frappe.set_user("Administrator")
		row.reload()
		self.assertEqual((row.status, row.summary), ("Failed", "Rejected by reviewer"))
		self.assertEqual(self._request(name).status, "Rejected")
		self.assertFalse(frappe.db.count(PARENT))

	def test_reject_writes_failed_back_to_the_source_record(self):
		frappe.set_user("Administrator")
		record = frappe.get_doc({"doctype": RECORD, "progress": "Needs Review"}).insert()
		rule = self._rule(
			intake_source="Record", intake_record_doctype=RECORD, intake_status_field="progress"
		)
		row = self._row(rule, target_doctype=RECORD, target_docname=record.name)
		draft.finish(row, {"title": "S", "customer": "ACME", "items": [{"item_name": "a", "qty": None}]})
		row.reload()
		self._decide(OWNER, row.approval_request, {}, approve=0)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(RECORD, record.name, "progress"), "Failed")

	# ---- board reads ----------------------------------------------------------
	def test_get_approval_gives_the_form_what_it_needs(self):
		_rule, row, name = self._pending()
		frappe.set_user(OWNER)
		res = approvals_api.get_approval(name)
		self.assertEqual(res["source"], "Intake")
		self.assertEqual(res["can_act"], 1)
		intake = res["intake"]
		self.assertEqual(intake["missing"][0]["key"], "items.2.qty")
		self.assertIn({"label": "Title", "value": "Sheet 1"}, intake["read"])
		self.assertEqual(intake["table"]["columns"], ["Item Name", "Qty", "Supplier"])
		self.assertEqual(intake["table"]["rows"][0][:2], ["bolt", "2"])
		self.assertTrue(intake["file"]["file_url"])
		self.assertTrue(intake["file"]["is_image"])
		frappe.set_user("Administrator")
		self.assertEqual(approvals_api.get_approval(name)["can_act"], 1)

	def test_stranger_cannot_read_and_owner_sees_it_on_the_board(self):
		_rule, row, name = self._pending()
		frappe.set_user(STRANGER)
		with self.assertRaises(frappe.PermissionError):
			approvals_api.get_approval(name)
		frappe.set_user(OWNER)
		page = approvals_api.list_approvals_page()
		self.assertIn(name, [r["name"] for r in page["rows"]])

	def test_intake_request_cannot_be_ignored(self):
		_rule, row, name = self._pending()
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			approvals_api.dismiss_approval(name)

	def test_decision_must_be_a_json_object(self):
		_rule, row, name = self._pending()
		frappe.set_user(OWNER)
		with self.assertRaises(frappe.ValidationError):
			approvals_api.decide(name, "Approved", 1)
		self.assertEqual(review.may_decide(frappe.get_doc(AR, name)), True)

	def test_apply_decision_is_the_plan_contract(self):
		_rule, row, name = self._pending()
		res = review.apply_decision(frappe.get_doc(AR, name), True, {"items.2.qty": 1})
		self.assertEqual(res["status"], "Approved")
		row.reload()
		self.assertEqual(row.status, "Success")

	# ---- fix round 1 ----------------------------------------------------------
	def _shared(self, row, name):
		return {
			"request": bool(
				frappe.db.exists("DocShare", {"share_doctype": AR, "share_name": name, "user": OWNER})
			),
			"file": bool(
				frappe.db.exists(
					"DocShare", {"share_doctype": "File", "share_name": row.source_file, "user": OWNER}
				)
			),
		}

	def test_shares_live_only_while_pending(self):
		_rule, row, name = self._pending()
		self.assertEqual(self._shared(row, name), {"request": True, "file": True})
		self._decide(OWNER, name, {"items.2.qty": 3})
		frappe.set_user("Administrator")
		# The owner keeps their history of the request; the private file is closed again.
		self.assertEqual(self._shared(row, name), {"request": True, "file": False})

	def test_shares_removed_on_reject(self):
		_rule, row, name = self._pending()
		self.assertEqual(self._shared(row, name), {"request": True, "file": True})
		self._decide(OWNER, name, {}, 0)
		frappe.set_user("Administrator")
		self.assertEqual(self._shared(row, name), {"request": True, "file": False})

	def test_shares_removed_when_the_draft_is_refused(self):
		_rule, row, name = self._pending()
		with mock.patch("jarvis.intake.draft.create", side_effect=draft.DraftFailed("no")):
			self._decide(OWNER, name, {"items.2.qty": 3})
		frappe.set_user("Administrator")
		self.assertEqual(self._shared(row, name), {"request": True, "file": False})

	def test_a_share_that_was_already_there_is_kept(self):
		rule = self._rule()
		row = self._row(rule)
		frappe.share.add("File", row.source_file, OWNER, read=1, flags={"ignore_share_permission": True})
		draft.finish(row, {"title": "S", "customer": "ACME", "items": [{"item_name": "a", "qty": None}]})
		row.reload()
		self._decide(OWNER, row.approval_request, {}, 0)
		frappe.set_user("Administrator")
		self.assertEqual(self._shared(row, row.approval_request), {"request": True, "file": True})

	def test_odd_answers_are_refused_not_a_500(self):
		_rule, row, name = self._pending()
		for answers in ({"items.2.qty": {"a": 1}}, {"items.2.qty": [1]}, {"items.x.qty": 3}, {1: 2}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._decide(OWNER, name, answers)
			self.assertIn("Still needed: Qty, row 2", str(ctx.exception))
		frappe.set_user("Administrator")
		self.assertEqual(self._request(name).status, "Pending")

	def test_existing_draft_also_finishes_the_activity(self):
		_rule, row, name = self._pending()
		frappe.set_user("Administrator")
		made = frappe.get_doc({"doctype": PARENT, "title": "Earlier", "customer": "ACME"}).insert()
		row.db_set({"draft_doctype": PARENT, "draft_name": made.name})
		self._decide(OWNER, name, {"items.2.qty": 3})
		frappe.set_user("Administrator")
		row.reload()
		self.assertEqual(row.status, "Success")
		self.assertEqual(row.summary, f"Draft ready: {PARENT} {made.name}")
