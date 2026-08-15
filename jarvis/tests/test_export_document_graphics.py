"""Tests for ``jarvis.tools._export.document.graphics`` (Slice 1: CSS charts,
KPI/RAG components, and locale-correct number formatting - no image deps).

Two groups:

* ``css_bar``/``kpi_tile``/``rag_chip`` are pure (no ``frappe`` import) and run
  WITHOUT a site: ``python -m pytest jarvis/tests/test_export_document_graphics.py``.
* ``fmt_amount``/``fmt_pct`` reuse ``frappe.utils.fmt_money`` /
  ``frappe.utils.data.get_number_format``, both of which read
  ``frappe.local.lang`` - they raise ``AttributeError: lang`` outside a real
  Frappe site/request context. The real-formatting tests below are marked
  ``skip`` with that reason (verified against this repo's bench-env python,
  see the task report) and belong to the end-to-end/site gate instead. The
  edge-case GUARD logic (None/NaN/Inf/-0.0 handling, before ``fmt_money`` is
  ever reached) is still fully unit-tested here by monkeypatching
  ``fmt_money``/``get_number_format`` to site-free stubs.
"""

import math

import pytest

from jarvis.tools._export.document import graphics
from jarvis.tools._export.document.graphics import css_bar, fmt_amount, fmt_pct, kpi_tile, rag_chip

# --- css_bar --------------------------------------------------------------


def test_css_bar_wraps_in_bar_chart_class() -> None:
	out = css_bar([{"label": "Q1", "value": "10", "pct": 50}])
	assert out.startswith('<div class="bar-chart">')
	assert out.endswith("</div>")


def test_css_bar_bar_class_and_width() -> None:
	out = css_bar([{"label": "Q1", "value": "10", "pct": 50}])
	assert '<div class="bar" style="width:50%">Q1 10</div>' in out


def test_css_bar_width_clamps_low() -> None:
	out = css_bar([{"label": "x", "value": "y", "pct": -5}])
	assert 'style="width:0%"' in out


def test_css_bar_width_clamps_high() -> None:
	out = css_bar([{"label": "x", "value": "y", "pct": 150}])
	assert 'style="width:100%"' in out


def test_css_bar_width_clamps_nan_to_zero() -> None:
	out = css_bar([{"label": "x", "value": "y", "pct": float("nan")}])
	assert 'style="width:0%"' in out


def test_css_bar_width_clamps_inf_to_hundred() -> None:
	out = css_bar([{"label": "x", "value": "y", "pct": float("inf")}])
	assert 'style="width:100%"' in out


def test_css_bar_width_non_numeric_clamps_to_zero() -> None:
	out = css_bar([{"label": "x", "value": "y", "pct": "garbage"}])
	assert 'style="width:0%"' in out


def test_css_bar_escapes_script_label() -> None:
	out = css_bar([{"label": "<script>alert(1)</script>", "value": "1", "pct": 10}])
	assert "<script" not in out
	assert "&lt;script&gt;" in out


def test_css_bar_escapes_quote_in_value() -> None:
	out = css_bar([{"label": "x", "value": '10" onmouseover="x()', "pct": 10}])
	assert 'onmouseover="x()"' not in out
	assert "&quot;" in out


def test_css_bar_accepts_positional_tuple_rows() -> None:
	out = css_bar([("Q1", "10", 50)])
	assert '<div class="bar" style="width:50%">Q1 10</div>' in out


def test_css_bar_multiple_rows() -> None:
	out = css_bar([{"label": "A", "value": "1", "pct": 10}, {"label": "B", "value": "2", "pct": 90}])
	assert out.count('<div class="bar"') == 2


def test_css_bar_empty_rows() -> None:
	assert css_bar([]) == '<div class="bar-chart"></div>'


def test_css_bar_missing_keys_default_safely() -> None:
	out = css_bar([{}])
	assert 'style="width:0%"' in out


# --- kpi_tile ---------------------------------------------------------------


def test_kpi_tile_class_name() -> None:
	out = kpi_tile("Revenue", "1,000")
	assert '<div class="kpi-tile">' in out


def test_kpi_tile_label_and_value_present() -> None:
	out = kpi_tile("Revenue", "1,000")
	assert "Revenue" in out
	assert "1,000" in out


def test_kpi_tile_without_delta_omits_delta_div() -> None:
	out = kpi_tile("Revenue", "1,000")
	assert "kpi-delta" not in out


def test_kpi_tile_with_delta() -> None:
	out = kpi_tile("Revenue", "1,000", delta="+5%")
	assert '<div class="kpi-delta">+5%</div>' in out


def test_kpi_tile_escapes_script_in_label() -> None:
	out = kpi_tile("<script>alert(1)</script>", "1")
	assert "<script" not in out
	assert "&lt;script&gt;" in out


def test_kpi_tile_escapes_quote_in_value() -> None:
	out = kpi_tile("x", '1" onmouseover="x()')
	assert 'onmouseover="x()"' not in out
	assert "&quot;" in out


def test_kpi_tile_escapes_delta() -> None:
	out = kpi_tile("x", "1", delta="<script>bad()</script>")
	assert "<script" not in out


# --- rag_chip -----------------------------------------------------------


@pytest.mark.parametrize(
	"status,expected_class",
	[("red", "rag-red"), ("amber", "rag-amber"), ("green", "rag-green")],
)
def test_rag_chip_class_names(status: str, expected_class: str) -> None:
	out = rag_chip(status)
	assert f'class="{expected_class}"' in out


def test_rag_chip_case_insensitive() -> None:
	out = rag_chip("GREEN")
	assert 'class="rag-green"' in out


