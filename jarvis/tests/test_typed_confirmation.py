"""Typed approval of a parked confirmation card.

A card gives two equal ways to say yes: the Confirm button, and saying so in the
composer. The second one is a convenience over the first and must never be wider
than it, so most of what follows asserts what does NOT confirm.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import approval_phrases
from jarvis.chat.api import _typed_confirmation


class TestApprovalPhrases(FrappeTestCase):
	"""The matcher alone: whole-message equality against a short fixed list."""

	def test_accepts_the_plain_go_aheads(self):
		for phrase in ("confirm", "yes", "go ahead", "proceed", "do it", "ok", "approve"):
			self.assertTrue(approval_phrases.is_approval(phrase), phrase)

	def test_ignores_case_padding_and_trailing_punctuation(self):
		for phrase in ("  Go Ahead  ", "YES!", "Confirm.", "ok,", "Yes  Please"):
			self.assertTrue(approval_phrases.is_approval(phrase), phrase)

	def test_rejects_an_approval_carrying_a_qualifier(self):
		"""The whole point of whole-message matching.

		Every one of these contains an approval word and none of them approves
		what is on the card. Substring matching would run the wrong write.
		"""
		for phrase in (
			"yes but change the quantity to 5",
			"yes, use Acme instead",
			"go ahead and also create the item",
			"confirm the other one",
			"ok so what does this do",
			"do it tomorrow",
			"no, go ahead with the first one",
		):
			self.assertFalse(approval_phrases.is_approval(phrase), phrase)

	def test_rejects_a_question_about_the_card(self):
		"""A question mark survives normalisation, so it cannot match."""
		for phrase in ("confirm?", "go ahead?", "ok?"):
			self.assertFalse(approval_phrases.is_approval(phrase), phrase)

	def test_rejects_declines_and_unrelated_text(self):
		for phrase in ("no", "cancel", "stop", "discard", "wait", "hello", ""):
			self.assertFalse(approval_phrases.is_approval(phrase), phrase)

	def test_rejects_the_strings_the_confirm_CARD_button_already_sends(self):
		"""A different card type already answers itself by sending a message.

		The model-authored ```confirm block's buttons call send() with these exact
		strings (ChatView.vue). They belong to that card, not to a parked gated
		write, so they must keep reaching the model. The internal comma is what
		keeps them out, which is easy to break by "tidying" the phrase list.
		"""
		for phrase in ("Yes, go ahead.", "No, cancel that.", "Yes — Confirm and save"):
			self.assertFalse(approval_phrases.is_approval(phrase), phrase)

	def test_rejects_anything_long_enough_to_be_a_message(self):
		self.assertFalse(approval_phrases.is_approval("yes " * 20))

	def test_tolerates_nullish_input(self):
		self.assertFalse(approval_phrases.is_approval(None))


class TestParseApproval(FrappeTestCase):
	"""Which of N cards a message approves. None means "not an approval at all"."""

	def test_a_plain_go_ahead_takes_the_only_card(self):
		self.assertEqual(approval_phrases.parse_approval("go ahead", 1), [0])

	def test_a_plain_go_ahead_takes_EVERY_card_when_several_are_parked(self):
		self.assertEqual(approval_phrases.parse_approval("yes", 3), [0, 1, 2])

	def test_explicit_bulk_phrases(self):
		for phrase in ("confirm all", "yes to all", "approve all of them", "all", "confirm both"):
			self.assertEqual(approval_phrases.parse_approval(phrase, 2), [0, 1], phrase)

	def test_selecting_by_number(self):
		self.assertEqual(approval_phrases.parse_approval("confirm 1", 3), [0])
		self.assertEqual(approval_phrases.parse_approval("confirm 1 and 3", 3), [0, 2])
		self.assertEqual(approval_phrases.parse_approval("approve 2, 3", 3), [1, 2])
		self.assertEqual(approval_phrases.parse_approval("yes to 2", 3), [1])
		self.assertEqual(approval_phrases.parse_approval("do 1 & 2", 3), [0, 1])

	def test_a_selection_is_deduped_and_sorted(self):
		self.assertEqual(approval_phrases.parse_approval("confirm 3, 1, 3", 3), [0, 2])

	def test_a_number_out_of_range_approves_NOTHING(self):
		"""Not "everything", and not "the ones that do exist".

		"Confirm 4" against three cards means the user is looking at something we
		are not. Running any write on that basis is the wrong recovery.
		"""
		self.assertIsNone(approval_phrases.parse_approval("confirm 4", 3))
		self.assertIsNone(approval_phrases.parse_approval("confirm 0", 3))
		self.assertIsNone(approval_phrases.parse_approval("confirm 1 and 9", 3))

	def test_a_qualifier_still_beats_every_form(self):
		for phrase in (
			"confirm 1 but change the date",
			"yes to all of the suppliers except the second",
			"confirm all the details look wrong",
			"approve 1 tomorrow",
		):
			self.assertIsNone(approval_phrases.parse_approval(phrase, 3), phrase)

	def test_a_bare_number_is_not_an_approval(self):
		"""Without a verb, "1" is far more likely to be an answer to a question."""
		self.assertIsNone(approval_phrases.parse_approval("1", 3))
		self.assertIsNone(approval_phrases.parse_approval("1 and 2", 3))

	def test_declines_are_never_approvals_in_any_form(self):
		for phrase in ("no", "cancel all", "no to all", "discard 1"):
			self.assertIsNone(approval_phrases.parse_approval(phrase, 3), phrase)

	def test_a_question_is_not_a_bulk_approval_either(self):
		"""The question-mark rule has to survive into the bulk forms.

		"All?" is a user asking what the cards are, not approving three writes.
		"""
		for phrase in ("all?", "confirm all?", "confirm 1?"):
			self.assertIsNone(approval_phrases.parse_approval(phrase, 3), phrase)

	def test_no_cards_means_no_approval(self):
		self.assertIsNone(approval_phrases.parse_approval("confirm all", 0))


class TestTypedConfirmation(FrappeTestCase):
	"""The intercept: which messages reach the confirmation, and which fall through."""

	def setUp(self):
		self.user = frappe.session.user
		self.conv = "conv-typed-confirm"

	def _card(self, token="tok-1"):
		return {"token": token, "tool": "create_doc", "summary": "create Supplier"}

	def test_a_lone_card_and_a_plain_go_ahead_runs_the_confirmation(self):
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=[self._card()]),
			patch("jarvis.chat.actions_api._confirm_core", return_value={"ok": True}) as core,
		):
			out = _typed_confirmation(self.user, self.conv, "go ahead")
		core.assert_called_once_with("tok-1", self.conv, batch=False)
		self.assertTrue(out["confirmed"])
		self.assertTrue(out["ok"])
		self.assertEqual(out["tokens"], ["tok-1"])
		self.assertEqual(out["conversation_id"], self.conv)

	def test_a_plain_go_ahead_with_several_cards_confirms_them_all(self):
		"""A user who lines up three writes and says go ahead means three."""
		cards = [self._card("a"), self._card("b"), self._card("c")]
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=cards),
			patch("jarvis.chat.actions_api._confirm_core", return_value={"ok": True}) as core,
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			out = _typed_confirmation(self.user, self.conv, "go ahead")
		self.assertEqual(core.call_count, 3)
		self.assertEqual(out["tokens"], ["a", "b", "c"])
		self.assertTrue(out["ok"])

	def test_a_batch_produces_ONE_continuation_turn_not_one_per_card(self):
		"""Ten approvals must not become ten turns racing through admission."""
		cards = [self._card(t) for t in ("a", "b", "c")]
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=cards),
			patch(
				"jarvis.chat.actions_api._confirm_core",
				return_value={"ok": True, "receipt_text": "created X."},
			) as core,
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}) as cont,
		):
			_typed_confirmation(self.user, self.conv, "confirm all")
		# Every card ran in batch mode, so none queued its own follow-up...
		for call in core.call_args_list:
			self.assertTrue(call.kwargs["batch"])
		# ...and exactly one carries all three receipts.
		cont.assert_called_once()
		self.assertEqual(cont.call_args.args[1], "created X. created X. created X.")

	def test_selecting_by_number_confirms_only_those_cards(self):
		cards = [self._card("a"), self._card("b"), self._card("c")]
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=cards),
			patch("jarvis.chat.actions_api._confirm_core", return_value={"ok": True}) as core,
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			out = _typed_confirmation(self.user, self.conv, "confirm 1 and 3")
		self.assertEqual([c.args[0] for c in core.call_args_list], ["a", "c"])
		self.assertEqual(out["tokens"], ["a", "c"])
		self.assertEqual([r["position"] for r in out["results"]], [1, 3])

	def test_a_number_that_does_not_exist_confirms_NOTHING(self):
		"""The wrong recovery would be to run all three anyway."""
		cards = [self._card("a"), self._card("b"), self._card("c")]
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=cards),
			patch("jarvis.chat.actions_api._confirm_core") as core,
		):
			self.assertIsNone(_typed_confirmation(self.user, self.conv, "confirm 4"))
		core.assert_not_called()

	def test_a_partial_batch_failure_is_reported_per_card(self):
		"""With three writes, "it worked" is not an answer when one did not."""
		cards = [self._card("a"), self._card("b")]
		envelopes = [
			{"ok": True, "receipt_text": "created X."},
			{"ok": False, "error": {"type": "InvalidConfirmation", "message": "gone"}},
		]
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=cards),
			patch("jarvis.chat.actions_api._confirm_core", side_effect=envelopes),
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			out = _typed_confirmation(self.user, self.conv, "confirm all")
		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["type"], "PartialConfirmation")
		self.assertEqual([r["ok"] for r in out["results"]], [True, False])

	def test_cards_are_ordered_so_the_numbers_mean_what_the_screen_shows(self):
		"""The store is a Redis SET with no order at all.

		Without the sort, "confirm 1" would pick whichever token the set happened
		to yield first, which is the wrong write roughly half the time.
		"""
		cards = [
			dict(self._card("late"), expires_at=200),
			dict(self._card("early"), expires_at=100),
		]
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=cards),
			patch("jarvis.chat.actions_api._confirm_core", return_value={"ok": True}) as core,
			patch("jarvis.chat.actions_api.enqueue_continuation", return_value={}),
		):
			_typed_confirmation(self.user, self.conv, "confirm 1")
		core.assert_called_once()
		self.assertEqual(core.call_args.args[0], "early")

	def test_no_parked_card_falls_through(self):
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=[]),
			patch("jarvis.chat.actions_api._confirm_core") as core,
		):
			self.assertIsNone(_typed_confirmation(self.user, self.conv, "confirm"))
		core.assert_not_called()

	def test_a_qualified_yes_reaches_the_model_even_with_a_card_open(self):
		"""The message that must NOT confirm, with a card sitting right there."""
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=[self._card()]),
			patch("jarvis.chat.actions_api._confirm_core") as core,
		):
			self.assertIsNone(_typed_confirmation(self.user, self.conv, "yes but change the quantity to 5"))
		# Reading the parked list is harmless; running a write is not.
		core.assert_not_called()

	def test_a_storage_outage_falls_through_instead_of_confirming(self):
		"""Unknown state is not approval.

		A "go ahead" that reaches the model is recoverable. A write nobody
		authorised is not, so an unreadable store must never resolve to a confirm.
		"""
		from jarvis.chat import pending_confirm

		with (
			patch(
				"jarvis.chat.pending_confirm.list_items_for_owner",
				side_effect=pending_confirm.PendingConfirmStorageError("redis down"),
			),
			patch("jarvis.chat.actions_api._confirm_core") as core,
		):
			self.assertIsNone(_typed_confirmation(self.user, self.conv, "yes"))
		core.assert_not_called()

	def test_a_failed_confirmation_still_reports_as_handled(self):
		"""The client has to know no run is coming, whatever the outcome.

		`confirmed` means "this was taken as an approval, not a turn". The
		envelope's own `ok` carries success or failure.
		"""
		envelope = {"ok": False, "error": {"type": "InvalidConfirmation", "message": "gone"}}
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=[self._card()]),
			patch("jarvis.chat.actions_api._confirm_core", return_value=envelope),
		):
			out = _typed_confirmation(self.user, self.conv, "yes")
		self.assertTrue(out["confirmed"])
		self.assertFalse(out["ok"])

	def test_the_queued_continuation_details_survive_to_the_client(self):
		"""So the approved action shows the queued chip instead of going silent."""
		envelope = {"ok": True, "queued": True, "queued_position": 2, "run_id": "r1"}
		with (
			patch("jarvis.chat.pending_confirm.list_items_for_owner", return_value=[self._card()]),
			patch("jarvis.chat.actions_api._confirm_core", return_value=envelope),
		):
			out = _typed_confirmation(self.user, self.conv, "confirm")
		self.assertTrue(out["queued"])
		self.assertEqual(out["queued_position"], 2)
		self.assertEqual(out["run_id"], "r1")


class TestConfirmCoreIsShared(FrappeTestCase):
	"""Both approval paths run the same implementation, so neither can drift."""

	def test_the_button_endpoint_delegates_to_the_shared_core(self):
		from jarvis.chat import actions_api

		with patch.object(actions_api, "_confirm_core", return_value={"ok": True}) as core:
			actions_api.confirm_tool("tok-9", "conv-9")
		core.assert_called_once_with("tok-9", "conv-9")
