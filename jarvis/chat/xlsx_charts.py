"""Charts of an .xlsx for the file preview panel.

openpyxl never loads charts from an existing workbook, so ``read_file`` (values
only) cannot show them. This reads the DrawingML chart parts straight from the
zip and returns them as ``jarvis-chart`` specs the chat's chart component
already renders. Preview only: nothing here feeds the agent's ``read_file``.
"""

from __future__ import annotations

import datetime
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_CHARTS = 10
MAX_POINTS = 500
_MAX_PART_BYTES = 2_000_000

_NS = {
	"c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
	"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
	"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
	"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
	"rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_R_ID = "{%s}id" % _NS["r"]
# DrawingML plot element -> (jarvis-chart type, bar direction aware)
_PLOTS = {
	"barChart": "bar",
	"lineChart": "line",
	"areaChart": "area",
	"pieChart": "pie",
	"doughnutChart": "donut",
}
_ISO_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})T00:00:00$")


def preview_cell(v):
	"""Preview-grid text for an ISO date(time) string from read_file; others unchanged."""
	if isinstance(v, str) and len(v) >= 19 and v[10:11] == "T":
		m = _ISO_MIDNIGHT.match(v)
		if m:
			return m.group(1)
		try:
			return datetime.datetime.fromisoformat(v).strftime("%Y-%m-%d %H:%M")
		except ValueError:
			return v
	return v


def extract_charts(content: bytes) -> list[dict]:
	"""``[{sheet, title, type, x, series:[{name, data}], options}]``; never raises."""
	try:
		with zipfile.ZipFile(io.BytesIO(content)) as zf:
			parsed = _charts_by_sheet(zf)
			if not parsed:
				return []
			values = _SheetValues(content)
			out = []
			for sheet, chart_xml in parsed:
				spec = _chart_spec(chart_xml, values)
				if spec:
					out.append({"sheet": sheet, **spec})
				if len(out) >= MAX_CHARTS:
					break
			return out
	except Exception:
		return []


def _xml(zf, part):
	"""Parsed XML of a zip member, or None. DOCTYPE/ENTITY parts are refused
	(entity-expansion bombs in an untrusted upload)."""
	try:
		info = zf.getinfo(part)
		if info.file_size > _MAX_PART_BYTES:
			return None
		data = zf.read(part)
	except KeyError:
		return None
	if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
		return None
	try:
		return ET.fromstring(data)
	except ET.ParseError:
		return None


def _rels(zf, part):
	"""{rId: absolute part path} from the .rels file of ``part``."""
	folder, name = posixpath.split(part)
	root = _xml(zf, posixpath.join(folder, "_rels", name + ".rels"))
	out = {}
	for rel in root if root is not None else []:
		target = rel.get("Target") or ""
		path = target[1:] if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
		out[rel.get("Id")] = path
	return out


def _charts_by_sheet(zf):
	"""[(sheet name, chart XML root)] in sheet then drawing order."""
	book = _xml(zf, "xl/workbook.xml")
	if book is None:
		return []
	book_rels = _rels(zf, "xl/workbook.xml")
	found = []
	for sh in book.findall("s:sheets/s:sheet", _NS):
		sheet_part = book_rels.get(sh.get(_R_ID))
		sheet = _xml(zf, sheet_part) if sheet_part else None
		if sheet is None:
			continue
		sheet_rels = _rels(zf, sheet_part)
		for d in sheet.findall("s:drawing", _NS):
			drawing_part = sheet_rels.get(d.get(_R_ID))
			drawing = _xml(zf, drawing_part) if drawing_part else None
			if drawing is None:
				continue
			drawing_rels = _rels(zf, drawing_part)
			for ref in drawing.iter("{%s}chart" % _NS["c"]):
				chart = _xml(zf, drawing_rels.get(ref.get(_R_ID), ""))
				if chart is not None:
					found.append((sh.get("name"), chart))
	return found


