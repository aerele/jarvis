"""Regression tests for the closed Reorder & Replenishment operator.

All rows in this module are synthetic. The legacy installation helper writes a
raw row after a safe install on purpose: it models an old row that predates the
controller gate, then proves every supported server route closes it.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now_datetime

from jarvis import api
from jarvis.chat import (
	actions_api,
	agent_catalog,
	agent_scheduler,
	agents_api,
	pending_confirm,
	pending_confirm_legacy,
)
from jarvis.chat.reorder_replenishment_gate import (
	CLOSED_STATUS,
	CLOSURE_GENERATION,
	FENCE_DOCTYPE,
	FENCE_NAME,
	PENDING_ACTION_FENCE_FIELD,
	REORDER_AGENT,
	RUN_UNAVAILABLE,
	activate_legacy_confirmation_fence,
	legacy_confirmation_generation,
	stamp_pending_confirmation,
)
from jarvis.exceptions import PermissionDeniedError
from jarvis.tools import _delegate_capability, _delegate_write_caps
from jarvis.tools.create_doc import create_doc
from jarvis.tools.update_doc import update_doc

INSTALLATION = "Jarvis Agent Installation"
LISTING = "Jarvis Agent Listing"
RUN = "Jarvis Agent Run"
ACTIVITY = "Jarvis Agent Activity"
SESSION = "Jarvis Chat Session"
PENDING_ACTION = "Jarvis Pending Action"
PENDING_ACTION_WAITER = "Jarvis Pending Action Waiter"
CONVERSATION = "Jarvis Conversation"
MESSAGE = "Jarvis Chat Message"
OWNER = "rro-gate-owner@example.invalid"
SAFE_SLUG = "rro-gate-safe-agent"
CURRENT_SESSION_PREFIX = f"agent:agent-{REORDER_AGENT}:rro-gate-"
HISTORIC_SESSION_PREFIX = f"agent:{REORDER_AGENT}:rro-gate-"
_CONFIRMATION_TITLE = "RRO close-fence fixture"
ODD_SESSION_KEY = "rro-gate-unusual-bearer"


def _ensure_owner() -> str:
	if not frappe.db.exists("User", OWNER):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": OWNER,
				"first_name": "RRO gate owner",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		user.insert(ignore_permissions=True)
		user.add_roles("Jarvis User")
	return OWNER


def _make_safe_listing() -> None:
	if frappe.db.exists(LISTING, SAFE_SLUG):
		return
	frappe.get_doc(
		{
			"doctype": LISTING,
			"agent_slug": SAFE_SLUG,
			"title": "Synthetic RRO gate fixture",
			"nature": "Auditor",
			"delivery": "delegate",
			"status": "Published",
			"version": "0.1.0",
			"rule_tokens": json.dumps([]),
			"min_apps": json.dumps([]),
			"doctypes_required": json.dumps([]),
			"writes": json.dumps([]),
			"rule_pack": "rro-gate-fixture",
		}
	).insert(ignore_permissions=True)


class TestReorderReplenishmentGate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_ensure_owner()
		if not frappe.db.exists(LISTING, REORDER_AGENT):
			frappe.throw("The test site has no Reorder & Replenishment listing.")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._wipe()
		_make_safe_listing()
		frappe.db.set_value(LISTING, REORDER_AGENT, "status", CLOSED_STATUS, update_modified=False)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._wipe()
		if frappe.db.exists(LISTING, REORDER_AGENT):
			frappe.db.set_value(LISTING, REORDER_AGENT, "status", CLOSED_STATUS, update_modified=False)
		frappe.db.commit()

	def _wipe(self):
		# The close migration intentionally cancels all old chat cards. Keep the
		# synthetic cards in this module separate from other fixtures and delete
		# their child rows first, since controller deletes are deliberately refused.
		cards = frappe.get_all(PENDING_ACTION, filters={"owner_user": OWNER}, pluck="name")
		if cards:
			frappe.db.delete(PENDING_ACTION_WAITER, {"parent": ["in", cards]})
			frappe.db.delete(PENDING_ACTION, {"name": ["in", cards]})
		conversations = frappe.get_all(
			CONVERSATION, filters={"owner": OWNER, "title": _CONFIRMATION_TITLE}, pluck="name"
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
		for session_prefix in (CURRENT_SESSION_PREFIX, HISTORIC_SESSION_PREFIX):
			frappe.db.delete(SESSION, {"session_key": ["like", f"{session_prefix}%"]})
		frappe.db.delete(SESSION, {"session_key": ODD_SESSION_KEY})
		frappe.db.delete(ACTIVITY, {"owner": OWNER, "agent": REORDER_AGENT})
		frappe.db.delete("Jarvis Agent Allowed User", {"parent": REORDER_AGENT, "user": OWNER})
		frappe.db.delete(FENCE_DOCTYPE, {"name": FENCE_NAME})
		if frappe.db.exists(LISTING, SAFE_SLUG):
			frappe.delete_doc(LISTING, SAFE_SLUG, force=True, ignore_permissions=True)

	def _confirmation_conversation(self) -> str:
		frappe.set_user(OWNER)
		conversation = frappe.get_doc(
			{"doctype": CONVERSATION, "title": _CONFIRMATION_TITLE}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return conversation.name

	def _historic_legacy_token(self, *, conversation: str = "", tool="create_doc", args=None) -> str:
		"""Make an old conversation-less generic write token with no RRO marker."""
		token = pending_confirm_legacy.mint(
			conversation=conversation,
			owner=OWNER,
			tool=tool,
			args=args
			or {"doctype": "Material Request", "values": {"material_request_type": "Purchase"}},
			run_id="",
		)
		record = pending_confirm_legacy.peek(token)
		record.pop(PENDING_ACTION_FENCE_FIELD, None)
		frappe.cache().set_value(pending_confirm_legacy._key(token), record, expires_in_sec=900)
		return token

	def _historic_durable_card(self) -> tuple[str, str]:
		"""Make a real old chat card, then remove its server fence marker."""
		conversation = self._confirmation_conversation()
		token = pending_confirm.mint(
			conversation=conversation,
			owner=OWNER,
			tool="create_doc",
			args={"doctype": "Material Request", "values": {"material_request_type": "Purchase"}},
			run_id="",
			preview={"card": {"title": "Create test material request", "fields": []}},
		)
		frappe.db.set_value(PENDING_ACTION, token, PENDING_ACTION_FENCE_FIELD, "", update_modified=False)
		frappe.db.commit()
		return token, conversation

	def _publish_reorder_raw(self):
		frappe.db.set_value(LISTING, REORDER_AGENT, "status", "Published", update_modified=False)
		frappe.db.commit()

	def _legacy_install(self, *, enabled=0, schedule_enabled=0) -> str:
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": SAFE_SLUG,
				"run_as_user": OWNER,
				"reviewer": OWNER,
				"activation_state": "shadow",
			}
		)
		doc.insert(ignore_permissions=True)
		# ``Document.insert`` stamps the current user as owner. Change it after the
		# safe insert, then turn the row into a raw pre-gate legacy installation.
		frappe.db.set_value(INSTALLATION, doc.name, "owner", OWNER, update_modified=False)
		frappe.db.set_value(
			INSTALLATION,
			doc.name,
			{
				"agent": REORDER_AGENT,
				"enabled": enabled,
				"schedule_enabled": schedule_enabled,
				"schedule_frequency": "daily",
				"next_run_at": add_days(now_datetime(), -1) if schedule_enabled else None,
			},
			update_modified=False,
		)
		frappe.db.commit()
		return doc.name

	def _legacy_run(self, installation: str, *, suffix="legacy", status="running") -> tuple[str, str]:
		session_key = f"{CURRENT_SESSION_PREFIX}{suffix}"
		values = {
			"doctype": RUN,
			"agent": REORDER_AGENT,
			"installation": installation,
			"trigger": "manual",
			"status": status,
			"started_at": now_datetime(),
			"session_key": session_key,
			"capability_contract": "legacy",
		}
		if status != "running":
			values["finished_at"] = now_datetime()
		run = frappe.get_doc(
			values
		)
		run.insert(ignore_permissions=True)
		frappe.db.set_value(RUN, run.name, "owner", OWNER, update_modified=False)
		frappe.get_doc(
			{"doctype": SESSION, "session_key": session_key, "user": OWNER}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return run.name, session_key

	def test_listing_and_new_install_stay_closed_when_status_is_tampered(self):
		self._publish_reorder_raw()
		with self.assertRaises(frappe.PermissionError):
			agents_api.set_listing_status(REORDER_AGENT, "Published")

		listing = frappe.get_doc(LISTING, REORDER_AGENT)
		listing.status = "Published"
		with self.assertRaises(frappe.PermissionError):
			listing.save(ignore_permissions=True)

		# Frappe restores ``agent_slug`` from the immutable document name after
		# validation. A writer must not be able to spoof that field during validate
		# and publish the fixed Reorder listing.
		frappe.db.set_value(LISTING, REORDER_AGENT, "status", CLOSED_STATUS, update_modified=False)
		listing = frappe.get_doc(LISTING, REORDER_AGENT)
		listing.agent_slug = "not-the-reorder-operator"
		listing.status = "Published"
		with self.assertRaises(frappe.PermissionError):
			listing.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(LISTING, REORDER_AGENT, "status"), CLOSED_STATUS)
		self.assertEqual(frappe.db.get_value(LISTING, REORDER_AGENT, "agent_slug"), REORDER_AGENT)

		frappe.set_user(OWNER)
		before = frappe.db.count(INSTALLATION, {"owner": OWNER, "agent": REORDER_AGENT})
		with self.assertRaises(frappe.PermissionError):
			agents_api.install_agent(REORDER_AGENT)
		self.assertEqual(frappe.db.count(INSTALLATION, {"owner": OWNER, "agent": REORDER_AGENT}), before)

		frappe.set_user("Administrator")
		direct = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": REORDER_AGENT,
				"run_as_user": OWNER,
				"reviewer": OWNER,
				"activation_state": "shadow",
			}
		)
		direct.owner = OWNER
		with self.assertRaises(frappe.PermissionError):
			direct.insert(ignore_permissions=True)

	def test_legacy_install_can_only_be_retired(self):
		name = self._legacy_install(enabled=1, schedule_enabled=1)
		with self.assertRaises(frappe.PermissionError):
			agents_api.set_enabled(name, 1)
		with self.assertRaises(frappe.PermissionError):
			agents_api.set_schedule(name, schedule_enabled=1)
		with self.assertRaises(frappe.PermissionError):
			agents_api.run_agent_now(name)

		doc = frappe.get_doc(INSTALLATION, name)
		with self.assertRaises(frappe.PermissionError):
			agent_scheduler._launch_audit(doc, trigger="scheduled")
		self.assertEqual(frappe.db.count(RUN, {"installation": name}), 0)

		with patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty:
			result = agents_api.set_schedule(name, schedule_enabled=0)
		mark_catalog_dirty.assert_called_once_with()
		self.assertTrue(result["ok"])
		row = frappe.db.get_value(
			INSTALLATION, name, ["enabled", "schedule_enabled", "next_run_at"], as_dict=True
		)
		self.assertEqual(row.enabled, 0)
		self.assertEqual(row.schedule_enabled, 0)
		self.assertIsNone(row.next_run_at)

	def test_scheduler_retires_a_raw_legacy_row_without_dispatch(self):
		name = self._legacy_install(enabled=1, schedule_enabled=1)
		row = frappe.get_doc(INSTALLATION, name)
		with (
			patch.object(agent_scheduler, "_launch_audit") as launch,
			patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty,
		):
			agent_scheduler._sweep_one(row, now_datetime(), set())
		launch.assert_not_called()
		mark_catalog_dirty.assert_called_once_with()
		state = frappe.db.get_value(
			INSTALLATION, name, ["enabled", "schedule_enabled", "next_run_at"], as_dict=True
		)
		self.assertEqual((state.enabled, state.schedule_enabled, state.next_run_at), (0, 0, None))
		self.assertEqual(frappe.db.count(RUN, {"installation": name}), 0)

	def test_fleet_payload_omits_reorder_from_both_legs_when_status_is_tampered(self):
		listing = frappe.get_doc(LISTING, REORDER_AGENT)
		listing.append("allowed_users", {"user": OWNER})
		listing.save(ignore_permissions=True)
		self._publish_reorder_raw()
		self._legacy_install(enabled=1)

		for payload in (agent_catalog.build_agent_push_payload(), agent_catalog.build_agent_push_payload(owner=OWNER)):
			self.assertNotIn(f"agent-{REORDER_AGENT}", [entry["slug"] for entry in payload])

	def test_legacy_delegate_and_direct_generic_writes_are_refused(self):
		cap = {
			"run": "RRO-LEGACY-RUN",
			"agent": REORDER_AGENT,
			"legacy": True,
			"tools_allow": [],
			"nature": "operator",
			"writes": [{"doctype": "Material Request", "mode": "draft"}],
			"run_as": OWNER,
		}
		for tool in ("create_doc", "update_doc", "submit_doc", "cancel_doc", "amend_doc", "delete_doc"):
			denial = _delegate_capability.tool_denial("ignored", tool, cap=cap)
			self.assertIsNotNone(denial)
			self.assertTrue(denial["fatal"])

		before = frappe.db.count("Material Request")
		with patch.object(_delegate_write_caps, "resolve_delegate", return_value=cap):
			with self.assertRaises(PermissionDeniedError):
				create_doc("Material Request", {"material_request_type": "Purchase"})
			with self.assertRaises(PermissionDeniedError):
				update_doc("Material Request", "RRO-NO-WRITE", {"status": "Stopped"})
		self.assertEqual(frappe.db.count("Material Request"), before)

	def test_legacy_delegate_dispatch_stops_before_a_generic_write(self):
		installation = self._legacy_install()
		run, session_key = self._legacy_run(installation)
		with patch.object(api, "_run_tool") as run_tool:
			result = api._dispatch_from_session(
				OWNER,
				session_key,
				"create_doc",
				{"doctype": "Material Request", "values": {"material_request_type": "Purchase"}},
			)
		run_tool.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "CapabilityDeniedError")
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "failed")

	def test_migration_retires_legacy_installs_and_live_bearers(self):
		name = self._legacy_install(enabled=1, schedule_enabled=1)
		run, session_key = self._legacy_run(name)
		terminal_run, terminal_session_key = self._legacy_run(name, suffix="terminal", status="completed")
		# This key is not one of the two historical prefixes. The close must still
		# revoke it because the Run row says it is a bearer for this agent.
		frappe.db.set_value(RUN, terminal_run, "session_key", ODD_SESSION_KEY, update_modified=False)
		frappe.db.set_value(SESSION, terminal_session_key, "session_key", ODD_SESSION_KEY, update_modified=False)
		frappe.db.commit()
		self._publish_reorder_raw()
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty:
			patch_module.execute()
		mark_catalog_dirty.assert_called_once_with(strict=True)
		row = frappe.db.get_value(
			INSTALLATION, name, ["enabled", "schedule_enabled", "next_run_at"], as_dict=True
		)
		self.assertEqual((row.enabled, row.schedule_enabled, row.next_run_at), (0, 0, None))
		self.assertEqual(frappe.db.get_value(LISTING, REORDER_AGENT, "status"), CLOSED_STATUS)
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "failed")
		self.assertEqual(frappe.db.get_value(RUN, run, "error"), RUN_UNAVAILABLE)
		self.assertEqual(frappe.db.get_value(RUN, terminal_run, "status"), "completed")
		self.assertFalse(frappe.db.exists(SESSION, {"session_key": session_key}))
		self.assertFalse(frappe.db.exists(SESSION, {"session_key": ODD_SESSION_KEY}))

		# A repeated manual execution keeps the state closed and asks for Apply again.
		with patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty:
			patch_module.execute()
		mark_catalog_dirty.assert_called_once_with(strict=True)

	def test_migration_fails_if_a_running_legacy_run_stays_running(self):
		"""A swallowed scheduler error must not let the never-skip close complete."""
		installation = self._legacy_install(enabled=1)
		run, _session_key = self._legacy_run(installation)
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with (
			patch.object(agents_api, "_mark_catalog_dirty"),
			patch.object(agent_scheduler, "fail_run", return_value=False),
			self.assertRaises(frappe.ValidationError),
		):
			patch_module.execute()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")

	def test_migration_requests_apply_without_a_legacy_install(self):
		self._publish_reorder_raw()
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty") as mark_catalog_dirty:
			patch_module.execute()
		mark_catalog_dirty.assert_called_once_with(strict=True)
		self.assertEqual(frappe.db.get_value(LISTING, REORDER_AGENT, "status"), CLOSED_STATUS)

	def test_migration_revokes_orphan_bearers_from_both_key_formats(self):
		keys = (f"{CURRENT_SESSION_PREFIX}orphan", f"{HISTORIC_SESSION_PREFIX}orphan")
		for key in keys:
			frappe.get_doc({"doctype": SESSION, "session_key": key, "user": OWNER}).insert(
				ignore_permissions=True
			)
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty"):
			patch_module.execute()
		for key in keys:
			self.assertFalse(frappe.db.exists(SESSION, {"session_key": key}))

	def test_deleted_orphan_bearer_cannot_use_an_auth_user_captured_before_close(self):
		"""The final dispatch gate closes the auth-before-migration session race."""
		session_key = f"{HISTORIC_SESSION_PREFIX}captured-auth-user"
		frappe.get_doc({"doctype": SESSION, "session_key": session_key, "user": OWNER}).insert(
			ignore_permissions=True
		)
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty"):
			patch_module.execute()
		self.assertFalse(frappe.db.exists(SESSION, {"session_key": session_key}))
		# ``_dispatch_from_session`` receives the user that plugin auth read before
		# the delete. It must still stop before any generic ERP tool can run.
		with (
			patch.object(api, "_run_tool") as run_tool,
			patch.object(api, "_persist_and_publish_tool_call"),
		):
			result = api._dispatch_from_session(
				OWNER,
				session_key,
				"create_doc",
				{"doctype": "Material Request", "values": {"material_request_type": "Purchase"}},
			)
		run_tool.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "PermissionError")

	def test_migration_cancels_old_confirmation_credentials_before_any_dispatch(self):
		"""An old generic RRO confirmation cannot create a Material Request after close."""
		durable_token, conversation = self._historic_durable_card()
		post_close_token, _post_close_conversation = self._historic_durable_card()
		# Model a normal card proposed after activation. The migration must not
		# cancel it while it drains the old untagged RRO card.
		frappe.db.set_value(
			PENDING_ACTION,
			post_close_token,
			PENDING_ACTION_FENCE_FIELD,
			CLOSURE_GENERATION,
			update_modified=False,
		)
		frappe.db.commit()
		legacy_token = self._historic_legacy_token()
		# This is the interval after durable activation while the migration cleans
		# up old cards. A normal newly minted token must keep its generation.
		activate_legacy_confirmation_fence()
		frappe.db.commit()
		post_close_legacy_token = pending_confirm_legacy.mint(
			conversation="",
			owner=OWNER,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "post-close migration race"}},
			run_id="",
		)
		self.assertEqual(
			pending_confirm_legacy.peek(post_close_legacy_token)[PENDING_ACTION_FENCE_FIELD], CLOSURE_GENERATION
		)
		before = frappe.db.count("Material Request")
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with (
			patch.object(agents_api, "_mark_catalog_dirty"),
			patch.object(api, "dispatch_confirmed") as dispatch_confirmed,
		):
			frappe.set_user("Administrator")
			patch_module.execute()
			frappe.set_user(OWNER)
			durable_result = actions_api._confirm_core(durable_token, conversation)
			legacy_result = actions_api._confirm_core(legacy_token, conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(durable_result["ok"])
		self.assertFalse(legacy_result["ok"])
		self.assertEqual(frappe.db.get_value(PENDING_ACTION, durable_token, "status"), "Cancelled")
		self.assertEqual(frappe.db.get_value(PENDING_ACTION, durable_token, "reason_code"), "safety_update")
		self.assertTrue(frappe.db.get_value(PENDING_ACTION, durable_token, "settled"))
		self.assertIsNone(frappe.db.get_value(PENDING_ACTION, durable_token, "sealed_call"))
		self.assertEqual(frappe.db.get_value(PENDING_ACTION, post_close_token, "status"), "Pending")
		self.assertIsNone(pending_confirm_legacy.peek(legacy_token))
		self.assertIsNotNone(pending_confirm_legacy.peek(post_close_legacy_token))
		self.assertEqual(frappe.db.count("Material Request"), before)

	def test_close_fence_rejects_an_old_token_that_arrives_after_the_cleanup(self):
		"""The runtime fence closes a token an old worker races in after the scan."""
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty"):
			patch_module.execute()
		legacy_token = self._historic_legacy_token()
		before = frappe.db.count("Material Request")
		frappe.set_user(OWNER)
		with patch.object(api, "dispatch_confirmed") as dispatch_confirmed:
			result = actions_api._confirm_core(legacy_token)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")
		self.assertIsNone(pending_confirm_legacy.peek(legacy_token))
		self.assertEqual(frappe.db.count("Material Request"), before)

	def test_close_fence_reports_a_safety_update_for_approve_and_run_too(self):
		"""The alternate confirm endpoint does not mislabel this as a Redis outage."""
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty"):
			patch_module.execute()
		legacy_token = self._historic_legacy_token()
		frappe.set_user(OWNER)
		with (
			patch.object(actions_api, "_approve_run_refusal", return_value=None),
			patch.object(api, "dispatch_confirmed") as dispatch_confirmed,
		):
			result = actions_api.approve_and_run(legacy_token)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")

	def test_approve_and_run_refuses_an_unmarked_token_before_dispatch(self):
		"""The alternate endpoint cannot pass an untagged generic credential."""
		conversation = self._confirmation_conversation()
		legacy_token = self._historic_legacy_token(conversation=conversation)
		frappe.set_user(OWNER)
		with (
			patch.object(actions_api, "_approve_run_refusal", return_value=None),
			patch.object(api, "dispatch_confirmed") as dispatch_confirmed,
		):
			result = actions_api.approve_and_run(legacy_token, conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")

	def test_close_fence_rejects_an_old_durable_card_that_arrives_after_the_cleanup(self):
		"""The durable executor checks the fence under its row lock before dispatch."""
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty"):
			patch_module.execute()
		token, conversation = self._historic_durable_card()
		before = frappe.db.count("Material Request")
		with patch.object(api, "dispatch_confirmed") as dispatch_confirmed:
			result = actions_api._confirm_core(token, conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")
		self.assertEqual(frappe.db.get_value(PENDING_ACTION, token, "status"), "Failed")
		self.assertTrue(frappe.db.get_value(PENDING_ACTION, token, "settled"))
		self.assertIsNone(frappe.db.get_value(PENDING_ACTION, token, "sealed_call"))
		self.assertEqual(frappe.db.count("Material Request"), before)

	def test_durable_unmarked_card_is_refused_before_it_can_claim_or_dispatch(self):
		"""A durable historic generic card fails closed before any tool call."""
		token, conversation = self._historic_durable_card()
		with patch.object(api, "dispatch_confirmed") as dispatch_confirmed:
			result = actions_api._confirm_core(token, conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")
		self.assertEqual(frappe.db.get_value(PENDING_ACTION, token, "status"), "Failed")

	def test_unmarked_legacy_import_never_reaches_generic_dispatch(self):
		"""The untagged path cannot stage or queue an import during migration."""
		conversation = self._confirmation_conversation()
		token = self._historic_legacy_token(
			conversation=conversation,
			tool="run_import",
			args={"doctype": "Material Request", "file_url": "/private/files/no-import.csv"},
		)
		frappe.set_user(OWNER)
		with patch.object(api, "dispatch_confirmed") as dispatch_confirmed:
			result = actions_api._confirm_core(token, conversation)
		dispatch_confirmed.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason_code"], "safety_update")

	def test_post_close_legacy_tokens_are_stamped_and_stay_confirmable(self):
		"""The close does not break later ordinary legacy confirmation cards."""
		patch_module = importlib.import_module("jarvis.patches.v2_31_disable_reorder_replenishment_installations")
		with patch.object(agents_api, "_mark_catalog_dirty"):
			patch_module.execute()
		token = pending_confirm_legacy.mint(
			conversation="",
			owner=OWNER,
			tool="create_doc",
			args={"doctype": "ToDo", "values": {"description": "post-close legacy token"}},
			run_id="",
		)
		self.assertEqual(pending_confirm_legacy.peek(token)[PENDING_ACTION_FENCE_FIELD], CLOSURE_GENERATION)
		self.assertIsNotNone(pending_confirm_legacy.consume(token, owner=OWNER, conversation=""))

	def test_settings_save_cannot_erase_the_pre_patch_log_fence(self):
		"""A stale Settings write cannot touch the separate fence namespace."""
		# update_single deletes and recreates every field in the *Settings*
		# namespace. Load this document before activation to model the stale object
		# that prompted the dedicated fence namespace.
		settings = frappe.get_single("Jarvis Settings")
		activate_legacy_confirmation_fence()
		frappe.db.commit()
		settings.update_single(settings.get_valid_dict())
		with patch("jarvis.chat.reorder_replenishment_gate._closure_patch_logged", return_value=False):
			self.assertEqual(legacy_confirmation_generation(), CLOSURE_GENERATION)

	def test_settings_save_cannot_clear_the_server_owned_apply_marker(self):
		"""A crafted settings save cannot keep a retired agent in the fleet roster."""
		fields = ("agent_catalog_dirty", "agent_catalog_version")
		before = {field: frappe.db.get_single_value("Jarvis Settings", field, cache=False) for field in fields}
		try:
			frappe.db.set_single_value("Jarvis Settings", "agent_catalog_dirty", 1)
			frappe.db.set_single_value("Jarvis Settings", "agent_catalog_version", 37)
			frappe.db.commit()
			settings = frappe.get_single("Jarvis Settings")
			settings.agent_catalog_dirty = 0
			settings.agent_catalog_version = 0
			# Avoid unrelated LLM network/configuration work. The real validate and
			# update_single path still run, which is the crafted-save threat surface.
			with patch.object(settings, "on_update"):
				settings.save(ignore_permissions=True)
			self.assertEqual(frappe.utils.cint(frappe.db.get_single_value("Jarvis Settings", "agent_catalog_dirty")), 1)
			self.assertEqual(
				frappe.utils.cint(frappe.db.get_single_value("Jarvis Settings", "agent_catalog_version")), 37
			)
		finally:
			for field, value in before.items():
				frappe.db.set_single_value("Jarvis Settings", field, value)
			frappe.db.commit()

	def test_activation_updates_a_preexisting_blank_primary_key_row(self):
		"""A blank fixed row cannot make the pre-Patch-Log close inactive."""
		frappe.db.sql(
			"""INSERT INTO `tabJarvis Reorder Safety Fence`
			(`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
			`legacy_confirmation_generation`)
			VALUES (%s, NOW(6), NOW(6), %s, %s, 0, 0, '')""",
			(FENCE_NAME, "Administrator", "Administrator"),
		)
		frappe.db.commit()
		with patch("jarvis.chat.reorder_replenishment_gate._closure_patch_logged", return_value=False):
			self.assertIsNone(legacy_confirmation_generation())
			activate_legacy_confirmation_fence()
			self.assertEqual(legacy_confirmation_generation(), CLOSURE_GENERATION)

	def test_pre_model_sync_fence_read_is_inactive_without_a_table(self):
		"""New code does not crash while model sync has not made the table yet."""
		with (
			patch.object(frappe.db, "table_exists", return_value=False),
			patch("jarvis.chat.reorder_replenishment_gate._closure_patch_logged", return_value=False),
		):
			self.assertIsNone(legacy_confirmation_generation())
			self.assertEqual(stamp_pending_confirmation(), "")
