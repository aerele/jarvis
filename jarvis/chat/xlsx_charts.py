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

MAX_CHARTS_PER_SHEET = 10
MAX_CHARTS_TOTAL = 30
MAX_RELS = 200
MAX_POINTS = 500
MAX_SERIES = 20
# Per-file budget, enforced before a part is read, so a crafted upload cannot make
# one preview parse thousands of parts or rescan sheets per series. Not covered:
# openpyxl's own loading of the workbook (shared strings, styles) when a series
# has no cached points and a sheet has to be read.
MAX_SHEETS = 50
MAX_PARTS_TOTAL = 150
MAX_BYTES_TOTAL = 10_000_000
MAX_RANGE_ROW = 5_000
MAX_COLS = 50
MAX_ROWS_SCANNED = 20_000
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
_XML_ENCODING = re.compile(rb'^\s*<\?xml[^>]*encoding=["\']([^"\']+)["\']')
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


class _OverBudget(Exception):
	"""The file used up its parse budget; keep what was found so far."""


class _Budget:
	"""Parts and bytes one file may cost; each zip path is parsed at most once."""

	def __init__(self):
		self.parts = 0
		self.size = 0
		self.seen = set()

	def take(self, part, size):
		"""True if ``part`` may be read now, False if already seen; raises when spent."""
		if part in self.seen:
			return False
		if self.parts >= MAX_PARTS_TOTAL or self.size + size > MAX_BYTES_TOTAL:
			raise _OverBudget
		self.seen.add(part)
		self.parts += 1
		self.size += size
		return True


def extract_charts(content: bytes) -> list[dict]:
	"""``[{sheet, title, type, x, series:[{name, data}], options}]``; never raises."""
	out = []
	try:
		with zipfile.ZipFile(io.BytesIO(content)) as zf:
			values = _SheetValues(content)
			per_sheet = {}
			try:
				for sheet, chart_xml in _charts_by_sheet(zf, _Budget()):
					if per_sheet.get(sheet, 0) >= MAX_CHARTS_PER_SHEET:
						continue
					spec = _chart_spec(chart_xml, values)
					if spec:
						out.append({"sheet": sheet, **spec})
						per_sheet[sheet] = per_sheet.get(sheet, 0) + 1
					if len(out) >= MAX_CHARTS_TOTAL:
						break
			except _OverBudget:
				pass
			return out
	except Exception:
		return out


