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


def _fill_caches(wb):
	"""Write numCache/strCache into every chart from the cells, as Excel does."""
	from openpyxl.chart.data_source import AxDataSource, NumData, NumVal, StrData, StrRef, StrVal
	from openpyxl.utils.datetime import to_excel

	def cells(ws, ref):
		got = ws[ref.split("!")[1].replace("$", "")]
		if not isinstance(got, tuple):
			return [got]
		return [c for row in got for c in (row if isinstance(row, tuple) else (row,))]

	for ws in wb.worksheets:
		for chart in ws._charts:
			for ser in chart.series:
				if ser.tx and ser.tx.strRef:
					(c,) = cells(ws, ser.tx.strRef.f)
					ser.tx.strRef.strCache = StrData(ptCount=1, pt=[StrVal(idx=0, v=str(c.value))])
				if ser.val and ser.val.numRef:
					cs = cells(ws, ser.val.numRef.f)
					pts = [
						NumVal(idx=i, v=c.value)
						for i, c in enumerate(cs)
						if isinstance(c.value, (int, float))
					]
					ser.val.numRef.numCache = NumData(formatCode="General", ptCount=len(cs), pt=pts)
				if ser.cat is not None and ser.cat.numRef:
					cs = cells(ws, ser.cat.numRef.f)
					if cs and isinstance(cs[0].value, (datetime.date, datetime.datetime)):
						pts = [NumVal(idx=i, v=to_excel(c.value)) for i, c in enumerate(cs)]
						ser.cat.numRef.numCache = NumData(
							formatCode=cs[0].number_format, ptCount=len(cs), pt=pts
						)
					else:
						ser.cat = AxDataSource(
							strRef=StrRef(
								f=ser.cat.numRef.f,
								strCache=StrData(
									ptCount=len(cs),
									pt=[StrVal(idx=i, v=str(c.value)) for i, c in enumerate(cs)],
								),
							)
						)


def _bytes(wb, cached=True):
	if cached:
		_fill_caches(wb)
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
		src = _bytes(wb, cached=False)
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

	def test_no_cache_means_skipped_and_no_sheet_read(self):
		wb, ws = _book()
		_add(ws, BarChart())
		src = _bytes(wb, cached=False)
		with mock.patch.object(openpyxl, "load_workbook") as load:
			self.assertEqual(extract_charts(src), [])
		load.assert_not_called()

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
	def _one(self, cached=True):
		wb, ws = _book()
		_add(ws, BarChart())
		return _bytes(wb, cached=cached)

	def test_huge_ptcount_and_idx_are_not_allocated(self):
		cache = '</f><numCache><ptCount val="999999999"/><pt idx="0"><v>7</v></pt><pt idx="4000000000"><v>1</v></pt></numCache>'
		src = _rewrite(
			self._one(cached=False),
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

	def test_series_are_capped(self):
		wb, ws = _book()
		chart = BarChart()
		for _ in range(100):
			chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=4), titles_from_data=True)
		chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=4))
		ws.add_chart(chart, "F2")
		(spec,) = extract_charts(_bytes(wb))
		self.assertEqual(len(spec["series"]), MAX_SERIES)

	def test_wide_rows_large_cells_and_deep_rows_cost_nothing(self):
		# worksheets are never read, however heavy they are
		wb, ws = _book()
		_add(ws, BarChart())
		ws["A9"] = "x" * 200_000
		ws.cell(row=2000, column=300, value=1)
		src = _bytes(wb)
		with mock.patch.object(openpyxl, "load_workbook") as load:
			self.assertEqual(len(extract_charts(src)), 1)
		load.assert_not_called()

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


class TestFormatKind(unittest.TestCase):
	def test_dates_times_and_others(self):
		kind = xlsx_charts._format_kind
		for code in (
			"yyyy-mm-dd",
			"dd/mm/yyyy",
			"d-mmm-yy",
			"mmm",
			"yyyymmdd",
			"yyyy-mm-dd hh:mm",
			'"FY" yyyy',
		):
			self.assertEqual(kind(code), "date", code)
		for code in ("h:mm", "hh:mm:ss", "[h]:mm:ss", "[h]:mm", "[mm]:ss", "mm:ss", "[m]", "h:mm AM/PM"):
			self.assertEqual(kind(code), "time", code)
		for code in ("General", "Standard", "0.00", "#,##0", r"0\ m", '0 "days"', r"0\ d", "0.0_m", "", None):
			self.assertIsNone(kind(code), code)

	def test_cached_dates_and_zero(self):
		def cache(fmt, *vals):
			pts = "".join(f'<pt idx="{i}"><v>{v}</v></pt>' for i, v in enumerate(vals))
			xml = f'<numRef xmlns="{xlsx_charts._NS["c"]}"><f>S!A1</f><numCache><formatCode>{fmt}</formatCode>{pts}</numCache></numRef>'
			return xlsx_charts._cache(xlsx_charts.ET.fromstring(xml))

		self.assertEqual(
			[xlsx_charts._label(v) for v in cache("yyyy-mm-dd", 46174, 0)], ["2026-06-01", "1899-12-30"]
		)
		self.assertEqual(cache(r"0\ m", 5), ["5"])
		self.assertEqual(cache("General", 5), ["5"])
		self.assertEqual(str(cache("h:mm:ss", 0)[0]), "00:00:00")


