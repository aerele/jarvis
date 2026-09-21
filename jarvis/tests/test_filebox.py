"""Intent guard for the File Box directed prompt (jarvis.chat.filebox.INBOUND_PROMPT).

Cheap substring assertions that the prompt still carries the four behaviours the
plan builds to: skill discovery with a guaranteed ocr-data-entry fallback, a wiki
read BEFORE drafting and an update AFTER, and the unattended-safety envelope that
overrides any discovered skill. This does not prove the agent behaves - that is the
live test-tenant drop - but it stops a future prompt edit silently dropping a step.
"""

from frappe.tests.utils import FrappeTestCase

from jarvis.chat.filebox import INBOUND_PROMPT


class TestFileboxPrompt(FrappeTestCase):
	def test_directs_skill_discovery_with_ocr_fallback(self):
		self.assertIn("find_skills", INBOUND_PROMPT)
		# ocr-data-entry is the guaranteed fallback when nothing else clearly matches
		self.assertIn("ocr-data-entry", INBOUND_PROMPT)

	def test_directs_wiki_read_before_and_write_after(self):
		self.assertIn("read_wiki", INBOUND_PROMPT)
		self.assertIn("update_wiki", INBOUND_PROMPT)
		# the write is best-effort so a wiki hiccup can't wedge the unattended run
		self.assertIn("best-effort", INBOUND_PROMPT)

	def test_keeps_unattended_safety_envelope_overriding_skills(self):
		self.assertIn("OVERRIDE", INBOUND_PROMPT)
		self.assertIn("Jarvis Approval Request", INBOUND_PROMPT)
		self.assertIn("Draft", INBOUND_PROMPT)  # drafts only, never submit
