"""A browser can supply a request, never an execution identity or session."""

import json
import uuid
from unittest.mock import patch

import frappe

from jarvis.chat import collaboration as c
from jarvis.chat import pump
from jarvis.tests._collaboration_committed import CommittedCollaborationCase
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, SHARE, as_user


class TestCollaborationSend(CommittedCollaborationCase):
	def setUp(self):
		super().setUp()
		self.nonce = uuid.uuid4().hex
		self.patches = [
			patch.object(pump, "ensure_pump"),
			patch.object(pump, "lpush_wake"),
			patch.object(
				pump,
				"transport_predicates_from_row",
				return_value={"mode": "pump", "pump_active": True, "configured": True},
			),
			patch("jarvis.chat.policy.validate_can_send", return_value=(True, None)),
		]
		for item in self.patches:
			item.start()
			self.addCleanup(item.stop)

	def send(self, text="recipient request", nonce=None):
		from jarvis.chat.collaboration_api import send_message

		with as_user(RECIPIENT):
			return send_message(self.conversation, text, nonce or self.nonce)

	def count(self):
		return (
			frappe.db.count("Jarvis Chat Message", {"conversation": self.conversation}),
			frappe.db.count(c.BINDING, {"conversation": self.conversation}),
			frappe.db.count("Jarvis Chat Turn", {"conversation": self.conversation}),
		)

	def test_legacy_and_mixed_attachment_resends_keep_the_original_run(self):
		from jarvis.chat.collaboration_api import send_message

		with as_user(RECIPIENT):
			files = []
			for label in ("first", "second"):
				doc = frappe.get_doc(
					{
						"doctype": "File",
						"file_name": label + uuid.uuid4().hex + ".txt",
						"content": label + uuid.uuid4().hex,
						"is_private": 1,
					}
				).insert(ignore_permissions=True)
				files.append(doc)
				self.records.append(("File", doc.name))
			frappe.db.commit()
			for atts in (
				[{"file_url": files[0].file_url}],
				[{"file_url": files[0].file_url}, {"file_url": files[1].file_url, "file_id": files[1].name}],
			):
				nonce = uuid.uuid4().hex
				first = send_message(self.conversation, "Read", nonce, atts)
				second = send_message(self.conversation, "Read", nonce, atts)
				self.assertEqual(
					(first["run_id"], first["message_id"]), (second["run_id"], second["message_id"])
				)
				with self.assertRaises(frappe.ValidationError):
					send_message(
						self.conversation,
						"Read",
						nonce,
						[{"file_url": files[0].file_url, "file_id": files[1].name}],
					)

	def test_exact_file_id_survives_shared_send_and_replay_scope(self):
		from jarvis.chat.collaboration_api import send_message
		from jarvis.tests.test_archive_sources import archive
		from jarvis.tools.query_file import query_file

		with as_user(RECIPIENT):
			content = archive([("bank.csv", "Amount\n0.10\n")])
			files = []
			for label in ("first", "second"):
				doc = frappe.get_doc(
					{
						"doctype": "File",
						"file_name": label + uuid.uuid4().hex + ".zip",
						"content": content,
						"is_private": 1,
					}
				).insert(ignore_permissions=True)
				files.append(doc)
				self.records.append(("File", doc.name))
			self.assertEqual(files[0].file_url, files[1].file_url)
			frappe.db.commit()
			with self.assertRaises(frappe.ValidationError):
				send_message(self.conversation, "Read", self.nonce, [{"file_url": files[0].file_url}])
			atts = [{"file_id": files[1].name, "file_url": files[1].file_url}]
			result = send_message(self.conversation, "Read", self.nonce, atts)
			turn = frappe.get_doc("Jarvis Chat Turn", result["run_id"])
			self.assertEqual(json.loads(turn.dispatch_payload)["attachments"][0]["file_id"], files[1].name)
			canvas = json.loads(frappe.db.get_value("Jarvis Chat Message", result["message_id"], "canvas"))
			self.assertEqual(canvas[0]["file_id"], files[1].name)
			self.assertEqual(
				send_message(self.conversation, "Read", self.nonce, atts)["run_id"], result["run_id"]
			)
			with self.assertRaises(frappe.ValidationError):
				send_message(
					self.conversation,
					"Read",
					self.nonce,
					[{"file_id": files[0].name, "file_url": files[0].file_url}],
				)
			with patch.dict(frappe.conf, {"jarvis_archive_reading_enabled": True}):
				inventory = query_file(file_id=files[1].name)
				view = query_file(
					file_id=files[1].name, archive_version=inventory["archive_version"], members=["m1"]
				)
				answer = query_file(
					file_id=files[1].name,
					archive_version=inventory["archive_version"],
					members=["m1"],
					version=view["version"],
					sql="SELECT A FROM m1_sheet1 WHERE _row=2",
				)
				self.assertEqual(answer["rows"], [["0.10"]])

	def test_recipient_send_is_atomically_admitted_as_recipient(self):
		result = self.send()
		self.assertTrue(result["ok"])
		self.assertEqual(self.count(), (2, 1, 1))
		turn = frappe.get_doc("Jarvis Chat Turn", result["run_id"])
		seed = frappe.get_doc("Jarvis Chat Message", turn.seed_message)
		self.assertEqual((turn.actor, seed.owner, seed.actor), (RECIPIENT,) * 3)
		self.assertIsNone(turn.collaboration_send_started_at)
		self.assertEqual(result["message_id"], seed.name)

	def test_same_nonce_retries_the_same_request_without_extra_rows(self):
		first, second = self.send(), self.send()
		self.assertEqual(first["run_id"], second["run_id"])
		self.assertEqual(first["message_id"], second["message_id"])
		self.assertEqual(self.count(), (2, 1, 1))

	def test_reused_nonce_with_changed_body_is_refused_without_losing_new_text(self):
		self.send()
		with self.assertRaises(frappe.ValidationError):
			self.send("different newer request")
		self.assertEqual(self.count(), (2, 1, 1))

	def test_view_grant_and_outsider_cannot_post(self):
		from jarvis.chat.collaboration_api import send_message

		frappe.db.set_value(SHARE, self.share, "access_mode", "View")
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			self.send()
		with as_user(OUTSIDER), self.assertRaises(frappe.PermissionError):
			send_message(self.conversation, "outsider", self.nonce)
		self.assertEqual(self.count(), (1, 0, 0))

	def test_frappe_rpc_drops_forged_actor_and_uses_authenticated_sender(self):
		from jarvis.chat.collaboration_api import send_message

		with as_user(RECIPIENT):
			result = frappe.call(
				send_message,
				conversation=self.conversation,
				message="legitimate recipient",
				request_id=self.nonce,
				actor=OWNER,
				session_key="owner-global-session",
				collaboration_binding="forged",
			)
		self.assertTrue(result["ok"])
		self.assertEqual(frappe.db.get_value("Jarvis Chat Turn", result["run_id"], "actor"), RECIPIENT)

	def test_owner_conversation_model_and_thinking_are_not_sent_as_recipient_defaults(self):
		frappe.db.set_value(
			c.CONVERSATION,
			self.conversation,
			{"model_override": "OWNER_PRIVATE_MODEL", "thinking_override": "high"},
		)
		frappe.db.commit()
		result = self.send()
		payload = frappe.db.get_value("Jarvis Chat Turn", result["run_id"], "dispatch_payload") or "{}"
		self.assertNotIn("OWNER_PRIVATE_MODEL", payload)
		self.assertNotIn("thinking_override", json.loads(payload))

	def test_billing_refusal_writes_no_seed_binding_or_turn(self):
		with patch("jarvis.chat.policy.validate_can_send", return_value=(False, "usage_limit")) as gate:
			result = self.send()
		self.assertFalse(result["ok"])
		self.assertEqual(result["reason"], "usage_limit")
		self.assertEqual(gate.call_args.args[0], RECIPIENT)
		self.assertEqual(self.count(), (1, 0, 0))

	def test_oversized_message_is_refused_before_admission(self):
		with self.assertRaises(frappe.ValidationError):
			self.send("x" * (16384 + 1))
		self.assertEqual(self.count(), (1, 0, 0))
