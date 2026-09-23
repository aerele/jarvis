"""Intent guard for the File Box directed prompt (jarvis.chat.filebox.INBOUND_PROMPT).

Cheap substring assertions that the prompt still carries the four behaviours the
plan builds to: skill discovery with a guaranteed ocr-data-entry fallback, a wiki
read BEFORE drafting and an update AFTER, and the unattended-safety envelope that
overrides any discovered skill. This does not prove the agent behaves - that is the
live test-tenant drop - but it stops a future prompt edit silently dropping a step.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat.filebox import (
	INBOUND_PROMPT,
	OCR_DATA_ENTRY,
	_validated_pinned_skill,
	build_inbound_prompt,
	drop_file,
	list_inbound_page,
)
from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError


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

	def test_build_inbound_prompt_none_is_verbatim(self):
		# The auto path MUST be byte-identical to the constant so a pin can never
		# drift the default flow (and any #1337 edit flows through unchanged).
		self.assertEqual(build_inbound_prompt(None), INBOUND_PROMPT)
		self.assertEqual(build_inbound_prompt(""), INBOUND_PROMPT)

	def test_build_inbound_prompt_tagged_is_additive(self):
		p = build_inbound_prompt("invoicing")
		self.assertIn("invoicing", p)  # the tagged skill is named
		self.assertIn("IN ADDITION", p)  # applied ALONGSIDE the base, not replacing it
		self.assertNotIn("do NOT run find_skills", p)  # the normal discovery still runs
		# the whole base flow + safety envelope (the constant) is preserved verbatim
		self.assertTrue(p.endswith(INBOUND_PROMPT))


class TestFileboxPinValidation(FrappeTestCase):
	"""_validated_pinned_skill resolves an untrusted client slug to the CANONICAL
	skill_name via get_skill (the same resolver the agent uses), with a UNIFORM
	fallback to None (no enumeration oracle)."""

	def test_ocr_data_entry_accepted_by_literal_no_get_skill(self):
		with patch("jarvis.tools.get_skill.get_skill") as gs:
			self.assertEqual(_validated_pinned_skill(OCR_DATA_ENTRY), OCR_DATA_ENTRY)
			gs.assert_not_called()  # persona skill has no Custom Skill row

	def test_blank_is_no_pin(self):
		self.assertIsNone(_validated_pinned_skill(None))
		self.assertIsNone(_validated_pinned_skill("   "))

	def test_valid_custom_skill_returns_canonical_skill_name(self):
		with patch("jarvis.tools.get_skill.get_skill", return_value={"skill_name": "invoicing"}) as gs:
			# a client could send any casing; the canonical value comes from get_skill
			self.assertEqual(_validated_pinned_skill("Invoicing"), "invoicing")
			gs.assert_called_once_with("Invoicing")

	def test_unknown_and_inaccessible_fall_back_identically(self):
		# uniform fallback: an unknown slug and another user's private (inaccessible)
		# skill must behave the SAME (both -> None, no raise) - no oracle.
		with patch("jarvis.tools.get_skill.get_skill", side_effect=InvalidArgumentError("unknown")):
			self.assertIsNone(_validated_pinned_skill("no-such-skill"))
		with patch("jarvis.tools.get_skill.get_skill", side_effect=PermissionDeniedError("no access")):
			self.assertIsNone(_validated_pinned_skill("someones-private-skill"))


class TestFileboxDropPin(FrappeTestCase):
	def _a_file(self):
		return frappe.get_doc(
			{"doctype": "File", "file_name": "invoice.txt", "content": "x", "is_private": 1}
		).insert(ignore_permissions=True)

	def _drop(self, skill=None):
		f = self._a_file()
		with patch("jarvis.chat.api.send_message", return_value={"ok": True, "run_id": "r1"}) as sm:
			res = drop_file(f.file_url, "invoice.txt", skill=skill)
		return res["conversation_id"], sm

	def _pinned(self, conv):
		return frappe.db.get_value("Jarvis Conversation", conv, "filebox_pinned_skill")

	def test_valid_pin_stored_and_threaded(self):
		with patch("jarvis.tools.get_skill.get_skill", return_value={"skill_name": "invoicing"}):
			conv, sm = self._drop(skill="invoicing")
		self.assertEqual(self._pinned(conv), "invoicing")
		self.assertEqual(sm.call_args.kwargs["message"], build_inbound_prompt("invoicing"))

	def test_no_skill_is_auto_no_pin(self):
		conv, sm = self._drop(skill=None)
		self.assertIn(self._pinned(conv), (None, ""))  # no pin
		self.assertEqual(sm.call_args.kwargs["message"], INBOUND_PROMPT)  # verbatim auto flow

	def test_forged_or_inaccessible_skill_falls_back_drop_survives(self):
		with patch("jarvis.tools.get_skill.get_skill", side_effect=InvalidArgumentError("unknown")):
			conv, sm = self._drop(skill="forged\nskill")
		self.assertIn(self._pinned(conv), (None, ""))  # NOT pinned
		self.assertEqual(sm.call_args.kwargs["message"], INBOUND_PROMPT)  # fell back to auto

	def test_ocr_data_entry_pin(self):
		conv, sm = self._drop(skill=OCR_DATA_ENTRY)
		self.assertEqual(self._pinned(conv), OCR_DATA_ENTRY)
		self.assertEqual(sm.call_args.kwargs["message"], build_inbound_prompt(OCR_DATA_ENTRY))

	def test_list_inbound_page_returns_pinned_skill_to_owner(self):
		with patch("jarvis.tools.get_skill.get_skill", return_value={"skill_name": "invoicing"}):
			conv, _ = self._drop(skill="invoicing")
		page = list_inbound_page()
		row = next((r for r in page["rows"] if r["name"] == conv), None)
		self.assertIsNotNone(row, "the dropped conversation should be in the owner's inbound list")
		self.assertEqual(row["pinned_skill"], "invoicing")
