"""Regression tests for the shared hard-close foundation.

Every fixture in this module is synthetic. The tests model rows and credentials
that an older server could have left behind; no test uses customer data.
"""

from __future__ import annotations

import importlib
import itertools
import json
from pathlib import Path
from unittest.mock import call, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from jarvis import api
from jarvis.chat import (
	actions_api,
	agent_catalog,
	agent_closure,
	agent_scheduler,
	agents_api,
	pending_confirm,
	pending_confirm_legacy,
)
from jarvis.chat import (
	api as chat_api,
)
from jarvis.chat.agent_closure import (
	CLOSED_AGENT_SPECS,
	CLOSED_STATUS,
	FENCE_DOCTYPE,
	PENDING_ACTION_FENCE_FIELD,
	assert_session_dispatch_allowed,
	closed_agent_for_session,
	closed_catalog_values,
	confirmation_is_current,
	direct_write_refusal,
	expected_confirmation_fences,
	normalize_confirmation_fences,
)
from jarvis.exceptions import PermissionDeniedError
from jarvis.settings_reset import ResetSpec
from jarvis.settings_reset import apply as apply_settings_reset
from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tools import _delegate_capability, _delegate_write_caps
from jarvis.tools.amend_doc import _amend_one
from jarvis.tools.apply_workflow_action import apply_workflow_action
from jarvis.tools.cancel_doc import _cancel_one
from jarvis.tools.create_doc import create_doc
from jarvis.tools.delete_doc import _delete_one
from jarvis.tools.submit_doc import _submit_one
from jarvis.tools.update_doc import update_doc

INSTALLATION = "Jarvis Agent Installation"
LISTING = "Jarvis Agent Listing"
RUN = "Jarvis Agent Run"
SESSION = "Jarvis Chat Session"
PENDING_ACTION = "Jarvis Pending Action"
PENDING_ACTION_WAITER = "Jarvis Pending Action Waiter"
CONVERSATION = "Jarvis Conversation"
MESSAGE = "Jarvis Chat Message"
OWNER = "agent-closure-owner@example.invalid"
SAFE_AGENT = "agent-closure-safe-agent"
CONVERSATION_TITLE = "Agent closure test confirmation"


def _expected_map() -> dict[str, str]:
	return {spec.slug: spec.generation for spec in sorted(CLOSED_AGENT_SPECS, key=lambda item: item.slug)}


def _ensure_owner() -> str:
	if not frappe.db.exists("User", OWNER):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": OWNER,
				"first_name": "Agent closure owner",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		user.insert(ignore_permissions=True)
		user.add_roles("Jarvis User")
	return OWNER


def _make_safe_listing() -> None:
	if frappe.db.exists(LISTING, SAFE_AGENT):
		return
	frappe.get_doc(
		{
			"doctype": LISTING,
			"agent_slug": SAFE_AGENT,
			"title": "Synthetic closure fixture",
			"nature": "Auditor",
			"delivery": "delegate",
			"status": "Published",
			"version": "0.1.0",
			"tools_required": json.dumps([]),
			"doctypes_required": json.dumps([]),
			"rule_tokens": json.dumps([]),
			"min_apps": json.dumps([]),
			"writes": json.dumps([]),
			"rule_pack": "agent-closure-fixture",
		}
	).insert(ignore_permissions=True)


