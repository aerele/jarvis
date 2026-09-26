"""Interrupted font-cache writes must not poison later document renders."""

import json
import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.tools._export.document import chart_font_cache, charts


def _metadata(version=390):
	return json.dumps(
		{"_version": version, "__class__": "FontManager", "ttflist": [], "afmlist": []}
	).encode()


class TestPrivateChartFontCache(unittest.TestCase):
	def test_cold_worker_does_not_run_external_font_discovery(self):
		with patch.object(chart_font_cache, "_FONT_CACHE", None), tempfile.TemporaryDirectory() as root:
			path = Path(root)
			marker = path / "font-command-ran"
			for command in ("fc-list", "system_profiler"):
				launcher = path / command
				launcher.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\nexit 1\n")
				launcher.chmod(0o700)
			with patch.dict(os.environ, {"PATH": root}):
				out = charts.render_charts(
					{0: charts.normalize_chart({"type": "bar", "rows": [{"label": "A", "value": 2}]})},
					deadline=time.monotonic() + 25,
				)
			self.assertIn("data:image/png;base64,", out[0])
			self.assertFalse(marker.exists(), "worker launched an external font-discovery command")

	def test_failed_worker_does_not_publish_font_metadata(self):
		with tempfile.TemporaryDirectory() as root:
			path = Path(root)
			(path / "chart_worker.py").write_text(
				"import os, sys\n"
				"from pathlib import Path\n"
				"cache = Path(os.environ['MPLCONFIGDIR'])\n"
				"(cache / 'fontlist-v390.json').write_text('{\"_version\":')\n"
				"(cache / 'fontlist-v390.json.matplotlib-lock').write_text('interrupted')\n"
				"sys.exit(1)\n"
			)
			with (
				patch.object(charts, "__file__", str(path / "charts.py")),
				patch.object(charts, "remember_font_cache") as remember,
			):
				with self.assertRaisesRegex(ValueError, "unavailable"):
					charts.render_charts(
						{0: charts.normalize_chart({"type": "bar", "rows": [{"label": "A", "value": 1}]})},
						deadline=time.monotonic() + 5,
					)
				remember.assert_not_called()

	def test_only_complete_bounded_font_metadata_is_reused(self):
		with patch.object(chart_font_cache, "_FONT_CACHE", None), tempfile.TemporaryDirectory() as root:
			path = Path(root)
			(path / "fontlist-v390.json").write_bytes(_metadata())
			chart_font_cache.remember_font_cache(path)
			cached = chart_font_cache._FONT_CACHE
			self.assertIsInstance(cached[1], bytes)
			for bad in (b'{"_version":', _metadata(999), b"x" * (2_000_000 + 1)):
				(path / "fontlist-v390.json").write_bytes(bad)
				chart_font_cache.remember_font_cache(path)
				self.assertEqual(chart_font_cache._FONT_CACHE, cached)
			other = path / "new-render"
			other.mkdir()
			chart_font_cache.seed_font_cache(other)
			self.assertEqual((other / "fontlist-v390.json").read_bytes(), cached[1])
			self.assertEqual([file.name for file in other.iterdir()], ["fontlist-v390.json"])

	def test_cache_filename_cannot_escape_request_directory(self):
		with (
			patch.object(chart_font_cache, "_FONT_CACHE", ("../outside.json", _metadata())),
			tempfile.TemporaryDirectory() as root,
		):
			path = Path(root) / "render"
			path.mkdir()
			chart_font_cache.seed_font_cache(path)
			self.assertEqual(list(path.iterdir()), [])
			self.assertFalse((path.parent / "outside.json").exists())

	def test_stale_shared_lock_and_partial_json_do_not_block_repeated_renders(self):
		inputs = {0: charts.normalize_chart({"type": "bar", "rows": [{"label": "A", "value": 2}]})}
		# Build one successful private cache. Later requests must use only that
		# complete immutable metadata, even if a shared user cache is poisoned.
		charts.render_charts(inputs, deadline=time.monotonic() + 25)
		with tempfile.TemporaryDirectory() as root:
			path = Path(root)
			partial = path / "fontlist-v390.json"
			lock = path / "fontlist-v390.json.matplotlib-lock"
			partial.write_text('{"_version":')
			lock.write_text("stale test lock")
			with patch.dict(os.environ, {"MPLCONFIGDIR": root}):
				for _ in range(2):
					out = charts.render_charts(inputs, deadline=time.monotonic() + 5)
					self.assertIn("data:image/png;base64,", out[0])
			self.assertEqual(partial.read_text(), '{"_version":')
			self.assertEqual(lock.read_text(), "stale test lock")
