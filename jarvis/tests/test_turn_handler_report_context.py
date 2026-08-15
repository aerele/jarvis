"""Tests for the query-report branch of ``_prepend_doc_context`` and its
sibling helper ``_format_report_filters``.

Both were added 2026-07-06 to close the gap where the Desk chat widget's
``readContext()`` had no branch for ``/app/query-report/*`` - the LLM
received zero filter context, so the model had to re-derive filters from
the user's natural-language message. The frontend now captures
``{report_name, filters}`` for query-report routes; these tests pin the
backend half of the round-trip.
"""

from __future__ import annotations

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.turn_handler import (
	_REPORT_FILTER_LINE_CAP,
	_format_report_filters,
	_prepend_doc_context,
)


class TestFormatReportFilters(FrappeTestCase):
	def test_empty_dict_returns_empty_string(self):
		self.assertEqual(_format_report_filters({}), "")

	def test_all_falsy_values_returns_empty_string(self):
		"""None / "" / [] / {} are all "unset" from the operator's
		perspective - dropping them keeps the prompt line tight."""
		self.assertEqual(
			_format_report_filters(
				{
					"customer": None,
					"company": "",
					"cost_centers": [],
					"tag_filters": {},
				}
			),
			"",
		)

	def test_scalar_values_render_as_key_repr_pairs(self):
		out = _format_report_filters(
			{
				"company": "Acme Ltd",
				"from_date": "2026-01-01",
				"show_opening_entries": 1,
			}
		)
		self.assertTrue(out.startswith(" with filters {"))
		self.assertIn("company='Acme Ltd'", out)
		self.assertIn("from_date='2026-01-01'", out)
		self.assertIn("show_opening_entries=1", out)
		self.assertTrue(out.endswith("}"))

	def test_list_and_operator_pair_values_survive(self):
		"""Frappe filters can be lists (multi-select) or ``["op", "value"]``
		pairs (negation, ranges). Both must render as-is so the model
		can call ``run_report`` with the same shape."""
		out = _format_report_filters(
			{
				"account": ["Debtors", "Creditors"],
				"posting_date": ["between", ["2026-01-01", "2026-01-31"]],
			}
		)
		self.assertIn("account=['Debtors', 'Creditors']", out)
		self.assertIn("posting_date=['between', ['2026-01-01', '2026-01-31']]", out)

	def test_truncates_at_cap_with_total_count(self):
		"""A pathological filter dict (many keys, or a single huge value)
		must not blow the prompt budget. Cap kicks in with a marker so
		the model knows more filters exist than what's shown."""
		# Build a filter dict whose rendered body clearly exceeds the cap.
		huge = {f"filter_{i}": "x" * 40 for i in range(20)}
		out = _format_report_filters(huge)
		# The prefix ' with filters {' + suffix '}' surround the body,
		# so the whole line will be > cap. What matters is that the body
		# is truncated and the count marker is present.
		self.assertIn("(truncated;", out)
		self.assertIn("20 filters total)", out)
		# The full body would be ~ 20 * (len("filter_XX='xxx...xx'") + 2)
		# = well over the cap; the truncation shortens it.
		self.assertLess(len(out), 800)  # generous upper bound; cap is 400

	def test_truncation_marker_matches_actual_key_count(self):
		"""Sanity: the "N filters total" number is derived from the input
		length, so it must equal the number of *non-falsy* keys."""
		filters = dict.fromkeys([f"k{i}" for i in range(50)], "value_" + "x" * 30)
		# Also add some falsy entries that must NOT be counted.
		filters["skip_a"] = None
		filters["skip_b"] = ""
		out = _format_report_filters(filters)
		self.assertIn("(truncated;", out)
		self.assertIn("50 filters total)", out)

	def test_line_cap_is_400(self):
		"""Pin the cap constant so a future change is a deliberate opt-in,
		not an accidental prompt-budget blowout."""
		self.assertEqual(_REPORT_FILTER_LINE_CAP, 400)


class TestPrependDocContextReportBranch(FrappeTestCase):
	"""Pin the query-report branch of ``_prepend_doc_context``. The
	pre-existing doctype / list branches are covered indirectly by
	``test_turn_handler_thinking.py`` (which pins that ``/think`` leads
	even when ``[Viewing: ...]`` prefixes); this class only exercises
	the report path added 2026-07-06."""

	def test_report_name_with_no_filters(self):
		out = _prepend_doc_context(
			"why zero rows?",
			{"report_name": "General Ledger", "filters": {}},
		)
		self.assertEqual(
			out,
			"[Viewing: General Ledger report; resolve 'this'/'here' against it]\n\nwhy zero rows?",
		)

	def test_report_name_with_filters(self):
		out = _prepend_doc_context(
			"widen the date range by a month",
			{
				"report_name": "General Ledger",
				"filters": {
					"company": "Acme Ltd",
					"from_date": "2026-01-01",
					"to_date": "2026-01-31",
				},
			},
		)
		self.assertIn(
			"[Viewing: General Ledger report with filters {",
			out,
		)
		self.assertIn("company='Acme Ltd'", out)
		self.assertIn("from_date='2026-01-01'", out)
		self.assertTrue(out.endswith("widen the date range by a month"))

	def test_report_name_takes_precedence_over_doctype(self):
		"""The report route also carries ``ref_doctype`` (e.g. General
		Ledger -> GL Entry). When both fields are set we want the report
		phrasing (with filter context), not the doctype phrasing."""
		out = _prepend_doc_context(
			"what is this?",
			{
				"report_name": "General Ledger",
				"filters": {"company": "Acme Ltd"},
				"doctype": "GL Entry",
				"name": "",
			},
		)
		self.assertIn("General Ledger report", out)
		self.assertNotIn("GL Entry", out)

	def test_missing_report_name_falls_through_to_doctype_branch(self):
		"""Regression check: the pre-existing doctype path must not
		break for a plain Form/List capture that has no report_name."""
		out = _prepend_doc_context(
			"is this overdue?",
			{"doctype": "Sales Invoice", "name": "SINV-0001"},
		)
		self.assertEqual(
			out,
			"[Viewing: Sales Invoice SINV-0001; resolve 'this'/'here' against it]\n\nis this overdue?",
		)

	def test_empty_context_returns_unchanged(self):
		"""Regression: no context still returns the raw user message."""
		self.assertEqual(_prepend_doc_context("hello", None), "hello")
		self.assertEqual(_prepend_doc_context("hello", {}), "hello")

	def test_report_name_whitespace_only_is_ignored(self):
		"""Whitespace-only report_name isn't a real report - fall through
		to the doctype branch (or return unchanged if that's empty too)."""
		out = _prepend_doc_context(
			"any updates?",
			{"report_name": "   ", "filters": {"x": 1}},
		)
		self.assertEqual(out, "any updates?")

	def test_filters_that_are_not_a_dict_are_dropped(self):
		"""If the frontend somehow ships a non-dict filters value (bad
		JSON, stale schema, etc.) the report line still renders - just
		without a "with filters" clause."""
		out = _prepend_doc_context(
			"tell me about this",
			{"report_name": "Stock Ledger", "filters": "oops-a-string"},
		)
		self.assertEqual(
			out,
			"[Viewing: Stock Ledger report; resolve 'this'/'here' against it]\n\ntell me about this",
		)
