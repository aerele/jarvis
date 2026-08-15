import csv as _csv
import io

from jarvis import compat
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


def csv(model: ExportModel) -> bytes:
	"""Plain CSV: escaped header + escaped rows, UTF-8. An empty rowset yields a
	valid header-only file (an honest 'no rows' export, not an error)."""
	buf = io.StringIO()
	w = _csv.writer(buf, quoting=_csv.QUOTE_MINIMAL)
	w.writerow([escape_formula(c) for c in model.columns])
	for row in model.rows:
		w.writerow([escape_formula(c) for c in row])
	return buf.getvalue().encode("utf-8")


def xlsx(model: ExportModel) -> bytes:
	"""Styled workbook via the shared xlsx_bytes builder (bold header + date/currency
	number formats from Frappe's make_xlsx). Header AND cells are formula-escaped;
	over-long cells are clipped-with-marker and flagged in ``model.meta``. Empty
	rowset -> valid header-only workbook."""
	header = [escape_formula(c) for c in model.columns]
	body = [[_clip(escape_formula(c), model) for c in row] for row in model.rows]
	title = (model.meta.get("title") or "Export")[:31]
	return compat.xlsx_bytes([(title, [header] + body)])
