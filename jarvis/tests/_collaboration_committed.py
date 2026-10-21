"""Committed fixtures for API/worker units that intentionally own transactions."""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import collaboration as c
from jarvis.tests._collaboration_helpers import CollaborationCase
from jarvis.tests._share_helpers import OUTSIDER, OWNER, RECIPIENT, SHARE, as_user, make_chat, make_user


@contextmanager
def repeatable_read():
	"""Arm a repeatable snapshot even on sites configured as READ COMMITTED."""
	levels = {
		"READ-UNCOMMITTED": "READ UNCOMMITTED",
		"READ-COMMITTED": "READ COMMITTED",
		"REPEATABLE-READ": "REPEATABLE READ",
		"SERIALIZABLE": "SERIALIZABLE",
	}
	previous = levels[frappe.db.sql("SELECT @@session.tx_isolation")[0][0]]
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
	frappe.db.commit()
	try:
		yield
	finally:
		frappe.db.rollback()
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL " + previous)
		frappe.db.commit()


class CommittedCollaborationCase(FrappeTestCase):
	binding = CollaborationCase.binding
	running_turn = CollaborationCase.running_turn

	def setUp(self):
		super().setUp()
		self.assertEqual(frappe.local.site, "test_jarvis")
		self.previous_user = frappe.session.user
		frappe.set_user("Administrator")
		self.setting = frappe.db.get_single_value("Jarvis Settings", "chat_sharing_enabled")
		self.people_before = {}
		for user in (OWNER, RECIPIENT, OUTSIDER):
			row = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
			self.people_before[user] = (
				row,
				frappe.db.get_values("Has Role", {"parent": user, "parenttype": "User"}, "role", pluck=True),
			)
			make_user(user)
			frappe.db.set_value("User", user, {"enabled": 1, "user_type": "System User"})
		self.conversation, self.message = make_chat()
		frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", 1)
		from jarvis.chat.sharing import set_shares

		with as_user(OWNER):
			set_shares(self.conversation, [RECIPIENT])
		self.share = frappe.db.get_value(
			SHARE, {"conversation": self.conversation, "user": RECIPIENT}, "name"
		)
		frappe.db.set_value(SHARE, self.share, {"access_mode": "Chat", "access_revision": 2})
		frappe.db.commit()
		self.rollout = patch.dict(
			frappe.conf,
			{
				"chat_collaboration_enabled": 1,
				"chat_collaboration_reader_floor": 1,
				"chat_collaboration_runtime_floor": 3,
				"chat_collaboration_skills_root": "/workspace/skills",
			},
		)
		self.rollout.start()
		self.records = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for doctype, name in reversed(self.records):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
		for name in frappe.get_all(
			"Jarvis Pending Action", {"conversation": self.conversation}, pluck="name"
		):
			frappe.db.delete("Jarvis Pending Action Waiter", {"parent": name})
			frappe.db.delete("Jarvis Pending Action", {"name": name})
		frappe.db.delete("Jarvis Approval Request", {"conversation": self.conversation})
		turns = frappe.get_all("Jarvis Chat Turn", {"conversation": self.conversation}, pluck="name")
		if turns:
			frappe.db.delete("Jarvis Turn Effect", {"turn": ["in", turns]})
			frappe.db.delete("Jarvis Turn Usage", {"run_id": ["in", turns]})
		for binding in frappe.get_all(c.BINDING, {"conversation": self.conversation}, pluck="name"):
			frappe.db.delete(c.SESSION, {"collaboration_binding": binding})
		frappe.db.delete(c.BINDING, {"conversation": self.conversation})
		frappe.db.delete("Jarvis Chat Message", {"conversation": self.conversation})
		frappe.delete_doc(c.CONVERSATION, self.conversation, ignore_permissions=True, force=True)
		for user, (before, roles) in self.people_before.items():
			if before:
				doc = frappe.get_doc("User", user)
				doc.enabled, doc.user_type = before.enabled, before.user_type
				doc.set("roles", [{"role": role} for role in roles])
				doc.save(ignore_permissions=True)
			else:
				frappe.delete_doc("User", user, ignore_permissions=True, force=True)
		if self.setting is None:
			frappe.db.delete("Singles", {"doctype": "Jarvis Settings", "field": "chat_sharing_enabled"})
		else:
			frappe.db.set_single_value("Jarvis Settings", "chat_sharing_enabled", self.setting)
		frappe.db.commit()
		self.rollout.stop()
		frappe.clear_cache()
		frappe.set_user(self.previous_user)
		super().tearDown()
