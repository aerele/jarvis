"""The plan and evaluator rules that the first review wave added.

* A schema fact (a column list, "a master has a restricted row") lives for one request or job, never longer.
* The plan build checks the pins first, and any other error in it sends the doctype to the per-document loop.
* Each block of the evaluator that copies Frappe cites the Frappe lines it copies, and the cited lines exist.
* The two modules cite no plan section or acceptance-criterion id.
"""

import ast
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from pymysql.err import OperationalError

from jarvis.payment_calendar import (
	batch_permission,
	batch_permission_eval,
	batch_permission_plan,
	compatibility,
)
from jarvis.tests.batch_permission_fixtures import (
	SI,
	BatchCase,
	meta_patched,
	oracle,
	session_as,
	switch_on,
)

SI_TABLE = "tabSales Invoice"


@contextmanager
def old_schema(table: str, column: str):
	"""Reads of information_schema for `table` miss `column`: the schema as it was before a Custom Field."""
	real = frappe.db.sql

	def sql(query, *args, **kwargs):
		rows = real(query, *args, **kwargs)
		values = args[0] if args else kwargs.get("values")
		if "information_schema.COLUMNS" in str(query) and values == (table,):
			return [row for row in rows if row["column"] != column]
		return rows

	with patch.object(frappe.db, "sql", sql):
		yield


@contextmanager
def request_end():
	"""What Frappe does when a request or job ends: every `frappe.local` value that the block set is gone."""
	batch_permission_plan._forget_facts()
	before = {key for key, _ in frappe.local}
	try:
		yield
	finally:
		for key in {key for key, _ in frappe.local} - before:
			delattr(frappe.local, key)


@contextmanager
def no_restricted_master():
	"""The scan for "has this master a restricted row" answers none: the state before a master was restricted."""
	real = frappe.db.sql

	def sql(query, *args, **kwargs):
		if "`restrict_to_companies` = 1 LIMIT 1" in str(query):
			return []
		return real(query, *args, **kwargs)

	with patch.object(frappe.db, "sql", sql):
		yield


def counting_sql(match: str):
	"""A `frappe.db.sql` wrapper that counts the statements that contain `match`."""
	real = frappe.db.sql
	seen: list[str] = []

	def sql(query, *args, **kwargs):
		if match in str(query):
			seen.append(str(query))
		return real(query, *args, **kwargs)

	return seen, patch.object(frappe.db, "sql", sql)


class TestSchemaFactsLiveOneRequest(BatchCase):
	def visible(self, user, names=None, doctype=SI):
		names = set(names or self.f.names(doctype))
		with session_as(user):
			plan = batch_permission.batch_plan(doctype)
			return plan, batch_permission._evaluate_batch(doctype, names, "read", plan).visible

	def test_a_column_added_after_the_column_memo_was_built_is_seen_by_the_next_request(self):
		f = self.f
		names = set(f.names(SI))
		with session_as(f.u1):
			expected = oracle(SI, names, "read")  # the real hook hides the invoice of the restricted customer
		self.assertNotIn(f.si_restr_b, expected)
		with request_end():
			with old_schema(SI_TABLE, "customer"):
				_, stale = self.visible(f.u1, names)
			# premise: the memo of the old schema lets the batch skip the restricted-master hook
			self.assertIn(f.si_restr_b, stale)
		with request_end():
			plan, got = self.visible(f.u1, names)
		self.assertEqual((plan.mode, plan.reason), ("batch", None))
		self.assertEqual(got, expected)

	def test_no_module_keeps_a_column_list_beyond_the_request(self):
		with request_end():
			batch_permission_plan._table_columns("tabDocShare")
		for module in (batch_permission_plan, batch_permission_eval):
			for name, value in vars(module).items():
				if isinstance(value, dict):
					keys = [part for key in value for part in (key if isinstance(key, tuple) else (key,))]
					self.assertNotIn("tabDocShare", keys, f"{module.__name__}.{name}")

	def test_a_column_list_is_read_once_in_a_request(self):
		with request_end():
			seen, counting = counting_sql("information_schema.COLUMNS")
			with counting:
				for _ in range(3):
					batch_permission_plan._table_columns("tabDocShare")
		self.assertEqual(len(seen), 1)

	def test_a_restricted_master_added_after_the_request_began_is_seen_by_the_next_one(self):
		f = self.f
		names = {f.si_restr_b, f.si_plain}
		with request_end():
			with no_restricted_master():
				_, hidden = self.visible(f.u1, names)
			self.assertIn(f.si_restr_b, hidden)  # premise: a memo of "none restricted" skips the hook
		with request_end():
			_, got = self.visible(f.u1, names)
		with session_as(f.u1):
			self.assertEqual(got, oracle(SI, names, "read"))

	def test_the_restricted_scan_runs_once_per_master_in_a_request(self):
		f = self.f
		names = set(f.names(SI))
		with request_end():
			seen, counting = counting_sql("`restrict_to_companies` = 1 LIMIT 1")
			with counting:
				self.visible(f.u1, names)
				self.visible(f.u1, names)
				self.visible(f.u1, names)
		scanned = [statement for statement in seen if "tabCustomer" in statement]
		self.assertEqual(len(scanned), 1, seen)

	def test_the_re_check_of_a_mismatch_reads_the_facts_again(self):
		f = self.f
		names = set(f.names(SI))
		with request_end():
			seen, counting = counting_sql("information_schema.COLUMNS")
			with counting, session_as(f.u1):
				plan = batch_permission.batch_plan(SI)
				first = batch_permission._evaluate_batch(SI, names, "read", plan)
				before = len(seen)
				batch_permission._evaluate_batch(SI, names, "read", plan, chunks=first.chunks, fresh=True)
		self.assertGreater(len(seen), before)


