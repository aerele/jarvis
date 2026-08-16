"""CSS charts, KPI/RAG components, and locale-correct number formatting for the
rich-PDF document engine.

This is the CSS/number half of the module (Slice 1 - zero new dependencies). A
later task adds the image half (matplotlib charts, QR, logo inlining) alongside
these functions; nothing here should assume or import an imaging library.

``css_bar``/``kpi_tile``/``rag_chip`` are pure builders: no inline ``style``
beyond a single numeric ``width:N%`` on a chart bar (a clamped float can never
carry markup), and every text field is escaped with the stdlib ``html.escape``.
They import nothing from ``frappe`` and are testable without a Frappe site.
Class names (``bar-chart``/``bar``/``kpi-tile``/``rag-red|amber|green``/``neg``)
match Task 2's ``THEME_CSS`` contract - keep them in lockstep if either side
changes.

``fmt_amount``/``fmt_pct`` DO need a Frappe site: they reuse
``frappe.utils.fmt_money``/``frappe.utils.data.get_number_format`` for
locale-correct digit grouping (Indian lakh/crore included) rather than
hand-rolling grouping logic. Both guard ``None``/``NaN``/``Inf`` explicitly
ahead of ``fmt_money`` - a naive ``"%.2f" % float("nan")`` renders the literal
string ``"nan"``, which must never reach a document - and treat ``-0.0`` as a
non-negative zero. Negative amounts render in parentheses wrapped in the
``.neg`` class hook from Task 2's conditional-table contract. NOTE for callers:
the returned string for a negative value is itself a small HTML fragment (a
``<span class="neg">...</span>``), meant for direct embedding (e.g. into a
table cell) - do NOT route it back through ``css_bar``/``kpi_tile``'s ``value``
escaping, which would double-escape the span tag.
"""

import html
import math
from collections.abc import Mapping, Sequence

from frappe.utils import fmt_money
from frappe.utils.data import get_number_format

_RAG_STATUSES = ("red", "amber", "green")


def css_bar(rows: Sequence[Mapping[str, object] | Sequence[object]]) -> str:
	"""Render an inert CSS bar chart: a ``.bar-chart`` wrapper holding one ``.bar``
	per row, per Task 2's contract classes.

	Each row supplies ``label``, ``value`` (rendered as escaped text, joined as
	``"{label} {value}"``) and ``pct`` (the bar's width, clamped to 0-100 - the
	only number that reaches the inline ``style`` attribute). A row may be a
	mapping with those three keys, or a positional ``(label, value, pct)``
	sequence.
	"""
	bars = []
	for row in rows:
		label, value, pct = _row_parts(row)
		width = _clamp_pct(pct)
		text = f"{_esc(label)} {_esc(value)}"
		bars.append(f'<div class="bar" style="width:{width}%">{text}</div>')
	return '<div class="bar-chart">' + "".join(bars) + "</div>"


def kpi_tile(label: str, value: str, delta: str | None = None) -> str:
	"""Render a ``.kpi-tile``: a labeled headline number with an optional delta
	line. All fields are escaped text content. Callers wanting delta styling
	(e.g. red for a negative change) format the delta string themselves before
	calling this - ``fmt_amount``/``fmt_pct`` already emit the ``.neg`` hook for
	negatives, but that hook is HTML and must be embedded directly, not passed
	through this function's escaping (see module docstring).
	"""
	parts = [
		'<div class="kpi-tile">',
		f'<div class="kpi-label">{_esc(label)}</div>',
		f'<div class="kpi-value">{_esc(value)}</div>',
	]
	if delta is not None:
		parts.append(f'<div class="kpi-delta">{_esc(delta)}</div>')
	parts.append("</div>")
	return "".join(parts)


def rag_chip(status: str) -> str:
	"""Render a ``.rag-{red,amber,green}`` status chip.

	``status`` must be exactly one of red/amber/green (case-insensitive). This is
	a tool-side builder fed by code that already computed the RAG bucket from
	real thresholds, not agent free text, so an unrecognized value fails loud
	rather than silently defaulting to some color (Task 2's caveat: a fabricated
	RAG status is a trust-signaling risk, so the builder should never invent one
	on bad input).
	"""
	normalized = str(status).strip().lower()
	if normalized not in _RAG_STATUSES:
		raise ValueError(f"rag_chip: status must be one of {_RAG_STATUSES}, got {status!r}")
	return f'<span class="rag-{normalized}">{html.escape(str(status))}</span>'