class TestAgentClosureContracts(FrappeTestCase):
	def test_static_registry_and_catalog_contract_close_both_agents(self):
		self.assertEqual({spec.slug for spec in CLOSED_AGENT_SPECS}, {
			"reorder-replenishment-operator",
			"ticket-triage-operator",
		})
		self.assertEqual(len({spec.fence_name for spec in CLOSED_AGENT_SPECS}), 2)
		self.assertEqual(len({spec.patch for spec in CLOSED_AGENT_SPECS}), 2)

		registry_path = Path(__file__).resolve().parents[1] / "agents" / "registry.json"
		registry = {
			item["agent_slug"]: item
			for item in json.loads(registry_path.read_text())["agents"]
			if isinstance(item, dict) and item.get("agent_slug")
		}
		for spec in CLOSED_AGENT_SPECS:
			contract = registry[spec.slug]
			self.assertEqual(contract["status"], CLOSED_STATUS)
			self.assertEqual(contract["tools_required"], [])
			self.assertEqual(contract["tools_allow"], [])
			self.assertEqual(contract["doctypes_required"], [])
			self.assertEqual(contract["rule_tokens"], [])
			self.assertEqual(contract["advisory_tokens"], [])
			self.assertIsNone(contract["writes"])
			forced = closed_catalog_values(spec.slug, {"status": "Published", "tools_required": "[\"x\"]"})
			self.assertEqual(forced["status"], CLOSED_STATUS)
			self.assertEqual(frappe.parse_json(forced["tools_required"]), [])
			self.assertEqual(frappe.parse_json(forced["writes"]), [])

	def test_exact_normalized_map_rejects_malformed_partial_stale_and_extra_values(self):
		expected = _expected_map()
		with patch.object(agent_closure, "expected_confirmation_fences", return_value=expected):
			self.assertEqual(normalize_confirmation_fences(dict(reversed(list(expected.items())))), expected)
			self.assertTrue(
				confirmation_is_current({"kind": "chat", PENDING_ACTION_FENCE_FIELD: json.dumps(expected)})
			)
			self.assertFalse(confirmation_is_current({"kind": "chat"}))
			self.assertFalse(
				confirmation_is_current(
					{"kind": "chat", PENDING_ACTION_FENCE_FIELD: {CLOSED_AGENT_SPECS[0].slug: "old"}}
				)
			)
			self.assertFalse(
				confirmation_is_current(
					{
						"kind": "chat",
						PENDING_ACTION_FENCE_FIELD: {**expected, "unexpected-agent": "v1"},
					}
				)
			)
			self.assertFalse(
				confirmation_is_current({"kind": "chat", PENDING_ACTION_FENCE_FIELD: "{not-json"})
			)
			self.assertTrue(confirmation_is_current({"kind": "file_box_sheet"}))

	def test_code_before_model_sync_has_no_usable_generic_confirmation(self):
		with patch.object(frappe.db, "table_exists", return_value=False):
			self.assertIsNone(expected_confirmation_fences())
			self.assertFalse(
				confirmation_is_current({"kind": "chat", PENDING_ACTION_FENCE_FIELD: _expected_map()})
			)

	def test_fence_activation_needs_both_specs_in_either_order_and_is_retry_safe(self):
		self.assertTrue(frappe.db.table_exists(FENCE_DOCTYPE))
		for index, (first, second) in enumerate(itertools.permutations(CLOSED_AGENT_SPECS)):
			savepoint = f"agent_closure_fence_order_{index}"
			frappe.db.savepoint(savepoint)
			try:
				frappe.db.delete(FENCE_DOCTYPE, {"name": ["in", [spec.fence_name for spec in CLOSED_AGENT_SPECS]]})
				with patch.object(agent_closure, "_patch_logged", return_value=False):
					agent_closure.activate_close_fence(first)
					self.assertIsNone(expected_confirmation_fences())
					agent_closure.activate_close_fence(second)
					self.assertEqual(expected_confirmation_fences(), _expected_map())
					agent_closure.activate_close_fence(first)
					self.assertEqual(expected_confirmation_fences(), _expected_map())
			finally:
				frappe.db.rollback(save_point=savepoint)

	def test_both_historic_session_prefixes_are_closed_and_an_ordinary_key_is_not(self):
		for spec in CLOSED_AGENT_SPECS:
			for prefix in spec.session_prefixes:
				key = f"{prefix}synthetic-bearer"
				self.assertEqual(closed_agent_for_session(key), spec)
				with self.assertRaises(frappe.PermissionError):
					assert_session_dispatch_allowed(key)
		self.assertIsNone(closed_agent_for_session("agent:agent-ordinary-agent:synthetic-bearer"))
		assert_session_dispatch_allowed("agent:agent-ordinary-agent:synthetic-bearer")

	def test_closed_delegate_denies_every_tool_and_all_direct_generic_writes_before_erp_io(self):
		attempts = (
			("create", lambda: create_doc("ToDo", {"description": "closure write guard"})),
			("update", lambda: update_doc("ToDo", "CLOSURE-MISSING", {"description": "blocked"})),
			("submit", lambda: _submit_one("ToDo", "CLOSURE-MISSING")),
			("cancel", lambda: _cancel_one("ToDo", "CLOSURE-MISSING")),
			("amend", lambda: _amend_one("ToDo", "CLOSURE-MISSING")),
			("delete", lambda: _delete_one("ToDo", "CLOSURE-MISSING")),
			("workflow", lambda: apply_workflow_action("ToDo", "CLOSURE-MISSING", "Approve")),
		)
		before = frappe.db.count("ToDo")
		for spec in CLOSED_AGENT_SPECS:
			for legacy in (True, False):
				cap = {
					"run": f"CLOSE-{spec.slug}-{legacy}",
					"agent": spec.slug,
					"legacy": legacy,
					"tools_allow": [],
					"nature": "operator",
					"writes": [],
					"run_as": OWNER,
				}
				for operation, attempt in attempts:
					denial = _delegate_capability.tool_denial("ignored", operation, cap=cap)
					self.assertIsNotNone(denial)
					self.assertTrue(denial["fatal"])
					with patch.object(_delegate_write_caps, "resolve_delegate", return_value=cap):
						with self.assertRaises(PermissionDeniedError):
							attempt()
		self.assertEqual(frappe.db.count("ToDo"), before)

	def test_ordinary_agent_closure_helpers_are_inert(self):
		values = {"status": "Published", "tools_required": "[\"jarvis__get_doc\"]"}
		self.assertIs(closed_catalog_values(SAFE_AGENT, values), values)
		self.assertIsNone(direct_write_refusal(SAFE_AGENT, "create", "ToDo"))
		self.assertIsNone(agent_closure.delegate_tool_denial({"run": "ORDINARY", "agent": SAFE_AGENT}, "get_doc"))

	def test_catalog_lock_helper_always_locks_catalog_then_version_then_dirty(self):
		with (
			patch.object(agents_api, "_lock_catalog_state") as lock_catalog,
			patch.object(frappe.db, "get_single_value", side_effect=[17, 1]) as get_single,
		):
			state = agents_api._locked_catalog_state()
		self.assertEqual(state, {"agent_catalog_version": 17, "agent_catalog_dirty": 1})
		lock_catalog.assert_called_once_with()
		self.assertEqual(
			get_single.call_args_list,
			[
				call("Jarvis Settings", "agent_catalog_version", cache=False, for_update=True),
				call("Jarvis Settings", "agent_catalog_dirty", cache=False, for_update=True),
			],
		)

	def test_catalog_settings_and_reset_use_the_shared_lock_state(self):
		settings = frappe.get_single("Jarvis Settings")
		state = {"agent_catalog_version": 41, "agent_catalog_dirty": 1}
		with patch.object(agents_api, "_locked_catalog_state", return_value=state) as lock_state:
			settings._preserve_agent_catalog_state()
		lock_state.assert_called_once_with()
		self.assertEqual(settings.agent_catalog_version, 41)
		self.assertEqual(settings.agent_catalog_dirty, 1)

		class SyntheticSettings:
			def __init__(self):
				self.writes = []

			def db_set(self, field, value):
				self.writes.append((field, value))

		fake = SyntheticSettings()
		with patch.object(agents_api, "_locked_catalog_state", return_value=state) as lock_state:
			apply_settings_reset(
				fake,
				ResetSpec(zero=("agent_catalog_version", "agent_catalog_dirty")),
			)
		lock_state.assert_called_once_with()
		self.assertEqual(
			fake.writes,
			[("agent_catalog_version", 0), ("agent_catalog_dirty", 0)],
		)

	def test_each_non_skippable_wrapper_calls_the_shared_migration(self):
		for spec in CLOSED_AGENT_SPECS:
			module = importlib.import_module(spec.patch)
			with patch.object(module, "run_close_migration") as run_close_migration:
				module.execute()
			run_close_migration.assert_called_once_with(spec.slug)
		self.assertTrue(set(spec.patch for spec in CLOSED_AGENT_SPECS).issubset(set(frappe.get_hooks("never_skip_patches"))))


