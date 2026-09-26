"""Numeric PDF chart contract, independent of a Frappe site."""

import base64
import json
import subprocess
import time
import unittest
from itertools import pairwise
from unittest.mock import patch

from jarvis.tests.test_export_document import _MODULE, _RichBase
from jarvis.tools._export.document.charts import normalize_chart, render_charts
from jarvis.tools.export_document import _splice_charts, export_document


def spec(kind="bar", rows=None, **kwargs):
	return {"type": kind, "rows": rows or [{"label": "A", "value": 3}, {"label": "B", "value": -2}], **kwargs}


class TestNumericChartValidation(unittest.TestCase):
	def test_signed_values_and_category_order_preserved(self):
		out = normalize_chart(spec())
		self.assertEqual(out["labels"], ["A", "B"])
		self.assertEqual(out["series"], [{"name": "", "values": [3, -2]}])

	def test_missing_line_points_are_gaps(self):
		out = normalize_chart(
			spec(
				"line",
				[
					{"label": "Mar", "value": 4, "series": "Actual"},
					{"label": "Jan", "value": None, "series": "Actual"},
					{"label": "Feb", "value": 2, "series": "Plan"},
				],
			)
		)
		self.assertEqual(out["labels"], ["Mar", "Jan", "Feb"])
		self.assertEqual(out["series"][0]["values"], [4, None, None])
		self.assertEqual(out["series"][1]["values"], [None, None, 2])

	def test_invalid_values_and_duplicates_rejected(self):
		for value in (True, "12", float("nan"), float("inf"), 1e16, 10**1000, None):
			with self.subTest(value=value), self.assertRaises(ValueError):
				normalize_chart(spec(rows=[{"label": "A", "value": value}]))
		with self.assertRaises(ValueError):
			normalize_chart(spec(rows=[{"label": "A", "value": 1}, {"label": "A", "value": 2}]))

	def test_mixed_series_names_and_missing_bar_values_rejected(self):
		for rows in (
			[{"label": "A", "value": 1}, {"label": "B", "value": 2, "series": "X"}],
			[{"label": "A", "value": 1, "series": "X"}, {"label": "B", "value": 2, "series": "Y"}],
		):
			with self.assertRaises(ValueError):
				normalize_chart(spec(rows=rows))

	def test_pie_requires_nonnegative_positive_total_single_series(self):
		for rows in (
			[{"label": "A", "value": -1}],
			[{"label": "A", "value": 0}],
			[{"label": "A", "value": 1, "series": "X"}, {"label": "B", "value": 2, "series": "Y"}],
		):
			with self.assertRaises(ValueError):
				normalize_chart(spec("pie", rows))
		self.assertEqual(normalize_chart(spec("pie", [{"label": "A", "value": 2}]))["type"], "pie")

	def test_bounds_reject_instead_of_truncate(self):
		for kind, limit in (("bar", 12), ("column", 12), ("line", 60), ("pie", 8)):
			rows = [{"label": str(i), "value": i + 1} for i in range(limit)]
			self.assertEqual(len(normalize_chart(spec(kind, rows))["labels"]), limit)
			with self.assertRaises(ValueError):
				normalize_chart(spec(kind, rows + [{"label": "extra", "value": 1}]))
		for field, length in (("title", 200), ("caption", 200), ("unit", 40)):
			normalize_chart(spec(**{field: "x" * length}))
			with self.assertRaises(ValueError):
				normalize_chart(spec(**{field: "x" * (length + 1)}))
		with self.assertRaises(ValueError):
			normalize_chart(spec(rows=[{"label": "x" * 81, "value": 1}]))
		with self.assertRaises(ValueError):
			normalize_chart(spec("line", [{"label": "A", "value": 1, "series": str(i)} for i in range(5)]))

	def test_empty_controls_and_unknown_fields_rejected(self):
		for bad in (
			{"type": "bar", "rows": []},
			spec("scatter"),
			spec(url="http://evil"),
			spec(rows=[{"label": "A\nB", "value": 1}]),
			spec(rows=[{"label": "", "value": 1}]),
			spec("line", [{"label": "A", "value": None}]),
		):
			with self.subTest(bad=bad), self.assertRaises(ValueError):
				normalize_chart(bad)


