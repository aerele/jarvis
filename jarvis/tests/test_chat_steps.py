"""Tests for jarvis.chat.steps - the live step line and the saved-reply cleanup.

Fixtures are real replies seen on e2e2 (2026-09-25): a DeepSeek turn whose saved
reply glued a wrong first draft and a "Hold on" step onto the real answer, and a
Claude-subscription reply that kept its "I'll count them now." step.
"""

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.steps import MAX_STEP_CHARS, display_line, strip_steps, visible_text

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


class TestStripSteps(FrappeTestCase):
	def test_claude_single_string_reply_loses_its_step(self):
		final = "I'll count them now.\n\nThere are **3 customers** on this site."
		self.assertEqual(
			strip_steps(final, ["I'll count them now."]),
			"There are **3 customers** on this site.",
		)

	def test_deepseek_glued_draft_is_removed(self):
		final = DEEPSEEK_DRAFT + DEEPSEEK_ANSWER
		self.assertEqual(strip_steps(final, [DEEPSEEK_DRAFT]), DEEPSEEK_ANSWER)

	def test_several_steps_in_order(self):
		final = "Checking customers.\n\nNow the invoices.\n\nThe answer is 3."
		self.assertEqual(
			strip_steps(final, ["Checking customers.", "Now the invoices."]),
			"The answer is 3.",
		)

	def test_first_step_missing_keeps_reply_unchanged(self):
		# The Codex path never puts its step updates in the final text.
		self.assertEqual(strip_steps("The answer is 3.", ["Checking."]), "The answer is 3.")

	def test_stops_at_the_first_step_that_does_not_match(self):
		final = "Checking customers.\n\nThe answer is 3."
		self.assertEqual(
			strip_steps(final, ["Checking customers.", "Now the invoices."]),
			"The answer is 3.",
		)

	def test_never_leaves_an_empty_reply(self):
		# Text written right before a last tool call with nothing after it is the
		# answer itself, so it must survive.
		final = "Here is the summary you asked for."
		self.assertEqual(strip_steps(final, [final]), final)

	def test_no_steps_or_no_text_is_a_no_op(self):
		self.assertEqual(strip_steps("Answer.", []), "Answer.")
		self.assertEqual(strip_steps("", ["Checking."]), "")
		self.assertIsNone(strip_steps(None, ["Checking."]))


class TestVisibleText(FrappeTestCase):
	def test_growing_text_hides_its_steps(self):
		self.assertEqual(visible_text("Checking.\n\nThe answer", ["Checking."]), "The answer")

	def test_all_steps_so_far_gives_empty(self):
		self.assertEqual(visible_text("Checking.  ", ["Checking."]), "")

	def test_fresh_segment_is_unchanged(self):
		self.assertEqual(visible_text("The answer is 3.", ["Checking."]), "The answer is 3.")
		self.assertEqual(visible_text("", ["Checking."]), "")
