"""User agents v1: the DocType rules, the endpoints, the chat binding and the sync."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe

from jarvis.chat import user_agent_sync, user_agents, user_agents_api
from jarvis.exceptions import AdminContractError, AdminRateLimitedError, AdminUnreachableError
from jarvis.tests._user_agent_helpers import DOCTYPE, OTHER, OWNER, UserAgentCase, _as

CONV = "Jarvis Conversation"
SESSION = "Jarvis Chat Session"


class TestUserAgentRules(UserAgentCase):
	def test_insert_sets_a_well_formed_agent_id_and_pending_sync(self):
		row = self.make_agent(synced=False)
		self.assertRegex(row.agent_id, r"^agent-u-[0-9a-f]{10}$")
		self.assertEqual(row.sync_status, "Pending")
		self.assertEqual(row.owner, OWNER)
		self.sync.assert_called()

	def test_agent_id_cannot_be_chosen_or_changed(self):
		row = self.make_agent(agent_id="agent-u-aaaaaaaaaa")
		self.assertNotEqual(row.agent_id, "agent-u-aaaaaaaaaa")
		with _as(OWNER):
			doc = frappe.get_doc(DOCTYPE, row.name)
			doc.agent_id = "agent-u-bbbbbbbbbb"
			doc.save()
		self.assertEqual(frappe.db.get_value(DOCTYPE, row.name, "agent_id"), row.agent_id)

	def test_administrator_and_guest_cannot_own_an_agent(self):
		for user in ("Administrator", "Guest"):
			with self.subTest(user=user), _as(user), self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{"doctype": DOCTYPE, "agent_name": "x", "instructions": "y", "tools_json": "[]"}
				).insert(ignore_permissions=True)

	def test_at_most_five_agents_per_owner(self):
		for i in range(user_agents.MAX_PER_OWNER):
			self.make_agent(agent_name=f"Agent {i}")
		with self.assertRaises(frappe.ValidationError):
			self.make_agent(agent_name="One too many")
		self.make_agent(OTHER, agent_name="Another owner still has room")

	def test_unpickable_or_private_tools_are_refused(self):
		for tools in (["recall"], ["record_agent_run"], ["no_such_tool"], "not a list"):
			with self.subTest(tools=tools), self.assertRaises(frappe.ValidationError):
				self.make_agent(tools_json=tools if isinstance(tools, str) else json.dumps(tools))

	def test_tool_ids_are_stored_bare_sorted_and_unique(self):
		row = self.make_agent(tools_json='["jarvis__get_list", "get_doc", "get_list"]')
		self.assertEqual(json.loads(row.tools_json), ["get_doc", "get_list"])

	def test_instructions_are_capped(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_agent(instructions="x" * 12001)

	def test_other_user_cannot_read_or_edit_an_agent(self):
		row = self.make_agent()
		with _as(OTHER):
			self.assertFalse(frappe.has_permission(DOCTYPE, "read", row.name))
			self.assertEqual(frappe.get_list(DOCTYPE, filters={"name": row.name}), [])
			for call in (
				lambda: user_agents_api.get_user_agent(row.name),
				lambda: user_agents_api.update_user_agent(row.name, agent_name="Mine now"),
				lambda: user_agents_api.start_user_agent_chat(row.name),
				lambda: user_agents_api.run_user_agent_now(row.name, prompt="go"),
				lambda: user_agents_api.delete_user_agent(row.name),
			):
				with self.assertRaises(frappe.PermissionError):
					call()

	def test_a_system_manager_cannot_edit_someone_elses_agent(self):
		row = self.make_agent()
		with self.assertRaises(frappe.PermissionError):
			doc = frappe.get_doc(DOCTYPE, row.name)
			doc.instructions = "Do something else"
			doc.save()

	def test_a_tool_call_cannot_change_an_agent(self):
		row = self.make_agent()
		frappe.local.jarvis_dispatch_depth = 1
		self.addCleanup(setattr, frappe.local, "jarvis_dispatch_depth", 0)
		with _as(OWNER), self.assertRaises(frappe.PermissionError):
			doc = frappe.get_doc(DOCTYPE, row.name)
			doc.tools_json = '["get_list", "delete_doc"]'
			doc.save()

	def test_schedule_needs_a_prompt_and_gets_a_next_run(self):
		row = self.make_agent()
		with _as(OWNER):
			with self.assertRaises(frappe.ValidationError):
				user_agents_api.set_user_agent_schedule(row.name, 1, schedule_frequency="daily")
			out = user_agents_api.set_user_agent_schedule(
				row.name, 1, schedule_frequency="daily", schedule_time="08:30:00", schedule_prompt="Summarise"
			)
			self.assertTrue(out["schedule"]["next_run_at"])
			off = user_agents_api.set_user_agent_schedule(row.name, 0)
		self.assertIsNone(off["schedule"]["next_run_at"])


class TestFlagOff(UserAgentCase):
	def test_every_endpoint_refuses_and_the_list_says_off(self):
		row = self.make_agent()
		self.flag(False)
		with _as(OWNER):
			self.assertEqual(user_agents_api.list_user_agents()["enabled"], False)
			self.assertEqual(user_agents_api.list_user_agents()["agents"], [])
			for call in (
				lambda: user_agents_api.get_user_agent(row.name),
				lambda: user_agents_api.create_user_agent("New", "Do it"),
				lambda: user_agents_api.update_user_agent(row.name, agent_name="x"),
				lambda: user_agents_api.delete_user_agent(row.name),
				lambda: user_agents_api.set_user_agent_enabled(row.name, 0),
				lambda: user_agents_api.set_user_agent_schedule(row.name, 0),
				lambda: user_agents_api.list_pickable_tools(),
				lambda: user_agents_api.start_user_agent_chat(row.name),
				lambda: user_agents_api.run_user_agent_now(row.name, prompt="go"),
				lambda: user_agents_api.retry_user_agent_sync(row.name),
			):
				with self.assertRaises(frappe.PermissionError):
					call()

	def test_a_bound_chat_cannot_send_with_the_flag_off(self):
		row = self.make_agent()
		with _as(OWNER):
			conversation = user_agents_api.start_user_agent_chat(row.name)["conversation"]
		self.flag(False)
		with self.assertRaises(frappe.PermissionError):
			user_agents.refuse_send(frappe.get_doc(CONV, conversation))


class TestEndpoints(UserAgentCase):
	def test_create_list_update_delete_round_trip(self):
		with _as(OWNER):
			made = user_agents_api.create_user_agent(
				"Stock watcher", "Tell me which items are below reorder level.", tools=["get_list"]
			)
			self.assertEqual(made["tools"], ["get_list"])
			self.assertEqual(made["sync"]["status"], "Pending")
			self.assertFalse(made["ready"])
			listed = user_agents_api.list_user_agents()
			self.assertEqual([a["name"] for a in listed["agents"]], [made["name"]])
			self.assertEqual(listed["max_agents"], 5)
			changed = user_agents_api.update_user_agent(made["name"], tools='["get_list", "get_doc"]')
			self.assertEqual(changed["tools"], ["get_doc", "get_list"])
			self.assertEqual(
				user_agents_api.delete_user_agent(made["name"]), {"ok": True, "name": made["name"]}
			)
		self.assertFalse(frappe.db.exists(DOCTYPE, made["name"]))

	def test_list_shows_only_the_callers_agents(self):
		self.make_agent()
		self.make_agent(OTHER)
		with _as(OTHER):
			agents = user_agents_api.list_user_agents()["agents"]
		self.assertEqual({a["agent_name"] for a in agents}, {"Overdue invoices"})
		self.assertEqual(len(agents), 1)

	def test_pickable_tools_are_grouped_and_exclude_private_tools(self):
		with _as(OWNER):
			out = user_agents_api.list_pickable_tools()
		ids = {t["id"] for g in out["groups"] for t in g["tools"]}
		self.assertIn("get_list", ids)
		self.assertIn("call_connector", ids)
		for private in ("recall", "remember", "record_agent_run", "get_engagement_config", "schedule_run"):
			self.assertNotIn(private, ids)
		self.assertEqual(out["groups"][0]["app"], "frappe")
		self.assertEqual(out["max_tools"], 64)


class TestStartChat(UserAgentCase):
	def test_start_chat_binds_a_new_conversation_to_the_agent(self):
		row = self.make_agent()
		with _as(OWNER):
			out = user_agents_api.start_user_agent_chat(row.name)
		self.assertEqual(out["agent_id"], row.agent_id)
		conv = frappe.db.get_value(
			CONV, out["conversation"], ["owner", "session_key", "user_agent"], as_dict=True
		)
		self.assertEqual(conv.owner, OWNER)
		self.assertEqual(conv.user_agent, row.agent_id)
		self.assertRegex(
			conv.session_key, rf"^agent:{row.agent_id}:dashboard:jarvis-chat-ua-owner-example-com-\d+$"
		)
		session = frappe.db.get_value(
			SESSION, {"session_key": conv.session_key}, ["user", "profile_agent_id"], as_dict=True
		)
		self.assertEqual(session.user, OWNER)
		self.assertEqual(session.profile_agent_id, row.agent_id)

	def test_an_unsynced_or_disabled_agent_cannot_chat(self):
		pending = self.make_agent(synced=False)
		off = self.make_agent(agent_name="Off", enabled=0)
		with _as(OWNER):
			for name in (pending.name, off.name):
				with self.assertRaises(user_agents.UserAgentUnavailableError):
					user_agents_api.start_user_agent_chat(name)

	def test_a_reminted_session_still_addresses_the_agent(self):
		row = self.make_agent()
		with patch.object(frappe.db, "commit"):
			key = user_agents.mint_for_conversation(row.agent_id, OWNER)
		self.assertTrue(key.startswith(f"agent:{row.agent_id}:dashboard:"))
		with self.assertRaises(frappe.PermissionError):
			user_agents.mint_for_conversation(row.agent_id, OTHER)

	def test_new_chat_never_reuses_an_empty_agent_chat(self):
		from jarvis.chat import api as chat_api

		row = self.make_agent()
		with _as(OWNER), patch.object(frappe.db, "commit"):
			bound = user_agents_api.start_user_agent_chat(row.name)["conversation"]
			fresh = chat_api.create_or_focus_empty()
		self.assertNotEqual(fresh, bound)

	def test_an_agent_chat_cannot_be_shared(self):
		from jarvis.chat import sharing

		row = self.make_agent()
		with _as(OWNER):
			conversation = user_agents_api.start_user_agent_chat(row.name)["conversation"]
			with self.assertRaises(frappe.PermissionError):
				sharing._set_shares(conversation, {OTHER})


class TestSync(UserAgentCase):
	def test_payload_shape_matches_the_fleet_contract(self):
		on = self.make_agent(tools_json='["get_list"]', allow_shell=1)
		self.make_agent(agent_name="Off", enabled=0)
		payload = user_agent_sync.build_payload()
		self.assertEqual(len(payload), 1)
		entry = payload[0]
		self.assertEqual(set(entry), {"id", "body", "tools_allow", "sandbox", "timeout_s"})
		self.assertEqual(entry["id"], on.agent_id)
		self.assertEqual(entry["tools_allow"], ["jarvis__get_list"])
		self.assertIs(entry["sandbox"], True)
		self.assertTrue(60 <= entry["timeout_s"] <= 5400)
		self.assertIn("List overdue Sales Invoices", entry["body"])
		self.assertIn("Ask before you create", entry["body"])
		self.assertLessEqual(len(entry["body"]), 20000)

	def test_success_marks_rows_synced(self):
		row = self.make_agent(synced=False)
		with (
			patch(
				"jarvis.admin_client.post_push_user_agents", return_value={"user_agents": [row.agent_id]}
			) as push,
			patch.object(frappe.db, "commit"),
		):
			user_agent_sync.run_sync()
		push.assert_called_once()
		self.assertEqual(push.call_args.kwargs["user_agents"][0]["id"], row.agent_id)
		state = frappe.db.get_value(
			DOCTYPE, row.name, ["sync_status", "sync_error", "synced_at"], as_dict=True
		)
		self.assertEqual((state.sync_status, state.sync_error), ("Synced", ""))
		self.assertTrue(state.synced_at)

	def test_typed_refusals_are_stored_with_a_sentence(self):
		cases = (
			(AdminContractError("off", code="user_agents_disabled"), "Failed", "user_agents_disabled"),
			(AdminContractError("busy", code="apply_in_progress"), "Pending", "apply_in_progress"),
			(AdminRateLimitedError("slow down", 60), "Pending", "RateLimitExceeded"),
			(AdminUnreachableError("502"), "Pending", "admin_unreachable"),
		)
		for exc, status, code in cases:
			with self.subTest(code=code):
				row = self.make_agent(synced=False, agent_name=code)
				with (
					patch("jarvis.admin_client.post_push_user_agents", side_effect=exc),
					patch.object(frappe.db, "commit"),
					patch.object(frappe.db, "rollback"),
				):
					user_agent_sync.run_sync()
				state = frappe.db.get_value(
					DOCTYPE, row.name, ["sync_status", "sync_error", "sync_error_message"], as_dict=True
				)
				self.assertEqual((state.sync_status, state.sync_error), (status, code))
				self.assertTrue(state.sync_error_message.endswith("."))
				frappe.db.delete(DOCTYPE, row.name)

	def test_a_failure_leaves_synced_rows_synced(self):
		synced = self.make_agent()
		with (
			patch("jarvis.admin_client.post_push_user_agents", side_effect=AdminUnreachableError("down")),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			user_agent_sync.run_sync()
		self.assertEqual(frappe.db.get_value(DOCTYPE, synced.name, "sync_status"), "Synced")

	def test_only_definition_changes_request_a_sync(self):
		row = self.make_agent()
		self.sync.reset_mock()
		with _as(OWNER):
			user_agents_api.set_user_agent_schedule(row.name, 1, schedule_prompt="Daily digest")
		self.sync.assert_not_called()
		with _as(OWNER):
			user_agents_api.set_user_agent_enabled(row.name, 0)
		self.sync.assert_called_once()