def _xml(zf, part, root_tag, budget):
	"""Parsed XML of a zip member, or None. Only plain UTF-8/ASCII parts with the
	expected root and no DOCTYPE/ENTITY are parsed (entity-expansion bombs and
	encodings that would hide them in an untrusted upload)."""
	try:
		size = zf.getinfo(part).file_size
		if size > _MAX_PART_BYTES or not budget.take(part, size):
			return None
		data = zf.read(part)
	except KeyError:
		return None
	declared = _XML_ENCODING.search(data[:200])
	if data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:4] in (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00"):
		return None
	if declared and declared.group(1).lower() not in (b"utf-8", b"us-ascii"):
		return None
	try:
		text = data.decode("utf-8")
	except UnicodeDecodeError:
		return None
	if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
		return None
	try:
		root = ET.fromstring(data)
	except ET.ParseError:
		return None
	return root if root.tag.split("}")[-1] == root_tag else None


def _rels(zf, part, budget):
	"""[(rId, type suffix, absolute part path)] from the .rels file of ``part``;
	targets resolving outside ``xl/`` are dropped."""
	folder, name = posixpath.split(part)
	root = _xml(zf, posixpath.join(folder, "_rels", name + ".rels"), "Relationships", budget)
	out = []
	for rel in list(root)[:MAX_RELS] if root is not None else []:
		target = rel.get("Target") or ""
		path = posixpath.normpath(target[1:] if target.startswith("/") else posixpath.join(folder, target))
		if path.startswith("xl/") and ".." not in path.split("/"):
			out.append((rel.get("Id"), (rel.get("Type") or "").rsplit("/", 1)[-1], path))
	return out


def _charts_by_sheet(zf, budget):
	"""Yield (sheet name, chart XML root) in sheet then drawing order, lazily.

	Worksheet XML is never parsed (it can be huge): a sheet's drawings are the
	drawing relationships in its small .rels file."""
	book = _xml(zf, "xl/workbook.xml", "workbook", budget)
	if book is None:
		return
	book_rels = {rid: path for rid, _, path in _rels(zf, "xl/workbook.xml", budget)}
	for sh in book.findall("s:sheets/s:sheet", _NS)[:MAX_SHEETS]:
		sheet_part = book_rels.get(sh.get(_R_ID))
		if not sheet_part:
			continue
		for _, kind, drawing_part in _rels(zf, sheet_part, budget):
			if kind != "drawing":
				continue
			drawing = _xml(zf, drawing_part, "wsDr", budget)
			if drawing is None:
				continue
			drawing_rels = {rid: path for rid, _, path in _rels(zf, drawing_part, budget)}
			for ref in list(drawing.iter("{%s}chart" % _NS["c"]))[:MAX_CHARTS_PER_SHEET]:
				chart = _xml(zf, drawing_rels.get(ref.get(_R_ID), ""), "chartSpace", budget)
				if chart is not None:
					yield sh.get("name"), chart


class _SheetValues:
	"""Cell values for series that carry no cached points in the chart part.

	Each sheet is scanned once, from row 1 (read-only openpyxl must parse every
	row before the one it is asked for), keeping at most MAX_RANGE_ROW rows and
	MAX_COLS columns; ranges are then sliced from memory. Rows scanned are
	charged against one MAX_ROWS_SCANNED budget for the file."""

	def __init__(self, content):
		self._content = content
		self._wb = None
		self._grids = {}
		self.rows_scanned = 0

	def read(self, ref):
		"""Flat list of the cells in ``'Sheet'!$A$1:$A$9``, or None if unusable."""
		from openpyxl.utils.cell import range_boundaries

		m = re.match(r"^(?:'((?:[^']|'')+)'|([^'!]+))!(\$?[A-Z]+\$?\d+(?::\$?[A-Z]+\$?\d+)?)$", ref.strip())
		if not m:
			return None
		sheet = (m.group(1) or m.group(2)).replace("''", "'")
		min_col, min_row, max_col, max_row = range_boundaries(m.group(3).replace("$", ""))
		if (min_col != max_col and min_row != max_row) or min_row > MAX_RANGE_ROW or max_col > MAX_COLS:
			return None
		grid = self._grid(sheet)
		if grid is None:
			return None
		max_row = min(max_row, min_row + MAX_POINTS - 1, len(grid))
		return [c for row in grid[min_row - 1 : max_row] for c in row[min_col - 1 : max_col]]

	def _grid(self, sheet):
		"""The sheet's leading rows (scanned once, failures remembered as None)."""
		if sheet not in self._grids:
			self._grids[sheet] = self._scan(sheet)
		return self._grids[sheet]

	def _scan(self, sheet):
		try:
			if self._wb is None:
				import openpyxl

				self._wb = openpyxl.load_workbook(io.BytesIO(self._content), read_only=True, data_only=True)
			if sheet not in self._wb.sheetnames:
				return None
			grid = []
			if self.rows_scanned >= MAX_ROWS_SCANNED:
				return None
			rows = self._wb[sheet].iter_rows(max_row=MAX_RANGE_ROW, max_col=MAX_COLS, values_only=True)
			for row in rows:
				self.rows_scanned += 1
				grid.append(list(row))
				if self.rows_scanned >= MAX_ROWS_SCANNED:
					break
			return grid
		except Exception:
			return None


def _is_date_format(code):
	"""True for a number format with date parts (quoted text and [..] stripped)."""
	return bool(re.search(r"[ymd]", re.sub(r'"[^"]*"|\[[^\]]*\]', "", code or "").lower()))


def _cache(node):
	"""Cached points under a c:strRef / c:numRef as a list (ptCount/idx are untrusted).
	Numbers under a date format come back as dates, like the live cells would."""
	from openpyxl.utils.datetime import from_excel

	fmt = node.find(".//c:formatCode", _NS)
	dated = fmt is not None and _is_date_format(fmt.text)
	pts = {}
	for pt in node.findall(".//c:pt", _NS):
		v = pt.find("c:v", _NS)
		idx = pt.get("idx", "")
		if v is not None and idx.isdigit() and int(idx) < MAX_POINTS:
			value = v.text
			if dated:
				try:
					value = from_excel(float(value))
				except (TypeError, ValueError, OverflowError):
					pass
			pts[int(idx)] = value
	return [pts.get(i) for i in range(max(pts, default=-1) + 1)]


def _points(node, values):
	"""Points of a series reference: the chart's cached points when it has any,
	else the live cells; [] if none."""
	if node is None:
		return []
	cells = _cache(node)
	if all(c is None for c in cells):
		f = node.find(".//c:f", _NS)
		cells = (values.read(f.text) if f is not None and f.text else None) or []
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
	for i, ser in enumerate(kind.findall("c:ser", _NS)[:MAX_SERIES]):
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
