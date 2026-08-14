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

		out, ev = enforce_result_budget([{"x": Boom()}], tool="get_list")
		self.assertIsNotNone(out)

	def test_cjk_not_overcounted(self):
		data = [{"name": "客户名称" * 3} for _ in range(80)]
		out, ev = enforce_result_budget(data, tool="get_list")
		self.assertIsNone(ev)
