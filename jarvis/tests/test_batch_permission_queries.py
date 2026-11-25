"""The batch reads in chunks (statement count) and every statement uses an index (EXPLAIN)."""

from unittest.mock import patch

import frappe

from jarvis.payment_calendar import batch_permission, permissions
from jarvis.tests.batch_permission_fixtures import SI, BatchCase, session_as, switch_on


class TestBatchPermissionQueries(BatchCase):
	def run_batch(self, names, chunk=2):
		"""The statements of one evaluation with chunks of `chunk` names, and the helper calls it made."""
		with switch_on(), session_as(self.f.u1), patch.object(batch_permission, "CHUNK", chunk):
			plan = batch_permission.batch_plan(SI)
			with (
				patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql,
				patch.object(batch_permission, "_lazy_doc") as lazy,
				patch.object(permissions, "_visible_names_per_doc") as loop,
			):
				result = batch_permission._evaluate_batch(SI, set(names), "read", plan, fresh=True)
		self.assertEqual((lazy.call_count, loop.call_count), (0, 0))
		return result, sql

	def plain_names(self, count):
		f = self.f
		names = [
			f.si_plain, f.si_b, f.si_twe_b, f.si_wrong_parentfield, f.si_wrong_parenttype,
			f.si_twe_empty, f.si_twe_a, f.si_represents_b, f.si_owner_b, f.si_other_owner_b,
		]  # fmt: skip
		return names[:count]

	def test_statements_grow_only_with_the_number_of_chunks(self):
		counts = {}
		for size in (2, 6, 10):
			result, sql = self.run_batch(self.plain_names(size))
			self.assertEqual(len(result.chunks), size // 2)
			counts[size] = sql.call_count
		per_chunk = (counts[6] - counts[2]) / 2
		self.assertGreater(per_chunk, 0)
		self.assertEqual(counts[10] - counts[6], counts[6] - counts[2])

	def test_every_batch_statement_has_an_index_plan_for_the_parent_and_the_child_table(self):
		_, sql = self.run_batch(self.plain_names(2))
		explained = {"c": 0, "p": 0, "s": 0, "m": 0, "tabSales Invoice": 0}
		for call in sql.call_args_list:
			query, values = call.args[0], call.args[1] if len(call.args) > 1 else None
			if not (query.startswith("SELECT") and "%(names)s" in query):
				continue
			for row in frappe.db.sql("EXPLAIN " + query, values, as_dict=True):
				if row["table"] in explained:
					explained[row["table"]] += 1
					self.assertNotEqual(row["type"], "ALL", (row["table"], query))
		# child rows (c), parent (p), the share statement (s) and the master join of the candidates (m)
		self.assertGreater(explained["c"], 0)
		self.assertGreater(explained["s"], 0)
		self.assertGreater(explained["m"], 0)
		self.assertGreater(explained["p"] + explained["tabSales Invoice"], 0)

	def test_each_restricted_master_candidate_adds_the_same_number_of_hook_statements(self):
		"""A candidate runs the real hook (`get_blocked_masters`): a fixed count of statements for each one."""
		f = self.f
		candidates = [f.si_restr_b, f.si_restr_a, f.si_restr_none, f.si_restr_case]
		base = self.plain_names(4)
		self.run_batch(
			base + candidates, chunk=500
		)  # Frappe fills its own column memo on the first hook call
		counts = {
			size: self.run_batch(base + candidates[:size], chunk=500)[1].call_count for size in (1, 2, 4)
		}
		per_candidate = counts[2] - counts[1]
		self.assertGreater(per_candidate, 0)
		self.assertEqual(counts[4] - counts[2], 2 * per_candidate)