class TestAgentClosureIntegration(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_owner()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._wipe()
		_make_safe_listing()
		for spec in CLOSED_AGENT_SPECS:
			self.assertTrue(frappe.db.exists(LISTING, spec.slug))
			frappe.db.set_value(LISTING, spec.slug, "status", CLOSED_STATUS, update_modified=False)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._wipe()
		for spec in CLOSED_AGENT_SPECS:
			if frappe.db.exists(LISTING, spec.slug):
				frappe.db.set_value(LISTING, spec.slug, "status", CLOSED_STATUS, update_modified=False)
		frappe.db.commit()

	def _wipe(self):
		cards = frappe.get_all(PENDING_ACTION, filters={"owner_user": OWNER}, pluck="name")
		if cards:
			frappe.db.delete(PENDING_ACTION_WAITER, {"parent": ["in", cards]})
			frappe.db.delete(PENDING_ACTION, {"name": ["in", cards]})
		conversations = frappe.get_all(
			CONVERSATION, filters={"owner": OWNER, "title": CONVERSATION_TITLE}, pluck="name"
		)
		if conversations:
			frappe.db.delete(MESSAGE, {"conversation": ["in", conversations]})
			frappe.db.delete("Jarvis Chat Turn", {"conversation": ["in", conversations]})
			frappe.db.delete(CONVERSATION, {"name": ["in", conversations]})
		for member in frappe.cache().smembers(pending_confirm_legacy._owner_key(OWNER)) or set():
			token = member.decode() if isinstance(member, bytes) else member
			pending_confirm_legacy.rollback_token(token, OWNER)
		frappe.db.delete(INSTALLATION, {"owner": OWNER})
		frappe.db.delete(RUN, {"owner": OWNER})
		for spec in CLOSED_AGENT_SPECS:
			for prefix in spec.session_prefixes:
				frappe.db.delete(SESSION, {"session_key": ["like", f"{prefix}closure-%"]})
			frappe.db.delete("Jarvis Agent Allowed User", {"parent": spec.slug, "user": OWNER})
		if frappe.db.exists(LISTING, SAFE_AGENT):
			frappe.delete_doc(LISTING, SAFE_AGENT, force=True, ignore_permissions=True)

	def _legacy_install(self, spec, *, enabled=0, schedule_enabled=0) -> str:
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": SAFE_AGENT,
				"run_as_user": OWNER,
				"reviewer": OWNER,
				"activation_state": "shadow",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(INSTALLATION, doc.name, "owner", OWNER, update_modified=False)
		frappe.db.set_value(
			INSTALLATION,
			doc.name,
			{
				"agent": spec.slug,
				"enabled": enabled,
				"schedule_enabled": schedule_enabled,
				"schedule_frequency": "daily",
				"next_run_at": add_days(now_datetime(), -1) if schedule_enabled else None,
			},
			update_modified=False,
		)
		frappe.db.commit()
		return doc.name

	def _legacy_run(self, spec, installation: str, *, prefix: str, status="running") -> tuple[str, str]:
		session_key = f"{prefix}closure-run"
		values = {
			"doctype": RUN,
			"agent": spec.slug,
			"installation": installation,
			"trigger": "manual",
			"status": status,
			"started_at": now_datetime(),
			"session_key": session_key,
			"capability_contract": "legacy",
		}
		if status != "running":
			values["finished_at"] = now_datetime()
		run = frappe.get_doc(values)
		run.insert(ignore_permissions=True)
		frappe.db.set_value(RUN, run.name, "owner", OWNER, update_modified=False)
		frappe.get_doc({"doctype": SESSION, "session_key": session_key, "user": OWNER}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		return run.name, session_key

	def _conversation(self) -> str:
		frappe.set_user(OWNER)
		conversation = frappe.get_doc({"doctype": CONVERSATION, "title": CONVERSATION_TITLE}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
		return conversation.name

	def _durable_card(self) -> tuple[str, str]:
		conversation = self._conversation()
		token = pending_confirm.mint(
			conversation=conversation,
			owner=OWNER,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "agent closure confirmation"}},
			run_id="",
			preview={"card": {"title": "Synthetic closure confirmation", "fields": []}},
		)
		self.assertTrue(token)
		return token, conversation

	def _legacy_token(self, *, conversation="") -> str:
		token = pending_confirm_legacy.mint(
			conversation=conversation,
			owner=OWNER,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "legacy closure confirmation"}},
			run_id="",
		)
		self.assertTrue(token)
		return token

	def _legacy_card(self, *, conversation: str | None = None) -> tuple[str, str]:
		conversation = conversation or self._conversation()
		token = self._legacy_token(conversation=conversation)
		record = pending_confirm_legacy.peek(token)
		self.assertIsNotNone(record)
		api.persist_pending_action(
			conversation,
			"create_doc",
			{"card": {"title": "Synthetic legacy closure confirmation", "fields": []}},
			token,
			record["expires_at"],
		)
		return token, conversation

	def _revoke_legacy_fence(self, token: str) -> None:
		record = pending_confirm_legacy.peek(token)
		self.assertIsNotNone(record)
		record[PENDING_ACTION_FENCE_FIELD] = {
			CLOSED_AGENT_SPECS[0].slug: CLOSED_AGENT_SPECS[0].generation
		}
		frappe.cache().set_value(pending_confirm_legacy._key(token), record, expires_in_sec=900)

	def _assert_revoked_legacy_receipt(self, token: str) -> None:
		row = frappe.db.get_value(
			MESSAGE,
			{"role": "tool", "tool_call_id": token},
			["tool_args", "tool_result", "tool_status", "action_outcome", "pending_card"],
			as_dict=True,
		)
		self.assertIsNotNone(row)
		self.assertEqual(row.tool_status, "")
		self.assertEqual(row.action_outcome, "cancelled")
		self.assertIsNone(row.pending_card)
		self.assertEqual(frappe.parse_json(row.tool_args), {})
		result = frappe.parse_json(row.tool_result)
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")
		self.assertEqual(result["error"]["type"], "InvalidConfirmation")
		self.assertEqual(
			result["error"]["message"],
			"This confirmation was cancelled during an agent safety update. "
			"Submit the action again if it is still needed.",
		)

	def _assert_suppressed_durable_card(self, conversation: str, token: str) -> None:
		"""The read response must make an unsafe durable row terminal without
		manufacturing another chat receipt. The migration can settle it later."""
		frappe.set_user(OWNER)
		history = chat_api.get_conversation(conversation)["messages"]
		row = next(item for item in history if item.get("tool_call_id") == token)
		self.assertEqual(row.get("tool_status"), "")
		self.assertEqual(row.get("action_outcome"), "cancelled")
		self.assertIsNone(row.get("pending_card"))
		self.assertFalse(row.get("pending_card_safe"))
		self.assertEqual(row.get("tool_args"), {})
		self.assertEqual(row.get("tool_result", {}).get("reason_code"), "safety_update")

	def test_both_agents_refuse_opening_routes_and_fleet_payload(self):
		for spec in CLOSED_AGENT_SPECS:
			frappe.db.set_value(LISTING, spec.slug, "status", "Published", update_modified=False)
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_listing_status(spec.slug, "Published")

			listing = frappe.get_doc(LISTING, spec.slug)
			listing.status = "Published"
			with self.assertRaises(frappe.PermissionError):
				listing.save(ignore_permissions=True)

			frappe.set_user(OWNER)
			with self.assertRaises(frappe.PermissionError):
				agents_api.install_agent(spec.slug)
			frappe.set_user("Administrator")

			installation = self._legacy_install(spec, enabled=1, schedule_enabled=1)
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_enabled(installation, 1)
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_schedule(installation, schedule_enabled=1)
			with self.assertRaises(frappe.PermissionError):
				agents_api.run_agent_now(installation)
			with self.assertRaises(frappe.PermissionError):
				agent_scheduler._launch_audit(frappe.get_doc(INSTALLATION, installation), trigger="scheduled")

			with patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty:
				result = agents_api.set_schedule(installation, schedule_enabled=0)
			self.assertTrue(result["ok"])
			mark_catalog_dirty.assert_called_once_with()
			state = frappe.db.get_value(
				INSTALLATION, installation, ["enabled", "schedule_enabled", "next_run_at"], as_dict=True
			)
			self.assertEqual((state.enabled, state.schedule_enabled, state.next_run_at), (0, 0, None))

			frappe.db.set_value(LISTING, spec.slug, "status", CLOSED_STATUS, update_modified=False)
			listing = frappe.get_doc(LISTING, spec.slug)
			listing.append("allowed_users", {"user": OWNER})
			listing.save(ignore_permissions=True)
			frappe.db.set_value(LISTING, spec.slug, "status", "Published", update_modified=False)
			frappe.db.set_value(INSTALLATION, installation, "enabled", 1, update_modified=False)

		for payload in (agent_catalog.build_agent_push_payload(), agent_catalog.build_agent_push_payload(owner=OWNER)):
			slugs = {entry["slug"] for entry in payload}
			for spec in CLOSED_AGENT_SPECS:
				self.assertNotIn(f"agent-{spec.slug}", slugs)

	def test_scheduler_retires_raw_rows_without_dispatch_for_both_agents(self):
		for spec in CLOSED_AGENT_SPECS:
			installation = self._legacy_install(spec, enabled=1, schedule_enabled=1)
			with (
				patch.object(agent_scheduler, "_launch_audit") as launch,
				patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty,
			):
				agent_scheduler._sweep_one(frappe.get_doc(INSTALLATION, installation), now_datetime(), set())
			launch.assert_not_called()
			mark_catalog_dirty.assert_called_once_with()
			state = frappe.db.get_value(
				INSTALLATION, installation, ["enabled", "schedule_enabled", "next_run_at"], as_dict=True
			)
			self.assertEqual((state.enabled, state.schedule_enabled, state.next_run_at), (0, 0, None))

	def test_migration_retires_runs_both_prefixes_and_retries_for_both_agents(self):
		for spec in CLOSED_AGENT_SPECS:
			installation = self._legacy_install(spec, enabled=1, schedule_enabled=1)
			run, current_key = self._legacy_run(spec, installation, prefix=spec.session_prefixes[0])
			historic_key = f"{spec.session_prefixes[1]}closure-orphan"
			frappe.get_doc({"doctype": SESSION, "session_key": historic_key, "user": OWNER}).insert(
				ignore_permissions=True
			)
			frappe.db.set_value(LISTING, spec.slug, "status", "Published", update_modified=False)
			frappe.db.commit()
			with (
				patch.object(agent_closure, "cancel_unfenced_chat_cards", return_value=0),
				patch.object(agent_closure, "revoke_unfenced_legacy_tokens", return_value=0),
				patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty,
			):
				agent_closure.run_close_migration(spec)
				agent_closure.run_close_migration(spec)
			self.assertEqual(mark_catalog_dirty.call_args_list, [call(strict=True), call(strict=True)])
			state = frappe.db.get_value(
				INSTALLATION, installation, ["enabled", "schedule_enabled", "next_run_at"], as_dict=True
			)
			self.assertEqual((state.enabled, state.schedule_enabled, state.next_run_at), (0, 0, None))
			self.assertEqual(frappe.db.get_value(LISTING, spec.slug, "status"), CLOSED_STATUS)
			self.assertEqual(frappe.db.get_value(RUN, run, "status"), "failed")
			self.assertFalse(frappe.db.exists(SESSION, {"session_key": current_key}))
			self.assertFalse(frappe.db.exists(SESSION, {"session_key": historic_key}))

	def test_final_session_dispatch_fence_blocks_both_prefixes_before_the_tool(self):
		frappe.set_user("Administrator")
		for spec in CLOSED_AGENT_SPECS:
			for prefix in spec.session_prefixes:
				with (
					patch.object(api, "_run_tool") as run_tool,
					patch.object(api, "_persist_and_publish_tool_call"),
				):
					result = api._dispatch_from_session(
						OWNER,
						f"{prefix}closure-late-auth",
						"create_doc",
						{"doctype": "ToDo", "values": {"description": "must not run"}},
					)
				run_tool.assert_not_called()
				self.assertFalse(result["ok"])
				self.assertEqual(result["error"]["code"], "PermissionError")

	def test_get_agent_marks_an_installed_closed_agent_as_terminal(self):
		for spec in CLOSED_AGENT_SPECS:
			self._legacy_install(spec, enabled=0, schedule_enabled=0)
			frappe.set_user(OWNER)
			result = agents_api.get_agent(spec.slug)
			self.assertEqual(result["status"], CLOSED_STATUS)
			self.assertEqual(result["runtime_closed"], 1)
			self.assertIsNotNone(result["installation"])
			frappe.set_user("Administrator")
		frappe.set_user("Administrator")
		self.assertEqual(agents_api.get_agent(SAFE_AGENT)["runtime_closed"], 0)

	def test_legacy_confirmation_map_fails_closed_before_dispatch_and_full_map_stays_usable(self):
		self.assertEqual(expected_confirmation_fences(), _expected_map())
		token = self._legacy_token()
		self.assertEqual(pending_confirm_legacy.peek(token)[PENDING_ACTION_FENCE_FIELD], _expected_map())
		self.assertIsNotNone(pending_confirm_legacy.consume(token, owner=OWNER, conversation=""))

		stale = self._legacy_token()
		record = pending_confirm_legacy.peek(stale)
		record[PENDING_ACTION_FENCE_FIELD] = {CLOSED_AGENT_SPECS[0].slug: CLOSED_AGENT_SPECS[0].generation}
		frappe.cache().set_value(pending_confirm_legacy._key(stale), record, expires_in_sec=900)
		frappe.set_user(OWNER)
		with patch.object(api, "dispatch_confirmed") as dispatch_confirmed:
			result = actions_api._confirm_core(stale)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")
		self.assertIsNone(pending_confirm_legacy.peek(stale))
		self.assertFalse(frappe.db.exists(MESSAGE, {"tool_call_id": stale}))

	def test_migration_cleanup_persists_revoked_legacy_card_and_keeps_current_card(self):
		before = frappe.db.count("ToDo")
		conversation = self._conversation()
		stale, _ = self._legacy_card(conversation=conversation)
		current, _ = self._legacy_card(conversation=conversation)
		self._revoke_legacy_fence(stale)

		agent_closure.revoke_unfenced_legacy_tokens()

		self.assertIsNone(pending_confirm_legacy.peek(stale))
		self._assert_revoked_legacy_receipt(stale)
		self.assertEqual(
			frappe.db.get_value(MESSAGE, {"tool_call_id": current}, "tool_status"), "pending"
		)
		self.assertEqual(frappe.db.count("ToDo"), before)

		frappe.set_user(OWNER)
		reloaded = chat_api.get_conversation(conversation)["messages"]
		self.assertFalse(
			any(
				row.get("tool_call_id") == stale
				and row.get("tool_status") == "pending"
				and row.get("pending_card")
				for row in reloaded
			)
		)
		self.assertEqual(
			[
				row.get("tool_call_id")
				for row in reloaded
				if row.get("role") == "tool"
				and row.get("tool_status") == "pending"
				and row.get("pending_card")
			],
			[current],
		)

	def test_runtime_fence_rejection_settles_legacy_card_after_capped_cleanup_scan(self):
		before = frappe.db.count("ToDo")
		conversation = self._conversation()
		stale, _ = self._legacy_card(conversation=conversation)
		current, _ = self._legacy_card(conversation=conversation)
		self._revoke_legacy_fence(stale)

		with patch.object(agent_closure, "_REDIS_SCAN_LIMIT", 0):
			self.assertEqual(agent_closure.revoke_unfenced_legacy_tokens(), 0)
		self.assertIsNotNone(pending_confirm_legacy.peek(stale))

		frappe.set_user(OWNER)
		with patch.object(api, "dispatch_confirmed") as dispatch_confirmed:
			result = actions_api._confirm_core(stale, conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")
		self.assertIsNone(pending_confirm_legacy.peek(stale))
		self._assert_revoked_legacy_receipt(stale)
		self.assertEqual(
			frappe.db.get_value(MESSAGE, {"tool_call_id": current}, "tool_status"), "pending"
		)
		self.assertEqual(frappe.db.count("ToDo"), before)

		reloaded = chat_api.get_conversation(conversation)["messages"]
		self.assertEqual(
			[
				row.get("tool_call_id")
				for row in reloaded
				if row.get("role") == "tool"
				and row.get("tool_status") == "pending"
				and row.get("pending_card")
			],
			[current],
		)

	def test_capped_cleanup_list_resync_settles_stale_legacy_card_before_returning_it(self):
		"""A reload must not re-show a capped stale legacy card before a click."""
		before = frappe.db.count("ToDo")
		conversation = self._conversation()
		stale, _ = self._legacy_card(conversation=conversation)
		current, _ = self._legacy_card(conversation=conversation)
		self._revoke_legacy_fence(stale)

		with patch.object(agent_closure, "_REDIS_SCAN_LIMIT", 0):
			self.assertEqual(agent_closure.revoke_unfenced_legacy_tokens(), 0)
		self.assertIsNotNone(pending_confirm_legacy.peek(stale))

		frappe.set_user(OWNER)
		listed = pending_confirm_legacy.list_for_owner(OWNER, conversation=conversation, strict=True)
		self.assertEqual([record["token"] for record in listed], [current])
		self.assertIsNone(pending_confirm_legacy.peek(stale))
		self._assert_revoked_legacy_receipt(stale)
		self.assertEqual(frappe.db.count("ToDo"), before)

		resync = actions_api.list_pending_confirmations(conversation)
		self.assertTrue(resync["ok"])
		self.assertEqual([item["token"] for item in resync["data"]["pending"]], [current])
		reloaded = chat_api.get_conversation(conversation)["messages"]
		self.assertEqual(
			[
				row.get("tool_call_id")
				for row in reloaded
				if row.get("role") == "tool"
				and row.get("tool_status") == "pending"
				and row.get("pending_card")
			],
			[current],
		)

	def test_transcript_read_fences_capped_stale_legacy_card_before_resync(self):
		"""History cannot show a stale card while list resync is still in flight."""
		conversation = self._conversation()
		stale, _ = self._legacy_card(conversation=conversation)
		current, _ = self._legacy_card(conversation=conversation)
		self._revoke_legacy_fence(stale)

		with patch.object(agent_closure, "_REDIS_SCAN_LIMIT", 0):
			self.assertEqual(agent_closure.revoke_unfenced_legacy_tokens(), 0)
		self.assertIsNotNone(pending_confirm_legacy.peek(stale))

		frappe.set_user(OWNER)
		# PWA mount starts getConversation and list_pending_confirmations together.
		# Make the transcript read win: it must settle and redact the stale row itself.
		history = chat_api.get_conversation(conversation)["messages"]
		self.assertEqual(
			[
				row.get("tool_call_id")
				for row in history
				if row.get("role") == "tool"
				and row.get("tool_status") == "pending"
				and row.get("pending_card")
			],
			[current],
		)
		current_row = next(row for row in history if row.get("tool_call_id") == current)
		self.assertTrue(current_row["pending_card_safe"])
		self.assertIsNone(pending_confirm_legacy.peek(stale))
		self._assert_revoked_legacy_receipt(stale)

		resync = actions_api.list_pending_confirmations(conversation)
		self.assertTrue(resync["ok"])
		self.assertEqual([item["token"] for item in resync["data"]["pending"]], [current])

	def test_transcript_read_hides_stale_legacy_card_when_durable_settlement_fails(self):
		"""A failed terminal flip must fail closed instead of offering Confirm."""
		conversation = self._conversation()
		stale, _ = self._legacy_card(conversation=conversation)
		current, _ = self._legacy_card(conversation=conversation)
		self._revoke_legacy_fence(stale)

		with patch.object(agent_closure, "settle_revoked_legacy_chat_card", return_value=False):
			frappe.set_user(OWNER)
			history = chat_api.get_conversation(conversation)["messages"]
			resync = actions_api.list_pending_confirmations(conversation)

		self.assertEqual(
			[
				row.get("tool_call_id")
				for row in history
				if row.get("role") == "tool"
				and row.get("tool_status") == "pending"
				and row.get("pending_card")
			],
			[current],
		)
		current_row = next(row for row in history if row.get("tool_call_id") == current)
		self.assertTrue(current_row["pending_card_safe"])
		stale_row = next(row for row in history if row.get("tool_call_id") == stale)
		self.assertEqual(stale_row.get("tool_status"), "")
		self.assertIsNone(stale_row.get("pending_card"))
		self.assertFalse(stale_row.get("pending_card_safe"))
		# The facade deliberately keeps the valid sibling when a legacy token cannot
		# settle. It must exclude the stale token rather than returning it as pending.
		self.assertTrue(resync["ok"])
		self.assertEqual([item["token"] for item in resync["data"]["pending"]], [current])
		# The failed flip leaves the credential intact for a later durable retry, but
		# the response must never turn that unsafe state into a client-side Confirm.
		self.assertIsNotNone(pending_confirm_legacy.peek(stale))
		self.assertEqual(
			frappe.db.get_value(MESSAGE, {"tool_call_id": stale}, "tool_status"),
			"pending",
		)

	def test_capped_cleanup_list_resync_keeps_token_when_terminal_settlement_fails(self):
		"""A failed durable flip must not burn a credential whose card still shows."""
		conversation = self._conversation()
		stale, _ = self._legacy_card(conversation=conversation)
		self._revoke_legacy_fence(stale)

		with patch.object(agent_closure, "settle_revoked_legacy_chat_card", return_value=False):
			with self.assertRaises(pending_confirm_legacy.PendingConfirmStorageError):
				pending_confirm_legacy.list_for_owner(OWNER, conversation=conversation, strict=True)

		self.assertIsNotNone(pending_confirm_legacy.peek(stale))
		self.assertEqual(
			frappe.db.get_value(MESSAGE, {"tool_call_id": stale}, "tool_status"),
			"pending",
		)

	def test_capped_cleanup_list_resync_never_inserts_an_unseen_legacy_receipt(self):
		"""A stale Redis-only token must disappear without manufacturing a transcript row."""
		before = frappe.db.count("ToDo")
		conversation = self._conversation()
		stale = self._legacy_token(conversation=conversation)
		self._revoke_legacy_fence(stale)

		with patch.object(agent_closure, "_REDIS_SCAN_LIMIT", 0):
			self.assertEqual(agent_closure.revoke_unfenced_legacy_tokens(), 0)
		self.assertFalse(frappe.db.exists(MESSAGE, {"tool_call_id": stale}))

		self.assertEqual(
			pending_confirm_legacy.list_for_owner(OWNER, conversation=conversation, strict=True),
			[],
		)
		self.assertIsNone(pending_confirm_legacy.peek(stale))
		self.assertFalse(frappe.db.exists(MESSAGE, {"tool_call_id": stale}))
		self.assertEqual(frappe.db.count("ToDo"), before)

	def test_capped_durable_cards_with_bad_fences_are_suppressed_but_a_valid_sibling_survives(self):
		"""The bounded close cleanup is not a card-read authority.

		A card beyond its cleanup snapshot can remain Pending in storage. It must
		still be hidden from both transcript and resync until a later cleanup can
		settle it. A separate current card must not be lost with it.
		"""
		before = frappe.db.count("ToDo")
		for label, fences in (
			("mapless", None),
			("malformed", json.dumps(["not", "a", "fence", "map"])),
			("partial", json.dumps({CLOSED_AGENT_SPECS[0].slug: CLOSED_AGENT_SPECS[0].generation})),
		):
			with self.subTest(label=label):
				stale, conversation = self._durable_card()
				current, _ = self._durable_card()
				frappe.db.set_value(
					PENDING_ACTION,
					stale,
					PENDING_ACTION_FENCE_FIELD,
					fences,
					update_modified=False,
				)
				frappe.db.commit()

				with patch.object(agent_closure, "_CLOSE_SNAPSHOT_LIMIT", 0):
					self.assertEqual(agent_closure.cancel_unfenced_chat_cards(), 0)
				self.assertEqual(frappe.db.get_value(PENDING_ACTION, stale, "status"), "Pending")

				self._assert_suppressed_durable_card(conversation, stale)
				resync = actions_api.list_pending_confirmations()
				self.assertTrue(resync["ok"])
				items = {item["token"]: item for item in resync["data"]["pending"]}
				self.assertNotIn(stale, items)
				self.assertIn(current, items)
				self.assertTrue(items[current]["pending_card_safe"])
				self.assertEqual(frappe.db.count("ToDo"), before)

	def test_cancelled_durable_row_never_falls_back_to_a_legacy_confirmation_card(self):
		"""A Pending transcript row is not proof that its durable action is live.

		The test doubles the legacy gate as permissive. The durable row must still
		be redacted before that legacy path gets a chance to re-enable its token.
		"""
		before = frappe.db.count("ToDo")
		token, conversation = self._durable_card()
		frappe.db.set_value(PENDING_ACTION, token, "status", "Cancelled", update_modified=False)
		frappe.db.commit()

		with patch.object(agent_closure, "legacy_confirmation_is_safe_to_render", return_value=True) as legacy:
			self._assert_suppressed_durable_card(conversation, token)
		legacy.assert_not_called()
		frappe.set_user(OWNER)
		resync = actions_api.list_pending_confirmations(conversation)
		self.assertTrue(resync["ok"])
		self.assertNotIn(token, [item["token"] for item in resync["data"]["pending"]])
		# A stale Redis record with the same token must not revive a cancelled
		# durable action. A distinct legacy sibling remains usable.
		with patch.object(
			pending_confirm_legacy,
			"list_for_owner",
			return_value=[
				{"token": token, "conversation": conversation},
				{"token": "legacy-safe-sibling", "conversation": conversation},
			],
		):
			listed = pending_confirm.list_for_owner(OWNER, conversation)
		self.assertEqual([item["token"] for item in listed], ["legacy-safe-sibling"])
		self.assertTrue(listed[0]["pending_card_safe"])
		self.assertEqual(frappe.db.count("ToDo"), before)

	def test_durable_card_fails_closed_when_the_close_fence_is_not_ready(self):
		"""A code-before or partial migration must not attest an otherwise-live row."""
		before = frappe.db.count("ToDo")
		token, conversation = self._durable_card()
		with patch.object(agent_closure, "expected_confirmation_fences", return_value=None):
			self._assert_suppressed_durable_card(conversation, token)
			frappe.set_user(OWNER)
			resync = actions_api.list_pending_confirmations(conversation)
		self.assertTrue(resync["ok"])
		self.assertNotIn(token, [item["token"] for item in resync["data"]["pending"]])
		self.assertEqual(frappe.db.count("ToDo"), before)

	def test_durable_card_fails_closed_before_the_fence_column_migration(self):
		"""Do not read a missing map column as proof that a durable card is safe."""
		before = frappe.db.count("ToDo")
		token, conversation = self._durable_card()
		with patch.object(agent_closure, "pending_action_fences_ready", return_value=False):
			self._assert_suppressed_durable_card(conversation, token)
			frappe.set_user(OWNER)
			resync = actions_api.list_pending_confirmations(conversation)
		self.assertTrue(resync["ok"])
		self.assertNotIn(token, [item["token"] for item in resync["data"]["pending"]])
		self.assertEqual(frappe.db.count("ToDo"), before)

	def test_durable_confirmation_checks_before_claim_and_after_claim(self):
		before = frappe.db.count("ToDo")
		token, conversation = self._durable_card()
		frappe.db.set_value(
			PENDING_ACTION,
			token,
			PENDING_ACTION_FENCE_FIELD,
			json.dumps({CLOSED_AGENT_SPECS[0].slug: CLOSED_AGENT_SPECS[0].generation}),
			update_modified=False,
		)
		frappe.db.commit()
		frappe.set_user(OWNER)
		with patch.object(api, "dispatch_confirmed") as dispatch_confirmed:
			result = actions_api._confirm_core(token, conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")
		self.assertEqual(frappe.db.get_value(PENDING_ACTION, token, "status"), "Cancelled")

		post_claim_token, post_claim_conversation = self._durable_card()
		with (
			patch.object(agent_closure, "confirmation_is_current", side_effect=[True, False]),
			patch.object(api, "dispatch_confirmed") as dispatch_confirmed,
		):
			post_claim_result = actions_api._confirm_core(post_claim_token, post_claim_conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(post_claim_result["ok"])
		self.assertEqual(post_claim_result["reason_code"], "safety_update")
		self.assertEqual(frappe.db.get_value(PENDING_ACTION, post_claim_token, "status"), "Cancelled")
		self.assertEqual(frappe.db.count("ToDo"), before)


class TestAgentClosureCollaborativeDispatch(CollaborationCase):
	def test_persisted_closed_binding_cannot_reach_callback_or_tool_dispatch(self):
		from jarvis.chat import collaboration as collaboration_service
		from jarvis.chat import collaboration_dispatch

		for index, spec in enumerate(CLOSED_AGENT_SPECS):
			for prefix_index, prefix in enumerate(spec.session_prefixes):
				binding = self.binding(run=f"{index * 2 + prefix_index + 1:032x}")
				old_key = binding.session_key
				historic_key = f"{prefix}closure-binding-{index}-{prefix_index}"
				# Simulate a retained immutable binding after its ordinary auth/session
				# row was retired. This is exactly the shared route that cannot rely on
				# Jarvis Chat Session rows for its identity classification.
				frappe.db.set_value(collaboration_service.BINDING, binding.name, "session_key", historic_key)
				frappe.db.delete(collaboration_service.SESSION, {"session_key": old_key})
				self.assertTrue(collaboration_service.is_collaborative_session(historic_key))
				with (
					patch.object(collaboration_dispatch, "callback_scope") as callback_scope,
					patch.object(api, "_run_tool") as run_tool,
				):
					result = collaboration_dispatch.dispatch(
						historic_key,
						"create_doc",
						{"doctype": "ToDo", "values": {"description": "must not run"}},
						"closed-binding-call",
					)
				callback_scope.assert_not_called()
				run_tool.assert_not_called()
				self.assertFalse(result["ok"])
				self.assertEqual(result["error"]["code"], "PermissionError")
