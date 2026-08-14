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

	def test_query_huge_sql_not_falsely_truncated(self):
		data = {"sql": "x" * (MAX_RESULT_CHARS + 5000), "rows": [{"n": 1}]}
		out, ev = enforce_result_budget(data, tool="query")
		self.assertIs(out, data)
		self.assertNotIn("_truncated", out)
		self.assertEqual(ev["kind"], "uncapped_nonrow")

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
		self.assertIs(out, data)
		self.assertIsNone(ev)

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
		self.assertGreaterEqual(out["shown"], 1)
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