class TestPlanBuildFailsSafe(BatchCase):
	def plan(self, user=None):
		with session_as(user or self.f.u1):
			return batch_permission.batch_plan(SI)

	def assert_plan_error(self):
		f = self.f
		names = set(f.names(SI))
		events = MagicMock()
		with (
			switch_on(),
			session_as(f.u1),
			patch.object(batch_permission, "queue_event", events),
			patch.object(batch_permission, "_evaluate_batch") as batch,
		):
			plan = batch_permission.batch_plan(SI)
			got = batch_permission.batch_visible(SI, set(names), "read")
			expected = oracle(SI, names, "read")
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "plan_error"))
		batch.assert_not_called()
		self.assertEqual(got, expected)
		return events

	def test_an_error_in_a_plan_helper_takes_the_loop(self):
		with patch.object(batch_permission, "_mro_reason", side_effect=RuntimeError("secret detail")):
			events = self.assert_plan_error()
		operations = {call.args[0] for call in events.call_args_list}
		self.assertEqual(operations, {"pcal_permission_plan_error"})
		message = events.call_args_list[0].args[1]
		self.assertIn("RuntimeError", message)
		self.assertNotIn("secret detail", message)

	def test_a_database_error_in_the_column_read_takes_the_loop(self):
		with patch.object(batch_permission, "_table_columns", side_effect=OperationalError(1146, "gone")):
			self.assert_plan_error()

	def test_a_frappe_that_rejects_the_include_computed_keyword_takes_the_loop(self):
		"""Frappe 15: `Meta.get_table_fields(self)` has no `include_computed`."""
		with meta_patched(SI, get_table_fields=lambda: []):
			self.assert_plan_error()

	def test_a_missing_frappe_function_takes_the_loop(self):
		"""Frappe 15 has no `get_lazy_controller`. Frappe's own check needs it, so only the plan is checked."""
		events = MagicMock()
		with (
			patch.object(frappe.model.document, "get_lazy_controller", side_effect=AttributeError("gone")),
			patch.object(batch_permission, "queue_event", events),
		):
			plan = self.plan()
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "plan_error"))
		self.assertEqual(events.call_args.args[0], "pcal_permission_plan_error")

	def test_a_stop_in_the_plan_build_is_not_swallowed(self):
		with (
			patch.object(batch_permission, "_mro_reason", side_effect=TimeoutError("stop")),
			session_as(self.f.u1),
			self.assertRaises(TimeoutError),
		):
			batch_permission.batch_plan(SI)

	def test_the_pins_are_checked_before_any_version_specific_api(self):
		events = MagicMock()
		layout = MagicMock(side_effect=AssertionError("the layout was built"))
		with (
			patch.object(
				compatibility, "permission_pin_drift", return_value=["frappe.permissions.get_doc_permissions"]
			),
			patch.object(batch_permission, "queue_event", events),
			patch.object(batch_permission_plan, "_Layout", layout),
			patch.object(frappe.model.document, "get_lazy_controller", side_effect=AttributeError("gone")),
		):
			plan = self.plan()
		self.assertEqual((plan.mode, plan.reason), ("per_doc", "pin_drift"))
		layout.assert_not_called()
		self.assertEqual(events.call_args.args[0], "pcal_permission_pin_drift")

	def test_a_plan_that_builds_is_not_an_error(self):
		plan = self.plan()
		self.assertEqual((plan.mode, plan.reason), ("batch", None))


