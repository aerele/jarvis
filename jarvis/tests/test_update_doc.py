"""Tests for jarvis.tools.update_doc - the first mutating tool.

Uses ``Note`` as the fixture DocType because it exists on every Frappe
site, has simple writeable fields, and modifying test rows can't break
anything else.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, InvalidFieldValueError, PermissionDeniedError
from jarvis.tools.update_doc import update_doc

NOTE_DT = "Note"


def _make_note(title: str = "jarvis-test-note", content: str = "before") -> str:
	"""Create a Note row and return its name. Caller is responsible for cleanup."""
	doc = frappe.get_doc(
		{
			"doctype": NOTE_DT,
			"title": title,
			"content": content,
			"public": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _cleanup_note(name: str) -> None:
	if frappe.db.exists(NOTE_DT, name):
		frappe.delete_doc(NOTE_DT, name, ignore_permissions=True, force=True)
		frappe.db.commit()


class TestUpdateDocValidation(FrappeTestCase):
	"""Argument-shape rejections fire before any DB access."""

	def test_rejects_empty_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			update_doc(doctype="", name="x", changes={"content": "y"})

	def test_rejects_empty_name(self):
		with self.assertRaises(InvalidArgumentError):
			update_doc(doctype=NOTE_DT, name="", changes={"content": "y"})

	def test_rejects_empty_changes_dict(self):
		with self.assertRaises(InvalidArgumentError):
			update_doc(doctype=NOTE_DT, name="anything", changes={})

	def test_rejects_non_dict_changes(self):
		with self.assertRaises(InvalidArgumentError):
			update_doc(doctype=NOTE_DT, name="anything", changes="not a dict")

	def test_rejects_protected_system_fields(self):
		"""Writing `name`, `owner`, `creation`, etc. is refused outright -
		Frappe maintains these and an LLM shouldn't be touching them."""
		for protected in ["name", "owner", "creation", "modified", "doctype", "docstatus", "idx"]:
			with self.assertRaises(InvalidArgumentError, msg=f"should reject {protected}"):
				update_doc(
					doctype=NOTE_DT,
					name="anything",
					changes={protected: "tampered", "content": "y"},
				)


class TestUpdateDocPermissions(FrappeTestCase):
	"""Calling user must hold WRITE on the target record."""

	def setUp(self):
		self.note = _make_note()

	def tearDown(self):
		_cleanup_note(self.note)

	def test_rejects_when_user_lacks_write_perm(self):
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				update_doc(
					doctype=NOTE_DT,
					name=self.note,
					changes={"content": "new"},
				)

	def test_denied_write_leaves_row_unchanged(self):
		"""No partial write: a denied update must not have committed a DB change.
		Guards that the has_permission check runs BEFORE doc.save() (the exact
		before-doc.set() ordering is asserted in test_checks_permission_on_loaded_doc)."""
		before = frappe.db.get_value(NOTE_DT, self.note, "content")
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				update_doc(doctype=NOTE_DT, name=self.note, changes={"content": "tampered"})
		after = frappe.db.get_value(NOTE_DT, self.note, "content")
		self.assertEqual(after, before)

	def test_checks_permission_on_loaded_doc(self):
		"""Permission is checked record-level on the LOADED Document object (not a
		bare name string): passing the object skips has_permission's duplicate
		lazy parent-row load while yielding the identical verdict. Also asserts the
		check runs on the UNMUTATED doc (before the doc.set() loop) - the field the
		verdict could key on still holds its pristine DB value at check time."""
		called_with = {}

		def fake_perm(doctype, ptype=None, doc=None, **_):
			called_with["doctype"] = doctype
			called_with["ptype"] = ptype
			called_with["doc"] = doc
			# Capture the field value AT CHECK TIME to prove the doc is unmutated.
			called_with["content_at_check"] = doc.get("content") if hasattr(doc, "get") else None
			return True

		with patch("frappe.has_permission", side_effect=fake_perm):
			update_doc(
				doctype=NOTE_DT,
				name=self.note,
				changes={"content": "new"},
			)

		self.assertEqual(called_with["doctype"], NOTE_DT)
		self.assertEqual(called_with["ptype"], "write")
		# doc is the loaded Document, not the name string.
		passed = called_with["doc"]
		self.assertNotIsInstance(passed, str)
		self.assertEqual(getattr(passed, "doctype", None), NOTE_DT)
		self.assertEqual(getattr(passed, "name", None), self.note)
		# The check ran BEFORE doc.set("content", "new"): the doc still carries the
		# original content. A reorder that checked after the mutation would see "new".
		self.assertEqual(called_with["content_at_check"], "before")

	def test_enforce_update_runs_before_permission_check(self):
		"""The delegate writes[] contract (enforce_update) must be evaluated
		BEFORE the Frappe permission check - a delegate is bounced by its own
		contract first."""
		order = []
		with patch(
			"jarvis.tools.update_doc.enforce_update",
			side_effect=lambda *a, **k: order.append("enforce_update"),
		):
			with patch(
				"frappe.has_permission",
				side_effect=lambda *a, **k: order.append("has_permission") or True,
			):
				update_doc(doctype=NOTE_DT, name=self.note, changes={"content": "new"})
		self.assertEqual(order, ["enforce_update", "has_permission"])


