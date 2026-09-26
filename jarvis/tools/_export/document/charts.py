"""Closed numeric chart contract and a deadline-bound, isolated raster worker.

Caller HTML stays image-free. Only validated numeric data reaches the worker,
and only its bounded PNGs are embedded after the document sanitizer.
"""

import base64
import binascii
import contextlib
import html
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jarvis.tools._export.document.chart_font_cache import remember_font_cache, seed_font_cache

MAX_TYPED_CHARTS = 8
_KINDS = {"bar": 12, "column": 12, "line": 60, "pie": 8}
_MAX_OUTPUT_BYTES = 12_000_000


def _text(value, limit: int, *, required=False) -> str:
	if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
		raise ValueError("chart text must be bounded plain text")
	if required and not value.strip():
		raise ValueError("chart labels and series names cannot be blank")
	return value


def normalize_chart(spec: dict) -> dict:
	"""Validate without coercion or truncation; expand missing line points to gaps."""
	if not isinstance(spec, dict) or set(spec) - {"type", "rows", "title", "caption", "unit"}:
		raise ValueError("unsupported chart fields")
	kind = spec.get("type")
	if not isinstance(kind, str) or kind not in _KINDS:
		raise ValueError("unsupported chart type")
	rows = spec.get("rows")
	if not isinstance(rows, list) or not rows or len(rows) > 240:
		raise ValueError("chart rows must be a nonempty bounded array")
	labels, names, points = [], [], {}
	for row in rows:
		if not isinstance(row, dict) or set(row) - {"label", "value", "series"}:
			raise ValueError("unsupported chart row fields")
		label = _text(row.get("label"), 80, required=True)
		name = _text(row["series"], 40, required=True) if "series" in row else ""
		value = row.get("value")
		if "value" not in row or (value is None and kind != "line"):
			raise ValueError("only line charts allow null gaps")
		if value is not None and (
			isinstance(value, bool)
			or not isinstance(value, (int, float))
			or abs(value) > 1e15
			or not math.isfinite(value)
		):
			raise ValueError("chart values must be finite numbers within +/-1e15")
		if label not in labels:
			labels.append(label)
		if name not in names:
			names.append(name)
		if (label, name) in points:
			raise ValueError("duplicate category and series")
		points[label, name] = value
	if len(labels) > _KINDS[kind] or len(names) > 4 or ("" in names and len(names) > 1):
		raise ValueError("chart category or series limits exceeded, or mixed named and unnamed series")
	series = [{"name": name, "values": [points.get((label, name)) for label in labels]} for name in names]
	if any(all(value is None for value in item["values"]) for item in series):
		raise ValueError("each chart series needs a numeric value")
	if kind != "line" and any(value is None for item in series for value in item["values"]):
		raise ValueError("bar, column and pie require every category value")
	if kind == "pie":
		values = series[0]["values"]
		if len(series) != 1 or any(value < 0 for value in values) or sum(values) <= 0:
			raise ValueError("pie requires one nonnegative series with positive total")
	return {
		"type": kind,
		"labels": labels,
		"series": series,
		"title": _text(spec.get("title", ""), 200),
		"caption": _text(spec.get("caption", ""), 200),
		"unit": _text(spec.get("unit", ""), 40),
	}


def remaining_seconds(deadline: float) -> float:
	remaining = deadline - time.monotonic()
	if remaining <= 0:
		raise TimeoutError("document render deadline exceeded")
	return remaining


def _markup(encoded: str, spec: dict) -> str:
	try:
		png = base64.b64decode(encoded, validate=True)
	except (ValueError, TypeError, binascii.Error):
		raise ValueError("chart renderer returned invalid image data") from None
	if (
		len(png) < 24
		or not png.startswith(b"\x89PNG\r\n\x1a\n")
		or not 1 <= int.from_bytes(png[16:20], "big") <= 1200
		or not 1 <= int.from_bytes(png[20:24], "big") <= 1200
	):
		raise ValueError("chart renderer returned invalid image dimensions")
	title = f'<p class="bar-chart-title">{html.escape(spec["title"])}</p>' if spec["title"] else ""
	caption = f'<p class="bar-chart-caption">{html.escape(spec["caption"])}</p>' if spec["caption"] else ""
	alt = html.escape(spec["title"] or f"{spec['type']} chart", quote=True)
	return f'<div class="numeric-chart">{title}<img src="data:image/png;base64,{encoded}" alt="{alt}"/>{caption}</div>'


