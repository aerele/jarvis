"""C2 self-heal: the run:end terminal carries any live parked confirmation card
for the conversation (settlement._extra_with_pending), so a card whose best-effort
action:pending push was missed re-surfaces at turn-end without a manual reload.

This is the shared enrichment used by BOTH settlement's authoritative terminal
publish and finalize's re-publish backstop, so testing it once covers both sites.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import pending_confirm, settlement


class TestTerminalCarriesPending(FrappeTestCase):
	_OWNER = "owner-c2@example.invalid"
	_CONV = "conv-c2"

	def setUp(self):
		# The per-owner index lives in Redis (not rolled back with the DB); clear it.
		frappe.cache().delete_value(pending_confirm._OWNER_PREFIX + self._OWNER)

	def _park(self):
		return pending_confirm.mint(
			conversation=self._CONV,
			owner=self._OWNER,
			tool="create_doc",
			args={"docs": [{"doctype": "ToDo", "values": {"description": "c2"}}]},
			run_id="",
			preview={"preview": True},
		)

	def test_terminal_carries_parked_card(self):
		self._park()
		base = {"enrichment_pending": True}
		out = settlement._extra_with_pending(base, self._OWNER, self._CONV)
		# The parked card rides the terminal in the client-facing item shape, and the
		# existing terminal fields are preserved.
		self.assertTrue(out.get("enrichment_pending"))
		self.assertEqual(len(out["pending"]), 1)
		self.assertEqual(out["pending"][0]["tool"], "create_doc")
		self.assertIn("token", out["pending"][0])
		self.assertTrue(out["pending"][0]["pending_card_safe"])
		# The caller's dict is NOT mutated (a new dict is returned).
		self.assertNotIn("pending", base)

	def test_no_card_leaves_extra_unchanged(self):
		# The common case: no parked card -> the input is returned unchanged (no new
		# dict, no empty ``pending`` key bloating every terminal).
		base = {"enrichment_pending": True}
		out = settlement._extra_with_pending(base, self._OWNER, self._CONV)
		self.assertNotIn("pending", out)
		self.assertIs(out, base)

	def test_no_owner_leaves_extra_unchanged(self):
		base = {"stopped": True}
		self.assertIs(settlement._extra_with_pending(base, None, self._CONV), base)

	def test_other_conversation_card_does_not_ride_this_terminal(self):
		# A card parked in a DIFFERENT (bound) conversation must not leak onto this
		# conversation's terminal.
		pending_confirm.mint(
			conversation="conv-other",
			owner=self._OWNER,
			tool="create_doc",
			args={"docs": [{"doctype": "ToDo", "values": {"description": "other"}}]},
			run_id="",
			preview={"preview": True},
		)
		out = settlement._extra_with_pending({"enrichment_pending": True}, self._OWNER, self._CONV)
		self.assertNotIn("pending", out)

	def test_terminal_never_carries_a_stale_durable_card(self):
		"""run:end and run:error share this helper, so both fail closed here."""
		owner = frappe.session.user
		conversation = frappe.get_doc(
			{"doctype": "Jarvis Conversation", "title": "terminal stale durable fixture"}
		).insert(ignore_permissions=True)

		def cleanup():
			frappe.db.delete("Jarvis Chat Message", {"conversation": conversation.name})
			frappe.db.delete("Jarvis Pending Action", {"conversation": conversation.name})
			frappe.db.delete("Jarvis Conversation", {"name": conversation.name})
			frappe.db.commit()

		self.addCleanup(cleanup)
		token = pending_confirm.mint(
			conversation=conversation.name,
			owner=owner,
			tool="create_doc",
			args={"docs": [{"doctype": "ToDo", "values": {"description": "c2-stale"}}]},
			run_id="",
			preview={"preview": True},
		)
		frappe.db.set_value(
			"Jarvis Pending Action",
			token,
			"agent_close_fences",
			'{"ticket-triage-operator":"old"}',
			update_modified=False,
		)
		frappe.db.commit()
		out = settlement._extra_with_pending(
			{"enrichment_pending": True}, owner, conversation.name
		)
		self.assertNotIn("pending", out)
