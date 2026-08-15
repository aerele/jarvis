"""Tests for the D5 insight-to-skill endpoints
(``jarvis.chat.learned_api.draft_insight_skill_update`` /
``apply_insight_skill_update``).

The LLM boundary (``jarvis.chat.voice.openrouter_complete``) is ALWAYS mocked.
Covers: the draft happy paths (update / create / none, incl. a fenced-JSON
response), the guard failures (A-class + wrong-status refusals return
``{ok: False, reason}`` without a model call; non-SM throws PermissionError;
an unoffered/managed/Personal target is rejected), candidate selection
(Personal + managed rows never offered, overlap-ranked top-N cap, language
directive rides the system prompt), and the apply seam (skill updated /
created through the controller, JLP terminal-marked like Acknowledge with the
stable applied-note prefix + ``materialized_skill`` provenance, Snoozed routed
through Proposed, everything revalidated server-side).

unittest.TestCase with explicit commits + prefix cleanup, mirroring
``test_learned_api`` (the sibling suite for this module).
"""

from __future__ import annotations

import contextlib
import json
import unittest
from unittest import mock

import frappe

from jarvis.chat import learned_api

JLP = "Jarvis Learned Pattern"
SKILL = "Jarvis Custom Skill"

NON_SM = "isu-nonsm@example.com"
KEY_PREFIX = "isu-test-"
SLUG_PREFIX = "isu-test-"

TARGET_SLUG = "isu-test-invoicing"
TARGET_DESC = "Wholesale discount and shipping charges rules for Acme Traders invoices."
TARGET_INSTR = "# Invoicing\n\n- Check the price list before invoicing."

STATEMENT = "Acme Traders invoices always apply the wholesale discount tier before shipping charges."

LLM = "jarvis.chat.voice.openrouter_complete"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _ensure_non_sm(email: str) -> str:
	# Same shape as test_learned_api._ensure_non_sm: a System User WITHOUT
	# System Manager is the realistic unauthorized actor for these endpoints.
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "isu-nonsm",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
				"roles": [{"role": "Sales User"}],
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
		frappe.db.commit()
	elif frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User")
		frappe.db.commit()
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	return email


@contextlib.contextmanager
def _as(user: str):
	orig = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(orig)


