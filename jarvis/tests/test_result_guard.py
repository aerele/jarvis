import json
import unittest

from jarvis.tools._result_guard import MAX_RESULT_CHARS, enforce_result_budget


def _rows(n, width):
	return [{"name": f"R{i}", "blob": "x" * width} for i in range(n)]


def _size(o):
	return len(json.dumps(o, default=str, ensure_ascii=False, separators=(",", ":")))


class TestEnforceResultBudget(unittest.TestCase):
	def test_small_unchanged_no_event(self):
		data = [{"name": "A"}]
		out, ev = enforce_result_budget(data, tool="get_list")
		self.assertIs(out, data)
		self.assertIsNone(ev)

	def test_bare_list_truncated_fits_budget(self):
		data = _rows(3000, 50)
		out, ev = enforce_result_budget(data, tool="get_list")
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["total"], 3000)
		self.assertEqual(out["shown"], len(out["rows"]))
		self.assertLess(out["shown"], 3000)
		self.assertIn("PARTIAL", out["note"])
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)
		self.assertEqual(ev["kind"], "truncated")
		self.assertEqual(ev["original_chars"], _size(data))

	def test_query_rows_truncated_sql_preserved(self):
		data = {"sql": "SELECT ...", "columns": ["a"], "rows": _rows(3000, 50)}
		out, ev = enforce_result_budget(data, tool="query")
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["sql"], "SELECT ...")
		self.assertEqual(out["columns"], ["a"])
		self.assertEqual(out["total"], 3000)
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)

	def test_run_report_result_key_truncated(self):
		data = {"columns": ["a", "b"], "result": _rows(3000, 50)}
		out, ev = enforce_result_budget(data, tool="run_report")
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["shown"], len(out["result"]))
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)

	def test_query_huge_sql_bounds_rows_keeps_sql(self):
		# The non-row bulk (the SQL text) alone exceeds the budget: rows can't be
		# truncated under the cap, so ALL rows are dropped to bound the blast radius
		# while the sql is preserved (not itself truncated). uncapped_nonrow.
		data = {"sql": "x" * (MAX_RESULT_CHARS + 5000), "rows": [{"n": 1}]}
		out, ev = enforce_result_budget(data, tool="query")
		self.assertEqual(ev["kind"], "uncapped_nonrow")
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["sql"], data["sql"])
		self.assertEqual(out["rows"], [])
		self.assertEqual(out["shown"], 0)
		self.assertEqual(ev["total"], 1)

	def test_single_giant_row_returns_empty_rows_note(self):
		data = [{"blob": "x" * (MAX_RESULT_CHARS + 5000)}]
		out, ev = enforce_result_budget(data, tool="get_list")
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["shown"], 0)
		self.assertEqual(out["rows"], [])
		self.assertIn("fewer fields", out["note"])
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)

	def test_non_list_passes_through_with_event(self):
		data = {"doctype": "Sales Invoice", "items": [{"x": "y" * 50} for _ in range(2000)]}
		out, ev = enforce_result_budget(data, tool="get_doc")
		self.assertIs(out, data)
		self.assertEqual(ev["kind"], "uncapped")

	def test_empty_list_unchanged(self):
		out, ev = enforce_result_budget([], tool="get_list")
		self.assertEqual(out, [])
		self.assertIsNone(ev)

	def test_unserializable_never_raises(self):
		class Boom:
			def __str__(self):
				raise RuntimeError("no")

		data = [{"x": Boom()}]
		out, ev = enforce_result_budget(data, tool="get_list")
		# Cannot even measure the result: pass it through unchanged, but surface a
		# distinct measure_failed signal (not the in-budget "small" -> None).
		self.assertIs(out, data)
		self.assertEqual(ev["kind"], "measure_failed")
		self.assertIsNone(ev["original_chars"])

	def test_cjk_measured_compact_not_ascii_escaped(self):
		# ~19k CJK chars: compact (ensure_ascii=False) ~21k < 35k -> NOT truncated;
		# ascii-escaped it would be ~115k > 35k. Fails if the guard used ensure_ascii=True.
		data = [{"name": "客户" * 80} for _ in range(120)]
		out, ev = enforce_result_budget(data, tool="get_list")
		self.assertIsNone(ev)
		self.assertIs(out, data)

	def test_small_leading_row_kept_when_later_rows_huge(self):
		data = [{"blob": "y" * 10}] + [{"blob": "x" * 100000} for _ in range(3)]
		out, ev = enforce_result_budget(data, tool="get_list")
		self.assertTrue(out["_truncated"])
		# Deterministic: only the tiny leading row fits; the 3 huge rows do not.
		self.assertEqual(out["shown"], 1)
		self.assertEqual(out["rows"][0], {"blob": "y" * 10})
		self.assertIn("PARTIAL", out["note"])
		self.assertNotIn("even one row", out["note"])
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)

	def test_nested_rows_truncated_with_fat_under_budget_meta(self):
		data = {"meta": "m" * 5000, "rows": _rows(3000, 50)}
		out, ev = enforce_result_budget(data, tool="query")
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["meta"], "m" * 5000)
		self.assertLess(out["shown"], 3000)
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)

	def test_uncapped_nonrow_bounds_rows(self):
		# Both the surrounding SQL AND the rows are large. The non-row bulk alone
		# blows the budget, so rows are bounded to [] (shown=0) - NOT shipped whole.
		data = {"sql": "x" * (MAX_RESULT_CHARS + 5000), "rows": _rows(3000, 50)}
		out, ev = enforce_result_budget(data, tool="query")
		self.assertEqual(ev["kind"], "uncapped_nonrow")
		self.assertEqual(ev["shown"], 0)
		self.assertEqual(ev["total"], 3000)
		self.assertEqual(out["shown"], 0)
		self.assertEqual(out["rows"], [])
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["sql"], data["sql"])
		# Dropping the rows collapses the payload far below the original 265KB+.
		self.assertLess(_size(out), _size(data) // 4)

	def test_scalar_row_list(self):
		# A bare list of scalars (strings) over budget truncates like any row list.
		data = ["s" * 200 for _ in range(3000)]
		out, ev = enforce_result_budget(data, tool="get_list")
		self.assertEqual(ev["kind"], "truncated")
		self.assertTrue(out["_truncated"])
		self.assertLess(out["shown"], 3000)
		self.assertGreaterEqual(out["shown"], 1)
		self.assertEqual(out["rows"], data[: out["shown"]])
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)

	def test_decimal_and_date_rows(self):
		# default=str must carry Decimal/date/datetime through without raising.
		from datetime import date, datetime
		from decimal import Decimal

		data = [
			{
				"amt": Decimal("10.50"),
				"d": date(2026, 1, 1),
				"ts": datetime(2026, 1, 1, 12, 0, 0),
				"blob": "x" * 50,
			}
			for _ in range(3000)
		]
		out, ev = enforce_result_budget(data, tool="query")
		self.assertEqual(ev["kind"], "truncated")
		self.assertTrue(out["_truncated"])
		self.assertGreaterEqual(out["shown"], 1)
		self.assertLess(out["shown"], 3000)
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)

	def test_empty_rows_huge_sql(self):
		# n == 0 rows but the sql alone is over budget: uncapped_nonrow, no crash.
		data = {"sql": "x" * (MAX_RESULT_CHARS + 5000), "rows": []}
		out, ev = enforce_result_budget(data, tool="query")
		self.assertEqual(ev["kind"], "uncapped_nonrow")
		self.assertEqual(ev["total"], 0)
		self.assertEqual(out["rows"], [])
		self.assertEqual(out["shown"], 0)
		self.assertTrue(out["_truncated"])
