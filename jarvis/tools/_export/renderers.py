import csv as _csv
import io

from jarvis import compat
from jarvis._xlsx_highlights import validate_highlights
from jarvis.tools._export.model import ExportModel
from jarvis.tools._export.safety import escape_formula

# Excel hard-caps a single cell at 32,767 chars; make_xlsx silently drops the
# overflow (write() returns -2, unchecked). Truncate visibly and flag it.
_EXCEL_CELL_LIMIT = 32_767
_TRUNC_MARKER = "…[truncated]"


def _clip(value, model: ExportModel):
	if isinstance(value, str) and len(value) > _EXCEL_CELL_LIMIT:
		model.meta["cells_truncated"] = True
		return value[: _EXCEL_CELL_LIMIT - len(_TRUNC_MARKER)] + _TRUNC_MARKER
	return value


def _xlsx_cell(value, model: ExportModel, unwrap):
	"""Prepare one xlsx cell. make_xlsx runs ``handle_html`` on every string cell
	AFTER we'd escape, which would unwrap ``<p>=FORMULA</p>`` back into a live
	``=FORMULA`` past the guard (formula injection). Mirror that unwrap HERE first
	(``unwrap`` is ``handle_html``, imported once per render), so ``escape_formula``
	runs on the same string make_xlsx ultimately stores (its second handle_html is
	then a no-op - it returns input unchanged when there is no ``<``/``>``). CSV
	needs none of this: it is not run through handle_html, and a ``<``-leading cell
	is inert (not a formula) in a spreadsheet."""
	if isinstance(value, str):
		value = unwrap(value)
	return _clip(escape_formula(value), model)


def _headers(model: ExportModel) -> list:
	"""The header row: the resolver's labels ("Supplier Group"), else the column keys."""
	headers = model.meta.get("headers")
	return list(headers) if headers and len(headers) == len(model.columns) else list(model.columns)


def _rules_by_header(highlights, model: ExportModel, headers: list):
	"""Highlight rules match the header row. A rule may name its column by the field
	name the caller asked for (``supplier_group``) or by its label; a field name is
	read as that column's label here."""
	if not isinstance(highlights, list):
		return highlights
	by_key = {k: h for k, h in zip(model.columns, headers, strict=True) if k != h}
	out = []
	for rule in highlights:
		if isinstance(rule, dict) and rule.get("column") in by_key and rule["column"] not in headers:
			rule = {**rule, "column": by_key[rule["column"]]}
		out.append(rule)
	return out


def csv(model: ExportModel) -> bytes:
	"""Plain CSV: escaped header + escaped rows, UTF-8. An empty rowset yields a
	valid header-only file (an honest 'no rows' export, not an error)."""
	buf = io.StringIO()
	w = _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL)
	w.writerow([escape_formula(c) for c in _headers(model)])
	for row in model.rows:
		w.writerow([escape_formula(c) for c in row])
	return buf.getvalue().encode("utf-8")


def xlsx(model: ExportModel, *, highlights=None) -> bytes:
	"""Styled workbook via the shared xlsx_bytes builder (bold header + date/currency
	number formats from Frappe's make_xlsx). Header AND cells are formula-escaped;
	over-long cells are clipped-with-marker and flagged in ``model.meta``; HTML is
	unwrapped before escaping so a formula hidden behind tags can't slip the guard.
	Empty rowset -> valid header-only workbook."""
	# Imported lazily (once per render, not per cell): xlsxutils pulls xlsxwriter,
	# which compat.py deliberately keeps out of module import for Frappe 15.
	from frappe.utils.xlsxutils import ILLEGAL_CHARACTERS_RE, handle_html

	labels = _headers(model)
	header = [_xlsx_cell(c, model, handle_html) for c in labels]
	# Writer sanitation also strips XML control characters. Match those headers
	# before resolving highlight columns, so a rule cannot target an alias that
	# becomes ambiguous only when the workbook is written.
	header = [ILLEGAL_CHARACTERS_RE.sub("", c) if isinstance(c, str) else c for c in header]
	body = [[_xlsx_cell(c, model, handle_html) for c in row] for row in model.rows]
	title = (model.meta.get("title") or "Export")[:31]
	data = [header] + body
	rules = validate_highlights(_rules_by_header(highlights, model, labels), data)
	model.meta["highlight_count"] = len(rules)
	return compat.xlsx_bytes([(title, data)], highlights=[rules])
