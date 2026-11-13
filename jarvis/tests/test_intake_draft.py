"""Intake draft stage: link matching as the uploader, the required-field gate, the draft."""

import json
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.intake import draft, spec

PARENT = "Intake Test Order"
CHILD = "Intake Test Order Item"
CUSTOMER = "Intake Test Customer"
ACTIVITY = "Jarvis Trigger Activity"
TRIGGER = "Jarvis Trigger"
MAKER_ROLE = "Intake Test Maker"
READER_ROLE = "Intake Test Reader"
MAKER = "intake-maker@example.com"
READER_ONLY = "intake-reader@example.com"
NO_PERM = "intake-noperm@example.com"
RECORD = "Intake Test Sheet"


def _field(fieldname, fieldtype, **kw):
	return {
		"fieldname": fieldname,
		"label": fieldname.replace("_", " ").title(),
		"fieldtype": fieldtype,
		**kw,
	}


def _perm(role, **rights):
	# DocPerm defaults create/write to 1, so say every right explicitly.
	return {"role": role, "permlevel": 0, "read": 0, "write": 0, "create": 0, **rights}


def _make_doctype(name, fields, perms, **kw):
	if frappe.db.exists("DocType", name):
		frappe.delete_doc("DocType", name, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": name,
			"module": "Jarvis",
			"custom": 1,
			"fields": fields,
			"permissions": perms,
			**kw,
		}
	).insert(ignore_permissions=True)


def _user(email, roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"send_welcome_email": 0,
			"roles": [{"role": r} for r in roles],
		}
	).insert(ignore_permissions=True)


