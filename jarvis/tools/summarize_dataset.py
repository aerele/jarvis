"""Compute summary statistics over a tabular dataset the agent already has.

The agent gathers rows (via query() / get_list / run_report), then passes them
here to get per-column descriptive stats in one shot - count, nulls, distinct,
a numeric summary (min/max/mean/median/stdev/sum) for number columns, and the
top value frequencies for categorical columns - instead of eyeballing rows or
making the model do arithmetic it gets wrong.

Pure-Python (``statistics`` stdlib); no pandas, no subprocess. It only
summarizes data passed in (no DB access of its own), so it inherits whatever
permission scoping produced that data.
"""

import hashlib
import math
import statistics
from collections import Counter

from jarvis.exceptions import InvalidArgumentError

_MAX_ROWS = 100_000
_MAX_COLS = 200
_DEFAULT_TOP_N = 5
_KEY_MAX = 256  # strings longer than this are hashed for the counting key
_DISPLAY_MAX = 80  # but a `top` value is shown truncated to keep output small


def summarize_dataset(rows: list, columns: list | None = None, top_n: int = 5) -> dict:
	"""Summarize ``rows`` (a list of dicts, or a list of lists with ``columns``).

	Returns ``{"row_count": N, "columns": {col: {...}}}``. Each column reports
	``count`` (non-null), ``null_count``, ``distinct`` and ``type``; numeric
	columns add ``min``/``max``/``sum``/``mean``/``median``/``stdev``;
	categorical/mixed columns add ``top`` (the ``top_n`` most common values).
	"""
	if not isinstance(rows, list) or not rows:
		raise InvalidArgumentError("rows must be a non-empty list")
	if len(rows) > _MAX_ROWS:
		raise InvalidArgumentError(f"too many rows ({len(rows)}); cap is {_MAX_ROWS}")
	top_n = max(1, min(int(top_n or _DEFAULT_TOP_N), 50))

	first = rows[0]
	if isinstance(first, dict):
		cols = list(columns) if columns else list(first.keys())

		def get(r, c):
			return r.get(c) if isinstance(r, dict) else None
	elif isinstance(first, (list, tuple)):
		if not columns:
			raise InvalidArgumentError("list-of-lists rows require a 'columns' list")
		cols = list(columns)
		idx = {c: i for i, c in enumerate(cols)}

		def get(r, c):
			if not isinstance(r, (list, tuple)):
				return None
			i = idx[c]
			return r[i] if i < len(r) else None
	else:
		raise InvalidArgumentError("rows must be a list of dicts or a list of lists")

	if len(cols) > _MAX_COLS:
		raise InvalidArgumentError(f"too many columns ({len(cols)}); cap is {_MAX_COLS}")

	# One row set throughout: the cap above, row_count below, and every column
	# loop all iterate `rows`. (A non-matching row contributes nulls via get().)
	out = {}
	for c in cols:
		vals = [get(r, c) for r in rows]
		non_null = [v for v in vals if v is not None and v != ""]
		nums = [n for n in (_num(v) for v in non_null) if n is not None]
		col = {"count": len(non_null), "null_count": len(vals) - len(non_null)}
		if non_null and len(nums) == len(non_null):
			col["type"] = "numeric"
			col["distinct"] = len(set(nums))
			col["min"] = min(nums)
			col["max"] = max(nums)
			col["sum"] = sum(nums)
			col["mean"] = statistics.fmean(nums)
			col["median"] = statistics.median(nums)
			col["stdev"] = statistics.pstdev(nums) if len(nums) > 1 else 0.0
		else:
			col["type"] = "mixed" if nums else "categorical"
			# One pass: Counter (keyed by the collision-safe key) gives both the
			# top values and distinct (= len(counts)); `display` maps each key
			# back to a readable, length-bounded value for output.
			counts = Counter()
			display = {}
			for v in non_null:
				k = _key(v)
				counts[k] += 1
				if k not in display:
					s = v if isinstance(v, str) else str(v)
					display[k] = s if len(s) <= _DISPLAY_MAX else s[:_DISPLAY_MAX] + "..."
			col["distinct"] = len(counts)
			col["top"] = [{"value": display[k], "count": n} for k, n in counts.most_common(top_n)]
		out[c] = col
	return {"row_count": len(rows), "columns": out}


def _num(v):
	"""Coerce to a finite number, or None. Booleans and non-finite values
	(``nan`` / ``inf``, including the strings ``"nan"``/``"inf"``) are NOT
	numbers here - they would otherwise poison a whole column's stats."""
	if isinstance(v, bool):
		return None
	if isinstance(v, (int, float)):
		return v if math.isfinite(v) else None
	if isinstance(v, str):
		try:
			n = float(v)
		except ValueError:
			return None
		return n if math.isfinite(n) else None
	return None


def _key(v):
	"""Hashable, memory-bounded, collision-safe key for distinct/top counting.

	Scalars key as themselves; a long string hashes to a fixed-size digest so
	distinct stays accurate (no conflating values that share a long prefix)
	without holding multi-KB strings as dict keys."""
	if isinstance(v, (int, float, bool)):
		return v
	s = v if isinstance(v, str) else str(v)
	if len(s) <= _KEY_MAX:
		return s
	return "#" + hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()
