"""Trusted gateway event correlation fences delayed callbacks within a chain."""

import frappe

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tests._share_helpers import RECIPIENT


class TestCollaborationCallbacks(CollaborationCase):
	def setUp(self):
		super().setUp()
		self.bound, self.turn = self.running_turn()
		frappe.db.set_value(
			c.BINDING,
			self.bound.name,
			{"runtime_session_id": "qa-physical", "runtime_policy_hash": "qa-attested"},
		)

	def record(self, call="qa-call-1", tool="jarvis__get_doc", run=None):
		from jarvis.chat.collaboration_callbacks import record_gateway_tool

		return record_gateway_tool(run or self.turn.name, call, tool)

	def authorize(self, call="qa-call-1", tool="get_doc"):
		from jarvis.chat.collaboration_callbacks import authorize_callback

		return authorize_callback(self.bound.session_key, tool, call, wait_s=0)

	def test_observed_call_resolves_exact_actor_and_run(self):
		self.assertTrue(self.record())
		result = self.authorize()
		self.assertEqual(result.binding, self.bound)
		self.assertEqual(result.binding.actor, RECIPIENT)
		self.assertEqual(result.run_id, self.turn.name)

	def test_unknown_call_id_never_guesses_the_current_turn(self):
		with self.assertRaises(frappe.PermissionError):
			self.authorize()

	def test_same_call_id_cannot_change_tool_name(self):
		self.record()
		self.assertFalse(self.record(tool="jarvis__delete_doc"))
		with self.assertRaises(frappe.PermissionError):
			self.authorize(tool="delete_doc")

	def test_terminal_turn_cannot_admit_a_late_first_callback(self):
		self.record()
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "state", "done")
		with self.assertRaises(frappe.PermissionError):
			self.authorize()

	def test_cancel_intent_fences_new_tools_before_gateway_abort_finishes(self):
		self.record()
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "cancel_requested", 1)
		with self.assertRaises(frappe.PermissionError):
			self.authorize()

	def test_old_call_id_cannot_move_to_a_new_child_run_in_same_binding(self):
		from jarvis.chat.collaboration_bindings import insert_message, stamp

		self.record()
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "state", "done")
		seed = insert_message(
			self.bound, {"role": "user", "content": "confirmed continuation"}, run_id="b" * 32
		)
		child = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": "b" * 32,
				"conversation": self.conversation,
				"relay_target_id": "default",
				"state": "dispatching",
				"seed_message": seed.name,
				"dispatching_at": frappe.utils.now_datetime(),
				"collaboration_send_started_at": frappe.utils.now_datetime(),
			}
		)
		stamp(child, self.bound)
		child.insert(ignore_permissions=True)
		self.assertFalse(self.record(run=child.name))
		with self.assertRaises(frappe.PermissionError):
			self.authorize()
		self.assertTrue(self.record(call="new-child-call", run=child.name))
		self.assertEqual(self.authorize(call="new-child-call").run_id, child.name)

	def test_revoked_or_disabled_actor_cannot_use_a_valid_observed_call(self):
		self.record()
		frappe.db.set_value("User", RECIPIENT, "enabled", 0)
		with self.assertRaises(frappe.PermissionError):
			self.authorize()
		frappe.db.set_value("User", RECIPIENT, "enabled", 1)
		frappe.db.delete(c.SHARE, {"name": self.bound.share})
		with self.assertRaises(frappe.PermissionError):
			self.authorize()

	def test_unprovisioned_binding_cannot_admit_tools(self):
		self.record()
		frappe.db.set_value(c.BINDING, self.bound.name, "runtime_session_id", None)
		with self.assertRaises(frappe.PermissionError):
			self.authorize()

	def test_future_queued_turn_cannot_supply_a_callback_capability(self):
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "state", "queued")
		self.assertFalse(self.record())
		with self.assertRaises(frappe.PermissionError):
			self.authorize()

	def test_unknown_new_tool_and_private_memory_never_admit_callbacks(self):
		for tool in ("recall", "remember", "future_unknown_tool", "record_app_wiki"):
			with self.subTest(tool=tool):
				self.record(call=tool, tool="jarvis__" + tool)
				with self.assertRaises(frappe.PermissionError):
					self.authorize(call=tool, tool=tool)

	def test_every_registered_tool_has_an_explicit_shared_policy(self):
		from jarvis.chat.collaboration_tools import READ_TOOLS, REFUSED_TOOLS, WRITE_TOOLS, tool_kind
		from jarvis.tools.registry import list_tools

		self.assertEqual(set(list_tools()), READ_TOOLS | REFUSED_TOOLS | WRITE_TOOLS)
		self.assertFalse(READ_TOOLS & WRITE_TOOLS)
		self.assertFalse(READ_TOOLS & REFUSED_TOOLS)
		self.assertFalse(WRITE_TOOLS & REFUSED_TOOLS)
		self.assertEqual(tool_kind("future_unknown_tool"), "refused")

	def test_landed_cost_preview_is_read_only_and_bound_to_the_initiating_actor(self):
		from jarvis.chat.collaboration_tools import tool_kind, validate_targets

		self.assertEqual(tool_kind("preview_landed_cost_voucher"), "read")
		validate_targets(
			"preview_landed_cost_voucher",
			{"company": "Example", "receipts": [{"doctype": "Purchase Receipt", "name": "PR-1"}]},
		)
		self.assertTrue(self.record(tool="jarvis__preview_landed_cost_voucher"))
		permit = self.authorize(tool="preview_landed_cost_voucher")
		self.assertEqual(permit.binding.actor, RECIPIENT)
		self.assertEqual(permit.run_id, self.turn.name)

	def test_generic_tools_cannot_read_private_memory_or_chat_rows(self):
		from jarvis.chat.collaboration_tools import validate_targets

		for tool, args in (
			("get_doc", {"doctype": "Jarvis User Memory", "name": RECIPIENT}),
			("get_list", {"doctype": "Jarvis Chat Message"}),
			("query", {"spec": {"from": "Sales Invoice", "joins": [{"doctype": "Jarvis User Settings"}]}}),
			("create_doc", {"docs": [{"doctype": "Jarvis User Memory", "content": "private memory"}]}),
			(
				"run_method",
				{"method": "jarvis.chat.api.get_conversation", "args": {"conversation": "private"}},
			),
			(
				"run_method",
				{"method": "frappe.client.get", "args": {"doctype": "Jarvis User Memory", "name": RECIPIENT}},
			),
		):
			with self.subTest(tool=tool, args=args), self.assertRaises(frappe.PermissionError):
				validate_targets(tool, args)

	def test_business_records_and_ordinary_text_are_not_confused_with_private_context(self):
		from jarvis.chat.collaboration_tools import validate_targets

		validate_targets("get_doc", {"doctype": "Sales Invoice", "name": "ACC-SINV-2026-001"})
		validate_targets(
			"query", {"spec": {"from": "Sales Invoice", "joins": [{"doctype": "Sales Invoice Item"}]}}
		)
		validate_targets(
			"create_doc",
			{
				"doctype": "Note",
				"doc": {"title": "Jarvis tips", "content": 'Example: {"doctype":"Jarvis User Memory"}'},
			},
		)

	def test_docshare_and_attachment_fields_cannot_disguise_a_private_chat_target(self):
		from jarvis.chat.collaboration_tools import validate_targets

		for doctype, target in (("DocShare", "share_doctype"), ("File", "attached_to_doctype")):
			with self.subTest(doctype=doctype), self.assertRaises(frappe.PermissionError):
				validate_targets(
					"create_doc",
					{
						"doctype": doctype,
						"values": {
							target: "Jarvis Conversation",
							"share_name": self.conversation,
							"attached_to_name": self.conversation,
							"user": RECIPIENT,
						},
					},
				)

	def test_method_override_cannot_disguise_a_private_jarvis_endpoint(self):
		from unittest.mock import patch

		from jarvis.chat.collaboration_tools import validate_targets

		with patch("frappe.override_whitelisted_method", return_value="jarvis.chat.api.get_conversation"):
			with self.assertRaises(frappe.PermissionError):
				validate_targets("run_method", {"method": "frappe.client.get"})