FRAPPE = Path(frappe.get_app_path("frappe"))
MIRROR = re.compile(
	r"# Mirrors frappe/(?P<path>[\w/]+\.py):(?P<qualname>[\w.]+) L(?P<first>\d+)-(?P<last>\d+) "
	r"\((?P<version>[\d.]+), pin (?P<pin>[\w.]+)\)"
)


def definition_lines(source: Path, qualname: str) -> tuple[int, int]:
	"""First and last line of the function or method `qualname` of the file `source`."""
	node: ast.AST = ast.parse(source.read_text())
	for part in qualname.split("."):
		node = next(
			child
			for child in ast.walk(node)
			if isinstance(child, ast.FunctionDef | ast.ClassDef) and child.name == part
		)
	return node.lineno, node.end_lineno


def comment_above(lines: list[str], number: int) -> str:
	"""The block of `#` lines right above the 1-based line `number`."""
	block = []
	index = number - 2
	while index >= 0 and lines[index].strip().startswith("#"):
		block.append(lines[index])
		index -= 1
	return "\n".join(reversed(block))


class TestTranscribedBlocksCiteFrappe(BatchCase):
	def mirrors(self):
		text = "".join(
			Path(module.__file__).read_text() for module in (batch_permission_eval, batch_permission_plan)
		)
		return [match.groupdict() for match in MIRROR.finditer(text)]

	def test_every_cited_range_lies_inside_the_cited_function_of_the_installed_frappe(self):
		found = self.mirrors()
		self.assertGreaterEqual(len(found), 10)
		for cite in found:
			with self.subTest(cite=cite["qualname"], first=cite["first"]):
				first, last = int(cite["first"]), int(cite["last"])
				start, end = definition_lines(FRAPPE / cite["path"], cite["qualname"])
				self.assertLessEqual(start, first)
				self.assertLessEqual(first, last)
				self.assertLessEqual(last, end)
				# The installed source is the judge of the ranges (above), so a point release of the same
				# major line is fine: CI's image and a developer's bench rarely carry the same patch level.
				# A cite of another major line is not.
				self.assertEqual(cite["version"].split(".")[0], frappe.__version__.split(".")[0])

	def test_every_cited_pin_is_a_pinned_name(self):
		pinned = compatibility._PERMISSION_PINS["16"]
		for cite in self.mirrors():
			with self.subTest(pin=cite["pin"]):
				self.assertIn(cite["pin"], pinned)

	def test_every_function_that_copies_frappe_cites_it(self):
		copies = {
			batch_permission_eval: (
				"_visible", "_verdict", "_permissions", "_hooks_allow", "_count_user_permission_denial",
				"_parent_rows", "_child_rows", "_shares", "get", "get_all_children",
			),
			batch_permission_plan: ("_hook_chain",),
		}  # fmt: skip
		for module, names in copies.items():
			source = Path(module.__file__).read_text()
			lines = source.splitlines()
			found = {
				node.name
				for node in ast.walk(ast.parse(source))
				if isinstance(node, ast.FunctionDef) and MIRROR.search(comment_above(lines, node.lineno))
			}
			for name in names:
				self.assertIn(name, found, f"{module.__name__}.{name}")


class TestNoPlanIds(BatchCase):
	ID = re.compile(r"\bplan \d|\bAC-[A-Z]?\d|\bE\d+\b|\bD\d+\b|\bS\d[ab]?\b|\bA1\b|\(\d+\.\d+\)|\brows? \d+")

	def test_the_two_modules_cite_no_plan_section_or_acceptance_id(self):
		for module in (batch_permission_plan, batch_permission_eval):
			for number, line in enumerate(Path(module.__file__).read_text().splitlines(), 1):
				self.assertIsNone(self.ID.search(line), f"{module.__name__}:{number}: {line}")
