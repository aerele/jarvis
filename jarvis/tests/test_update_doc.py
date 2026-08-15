"""Tests for jarvis.tools.update_doc - the first mutating tool.

Uses ``Note`` as the fixture DocType because it exists on every Frappe
site, has simple writeable fields, and modifying test rows can't break
anything else.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
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

	def test_passes_doc_to_has_permission(self):
		"""Permission is checked record-level (with doc=name), not just at
		the DocType level."""
		called_with = {}

		def fake_perm(doctype, ptype=None, doc=None, **_):
			called_with["doctype"] = doctype
			called_with["ptype"] = ptype
			called_with["doc"] = doc
			return True

		with patch("frappe.has_permission", side_effect=fake_perm):
			update_doc(
				doctype=NOTE_DT,
				name=self.note,
				changes={"content": "new"},
			)

		self.assertEqual(called_with["doctype"], NOTE_DT)
		self.assertEqual(called_with["ptype"], "write")
		self.assertEqual(called_with["doc"], self.note)


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
