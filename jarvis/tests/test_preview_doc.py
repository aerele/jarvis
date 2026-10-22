"""Tests for jarvis.tools.preview_doc - the dry-run create.

Uses ``Note`` as the fixture DocType (same reasoning as test_create_doc):
exists on every site, simple fields, no cross-module side effects. The
essential contract: the full insert pipeline runs, and NOTHING persists.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import preview_doc as preview_mod
from jarvis.tools.preview_doc import preview_doc

NOTE_DT = "Note"


class TestPreviewDoc(FrappeTestCase):
	def test_previews_without_persisting(self):
		before = frappe.db.count(NOTE_DT)
		out = preview_doc(NOTE_DT, {"title": "jarvis-preview-test", "content": "x"})
		self.assertTrue(out["valid"])
		self.assertEqual(out["resolved"].get("title"), "jarvis-preview-test")
		self.assertEqual(frappe.db.count(NOTE_DT), before)
		self.assertFalse(frappe.db.exists(NOTE_DT, {"title": "jarvis-preview-test"}))

	def test_reports_server_filled_defaults(self):
		out = preview_doc(NOTE_DT, {"title": "jarvis-preview-defaults"})
		self.assertTrue(out["valid"])
		# Note.public defaults to 0 via the insert pipeline; the caller did
		# not pass it, so any resolved-but-not-passed field lands here.
		for f in out["server_filled"]:
			self.assertNotIn(f, ("title",))

	def test_invalid_doc_returns_valid_false_not_raise(self):
		# ToDo requires description; missing it must come back as a clean
		# {valid: false} instead of an exception.
		out = preview_doc("ToDo", {"priority": "High"})
		self.assertFalse(out["valid"])
		self.assertTrue(out["error"])

	def test_explicit_values_win(self):
		out = preview_doc("ToDo", {"description": "jarvis preview", "priority": "High"})
		self.assertTrue(out["valid"])
		self.assertEqual(out["resolved"].get("priority"), "High")
		self.assertEqual(frappe.db.count("ToDo", {"description": "jarvis preview"}), 0)

	def test_rejects_protected_fields(self):
		with self.assertRaises(InvalidArgumentError):
			preview_doc(NOTE_DT, {"title": "x", "owner": "hacker@example.com"})

	def test_rejects_empty_values(self):
		with self.assertRaises(InvalidArgumentError):
			preview_doc(NOTE_DT, {})

	def test_permission_denied(self):
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(PermissionDeniedError):
				preview_doc(NOTE_DT, {"title": "x"})

	def test_commit_restored_after_preview(self):
		# Compare __func__: attribute access mints a fresh bound method, so
		# identity on the bound method itself always fails.
		real_func = frappe.db.commit.__func__
		preview_doc(NOTE_DT, {"title": "jarvis-preview-commit"})
		self.assertIs(getattr(frappe.db.commit, "__func__", None), real_func)
		preview_doc("ToDo", {"priority": "High"})  # invalid -> valid:false path
		self.assertIs(getattr(frappe.db.commit, "__func__", None), real_func)

	def test_preview_drops_queued_commit_callbacks(self):
		# Savepoint rollback does not clear the after_commit queue; without
		# the sandbox restore, a queued webhook would fire on the request's
		# next real commit for a rolled-back document.
		from jarvis.tools._preview_sandbox import preview_sandbox

		before = len(frappe.db.after_commit._functions)
		with preview_sandbox():
			frappe.db.after_commit.add(lambda: None)
		self.assertEqual(len(frappe.db.after_commit._functions), before)

	def test_sandbox_restores_commit_when_savepoint_fails(self):
		from jarvis.tools._preview_sandbox import preview_sandbox

		real_func = frappe.db.commit.__func__
		with patch.object(frappe.db, "savepoint", side_effect=Exception("boom")):
			with self.assertRaises(Exception):
				with preview_sandbox():
					pass  # never reached
		self.assertIs(getattr(frappe.db.commit, "__func__", None), real_func)

	def test_zero_defaults_not_reported_as_server_filled(self):
		out = preview_doc("ToDo", {"description": "jarvis preview baseline"})
		self.assertTrue(out["valid"])
		self.assertNotIn("status", out["server_filled"])


class _Inv:
	"""Minimal stand-in for a built invoice: attribute + .get() access."""

	def __init__(self, doctype="Purchase Invoice", **kw):
		self.doctype = doctype
		self.supplier = "SUP"
		self.customer = "CUST"
		self.company = "Co"
		self.posting_date = "2026-10-20"
		self.grand_total = 500.0
		self.bill_no = None
		self.__dict__.update(kw)

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestPossibleDuplicates(FrappeTestCase):
	def _run(self, doc, rows_by_call, caller=None):
		with patch("frappe.get_list", side_effect=rows_by_call) as gl:
			out = preview_mod._possible_duplicates(doc, caller or {})
		return out, gl

	def _row(self, name, **kw):
		return {"name": name, "posting_date": "2026-10-20", "grand_total": 500.0, "docstatus": 1, **kw}

	def test_found_by_date_and_total(self):
		out, gl = self._run(_Inv(), [[self._row("PI-1")]])
		self.assertEqual([r["name"] for r in out], ["PI-1"])
		self.assertEqual(gl.call_count, 1)  # no bill_no, one query
		filters = gl.call_args.kwargs["filters"]
		self.assertEqual(filters["supplier"], "SUP")
		self.assertEqual(filters["company"], "Co")
		self.assertEqual(filters["posting_date"], "2026-10-20")
		self.assertEqual(filters["grand_total"], 500.0)

	def test_cancelled_excluded_by_filter(self):
		_, gl = self._run(_Inv(), [[]])
		self.assertEqual(gl.call_args.kwargs["filters"]["docstatus"], ["<", 2])

	def test_found_by_bill_no_and_deduped(self):
		doc = _Inv(bill_no="INV-9")
		out, gl = self._run(doc, [[self._row("PI-1")], [self._row("PI-1"), self._row("PI-2")]])
		self.assertEqual([r["name"] for r in out], ["PI-1", "PI-2"])
		self.assertEqual(gl.call_count, 2)
		self.assertEqual(gl.call_args.kwargs["filters"]["bill_no"], "INV-9")
		self.assertNotIn("grand_total", gl.call_args.kwargs["filters"])
		self.assertIn("bill_no", gl.call_args.kwargs["fields"])

	def test_none_found_is_empty_list(self):
		out, _ = self._run(_Inv(bill_no="X"), [[], []])
		self.assertEqual(out, [])

	def test_caller_posting_date_wins_over_pipeline_date(self):
		doc = _Inv(posting_date="2026-10-22")
		_, gl = self._run(doc, [[]], caller={"posting_date": "2026-10-20"})
		self.assertEqual(gl.call_args.kwargs["filters"]["posting_date"], "2026-10-20")

	def test_capped_at_five(self):
		rows = [self._row(f"PI-{i}") for i in range(8)]
		out, _ = self._run(_Inv(), [rows])
		self.assertEqual(len(out), 5)

	def test_sales_invoice_uses_customer_and_no_bill_no(self):
		doc = _Inv("Sales Invoice", bill_no="IGNORED")
		_, gl = self._run(doc, [[]])
		self.assertEqual(gl.call_count, 1)
		filters = gl.call_args.kwargs["filters"]
		self.assertEqual(filters["customer"], "CUST")
		self.assertNotIn("supplier", filters)
		self.assertNotIn("bill_no", gl.call_args.kwargs["fields"])

	def test_key_absent_for_non_invoice_doctype(self):
		out = preview_doc(NOTE_DT, {"title": "jarvis-preview-nodup"})
		self.assertNotIn("possible_duplicates", out)

	def test_key_present_even_when_empty(self):
		with (
			patch.dict(preview_mod._DUPLICATE_CHECK, {NOTE_DT: {"party": "title", "bill_no": None}}),
			patch.object(preview_mod, "_possible_duplicates", return_value=[]),
		):
			out = preview_doc(NOTE_DT, {"title": "jarvis-preview-dupkey"})
		self.assertEqual(out["possible_duplicates"], [])

	def test_no_read_permission_drops_the_key_not_the_preview(self):
		with (
			patch.dict(preview_mod._DUPLICATE_CHECK, {NOTE_DT: {"party": "title", "bill_no": None}}),
			patch.object(preview_mod, "_possible_duplicates", side_effect=frappe.PermissionError),
		):
			out = preview_doc(NOTE_DT, {"title": "jarvis-preview-noread"})
		self.assertTrue(out["valid"])
		self.assertNotIn("possible_duplicates", out)

	def test_invalid_path_has_no_duplicates_key(self):
		with patch.dict(preview_mod._DUPLICATE_CHECK, {"ToDo": {"party": "owner", "bill_no": None}}):
			out = preview_doc("ToDo", {"priority": "High"})
		self.assertFalse(out["valid"])
		self.assertNotIn("possible_duplicates", out)
