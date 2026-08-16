"""Tests for ``jarvis.tools._export.document.graphics`` (Slice 1: CSS charts,
KPI/RAG components, and locale-correct number formatting - no image deps).

Two groups:

* ``css_bar``/``kpi_tile``/``rag_chip`` are pure (no ``frappe`` import) and run
  WITHOUT a site: ``python -m unittest jarvis.tests.test_export_document_graphics``.
* ``fmt_amount``/``fmt_pct`` reuse ``frappe.utils.fmt_money`` /
  ``frappe.utils.data.get_number_format``, both of which read
  ``frappe.local.lang`` - they raise ``AttributeError: lang`` outside a real
  Frappe site/request context. The real-formatting tests below are marked
  ``skip`` with that reason (verified against this repo's bench-env python,
  see the task report) and belong to the end-to-end/site gate instead. The
  edge-case GUARD logic (None/NaN/Inf/-0.0 handling, before ``fmt_money`` is
  ever reached) is still fully unit-tested here by patching
  ``fmt_money``/``get_number_format`` to site-free stubs.
"""

import html
import math
import unittest
from unittest.mock import patch

import frappe

from jarvis.tools._export.document import graphics
from jarvis.tools._export.document.graphics import css_bar, fmt_amount, fmt_pct, kpi_tile, rag_chip

_HAS_SITE = bool(getattr(frappe.local, "site", None))

# --- css_bar --------------------------------------------------------------


class TestCssBar(unittest.TestCase):
	def test_css_bar_wraps_in_bar_chart_class(self) -> None:
		out = css_bar([{"label": "Q1", "value": "10", "pct": 50}])
		self.assertTrue(out.startswith('<div class="bar-chart">'))
		self.assertTrue(out.endswith("</div>"))

	def test_css_bar_bar_class_and_width(self) -> None:
		out = css_bar([{"label": "Q1", "value": "10", "pct": 50}])
		self.assertIn('<div class="bar" style="width:50%">Q1 10</div>', out)

	def test_css_bar_width_clamps_low(self) -> None:
		out = css_bar([{"label": "x", "value": "y", "pct": -5}])
		self.assertIn('style="width:0%"', out)

	def test_css_bar_width_clamps_high(self) -> None:
		out = css_bar([{"label": "x", "value": "y", "pct": 150}])
		self.assertIn('style="width:100%"', out)

	def test_css_bar_width_clamps_nan_to_zero(self) -> None:
		out = css_bar([{"label": "x", "value": "y", "pct": float("nan")}])
		self.assertIn('style="width:0%"', out)

	def test_css_bar_width_clamps_inf_to_hundred(self) -> None:
		out = css_bar([{"label": "x", "value": "y", "pct": float("inf")}])
		self.assertIn('style="width:100%"', out)

	def test_css_bar_width_non_numeric_clamps_to_zero(self) -> None:
		out = css_bar([{"label": "x", "value": "y", "pct": "garbage"}])
		self.assertIn('style="width:0%"', out)

	def test_css_bar_escapes_script_label(self) -> None:
		out = css_bar([{"label": "<script>alert(1)</script>", "value": "1", "pct": 10}])
		self.assertNotIn("<script", out)
		self.assertIn("&lt;script&gt;", out)

	def test_css_bar_escapes_quote_in_value(self) -> None:
		out = css_bar([{"label": "x", "value": '10" onmouseover="x()', "pct": 10}])
		self.assertNotIn('onmouseover="x()"', out)
		self.assertIn("&quot;", out)

	def test_css_bar_accepts_positional_tuple_rows(self) -> None:
		out = css_bar([("Q1", "10", 50)])
		self.assertIn('<div class="bar" style="width:50%">Q1 10</div>', out)

	def test_css_bar_multiple_rows(self) -> None:
		out = css_bar([{"label": "A", "value": "1", "pct": 10}, {"label": "B", "value": "2", "pct": 90}])
		self.assertEqual(out.count('<div class="bar"'), 2)

	def test_css_bar_empty_rows(self) -> None:
		self.assertEqual(css_bar([]), '<div class="bar-chart"></div>')

	def test_css_bar_missing_keys_default_safely(self) -> None:
		out = css_bar([{}])
		self.assertIn('style="width:0%"', out)

	def test_css_bar_malformed_row_raises(self) -> None:
		"""A row that is neither a mapping nor a 3-item (label, value, pct) sequence
		raises ValueError rather than a blind-unpack TypeError/ValueError - so the
		orchestrator's _splice_charts can catch it and degrade the chart to a note
		instead of the whole export crashing with a raw 500 and no telemetry."""
		for bad in [5, None, True, "abc", [1, 2], [1, 2, 3, 4], object()]:
			with self.subTest(bad=bad):
				with self.assertRaises(ValueError):
					css_bar([bad])

	def test_css_bar_valid_3seq_does_not_raise(self) -> None:
		# Exactly three positional items is the legitimate shape and must still work.
		out = css_bar([[1, 2, 50]])
		self.assertIn('<div class="bar" style="width:50%">1 2</div>', out)

	def test_css_bar_none_label_value_render_blank_not_none(self) -> None:
		out = css_bar([{"label": None, "value": None, "pct": 10}])
		self.assertNotIn("None", out)
		self.assertIn('<div class="bar" style="width:10%"> </div>', out)

	def test_css_bar_tiny_pct_no_scientific_notation(self) -> None:
		# 0.00001 formatted with :g would be "1e-05", which old WebKit drops. Fixed
		# notation renders it as a plain (rounded-to-zero) width.
		out = css_bar([{"label": "x", "value": "y", "pct": 0.00001}])
		self.assertNotIn("e-", out)
		self.assertIn('style="width:0%"', out)

	def test_css_bar_fractional_pct_trims_trailing_zeros(self) -> None:
		out = css_bar([{"label": "x", "value": "y", "pct": 33.5}])
		self.assertIn('style="width:33.5%"', out)