def render_charts(specs: dict[int, dict], *, deadline: float, colors=None, notes=None) -> dict[int, str]:
	"""Render a validated batch once. Timeout fails closed; unavailable renderer
	can be reported as an indexed note by the caller. No child stderr is logged.
	Complete process-local font metadata seeds a private worker cache. Request
	data and plotting state never survive; the temporary cwd excludes site rc files.
	"""
	if not specs:
		return {}
	remaining_seconds(deadline)
	palette = [
		(colors or {}).get(f"s{i}", fallback)
		for i, fallback in enumerate(("#1f4e79", "#2e7d6b", "#a9791c", "#6b4a7a"), 1)
	]
	if any(not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color) for color in palette):
		raise ValueError("chart renderer requires valid theme colors")
	with tempfile.TemporaryDirectory(prefix="document-charts-") as workdir:
		font_cache = Path(workdir) / "matplotlib"
		font_cache.mkdir()
		seed_font_cache(font_cache)
		# Agg uses bundled DejaVu Sans and needs no external binaries. Optional
		# fc-list/system_profiler discovery can consume the whole cold deadline;
		# exclude it without changing the serving process or PDF renderer's PATH.
		env = {**os.environ, "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(font_cache), "PATH": ""}
		try:
			with subprocess.Popen(
				[sys.executable, str(Path(__file__).with_name("chart_worker.py"))],
				text=True,
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
				env=env,
				cwd=workdir,
				start_new_session=True,
			) as worker:
				try:
					output, _ = worker.communicate(
						json.dumps({"charts": specs, "colors": palette}, allow_nan=False),
						timeout=remaining_seconds(deadline),
					)
				except BaseException:
					# Font discovery can start fc-list/system_profiler. Killing only
					# the worker leaves those scanners running after a timed-out call.
					with contextlib.suppress(ProcessLookupError):
						os.killpg(worker.pid, signal.SIGKILL)
					worker.communicate()
					raise
				returncode = worker.returncode
				if returncode:
					# A crashed worker can leave a font scanner alive even though
					# communicate completed normally. Its session is still ours.
					with contextlib.suppress(ProcessLookupError):
						os.killpg(worker.pid, signal.SIGKILL)
		except subprocess.TimeoutExpired:
			raise TimeoutError("document chart render deadline exceeded") from None
		except OSError:
			raise ValueError("chart renderer unavailable") from None
		if returncode == 0:
			remember_font_cache(font_cache)
	remaining_seconds(deadline)
	if returncode or len(output) > _MAX_OUTPUT_BYTES:
		raise ValueError("chart renderer unavailable")
	try:
		result = json.loads(output)
		if not isinstance(result, dict) or set(result) != {"images", "omitted"}:
			raise ValueError
		images, omitted = result["images"], result["omitted"]
		if (
			not isinstance(images, dict)
			or not isinstance(omitted, dict)
			or set(images) & set(omitted)
			or set(images) | set(omitted) != {str(i) for i in specs}
			or any(reason != "unsupported_text" for reason in omitted.values())
		):
			raise ValueError
		markup = {i: _markup(images[str(i)], spec) for i, spec in specs.items() if str(i) in images}
		if notes is not None:
			for index in omitted:
				notes.append(
					f"chart {index} was omitted because its text needs a font or text shaping unsupported by the chart renderer; use supported labels or a table"
				)
		return markup
	except (ValueError, TypeError, KeyError):
		raise ValueError("chart renderer returned invalid image data") from None