def fmt_amount(value: float | int | str | None, currency: str | None = None) -> str:
	"""Format ``value`` as a locale-correct money amount via ``fmt_money``
	(Indian lakh/crore grouping when the site's number format calls for it - do
	NOT hand-roll digit grouping).

	``None``, ``NaN``, and ``Inf``/``-Inf`` all render as ``"—"`` rather than
	being handed to ``fmt_money`` (which would render the literal "nan"/"inf").
	``-0.0`` is a non-negative zero. Negative amounts render in parentheses
	wrapped in a ``.neg`` class hook, so a caller dropping the string straight
	into a ``<td>`` gets negative styling for free.
	"""
	num = _to_float_or_none(value)
	if num is None:
		return "—"
	formatted = html.escape(fmt_money(abs(num), currency=currency))
	return f'<span class="neg">({formatted})</span>' if num < 0 else formatted


def fmt_pct(value: float | int | str | None) -> str:
	"""Format ``value`` - already a percent number, e.g. ``42.5`` -> ``"42.5%"``
	- via ``fmt_money`` at the site's own number-format precision.

	Explicitly passes ``get_number_format().precision`` rather than letting
	``fmt_money`` fall back to the system currency-precision default: a percent
	has nothing to do with currency precision, so decoupling keeps the decimal
	count locale-correct without accidentally inheriting a currency setting.

	Same edge-case handling as ``fmt_amount``: ``None``/``NaN``/``Inf`` -> "—",
	``-0.0`` is non-negative, negatives get parentheses + the ``.neg`` hook.
	"""
	num = _to_float_or_none(value)
	if num is None:
		return "—"
	precision = get_number_format().precision
	formatted = html.escape(fmt_money(abs(num), precision=precision))
	return f'<span class="neg">({formatted}%)</span>' if num < 0 else f"{formatted}%"


def _esc(value: object) -> str:
	"""Escape a text field for embedding, rendering ``None`` as empty rather than
	the literal string ``"None"`` (parity with ``fmt_amount``'s None handling)."""
	return "" if value is None else html.escape(str(value))


def _row_parts(row: Mapping[str, object] | Sequence[object]) -> tuple[object, object, object]:
	"""Pull ``(label, value, pct)`` out of a ``css_bar`` row - a mapping with
	those keys, or a positional 3-item sequence.

	The plugin schema types each row as ``Any``, so a runaway/confused model can
	send an int, ``None``, a string, or a wrong-length sequence. Rather than let a
	blind unpack raise ``TypeError``/``ValueError`` that escapes the tool as a raw
	500 (``_splice_charts`` catches this and degrades the chart to a note), reject
	a malformed row shape with a clear ``ValueError``."""
	if isinstance(row, Mapping):
		return row.get("label", ""), row.get("value", ""), row.get("pct", 0)
	if isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) == 3:
		return row[0], row[1], row[2]
	raise ValueError(
		f"css_bar row must be a mapping or a 3-item (label, value, pct) sequence, got {type(row).__name__}"
	)


def _clamp_pct(pct: object) -> str:
	"""Clamp a bar width to 0-100 and render it as a bare number for
	``width:N%``.

	Non-numeric or NaN input clamps to 0 (an empty bar, never a broken ``style``
	attribute); ``+Inf`` clamps to 100. Uses FIXED (never scientific) notation:
	``:g`` renders a tiny positive width like ``0.00001`` as ``1e-05``, which old
	WebKit rejects as an invalid ``width`` and drops - fixed formatting with the
	trailing zeros trimmed keeps it a plain number.
	"""
	try:
		num = float(pct)
	except (TypeError, ValueError):
		return "0"
	if math.isnan(num):
		return "0"
	num = max(0.0, min(100.0, num))
	return f"{num:.4f}".rstrip("0").rstrip(".") or "0"


def _to_float_or_none(value: object) -> float | None:
	"""Normalize a raw numeric-ish value ahead of Frappe's formatters.

	``None`` and any unparseable value collapse to ``None`` (rendered "—").
	``NaN``/``Inf``/``-Inf`` also collapse to ``None`` rather than being handed to
	``fmt_money``, which would render the literal string "nan"/"inf". ``-0.0``
	survives as a float whose sign Python's own ``< 0`` already reports False
	for, so it is never treated as negative.
	"""
	if value is None:
		return None
	if isinstance(value, str):
		value = value.replace(",", "").strip()
	try:
		num = float(value)
	except (TypeError, ValueError):
		return None
	if math.isnan(num) or math.isinf(num):
		return None
	return num
