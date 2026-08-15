"""Tests for jarvis.tools.amend_doc - closes the mutation lifecycle.

Mock-heavy like submit/cancel: ``frappe.copy_doc`` and the ``amended_from``
machinery are Frappe-owned. We verify our orchestration:
  - arg + state machine validation
  - permission gate (ptype="amend", record-level)
  - new doc gets ``amended_from`` + ``docstatus=0`` before insert
  - return value is the new draft, not the original
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.amend_doc import amend_doc


def _fake_meta(submittable: bool = True) -> MagicMock:
	m = MagicMock()
	m.is_submittable = submittable
	return m


def _fake_source(docstatus: int = 2, name: str = "SINV-2026-001") -> MagicMock:
	d = MagicMock()
	d.docstatus = docstatus
	d.name = name
	return d


def _fake_copy(amended_from: str = "") -> MagicMock:
	"""Stand-in for what ``frappe.copy_doc`` returns: a fresh doc ready
	to be configured + inserted."""
	d = MagicMock()
	d.amended_from = amended_from
	d.docstatus = 0
	d.as_dict.return_value = {
		"name": "SINV-2026-001-1",
		"doctype": "Sales Invoice",
		"docstatus": 0,
		"amended_from": amended_from or "SINV-2026-001",
	}
	return d


class TestAmendDocValidation(FrappeTestCase):
	def test_rejects_empty_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			amend_doc(doctype="", name="any")

	def test_rejects_empty_name(self):
		with self.assertRaises(InvalidArgumentError):
			amend_doc(doctype="Sales Invoice", name="")

	def test_rejects_non_submittable_doctype(self):
		with patch("frappe.get_meta", return_value=_fake_meta(submittable=False)):
			with self.assertRaises(InvalidArgumentError) as ctx:
				amend_doc(doctype="Note", name="any")
		self.assertIn("not submittable", str(ctx.exception))


class TestAmendDocPermissions(FrappeTestCase):
	def test_rejects_when_user_lacks_amend_perm(self):
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=False):
				with self.assertRaises(PermissionDeniedError):
					amend_doc(doctype="Sales Invoice", name="SINV-001")

	def test_checks_amend_ptype_at_record_level(self):
		called_with = {}

		def fake_perm(doctype, ptype=None, doc=None, **_):
			called_with["doctype"] = doctype
			called_with["ptype"] = ptype
			called_with["doc"] = doc
			return True

		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", side_effect=fake_perm):
				with patch("frappe.get_doc", return_value=_fake_source(docstatus=2)):
					with patch("frappe.copy_doc", return_value=_fake_copy()):
						amend_doc(doctype="Sales Invoice", name="SINV-001")

		self.assertEqual(called_with["ptype"], "amend")
		self.assertEqual(called_with["doc"], "SINV-001")


class TestAmendDocStateMachine(FrappeTestCase):
	"""Source must be Cancelled. Drafts and Submitted have other paths."""

	def test_rejects_draft_source(self):
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=_fake_source(docstatus=0)):
					with self.assertRaises(InvalidArgumentError) as ctx:
						amend_doc(doctype="Sales Invoice", name="SINV-001")
		# Error guides toward update_doc for Drafts
		self.assertIn("update_doc", str(ctx.exception))

	def test_rejects_submitted_source(self):
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=_fake_source(docstatus=1)):
					with self.assertRaises(InvalidArgumentError) as ctx:
						amend_doc(doctype="Sales Invoice", name="SINV-001")
		self.assertIn("cancel_doc", str(ctx.exception))


class TestAmendDocHappyPath(FrappeTestCase):
	def test_copies_links_and_inserts_as_draft(self):
		source = _fake_source(docstatus=2, name="SINV-2026-001")
		new_doc = _fake_copy()
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=source):
					with patch("frappe.copy_doc", return_value=new_doc):
						result = amend_doc(
							doctype="Sales Invoice",
							name="SINV-2026-001",
						)

		# amended_from was wired to the source name
		self.assertEqual(new_doc.amended_from, "SINV-2026-001")
		# docstatus reset to Draft before insert
		self.assertEqual(new_doc.docstatus, 0)
		# insert() was called on the new draft
		new_doc.insert.assert_called_once()
		# Returned the NEW draft, not the source
		self.assertEqual(result["name"], "SINV-2026-001-1")
		self.assertEqual(result["docstatus"], 0)
		self.assertEqual(result["amended_from"], "SINV-2026-001")

	def test_propagates_validation_error_from_insert(self):
		"""If the cancelled source had data that fails validation in the
		new Draft (e.g., expired tax rate), the error surfaces unchanged
		so the agent can tell the user what needs fixing."""
		source = _fake_source(docstatus=2)
		new_doc = _fake_copy()
		new_doc.insert.side_effect = frappe.ValidationError("Tax rate has expired")
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=source):
					with patch("frappe.copy_doc", return_value=new_doc):
						with self.assertRaises(frappe.ValidationError) as ctx:
							amend_doc(doctype="Sales Invoice", name="SINV-001")
		self.assertIn("Tax rate", str(ctx.exception))
