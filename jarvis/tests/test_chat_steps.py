"""Tests for jarvis.chat.steps - the live step line and the saved-reply cleanup.

Fixtures are real replies seen on e2e2 (2026-09-25): a DeepSeek turn whose saved
reply glued a wrong first draft and a "Hold on" step onto the real answer, and a
Claude-subscription reply that kept its "I'll count them now." step.
"""

import json
import os
import re
from unittest.mock import patch as mock_patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import steps as chat_steps
from jarvis.chat.steps import (
	MAX_PREAMBLE_STEP_CHARS,
	MAX_STEP_CHARS,
	MAX_STEP_SENTENCES,
	MAX_STEP_TEXT_CHARS,
	collapse_steps,
	display_line,
	is_preamble_step,
	is_step,
	join_segments,
	remove_steps,
	strip_preambles,
	strip_steps,
)

# Narration the Claude subscription route wrote right before a tool call, seen on
# e2e2 (2026-10-21): two or three sentences, so the one-sentence rule left them
# at the top of the saved answer.
CLAUDE_NARRATION = [
	"Links all resolve. Now the invoice checks: supplier status, item setup, duplicates, and real-record conventions.",
	"Found something important: a submitted invoice for MA Inc. today already matches this exactly. "
	"Let me verify the supplier and run the integrity dry-run.",
	"All three links resolve. One thing stands out before I draft: an invoice for this exact supplier, "
	"date and amount already exists today. Checking it.",
	"Both items and the supplier exist. Checking how your purchase invoices for this supplier are usually filled.",
	"All three records exist. Pulling the org's real purchase-invoice conventions and checking for a duplicate.",
]

DEEPSEEK_DRAFT = (
	"West View Software Ltd. is your biggest receivable at **2,29,000 INR** outstanding "
	"(from a single submitted invoice).\n\nIts 3 most recent sales invoices:\n\n"
	"| Invoice | Date | Grand total | Outstanding | Status |\n|---|---|---|---|---|\n"
	"| not found | - | - | - | - |\n\n"
	"Hold on, I need to actually pull the invoice list, one moment."
)
DEEPSEEK_ANSWER = (
	"**West View Software Ltd.** has the highest total outstanding at **2,29,000 INR** "
	"(one submitted invoice, currently Overdue).\n\n| Invoice | Status |\n|---|---|\n"
	"| ACC-SINV-2026-00004 | Paid |"
)


class TestDisplayLine(FrappeTestCase):
	def test_plain_sentence_is_kept(self):
		text = "I'll pull the submitted invoices with an outstanding balance."
		self.assertEqual(display_line(text), text)

	def test_takes_the_last_prose_line_not_a_table_row(self):
		self.assertEqual(
			display_line(DEEPSEEK_DRAFT),
			"Hold on, I need to actually pull the invoice list, one moment.",
		)

	def test_strips_markdown_marks_and_bullets(self):
		self.assertEqual(
			display_line("- **Checking** the `Sales Invoice` list"),
			"Checking the Sales Invoice list",
		)

	def test_long_line_is_truncated(self):
		line = display_line("word " * 100)
		self.assertLessEqual(len(line), MAX_STEP_CHARS)
		self.assertTrue(line.endswith("…"))

	def test_table_only_or_empty_gives_no_line(self):
		self.assertEqual(display_line("| a | b |\n|---|---|"), "")
		self.assertEqual(display_line("   \n "), "")
		self.assertEqual(display_line(""), "")


