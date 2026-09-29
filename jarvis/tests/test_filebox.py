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

from jarvis.chat import filebox_skills
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
	def test_directs_skill_choice_from_the_list_with_ocr_fallback(self):
		self.assertIn("File Box skill list", INBOUND_PROMPT)
		self.assertIn("get_skill", INBOUND_PROMPT)
		self.assertNotIn("cat its SKILL.md", INBOUND_PROMPT)
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
	"""_validated_pinned_skill resolves an untrusted client slug for the dropper
	(resolve_skill) and needs a skill eligible as a pin (use_in_file_box, not learned),
	with a UNIFORM None for unknown / inaccessible / opted-out / learned skills (no
	enumeration oracle)."""

	ROW = frappe._dict(
		name="sk-1",
		skill_name="invoicing",
		file_box_creates="",
		enabled=1,
		use_in_file_box=1,
		managed_by_learning=0,
	)

	def test_ocr_data_entry_accepted_by_literal_no_resolve(self):
		with patch("jarvis.tools.get_skill.resolve_skill") as rs:
			self.assertEqual(_validated_pinned_skill(OCR_DATA_ENTRY)["slug"], OCR_DATA_ENTRY)
			rs.assert_not_called()  # persona skill has no Custom Skill row

	def test_blank_is_no_pin(self):
		self.assertIsNone(_validated_pinned_skill(None))
		self.assertIsNone(_validated_pinned_skill("   "))

	def test_valid_custom_skill_returns_the_canonical_pin(self):
		with patch("jarvis.tools.get_skill.resolve_skill", return_value=self.ROW) as rs:
			# a client could send any casing; the canonical value comes from the resolver
			pin = _validated_pinned_skill("Invoicing")
		self.assertEqual((pin["slug"], pin["docname"], pin["pinned"]), ("invoicing", "sk-1", True))
		self.assertEqual(rs.call_args.args[0], "Invoicing")

	def test_unknown_inaccessible_and_opted_out_are_all_none(self):
		# uniform: an unknown slug, another user's private skill and an opted-out one
		# behave the SAME (None, no raise) - no oracle.
		for effect in (InvalidArgumentError("unknown"), PermissionDeniedError("no access")):
			with patch("jarvis.tools.get_skill.resolve_skill", side_effect=effect):
				self.assertIsNone(_validated_pinned_skill("someones-private-skill"))
		for row in (frappe._dict(self.ROW, use_in_file_box=0), frappe._dict(self.ROW, managed_by_learning=1)):
			with patch("jarvis.tools.get_skill.resolve_skill", return_value=row):
				self.assertIsNone(_validated_pinned_skill("invoicing"))


class TestFileboxDropPin(FrappeTestCase):
	def _a_file(self):
		return frappe.get_doc(
			{"doctype": "File", "file_name": "invoice.txt", "content": "x", "is_private": 1}
		).insert(ignore_permissions=True)

	ROW = TestFileboxPinValidation.ROW

	def _drop(self, skill=None):
		f = self._a_file()
		with patch("jarvis.chat.api.send_message", return_value={"ok": True, "run_id": "r1"}) as sm:
			res = drop_file(f.file_url, "invoice.txt", skill=skill)
		self.addCleanup(self._purge, res["conversation_id"])
		return res["conversation_id"], sm

	def _purge(self, conv):
		frappe.db.rollback()
		frappe.db.delete("Jarvis Approval Request", {"conversation": conv})
		frappe.db.delete("File", {"attached_to_name": conv})
		frappe.db.delete("Jarvis Conversation", {"name": conv})
		frappe.db.commit()

	def _pinned(self, conv):
		return frappe.db.get_value("Jarvis Conversation", conv, "filebox_pinned_skill")

	def _prompt(self, conv, skill=None):
		return build_inbound_prompt(skill, *filebox_skills.prompt_skills(conv))

	def test_valid_pin_stored_and_threaded(self):
		with patch("jarvis.tools.get_skill.resolve_skill", return_value=self.ROW):
			conv, sm = self._drop(skill="invoicing")
		self.assertEqual(self._pinned(conv), "invoicing")
		self.assertEqual(sm.call_args.kwargs["message"], self._prompt(conv, "invoicing"))

	def test_no_skill_is_auto_no_pin(self):
		conv, sm = self._drop(skill=None)
		self.assertIn(self._pinned(conv), (None, ""))  # no pin
		self.assertEqual(sm.call_args.kwargs["message"], self._prompt(conv))
		self.assertTrue(sm.call_args.kwargs["message"].endswith(INBOUND_PROMPT))

	def test_forged_or_inaccessible_skill_asks_and_never_sends(self):
		with patch("jarvis.tools.get_skill.resolve_skill", side_effect=InvalidArgumentError("unknown")):
			conv, sm = self._drop(skill="forged\nskill")
		self.assertIn(self._pinned(conv), (None, ""))  # NOT pinned
		sm.assert_not_called()  # K-D2: no silent fallback to auto
		self.assertTrue(
			frappe.db.exists("Jarvis Approval Request", {"conversation": conv, "routing": "skill_missing"})
		)

	def test_ocr_data_entry_pin(self):
		conv, sm = self._drop(skill=OCR_DATA_ENTRY)
		self.assertEqual(self._pinned(conv), OCR_DATA_ENTRY)
		self.assertEqual(sm.call_args.kwargs["message"], self._prompt(conv, OCR_DATA_ENTRY))

	def test_list_inbound_page_returns_pinned_skill_to_owner(self):
		with patch("jarvis.tools.get_skill.resolve_skill", return_value=self.ROW):
			conv, _ = self._drop(skill="invoicing")
		page = list_inbound_page()
		row = next((r for r in page["rows"] if r["name"] == conv), None)
		self.assertIsNotNone(row, "the dropped conversation should be in the owner's inbound list")
		self.assertEqual(row["pinned_skill"], "invoicing")