class TestChartWorkerBoundary(unittest.TestCase):
	_PNG_BASE64 = (
		"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
	)

	def test_dependency_failure_is_safe_and_temp_directory_removed(self):
		with patch(
			"jarvis.tools._export.document.charts.subprocess.Popen", side_effect=OSError("secret")
		) as run:
			with self.assertRaisesRegex(ValueError, "unavailable") as error:
				render_charts({0: normalize_chart(spec())}, deadline=time.monotonic() + 20)
		self.assertNotIn("secret", str(error.exception))
		from pathlib import Path

		self.assertFalse(Path(run.call_args.kwargs["cwd"]).exists())
		self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

	def test_expired_deadline_never_launches(self):
		with patch("jarvis.tools._export.document.charts.subprocess.Popen") as run:
			with self.assertRaises(TimeoutError):
				render_charts({0: normalize_chart(spec())}, deadline=time.monotonic() - 1)
		run.assert_not_called()

	def test_subprocess_timeout_propagates_no_partial_output(self):
		with (
			patch("jarvis.tools._export.document.charts.subprocess.Popen") as popen,
			patch("jarvis.tools._export.document.charts.os.killpg") as kill_group,
		):
			worker = popen.return_value.__enter__.return_value
			worker.communicate.side_effect = [subprocess.TimeoutExpired("worker", 1), ("", None)]
			with self.assertRaises(TimeoutError):
				render_charts({0: normalize_chart(spec())}, deadline=time.monotonic() + 1)
			kill_group.assert_called_once()
			self.assertEqual(worker.communicate.call_count, 2)
			self.assertTrue(popen.call_args.kwargs["start_new_session"])

	def test_untrusted_worker_output_cannot_add_remote_images(self):
		with self.assertRaises(ValueError):
			self._render_worker_image("http://evil")

	def test_worker_image_requires_png_bytes_and_bounded_dimensions(self):
		png = base64.b64decode(self._PNG_BASE64)
		invalid = [b"Not a PNG despite having more than twenty-four bytes", png[:23]]
		for start in (16, 20):
			for dimension in (0, 1201):
				invalid.append(png[:start] + dimension.to_bytes(4, "big") + png[start + 4 :])
		for payload in invalid:
			with self.subTest(payload=payload), self.assertRaises(ValueError):
				self._render_worker_image(base64.b64encode(payload).decode("ascii"))

	def test_valid_worker_png_is_embedded(self):
		images = self._render_worker_image(self._PNG_BASE64)
		self.assertEqual(set(images), {0})
		self.assertIn(f'src="data:image/png;base64,{self._PNG_BASE64}"', images[0])
		self.assertIn('class="numeric-chart"', images[0])

	def _render_worker_image(self, encoded):
		with patch("jarvis.tools._export.document.charts.subprocess.Popen") as popen:
			worker = popen.return_value.__enter__.return_value
			worker.communicate.return_value = (json.dumps({"images": {"0": encoded}, "omitted": {}}), None)
			worker.returncode = 0
			return render_charts({0: normalize_chart(spec())}, deadline=time.monotonic() + 10)


