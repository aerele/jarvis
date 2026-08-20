import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.create_docs import create_docs


class TestCreateDocs(FrappeTestCase):
	def test_creates_all_and_returns_lean_shape(self):
		out = create_docs(
			[
				{"doctype": "ToDo", "values": {"description": "jarvis-batch-a"}},
				{"doctype": "ToDo", "values": {"description": "jarvis-batch-b"}},
			],
			notes=["Reuse existing User 'Administrator'"],
		)
		self.assertEqual(len(out["created"]), 2)
		self.assertEqual(out["created"][0]["doctype"], "ToDo")
		self.assertTrue(out["created"][0]["name"])
		self.assertEqual(out["notes"], ["Reuse existing User 'Administrator'"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": "jarvis-batch-a"}))
		self.assertTrue(frappe.db.exists("ToDo", {"description": "jarvis-batch-b"}))

	def test_rolls_back_whole_batch_on_failure(self):
		# Second doc has a bad Link (allocated_to -> a non-existent User), which
		# fails at insert AFTER the first doc inserted. The savepoint must undo it.
		# (allocated_to, not assigned_by: assigned_by has a fetch_from sibling on
		# ToDo - assigned_by_full_name - and Frappe 15's get_invalid_links silently
		# skips the invalid-link check for such fields; allocated_to has none, so
		# it raises on both majors.)
		with self.assertRaises(Exception):
			create_docs(
				[
					{"doctype": "ToDo", "values": {"description": "jarvis-batch-ok"}},
					{
						"doctype": "ToDo",
						"values": {
							"description": "jarvis-batch-bad",
							"allocated_to": "no-such-user@invalid.example",
						},
					},
				]
			)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "jarvis-batch-ok"}))
		self.assertFalse(frappe.db.exists("ToDo", {"description": "jarvis-batch-bad"}))

	def test_empty_list_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_docs([])

	def test_item_missing_values_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_docs([{"doctype": "ToDo"}])

	def test_protected_field_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			create_docs([{"doctype": "ToDo", "values": {"description": "x", "owner": "a@b.c"}}])

	def test_notes_default_empty(self):
		out = create_docs([{"doctype": "ToDo", "values": {"description": "jarvis-batch-n"}}])
		self.assertEqual(out["notes"], [])

	def test_failed_batch_leaves_no_queued_side_effects(self):
		# frappe.db.rollback(save_point=...) rolls back the rows but does NOT
		# clear the commit/rollback callback queues. Without the fix, the first
		# (successfully-inserted-then-rolled-back) doc's queued after_commit
		# callbacks survive and would fire on the request's real commit.
		# Bad Link uses allocated_to (not assigned_by) so it fails on Frappe 15
		# too - see test_rolls_back_whole_batch_on_failure for why.
		before = len(frappe.db.after_commit._functions)
		with self.assertRaises(Exception):
			create_docs(
				[
					{"doctype": "ToDo", "values": {"description": "jarvis-batch-cbq-ok"}},
					{
						"doctype": "ToDo",
						"values": {
							"description": "jarvis-batch-cbq-bad",
							"allocated_to": "no-such-user@invalid.example",
						},
					},
				]
			)
		self.assertEqual(len(frappe.db.after_commit._functions), before)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "jarvis-batch-cbq-ok"}))

	def test_batch_cap_rejected(self):
		docs = [{"doctype": "ToDo", "values": {"description": f"jarvis-batch-cap-{i}"}} for i in range(21)]
		with self.assertRaises(InvalidArgumentError):
			create_docs(docs)
