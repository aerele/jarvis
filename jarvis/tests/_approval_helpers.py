"""Shared fixture for the approval decision-message tests (jarvis-admin-v2#793): a
conversation plus one decided Chat question, built straight into the DB. It lives here so no
test module imports another test module for it."""

from __future__ import annotations

from uuid import uuid4

import frappe

from jarvis.tests._pending_action_helpers import as_user, ensure_user

AR = "Jarvis Approval Request"
CONV = "Jarvis Conversation"


class DecisionMessageFixture:
	"""A conversation plus one decided Chat question, built straight into the DB."""

	def setUp(self):
		super().setUp()
		tag = uuid4().hex[:8]
		self.owner = ensure_user(f"zz793-{tag}-owner@example.com")
		self.decider = ensure_user(f"zz793-{tag}-decider@example.com")
		frappe.db.set_value("User", self.decider, "full_name", "Dee Cider")
		self.convs, self.rows = [], []
		self.addCleanup(self._cleanup)

	def _cleanup(self):
		frappe.db.rollback()
		for name in self.rows:
			frappe.db.delete(AR, {"name": name})
		for name in self.convs:
			frappe.db.delete(CONV, {"name": name})
		for user in (self.owner, self.decider):
			frappe.db.delete("Notification Log", {"for_user": user})
			if frappe.db.exists("User", user):
				frappe.delete_doc("User", user, ignore_permissions=True, force=True)
		frappe.db.commit()

	def make_conversation(self, title="Customer outstanding", *, file_box=0):
		with as_user(self.owner):
			conv = frappe.get_doc({"doctype": CONV, "title": title or "New chat"}).insert(
				ignore_permissions=True
			)
		if not title:
			frappe.db.set_value(CONV, conv.name, "title", "", update_modified=False)
		if file_box:
			frappe.db.set_value(CONV, conv.name, "file_box", 1, update_modified=False)
		self.convs.append(conv.name)
		return conv.name

	def make_row(
		self,
		*,
		conversation=None,
		source="Chat",
		question="How should I get the recipient addresses?",
		decision="Use the customer master",
		context_md="",
		status="Approved",
		title="Recipient addresses",
	):
		conversation = conversation or self.make_conversation()
		with as_user(self.owner):
			doc = frappe.get_doc(
				{
					"doctype": AR,
					"title": title,
					"source": source,
					"conversation": conversation,
					"question": question,
					"context_md": context_md,
					"status": "Pending",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value(
			AR,
			doc.name,
			{"status": status, "decision": decision, "decided_by": self.decider},
			update_modified=False,
		)
		self.rows.append(doc.name)
		return frappe.get_doc(AR, doc.name)
