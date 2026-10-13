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
		# A document-shaped result from any tool but get_doc has no continuation to
		# offer, so it still passes through and is only counted.
		data = {"doctype": "Sales Invoice", "items": [{"x": "y" * 50} for _ in range(2000)]}
		out, ev = enforce_result_budget(data, tool="create_doc")
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

	def test_guard_metadata_wins_over_colliding_data_key(self):
		# A tool result carrying its own top-level "total"/"note" key must NOT
		# overwrite the guard's authoritative metadata (regression: the M6 reorder
		# copied data keys after the metadata, letting a collision clobber it).
		data = {"total": "SUMMARY", "note": "data note", "rows": _rows(3000, 50)}
		out, ev = enforce_result_budget(data, tool="query")
		self.assertTrue(out["_truncated"])
		self.assertEqual(out["total"], 3000)  # true row count, not "SUMMARY"
		self.assertIn("PARTIAL", out["note"])  # guard's note, not "data note"
		self.assertEqual(ev["total"], 3000)
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)


def _doc(rows=300, terms=20_000, name="SINV-0001"):
	"""A get_doc-shaped invoice: header scalars, a long text field, a big and a
	small child table whose rows carry what as_dict gives every child row."""

	def row(table, child, i, width):
		return {
			"name": f"{table}-{i}",
			"idx": i + 1,
			"parent": name,
			"parentfield": table,
			"parenttype": "Sales Invoice",
			"doctype": child,
			"blob": "r" * width,
		}

	return {
		"name": name,
		"doctype": "Sales Invoice",
		"customer": "Acme",
		"total": 123.5,
		"note": "a field called note",
		"terms": "T" * terms,
		"items": [row("items", "Sales Invoice Item", i, 300) for i in range(rows)],
		"taxes": [row("taxes", "Sales Taxes and Charges", i, 50) for i in range(3)],
		"payments": [],
	}


