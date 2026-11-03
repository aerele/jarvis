"""Disposable live-user fixtures for collaboration service tests."""

from unittest.mock import patch

import frappe

from jarvis.tests._share_helpers import RECIPIENT, SHARE
from jarvis.tests.test_chat_sharing import SharingCase


class CollaborationCase(SharingCase):
	def setUp(self):
		super().setUp()
		self.share()
		self.rollout = patch.dict(
			frappe.conf,
			{
				"chat_collaboration_enabled": 1,
				"chat_collaboration_reader_floor": 1,
				"chat_collaboration_runtime_floor": 3,
			},
		)
		self.rollout.start()
		self.addCleanup(self.rollout.stop)
		grant = frappe.db.get_value(SHARE, {"conversation": self.conversation, "user": RECIPIENT}, "name")
		frappe.db.set_value(SHARE, grant, {"access_mode": "Chat", "access_revision": 2})

	def binding(self, run="a" * 32, actor=RECIPIENT):
		from jarvis.chat.collaboration_bindings import create_binding

		return create_binding(self.conversation, run, actor=actor)

	def running_turn(self, binding=None):
		from jarvis.chat.collaboration_bindings import insert_message, stamp

		binding = binding or self.binding()
		seed = insert_message(binding, {"role": "user", "content": "actor request"})
		turn = frappe.get_doc(
			{
				"doctype": "Jarvis Chat Turn",
				"run_id": binding.logical_run_id,
				"conversation": self.conversation,
				"relay_target_id": "default",
				"state": "dispatching",
				"collaboration_executor": "pump",
				"seed_message": seed.name,
				"dispatching_at": frappe.utils.now_datetime(),
				"collaboration_send_started_at": frappe.utils.now_datetime(),
			}
		)
		stamp(turn, binding)
		turn.insert(ignore_permissions=True)
		return binding, turn
