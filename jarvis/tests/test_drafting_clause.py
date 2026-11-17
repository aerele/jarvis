"""The drafting note in the per-turn ``[Context: ...]`` bracket: which turns get it.

``turn_handler._drafting_clause`` decides from the site flag, the conversation's own flags,
the binding and the origin of the turn's user row, so the rule runs without a site. The
assembled prompt (one bracket, the order, byte-identity with the flag off) is in
``test_live_draft`` (``TestDraftingClause``, ``TestParkedToolDraft``).
"""

import json
import unittest
from unittest.mock import patch

from jarvis.chat import turn_handler
from jarvis.chat.turn_handler import DRAFTING_CLAUSE, _drafting_clause

ENABLED = "jarvis.chat.draft_card.enabled"


def _banned_terms() -> list[str]:
	"""The words the runtime-branding guard refuses in any file, read from the policy the CI
	check itself loads (the words are not spelled out here: this file is scanned too)."""
	from jarvis.ci.inputs import decode

	return [term.lower() for term in json.loads(decode("policy"))["terms"]]


class TestDraftingClauseRule(unittest.TestCase):
	def clause(self, conv=None, binding=None, origin="human", live="", flag=True):
		with patch(ENABLED, return_value=flag):
			return _drafting_clause(conv or {}, binding, origin, live)

	def test_an_ordinary_turn_gets_the_note_and_the_flag_off_gets_nothing(self):
		"""The site flag is the only switch for everyone else: if the helper ignored it, every
		customer site would tell the agent to draft with a tool that parks no draft card."""
		self.assertEqual(self.clause(), DRAFTING_CLAUSE)
		self.assertEqual(self.clause(flag=False), "")

	def test_a_shared_chat_gets_nothing(self):
		"""A shared chat's draft card UI is unverified: the binding must keep the note out."""
		self.assertEqual(self.clause(binding=object()), "")

	def test_a_turn_a_person_does_not_start_gets_nothing(self):
		"""A macro step, a system or agent-run turn, a File Box drop or a delegated re-entry has no
		person at a card; an app send, the follow-up after a decision and a blank (legacy) origin
		do. Dropping an origin from the skip set, or adding one, changes who is told to draft."""
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
		break, a ``]`` or a backtick fence would break the bracket or the agent's reading of it.
		The persona quotes this constant, and the branding guard refuses its words."""
		text = DRAFTING_CLAUSE
		self.assertTrue(text.startswith("; drafts: "))
		for bad in ("\n", "\r", "]", "[", "`", "—", "–"):
			self.assertNotIn(bad, text)
		self.assertFalse(text.endswith((".", ";", " ")))
		lowered = text.lower()
		for term in _banned_terms():
			self.assertNotIn(term, lowered)


if __name__ == "__main__":
	unittest.main()