def _wipe() -> None:
	for name in frappe.get_all(JLP, filters={"pattern_key": ["like", f"{KEY_PREFIX}%"]}, pluck="name"):
		frappe.delete_doc(JLP, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(SKILL, filters={"skill_name": ["like", f"{SLUG_PREFIX}%"]}, pluck="name"):
		frappe.delete_doc(SKILL, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk(key: str, **kw) -> str:
	"""Insert a JLP row directly (engine flag bypasses the transition guard).
	Defaults to a B-class Proposed insight - the D5 target population."""
	fields = {
		"doctype": JLP,
		"detector_id": kw.pop("detector_id", "isu-test-det"),
		"pattern_key": KEY_PREFIX + key,
		"domain": kw.pop("domain", "selling"),
		"pattern_statement": kw.pop("statement", STATEMENT),
		"skill_draft": kw.pop(
			"skill_draft",
			f"- {STATEMENT} Evidence: stated in 3 voice note(s) by 2 user(s).",
		),
		"status": kw.pop("status", "Proposed"),
		"surfaced": kw.pop("surfaced", 1),
		"strength_band": kw.pop("strength_band", "Medium"),
		"sensitivity": kw.pop("sensitivity", "B"),
		"effective_sensitivity": kw.pop("effective_sensitivity", "B"),
	}
	evidence = kw.pop("evidence", None)
	if evidence is not None:
		fields["evidence"] = json.dumps(evidence)
	fields.update(kw)

	frappe.flags.jarvis_pattern_engine = True
	try:
		doc = frappe.get_doc(fields)
		doc.flags.ignore_permissions = True
		doc.insert()
	finally:
		frappe.flags.jarvis_pattern_engine = False
	frappe.db.commit()
	return doc.name


def _mk_skill(
	slug=TARGET_SLUG,
	description=TARGET_DESC,
	instructions=TARGET_INSTR,
	scope="Org",
	managed=0,
	enabled=1,
) -> str:
	frappe.flags.jarvis_pattern_engine = bool(managed)
	try:
		doc = frappe.get_doc(
			{
				"doctype": SKILL,
				"skill_name": slug,
				"description": description,
				"instructions": instructions,
				"scope": scope,
				"enabled": enabled,
				"managed_by_learning": 1 if managed else 0,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
	finally:
		frappe.flags.jarvis_pattern_engine = False
	frappe.db.commit()
	return doc.name


def _update_response(slug=TARGET_SLUG, updated="- Updated instructions.") -> str:
	return json.dumps(
		{
			"worth_applying": True,
			"reason": "The insight extends the invoicing skill.",
			"action": "update",
			"skill_name": slug,
			"updated_instructions": updated,
			"new_skill": None,
		}
	)


def _create_response(slug="isu-test-acme-terms") -> str:
	return json.dumps(
		{
			"worth_applying": True,
			"reason": "No candidate covers Acme billing quirks.",
			"action": "create",
			"skill_name": "",
			"updated_instructions": "",
			"new_skill": {
				"skill_name": slug,
				"description": "Acme Traders billing quirks.",
				"instructions": "- Always apply the wholesale tier before shipping.",
			},
		}
	)


class TestInsightSkillUpdate(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.non_sm = _ensure_non_sm(NON_SM)

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	# ------------------------------------------------------------------ #
	# gating
	# ------------------------------------------------------------------ #
	def test_non_sm_is_refused(self):
		name = _mk("g1")
		with _as(self.non_sm):
			with self.assertRaises(frappe.PermissionError):
				learned_api.draft_insight_skill_update(name)
			with self.assertRaises(frappe.PermissionError):
				learned_api.apply_insight_skill_update(
					name,
					"update",
					skill_name=TARGET_SLUG,
					updated_instructions="- x",
				)

	def test_draft_refuses_a_class_without_model_call(self):
		name = _mk("a1", effective_sensitivity="A", sensitivity="A")
		with mock.patch(LLM) as m:
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])
		self.assertIn("A-class", out["reason"])
		m.assert_not_called()

	def test_draft_refuses_wrong_status_without_model_call(self):
		name = _mk("s1", status="Rejected", review_note="no")
		with mock.patch(LLM) as m:
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])
		self.assertIn("Rejected", out["reason"])
		m.assert_not_called()

	# ------------------------------------------------------------------ #
	# draft: happy paths
	# ------------------------------------------------------------------ #
	def test_draft_update_happy_path(self):
		_mk_skill()
		name = _mk("u1")
		with mock.patch(LLM, return_value=_update_response()) as m:
			out = learned_api.draft_insight_skill_update(name)
		self.assertTrue(out["ok"])
		self.assertTrue(out["worth_applying"])
		self.assertEqual(out["action"], "update")
		self.assertEqual(out["skill_name"], TARGET_SLUG)
		# before_instructions is the FULL stored text (for the confirm diff).
		self.assertEqual(out["before_instructions"], TARGET_INSTR)
		self.assertEqual(out["updated_instructions"], "- Updated instructions.")
		self.assertIsNone(out["new_skill"])
		self.assertTrue(out["reason"])

		# ONE call; the candidate rides the user prompt, the D5 schema rides
		# the system prompt.
		m.assert_called_once()
		messages = m.call_args.args[0]
		self.assertIn(learned_api._INSIGHT_SKILL_SYSTEM, messages[0]["content"])
		self.assertIn(TARGET_SLUG, messages[1]["content"])
		self.assertIn(STATEMENT, messages[1]["content"])

	def test_draft_create_happy_path_with_fenced_json(self):
		# No candidates at all + a markdown-fenced response (the tolerant
		# strict-JSON parse the voice_facts pattern prescribes).
		name = _mk("c1", evidence={"source": "voice", "notes": ["VN-1"]})
		raw = "```json\n" + _create_response() + "\n```"
		with mock.patch(LLM, return_value=raw):
			out = learned_api.draft_insight_skill_update(name)
		self.assertTrue(out["ok"])
		self.assertTrue(out["worth_applying"])
		self.assertEqual(out["action"], "create")
		self.assertEqual(out["skill_name"], "isu-test-acme-terms")
		self.assertEqual(out["new_skill"]["skill_name"], "isu-test-acme-terms")
		self.assertEqual(out["new_skill"]["description"], "Acme Traders billing quirks.")
		self.assertEqual(out["before_instructions"], "")

	def test_draft_none_verdict(self):
		name = _mk("n1")
		raw = json.dumps(
			{
				"worth_applying": False,
				"reason": "Too transient to keep.",
				"action": "none",
				"skill_name": "",
				"updated_instructions": "",
				"new_skill": None,
			}
		)
		with mock.patch(LLM, return_value=raw):
			out = learned_api.draft_insight_skill_update(name)
		self.assertTrue(out["ok"])
		self.assertFalse(out["worth_applying"])
		self.assertEqual(out["action"], "none")
		self.assertEqual(out["reason"], "Too transient to keep.")

	# ------------------------------------------------------------------ #
	# draft: candidate selection
	# ------------------------------------------------------------------ #
	def test_candidates_exclude_personal_and_managed(self):
		_mk_skill()
		_mk_skill(
			slug="isu-test-personal", scope="Personal", description=TARGET_DESC, instructions=TARGET_INSTR
		)
		_mk_skill(slug="isu-test-managed", managed=1, description=TARGET_DESC, instructions=TARGET_INSTR)
		_mk_skill(slug="isu-test-disabled", enabled=0, description=TARGET_DESC, instructions=TARGET_INSTR)
		name = _mk("cand1")
		with mock.patch(LLM, return_value=_update_response()) as m:
			learned_api.draft_insight_skill_update(name)
		prompt = m.call_args.args[0][1]["content"]
		self.assertIn(TARGET_SLUG, prompt)
		self.assertNotIn("isu-test-personal", prompt)
		self.assertNotIn("isu-test-managed", prompt)
		self.assertNotIn("isu-test-disabled", prompt)

	def test_candidates_overlap_ranked_and_capped(self):
		# Six zero-overlap fillers + one strongly overlapping target: the cap
		# keeps 5, the overlap winner is in, the oldest filler falls out.
		for i in range(1, 7):
			_mk_skill(
				slug=f"isu-test-filler-{i}",
				description="Unrelated payroll onboarding checklist.",
				instructions="- Unrelated.",
			)
		_mk_skill()  # the overlap winner
		name = _mk("cand2")
		with mock.patch(LLM, return_value=_update_response()) as m:
			learned_api.draft_insight_skill_update(name)
		prompt = m.call_args.args[0][1]["content"]
		self.assertIn(TARGET_SLUG, prompt)
		self.assertNotIn("isu-test-filler-1", prompt)

	# ------------------------------------------------------------------ #
	# draft: model-output validation + failure paths
	# ------------------------------------------------------------------ #
	def test_draft_rejects_unoffered_update_target(self):
		_mk_skill()
		_mk_skill(slug="isu-test-personal", scope="Personal")
		name = _mk("bt1")
		with mock.patch(LLM, return_value=_update_response(slug="isu-test-personal")):
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])
		self.assertIn("isu-test-personal", out["reason"])

	def test_draft_rejects_invalid_new_skill_slug(self):
		name = _mk("bs1")
		raw = json.dumps(
			{
				"worth_applying": True,
				"reason": "r",
				"action": "create",
				"skill_name": "",
				"updated_instructions": "",
				"new_skill": {
					"skill_name": "Bad Slug!",
					"description": "d",
					"instructions": "- i",
				},
			}
		)
		with mock.patch(LLM, return_value=raw):
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])

	def test_draft_rejects_reserved_new_skill_prefix(self):
		name = _mk("bs2")
		raw = json.dumps(
			{
				"worth_applying": True,
				"reason": "r",
				"action": "create",
				"skill_name": "",
				"updated_instructions": "",
				"new_skill": {
					"skill_name": "learned-selling",
					"description": "d",
					"instructions": "- i",
				},
			}
		)
		with mock.patch(LLM, return_value=raw):
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])
		self.assertIn("reserved", out["reason"])

	def test_draft_rejects_oversized_update_instructions(self):
		_mk_skill()
		name = _mk("big1")
		with mock.patch(LLM, return_value=_update_response(updated="x" * 20001)):
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])
		self.assertIn("cap", out["reason"])

	def test_draft_call_failure_returns_reason(self):
		name = _mk("f1")
		with mock.patch(LLM, side_effect=frappe.ValidationError("STT not configured")):
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])
		self.assertIn("language model call failed", out["reason"])

	def test_draft_unparseable_output_returns_reason(self):
		name = _mk("f2")
		with mock.patch(LLM, return_value="sorry, I cannot produce JSON"):
			out = learned_api.draft_insight_skill_update(name)
		self.assertFalse(out["ok"])
		self.assertIn("unparseable", out["reason"])

	# ------------------------------------------------------------------ #
	# apply: update
	# ------------------------------------------------------------------ #
	def test_apply_update_updates_skill_and_marks_jlp(self):
		skill = _mk_skill()
		name = _mk("ap1")
		out = learned_api.apply_insight_skill_update(
			name,
			"update",
			skill_name=TARGET_SLUG,
			updated_instructions="- New rule text.",
		)
		self.assertEqual(out, {"ok": True, "skill_name": TARGET_SLUG})
		self.assertEqual(frappe.db.get_value(SKILL, skill, "instructions"), "- New rule text.")
		row = frappe.db.get_value(
			JLP,
			name,
			["status", "review_note", "materialized_skill", "reviewed_by", "reviewed_at"],
			as_dict=True,
		)
		self.assertEqual(row.status, "Rejected")
		self.assertEqual(row.review_note, learned_api.APPLIED_NOTE_PREFIX + TARGET_SLUG)
		self.assertEqual(row.review_note, "Acknowledged - applied to skill: " + TARGET_SLUG)
		self.assertEqual(row.materialized_skill, skill)
		self.assertEqual(row.reviewed_by, "Administrator")
		self.assertTrue(row.reviewed_at)

	def test_apply_update_refuses_illegal_targets(self):
		managed = _mk_skill(slug="isu-test-managed", managed=1)
		personal = _mk_skill(slug="isu-test-personal", scope="Personal")
		name = _mk("ap2")
		for slug in ("isu-test-managed", "isu-test-personal", "isu-test-missing", ""):
			with self.assertRaises(frappe.ValidationError):
				learned_api.apply_insight_skill_update(
					name, "update", skill_name=slug, updated_instructions="- x"
				)
		# nothing was written anywhere
		self.assertEqual(frappe.db.get_value(SKILL, managed, "instructions"), TARGET_INSTR)
		self.assertEqual(frappe.db.get_value(SKILL, personal, "instructions"), TARGET_INSTR)
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Proposed")

	def test_apply_update_requires_instructions(self):
		_mk_skill()
		name = _mk("ap3")
		with self.assertRaises(frappe.ValidationError):
			learned_api.apply_insight_skill_update(
				name, "update", skill_name=TARGET_SLUG, updated_instructions="   "
			)

	def test_apply_oversized_instructions_hit_controller_cap(self):
		_mk_skill()
		name = _mk("ap4")
		with self.assertRaises(frappe.ValidationError):
			learned_api.apply_insight_skill_update(
				name,
				"update",
				skill_name=TARGET_SLUG,
				updated_instructions="x" * 20001,
			)
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Proposed")

	# ------------------------------------------------------------------ #
	# apply: create
	# ------------------------------------------------------------------ #
	def test_apply_create_creates_org_skill_and_marks_jlp(self):
		name = _mk("ac1")
		out = learned_api.apply_insight_skill_update(
			name,
			"create",
			new_skill={
				"skill_name": "isu-test-acme-terms",
				"description": "Acme Traders billing quirks.",
				"instructions": "- Always apply the wholesale tier.",
			},
		)
		self.assertTrue(out["ok"])
		self.assertEqual(out["skill_name"], "isu-test-acme-terms")
		row = frappe.get_all(
			SKILL,
			filters={"skill_name": "isu-test-acme-terms"},
			fields=["name", "scope", "enabled", "user_invocable", "owner", "managed_by_learning"],
		)[0]
		self.assertEqual(row.scope, "Org")
		self.assertEqual(int(row.enabled), 1)
		self.assertEqual(int(row.user_invocable), 1)
		self.assertEqual(int(row.managed_by_learning), 0)
		self.assertEqual(row.owner, "Administrator")
		jlp = frappe.db.get_value(JLP, name, ["status", "review_note", "materialized_skill"], as_dict=True)
		self.assertEqual(jlp.status, "Rejected")
		self.assertEqual(
			jlp.review_note,
			learned_api.APPLIED_NOTE_PREFIX + "isu-test-acme-terms",
		)
		self.assertEqual(jlp.materialized_skill, row.name)

	def test_apply_create_requires_payload(self):
		name = _mk("ac2")
		with self.assertRaises(frappe.ValidationError):
			learned_api.apply_insight_skill_update(name, "create", new_skill=None)
		with self.assertRaises(frappe.ValidationError):
			learned_api.apply_insight_skill_update(name, "create", new_skill={"skill_name": "isu-test-x"})
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Proposed")

	# ------------------------------------------------------------------ #
	# apply: guards + Snoozed routing
	# ------------------------------------------------------------------ #
	def test_apply_refuses_a_class(self):
		_mk_skill()
		name = _mk("ag1", effective_sensitivity="A", sensitivity="A")
		with self.assertRaises(frappe.ValidationError):
			learned_api.apply_insight_skill_update(
				name, "update", skill_name=TARGET_SLUG, updated_instructions="- x"
			)
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Proposed")

	def test_apply_refuses_wrong_status(self):
		_mk_skill()
		name = _mk("ag2", status="Active")
		with self.assertRaises(frappe.ValidationError):
			learned_api.apply_insight_skill_update(
				name, "update", skill_name=TARGET_SLUG, updated_instructions="- x"
			)
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Active")

	def test_apply_refuses_bad_action(self):
		_mk_skill()
		name = _mk("ag3")
		with self.assertRaises(frappe.ValidationError):
			learned_api.apply_insight_skill_update(name, "delete")
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Proposed")

	def test_apply_from_snoozed_routes_through_proposed(self):
		# Snoozed -> Rejected is not a legal JLP transition; the apply routes
		# Snoozed -> Proposed -> Rejected so the state machine holds.
		_mk_skill()
		name = _mk("sz1", status="Snoozed", snoozed_until="2030-01-01")
		out = learned_api.apply_insight_skill_update(
			name,
			"update",
			skill_name=TARGET_SLUG,
			updated_instructions="- From snoozed.",
		)
		self.assertTrue(out["ok"])
		row = frappe.db.get_value(JLP, name, ["status", "snoozed_until", "review_note"], as_dict=True)
		self.assertEqual(row.status, "Rejected")
		self.assertFalse(row.snoozed_until)
		self.assertEqual(row.review_note, learned_api.APPLIED_NOTE_PREFIX + TARGET_SLUG)

	def test_apply_from_stale(self):
		_mk_skill()
		name = _mk("st1", status="Stale", stale_reason="drifted")
		out = learned_api.apply_insight_skill_update(
			name,
			"update",
			skill_name=TARGET_SLUG,
			updated_instructions="- From stale.",
		)
		self.assertTrue(out["ok"])
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Rejected")


if __name__ == "__main__":
	unittest.main()