class TestIsStep(FrappeTestCase):
	def test_one_short_sentence_is_a_step(self):
		self.assertTrue(is_step("I'll count them now."))
		self.assertTrue(is_step("Checking invoices over 1.5 lakh."))
		self.assertTrue(is_step("  Let me check the overdue invoices  "))

	def test_anything_that_may_be_answer_is_not(self):
		self.assertFalse(
			is_step(
				"Invoice #4521 is due Oct 3. It is unpaid. The customer was reminded. I'll open a card for it."
			)
		)
		self.assertFalse(is_step("| a | b |"))
		self.assertFalse(is_step("- first item"))
		self.assertFalse(is_step("Line one\nLine two"))
		self.assertFalse(is_step("word " * 80))
		self.assertFalse(is_step(DEEPSEEK_DRAFT))
		self.assertFalse(is_step(""))

	def test_claude_narration_of_up_to_three_sentences_is_a_step(self):
		for text in CLAUDE_NARRATION:
			with self.subTest(text=text):
				self.assertTrue(is_step(text))

	def test_the_sentence_cap_is_three(self):
		self.assertEqual(MAX_STEP_SENTENCES, 3)
		self.assertTrue(is_step("One. Two. Three."))
		self.assertFalse(is_step("One. Two. Three. Four."))

	def test_a_newline_or_a_structural_start_is_not_a_step(self):
		self.assertFalse(is_step("Checking the invoices.\nThen the customers."))
		for text in (
			"| a | b |",
			"- Checking this. Then that.",
			"1. Checking. Then that.",
			"## Plan. Then run.",
		):
			with self.subTest(text=text):
				self.assertFalse(is_step(text))

	def test_the_length_limit_still_applies(self):
		self.assertFalse(is_step("a" * (MAX_STEP_TEXT_CHARS + 1)))
		self.assertTrue(is_step("a" * MAX_STEP_TEXT_CHARS))

	def test_real_pre_tool_segments(self):
		with open(os.path.join(os.path.dirname(__file__), "fixtures", "reply_cleanup_real_chats.json")) as f:
			fixture = json.load(f)
		for text in fixture["steps_accepted"]:
			with self.subTest(text=text[:60]):
				self.assertTrue(is_step(text))
		# A long, multi-line split answer written before a lookup stays in the reply.
		for text in fixture["steps_rejected"]:
			with self.subTest(text=text[:60]):
				self.assertFalse(is_step(text))


class TestRemoveSteps(FrappeTestCase):
	def test_growing_text_loses_its_steps(self):
		self.assertEqual(remove_steps("Checking.\n\nThe answer", ["Checking."]), "The answer")

	def test_all_steps_so_far_gives_empty(self):
		self.assertEqual(remove_steps("Checking.  ", ["Checking."]), "")

	def test_a_step_after_other_text_is_cut_from_the_middle(self):
		self.assertEqual(
			remove_steps("Here is a table.\n\n| a |\n\nNow checking B.\n\nDone.", ["Now checking B."]),
			"Here is a table.\n\n| a |\n\nDone.",
		)

	def test_text_without_the_steps_is_unchanged(self):
		self.assertEqual(remove_steps("  The answer is 3.", ["Checking."]), "  The answer is 3.")
		self.assertEqual(remove_steps("", ["Checking."]), "")


class TestStripSteps(FrappeTestCase):
	def test_claude_single_string_reply_loses_its_step(self):
		final = "I'll count them now.\n\nThere are **3 customers** on this site."
		self.assertEqual(
			strip_steps(final, ["I'll count them now."]),
			"There are **3 customers** on this site.",
		)

	def test_api_key_glued_final_loses_its_step_segment(self):
		# The runtime joins per-call segments with no separator.
		final = "Checking the invoices." + DEEPSEEK_ANSWER
		self.assertEqual(strip_steps(final, ["Checking the invoices."]), DEEPSEEK_ANSWER)

	def test_several_steps_in_order(self):
		final = "Checking customers.\n\nNow the invoices.\n\nThe answer is 3 customers and 5 invoices."
		self.assertEqual(
			strip_steps(final, ["Checking customers.", "Now the invoices."]),
			"The answer is 3 customers and 5 invoices.",
		)

	def test_steps_not_in_the_reply_keep_it_unchanged(self):
		# The Codex path never puts its step updates in the final text.
		self.assertEqual(strip_steps("The answer is 3.", ["Checking."]), "The answer is 3.")

	def test_a_short_answer_before_a_card_is_kept(self):
		# Review finding: text before the last lookup can be the answer itself,
		# with only a short remark after the card tool call. Keep it all.
		final = "You have 3 active customers.\n\nHere they are."
		self.assertEqual(strip_steps(final, ["You have 3 active customers."]), final)

	def test_a_short_final_whole_is_kept_when_the_step_is_longer(self):
		# The guard that matters most now that a step may be three sentences.
		step = CLAUDE_NARRATION[0]
		final = f"{step}\n\nAll good."
		self.assertEqual(strip_steps(final, [step]), final)

	def test_a_three_sentence_step_is_cut_from_a_longer_answer(self):
		step = CLAUDE_NARRATION[3]
		answer = "The supplier and both items exist. " * 8
		self.assertEqual(strip_steps(f"{step}\n\n{answer.strip()}", [step]), answer.strip())

	def test_never_leaves_an_empty_reply(self):
		final = "Here is the summary you asked for."
		self.assertEqual(strip_steps(final, [final]), final)

	def test_no_steps_or_no_text_is_a_no_op(self):
		self.assertEqual(strip_steps("Answer.", []), "Answer.")
		self.assertEqual(strip_steps("", ["Checking."]), "")
		self.assertIsNone(strip_steps(None, ["Checking."]))


