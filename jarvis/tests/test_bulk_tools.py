"""Bulk (list-param) overload behaviour for the write tools + the gating helpers.

The shared atomic engine (run_atomic_batch: savepoint rollback + callback-queue
restore) is already exercised by test_create_docs; here we verify each tool's
bulk branch wires into it (lean return, all-or-nothing rollback, validation) and
that the api gate treats a bulk call correctly (never auto-applies create/update;
routes consequential bulk writes to described-intent).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.api import (
	_bulk_targets,
	_describe_call,
	_is_bulk_call,
	_pending_preview,
)
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools.create_doc import create_doc
from jarvis.tools.delete_doc import delete_doc
from jarvis.tools.submit_doc import submit_doc
from jarvis.tools.update_doc import update_doc

_BAD_USER = "no-such-user@invalid.example"


class TestCreateDocBulk(FrappeTestCase):
	def test_docs_batch_creates_all_lean(self):
		out = create_doc(
			docs=[
				{"doctype": "ToDo", "values": {"description": "jbulk-create-a"}},
				{"doctype": "ToDo", "values": {"description": "jbulk-create-b"}},
			]
		)
		self.assertEqual(len(out["created"]), 2)
		self.assertTrue(frappe.db.exists("ToDo", {"description": "jbulk-create-a"}))

	def test_docs_batch_atomic_rollback(self):
		with self.assertRaises(Exception):
			create_doc(
				docs=[
					{"doctype": "ToDo", "values": {"description": "jbulk-create-ok"}},
					{
						"doctype": "ToDo",
						"values": {"description": "jbulk-create-bad", "assigned_by": _BAD_USER},
					},
				]
			)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "jbulk-create-ok"}))


class TestUpdateDocBulk(FrappeTestCase):
	def _todo(self, desc):
		return frappe.get_doc({"doctype": "ToDo", "description": desc}).insert().name

	def test_updates_batch_applies_all_lean(self):
		a, b = self._todo("jbulk-upd-a"), self._todo("jbulk-upd-b")
		out = update_doc(
			"ToDo",
			updates=[
				{"name": a, "changes": {"priority": "High"}},
				{"name": b, "changes": {"priority": "Low"}},
			],
		)
		self.assertEqual(out["doctype"], "ToDo")
		self.assertEqual(out["count"], 2)
		self.assertEqual(set(out["updated"]), {a, b})
		self.assertEqual(frappe.db.get_value("ToDo", a, "priority"), "High")

	def test_updates_batch_atomic_rollback(self):
		a, b = self._todo("jbulk-upd-ok"), self._todo("jbulk-upd-bad")
		with self.assertRaises(Exception):
			update_doc(
				"ToDo",
				updates=[
					{"name": a, "changes": {"priority": "High"}},
					{"name": b, "changes": {"assigned_by": _BAD_USER}},  # bad Link -> save fails
				],
			)
		# first update must have rolled back
		self.assertNotEqual(frappe.db.get_value("ToDo", a, "priority"), "High")

	def test_empty_updates_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			update_doc("ToDo", updates=[])

	def test_update_item_missing_name_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			update_doc("ToDo", updates=[{"changes": {"priority": "High"}}])


class TestDeleteDocBulk(FrappeTestCase):
	def _todo(self, desc):
		return frappe.get_doc({"doctype": "ToDo", "description": desc}).insert().name

	def test_deletes_batch_all_lean(self):
		a, b = self._todo("jbulk-del-a"), self._todo("jbulk-del-b")
		out = delete_doc("ToDo", names=[a, b])
		self.assertEqual(out["count"], 2)
		self.assertEqual(set(out["deleted"]), {a, b})
		self.assertFalse(frappe.db.exists("ToDo", a))

	def test_delete_batch_atomic_rollback_on_missing(self):
		a = self._todo("jbulk-del-ok")
		with self.assertRaises(Exception):
			delete_doc("ToDo", names=[a, "TODO-does-not-exist-xyz"])
		# the valid row must survive - nothing deleted
		self.assertTrue(frappe.db.exists("ToDo", a))


class TestSubmitDocBulkWiring(FrappeTestCase):
	"""submit needs a submittable fixture; the per-doc guards are covered by
	test_submit_doc. Here we mock _submit_one to verify the batch wiring:
	lean return, one call per name, all-or-nothing rollback."""

	def test_submit_batch_lean_return(self):
		with patch("jarvis.tools.submit_doc._submit_one") as m:
			m.return_value = object()
			out = submit_doc("Sales Invoice", names=["SI-1", "SI-2", "SI-3"])
		self.assertEqual(out, {"doctype": "Sales Invoice", "submitted": ["SI-1", "SI-2", "SI-3"], "count": 3})
		self.assertEqual(m.call_count, 3)

	def test_submit_batch_propagates_and_rolls_back(self):
		def _side(doctype, name):
			if name == "SI-2":
				raise InvalidArgumentError("boom on SI-2")
			return object()

		with patch("jarvis.tools.submit_doc._submit_one", side_effect=_side):
			with self.assertRaises(InvalidArgumentError):
				submit_doc("Sales Invoice", names=["SI-1", "SI-2", "SI-3"])

	def test_submit_empty_names_rejected(self):
		with self.assertRaises(InvalidArgumentError):
			submit_doc("Sales Invoice", names=[])


class TestBulkGating(FrappeTestCase):
	def test_is_bulk_call_detects_list_payloads(self):
		self.assertTrue(_is_bulk_call({"doctype": "X", "names": ["a"]}))
		self.assertTrue(_is_bulk_call({"updates": [{"name": "a", "changes": {}}]}))
		self.assertTrue(_is_bulk_call({"docs": [{"doctype": "X", "values": {}}]}))
		self.assertTrue(_is_bulk_call({"messages": [{"name": "a"}]}))

	def test_is_bulk_call_false_for_single(self):
		self.assertFalse(_is_bulk_call({"doctype": "X", "name": "a", "changes": {}}))
		self.assertFalse(_is_bulk_call({"doctype": "X", "names": []}))  # empty list is not bulk
		self.assertFalse(_is_bulk_call("not a dict"))

	def test_bulk_targets_extracts_names(self):
		self.assertEqual(_bulk_targets({"names": ["a", "b"]}), ["a", "b"])
		self.assertEqual(_bulk_targets({"updates": [{"name": "a"}, {"name": "b"}]}), ["a", "b"])
		self.assertEqual(_bulk_targets({"docs": [{"doctype": "ToDo"}]}), ["ToDo"])

	def test_describe_call_bulk_renders_count_and_targets(self):
		s = _describe_call("cancel_doc", {"doctype": "Purchase Order", "names": ["PO-1", "PO-2", "PO-3"]})
		self.assertIn("cancel_doc", s)
		self.assertIn("count=3", s)
		self.assertIn("doctype=Purchase Order", s)
		self.assertIn("PO-1", s)

	def test_describe_call_bulk_truncates_long_lists(self):
		names = [f"N-{i}" for i in range(15)]
		s = _describe_call("submit_doc", {"doctype": "ToDo", "names": names})
		self.assertIn("count=15", s)
		self.assertIn("more", s)  # "+5 more"

	def test_bulk_consequential_write_routes_to_described(self):
		# A bulk submit must NOT be sandbox-dry-run (that fires on_submit N times
		# at park); _pending_preview returns the described-intent card instead.
		out = _pending_preview("submit_doc", {"doctype": "Sales Invoice", "names": ["SI-1", "SI-2"]})
		self.assertTrue(out.get("described"))
		self.assertFalse(out.get("preview"))
		self.assertIn("count=2", out["summary"])


class TestCollabBulk(FrappeTestCase):
	"""Representative collab-tool batch (add_tag): all-or-nothing + per-record
	existence. Per-record PERMISSION is enforced in each tool's _one (verified
	by test_tier3_desk_actions + test_unshare_doc_share_boundary); here we lock
	the batch atomicity for the collab family."""

	def _todo(self, desc):
		return frappe.get_doc({"doctype": "ToDo", "description": desc}).insert().name

	def test_add_tag_batch_tags_all(self):
		from jarvis.tools.add_tag import add_tag

		a, b = self._todo("jbulk-tag-a"), self._todo("jbulk-tag-b")
		out = add_tag("ToDo", tag="q3-review", names=[a, b])
		self.assertEqual(out["count"], 2)
		self.assertEqual(out["tag"], "q3-review")
		self.assertEqual(set(out["tagged"]), {a, b})
		self.assertIn("q3-review", frappe.get_value("ToDo", a, "_user_tags") or "")

	def test_add_tag_batch_atomic_rollback_on_unknown(self):
		from jarvis.tools.add_tag import add_tag

		a = self._todo("jbulk-tag-ok")
		with self.assertRaises(Exception):
			add_tag("ToDo", tag="q3-review", names=[a, "ToDo-nope-xyz"])
		# the valid row must NOT have been tagged - whole batch rolled back
		self.assertNotIn("q3-review", frappe.get_value("ToDo", a, "_user_tags") or "")


class TestBulkWriteGating(FrappeTestCase):
	"""A bulk write to a normally-UNGATED light collab tool (add_tag/comment)
	must PARK a confirmation card (the plugin/persona promise one card per batch),
	even though the single form of those tools executes immediately."""

	def _todo(self, desc):
		return frappe.get_doc({"doctype": "ToDo", "description": desc}).insert().name

	def test_bulk_light_write_parks(self):
		a, b = self._todo("jbulk-gate-a"), self._todo("jbulk-gate-b")
		r = api._run_tool("add_tag", {"doctype": "ToDo", "names": [a, b], "tag": "jbulk-gate"})
		self.assertEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		# parked, not executed - neither doc got tagged
		self.assertNotIn("jbulk-gate", frappe.get_value("ToDo", a, "_user_tags") or "")

	def test_single_light_write_still_executes(self):
		a = self._todo("jbulk-gate-single")
		r = api._run_tool("add_tag", {"doctype": "ToDo", "name": a, "tag": "jbulk-single"})
		self.assertNotEqual((r.get("data") or {}).get("status"), "pending_confirmation")
		self.assertIn("jbulk-single", frappe.get_value("ToDo", a, "_user_tags") or "")

	def test_read_batch_is_capped(self):
		from jarvis.tools.get_doc import get_doc

		with self.assertRaises(InvalidArgumentError):
			get_doc("ToDo", names=[f"x-{i}" for i in range(21)])

	def test_bulk_workflow_refuses_queue_in_background(self):
		from jarvis.tools.apply_workflow_action import apply_workflow_action

		with (
			patch("jarvis.tools.apply_workflow_action.get_workflow_name", return_value="WF"),
			patch("frappe.get_meta", return_value=frappe._dict(queue_in_background=1)),
		):
			with self.assertRaises(InvalidArgumentError):
				apply_workflow_action("Sales Invoice", action="Approve", names=["SI-1", "SI-2"])
