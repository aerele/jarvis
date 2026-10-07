"""Excel charts and date cells in the file preview panel (#627)."""

from __future__ import annotations

import datetime
import io
import unittest
import zipfile
from unittest import mock

import openpyxl
from openpyxl.chart import AreaChart, BarChart, LineChart, PieChart, Reference, ScatterChart

from jarvis.chat import xlsx_charts
from jarvis.chat.xlsx_charts import (
	MAX_CHARTS_PER_SHEET,
	MAX_CHARTS_TOTAL,
	MAX_PARTS_TOTAL,
	MAX_POINTS,
	MAX_SERIES,
	MAX_SHEETS,
	extract_charts,
	preview_cell,
)


def _book(title="Data", rows=None):
	wb = openpyxl.Workbook()
	ws = wb.active
	ws.title = title
	for r in rows or [
		("Customer", "Outstanding", "Paid"),
		("Acme", 100, 5),
		("Zed", 250.5, 7),
		("Bob", 40, 9),
	]:
		ws.append(r)
	return wb, ws


def _add(ws, chart, value_cols=(2,), anchor="F2", title=None):
	for col in value_cols:
		chart.add_data(Reference(ws, min_col=col, min_row=1, max_row=ws.max_row), titles_from_data=True)
	chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
	if title:
		chart.title = title
	ws.add_chart(chart, anchor)


def _bytes(wb):
	buf = io.BytesIO()
	wb.save(buf)
	return buf.getvalue()