class TestBadCacheNumbers(unittest.TestCase):
	def test_unicode_digit_idx_does_not_drop_later_charts(self):
		wb, ws = _book()
		_add(ws, BarChart(), title="Bad")
		_add(ws, LineChart(), anchor="F20", title="Good")
		src = _rewrite(
			_bytes(wb),
			{
				"xl/charts/chart1.xml": lambda d: d.replace(
					b'<pt idx="0">', '<pt idx="\u00b2">'.encode()
				).replace(b'<ptCount val="3"/>', '<ptCount val="\u00b2"/>'.encode())
			},
		)
		self.assertIn("Good", [s["title"] for s in extract_charts(src)])


class TestExportRoundTrip(unittest.TestCase):
	"""Jarvis's own exports carry chart caches, so their charts preview."""

	DATA = [
		["Day", "Sales", "Cost"],
		[datetime.date(2026, 6, 1), 10, 4.5],
		[datetime.date(2026, 6, 2), 12, 5],
		[datetime.date(2026, 6, 3), None, 6],
	]

	def _charts(self):
		from jarvis._xlsx_charts import ExcelChart

		return [ExcelChart("column", 0, (1, 2), "Sales vs cost")]

	def _check(self, content):
		(spec,) = extract_charts(content)
		self.assertEqual(spec["title"], "Sales vs cost")
		self.assertEqual(spec["x"], ["2026-06-01", "2026-06-02", "2026-06-03"])
		self.assertEqual([s["name"] for s in spec["series"]], ["Sales", "Cost"])
		self.assertEqual(spec["series"][0]["data"], [10.0, 12.0, None])
		self.assertEqual(spec["series"][1]["data"], [4.5, 5.0, 6.0])

	def test_openpyxl_path(self):
		# the Frappe 15 export path: add_openpyxl_charts on a sheet holding the data
		from jarvis._xlsx_charts import add_openpyxl_charts

		wb = openpyxl.Workbook()
		ws = wb.active
		ws.title = "Data"
		for row in self.DATA:
			ws.append(row)
		add_openpyxl_charts(ws, self.DATA, self._charts())
		buf = io.BytesIO()
		wb.save(buf)
		self._check(buf.getvalue())

	def test_xlsxwriter_path(self):
		# Frappe 16 export path; xlsxwriter is a Frappe 16 dependency only, so
		# a Frappe 15 bench (openpyxl export) skips it.
		try:
			import xlsxwriter
		except ImportError:
			self.skipTest("xlsxwriter is not installed (Frappe 15 bench)")

		from jarvis._xlsx_charts import add_xlsxwriter_charts

		buf = io.BytesIO()
		wb = xlsxwriter.Workbook(buf, {"in_memory": True})
		ws = wb.add_worksheet("Data")
		for r, row in enumerate(self.DATA):
			for c, v in enumerate(row):
				if isinstance(v, datetime.date):
					v = v.isoformat()
				ws.write(r, c, v)
		add_xlsxwriter_charts(wb, ws, self.DATA, self._charts())
		wb.close()
		self._check(buf.getvalue())


class TestPreviewCell(unittest.TestCase):
	def test_dates(self):
		self.assertEqual(preview_cell("2026-06-01T00:00:00"), "2026-06-01")
		self.assertEqual(preview_cell("2026-06-01T14:30:05"), "2026-06-01 14:30")
		self.assertEqual(preview_cell("2026-06-01"), "2026-06-01")
		self.assertEqual(preview_cell("12:30:00"), "12:30:00")
		self.assertEqual(preview_cell("note T text here, long enough"), "note T text here, long enough")
		self.assertEqual(preview_cell(5), 5)
		self.assertEqual(preview_cell(""), "")