class TestIntakeDraft(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for role in (MAKER_ROLE, READER_ROLE):
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert()
		_make_doctype(
			CUSTOMER,
			[_field("code", "Data", unique=1), _field("title", "Data")],
			[_perm("System Manager", read=1, create=1, write=1), _perm(READER_ROLE, read=1)],
			autoname="field:code",
			title_field="title",
		)
		_make_doctype(
			CHILD,
			[
				_field("item_name", "Data", reqd=1, in_list_view=1),
				_field("qty", "Float", reqd=1, in_list_view=1),
				_field("rate", "Currency"),
				_field("supplier", "Link", options=CUSTOMER),
			],
			[],
			istable=1,
		)
		_make_doctype(
			PARENT,
			[
				_field("title", "Data", reqd=1),
				_field("customer", "Link", options=CUSTOMER),
				_field("notes", "Small Text"),
				_field("priority", "Select", options="Low\nHigh"),
				_field("items", "Table", options=CHILD),
			],
			[
				_perm("System Manager", read=1, create=1, write=1),
				_perm(MAKER_ROLE, read=1, create=1, write=1),
				_perm(READER_ROLE, read=1),
			],
		)
		_make_doctype(
			RECORD,
			[_field("progress", "Data"), _field("draft_link", "Data"), _field("other", "Data")],
			[_perm("System Manager", read=1, create=1, write=1)],
		)
		_user(MAKER, [MAKER_ROLE, READER_ROLE])
		_user(READER_ONLY, [READER_ROLE])
		_user(NO_PERM, [])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for dt in (PARENT, CHILD, CUSTOMER, RECORD):
			frappe.db.delete(dt)
			frappe.delete_doc("DocType", dt, force=True, ignore_permissions=True)
		for u in (MAKER, READER_ONLY, NO_PERM):
			frappe.delete_doc("User", u, force=True, ignore_permissions=True)
		for role in (MAKER_ROLE, READER_ROLE):
			frappe.delete_doc("Role", role, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		for code, title in (("BRK-2290", "Brake pad"), ("BRK-2291", "Brake disc"), ("ACME", "Acme Traders")):
			if not frappe.db.exists(CUSTOMER, code):
				frappe.get_doc({"doctype": CUSTOMER, "code": code, "title": title}).insert()
		p = mock.patch("jarvis.permissions.is_valid_unattended_owner", return_value=True)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.set_user, "Administrator")
		# The class shares one transaction, so earlier tests' drafts would skew the counts.
		frappe.db.delete(PARENT)
		self._rules: list[str] = []
		self.addCleanup(self._drop_rules)

	def _drop_rules(self):
		for name in self._rules:
			frappe.db.delete(ACTIVITY, {"trigger": name})
			frappe.delete_doc(TRIGGER, name, force=True, ignore_permissions=True)

	# ---- helpers --------------------------------------------------------------
	def _spec(self, **over):
		data = {
			"fields": ["title", "customer", "notes", "priority"],
			"table": {"fieldname": "items", "columns": ["item_name", "qty", "rate", "supplier"]},
			"match": ["customer"],
		}
		data.update(over)
		return spec.IntakeSpec(
			target=PARENT,
			fields=data["fields"],
			table=data["table"],
			match=data["match"],
			defaults=data.get("defaults", {}),
		)

	def _rule(self, spec_data=None, **over):
		doc = {
			"doctype": TRIGGER,
			"trigger_name": f"intake-{frappe.generate_hash(length=8)}",
			"action_type": "Intake",
			"intake_source": "Folder",
			"intake_folder": "Home/Attachments",
			"intake_target_doctype": PARENT,
			"intake_spec": json.dumps(
				spec_data
				or {
					"fields": ["title", "customer", "notes"],
					"table": {"fieldname": "items", "columns": ["item_name", "qty", "rate"]},
					"match": ["customer"],
				}
			),
			"enabled": 1,
		}
		doc.update(over)
		rule = frappe.get_doc(doc).insert(ignore_permissions=True)
		self._rules.append(rule.name)
		return rule

	def _file(self):
		doc = frappe.get_doc(
			{"doctype": "File", "file_name": "sheet.txt", "content": b"hello", "is_private": 1}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "File", doc.name, force=True, ignore_permissions=True)
		return doc

	def _row(self, rule, uploader=MAKER, **over):
		values = {
			"doctype": ACTIVITY,
			"trigger": rule.name,
			"trigger_label": rule.trigger_name,
			"action_type": "Intake",
			"status": "Running",
			"source_file": self._file().name,
			"target_doctype": PARENT,
			"event_user": uploader,
			"summary": "Creating the draft",
		}
		values.update(over)
		# The uploader owns the file they uploaded, which is what lets them attach it.
		frappe.db.set_value("File", values["source_file"], "owner", uploader)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def _state(self, row):
		return frappe.db.get_value(
			ACTIVITY,
			row.name,
			["status", "summary", "draft_doctype", "draft_name", "approval_request", "detail"],
			as_dict=True,
		)

	def _resolve(self, user, values, **spec_over):
		frappe.set_user(user)
		return draft.resolve_links(self._spec(**spec_over), values)

	# ---- matching -------------------------------------------------------------
	def test_exact_and_unique_match(self):
		values, unresolved = self._resolve(MAKER, {"customer": "ACME"})
		self.assertEqual((values["customer"], unresolved), ("ACME", []))
		# Case-insensitive on name, and on the title field.
		values, unresolved = self._resolve(MAKER, {"customer": "acme"})
		self.assertEqual((values["customer"], unresolved), ("ACME", []))
		values, unresolved = self._resolve(MAKER, {"customer": "acme TRADERS"})
		self.assertEqual((values["customer"], unresolved), ("ACME", []))
		# A name with an underscore wildcard must not match a different name.
		values, unresolved = self._resolve(MAKER, {"customer": "AC_E"})
		self.assertEqual(unresolved[0]["field"], "customer")

	def test_ambiguous_match_unresolved(self):
		frappe.get_doc({"doctype": CUSTOMER, "code": "BRK-9", "title": "Dup"}).insert()
		frappe.get_doc({"doctype": CUSTOMER, "code": "DUP-2", "title": "dup"}).insert()
		values, unresolved = self._resolve(MAKER, {"customer": "dup"})
		self.assertIsNone(values["customer"])
		self.assertEqual(unresolved[0]["field"], "customer")
		self.assertIsNone(unresolved[0]["row"])
		self.assertEqual(unresolved[0]["value"], "dup")
		# Near misses are offered as candidates, at most three.
		values, unresolved = self._resolve(MAKER, {"customer": "BRK-2299"})
		self.assertIsNone(values["customer"])
		self.assertIn("BRK-2290", unresolved[0]["candidates"])
		self.assertLessEqual(len(unresolved[0]["candidates"]), 3)

	def test_match_uses_uploader_permissions(self):
		# The uploader may not read the customers: nothing resolves, nothing leaks.
		frappe.set_user(NO_PERM)
		values, unresolved = draft.resolve_links(self._spec(), {"customer": "ACME"})
		self.assertIsNone(values["customer"])
		self.assertEqual(unresolved[0]["candidates"], [])

	def test_link_outside_match_needs_exact_name(self):
		rows = [
			{"item_name": "a", "qty": 1, "supplier": "ACME"},
			{"item_name": "b", "qty": 1, "supplier": "acme"},
		]
		values, unresolved = self._resolve(MAKER, {"items": rows})
		self.assertEqual(values["items"][0]["supplier"], "ACME")
		self.assertIsNone(values["items"][1]["supplier"])
		self.assertEqual((unresolved[0]["field"], unresolved[0]["row"]), ("supplier", 2))

	# ---- finish ---------------------------------------------------------------
	def test_strict_missing_raises_review_no_draft(self):
		rule = self._rule()
		row = self._row(rule)
		reading = {
			"title": "Sheet 1",
			"customer": "Nobody Ltd",
			"notes": None,
			"items": [{"item_name": "bolt", "qty": None, "rate": 3}],
			"_note": "Read 1 of 2 pages",
		}
		stored = json.loads(json.dumps(reading))
		with mock.patch("jarvis.intake.review.raise_request", return_value=self._approval()) as rr:
			draft.finish(row, reading)
		self.assertEqual(reading, stored, "finish must not mutate the stored reading")
		activity, values, missing = rr.call_args.args
		self.assertEqual(activity.name, row.name)
		self.assertEqual(values["title"], "Sheet 1")
		self.assertIn(("qty", 1), {(m["field"], m["row"]) for m in missing})
		state = self._state(row)
		self.assertEqual(state.status, "Needs Review")
		self.assertTrue(state.approval_request)
		self.assertIn("Waiting for a reviewer: ", state.summary)
		self.assertIn("field(s) need a value", state.summary)
		self.assertIn("Read 1 of 2 pages", state.summary)
		self.assertFalse(state.draft_name)
		self.assertFalse(frappe.db.count(PARENT))

	def _approval(self):
		return (
			frappe.get_doc({"doctype": "Jarvis Approval Request", "title": "t", "question": "q"})
			.insert(ignore_permissions=True)
			.name
		)

	def test_incomplete_creates_draft_with_comment(self):
		rule = self._rule(allow_incomplete_drafts=1)
		row = self._row(rule)
		reading = {
			"title": None,
			"customer": "BRK-2299",
			"notes": None,
			"items": [
				{"item_name": "bolt", "qty": None, "rate": 3},
				{"item_name": "nut", "qty": 2, "rate": None},
			],
		}
		with mock.patch("jarvis.intake.review.raise_request") as rr:
			draft.finish(row, reading)
		rr.assert_not_called()
		state = self._state(row)
		self.assertEqual(state.status, "Needs Review")
		self.assertIsNone(state.approval_request)
		self.assertEqual(state.draft_doctype, PARENT)
		self.assertIn(f"Draft ready: {PARENT} {state.draft_name}", state.summary)
		self.assertIn("field(s) to check)", state.summary)
		comment = frappe.db.get_value(
			"Comment",
			{"reference_doctype": PARENT, "reference_name": state.draft_name, "comment_type": "Comment"},
			"content",
		)
		self.assertIn("Qty, row 1: unreadable", comment)
		self.assertIn("Title: unreadable", comment)
		self.assertIn("BRK-2299: not found, closest", comment)
		self.assertEqual(
			frappe.db.count(
				"Comment",
				{"reference_doctype": PARENT, "reference_name": state.draft_name, "comment_type": "Comment"},
			),
			1,
		)

	def test_complete_reading_is_success(self):
		rule = self._rule()
		row = self._row(rule)
		reading = {
			"title": "Sheet <b>1</b>",
			"customer": "acme",
			"notes": "",
			"items": [{"item_name": "bolt", "qty": 4, "rate": 3}],
		}
		draft.finish(row, reading)
		state = self._state(row)
		self.assertEqual(state.status, "Success")
		doc = frappe.get_doc(PARENT, state.draft_name)
		self.assertEqual((doc.customer, len(doc.items), doc.items[0].qty), ("ACME", 1, 4))
		self.assertTrue(
			frappe.db.exists("Notification Log", {"for_user": MAKER, "subject": ["like", "Draft ready:%"]})
		)

	def test_draft_owned_by_uploader_and_source_attached(self):
		rule = self._rule()
		row = self._row(rule)
		draft.finish(
			row,
			{
				"title": "T",
				"customer": None,
				"notes": None,
				"items": [{"item_name": "x", "qty": 1, "rate": 1}],
			},
		)
		state = self._state(row)
		self.assertEqual(frappe.db.get_value(PARENT, state.draft_name, "owner"), MAKER)
		src_url = frappe.db.get_value("File", row.source_file, "file_url")
		self.assertTrue(
			frappe.db.exists(
				"File",
				{"attached_to_doctype": PARENT, "attached_to_name": state.draft_name, "file_url": src_url},
			)
		)
		self.assertEqual(frappe.session.user, "Administrator", "session restored")

	def test_writeback_only_configured_fields(self):
		sheet = frappe.get_doc({"doctype": RECORD, "progress": "new", "other": "keep"}).insert()
		rule = self._rule(
			intake_source="Record",
			intake_folder=None,
			intake_record_doctype=RECORD,
			intake_status_field="progress",
			intake_link_field="draft_link",
		)
		row = self._row(rule, target_docname=sheet.name)
		before = frappe.db.get_value(RECORD, sheet.name, "modified")
		draft.finish(
			row,
			{
				"title": "T",
				"customer": None,
				"notes": None,
				"items": [{"item_name": "x", "qty": 1, "rate": 1}],
			},
		)
		state = self._state(row)
		after = frappe.db.get_value(
			RECORD, sheet.name, ["progress", "draft_link", "other", "modified"], as_dict=True
		)
		self.assertEqual(
			(after.progress, after.draft_link, after.other), ("Success", state.draft_name, "keep")
		)
		self.assertEqual(after.modified, before)

	def test_writeback_skips_unset_and_unknown_fields(self):
		sheet = frappe.get_doc({"doctype": RECORD, "progress": "new"}).insert()
		rule = self._rule(
			intake_source="Record",
			intake_folder=None,
			intake_record_doctype=RECORD,
			intake_status_field="progress",
			intake_link_field=None,
		)
		row = self._row(rule, target_docname=sheet.name)
		draft.finish(
			row,
			{
				"title": "T",
				"customer": None,
				"notes": None,
				"items": [{"item_name": "x", "qty": 1, "rate": 1}],
			},
		)
		self.assertEqual(
			frappe.db.get_value(RECORD, sheet.name, ["progress", "draft_link"], as_dict=True),
			{"progress": "Success", "draft_link": None},
		)

	def test_no_create_permission_fails(self):
		rule = self._rule()
		row = self._row(rule, uploader=READER_ONLY)
		draft.finish(
			row,
			{
				"title": "T",
				"customer": None,
				"notes": None,
				"items": [{"item_name": "x", "qty": 1, "rate": 1}],
			},
		)
		state = self._state(row)
		self.assertEqual(state.status, "Failed")
		self.assertEqual(state.summary, f"You can't create {PARENT}")
		self.assertFalse(frappe.db.count(PARENT))

	def _insert_raises(self, error):
		real_insert = frappe.model.document.Document.insert

		def insert(doc, *args, **kwargs):
			if doc.doctype == PARENT:
				raise error
			return real_insert(doc, *args, **kwargs)

		return mock.patch("frappe.model.document.Document.insert", insert)

	def test_validation_error_is_failed_and_retryable(self):
		row = self._row(self._rule())
		with self._insert_raises(frappe.ValidationError("<b>Total</b> must be <i>positive</i>")):
			draft.finish(row, self._simple_reading())
		state = self._state(row)
		self.assertEqual(state.status, "Failed")
		# The target's own validation text is user-facing by design; its HTML is stripped.
		self.assertEqual(state.summary, "The draft could not be saved: Total must be positive")
		self.assertFalse(frappe.db.count(PARENT))

	def test_unexpected_error_text_stays_off_the_uploader_summary(self):
		row = self._row(self._rule())
		boom = RuntimeError("/home/secret/app.py: SELECT password FROM tabUser")
		with self._insert_raises(boom):
			draft.finish(row, self._simple_reading())
		state = self._state(row)
		self.assertEqual((state.status, state.summary), ("Failed", "The draft could not be saved."))
		self.assertIn("SELECT password", state.detail)  # managers only

	def test_invalid_uploader_fails(self):
		rule = self._rule()
		row = self._row(rule)
		with mock.patch("jarvis.permissions.is_valid_unattended_owner", return_value=False):
			draft.finish(row, {"title": "T"})
		state = self._state(row)
		self.assertEqual(state.status, "Failed")
		self.assertIn("can't be used for unattended work", state.summary)

	def test_defaults_fill_only_null_reads(self):
		rule = self._rule(
			spec_data={
				"fields": ["title", "priority"],
				"table": None,
				"match": [],
				"defaults": {"priority": "Low", "title": "Fallback"},
			}
		)
		row = self._row(rule)
		draft.finish(row, {"title": None, "priority": "High"})
		state = self._state(row)
		doc = frappe.get_doc(PARENT, state.draft_name)
		self.assertEqual((doc.title, doc.priority, state.status), ("Fallback", "High", "Success"))

	# ---- fix round 1 ----------------------------------------------------------
	def _simple_reading(self, **over):
		reading = {
			"title": "T",
			"customer": None,
			"notes": None,
			"items": [{"item_name": "x", "qty": 1, "rate": 1}],
		}
		reading.update(over)
		return reading

	def test_retry_after_post_insert_failure_makes_no_second_draft(self):
		row = self._row(self._rule())
		with mock.patch("jarvis.intake.draft._ignored_blanks", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				draft.finish(row, self._simple_reading())
		self.assertEqual(frappe.db.count(PARENT), 1)
		self.assertTrue(frappe.db.get_value(ACTIVITY, row.name, "draft_name"))
		draft.finish(row, self._simple_reading())
		self.assertEqual(frappe.db.count(PARENT), 1)
		state = self._state(row)
		self.assertEqual(state.status, "Success")
		self.assertIn(state.draft_name, state.summary)

	def test_failing_comment_or_attach_still_links_the_draft(self):
		row = self._row(self._rule(allow_incomplete_drafts=1))
		with (
			mock.patch("frappe.model.document.Document.add_comment", side_effect=RuntimeError("c")),
			mock.patch("jarvis.intake.draft.attach_file_to_doc", side_effect=RuntimeError("a")),
			mock.patch("jarvis.intake.draft._write_back", side_effect=RuntimeError("w")),
		):
			draft.finish(row, self._simple_reading(title=None))
		state = self._state(row)
		self.assertEqual(state.status, "Needs Review")
		self.assertTrue(state.draft_name)

	def test_unparseable_number_is_missing_not_zero(self):
		reading = self._simple_reading(items=[{"item_name": "x", "qty": "abc", "rate": 1}])
		rule = self._rule()
		row = self._row(rule)
		with mock.patch("jarvis.intake.review.raise_request", return_value=self._approval()) as rr:
			draft.finish(row, reading)
		missing = rr.call_args.args[2]
		self.assertEqual(
			[(m["field"], m["row"], m["reason"]) for m in missing],
			[("qty", 1, "Qty, row 1: not a number")],
		)
		self.assertFalse(frappe.db.count(PARENT))
		frappe.db.set_value(TRIGGER, rule.name, "allow_incomplete_drafts", 1)
		row2 = self._row(rule)
		draft.finish(row2, reading)
		state = self._state(row2)
		self.assertEqual(state.status, "Needs Review")
		item = frappe.get_doc(PARENT, state.draft_name).items[0]
		self.assertFalse(item.qty)
		comment = frappe.db.get_value(
			"Comment", {"reference_name": state.draft_name, "comment_type": "Comment"}, "content"
		)
		self.assertIn("Qty, row 1: not a number", comment)

	def test_number_and_date_normalising(self):
		values, unresolved = self._resolve(
			MAKER, {"items": [{"item_name": "a", "qty": "1,200.50", "rate": "\u20b91,000"}]}, fields=["title"]
		)
		self.assertEqual((values["items"][0]["qty"], values["items"][0]["rate"]), (1200.5, 1000))
		self.assertEqual(unresolved, [])

	def test_non_scalar_value_is_blank(self):
		values, unresolved = self._resolve(MAKER, {"title": ["a", "b"], "notes": {"x": 1}})
		self.assertEqual((values["title"], values["notes"]), (None, None))
		self.assertEqual({u["field"] for u in unresolved}, {"title", "notes"})

	def test_duplicate_entry_ends_failed_cleanly(self):
		row = self._row(self._rule())
		real_insert = frappe.model.document.Document.insert

		def insert(doc, *args, **kwargs):
			if doc.doctype == PARENT:
				raise frappe.DuplicateEntryError(PARENT, "x", None)
			return real_insert(doc, *args, **kwargs)

		with mock.patch("frappe.model.document.Document.insert", insert):
			draft.finish(row, self._simple_reading())
		state = self._state(row)
		self.assertEqual(state.status, "Failed")
		self.assertEqual(state.summary, "The draft could not be saved.")
		self.assertFalse(frappe.db.count(PARENT))
		self.assertEqual(frappe.session.user, "Administrator")

	def test_repeated_unresolved_value_is_looked_up_once(self):
		rows = [{"item_name": str(i), "qty": 1, "supplier": "NOPE-1"} for i in range(30)]
		with mock.patch("jarvis.intake.draft._readable_names", wraps=draft._readable_names) as spy:
			values, unresolved = self._resolve(MAKER, {"items": rows})
		self.assertEqual(len(unresolved), 30)
		self.assertEqual(spy.call_count, 2)  # exact name, then close names

	def test_failed_row_writes_status_back_but_not_the_link(self):
		sheet = frappe.get_doc({"doctype": RECORD, "progress": "new", "draft_link": "keep"}).insert()
		rule = self._rule(
			intake_source="Record",
			intake_folder=None,
			intake_record_doctype=RECORD,
			intake_status_field="progress",
			intake_link_field="draft_link",
		)
		row = self._row(rule, uploader=READER_ONLY, target_docname=sheet.name)
		draft.finish(row, self._simple_reading())
		self.assertEqual(self._state(row).status, "Failed")
		self.assertEqual(
			frappe.db.get_value(RECORD, sheet.name, ["progress", "draft_link"], as_dict=True),
			{"progress": "Failed", "draft_link": "keep"},
		)
		self.assertEqual(frappe.session.user, "Administrator", "session restored after DraftFailed")

	def test_detail_is_the_short_reason(self):
		row = self._row(self._rule())
		with self._insert_raises(frappe.ValidationError("x" * 300)):
			draft.finish(row, self._simple_reading())
		detail = self._state(row).detail
		self.assertLessEqual(len(detail), 140)
		self.assertNotIn("Traceback", detail)

	def test_clean_dates(self):
		date = frappe._dict(fieldtype="Date")
		self.assertEqual(draft._clean(date, "2026-11-03"), ("2026-11-03", None))
		self.assertEqual(draft._clean(date, "not a date"), (None, "not a date"))
		self.assertEqual(draft._clean(frappe._dict(fieldtype="Datetime"), "31/31/2026"), (None, "not a date"))
		self.assertEqual(draft._clean(frappe._dict(fieldtype="Int"), "2.5"), (None, "not a number"))

	def test_clean_select_and_check(self):
		select = frappe._dict(fieldtype="Select", options="\nOpen\nClosed")
		self.assertEqual(draft._clean(select, "Open"), ("Open", None))
		self.assertEqual(draft._clean(select, "Pending"), (None, "not an option"))
		self.assertEqual(draft._clean(select, ""), (None, None))
		check = frappe._dict(fieldtype="Check")
		for given, want in ((1, 1), (0, 0), (True, 1), (False, 0), ("1", 1), ("true", 1), ("False", 0)):
			self.assertEqual(draft._clean(check, given), (want, None), given)
		for bad in ("maybe", 2, "yes", 0.5):
			self.assertEqual(draft._clean(check, bad), (None, "not a yes/no value"), bad)

	def test_off_list_select_value_no_longer_fails_the_insert(self):
		row = self._row(self._rule(spec_data={"fields": ["title", "priority"], "table": None, "match": []}))
		draft.finish(row, {"title": "T", "priority": "Whenever"})
		got = self._state(row)
		self.assertEqual(got.status, "Success")
		self.assertIn("field(s) to check", got.summary)
		self.assertNotEqual(frappe.db.get_value(got.draft_doctype, got.draft_name, "priority"), "Whenever")

	def _site(self, number_format="#,###.##", date_format="yyyy-mm-dd"):
		"""Pretend the site has these System Settings (no global state is written)."""
		settings = {"number_format": number_format, "date_format": date_format}
		return mock.patch("frappe.get_system_settings", side_effect=settings.get)

	def _num(self, text, number_format="#,###.##", fieldtype="Float"):
		with self._site(number_format):
			return draft._clean(frappe._dict(fieldtype=fieldtype), text)

	def test_numbers_follow_the_site_format(self):
		bad = (None, "not a number")
		self.assertEqual(self._num("1,200.50"), (1200.5, None))
		self.assertEqual(self._num("1.200,50"), bad)
		self.assertEqual(self._num("1,5"), bad)
		self.assertEqual(self._num("1,20"), bad)
		self.assertEqual(self._num("1.200"), (1.2, None))  # a valid dot-decimal number
		self.assertEqual(self._num("1200.5"), (1200.5, None))
		self.assertEqual(self._num("-\u20b91,200"), (-1200, None))
		self.assertEqual(self._num("Rs. 15"), (15, None))
		self.assertEqual(self._num("1,2000"), bad)
		self.assertEqual(self._num("12abc"), bad)
		self.assertEqual(self._num(1200.5), (1200.5, None))
		self.assertEqual(self._num("1.200,50", "#.###,##"), (1200.5, None))
		self.assertEqual(self._num("1,5", "#.###,##"), (1.5, None))
		self.assertEqual(self._num("1,200.50", "#.###,##"), bad)
		self.assertEqual(self._num("1,20,000.50", "#,##,###.##"), (120000.5, None))
		self.assertEqual(self._num("120,000.50", "#,##,###.##"), bad)
		self.assertEqual(self._num("1 200,5", "# ###,##"), (1200.5, None))

	def test_whole_number_formats_take_integers_only(self):
		bad = (None, "not a number")
		self.assertEqual(self._num("1.2345", "#.###"), bad)
		self.assertEqual(self._num("1,2345", "#,###"), bad)
		self.assertEqual(self._num("1.234", "#.###"), (1234, None))
		self.assertEqual(self._num("1,234", "#,###"), (1234, None))
		self.assertEqual(self._num("1234", "#,###"), (1234, None))

	def test_dates_follow_iso_or_the_site_format(self):
		date = frappe._dict(fieldtype="Date")
		with self._site(date_format="dd-mm-yyyy"):
			self.assertEqual(draft._clean(date, "2026-11-03"), ("2026-11-03", None))
			self.assertEqual(draft._clean(date, "03-11-2026"), ("2026-11-03", None))
			self.assertEqual(draft._clean(date, "03/11/2026"), (None, "not a date"))
			self.assertEqual(draft._clean(date, "11-03-26"), (None, "not a date"))
			dt = frappe._dict(fieldtype="Datetime")
			self.assertEqual(draft._clean(dt, "2026-11-03 10:30:00"), ("2026-11-03 10:30:00", None))
			self.assertEqual(draft._clean(dt, "03-11-2026 10:30"), ("2026-11-03 10:30:00", None))
		with self._site(date_format="mm/dd/yyyy"):
			self.assertEqual(draft._clean(date, "03/11/2026"), ("2026-03-11", None))

	def test_prompt_asks_for_plain_numbers_and_iso_dates(self):
		from jarvis.intake import reader

		with self._site(date_format="dd-mm-yyyy"):
			prompt = reader.build_prompt(self._spec())
		self.assertIn("plain JSON numbers", prompt)
		self.assertIn("YYYY-MM-DD", prompt)
		self.assertIn("dd-mm-yyyy", prompt)

	def test_resumed_draft_does_not_attach_twice(self):
		row = self._row(self._rule())
		draft.finish(row, self._simple_reading())
		with mock.patch("jarvis.intake.draft.attach_file_to_doc") as attach:
			draft.finish(row, self._simple_reading())
		attach.assert_not_called()

	def test_failed_write_back_does_not_hide_the_failure(self):
		row = self._row(self._rule(), uploader=READER_ONLY)
		with mock.patch("jarvis.intake.draft._write_back", side_effect=RuntimeError("w")):
			draft.finish(row, self._simple_reading())
		self.assertEqual(self._state(row).status, "Failed")
