"""Tests for v2_22: stored query-tool dashboard sources move ``fields`` to ``select``.

The query tool's key allowlist rejects ``fields`` (get_list's column key), so a
saved query source carrying it would error per tile after upgrade. The patch
renames it, and must leave get_list sources, specs that already use ``select``,
and unparseable specs alone.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.tools.query import _validate_spec_shape

SOURCE = "Jarvis Dashboard Source"


class TestQuerySourceFieldsToSelect(FrappeTestCase):
	def _row(self, tool, spec):
		# db_insert, not insert: the parent dashboard's validate would reject the
		# legacy shape this patch exists to repair, and that is exactly the stored
		# data it must handle.
		doc = frappe.get_doc(
			{
				"doctype": SOURCE,
				"parent": "v2-22-patch-test",
				"parenttype": "Jarvis Dashboard",
				"parentfield": "sources",
				"idx": 1,
				"source_name": frappe.generate_hash(length=8),
				"tool": tool,
				"spec": spec if isinstance(spec, str) else json.dumps(spec),
			}
		)
		doc.db_insert()
		self.addCleanup(frappe.db.delete, SOURCE, {"name": doc.name})
		return doc.name

	def _spec(self, name):
		return frappe.db.get_value(SOURCE, name, "spec")

	def _run(self):
		from jarvis.patches.v2_22_query_source_fields_to_select import execute

		execute()

	def test_query_source_with_fields_moves_to_select_keeping_other_keys(self):
		name = self._row(
			"query",
			{
				"from": "ToDo",
				"fields": ["name", "status"],
				"where": [{"field": "status", "op": "=", "value": "Open"}],
				"limit": 5,
			},
		)
		self._run()
		spec = json.loads(self._spec(name))
		self.assertEqual(
			spec,
			{
				"from": "ToDo",
				"select": ["name", "status"],
				"where": [{"field": "status", "op": "=", "value": "Open"}],
				"limit": 5,
			},
		)
		_validate_spec_shape(spec)  # the migrated spec passes the new allowlist

	def test_query_source_already_using_select_is_untouched(self):
		raw = json.dumps({"from": "ToDo", "select": ["name"]})
		name = self._row("query", raw)
		self._run()
		self.assertEqual(self._spec(name), raw)

	def test_query_source_with_both_keys_is_left_for_the_author(self):
		raw = json.dumps({"from": "ToDo", "select": ["name"], "fields": ["status"]})
		name = self._row("query", raw)
		self._run()
		self.assertEqual(self._spec(name), raw)

	def test_get_list_source_keeps_fields(self):
		raw = json.dumps({"doctype": "ToDo", "fields": ["name"]})
		name = self._row("get_list", raw)
		self._run()
		self.assertEqual(self._spec(name), raw)

	def test_unparseable_spec_is_skipped_and_logged(self):
		bad = self._row("query", '{"from": "ToDo", "fields": [')
		not_object = self._row("query", '["fields"]')
		before = frappe.db.count("Error Log")
		self._run()
		self.assertEqual(self._spec(bad), '{"from": "ToDo", "fields": [')
		self.assertEqual(self._spec(not_object), '["fields"]')
		self.assertEqual(frappe.db.count("Error Log"), before + 1)

	def test_rerun_is_a_no_op(self):
		name = self._row("query", {"from": "ToDo", "fields": ["name"]})
		self._run()
		first = self._spec(name)
		self._run()
		self.assertEqual(self._spec(name), first)
		self.assertEqual(json.loads(first), {"from": "ToDo", "select": ["name"]})
