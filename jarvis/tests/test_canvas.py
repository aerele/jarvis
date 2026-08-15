"""Unit tests for jarvis.chat.canvas — the pure detection/classification/strip
helpers that turn an agent reply referencing agent artifacts into render
metadata. Network fetch + File persistence are covered by the worker path.
"""

import unittest

from jarvis.chat import canvas


class TestCanvasDetection(unittest.TestCase):
	def test_detect_html_svg_top_level(self):
		self.assertEqual(
			canvas.detect_canvas_names(
				"Here's the chart: [x](/home/node/.openclaw/canvas/sales-this-month.svg)"
			),
			["sales-this-month.svg"],
		)
		self.assertEqual(
			canvas.detect_canvas_names("created [r](/home/node/.openclaw/canvas/report.html)"),
			["report.html"],
		)

	def test_detect_subdir(self):
		# The bug fix: artifacts in a subdir must be detected, path preserved.
		self.assertEqual(
			canvas.detect_canvas_names(
				"[overdue](/home/node/.openclaw/canvas/charts/sales-orders-overdue-june-2026.html)"
			),
			["charts/sales-orders-overdue-june-2026.html"],
		)
		self.assertEqual(canvas.detect_canvas_names("see canvas/a/b/c/deep.svg"), ["a/b/c/deep.svg"])

	def test_detect_pdf_image_excel(self):
		self.assertEqual(canvas.detect_canvas_names("canvas/invoice.pdf"), ["invoice.pdf"])
		self.assertEqual(canvas.detect_canvas_names("canvas/chart.png done"), ["chart.png"])
		self.assertEqual(canvas.detect_canvas_names("canvas/report.jpeg"), ["report.jpeg"])
		self.assertEqual(canvas.detect_canvas_names("canvas/export.xlsx"), ["export.xlsx"])

	def test_detect_none_and_boundary(self):
		self.assertEqual(canvas.detect_canvas_names("no charts, 32,000 INR"), [])
		self.assertEqual(canvas.detect_canvas_names(""), [])
		self.assertEqual(canvas.detect_canvas_names("see /var/www/index.html"), [])
		# boundary: foo.htmlx must NOT match as foo.html
		self.assertEqual(canvas.detect_canvas_names("canvas/foo.htmlx"), [])

	def test_dedup_and_cap(self):
		self.assertEqual(canvas.detect_canvas_names("canvas/a.svg canvas/a.svg"), ["a.svg"])
		many = " ".join(f"canvas/c{i}.png" for i in range(12))
		self.assertEqual(len(canvas.detect_canvas_names(many)), canvas._MAX_CANVAS_PER_TURN)

	def test_type_classification(self):
		self.assertEqual(canvas._type_for("foo.html"), "html")
		self.assertEqual(canvas._type_for("foo.htm"), "html")
		self.assertEqual(canvas._type_for("foo.svg"), "svg")
		self.assertEqual(canvas._type_for("foo.pdf"), "pdf")
		self.assertEqual(canvas._type_for("charts/foo.png"), "image")
		self.assertEqual(canvas._type_for("foo.JPEG"), "image")
		self.assertEqual(canvas._type_for("foo.xlsx"), "file")
		self.assertEqual(canvas._type_for("foo.csv"), "file")

	def test_strip_removes_dead_link_incl_subdir(self):
		out = canvas.strip_canvas_refs(
			"Chart: [c](/home/node/.openclaw/canvas/charts/a.html)",
			["charts/a.html"],
		)
		self.assertNotIn("canvas/charts/a.html", out)
		self.assertNotIn("](", out)
		self.assertIn("Chart", out)

	def test_title_from_filename_and_title_tag(self):
		self.assertEqual(canvas._title_for("charts/sales-this-month.svg", b"", "svg"), "Sales This Month")
		self.assertEqual(canvas._title_for("invoice.pdf", b"%PDF-1.7", "pdf"), "Invoice")
		self.assertEqual(canvas._title_for("x.html", b"<title>My Chart</title>", "html"), "My Chart")

	def test_http_base(self):
		self.assertEqual(canvas._http_base("ws://127.0.0.1:19002"), "http://127.0.0.1:19002")
		self.assertEqual(canvas._http_base("wss://host:443/"), "https://host:443")
		self.assertEqual(canvas._http_base(""), "")


if __name__ == "__main__":
	unittest.main()