class TestRealNumericCharts(unittest.TestCase):
	def test_unsupported_text_omits_only_affected_charts_with_safe_notes(self):
		labels = ["Supported", "தமிழ்", "中文", "العربية"]
		inputs = {
			i: normalize_chart(spec("bar", [{"label": label, "value": 1}])) for i, label in enumerate(labels)
		}
		notes = []
		images = render_charts(inputs, deadline=time.monotonic() + 25, notes=notes)
		self.assertEqual(set(images), {0})
		self.assertEqual(len(notes), 3)
		for i in (1, 2, 3):
			self.assertTrue(any(f"chart {i}" in note for note in notes))
			self.assertNotIn(labels[i], " ".join(notes))
		self.assertTrue(all("font or text shaping" in note for note in notes))

	def test_named_series_legends_preserve_underscores_and_fit_the_canvas(self):
		from matplotlib import rc_context
		from matplotlib.backends.backend_agg import FigureCanvasAgg

		from jarvis.tools._export.document.chart_worker import build_figure

		for kind in ("bar", "column", "line"):
			with self.subTest(kind=kind), rc_context({"text.parse_math": False}):
				names = ["_Actual", "Series 1 " + "W" * 31, "Series 2 " + "W" * 31, "Series 3 " + "W" * 31]
				rows = [
					{"label": label, "value": i + 1, "series": name}
					for label in ("Jan", "Feb")
					for i, name in enumerate(names)
				]
				fig = build_figure(normalize_chart(spec(kind, rows)))
				canvas = FigureCanvasAgg(fig)
				canvas.draw()
				legends = fig.legends or [fig.axes[0].get_legend()]
				texts = [text for legend in legends for text in legend.get_texts()]
				self.assertEqual(
					[text.get_text().replace("\n", "").replace(" ", "") for text in texts],
					[name.replace(" ", "") for name in names],
				)
				self._assert_text_inside(fig, texts, canvas.get_renderer())
				fig.clear()

	def _assert_text_inside(self, fig, texts, renderer):
		for text in texts:
			box = text.get_window_extent(renderer)
			self.assertGreaterEqual(box.x0, 0, text.get_text())
			self.assertGreaterEqual(box.y0, 0, text.get_text())
			self.assertLessEqual(box.x1, fig.bbox.width, text.get_text())
			self.assertLessEqual(box.y1, fig.bbox.height, text.get_text())

	def test_maximum_pie_legend_wraps_labels_values_and_units_without_clipping(self):
		from matplotlib import rc_context
		from matplotlib.backends.backend_agg import FigureCanvasAgg

		from jarvis.tools._export.document.chart_worker import build_figure

		rows = [{"label": f"Category {i} " + "X" * 68, "value": i + 1} for i in range(8)]
		unit = ("Longmeasurementunit" + "W" * 22)[:40]
		with rc_context({"text.parse_math": False}):
			fig = build_figure(normalize_chart(spec("pie", rows, unit=unit)))
			canvas = FigureCanvasAgg(fig)
			canvas.draw()
			legends = fig.legends or [fig.axes[0].get_legend()]
			texts = [text for legend in legends for text in legend.get_texts()]
			self._assert_text_inside(fig, texts, canvas.get_renderer())
			for text, row in zip(texts, rows, strict=True):
				self.assertEqual(
					text.get_text().replace("\n", "").replace(" ", ""),
					f"{row['label']} ({row['value']} {unit})".replace(" ", ""),
				)
			self.assertLessEqual(fig.bbox.height, 1200)
			fig.clear()

	def test_twelve_long_bar_labels_do_not_overlap(self):
		from matplotlib import rc_context
		from matplotlib.backends.backend_agg import FigureCanvasAgg

		from jarvis.tools._export.document.chart_worker import build_figure

		rows = [
			{"label": f"Region {i:02} " + "W" * 70, "value": i + 1, "series": f"{s} " + "W" * 38}
			for i in range(12)
			for s in range(4)
		]
		with rc_context({"text.parse_math": False}):
			fig = build_figure(normalize_chart(spec("bar", rows, unit="W" * 40)))
			canvas = FigureCanvasAgg(fig)
			canvas.draw()
			texts = fig.axes[0].get_yticklabels()
			self._assert_text_inside(fig, texts, canvas.get_renderer())
			boxes = sorted(
				[text.get_window_extent(canvas.get_renderer()) for text in texts], key=lambda box: box.y0
			)
			for lower, upper in pairwise(boxes):
				self.assertLessEqual(lower.y1, upper.y0)
			fig.clear()

	def test_twelve_long_column_labels_do_not_overlap(self):
		from matplotlib import rc_context
		from matplotlib.backends.backend_agg import FigureCanvasAgg

		from jarvis.tools._export.document.chart_worker import build_figure

		rows = [
			{
				"label": f"Item {i:02} enterprise product manufacturing and international specialist parts",
				"value": i + 1,
			}
			for i in range(12)
		]
		with rc_context({"text.parse_math": False}):
			fig = build_figure(normalize_chart(spec("column", rows)))
			canvas = FigureCanvasAgg(fig)
			canvas.draw()
			texts = fig.axes[0].get_xticklabels()
			self._assert_text_inside(fig, texts, canvas.get_renderer())
			boxes = sorted(
				[text.get_window_extent(canvas.get_renderer()) for text in texts], key=lambda box: box.x0
			)
			for left, right in pairwise(boxes):
				self.assertLessEqual(left.x1, right.x0)
			fig.clear()

	def test_pie_percent_labels_contrast_with_light_and_dark_slices(self):
		from matplotlib import rc_context

		from jarvis.tools._export.document.chart_worker import build_figure

		with rc_context({"text.parse_math": False}):
			fig = build_figure(
				normalize_chart(spec("pie", [{"label": "A", "value": 1}, {"label": "B", "value": 1}])),
				colors=("#ffffff", "#000000"),
			)
			percent_labels = [text for text in fig.axes[0].texts if "%" in text.get_text()]
			self.assertEqual([text.get_color() for text in percent_labels], ["black", "white"])
			fig.clear()

	def test_smallest_positive_pie_is_still_renderable(self):
		from matplotlib import rc_context

		from jarvis.tools._export.document.chart_worker import build_figure

		with rc_context({"text.parse_math": False}):
			fig = build_figure(normalize_chart(spec("pie", [{"label": "A", "value": 5e-324}])))
			self.assertEqual(len(fig.axes[0].patches), 1)
			self.assertAlmostEqual(fig.axes[0].patches[0].theta2 - fig.axes[0].patches[0].theta1, 360)
			fig.clear()

	def test_axes_and_gaps_preserve_values(self):
		from matplotlib import rc_context

		from jarvis.tools._export.document.chart_worker import build_figure

		with rc_context({"text.parse_math": False}):
			for kind in ("bar", "column"):
				fig = build_figure(normalize_chart(spec(kind)))
				bars = fig.axes[0].patches
				actual = [bar.get_width() if kind == "bar" else bar.get_height() for bar in bars]
				self.assertEqual(actual, [3, -2])
				baseline = [bar.get_x() if kind == "bar" else bar.get_y() for bar in bars]
				self.assertEqual(baseline, [0, 0])
				fig.clear()
			fig = build_figure(
				normalize_chart(
					spec(
						"line",
						[
							{"label": "A", "value": -2},
							{"label": "B", "value": None},
							{"label": "C", "value": 4},
						],
					)
				)
			)
			line = fig.axes[0].lines[0]
			import math

			self.assertTrue(math.isnan(line.get_ydata()[1]))
			self.assertEqual(list(line.get_xdata()), [0, 1, 2])
			self.assertEqual(line.get_marker(), "o")
			fig.clear()

	def test_all_types_render_one_bounded_png_each(self):
		inputs = {
			i: normalize_chart(
				spec(kind, [{"label": "Alpha <&$>", "value": 4}, {"label": "Beta", "value": 2}])
			)
			for i, kind in enumerate(("bar", "column", "line", "pie"))
		}
		images = render_charts(inputs, deadline=time.monotonic() + 25)
		self.assertEqual(set(images), set(inputs))
		for markup in images.values():
			self.assertIn('class="numeric-chart"', markup)
			self.assertIn('src="data:image/png;base64,', markup)
			self.assertNotIn("http:", markup)
			encoded = markup.split('src="data:image/png;base64,')[1].split('"')[0]
			png = base64.b64decode(encoded)
			self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
			self.assertLessEqual(int.from_bytes(png[16:20], "big"), 1200)
			self.assertLessEqual(int.from_bytes(png[20:24], "big"), 1200)