# --- kpi_tile ---------------------------------------------------------------


class TestKpiTile(unittest.TestCase):
	def test_kpi_tile_class_name(self) -> None:
		out = kpi_tile("Revenue", "1,000")
		self.assertIn('<div class="kpi-tile">', out)

	def test_kpi_tile_label_and_value_present(self) -> None:
		out = kpi_tile("Revenue", "1,000")
		self.assertIn("Revenue", out)
		self.assertIn("1,000", out)

	def test_kpi_tile_without_delta_omits_delta_div(self) -> None:
		out = kpi_tile("Revenue", "1,000")
		self.assertNotIn("kpi-delta", out)

	def test_kpi_tile_with_delta(self) -> None:
		out = kpi_tile("Revenue", "1,000", delta="+5%")
		self.assertIn('<div class="kpi-delta">+5%</div>', out)

	def test_kpi_tile_escapes_script_in_label(self) -> None:
		out = kpi_tile("<script>alert(1)</script>", "1")
		self.assertNotIn("<script", out)
		self.assertIn("&lt;script&gt;", out)

	def test_kpi_tile_escapes_quote_in_value(self) -> None:
		out = kpi_tile("x", '1" onmouseover="x()')
		self.assertNotIn('onmouseover="x()"', out)
		self.assertIn("&quot;", out)

	def test_kpi_tile_escapes_delta(self) -> None:
		out = kpi_tile("x", "1", delta="<script>bad()</script>")
		self.assertNotIn("<script", out)

	def test_kpi_tile_none_value_renders_blank_not_none(self) -> None:
		out = kpi_tile("Revenue", None)
		self.assertNotIn("None", out)
		self.assertIn('<div class="kpi-value"></div>', out)


# --- rag_chip -----------------------------------------------------------


class TestRagChip(unittest.TestCase):
	def test_rag_chip_class_names(self) -> None:
		for status, expected_class in [("red", "rag-red"), ("amber", "rag-amber"), ("green", "rag-green")]:
			with self.subTest(status=status):
				out = rag_chip(status)
				self.assertIn(f'class="{expected_class}"', out)

	def test_rag_chip_case_insensitive(self) -> None:
		out = rag_chip("GREEN")
		self.assertIn('class="rag-green"', out)

	def test_rag_chip_invalid_status_raises(self) -> None:
		with self.assertRaises(ValueError):
			rag_chip("blue")

	def test_rag_chip_preserves_original_casing_as_escaped_text(self) -> None:
		out = rag_chip("Red")
		self.assertEqual(out, '<span class="rag-red">Red</span>')


# --- fmt_amount / fmt_pct: real integration (needs a Frappe site) -----------
#
# frappe.utils.fmt_money / frappe.utils.data.get_number_format read
# frappe.local.lang and raise `AttributeError: lang` outside a site. These use
# ``skipUnless`` (NOT unconditional ``skip``, which would never run even inside
# the bench gate) so they DO run once a site is present, and they assert exact
# DELEGATION to fmt_money rather than a hard-coded locale string - proving
# fmt_amount/fmt_pct reuse the framework formatter without pinning the test to
# one site's number-format setting.


class TestFmtRealIntegration(unittest.TestCase):
	@unittest.skipUnless(_HAS_SITE, "needs a bench site (frappe.local.lang)")
	def test_fmt_amount_delegates_to_fmt_money(self) -> None:
		from frappe.utils import fmt_money

		# Whatever the site's number format (incl. Indian lakh/crore), fmt_amount must
		# equal the escaped fmt_money output - i.e. it reuses the framework grouping.
		self.assertEqual(fmt_amount(1234567), html.escape(fmt_money(1234567)))

	@unittest.skipUnless(_HAS_SITE, "needs a bench site (frappe.local.lang)")
	def test_fmt_pct_delegates_at_number_format_precision(self) -> None:
		from frappe.utils import fmt_money
		from frappe.utils.data import get_number_format

		expected = html.escape(fmt_money(42.5, precision=get_number_format().precision)) + "%"
		self.assertEqual(fmt_pct(42.5), expected)


