"""Freeze the owner response before the sharing helper extraction.

The checked-in JSON was recorded against the unchanged owner endpoint. Every
identifier and timestamp is fixture-controlled; this test never records or
updates its own expectation. No commit is made by the fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.api import get_conversation

OWNER = "jarvis-owner-characterization@example.com"
CONVERSATION = "CHAR-OWNER-CONVERSATION"
RUN = "CHAR-OWNER-RUN"
STAMP = datetime(2021, 1, 1, 12, 0, 0)
GOLDEN = Path(__file__).parent / "fixtures" / "chat_owner_response.json"
CASES = {
	"unpaged": {},
	"newest": {"limit": 3},
	"before": {"limit": 3, "before": 8},
	"after": {"limit": 3, "after": 4},
	"refresh": {"limit": 3, "refresh": json.dumps(["CHAR-MESSAGE-03", "CHAR-MESSAGE-05"])},
}


def owner_fixture() -> None:
	"""Create one rich, deterministic transcript inside the current transaction."""
	frappe.set_user("Administrator")
	if not frappe.db.exists("User", OWNER):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": OWNER,
				"first_name": "Characterization",
				"last_name": "Owner",
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		user.add_roles("Jarvis User")
	frappe.set_user(OWNER)
	frappe.get_doc({"doctype": "Jarvis Conversation", "title": "Owner contract"}).insert(
		ignore_permissions=True, set_name=CONVERSATION
	)
	frappe.db.set_value(
		"Jarvis Conversation",
		CONVERSATION,
		{
			"session_key": "characterization-session",
			"model_override": "characterization-model",
			"thinking_override": "high",
			"auto_mode": 1,
			"last_active_at": STAMP,
		},
		update_modified=False,
	)
	rows = [
		{"role": "user", "content": "Question one", "origin": "human"},
		{
			"role": "assistant",
			"content": "Answer one",
			"steps": json.dumps([{"text": "Looked up records", "at": str(STAMP)}]),
			"canvas": json.dumps(
				[
					{
						"name": "owner.txt",
						"type": "file",
						"title": "Owner attachment",
						"file_url": "/private/files/owner.txt",
					}
				]
			),
			"reply_duration_ms": 120,
		},
		{
			"role": "tool",
			"tool_name": "submit_doc",
			"tool_status": "pending",
			"tool_call_id": "characterization-pending",
			"pending_card": json.dumps({"title": "Owner confirmation", "token": "owner-only-token"}),
			"expires_at": "2035-01-01 00:00:00",
		},
		{"role": "user", "content": "Question two", "origin": "human"},
		{
			"role": "tool",
			"tool_name": "get_list",
			"tool_status": "completed",
			"tool_args": '{"doctype":"Sales Invoice"}',
			"tool_result": json.dumps(
				{
					"ok": True,
					"data": {"doctype": "Sales Invoice", "rows": [{"name": "SINV-CHAR", "note": "x" * 2100}]},
				}
			),
			"tool_call_ids": '["characterization-call"]',
		},
		{"role": "assistant", "content": "Answer two", "error": "Owner-only raw error"},
		{"role": "user", "content": "Hidden system prompt", "hidden": 1},
		{"role": "user", "content": "Question three", "origin": "human"},
		{
			"role": "assistant",
			"content": "Checking records.\n\nAnswer three",
			"streaming": 1,
			"model": "characterization-model",
			"provider": "characterization-provider",
		},
		{
			"role": "tool",
			"tool_name": "get_doc",
			"tool_status": "completed",
			"action_outcome": "confirmed",
			"tool_result": '{"ok":true}',
		},
	]
	for seq, values in enumerate(rows, 1):
		name = f"CHAR-MESSAGE-{seq:02d}"
		doc = frappe.get_doc(
			{"doctype": "Jarvis Chat Message", "conversation": CONVERSATION, "seq": seq, **values}
		)
		doc.flags.jarvis_server_write = True
		doc.insert(ignore_permissions=True, set_name=name)
		frappe.db.set_value(
			"Jarvis Chat Message", name, {"creation": STAMP, "modified": STAMP}, update_modified=False
		)
	frappe.get_doc(
		{
			"doctype": "Jarvis Chat Turn",
			"run_id": RUN,
			"conversation": CONVERSATION,
			"relay_target_id": "characterization-target",
			"turn_class": "interactive",
			"state": "streaming",
			"seed_message": "CHAR-MESSAGE-08",
			"assistant_message": "CHAR-MESSAGE-09",
			"enqueued_at": STAMP,
			"pump_epoch": 2,
			"last_event_seq": 17,
		}
	).insert(ignore_permissions=True)


def owner_responses() -> dict:
	"""Freeze clocks and external step cache, retaining real SQL/projection logic."""
	with (
		patch("frappe.utils.now_datetime", return_value=STAMP),
		patch("jarvis.chat.pending_confirm.time.time", return_value=1609502400),
		patch("jarvis.chat.pump._read_run_steps", return_value=["Checking records."]),
		patch(
			"jarvis.chat.pump._read_run_step_lines",
			return_value=[{"text": "Checking records.", "at": str(STAMP)}],
		),
	):
		return {name: get_conversation(CONVERSATION, **kwargs) for name, kwargs in CASES.items()}


class TestOwnerResponseCharacterization(FrappeTestCase):
	def test_owner_response_matches_pre_extraction_recording(self):
		original = frappe.session.user
		frappe.db.savepoint("owner_characterization")
		try:
			owner_fixture()
			actual = owner_responses()
			expected = json.loads(GOLDEN.read_text())
			self.assertEqual(set(actual), set(CASES))
			for name in CASES:
				with self.subTest(call=name):
					self.assertEqual(
						json.dumps(actual[name], sort_keys=True, default=str),
						json.dumps(expected[name], sort_keys=True, default=str),
					)
			self.assertEqual(actual["newest"]["older_pending"][0]["name"], "CHAR-MESSAGE-03")
			live = next(row for row in actual["newest"]["messages"] if row["streaming"])
			self.assertEqual(live["run_id"], RUN)
			self.assertEqual(live["content"], "Answer three")
			self.assertTrue(actual["refresh"]["refreshed"][1]["tool_result_compact"])
		finally:
			frappe.db.rollback(save_point="owner_characterization")
			frappe.set_user(original)