class TestNumericChartIntegration(_RichBase):
	def test_empty_legacy_specs_do_not_count_as_charts(self):
		from jarvis.exceptions import InvalidArgumentError

		for empty in (None, {}, {"rows": []}):
			with self.subTest(empty=empty):
				out = export_document("Body {{chart:0}}", charts=[empty])
				self.assertEqual(out["chart_count"], 0)
				self.assertTrue(any("chart 0" in note for note in out["notes"]))
				self.assertNotIn('class="bar-chart"', self._rich_doc_body())
				self.save_rich.reset_mock()
				with self.assertRaisesRegex(InvalidArgumentError, "No chart content"):
					export_document("{{chart:0}}", charts=[empty])
				self.save_rich.assert_not_called()

	def test_repeated_placeholders_cannot_amplify_images_without_bound(self):
		from jarvis.exceptions import InvalidArgumentError

		with patch(f"{_MODULE}.render_charts") as render:
			with self.assertRaisesRegex(InvalidArgumentError, "20.*placeholder"):
				export_document("{{chart:0}} " * 21, charts=[spec()])
			render.assert_not_called()

	def test_eligible_typed_specs_only_render_once_and_count(self):
		with patch(
			f"{_MODULE}.render_charts", return_value={0: '<div class="numeric-chart">image</div>'}
		) as render:
			out = export_document(
				"<p>{{chart:0}} {{chart:0}}</p><code>{{chart:1}}</code>",
				content_is_html=True,
				charts=[spec(), spec(), spec()],
			)
		self.assertEqual(out["chart_count"], 1)
		self.assertEqual(set(render.call_args.args[0]), {0})
		self.assertIn("{{chart:1}}", self._rich_doc_body())
		self.assertTrue(any("chart 2" in note for note in out["notes"]))

	def test_bad_typed_chart_is_not_legacy_progress_bar(self):
		out = export_document("Body {{chart:0}}", charts=[spec(rows=[{"label": "A", "value": "12"}])])
		self.assertEqual(out["chart_count"], 0)
		self.assertTrue(any("chart 0" in note for note in out["notes"]))
		self.assertNotIn('class="bar-chart"', self._rich_doc_body())

	def test_renderer_failure_is_safe_note_and_chart_only_fails_closed(self):
		with patch(f"{_MODULE}.render_charts", side_effect=ValueError("private data")):
			out = export_document("Body {{chart:0}}", charts=[spec()])
			self.assertEqual(out["chart_count"], 0)
			self.assertNotIn("private data", str(out["notes"]))
			from jarvis.exceptions import InvalidArgumentError

			self.save_rich.reset_mock()
			with self.assertRaisesRegex(InvalidArgumentError, "chart renderer was unavailable") as error:
				export_document("{{chart:0}}", charts=[spec()])
			self.assertNotIn("private data", str(error.exception))
			self.assertEqual(self.telemetry.call_args.kwargs["outcome"], "rejected")
			self.assertIn("chart renderer was unavailable", self.telemetry.call_args.kwargs["detail"])
			self.save_rich.assert_not_called()

	def test_chart_only_unsupported_text_preserves_safe_reason(self):
		from jarvis.exceptions import InvalidArgumentError

		def omit(_specs, *, notes, **_kwargs):
			notes.append(
				"chart 0 was omitted because its text needs a font or text shaping unsupported by the chart renderer; use supported labels or a table"
			)
			return {}

		with patch(f"{_MODULE}.render_charts", side_effect=omit):
			with self.assertRaisesRegex(InvalidArgumentError, "font or text shaping"):
				export_document("{{chart:0}}", charts=[spec(rows=[{"label": "private label", "value": 1}])])
		self.assertEqual(self.telemetry.call_args.kwargs["outcome"], "rejected")
		self.assertIn("font or text shaping", self.telemetry.call_args.kwargs["detail"])
		self.assertNotIn("private label", str(self.telemetry.call_args))
		self.render.assert_not_called()
		self.save_rich.assert_not_called()

	def test_timeout_does_not_save_degraded_document(self):
		from jarvis.exceptions import InvalidArgumentError

		with patch(f"{_MODULE}.render_charts", side_effect=TimeoutError("deadline")):
			with self.assertRaisesRegex(InvalidArgumentError, "time limit"):
				export_document("Body {{chart:0}}", charts=[spec()])
		self.save_rich.assert_not_called()

	def test_expired_shared_budget_never_calls_pdf_renderer(self):
		from jarvis.exceptions import InvalidArgumentError

		with patch(f"{_MODULE}.time.monotonic", side_effect=[0, 26]):
			with self.assertRaisesRegex(InvalidArgumentError, "time limit"):
				export_document("Body", theme=True)
		self.render.assert_not_called()
		self.save_rich.assert_not_called()

	def test_more_than_eight_typed_charts_rejected(self):
		from jarvis.exceptions import InvalidArgumentError

		with self.assertRaises(InvalidArgumentError):
			export_document("Body", charts=[spec() for _ in range(9)])

	def test_legacy_count_and_caller_image_sanitization(self):
		out = export_document(
			'<img src="http://evil"><p>{{chart:0}} {{chart:0}}</p>',
			content_is_html=True,
			charts=[{"rows": [{"label": "A", "value": "3", "pct": 50}]}],
		)
		self.assertEqual(out["chart_count"], 1)
		self.assertNotIn('src="http://evil"', self._rich_doc_body())