def test_rag_chip_invalid_status_raises() -> None:
	with pytest.raises(ValueError):
		rag_chip("blue")


def test_rag_chip_preserves_original_casing_as_escaped_text() -> None:
	out = rag_chip("Red")
	assert out == '<span class="rag-red">Red</span>'


# --- fmt_amount / fmt_pct: real integration (needs a Frappe site) -----------
#
# Confirmed against this repo's bench-env python (see task report): calling
# frappe.utils.fmt_money / frappe.utils.data.get_number_format outside a site
# raises `AttributeError: lang` (frappe.local.lang is unset). These are the
# true, unstubbed formatting behaviors and belong in the site-backed
# end-to-end gate, not in this no-site suite.


@pytest.mark.skip(reason="needs bench site — runs in the end-to-end gate")
def test_fmt_amount_real_lakh_crore_grouping() -> None:
	# Indian lakh/crore grouping is a site-level number-format setting; this
	# proves fmt_amount reuses frappe.utils.fmt_money rather than hand-rolling
	# digit grouping, once a real site/number-format is available.
	assert fmt_amount(1234567) == "12,34,567.00"  # depends on site's #,##,###.## format


@pytest.mark.skip(reason="needs bench site — runs in the end-to-end gate")
def test_fmt_pct_real_precision() -> None:
	assert fmt_pct(42.5) == "42.50%"


# --- fmt_amount / fmt_pct: edge-case guard logic (no site needed) -----------
#
# fmt_money/get_number_format are monkeypatched to site-free stubs so ONLY the
# wrapper's own None/NaN/Inf/-0.0/negative-parens guard logic is under test.
# For the None/NaN/Inf cases the guard returns before ever calling the stub
# (proven below by asserting the stub was not invoked), so those specific
# assertions do not even depend on the monkeypatch being correct.


@pytest.fixture
def stub_formatters(monkeypatch: pytest.MonkeyPatch) -> dict:
	calls = {"fmt_money": [], "get_number_format": 0}

	def _fmt_money_stub(amount, precision=None, currency=None, format=None):
		calls["fmt_money"].append((amount, precision, currency))
		return f"{amount:.2f}"

	class _NF:
		precision = 2

	def _get_number_format_stub():
		calls["get_number_format"] += 1
		return _NF()

	monkeypatch.setattr(graphics, "fmt_money", _fmt_money_stub)
	monkeypatch.setattr(graphics, "get_number_format", _get_number_format_stub)
	return calls


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
def test_fmt_amount_none_nan_inf_render_em_dash(bad, stub_formatters: dict) -> None:
	assert fmt_amount(bad) == "—"
	assert stub_formatters["fmt_money"] == []  # never reaches the (stubbed) formatter


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf")])
def test_fmt_pct_none_nan_inf_render_em_dash(bad, stub_formatters: dict) -> None:
	assert fmt_pct(bad) == "—"
	assert stub_formatters["fmt_money"] == []
	assert stub_formatters["get_number_format"] == 0


def test_fmt_amount_negative_zero_is_not_negative(stub_formatters: dict) -> None:
	out = fmt_amount(-0.0)
	assert out != "—"
	assert "neg" not in out
	# the stub received a non-negative magnitude, never a bare "-0.00"
	((amount, _precision, _currency),) = stub_formatters["fmt_money"]
	assert amount == 0.0
	assert math.copysign(1, amount) == 1.0


def test_fmt_pct_negative_zero_is_not_negative(stub_formatters: dict) -> None:
	out = fmt_pct(-0.0)
	assert out != "—"
	assert "neg" not in out


def test_fmt_amount_negative_wraps_in_parens_and_neg_class(stub_formatters: dict) -> None:
	out = fmt_amount(-1234.5)
	assert out == '<span class="neg">(1234.50)</span>'


def test_fmt_amount_positive_has_no_neg_class(stub_formatters: dict) -> None:
	out = fmt_amount(1234.5)
	assert "neg" not in out
	assert out == "1234.50"


def test_fmt_amount_passes_abs_value_to_formatter(stub_formatters: dict) -> None:
	fmt_amount(-1234.5)
	((amount, _precision, _currency),) = stub_formatters["fmt_money"]
	assert amount == 1234.5


def test_fmt_amount_passes_currency_through(stub_formatters: dict) -> None:
	fmt_amount(10, currency="INR")
	((_amount, _precision, currency),) = stub_formatters["fmt_money"]
	assert currency == "INR"


def test_fmt_pct_negative_wraps_in_parens_and_neg_class(stub_formatters: dict) -> None:
	out = fmt_pct(-42.5)
	assert out == '<span class="neg">(42.50%)</span>'


def test_fmt_pct_positive_has_percent_suffix_no_neg_class(stub_formatters: dict) -> None:
	out = fmt_pct(42.5)
	assert out == "42.50%"
	assert "neg" not in out


def test_fmt_pct_uses_number_format_precision_not_currency_default(stub_formatters: dict) -> None:
	fmt_pct(42.5)
	assert stub_formatters["get_number_format"] == 1
	((_amount, precision, _currency),) = stub_formatters["fmt_money"]
	assert precision == 2  # from the stub NumberFormat, not a currency-precision default


def test_fmt_amount_string_input_with_commas(stub_formatters: dict) -> None:
	out = fmt_amount("1,234.5")
	assert out == "1234.50"


def test_fmt_amount_unparseable_string_renders_em_dash(stub_formatters: dict) -> None:
	assert fmt_amount("not-a-number") == "—"
	assert stub_formatters["fmt_money"] == []
