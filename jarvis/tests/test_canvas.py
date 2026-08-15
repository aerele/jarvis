"""Unit tests for jarvis.chat.canvas — the pure detection/classification/strip
helpers that turn an agent reply referencing agent artifacts into render
metadata. Network fetch + File persistence are covered by the worker path.
"""

import unittest
from unittest.mock import Mock, patch

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


class TestGatewayFallbackDetection(unittest.TestCase):
	"""``_gateway_fakes_missing_canvas_as_ok`` — the sentinel probe that tells a
	healthy canvas host apart from a gateway that fell through to its own web
	UI as a catch-all (which answers every path, real or fake, with 200)."""

	def test_healthy_gateway_404s_the_sentinel(self):
		resp = Mock(status_code=404)
		with patch("requests.get", return_value=resp) as get:
			result = canvas._gateway_fakes_missing_canvas_as_ok("ws://127.0.0.1:19000", "tok")
		self.assertFalse(result)
		# Probed a name that cannot exist, through the same canvas route.
		(url,), kwargs = get.call_args
		self.assertIn("/__openclaw__/canvas/documents/", url)
		self.assertIn("index.html", url)
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")

	def test_broken_gateway_200s_the_sentinel(self):
		resp = Mock(status_code=200)
		with patch("requests.get", return_value=resp):
			result = canvas._gateway_fakes_missing_canvas_as_ok("ws://127.0.0.1:19000", "tok")
		self.assertTrue(result)

	def test_non_404_error_status_also_counts_as_broken(self):
		resp = Mock(status_code=500)
		with patch("requests.get", return_value=resp):
			result = canvas._gateway_fakes_missing_canvas_as_ok("ws://127.0.0.1:19000", "tok")
		self.assertTrue(result)

	def test_probe_network_error_is_inconclusive_not_broken(self):
		with patch("requests.get", side_effect=Exception("boom")):
			result = canvas._gateway_fakes_missing_canvas_as_ok("ws://127.0.0.1:19000", "tok")
		self.assertFalse(result)

	def test_missing_agent_url_or_token_short_circuits(self):
		with patch("requests.get") as get:
			self.assertFalse(canvas._gateway_fakes_missing_canvas_as_ok("", "tok"))
			self.assertFalse(canvas._gateway_fakes_missing_canvas_as_ok("ws://x", ""))
		get.assert_not_called()

	def test_sentinel_name_is_unique_per_call(self):
		resp = Mock(status_code=404)
		with patch("requests.get", return_value=resp) as get:
			canvas._gateway_fakes_missing_canvas_as_ok("ws://127.0.0.1:19000", "tok")
			canvas._gateway_fakes_missing_canvas_as_ok("ws://127.0.0.1:19000", "tok")
		first_url = get.call_args_list[0].args[0]
		second_url = get.call_args_list[1].args[0]
		self.assertNotEqual(first_url, second_url)


if __name__ == "__main__":
	unittest.main()