class TestStripPreambles(FrappeTestCase):
	def test_strips_a_flagged_preamble_even_before_a_shorter_answer(self):
		final = "Checking your customer count.\n\nYou have **3 customers**."
		self.assertEqual(
			strip_preambles(final, ["Checking your customer count."]),
			"You have **3 customers**.",
		)

	def test_strips_a_multi_sentence_preamble(self):
		preamble = "I found the report. I'm checking its filters."
		self.assertEqual(strip_preambles(f"{preamble}\n\nThe answer is 3.", [preamble]), "The answer is 3.")

	def test_keeps_the_reply_whole_when_nothing_would_remain(self):
		final = "I found the report. I'm checking its filters."
		self.assertEqual(strip_preambles(final, [final]), final)

	def test_no_preambles_or_no_text_is_a_no_op(self):
		self.assertEqual(strip_preambles("Answer.", []), "Answer.")
		self.assertEqual(strip_preambles("", ["Checking."]), "")
		self.assertIsNone(strip_preambles(None, ["Checking."]))


class TestIsPreambleStep(FrappeTestCase):
	"""R4: the looser rule a runtime-flagged preamble is checked against when
	its text is also streaming in the reply (relay_mux._route_preamble_step)."""

	def test_one_short_sentence_is_accepted(self):
		self.assertTrue(is_preamble_step("I'll check the overdue invoices."))

	def test_more_than_three_sentences_up_to_the_limit_are_accepted(self):
		# Looser than is_step: the runtime already flagged this as narration.
		text = (
			"I'll check the overdue invoices. Then I'll total them by customer. "
			"After that I'll rank the customers. Finally I'll draft the reminders."
		)
		self.assertFalse(is_step(text))
		self.assertLessEqual(len(text), MAX_PREAMBLE_STEP_CHARS)
		self.assertTrue(is_preamble_step(text))

	def test_structural_text_is_rejected(self):
		self.assertFalse(is_preamble_step("| a | b |"))
		self.assertFalse(is_preamble_step("- first item"))
		self.assertFalse(is_preamble_step("# Heading"))
		self.assertFalse(is_preamble_step("> Quote"))
		self.assertFalse(is_preamble_step("```code"))
		self.assertFalse(is_preamble_step("Line one\nLine two"))

	def test_over_the_limit_is_rejected(self):
		self.assertFalse(is_preamble_step("word " * 100))

	def test_empty_is_rejected(self):
		self.assertFalse(is_preamble_step(""))
		self.assertFalse(is_preamble_step("   "))

	def test_zero_constant_rejects_everything(self):
		# R4 killswitch: MAX_PREAMBLE_STEP_CHARS=0 must reject even a short
		# one-sentence preamble that would otherwise pass, so relay_mux falls
		# back to always offering the step (today's behaviour before this
		# feature - see MAX_PREAMBLE_STEP_CHARS's docstring comment).
		with mock_patch.object(chat_steps, "MAX_PREAMBLE_STEP_CHARS", 0):
			self.assertFalse(is_preamble_step("I'll check the overdue invoices."))