class TestExtractCharts(unittest.TestCase):
	def test_column_chart(self):
		wb, ws = _book()
		chart = BarChart()
		chart.type = "col"
		_add(ws, chart, title="Outstanding by customer")
		(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(spec["sheet"], "Data")
		self.assertEqual(spec["type"], "bar")
		self.assertEqual(spec["title"], "Outstanding by customer")
		self.assertEqual(spec["x"], ["Acme", "Zed", "Bob"])
		self.assertEqual(spec["series"], [{"name": "Outstanding", "data": [100.0, 250.5, 40.0]}])
		self.assertFalse(spec["options"].get("horizontal"))

	def test_horizontal_bar(self):
		wb, ws = _book()
		chart = BarChart()
		chart.type = "bar"
		_add(ws, chart, value_cols=(2, 3))
		(spec,) = extract_charts(_bytes(wb))
		self.assertTrue(spec["options"]["horizontal"])
		self.assertEqual([s["name"] for s in spec["series"]], ["Outstanding", "Paid"])

	def test_line_area_pie(self):
		for cls, expected in ((LineChart, "line"), (AreaChart, "area"), (PieChart, "pie")):
			wb, ws = _book()
			_add(ws, cls())
			(spec,) = extract_charts(_bytes(wb))
			self.assertEqual(spec["type"], expected)

	def test_sheet_name_with_spaces_and_quote(self):
		wb, ws = _book(title="Q1 O'Neil")
		_add(ws, BarChart())
		(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(spec["sheet"], "Q1 O'Neil")
		self.assertEqual(spec["series"][0]["data"], [100.0, 250.5, 40.0])

	def test_two_charts_on_two_sheets(self):
		wb, ws = _book()
		_add(ws, BarChart(), title="One")
		_add(ws, LineChart(), anchor="F20", title="Two")
		ws2 = wb.create_sheet("Other")
		ws2.append(("k", "v"))
		ws2.append(("a", 1))
		_add(ws2, PieChart(), title="Three")
		specs = extract_charts(_bytes(wb))
		self.assertEqual(
			[(s["sheet"], s["title"]) for s in specs], [("Data", "One"), ("Data", "Two"), ("Other", "Three")]
		)

	def test_no_charts(self):
		wb, _ = _book()
		self.assertEqual(extract_charts(_bytes(wb)), [])

	def test_not_a_zip(self):
		self.assertEqual(extract_charts(b"nope"), [])

	def test_malformed_chart_part_skipped(self):
		wb, ws = _book()
		_add(ws, BarChart(), title="Good")
		_add(ws, LineChart(), anchor="F20", title="Broken")
		src = _bytes(wb)
		out = io.BytesIO()
		with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
			for item in zin.infolist():
				data = zin.read(item.filename)
				if item.filename == "xl/charts/chart2.xml":
					data = b"<c:chartSpace><oops"
				zout.writestr(item, data)
		specs = extract_charts(out.getvalue())
		self.assertEqual([s["title"] for s in specs], ["Good"])

	def test_doctype_part_refused(self):
		wb, ws = _book()
		_add(ws, BarChart())
		src = _bytes(wb)
		out = io.BytesIO()
		with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
			for item in zin.infolist():
				data = zin.read(item.filename)
				if item.filename == "xl/charts/chart1.xml":
					data = b'<!DOCTYPE x [<!ENTITY a "b">]>' + data
				zout.writestr(item, data)
		self.assertEqual(extract_charts(out.getvalue()), [])

	def test_cache_fallback_when_cells_unavailable(self):
		wb, ws = _book()
		_add(ws, BarChart())
		src = _bytes(wb)
		out = io.BytesIO()
		cache = (
			'</f><numCache><ptCount val="2"/><pt idx="0"><v>7</v></pt><pt idx="1"><v>9</v></pt></numCache>'
		)
		with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
			for item in zin.infolist():
				data = zin.read(item.filename)
				if item.filename == "xl/charts/chart1.xml":
					text = data.decode().replace("'Data'!", "'Gone'!")
					text = text.replace("</f></numRef>", cache + "</numRef>")
					data = text.encode()
				zout.writestr(item, data)
		(spec,) = extract_charts(out.getvalue())
		self.assertEqual(spec["series"][0]["data"], [7.0, 9.0])

	def test_no_cells_and_no_cache_skips_chart(self):
		wb, ws = _book()
		_add(ws, BarChart())
		src = _bytes(wb)
		out = io.BytesIO()
		with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
			for item in zin.infolist():
				data = zin.read(item.filename)
				if item.filename == "xl/charts/chart1.xml":
					data = data.decode().replace("'Data'!", "'Gone'!").encode()
				zout.writestr(item, data)
		self.assertEqual(extract_charts(out.getvalue()), [])

	def test_bounds(self):
		rows = [("k", "v")] + [(f"r{i}", i) for i in range(MAX_POINTS + 100)]
		wb, ws = _book(rows=rows)
		_add(ws, BarChart())
		(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(len(spec["x"]), MAX_POINTS)
		self.assertEqual(len(spec["series"][0]["data"]), MAX_POINTS)
		wb, ws = _book()
		for i in range(MAX_CHARTS_PER_SHEET + 3):
			_add(ws, BarChart(), anchor=f"F{2 + i * 20}")
		self.assertEqual(len(extract_charts(_bytes(wb))), MAX_CHARTS_PER_SHEET)

	def test_date_categories(self):
		rows = [("Day", "v"), (datetime.datetime(2026, 6, 1), 1), (datetime.datetime(2026, 6, 2), 2)]
		wb, ws = _book(rows=rows)
		_add(ws, LineChart())
		(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(spec["x"], ["2026-06-01", "2026-06-02"])


def _rewrite(src, edits):
	"""Copy a workbook, applying {member: fn(bytes) -> bytes}."""
	out = io.BytesIO()
	with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
		for item in zin.infolist():
			data = zin.read(item.filename)
			if item.filename in edits:
				data = edits[item.filename](data)
			zout.writestr(item, data)
	return out.getvalue()


class TestHardening(unittest.TestCase):
	def _one(self):
		wb, ws = _book()
		_add(ws, BarChart())
		return _bytes(wb)

	def test_huge_ptcount_and_idx_are_not_allocated(self):
		cache = '</f><numCache><ptCount val="999999999"/><pt idx="0"><v>7</v></pt><pt idx="4000000000"><v>1</v></pt></numCache>'
		src = _rewrite(
			self._one(),
			{
				"xl/charts/chart1.xml": lambda d: d.decode()
				.replace("'Data'!", "'Gone'!")
				.replace("</f></numRef>", cache + "</numRef>")
				.encode()
			},
		)
		(spec,) = extract_charts(src)
		self.assertEqual(spec["series"][0]["data"], [7.0])

	def test_utf16_part_refused(self):
		src = _rewrite(
			self._one(),
			{
				"xl/charts/chart1.xml": lambda d: ('<!DOCTYPE x [<!ENTITY a "b">]>' + d.decode()).encode(
					"utf-16"
				)
			},
		)
		self.assertEqual(extract_charts(src), [])

	def test_non_utf8_declaration_refused(self):
		src = _rewrite(
			self._one(),
			{"xl/charts/chart1.xml": lambda d: b'<?xml version="1.0" encoding="UTF-16"?>' + d},
		)
		self.assertEqual(extract_charts(src), [])

	def test_wrong_root_refused(self):
		src = _rewrite(self._one(), {"xl/charts/chart1.xml": lambda d: b"<other/>"})
		self.assertEqual(extract_charts(src), [])

	def test_huge_worksheet_xml_is_not_parsed(self):
		src = _rewrite(self._one(), {"xl/worksheets/sheet1.xml": lambda d: d + b" " * 3_000_000})
		self.assertEqual(len(extract_charts(src)), 1)

	def test_per_sheet_cap_leaves_later_sheets(self):
		wb, ws = _book()
		for i in range(MAX_CHARTS_PER_SHEET + 2):
			_add(ws, BarChart(), anchor=f"F{2 + i * 20}")
		ws2 = wb.create_sheet("Later")
		ws2.append(("k", "v"))
		ws2.append(("a", 1))
		_add(ws2, PieChart(), title="Later chart")
		specs = extract_charts(_bytes(wb))
		self.assertEqual(len(specs), MAX_CHARTS_PER_SHEET + 1)
		self.assertEqual(specs[-1]["sheet"], "Later")
		self.assertLessEqual(len(specs), MAX_CHARTS_TOTAL)

	def test_rels_target_outside_xl_ignored(self):
		src = _rewrite(
			self._one(),
			{
				"xl/drawings/_rels/drawing1.xml.rels": lambda d: d.replace(
					b"/xl/charts/chart1.xml", b"../../evil.xml"
				)
			},
		)
		self.assertEqual(extract_charts(src), [])


_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rels_xml(entries):
	body = "".join(f'<Relationship Id="{i}" Type="{_OD}/{t}" Target="{p}"/>' for i, t, p in entries)
	return f'<?xml version="1.0"?><Relationships xmlns="{_REL_NS}">{body}</Relationships>'.encode()


def _scatter_part():
	"""chart1.xml of a scatter chart: a type that never yields a spec."""
	wb, ws = _book()
	chart = ScatterChart()
	chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=4), titles_from_data=True)
	ws.add_chart(chart, "F2")
	with zipfile.ZipFile(io.BytesIO(_bytes(wb))) as z:
		return z.read("xl/charts/chart1.xml")


def _crafted(sheets, drawings_per_sheet, charts_per_drawing, distinct):
	"""A workbook of ``sheets`` sheets, each pointing at ``drawings_per_sheet``
	drawings that each reference ``charts_per_drawing`` scatter charts. With
	``distinct`` every sheet/drawing/chart is its own part, else all repeat one."""
	chart = _scatter_part()
	parts = {}
	wb_rels, sheet_tags = [], []
	for si in range(sheets):
		n = si + 1 if distinct else 1
		wb_rels.append((f"rId{si + 1}", "worksheet", f"worksheets/sheet{n}.xml"))
		sheet_tags.append(f'<sheet name="S{si}" sheetId="{si + 1}" r:id="rId{si + 1}"/>')
		parts[f"xl/worksheets/sheet{n}.xml"] = b"<worksheet/>"
		drawings = [f"/xl/drawings/drawing{n if distinct else 1}.xml"] * drawings_per_sheet
		parts[f"xl/worksheets/_rels/sheet{n}.xml.rels"] = _rels_xml(
			[(f"d{i}", "drawing", d) for i, d in enumerate(drawings)]
		)
		parts[f"xl/drawings/drawing{n if distinct else 1}.xml"] = (
			'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
			f'xmlns:c="{xlsx_charts._NS["c"]}" xmlns:r="{_OD}">'
			+ "".join(f'<c:chart r:id="c{i}"/>' for i in range(charts_per_drawing))
			+ "</xdr:wsDr>"
		).encode()
		parts[f"xl/drawings/_rels/drawing{n if distinct else 1}.xml.rels"] = _rels_xml(
			[
				(f"c{i}", "chart", f"/xl/charts/chart{n if distinct else 1}_{i if distinct else 0}.xml")
				for i in range(charts_per_drawing)
			]
		)
		for i in range(charts_per_drawing):
			parts[f"xl/charts/chart{n if distinct else 1}_{i if distinct else 0}.xml"] = chart
	parts["xl/workbook.xml"] = (
		f'<workbook xmlns="{xlsx_charts._NS["s"]}" xmlns:r="{_OD}"><sheets>{"".join(sheet_tags)}</sheets></workbook>'
	).encode()
	parts["xl/_rels/workbook.xml.rels"] = _rels_xml(wb_rels)
	out = io.BytesIO()
	with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
		for name, data in parts.items():
			z.writestr(name, data)
	return out.getvalue()


class _Counting:
	"""Count XML parses and chart-spec builds done for one extract_charts call."""

	def __enter__(self):
		self.parses = self.specs = 0
		real_parse, real_spec = xlsx_charts.ET.fromstring, xlsx_charts._chart_spec

		def parse(*a, **k):
			self.parses += 1
			return real_parse(*a, **k)

		def spec(*a, **k):
			self.specs += 1
			return real_spec(*a, **k)

		self._patches = [
			mock.patch.object(xlsx_charts.ET, "fromstring", parse),
			mock.patch.object(xlsx_charts, "_chart_spec", spec),
		]
		for p in self._patches:
			p.start()
		return self

	def __exit__(self, *exc):
		for p in self._patches:
			p.stop()


class _RowSpy:
	"""Count openpyxl read-only iter_rows calls and the rows they yield."""

	def __init__(self, fail=False):
		self.fail = fail

	def __enter__(self):
		from openpyxl.worksheet._read_only import ReadOnlyWorksheet

		self.calls = self.rows = 0
		real = ReadOnlyWorksheet.iter_rows
		spy = self

		def iter_rows(ws, *a, **k):
			spy.calls += 1
			if spy.fail:
				raise ValueError("broken sheet")
			for row in real(ws, *a, **k):
				spy.rows += 1
				yield row

		self._patch = mock.patch.object(ReadOnlyWorksheet, "iter_rows", iter_rows)
		self._patch.start()
		return self

	def __exit__(self, *exc):
		self._patch.stop()


class TestParseBudget(unittest.TestCase):
	def test_repeated_references_parse_each_part_once(self):
		src = _crafted(sheets=400, drawings_per_sheet=200, charts_per_drawing=10, distinct=False)
		with _Counting() as n:
			self.assertEqual(extract_charts(src), [])
		# workbook, its rels, sheet rels, drawing, drawing rels, chart
		self.assertLessEqual(n.parses, 6)
		self.assertEqual(n.specs, 1)

	def test_many_distinct_parts_stop_at_the_budget(self):
		src = _crafted(sheets=400, drawings_per_sheet=1, charts_per_drawing=10, distinct=True)
		with _Counting() as n:
			self.assertEqual(extract_charts(src), [])
		self.assertLessEqual(n.parses, MAX_PARTS_TOTAL)

	def test_budget_keeps_charts_found_before_it_ran_out(self):
		wb, ws = _book()
		_add(ws, BarChart(), title="First")
		good = _bytes(wb)
		with mock.patch.object(xlsx_charts, "MAX_PARTS_TOTAL", 5):
			specs = extract_charts(good)
		self.assertLessEqual(len(specs), 1)
		self.assertEqual(len(extract_charts(good)), 1)

	def test_series_and_range_reads_are_bounded(self):
		wb, ws = _book()
		ws.append(("x", 1, 1))
		chart = BarChart()
		for _ in range(100):
			chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=5), titles_from_data=True)
		chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=5))
		ws.add_chart(chart, "F2")
		real_load = openpyxl.load_workbook
		with mock.patch.object(openpyxl, "load_workbook", wraps=real_load) as load:
			(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(len(spec["series"]), MAX_SERIES)
		self.assertEqual(load.call_count, 1)

	def test_sheet_is_scanned_once_for_all_series(self):
		wb, ws = _book()
		chart = BarChart()
		for _ in range(5):
			chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=4), titles_from_data=True)
		chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=4))
		ws.add_chart(chart, "F2")
		with _RowSpy() as spy:
			(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(len(spec["series"]), 5)
		self.assertEqual(spy.calls, 1)
		self.assertEqual(spy.rows, 4)

	def test_failing_sheet_read_is_not_repeated(self):
		wb, ws = _book()
		chart = BarChart()
		for _ in range(10):
			chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=4), titles_from_data=True)
		ws.add_chart(chart, "F2")
		with _RowSpy(fail=True) as spy:
			self.assertEqual(extract_charts(_bytes(wb)), [])
		self.assertEqual(spy.calls, 1)

	def test_rows_scanned_budget_and_range_row_cap(self):
		wb = openpyxl.Workbook()
		for i in range(4):
			ws = wb.active if i == 0 else wb.create_sheet()
			ws.title = f"D{i}"
			for r in range(1, 3001):
				ws.append((f"c{r}", r))
			_add(ws, BarChart())
		with mock.patch.object(xlsx_charts, "MAX_ROWS_SCANNED", 4000), _RowSpy() as spy:
			specs = extract_charts(_bytes(wb))
		self.assertLessEqual(spy.rows, 4000)
		self.assertLess(len(specs), 4)
		ws = openpyxl.Workbook().active
		ws.append(("a", "b"))
		with mock.patch.object(xlsx_charts, "MAX_RANGE_ROW", 2), _RowSpy() as spy:
			values = xlsx_charts._SheetValues(_bytes(ws.parent))
			self.assertIsNone(values.read("Sheet!$A$3:$A$9"))
		self.assertEqual(spy.calls, 0)

	def test_cached_points_are_preferred_over_sheet_reads(self):
		wb, ws = _book()
		_add(ws, BarChart())
		src = _rewrite(
			_bytes(wb),
			{
				"xl/charts/chart1.xml": lambda d: d.replace(
					b"</f>",
					b'</f><numCache><formatCode>General</formatCode><ptCount val="2"/>'
					b'<pt idx="0"><v>7</v></pt><pt idx="1"><v>8</v></pt></numCache>',
				)
			},
		)
		with _RowSpy() as spy:
			(spec,) = extract_charts(src)
		self.assertEqual(spy.calls, 0)
		self.assertEqual(spec["series"][0]["data"], [7.0, 8.0])

	def test_sheet_cap(self):
		src = _crafted(sheets=MAX_SHEETS + 20, drawings_per_sheet=1, charts_per_drawing=1, distinct=True)
		with _Counting() as n:
			extract_charts(src)
		# per sheet: its rels, drawing, drawing rels, chart; plus workbook and its rels
		self.assertLessEqual(n.parses, 2 + 4 * MAX_SHEETS)

	def test_byte_budget_stops_parsing(self):
		src = _crafted(sheets=5, drawings_per_sheet=1, charts_per_drawing=3, distinct=True)
		with mock.patch.object(xlsx_charts, "MAX_BYTES_TOTAL", 3000), _Counting() as n:
			extract_charts(src)
		self.assertLess(n.parses, 10)
		with _Counting() as full:
			extract_charts(src)
		self.assertGreater(full.parses, n.parses)

	def test_charts_on_a_later_sheet_are_returned_with_their_sheet(self):
		wb, ws = _book("Summary")
		data = wb.create_sheet("Data")
		for row in [("k", "v"), ("a", 1), ("b", 2)]:
			data.append(row)
		_add(data, BarChart(), title="On the second sheet")
		(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(spec["sheet"], "Data")


class TestPreviewCell(unittest.TestCase):
	def test_dates(self):
		self.assertEqual(preview_cell("2026-06-01T00:00:00"), "2026-06-01")
		self.assertEqual(preview_cell("2026-06-01T14:30:05"), "2026-06-01 14:30")
		self.assertEqual(preview_cell("2026-06-01"), "2026-06-01")
		self.assertEqual(preview_cell("12:30:00"), "12:30:00")
		self.assertEqual(preview_cell("note T text here, long enough"), "note T text here, long enough")
		self.assertEqual(preview_cell(5), 5)
		self.assertEqual(preview_cell(""), "")