class TestDocumentBudget(unittest.TestCase):
	"""F11 (#715): an over-budget get_doc document is bounded, keeps its shape,
	says what was cut and how to read it."""

	def bound(self, data):
		out, ev = enforce_result_budget(data, tool="get_doc")
		self.assertEqual(ev["kind"], "bounded_document")
		self.assertLessEqual(_size(out), MAX_RESULT_CHARS)
		self.assertEqual(json.loads(json.dumps(out)), out)  # plain, valid JSON
		self.assertEqual(list(out)[:2], ["_truncated", "_partial"])  # survive a head slice
		self.assertIs(out["_truncated"], True)
		return out, ev

	def test_document_over_budget_is_bounded_with_its_shape(self):
		data = _doc()
		out, ev = self.bound(data)
		partial = out["_partial"]
		self.assertEqual(set(out) - {"_truncated", "_partial"}, set(data))
		for key in ("name", "doctype", "customer", "total", "note", "payments"):
			self.assertEqual(out[key], data[key])  # never clobbered by guard metadata
		self.assertEqual(out["taxes"], data["taxes"])  # small table kept whole
		shown = partial["child_tables"]["items"]["returned"]
		self.assertGreater(shown, 0)
		self.assertEqual(out["items"], data["items"][:shown])  # whole rows, in order
		self.assertEqual(
			partial["child_tables"]["items"],
			{
				"returned": shown,
				"total": 300,
				"has_more": True,
				"next_start": shown,
				"next_call": {
					"tool": "get_doc",
					"args": {
						"doctype": "Sales Invoice",
						"name": "SINV-0001",
						"child_table": "items",
						"start": shown,
					},
				},
			},
		)
		self.assertNotIn("taxes", partial["child_tables"])
		self.assertTrue(out["terms"].startswith("T" * 2000))
		self.assertIn("[TRUNCATED", out["terms"])
		self.assertEqual(
			partial["text_fields"]["terms"],
			{
				"returned_chars": 2000,
				"total_chars": 20_000,
				"next_call": {
					"tool": "get_doc",
					"args": {"doctype": "Sales Invoice", "name": "SINV-0001", "fields": ["terms"]},
				},
			},
		)
		self.assertIn("PARTIAL", partial["note"])
		self.assertEqual((ev["shown"], ev["total"]), (shown + 3, 303))
		self.assertEqual(ev["original_chars"], _size(data))

	def test_document_under_budget_is_untouched(self):
		data = _doc(rows=5, terms=100)
		out, ev = enforce_result_budget(data, tool="get_doc")
		self.assertIs(out, data)
		self.assertIsNone(ev)

	def test_text_only_document_shows_as_much_text_as_fits(self):
		# The audit's probe: a 100,040-char document crossed the budget unchanged.
		data = {"name": "N-1", "doctype": "Note", "content": "x" * 100_040}
		out, _ = self.bound(data)
		entry = out["_partial"]["text_fields"]["content"]
		self.assertEqual(entry["total_chars"], 100_040)
		self.assertGreater(entry["returned_chars"], 30_000)
		# Longer than any one result: no call could return it whole, so none is offered.
		self.assertNotIn("next_call", entry)
		self.assertTrue(out["content"].startswith("x" * entry["returned_chars"] + " [TRUNCATED"))

	def test_tables_share_the_budget(self):
		data = _doc(rows=200, terms=10)
		data["taxes"] = [dict(r, name=f"t-{i}", parentfield="taxes") for i, r in enumerate(data["items"])]
		out, _ = self.bound(data)
		tables = out["_partial"]["child_tables"]
		self.assertGreater(tables["items"]["returned"], 0)
		self.assertLessEqual(abs(tables["items"]["returned"] - tables["taxes"]["returned"]), 1)

	def test_document_with_a_table_called_rows_is_still_a_document(self):
		data = {
			"name": "X-1",
			"doctype": "Odd",
			"rows": [{"name": f"r{i}", "blob": "b" * 200} for i in range(500)],
		}
		out, _ = self.bound(data)
		self.assertNotIn("shown", out)
		self.assertEqual(out["_partial"]["child_tables"]["rows"]["total"], 500)

	def test_batch_keeps_whole_documents_and_names_the_rest(self):
		docs = [_doc(rows=30, terms=10, name=f"SINV-{i}") for i in range(6)]
		data = {"doctype": "Sales Invoice", "docs": docs, "count": 6}
		out, ev = self.bound(data)
		shown = out["_partial"]["docs"]["returned"]
		self.assertGreater(shown, 0)
		self.assertLess(shown, 6)
		self.assertEqual(out["docs"], docs[:shown])
		self.assertEqual(out["count"], shown)
		self.assertEqual(
			out["_partial"]["docs"]["next_call"],
			{
				"tool": "get_doc",
				"args": {"doctype": "Sales Invoice", "names": [f"SINV-{i}" for i in range(shown, 6)]},
			},
		)
		self.assertEqual((ev["shown"], ev["total"]), (shown, 6))

	def test_batch_whose_first_document_alone_is_too_big_cuts_that_document(self):
		docs = [_doc(name="SINV-A"), _doc(rows=2, terms=10, name="SINV-B")]
		out, _ = self.bound({"doctype": "Sales Invoice", "docs": docs, "count": 2})
		self.assertEqual(len(out["docs"]), 1)
		self.assertIs(out["docs"][0]["_truncated"], True)
		self.assertEqual(out["docs"][0]["name"], "SINV-A")
		self.assertEqual(out["_partial"]["docs"]["next_call"]["args"]["names"], ["SINV-B"])

	def test_child_table_page_is_cut_like_a_list_page(self):
		rows = [{"name": f"r{i}", "idx": i + 1, "blob": "b" * 2000} for i in range(40, 60)]
		data = {
			"list_contract": "list-page-v1",
			"doctype": "Sales Invoice",
			"name": "SINV-0001",
			"child_table": "items",
			"rows": rows,
			"coverage": {
				"start": 40,
				"limit": 20,
				"returned": 20,
				"total": 300,
				"has_more": True,
				"next_start": 60,
			},
			"note": "page",
		}
		out, ev = enforce_result_budget(data, tool="get_doc")
		self.assertEqual(ev["kind"], "truncated")
		shown = len(out["rows"])
		self.assertEqual(out["rows"], rows[:shown])
		self.assertEqual(out["coverage"]["next_start"], 40 + shown)
		self.assertEqual(out["coverage"]["total"], 300)
		self.assertEqual(out["child_table"], "items")
