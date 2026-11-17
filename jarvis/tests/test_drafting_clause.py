"""The drafting note in the per-turn ``[Context: ...]`` bracket: which turns get it.

``turn_handler._drafting_clause`` decides from the site flag, the conversation's own flags,
the binding, the origin of the turn's user row and the turn's context, so the rule runs
without a site. What needs a site (the origin read from the stored row, the one bracket, the
order, byte-identity with the flag off) is in ``test_live_draft`` (``TestDraftingClause``,
``TestParkedToolDraft``).
"""

import unittest
from unittest.mock import patch

from jarvis.chat import turn_handler
from jarvis.chat.turn_handler import DRAFTING_CLAUSE, _drafting_clause

ENABLED = "jarvis.chat.draft_card.enabled"
ARMED = "jarvis.chat.turn_handler._invokes_armed_skill"


class TestDraftingClauseRule(unittest.TestCase):
	def clause(self, conv=None, binding=None, origin="human", live="", flag=True, armed=False):
		with patch(ENABLED, return_value=flag), patch(ARMED, return_value=armed):
			return _drafting_clause(conv or {}, binding, origin, live, message="hi", chat_user="a@b.c")

	def test_an_ordinary_turn_gets_the_note_and_the_flag_off_gets_nothing(self):
		"""The site flag is the only switch for everyone else: if the helper ignored it, every
		customer site would tell the agent to draft with a tool that parks no draft card."""
		self.assertEqual(self.clause(), DRAFTING_CLAUSE)
		self.assertEqual(self.clause(flag=False), "")

	def test_a_chat_that_runs_writes_without_a_card_gets_nothing(self):
		"""Auto mode, an armed macro, an approved skill run, confirm all and a File Box chat run
		a create / update with no card for a person, and a shared chat's draft card UI is
		unverified: the note would promise a card that never comes. Each flag is read from the
		conversation, so dropping one from the shared predicate shows here."""
		for flag in ("auto_mode", "skip_confirmation", "skill_autorun", "request_autorun", "file_box"):
			with self.subTest(flag=flag):
				self.assertEqual(self.clause(conv={flag: 1}), "")
		self.assertEqual(self.clause(binding=object()), "")

	def test_a_turn_a_person_does_not_start_gets_nothing(self):
		"""A macro step, a system or agent-run turn, a File Box drop and a delegated re-entry have
		no person at a card; an app send, the follow-up after a decision and a blank (legacy)
		origin do. Dropping an origin from the skip set, or adding one, changes who is told to
		draft."""
		for origin in ("macro", "system", "agent", "file_box", "delegated", "anything-else"):
			with self.subTest(origin=origin):
				self.assertEqual(self.clause(origin=origin), "")
		for origin in ("human", "continuation", "board_answer", "", None):
			with self.subTest(origin=origin):
				self.assertEqual(self.clause(origin=origin), DRAFTING_CLAUSE)

	def test_no_builder_chat_gets_the_note_and_an_ordinary_chat_does(self):
		"""A builder pane shows a draft card only as a title and a link, and saves with one tap:
		telling its agent to draft with the tool would send people out of the builder. The skip set
		is the app's own list of builders, so a builder added there is skipped here too."""
		from jarvis.chat.api import _BUILDER_ORIGINS

		self.assertGreaterEqual(_BUILDER_ORIGINS, {"dashboards", "triggers"})
		for page in sorted(_BUILDER_ORIGINS):
			with self.subTest(origin_page=page):
				self.assertEqual(self.clause(conv={"origin_page": page}), "")
		self.assertEqual(self.clause(conv={"origin_page": ""}), DRAFTING_CLAUSE)

	def test_a_turn_that_invokes_an_armed_skill_gets_nothing(self):
		"""The gate offers "Approve & run" on the first covered write of an armed skill's turn, and
		only the ordinary card has that control: a draft card would lose it. Whether the turn
		invokes an armed skill is decided by ``_invokes_armed_skill`` (a site test in
		``test_skill_approve_and_run`` runs it for real)."""
		self.assertEqual(self.clause(armed=True), "")
		self.assertEqual(self.clause(armed=False), DRAFTING_CLAUSE)

	def test_a_failure_leaves_the_note_out_and_never_fails_the_turn(self):
		"""Every neighbouring clause is best-effort: a prompt line must never break a turn. With
		the flag on, a dependency that raises gives no note and one log line, not an error."""
		with (
			patch(ARMED, side_effect=RuntimeError("boom")),
			patch(ENABLED, return_value=True),
			patch("frappe.log_error") as log,
		):
			self.assertEqual(_drafting_clause({}, None, "human", message="hi", chat_user="a@b.c"), "")
		log.assert_called_once()

	def test_a_waiting_typed_draft_is_not_told_to_stop_writing_blocks(self):
		"""The typed draft's reminder says to re-emit the block for a change; the note says never
		to write one. Telling the agent both would break one of them, so the note steps aside;
		a waiting parked draft's reminder (call the tool again) agrees with it and keeps it."""
		typed = turn_handler._draft_clause_text("create ToDo", "Create")
		parked = turn_handler._draft_clause_text("create ToDo", "Create", parked=True)
		self.assertIn("re-emit the block", typed)
		self.assertEqual(self.clause(live=typed), "")
		self.assertEqual(self.clause(live=parked), DRAFTING_CLAUSE)

	def test_the_clause_is_one_safe_line_that_fits_the_bracket(self):
		"""The bracket is ``; ``-joined and closed by ``]`` on one line: a leading ``; ``, a line
		break, a ``]`` or a backtick fence would break the bracket or the agent's reading of it,
		and neighbours use only ``:`` and ``,`` inside a clause, so an inner ``;`` or sentence stop
		would read as the next clause. The persona and plugin refer to the ``drafts:`` key."""
		text = DRAFTING_CLAUSE
		self.assertTrue(text.startswith("; drafts: "))
		body = text[2:]
		for bad in ("\n", "\r", "]", "[", "`", ";", ". ", chr(0x2014), chr(0x2013)):
			self.assertNotIn(bad, body)
		self.assertFalse(text.endswith((".", " ")))


if __name__ == "__main__":
	unittest.main()
