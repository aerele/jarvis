"""The typed card-reply grammar (decision 14, §4.7): approve / reject / neither.

Pure: whole-message equality, like the approval list. A rejection discards cards
AND still reaches the model, so the list must never swallow an ERP verb or a
natural answer word."""

from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approval_phrases as ap


class TestClassifyCardReply(FrappeTestCase):
	def test_the_grammar_table(self):
		table = {
			ap.APPROVE: ("yes", "Go ahead", "confirm all", "confirm 2", "do 1 & 2", "OK!", "Go."),
			ap.REJECT: (
				"no",
				"No.",
				"NO!",
				"nope",
				"nah",
				"n",
				"No thanks",
				"no thank you",
				"don't",
				"Don’t",
				"don‘t do it",
				"do not",
				"discard",
				"Discard it.",
				"never mind",
				"nevermind",
				"forget it",
				"discard all",
				"discard 2",
				"skip 2",
				"drop 1 and 3",
			),
			ap.NEITHER: (
				"no?",
				"no, make it 5",
				"no to all",
				"cancel",
				"cancel it",
				"reject",
				"decline",
				"none",
				"stop",
				"wait",
				"hold on",
				"discard 0",
				"discard the second one",
				"Here are my answers: 1. Acme 2. no",
				"Yes, go ahead.",
				"No, cancel that.",
				"",
				"no " * 30,
			),
		}
		for want, phrases in table.items():
			for phrase in phrases:
				self.assertEqual(ap.classify_card_reply(phrase), want, phrase)

	def test_a_voice_go_and_no_pair_is_pinned(self):
		"""Dictation capitalises and adds a full stop; both still read as intended."""
		self.assertEqual(ap.classify_card_reply("Go."), ap.APPROVE)
		self.assertEqual(ap.classify_card_reply("No."), ap.REJECT)

	def test_the_approval_and_rejection_sets_are_disjoint(self):
		self.assertEqual(ap.APPROVAL_PHRASES & ap.REJECTION_PHRASES, frozenset())
		for phrase in ap.REJECTION_PHRASES:
			self.assertIsNone(ap.parse_approval(phrase, 3), phrase)
		for phrase in ap.APPROVAL_PHRASES:
			self.assertIsNone(ap.parse_rejection(phrase, 3), phrase)

	def test_the_erp_verbs_never_discard(self):
		for phrase in ("cancel", "cancel it", "reject", "reject it", "decline", "cancel all", "cancel 1"):
			self.assertIsNone(ap.parse_rejection(phrase, 3), phrase)

	def test_a_numbered_discard_is_range_checked_against_the_count(self):
		self.assertEqual(ap.parse_rejection("discard 2", 3), [1])
		self.assertEqual(ap.parse_rejection("drop 3, 1", 3), [0, 2])
		self.assertIsNone(ap.parse_rejection("discard 4", 3))
		self.assertIsNone(ap.parse_rejection("discard 0", 3))
		# Without a count the shape is judged; the caller range-checks it.
		self.assertEqual(ap.classify_card_reply("discard 4"), ap.REJECT)
		self.assertEqual(ap.classify_card_reply("discard 4", 3), ap.NEITHER)

	def test_bare_and_sweep_take_every_displayed_card(self):
		self.assertEqual(ap.parse_rejection("no", 2), [0, 1])
		self.assertEqual(ap.parse_rejection("discard all", 2), [0, 1])
		self.assertTrue(ap.is_reject_sweep("Discard all."))
		self.assertFalse(ap.is_reject_sweep("no"))

	def test_numbered_picks_are_told_apart_from_bare_phrases(self):
		for phrase in ("confirm 2", "yes to 1 and 3", "discard 2", "skip 1"):
			self.assertTrue(ap.is_numbered(phrase), phrase)
		for phrase in ("yes", "confirm all", "no", "discard all", "discard"):
			self.assertFalse(ap.is_numbered(phrase), phrase)
