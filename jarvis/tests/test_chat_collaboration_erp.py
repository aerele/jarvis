"""Real framework ERP permission checks through authenticated shared callbacks."""

import uuid
from unittest.mock import patch

import frappe

from jarvis import api
from jarvis.chat import collaboration as c
from jarvis.chat.collaboration_callbacks import record_gateway_tool
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OWNER, RECIPIENT, as_user


class TestCollaborationERP(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		for user in (OWNER, RECIPIENT):
			frappe.get_doc("User", user).add_roles("Desk User")
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex))
		frappe.db.set_value(
			c.BINDING,
			self.bound.name,
			{"runtime_session_id": "physical-erp", "runtime_policy_hash": "verified"},
		)
		frappe.db.commit()
		self.headers = {"X-Jarvis-Token": "qa-authenticated"}
		for target in (
			patch("jarvis.api.validate_plugin_request", side_effect=lambda *_args: self.bound.session_key),
			patch("jarvis.api._get_header", side_effect=lambda name: self.headers.get(name, "")),
			patch("frappe.enqueue"),
			patch("frappe.publish_realtime"),
		):
			target.start()
			self.addCleanup(target.stop)

	def call(self, tool, args):
		call_id = "qa-erp-" + uuid.uuid4().hex
		self.headers["X-Jarvis-Tool-Call-Id"] = call_id
		record_gateway_tool(self.turn.name, call_id, "jarvis__" + tool)
		frappe.db.commit()
		return api.call_tool(tool, args)

	def note(self, owner, content):
		with as_user("Administrator"):
			doc = frappe.get_doc(
				{
					"doctype": "Note",
					"title": "Shared ERP " + uuid.uuid4().hex,
					"content": content,
					"public": 0,
					"seen_by": [{"user": "Administrator"}],
				}
			).insert()
			frappe.db.set_value("Note", doc.name, "owner", owner)
			frappe.db.commit()
		self.records.append(("Note", doc.name))
		return doc.name

	def test_owners_erp_access_is_never_borrowed_by_the_recipient(self):
		frappe.get_doc("User", OWNER).add_roles("System Manager")
		name = self.note(OWNER, "OWNER_PRIVATE_SENTINEL")
		with as_user(OWNER):
			self.assertTrue(frappe.has_permission("Note", "read", doc=frappe.get_doc("Note", name)))
		result = self.call("get_doc", {"doctype": "Note", "name": name})
		self.assertFalse(result["ok"], result)
		self.assertNotIn("OWNER_PRIVATE_SENTINEL", str(result))

	def test_recipient_reads_own_record_with_restricted_fields_masked(self):
		name = self.note(RECIPIENT, "Visible recipient content")
		self.assertTrue(frappe.get_doc("Note", name).seen_by)
		result = self.call("get_doc", {"doctype": "Note", "name": name})
		self.assertTrue(result["ok"], result)
		self.assertIn("Visible recipient content", result["data"]["content"])
		self.assertEqual(result["data"]["seen_by"], [])

	def test_reverse_privilege_does_not_let_owner_execute_as_the_recipient(self):
		name = self.note(RECIPIENT, "Recipient private record")
		self.assertTrue(self.call("get_doc", {"doctype": "Note", "name": name})["ok"])
		frappe.db.set_value("Jarvis Chat Turn", self.turn.name, "state", "done")
		self.bound, self.turn = self.running_turn(self.binding(uuid.uuid4().hex, actor=OWNER))
		frappe.db.set_value(
			c.BINDING,
			self.bound.name,
			{"runtime_session_id": "physical-owner", "runtime_policy_hash": "verified"},
		)
		frappe.db.commit()
		result = self.call("get_doc", {"doctype": "Note", "name": name})
		self.assertFalse(result["ok"], result)
		self.assertNotIn("Recipient private record", str(result))

	def invoice(self, company=None):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"name": "QA-SHARED-" + uuid.uuid4().hex,
				"docstatus": 0,
				"company": company,
				"owner": OWNER,
			}
		)
		doc.db_insert()
		self.records.append(("Sales Invoice", doc.name))
		frappe.db.commit()
		return doc.name

	def test_create_write_submit_and_cancel_refuse_without_recipient_erp_permission(self):
		name = self.invoice()
		for tool, args in (
			("create_doc", {"doctype": "Sales Invoice", "values": {"remarks": "forbidden"}}),
			("update_doc", {"doctype": "Sales Invoice", "name": name, "changes": {"remarks": "forbidden"}}),
			("submit_doc", {"doctype": "Sales Invoice", "name": name}),
			("cancel_doc", {"doctype": "Sales Invoice", "name": name}),
		):
			with self.subTest(tool=tool):
				result = self.call(tool, args)
				if result.get("ok") and (result.get("data") or {}).get("status") == "pending_confirmation":
					from jarvis.chat.collaboration_actions import confirm_action

					action = frappe.db.get_value(
						"Jarvis Pending Action",
						{
							"conversation": self.conversation,
							"collaboration_binding": self.bound.name,
							"run_id": self.turn.name,
							"owner_user": RECIPIENT,
							"tool": tool,
							"status": "Pending",
						},
						"name",
					)
					self.assertTrue(action)
					with as_user(RECIPIENT):
						result = confirm_action(self.conversation, action)
				self.assertFalse(result["ok"], result)
				self.assertEqual(result["error"]["code"], "PermissionDeniedError", result)
				self.assertEqual(frappe.db.get_value("Sales Invoice", name, "docstatus"), 0)
				self.assertNotEqual(frappe.db.get_value("Sales Invoice", name, "remarks"), "forbidden")
		self.assertEqual(frappe.db.count("Sales Invoice", {"name": ["like", "QA-SHARED-%"]}), 1)

	def test_current_company_user_permission_refuses_a_previously_allowed_read(self):
		frappe.get_doc("User", RECIPIENT).add_roles("Accounts User")
		companies = []
		for suffix in ("A", "B"):
			name = "Shared permission " + suffix + " " + uuid.uuid4().hex
			frappe.get_doc(
				{
					"doctype": "Company",
					"name": name,
					"company_name": name,
					"abbr": "Q" + suffix,
					"default_currency": "USD",
				}
			).db_insert()
			self.records.append(("Company", name))
			companies.append(name)
		name = self.invoice(companies[1])
		self.assertTrue(self.call("get_doc", {"doctype": "Sales Invoice", "name": name})["ok"])
		with as_user("Administrator"):
			permission = frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": RECIPIENT,
					"allow": "Company",
					"for_value": companies[0],
					"applicable_for": "Sales Invoice",
				}
			).insert()
		self.records.append(("User Permission", permission.name))
		frappe.db.commit()
		result = self.call("get_doc", {"doctype": "Sales Invoice", "name": name})
		self.assertFalse(result["ok"], result)