class TestUpdateDocHappyPath(FrappeTestCase):
	"""End-to-end: real Note, real DB write, real perm check (Administrator
	has write on Note)."""

	def setUp(self):
		self.note = _make_note(content="before update")

	def tearDown(self):
		_cleanup_note(self.note)

	def test_writes_change_to_db(self):
		result = update_doc(
			doctype=NOTE_DT,
			name=self.note,
			changes={"content": "after update"},
		)
		# Returned dict reflects the new value
		self.assertEqual(result["content"], "after update")
		# DB row reflects the new value
		fresh = frappe.db.get_value(NOTE_DT, self.note, "content")
		self.assertEqual(fresh, "after update")

	def test_multi_field_update(self):
		update_doc(
			doctype=NOTE_DT,
			name=self.note,
			changes={"content": "updated content", "title": "updated title"},
		)
		fresh = frappe.db.get_value(NOTE_DT, self.note, ["content", "title"], as_dict=True)
		self.assertEqual(fresh["content"], "updated content")
		self.assertEqual(fresh["title"], "updated title")

	def test_returns_full_saved_doc(self):
		"""Caller (the agent) needs the post-save state so it can confirm
		what happened - include all fields, not just the changed ones."""
		result = update_doc(
			doctype=NOTE_DT,
			name=self.note,
			changes={"content": "x"},
		)
		self.assertEqual(result["doctype"], NOTE_DT)
		self.assertEqual(result["name"], self.note)
		# Whatever else Note has (title, public) should be in there too
		self.assertIn("title", result)


class TestUpdateDocMissing(FrappeTestCase):
	"""Unknown record propagates Frappe's DoesNotExistError unchanged."""

	def test_unknown_name_raises(self):
		with self.assertRaises(frappe.DoesNotExistError):
			update_doc(
				doctype=NOTE_DT,
				name="JV-NONEXISTENT-99999",
				changes={"content": "x"},
			)


BAD_SCRIPT = 'x = "PO {}".format(doc.name)\n'
CLEAN_SCRIPT = 'x = f"PO {doc.name}"\n'