# --- fmt_amount / fmt_pct: edge-case guard logic (no site needed) -----------
#
# fmt_money/get_number_format are patched to site-free stubs so ONLY the
# wrapper's own None/NaN/Inf/-0.0/negative-parens guard logic is under test.
# For the None/NaN/Inf cases the guard returns before ever calling the stub
# (proven below by asserting the stub was not invoked), so those specific
# assertions do not even depend on the patch being correct.


class TestFmtGuards(unittest.TestCase):
	def setUp(self) -> None:
		self.calls = {"fmt_money": [], "get_number_format": 0}

		def _fmt_money_stub(amount, precision=None, currency=None, format=None):
			self.calls["fmt_money"].append((amount, precision, currency))
			return f"{amount:.2f}"

		class _NF:
			precision = 2

		def _get_number_format_stub():
			self.calls["get_number_format"] += 1
			return _NF()

		patch.object(graphics, "fmt_money", _fmt_money_stub).start()
		patch.object(graphics, "get_number_format", _get_number_format_stub).start()
		self.addCleanup(patch.stopall)

	def test_fmt_amount_none_nan_inf_render_em_dash(self) -> None:
		for bad in [None, float("nan"), float("inf"), float("-inf")]:
			with self.subTest(bad=bad):
				self.assertEqual(fmt_amount(bad), "—")
				self.assertEqual(self.calls["fmt_money"], [])  # never reaches the (stubbed) formatter

	def test_fmt_pct_none_nan_inf_render_em_dash(self) -> None:
		for bad in [None, float("nan"), float("inf"), float("-inf")]:
			with self.subTest(bad=bad):
				self.assertEqual(fmt_pct(bad), "—")
				self.assertEqual(self.calls["fmt_money"], [])
				self.assertEqual(self.calls["get_number_format"], 0)

	def test_fmt_amount_negative_zero_is_not_negative(self) -> None:
		out = fmt_amount(-0.0)
		self.assertNotEqual(out, "—")
		self.assertNotIn("neg", out)
		# the stub received a non-negative magnitude, never a bare "-0.00"
		((amount, _precision, _currency),) = self.calls["fmt_money"]
		self.assertEqual(amount, 0.0)
		self.assertEqual(math.copysign(1, amount), 1.0)

	def test_fmt_pct_negative_zero_is_not_negative(self) -> None:
		out = fmt_pct(-0.0)
		self.assertNotEqual(out, "—")
		self.assertNotIn("neg", out)

	def test_fmt_amount_negative_wraps_in_parens_and_neg_class(self) -> None:
		out = fmt_amount(-1234.5)
		self.assertEqual(out, '<span class="neg">(1234.50)</span>')

	def test_fmt_amount_positive_has_no_neg_class(self) -> None:
		out = fmt_amount(1234.5)
		self.assertNotIn("neg", out)
		self.assertEqual(out, "1234.50")

	def test_fmt_amount_passes_abs_value_to_formatter(self) -> None:
		fmt_amount(-1234.5)
		((amount, _precision, _currency),) = self.calls["fmt_money"]
		self.assertEqual(amount, 1234.5)

	def test_fmt_amount_passes_currency_through(self) -> None:
		fmt_amount(10, currency="INR")
		((_amount, _precision, currency),) = self.calls["fmt_money"]
		self.assertEqual(currency, "INR")

	def test_fmt_pct_negative_wraps_in_parens_and_neg_class(self) -> None:
		out = fmt_pct(-42.5)
		self.assertEqual(out, '<span class="neg">(42.50%)</span>')

	def test_fmt_pct_positive_has_percent_suffix_no_neg_class(self) -> None:
		out = fmt_pct(42.5)
		self.assertEqual(out, "42.50%")
		self.assertNotIn("neg", out)

	def test_fmt_pct_uses_number_format_precision_not_currency_default(self) -> None:
		fmt_pct(42.5)
		self.assertEqual(self.calls["get_number_format"], 1)
		((_amount, precision, _currency),) = self.calls["fmt_money"]
		self.assertEqual(precision, 2)  # from the stub NumberFormat, not a currency-precision default

	def test_fmt_amount_string_input_with_commas(self) -> None:
		out = fmt_amount("1,234.5")
		self.assertEqual(out, "1234.50")

	def test_fmt_amount_unparseable_string_renders_em_dash(self) -> None:
		self.assertEqual(fmt_amount("not-a-number"), "—")
		self.assertEqual(self.calls["fmt_money"], [])