class _SheetValues:
	"""Lazy cell-range reads against the workbook's cached values."""

	def __init__(self, content):
		self._content = content
		self._wb = None

	def read(self, ref):
		"""Flat list of the cells in ``'Sheet'!$A$1:$A$9``, or None if unusable."""
		from openpyxl.utils.cell import range_boundaries

		m = re.match(r"^(?:'((?:[^']|'')+)'|([^'!]+))!(\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)$", ref.strip())
		if not m:
			return None
		sheet = (m.group(1) or m.group(2)).replace("''", "'")
		min_col, min_row, max_col, max_row = range_boundaries(m.group(3).replace("$", ""))
		if min_col != max_col and min_row != max_row:
			return None
		max_row = min(max_row, min_row + MAX_POINTS - 1)
		if self._wb is None:
			import openpyxl

			self._wb = openpyxl.load_workbook(io.BytesIO(self._content), read_only=True, data_only=True)
		if sheet not in self._wb.sheetnames:
			return None
		rows = self._wb[sheet].iter_rows(
			min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col, values_only=True
		)
		return [c for row in rows for c in row]


def _cache(node):
	"""Cached points under a c:strRef / c:numRef as a list."""
	pts = {}
	for pt in node.findall(".//c:pt", _NS):
		v = pt.find("c:v", _NS)
		if v is not None and pt.get("idx", "").isdigit():
			pts[int(pt.get("idx"))] = v.text
	count = node.find(".//c:ptCount", _NS)
	size = int(count.get("val")) if count is not None and count.get("val", "").isdigit() else 0
	return [pts.get(i) for i in range(max(size, max(pts, default=-1) + 1))]


def _points(node, values):
	"""Cell values of a series reference (live cells, else the cache); [] if none."""
	if node is None:
		return []
	f = node.find(".//c:f", _NS)
	cells = None
	if f is not None and f.text:
		try:
			cells = values.read(f.text)
		except Exception:
			cells = None
	if cells is None or all(c is None for c in cells):
		cells = _cache(node)
	return cells[:MAX_POINTS]


def _label(v):
	if isinstance(v, datetime.datetime):
		return v.strftime("%Y-%m-%d" if not (v.hour or v.minute or v.second) else "%Y-%m-%d %H:%M")
	if isinstance(v, datetime.date):
		return v.isoformat()
	if isinstance(v, float) and v.is_integer():
		return str(int(v))
	return "" if v is None else str(v)


def _number(v):
	try:
		return None if v is None or isinstance(v, bool) else float(v)
	except (TypeError, ValueError):
		return None


def _title(node):
	title = node.find("c:title", _NS)
	if title is None:
		return None
	return "".join(t.text or "" for t in title.iter("{%s}t" % _NS["a"])).strip() or None


def _chart_spec(chart, values):
	plot = chart.find("c:chart/c:plotArea", _NS)
	kind = next((el for el in plot if el.tag.split("}")[-1] in _PLOTS), None) if plot is not None else None
	if kind is None:
		return None
	name = kind.tag.split("}")[-1]
	options = {}
	bar_dir = kind.find("c:barDir", _NS)
	if bar_dir is not None and bar_dir.get("val") == "bar":
		options["horizontal"] = True
	grouping = kind.find("c:grouping", _NS)
	if grouping is not None and grouping.get("val") in ("stacked", "percentStacked"):
		options["stacked"] = True

	x, series = [], []
	for i, ser in enumerate(kind.findall("c:ser", _NS)):
		if not x:
			x = [_label(v) for v in _points(ser.find("c:cat", _NS), values)]
		tx = ser.find("c:tx", _NS)
		literal = tx.find("c:v", _NS) if tx is not None else None
		name_pts = [literal.text] if literal is not None else _points(tx, values)
		data = [_number(v) for v in _points(ser.find("c:val", _NS), values)]
		series.append({"name": _label(name_pts[0]) if name_pts else f"Series {i + 1}", "data": data})
	if not series or not any(v is not None for s in series for v in s["data"]):
		return None
	if not x:
		x = [str(i + 1) for i in range(len(series[0]["data"]))]
	spec = {"title": _title(chart.find("c:chart", _NS)), "type": _PLOTS[name], "x": x, "series": series}
	spec["options"] = options
	return spec