class TestUpdateDocSandboxCode(FrappeTestCase):
	"""jarvis-admin-v2#818 (T4): an agent update of sandbox code is checked, but only
	for what the update changes (an old script stays editable)."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.tag = frappe.generate_hash(length=8)
		self.addCleanup(frappe.db.rollback)

	def _report(self, suffix: str = "", script: str = CLEAN_SCRIPT) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": f"zz818 upd {self.tag}{suffix}",
				"ref_doctype": "ToDo",
				"report_type": "Script Report",
				"is_standard": "No",
				"report_script": script,
			}
		).insert()
		return doc.name

	def _server_script(self, *, disabled: int, stored: str = BAD_SCRIPT) -> str:
		"""A Server Script whose stored code is ``stored``: inserted clean, then the
		column is written directly (as an old script from Desk would be)."""
		doc = frappe.get_doc(
			{
				"doctype": "Server Script",
				"__newname": f"zz818 ss {self.tag}",
				"script_type": "DocType Event",
				"reference_doctype": "ToDo",
				"doctype_event": "Before Save",
				"script": CLEAN_SCRIPT,
				"disabled": disabled,
			}
		).insert()
		frappe.db.set_value("Server Script", doc.name, "script", stored)
		return doc.name

	def test_report_script_with_format_is_refused_and_unchanged(self):
		name = self._report()
		with self.assertRaises(InvalidFieldValueError) as ctx:
			update_doc(doctype="Report", name=name, changes={"report_script": BAD_SCRIPT})
		self.assertIn(f"Report {name} report_script:", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Report", name, "report_script"), CLEAN_SCRIPT)

	def test_clean_report_script_update_is_saved(self):
		name = self._report()
		update_doc(doctype="Report", name=name, changes={"report_script": 'y = f"{1}"\n'})
		self.assertEqual(frappe.db.get_value("Report", name, "report_script"), 'y = f"{1}"\n')

	def test_disabling_with_the_unchanged_stored_bad_script_succeeds(self):
		name = self._server_script(disabled=0)
		update_doc(doctype="Server Script", name=name, changes={"disabled": 1, "script": BAD_SCRIPT})
		self.assertEqual(frappe.db.get_value("Server Script", name, "disabled"), 1)

	def test_reenabling_a_stored_bad_script_is_refused(self):
		name = self._server_script(disabled=1)
		with self.assertRaises(InvalidFieldValueError) as ctx:
			update_doc(doctype="Server Script", name=name, changes={"disabled": 0})
		text = str(ctx.exception)
		self.assertIn(f"Server Script {name} script (stored code, which this update makes active):", text)
		self.assertIn("Send a corrected script in the same update, or call validate_script first.", text)
		self.assertEqual(frappe.db.get_value("Server Script", name, "disabled"), 1)

	def test_reenabling_a_clean_script_succeeds(self):
		name = self._server_script(disabled=1, stored=CLEAN_SCRIPT)
		update_doc(doctype="Server Script", name=name, changes={"disabled": 0})
		self.assertEqual(frappe.db.get_value("Server Script", name, "disabled"), 0)

	def test_script_type_change_of_an_enabled_stored_bad_script_is_refused(self):
		name = self._server_script(disabled=0)
		with self.assertRaises(InvalidFieldValueError) as ctx:
			update_doc(doctype="Server Script", name=name, changes={"script_type": "Permission Query"})
		self.assertIn(f"Server Script {name} script (stored code,", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Server Script", name, "script_type"), "DocType Event")

	def test_script_type_change_of_a_disabled_stored_bad_script_succeeds(self):
		name = self._server_script(disabled=1)
		update_doc(doctype="Server Script", name=name, changes={"script_type": "Permission Query"})
		self.assertEqual(frappe.db.get_value("Server Script", name, "script_type"), "Permission Query")

	def test_script_type_change_with_enabling_checks_the_stored_script(self):
		name = self._server_script(disabled=1)
		with self.assertRaises(InvalidFieldValueError):
			update_doc(
				doctype="Server Script",
				name=name,
				changes={"script_type": "Permission Query", "disabled": 0},
			)
		self.assertEqual(frappe.db.get_value("Server Script", name, "script_type"), "DocType Event")

	def _stored_bad_report(self, **columns) -> str:
		"""A Report whose stored code is BAD_SCRIPT, as an old report from Desk would be."""
		name = self._report()
		columns.setdefault("report_script", BAD_SCRIPT)
		frappe.db.set_value("Report", name, columns)
		return name

	def test_report_type_change_to_script_report_checks_the_stored_script(self):
		name = self._stored_bad_report(report_type="Query Report", query="select 1")
		with self.assertRaises(InvalidFieldValueError) as ctx:
			update_doc(doctype="Report", name=name, changes={"report_type": "Script Report"})
		self.assertIn(f"Report {name} report_script (stored code,", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Report", name, "report_type"), "Query Report")

	def test_report_type_change_to_script_report_with_clean_stored_script_succeeds(self):
		name = self._stored_bad_report(
			report_type="Query Report", query="select 1", report_script=CLEAN_SCRIPT
		)
		update_doc(doctype="Report", name=name, changes={"report_type": "Script Report"})
		self.assertEqual(frappe.db.get_value("Report", name, "report_type"), "Script Report")

	def test_reenabling_a_report_with_a_stored_bad_script_is_refused(self):
		name = self._stored_bad_report(disabled=1)
		with self.assertRaises(InvalidFieldValueError) as ctx:
			update_doc(doctype="Report", name=name, changes={"disabled": 0})
		self.assertIn(f"Report {name} report_script (stored code,", str(ctx.exception))
		self.assertEqual(frappe.db.get_value("Report", name, "disabled"), 1)

	def test_reenabling_a_report_with_a_clean_stored_script_succeeds(self):
		name = self._stored_bad_report(disabled=1, report_script=CLEAN_SCRIPT)
		update_doc(doctype="Report", name=name, changes={"disabled": 0})
		self.assertEqual(frappe.db.get_value("Report", name, "disabled"), 0)

	def test_disabling_a_report_with_a_stored_bad_script_succeeds(self):
		name = self._stored_bad_report(disabled=0)
		update_doc(doctype="Report", name=name, changes={"disabled": 1})
		self.assertEqual(frappe.db.get_value("Report", name, "disabled"), 1)

	def test_a_standard_report_going_non_standard_checks_the_stored_script(self):
		# Frappe then runs report_script (report.py execute_script_report), so it is checked
		# before the save.
		name = self._stored_bad_report(is_standard="Yes")
		with self.assertRaises(InvalidFieldValueError) as ctx:
			update_doc(doctype="Report", name=name, changes={"is_standard": "No"})
		self.assertIn(f"Report {name} report_script (stored code,", str(ctx.exception))

	def test_batch_with_one_blocked_item_rolls_back_the_whole_batch(self):
		first, second = self._report("a"), self._report("b")
		with self.assertRaises(InvalidFieldValueError):
			update_doc(
				doctype="Report",
				updates=[
					{"name": first, "changes": {"report_script": 'z = f"{1}"\n'}},
					{"name": second, "changes": {"report_script": BAD_SCRIPT}},
				],
			)
		self.assertEqual(frappe.db.get_value("Report", first, "report_script"), CLEAN_SCRIPT)
		self.assertEqual(frappe.db.get_value("Report", second, "report_script"), CLEAN_SCRIPT)
