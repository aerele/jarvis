"""Tests for jarvis.tools.delete_doc - the destructive tool.

End-to-end uses ``Note`` as the fixture DocType (same as create/update):
delete on Note works without side effects, and we get to exercise the
real Frappe ``delete_doc`` plumbing on the happy path.

Permission and submitted-doc gate tests use mocks because there's no
easy way to construct a submitted state on a non-submittable DocType.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.delete_doc import delete_doc

NOTE_DT = "Note"


def _make_note(title: str = "jarvis-delete-test") -> str:
	doc = frappe.get_doc(
		{
			"doctype": NOTE_DT,
			"title": title,
			"content": "to be deleted",
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _fake_submitted_doc() -> MagicMock:
	d = MagicMock()
	d.docstatus = 1
	return d


class TestDeleteDocValidation(FrappeTestCase):
	def test_rejects_empty_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			delete_doc(doctype="", name="any")

	def test_rejects_empty_name(self):
		with self.assertRaises(InvalidArgumentError):
			delete_doc(doctype=NOTE_DT, name="")


class TestDeleteDocPermissions(FrappeTestCase):
	def test_rejects_when_user_lacks_delete_perm(self):
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				delete_doc(doctype=NOTE_DT, name="any")

	def test_checks_delete_ptype_at_record_level(self):
		"""Like update / cancel, delete perm is record-scoped."""
		called_with = {}

		def fake_perm(doctype, ptype=None, doc=None, **_):
			called_with["doctype"] = doctype
			called_with["ptype"] = ptype
			called_with["doc"] = doc
			return True

		note = _make_note()
		try:
			with patch("frappe.has_permission", side_effect=fake_perm):
				delete_doc(doctype=NOTE_DT, name=note)
		finally:
			if frappe.db.exists(NOTE_DT, note):
				frappe.delete_doc(NOTE_DT, note, ignore_permissions=True, force=True)
				frappe.db.commit()

		self.assertEqual(called_with["doctype"], NOTE_DT)
		self.assertEqual(called_with["ptype"], "delete")
		self.assertEqual(called_with["doc"], note)


class TestDeleteDocSubmittedGuard(FrappeTestCase):
	"""Submitted (docstatus=1) docs must be cancelled before delete.
	Pre-checking gives the agent a clear "cancel first" message instead
	of Frappe's generic "cannot delete submitted document" error."""

	def test_rejects_submitted_doc(self):
		with patch("frappe.has_permission", return_value=True):
			with patch("frappe.get_doc", return_value=_fake_submitted_doc()):
				with self.assertRaises(InvalidArgumentError) as ctx:
					delete_doc(doctype="Sales Invoice", name="SINV-001")
		self.assertIn("Submitted", str(ctx.exception))
		self.assertIn("cancel_doc", str(ctx.exception))


class TestDeleteDocHappyPath(FrappeTestCase):
	"""End-to-end: real Note insert → real delete → row is gone."""

	def test_deletes_row_and_returns_confirmation(self):
		note = _make_note()
		self.assertTrue(frappe.db.exists(NOTE_DT, note))

		result = delete_doc(doctype=NOTE_DT, name=note)

		self.assertEqual(result["deleted"], True)
		self.assertEqual(result["doctype"], NOTE_DT)
		self.assertEqual(result["name"], note)
		self.assertFalse(frappe.db.exists(NOTE_DT, note))

	def test_unknown_record_raises(self):
		with self.assertRaises(frappe.DoesNotExistError):
			delete_doc(doctype=NOTE_DT, name="JV-NONEXISTENT-99999")
