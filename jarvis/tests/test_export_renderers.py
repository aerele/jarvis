import io

from frappe.tests.utils import FrappeTestCase

from jarvis.tools._export import renderers
from jarvis.tools._export.model import ExportModel


class TestCsvRenderer(FrappeTestCase):
	def test_header_and_rows_present(self):
		m = ExportModel(columns=["name", "qty"], rows=[["A", 1], ["B", 2]], total=2)
		out = renderers.csv(m).decode("utf-8").splitlines()
		self.assertIn("name", out[0])
		self.assertIn("qty", out[0])
		self.assertIn("A", out[1])
		self.assertIn("B", out[2])

	def test_escapes_formula_cell(self):
		m = ExportModel(columns=["c"], rows=[["=SUM(A1:A9)"]], total=1)
		out = renderers.csv(m).decode("utf-8")
		self.assertIn("'=SUM(A1:A9)", out)

	def test_escapes_formula_header(self):
		m = ExportModel(columns=["=cmd"], rows=[["ok"]], total=1)
		out = renderers.csv(m).decode("utf-8")
		self.assertTrue(out.splitlines()[0].startswith("'=cmd") or '"\'=cmd"' in out.splitlines()[0])

	def test_empty_rows_gives_header_only_file(self):
		m = ExportModel(columns=["name"], rows=[], total=0)
		out = renderers.csv(m).decode("utf-8").splitlines()
		self.assertEqual(len(out), 1)
		self.assertIn("name", out[0])


class TestXlsxRenderer(FrappeTestCase):
	def _cells(self, content: bytes):
		import openpyxl

		wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
		ws = wb.active
		return [[c for c in row] for row in ws.iter_rows(values_only=True)]

	def test_produces_zip_workbook(self):
		m = ExportModel(columns=["name", "qty"], rows=[["A", 1]], total=1)
		self.assertEqual(renderers.xlsx(m)[:2], b"PK")

	def test_escaped_formula_is_in_the_real_cell(self):
		m = ExportModel(columns=["c"], rows=[["=cmd"]], total=1)
		cells = self._cells(renderers.xlsx(m))
		# Row 0 header, row 1 the escaped value.
		self.assertEqual(cells[1][0], "'=cmd")

	def test_header_only_when_empty(self):
		m = ExportModel(columns=["name"], rows=[], total=0)
		cells = self._cells(renderers.xlsx(m))
		self.assertEqual(len(cells), 1)
		self.assertEqual(cells[0][0], "name")

	def test_overlong_cell_truncated_and_flagged(self):
		big = "x" * 40_000
		m = ExportModel(columns=["c"], rows=[[big]], total=1)
		content = renderers.xlsx(m)
		self.assertTrue(m.meta.get("cells_truncated"))
		cell = self._cells(content)[1][0]
		self.assertLessEqual(len(cell), 32_767)
		self.assertTrue(cell.endswith("…[truncated]"))
