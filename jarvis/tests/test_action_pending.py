"""Tests for the ``action:pending`` realtime event (issue #186, Task 3).

The write-safety gate in ``jarvis.api._run_tool`` parks a gated write and
mints a single-use token in ``pending_confirm`` instead of running it
(Tasks 1+2). THIS module covers the out-of-band delivery of that token to
the human's UI: an ``action:pending`` event published to the owner over the
realtime channel (``jarvis.chat.events.publish_to_user``), carrying the
token that the model-facing return never does.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis import api
from jarvis.chat import pending_confirm
from jarvis.chat.actions_api import confirm_tool


class TestActionPendingEvent(FrappeTestCase):
	def test_park_publishes_action_pending_to_owner_with_token(self):
		desc = "jarvis-test-action-pending-park-030"
		with patch("jarvis.chat.events.publish_to_user") as pub:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
				},
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		# Exactly one action:pending event, published to the owner.
		pub.assert_called_once()
		user_arg, payload = pub.call_args[0]
		self.assertEqual(user_arg, frappe.session.user)
		self.assertEqual(payload["kind"], "action:pending")
		self.assertEqual(payload["tool"], "create_doc")
		self.assertIn("preview", payload)
		self.assertIn("conversation", payload)
		self.assertIn("run_id", payload)
		# The token is valid in the store and bound to this exact call.
		token = payload["token"]
		record = pending_confirm.peek(token)
		self.assertIsNotNone(record)
		self.assertEqual(record["tool"], "create_doc")
		self.assertEqual(record["conversation"], payload["conversation"])

	def test_model_facing_return_still_has_no_token(self):
		desc = "jarvis-test-action-pending-no-token-031"
		with patch("jarvis.chat.events.publish_to_user") as pub:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
				},
			)
		token = pub.call_args[0][1]["token"]
		self.assertNotIn("token", r["data"])
		self.assertNotIn(token, frappe.as_json(r))
		self.assertEqual(r["data"]["status"], "pending_confirmation")

	def test_published_token_confirms_end_to_end(self):
		desc = "jarvis-test-action-pending-e2e-032"
		with patch("jarvis.chat.events.publish_to_user") as pub:
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
				},
			)
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		token = pub.call_args[0][1]["token"]
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))

		res = confirm_tool(token)
		self.assertTrue(res["ok"])
		self.assertTrue(frappe.db.exists("ToDo", {"description": desc}))

	def test_publish_failure_is_swallowed_and_does_not_execute(self):
		desc = "jarvis-test-action-pending-pubfail-033"
		captured = {}
		real_mint = pending_confirm.mint

		def spy(**kwargs):
			token = real_mint(**kwargs)
			captured["token"] = token
			return token

		with (
			patch("jarvis.chat.pending_confirm.mint", side_effect=spy),
			patch("jarvis.chat.events.publish_to_user", side_effect=RuntimeError("boom")),
		):
			r = api._run_tool(
				"create_doc",
				{
					"doctype": "ToDo",
					"values": {"description": desc},
				},
			)
		# No exception escaped the gate, and it still reports the park.
		self.assertTrue(r["ok"])
		self.assertEqual(r["data"]["status"], "pending_confirmation")
		# The write did NOT execute.
		self.assertFalse(frappe.db.exists("ToDo", {"description": desc}))
		# The token still lives in the store despite the publish failure -
		# a future resync or retry can still surface/confirm it.
		self.assertIsNotNone(pending_confirm.peek(captured["token"]))