class TestJoinSegments(FrappeTestCase):
	"""R6: the saved-reply glue for an API-key model's separator-free segment
	boundaries ("...addresses.I couldn't find..." from the live capture)."""

	def test_inserts_a_break_at_a_verbatim_tail(self):
		text = "First segment done.Second segment starts."
		self.assertEqual(
			join_segments(text, ["First segment done."]),
			"First segment done.\n\nSecond segment starts.",
		)

	def test_skips_a_tail_already_followed_by_whitespace(self):
		text = "First segment.\n\nSecond segment."
		self.assertEqual(join_segments(text, ["First segment."]), text)

	def test_skips_a_tail_not_found(self):
		text = "Answer only."
		self.assertEqual(join_segments(text, ["Checking."]), text)

	def test_real_fixture_glues_the_captured_boundary(self):
		# Live capture (2026-09-28): the runtime glued two per-call segments
		# with no separator at all.
		text = "...usable email addresses.I couldn't find a usable email address for the customer."
		tail = "...usable email addresses."
		self.assertEqual(
			join_segments(text, [tail]),
			"...usable email addresses.\n\nI couldn't find a usable email address for the customer.",
		)

	def test_several_tails_join_in_order(self):
		text = "One.Two.Three."
		self.assertEqual(join_segments(text, ["One.", "Two."]), "One.\n\nTwo.\n\nThree.")

	def test_no_tails_or_no_text_is_a_no_op(self):
		self.assertEqual(join_segments("Answer.", []), "Answer.")
		self.assertEqual(join_segments("", ["x"]), "")

	# -- C2 (code review): boundary safety - never split a token/URL/word ----

	def test_mid_url_boundary_is_never_broken(self):
		# Code review C1 proof (prove_join_segments_midword.py): a tail whose
		# capture point falls mid-word/mid-URL (lowercase both sides) must be
		# left alone - neither side looks like a sentence end/start.
		seg1_end = "See https://example.com/very-long-path-that-keeps-goin"
		tail = seg1_end[-60:]
		final_glued = seg1_end + "g-right-here for details."
		self.assertEqual(join_segments(final_glued, [tail]), final_glued)

	def test_curly_apostrophe_elsewhere_does_not_block_a_real_boundary(self):
		# The reviewer's fixture, with the runtime's actual curly apostrophe
		# (U+2019) in "couldn't" - irrelevant to the boundary check, which only
		# looks at the characters immediately either side of the cut.
		text = "...usable email addresses.I couldn’t find a usable email address for the customer."
		tail = "...usable email addresses."
		self.assertEqual(
			join_segments(text, [tail]),
			"...usable email addresses.\n\nI couldn’t find a usable email address for the customer.",
		)

	def test_boundary_inside_a_code_fence_is_skipped(self):
		# Before/after alone would pass ("." then "N"), but the cut sits inside
		# an OPEN fence (one "```" seen so far, odd) - never break code.
		text = "```\nEnd of code.Next line here\n```"
		tail = "End of code."
		self.assertEqual(join_segments(text, [tail]), text)

	def test_lowercase_continuation_is_not_a_boundary(self):
		text = "The report was fetched.and then processed."
		tail = "The report was fetched."
		self.assertEqual(join_segments(text, [tail]), text)


class TestCollapseSteps(FrappeTestCase):
	PARTIAL = "I checked the ledger"  # a streaming prefix of FULL
	FULL = "I checked the ledger and found stale entries."

	def test_a_growing_resend_replaces_the_prefix_it_extends(self):
		self.assertEqual(collapse_steps([self.PARTIAL, self.FULL]), [self.FULL])

	def test_the_longer_step_keeps_the_shorter_ones_place(self):
		other = "I'm pulling the invoices."
		self.assertEqual(collapse_steps([self.PARTIAL, other, self.FULL]), [self.FULL, other])

	def test_repeats_and_blanks_are_dropped(self):
		self.assertEqual(collapse_steps([self.FULL, "", self.FULL, None]), [self.FULL])
		self.assertEqual(collapse_steps(None), [])

	def test_collapsed_steps_strip_without_a_dangling_tail(self):
		# The review's case: stripping [partial, full] cuts the partial first,
		# the full step no longer matches and " and found stale entries." stays.
		text = f"{self.FULL}\n\nThe ledger has 4 stale entries dated before April, all from the old import."
		self.assertIn("and found stale entries", remove_steps(text, [self.PARTIAL, self.FULL]))
		self.assertEqual(
			remove_steps(text, collapse_steps([self.PARTIAL, self.FULL])),
			"The ledger has 4 stale entries dated before April, all from the old import.",
		)


class TestPreambleLimitLockstep(FrappeTestCase):
	def test_frontend_candidate_limit_matches_the_backend(self):
		# frontend/src/lib/liveTurn.js MAX_CANDIDATE_CHARS must equal
		# MAX_PREAMBLE_STEP_CHARS, or the SPA types into the step box text the
		# relay leaves in the reply (or the reverse).
		path = os.path.join(frappe.get_app_path("jarvis"), "..", "frontend", "src", "lib", "liveTurn.js")
		with open(path, encoding="utf-8") as fh:
			match = re.search(r"export const MAX_CANDIDATE_CHARS = (\d+);", fh.read())
		self.assertIsNotNone(match)
		self.assertEqual(int(match.group(1)), MAX_PREAMBLE_STEP_CHARS)
