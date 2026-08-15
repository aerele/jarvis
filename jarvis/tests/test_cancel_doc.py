"""Tests for jarvis.tools.cancel_doc - fourth mutating tool.

Mirror of test_submit_doc.py - cancel is the inverse state transition
(docstatus 1 → 2). Same mock-heavy approach: on_cancel hooks belong to
the DocType, not us.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools.cancel_doc import cancel_doc


def _fake_meta(submittable: bool = True) -> MagicMock:
	m = MagicMock()
	m.is_submittable = submittable
	return m


def _fake_doc(docstatus: int = 1) -> MagicMock:
	d = MagicMock()
	d.docstatus = docstatus
	d.as_dict.return_value = {
		"name": "fake-001",
		"doctype": "Sales Invoice",
		"docstatus": 2,  # post-cancel value
	}
	return d


class TestCancelDocValidation(FrappeTestCase):
	def test_rejects_empty_doctype(self):
		with self.assertRaises(InvalidArgumentError):
			cancel_doc(doctype="", name="any")

	def test_rejects_empty_name(self):
		with self.assertRaises(InvalidArgumentError):
			cancel_doc(doctype="Sales Invoice", name="")

	def test_rejects_non_submittable_doctype(self):
		with patch("frappe.get_meta", return_value=_fake_meta(submittable=False)):
			with self.assertRaises(InvalidArgumentError) as ctx:
				cancel_doc(doctype="Note", name="any")
		self.assertIn("not submittable", str(ctx.exception))


class TestCancelDocPermissions(FrappeTestCase):
	def test_rejects_when_user_lacks_cancel_perm(self):
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=False):
				with self.assertRaises(PermissionDeniedError):
					cancel_doc(doctype="Sales Invoice", name="SINV-001")

	def test_checks_cancel_ptype_at_record_level(self):
		called_with = {}

		def fake_perm(doctype, ptype=None, doc=None, **_):
			called_with["doctype"] = doctype
			called_with["ptype"] = ptype
			called_with["doc"] = doc
			return True

		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", side_effect=fake_perm):
				with patch("frappe.get_doc", return_value=_fake_doc(docstatus=1)):
					cancel_doc(doctype="Sales Invoice", name="SINV-001")

		self.assertEqual(called_with["ptype"], "cancel")
		self.assertEqual(called_with["doc"], "SINV-001")


class TestCancelDocStateMachine(FrappeTestCase):
	"""Only Submitted (1) can be cancelled. Draft (0) and Cancelled (2)
	are refused with state-aware messages."""

	def test_rejects_draft(self):
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=_fake_doc(docstatus=0)):
					with self.assertRaises(InvalidArgumentError) as ctx:
						cancel_doc(doctype="Sales Invoice", name="SINV-001")
		# Error guides agent to suggest delete_doc for Drafts
		self.assertIn("Draft", str(ctx.exception))
		self.assertIn("delete_doc", str(ctx.exception))

	def test_rejects_already_cancelled(self):
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=_fake_doc(docstatus=2)):
					with self.assertRaises(InvalidArgumentError) as ctx:
						cancel_doc(doctype="Sales Invoice", name="SINV-001")
		self.assertIn("already cancelled", str(ctx.exception).lower())


class TestCancelDocHappyPath(FrappeTestCase):
	def test_calls_doc_cancel_and_returns_dict(self):
		doc = _fake_doc(docstatus=1)
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=doc):
					result = cancel_doc(doctype="Sales Invoice", name="SINV-001")
		doc.cancel.assert_called_once()
		self.assertEqual(result["docstatus"], 2)

	def test_propagates_validation_error_from_cancel(self):
		"""on_cancel hooks may reject - e.g. invoice partly paid → can't
		cancel without first reversing payment. Let the real reason surface."""
		doc = _fake_doc(docstatus=1)
		doc.cancel.side_effect = frappe.ValidationError("Cannot cancel: payment of ₹50,000 has been applied")
		with patch("frappe.get_meta", return_value=_fake_meta()):
			with patch("frappe.has_permission", return_value=True):
				with patch("frappe.get_doc", return_value=doc):
					with self.assertRaises(frappe.ValidationError) as ctx:
						cancel_doc(doctype="Sales Invoice", name="SINV-001")
		self.assertIn("payment", str(ctx.exception))
