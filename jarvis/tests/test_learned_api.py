"""Tests for the SM-gated learning-board API (jarvis/chat/learned_api.py).

Covers, per plan sections 6.4/6.5/5.1: System-Manager gating (non-SM ->
PermissionError), the frozen list envelope + domain facets +
board counters, the approve/reject/un-approve/restore/snooze lifecycle
transitions with their TOCTOU source guards, the A-class-only batch_approve
guard, run-now enqueue, apply delegation to the compiler, the settings
read/write window validation, the LLM-polish endpoint (A-class gate, column
writes only, draft_polished marker), the correction loop's distinct-user
quorum + per-user cooldown + durable flag_band_cap + Low-rung Stale flip, and
the cutover custom-sync surface on the apply-status poll.

unittest.TestCase with explicit commits + prefix cleanup (like
test_feature_pages_api / test_agents_marketplace): the endpoints run raw SQL
and need a real non-SM user to prove the permission gate. Every fixture row
carries an ``la-test-`` marker so cleanup is owner-independent.
"""

from __future__ import annotations

import contextlib
import json
import sys
import types
import unittest
from unittest import mock

import frappe
from frappe.utils import add_days, now_datetime

from jarvis.chat import learned_api
from jarvis.tests._role_guard import enforced_role_guards

JLP = "Jarvis Learned Pattern"
RUN = "Jarvis Pattern Run"
SETTINGS = "Jarvis Settings"

NON_SM = "la-nonsm@example.com"
WEB_USER = "la-webuser@example.com"
KEY_PREFIX = "la-test-"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _ensure_non_sm(email: str) -> str:
	# A realistic unauthorized actor for these desk-admin endpoints is a System
	# User who lacks System Manager (e.g. a Sales User), NOT a portal/Website
	# User - a Website User is a weaker negative test and could mask a bug if an
	# endpoint ever gated on user_type instead of the role.
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "la-nonsm",
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
		# Promote a fixture left over from an older run as a Website User.
		frappe.db.set_value("User", email, "user_type", "System User")
		frappe.db.commit()
	if "System Manager" in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).remove_roles("System Manager")
		frappe.db.commit()
	return email


def _ensure_website_user(email: str) -> str:
	# A portal user: the correction-loop endpoint must refuse it (System User
	# only), unlike the deliberately weaker Website-User negative the SM-gate
	# tests avoid (see _ensure_non_sm).
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "la-web",
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "Website User",
			}
		)
		u.flags.ignore_permissions = True
		u.insert()
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


@contextlib.contextmanager
def _polish_flag(on: int):
	"""Temporarily set the pattern_llm_polish Settings flag."""
	orig = frappe.db.get_single_value(SETTINGS, "pattern_llm_polish")
	frappe.db.set_single_value(SETTINGS, "pattern_llm_polish", on, update_modified=False)
	frappe.db.commit()
	try:
		yield
	finally:
		frappe.db.set_single_value(SETTINGS, "pattern_llm_polish", orig or 0, update_modified=False)
		frappe.db.commit()


def _wipe() -> None:
	for name in frappe.get_all(JLP, filters={"pattern_key": ["like", f"{KEY_PREFIX}%"]}, pluck="name"):
		frappe.delete_doc(JLP, name, force=True, ignore_permissions=True)
	for name in frappe.get_all(RUN, filters={"requested_by": ["like", "%learned-run%"]}, pluck="name"):
		frappe.delete_doc(RUN, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _mk(key: str, **kw) -> str:
	"""Insert a JLP row directly (engine flag bypasses the transition guard so a
	fixture can start in any status)."""
	fields = {
		"doctype": JLP,
		"detector_id": kw.pop("detector_id", "la-test-det"),
		"pattern_key": KEY_PREFIX + key,
		"domain": kw.pop("domain", "selling"),
		"pattern_statement": kw.pop("statement", f"Statement for {key}"),
		"skill_draft": kw.pop(
			"skill_draft",
			f"- Rule {key}. Evidence: 90% of 100 Sales Invoices since 2024-01.",
		),
		"status": kw.pop("status", "Proposed"),
		"surfaced": kw.pop("surfaced", 1),
		"strength_band": kw.pop("strength_band", "High"),
		"sensitivity": kw.pop("sensitivity", "A"),
		"effective_sensitivity": kw.pop("effective_sensitivity", "A"),
	}
	evidence = kw.pop("evidence", None)
	if evidence is not None:
		fields["evidence"] = json.dumps(evidence)
	roles = kw.pop("roles", None)
	if roles:
		fields["roles"] = [{"role": r} for r in roles]
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


class TestLearnedApi(unittest.TestCase):
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
	# SM gating
	# ------------------------------------------------------------------ #
	def test_non_sm_is_refused(self):
		_mk("g1")
		with _as(self.non_sm), enforced_role_guards():
			for call in (
				lambda: learned_api.list_learned_patterns_page(),
				lambda: learned_api.pending_learned_count(),
				lambda: learned_api.get_learning_status(),
				lambda: learned_api.run_pattern_analysis_now(),
			):
				with self.assertRaises(frappe.PermissionError):
					call()

	# ------------------------------------------------------------------ #
	# list envelope + facets + counters
	# ------------------------------------------------------------------ #
	def test_list_envelope_shape_and_facets(self):
		_mk("e1", domain="selling", surfaced=1)
		_mk("e2", domain="buying", surfaced=1)
		_mk("e3", domain="selling", surfaced=1, strength_band="Low")
		_mk("q1", domain="stock", surfaced=0)  # queued (unsurfaced)
		_mk("ap1", domain="accounts", surfaced=1, status="Approved")

		out = learned_api.list_learned_patterns_page(status="Proposed", surfaced=1)
		for key in (
			"rows",
			"total",
			"has_more",
			"start",
			"page_length",
			"facets",
			"queued_count",
			"pending_apply_count",
			"review_activity",
		):
			self.assertIn(key, out)

		self.assertIn("domain", out["facets"])
		facet_domains = {f["value"] for f in out["facets"]["domain"]}
		self.assertIn("selling", facet_domains)
		self.assertIn("buying", facet_domains)

		# queued_count counts the unsurfaced Proposed row.
		self.assertGreaterEqual(out["queued_count"], 1)
		# pending_apply_count counts the Approved row.
		self.assertGreaterEqual(out["pending_apply_count"], 1)

		# rows are plain-English cards: raw stats must NOT be present.
		self.assertTrue(out["rows"])
		row = out["rows"][0]
		for banned in ("support_n", "n_rows", "confidence_pct", "wilson_low", "gap"):
			self.assertNotIn(banned, row)
		for present in ("pattern_statement", "strength_band", "domain", "status"):
			self.assertIn(present, row)

	def test_list_domain_filter_and_search(self):
		_mk("d1", domain="selling", statement="apples for dealer")
		_mk("d2", domain="buying", statement="oranges for supplier")

		only_selling = learned_api.list_learned_patterns_page(domain="selling")
		stmts = {r["pattern_statement"] for r in only_selling["rows"]}
		self.assertIn("apples for dealer", stmts)
		self.assertNotIn("oranges for supplier", stmts)

		found = learned_api.list_learned_patterns_page(search="oranges")
		stmts = {r["pattern_statement"] for r in found["rows"]}
		self.assertIn("oranges for supplier", stmts)
		self.assertNotIn("apples for dealer", stmts)

	def test_list_rejects_bad_filters(self):
		with self.assertRaises(frappe.ValidationError):
			learned_api.list_learned_patterns_page(domain="not-a-domain")
		with self.assertRaises(frappe.ValidationError):
			learned_api.list_learned_patterns_page(status="Bogus")
		with self.assertRaises(frappe.ValidationError):
			learned_api.list_learned_patterns_page(strength="Enormous")

	# ------------------------------------------------------------------ #
	# detail drill-down
	# ------------------------------------------------------------------ #
	def test_get_learned_pattern_drilldown(self):
		name = _mk(
			"det1",
			support_n=214,
			n_rows=800,
			confidence_pct=96.0,
			wilson_low=0.91,
			gap=0.4,
			roles=["System Manager"],
			evidence={"exceptions": [{"ref": "SINV-1"}], "base_rate": 0.5},
		)
		out = learned_api.get_learned_pattern(name)
		self.assertEqual(out["name"], name)
		self.assertEqual(out["support_n"], 214)  # drill-down keeps the raw stats
		self.assertEqual(out["roles"], ["System Manager"])
		self.assertEqual(out["evidence"]["base_rate"], 0.5)
		self.assertEqual(len(out["exceptions"]), 1)
		self.assertTrue(out["compiled_preview"])  # falls back to skill_draft

	def test_compiled_preview_is_single_bullet(self):
		# The drill-down preview is THIS pattern's compiled bullet (previously the
		# call passed a pattern name where compile_preview wanted a domain, so it
		# always fell back to the raw draft - fix 4).
		name = _mk(
			"cp1",
			skill_draft="- Prefer letterhead LH1. Evidence: 95% of 80 Sales Invoices since 2024-03.",
		)
		out = learned_api.get_learned_pattern(name)
		self.assertTrue(out["compiled_preview"].startswith("- "))
		self.assertIn(name, out["compiled_preview"])  # JLP ref appended by the compiler

	# ------------------------------------------------------------------ #
	# lifecycle transitions + TOCTOU
	# ------------------------------------------------------------------ #
	def test_approve_stamps_and_transitions(self):
		name = _mk("a1")
		out = learned_api.approve_learned_pattern(name)
		self.assertEqual(out["status"], "Approved")
		doc = frappe.get_doc(JLP, name)
		self.assertEqual(doc.status, "Approved")
		self.assertEqual(doc.reviewed_by, "Administrator")
		self.assertEqual(doc.approved_by, "Administrator")
		self.assertTrue(doc.reviewed_at)
		self.assertFalse(doc.draft_edited)

	def test_approve_with_edit_freezes_evidence(self):
		name = _mk("a2")
		out = learned_api.approve_learned_pattern(name, edited_skill_draft="- My edited rule.")
		self.assertEqual(out["draft_edited"], 1)
		doc = frappe.get_doc(JLP, name)
		self.assertEqual(doc.skill_draft, "- My edited rule.")
		self.assertEqual(doc.draft_edited, 1)
		detail = learned_api.get_learned_pattern(name)
		self.assertTrue(detail["frozen_evidence_label"])

	def test_approve_toctou_guard(self):
		name = _mk("a3")
		learned_api.approve_learned_pattern(name)
		# A second approve sees the row is no longer Proposed and refuses.
		with self.assertRaises(frappe.ValidationError):
			learned_api.approve_learned_pattern(name)

	def test_approve_refuses_non_a_class(self):
		# B/C are insight-only in Phase 1: approve must refuse (the compiler is
		# A-only, so a B/C Approved row would never activate and would wedge the
		# pending-apply bar) and point the SM to Acknowledge.
		b = _mk("bc1", effective_sensitivity="B")
		with self.assertRaises(frappe.ValidationError):
			learned_api.approve_learned_pattern(b)
		self.assertEqual(frappe.db.get_value(JLP, b, "status"), "Proposed")
		c = _mk("bc2", effective_sensitivity="C")
		with self.assertRaises(frappe.ValidationError):
			learned_api.approve_learned_pattern(c)
		self.assertEqual(frappe.db.get_value(JLP, c, "status"), "Proposed")

	def test_approve_clears_flag_band_cap_and_flag_stale_reason(self):
		# Shared contract: a human approval overrides a flag-driven demotion -
		# the durable band cap and the flag-origin stale_reason are cleared, so
		# the pipeline stops clamping and the board banner goes away.
		name = _mk(
			"fc1",
			status="Stale",
			strength_band="Low",
			stale_reason="flagged by 2 users",
			flag_band_cap="Low",
		)
		out = learned_api.approve_learned_pattern(name)
		self.assertEqual(out["status"], "Approved")
		row = frappe.db.get_value(JLP, name, ["flag_band_cap", "stale_reason"], as_dict=True)
		self.assertFalse(row.flag_band_cap)
		self.assertFalse(row.stale_reason)

	def test_approve_keeps_drift_stale_reason(self):
		# Only FLAG-origin reasons are cleared on approve; a drift-origin reason
		# stays (the drift pass owns it).
		name = _mk(
			"fc2",
			status="Stale",
			stale_reason="confidence dropped 96% -> 71% (window 18 months)",
		)
		learned_api.approve_learned_pattern(name)
		self.assertIn(
			"confidence dropped",
			frappe.db.get_value(JLP, name, "stale_reason") or "",
		)

	def test_acknowledge_b_class_is_terminal_and_uncounted(self):
		before = learned_api._pending_apply_count()
		b = _mk("ack1", effective_sensitivity="B", surfaced=1)
		out = learned_api.acknowledge_learned_pattern(b)
		self.assertTrue(out["acknowledged"])
		self.assertEqual(out["status"], "Rejected")
		self.assertEqual(frappe.db.get_value(JLP, b, "review_note"), learned_api.ACK_NOTE)
		self.assertEqual(frappe.db.get_value(JLP, b, "reviewed_by"), "Administrator")
		# acknowledged rows are NOT pending-apply (they are terminal, not Approved).
		self.assertEqual(learned_api._pending_apply_count(), before)
		# already terminal -> a second acknowledge is refused (source guard).
		with self.assertRaises(frappe.ValidationError):
			learned_api.acknowledge_learned_pattern(b)

	def test_acknowledge_refuses_a_class(self):
		a = _mk("ack2", effective_sensitivity="A")
		with self.assertRaises(frappe.ValidationError):
			learned_api.acknowledge_learned_pattern(a)
		self.assertEqual(frappe.db.get_value(JLP, a, "status"), "Proposed")

	def test_pending_apply_count_excludes_bc_approved(self):
		# Only A-class Approved rows are pending-apply; B/C Approved (which the
		# compiler would never activate) must not inflate the count.
		baseline = learned_api._pending_apply_count()
		_mk("pa1", status="Approved", effective_sensitivity="A")
		_mk("pa2", status="Approved", effective_sensitivity="B")
		_mk("pa3", status="Approved", effective_sensitivity="C")
		self.assertEqual(learned_api._pending_apply_count(), baseline + 1)

	def test_reject_requires_reason(self):
		name = _mk("r1")
		with self.assertRaises(frappe.ValidationError):
			learned_api.reject_learned_pattern(name, reason="   ")
		out = learned_api.reject_learned_pattern(name, reason="not a real habit")
		self.assertEqual(out["status"], "Rejected")
		self.assertEqual(frappe.db.get_value(JLP, name, "review_note"), "not a real habit")

	def test_unapprove_returns_to_proposed(self):
		name = _mk("u1")
		learned_api.approve_learned_pattern(name)
		out = learned_api.unapprove_learned_pattern(name)
		self.assertEqual(out["status"], "Proposed")
		self.assertFalse(frappe.db.get_value(JLP, name, "approved_by"))
		# Cannot un-approve something that is not Approved.
		with self.assertRaises(frappe.ValidationError):
			learned_api.unapprove_learned_pattern(name)

	def test_unapprove_blocked_while_apply_pending(self):
		name = _mk("u2", status="Approved")
		with mock.patch.object(learned_api, "_apply_pending", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				learned_api.unapprove_learned_pattern(name)
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Approved")

	def test_unapprove_blocked_while_apply_marker_set(self):
		# The compiler's apply-in-progress marker closes the compile -> flip TOCTOU
		# window that the sync-status `pending` flag alone misses.
		name = _mk("u3", status="Approved")
		with mock.patch("jarvis.learning.compiler.apply_in_progress", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				learned_api.unapprove_learned_pattern(name)
		self.assertEqual(frappe.db.get_value(JLP, name, "status"), "Approved")

	def test_restore_rejected(self):
		name = _mk("rr1", status="Rejected", review_note="was wrong")
		out = learned_api.restore_rejected_pattern(name)
		self.assertEqual(out["status"], "Proposed")
		with self.assertRaises(frappe.ValidationError):
			learned_api.restore_rejected_pattern(name)  # already Proposed

	def test_snooze_validates_days(self):
		name = _mk("sn1")
		with self.assertRaises(frappe.ValidationError):
			learned_api.snooze_learned_pattern(name, days=5)
		out = learned_api.snooze_learned_pattern(name, days=30)
		self.assertEqual(out["status"], "Snoozed")
		self.assertTrue(frappe.db.get_value(JLP, name, "snoozed_until"))

	# ------------------------------------------------------------------ #
	# batch approve (A-only)
	# ------------------------------------------------------------------ #
	def test_batch_approve_a_only(self):
		a1 = _mk("b1", effective_sensitivity="A")
		a2 = _mk("b2", effective_sensitivity="A")
		out = learned_api.batch_approve([a1, a2])
		self.assertEqual(out["count"], 2)
		self.assertEqual(frappe.db.get_value(JLP, a1, "status"), "Approved")
		self.assertEqual(frappe.db.get_value(JLP, a2, "status"), "Approved")

	def test_batch_approve_refuses_mixed_sensitivity(self):
		a1 = _mk("b3", effective_sensitivity="A")
		b1 = _mk("b4", effective_sensitivity="B")
		with self.assertRaises(frappe.ValidationError):
			learned_api.batch_approve([a1, b1])
		# The whole batch is refused: the A-class row must NOT have been approved.
		self.assertEqual(frappe.db.get_value(JLP, a1, "status"), "Proposed")

	def test_batch_approve_unknown_name(self):
		with self.assertRaises(frappe.ValidationError):
			learned_api.batch_approve(["JLP-does-not-exist"])

	# ------------------------------------------------------------------ #
	# badges / counters
	# ------------------------------------------------------------------ #
	def test_pending_learned_count(self):
		_mk("p1", surfaced=1, status="Proposed")
		_mk("p2", surfaced=0, status="Proposed")  # queued, not counted
		_mk("p3", surfaced=1, status="Approved")  # decided, not counted
		self.assertEqual(learned_api.pending_learned_count(), 1)

	# ------------------------------------------------------------------ #
	# apply delegation + status proxy
	# ------------------------------------------------------------------ #
	def test_apply_learned_skills_delegates_to_compiler(self):
		# Patch the delegated function directly, NOT the whole module via
		# sys.modules. apply_learned_skills() reaches the compiler with
		# ``from jarvis.learning import compiler`` (a package-attribute bind that
		# returns the REAL module once it has been imported anywhere), so a
		# sys.modules stub is bypassed for the top-level call yet still shadows
		# the deep ``from jarvis.learning.compiler import build_learned_push_payload``
		# in enqueue_learned_skills_push -> ImportError "(unknown location)".
		# That made the test order-dependent (green alone, red in the full suite/CI).
		with mock.patch(
			"jarvis.learning.compiler.apply_learned_skills",
			return_value={"ok": True, "sentinel": "compiled"},
		) as m:
			out = learned_api.apply_learned_skills()
		self.assertEqual(out["sentinel"], "compiled")
		self.assertTrue(m.called)

	def test_get_learned_apply_status_proxies_sync(self):
		out = learned_api.get_learned_apply_status()
		self.assertIn("last_sync_status", out)
		self.assertIn("pending", out)

	# ------------------------------------------------------------------ #
	# run now
	# ------------------------------------------------------------------ #
	def test_run_now_enqueues(self):
		orig_enabled = frappe.db.get_single_value(SETTINGS, "pattern_learning_enabled")
		frappe.db.set_single_value(SETTINGS, "pattern_learning_enabled", 1)
		frappe.db.commit()
		try:
			with mock.patch("frappe.enqueue") as enq:
				out = learned_api.run_pattern_analysis_now()
			self.assertTrue(out["ok"])
			self.assertTrue(out["run"])
			self.assertTrue(frappe.db.exists(RUN, out["run"]))
			self.assertTrue(enq.called)
			# clean the run row up regardless of its requested_by value
			frappe.delete_doc(RUN, out["run"], force=True, ignore_permissions=True)
			frappe.db.commit()
		finally:
			frappe.db.set_single_value(SETTINGS, "pattern_learning_enabled", orig_enabled or 0)
			frappe.db.commit()

	# ------------------------------------------------------------------ #
	# settings read/write + window validation
	# ------------------------------------------------------------------ #
	def test_get_learning_settings_shape(self):
		out = learned_api.get_learning_settings()
		self.assertIn("settings", out)
		self.assertIn("pattern_window_start", out["settings"])
		self.assertIsNone(out["preflight"])  # lazy: not computed unless asked

	def test_set_learning_settings_writes_and_validates_window(self):
		s = frappe.get_single(SETTINGS)
		orig = {f: s.get(f) for f in learned_api._SETTINGS_FIELDS}
		try:
			with mock.patch("frappe.enqueue"):
				# A valid write persists.
				learned_api.set_learning_settings({"pattern_max_proposals_per_run": 7})
				self.assertEqual(frappe.db.get_single_value(SETTINGS, "pattern_max_proposals_per_run"), 7)
				# Enabling with a sub-1h window is rejected by the doc validation.
				with self.assertRaises(frappe.ValidationError):
					learned_api.set_learning_settings(
						{
							"pattern_learning_enabled": 1,
							"pattern_window_start": "02:00:00",
							"pattern_window_end": "02:30:00",
						}
					)
		finally:
			with mock.patch("frappe.enqueue"):
				restore = frappe.get_single(SETTINGS)
				for f, v in orig.items():
					restore.set(f, v)
				restore.save()
				frappe.db.commit()

	def test_set_learning_settings_rejects_unknown_field(self):
		with self.assertRaises(frappe.ValidationError):
			learned_api.set_learning_settings({"pattern_learning_enabled": 1, "llm_model": "x"})

	def test_set_learning_settings_no_on_update_side_effect(self):
		# A pattern_* write must NOT run Jarvis Settings.on_update (LLM pool
		# re-sync); the fix writes via db.set_value(update_modified=False) after an
		# inline window validation, so no save hook fires (fix 5).
		s = frappe.get_single(SETTINGS)
		orig = {f: s.get(f) for f in learned_api._SETTINGS_FIELDS}
		try:
			with mock.patch("frappe.enqueue") as enq:
				learned_api.set_learning_settings({"pattern_max_proposals_per_run": 9})
			self.assertEqual(frappe.db.get_single_value(SETTINGS, "pattern_max_proposals_per_run"), 9)
			self.assertFalse(enq.called)
		finally:
			frappe.db.set_single_value(SETTINGS, orig, update_modified=False)
			frappe.db.commit()

	def test_get_learning_settings_coalesces_nulls_to_defaults(self):
		# A never-seeded Single reads null for the pattern_* config; the read must
		# coalesce to the field defaults so the card never shows blank/0 (fix 6).
		s = frappe.get_single(SETTINGS)
		orig = {f: s.get(f) for f in learned_api._SETTINGS_FIELDS}
		try:
			frappe.db.set_value(
				SETTINGS,
				SETTINGS,
				{
					"pattern_window_start": None,
					"pattern_window_end": None,
					"pattern_max_proposals_per_run": None,
					"pattern_row_budget_per_night": None,
				},
				update_modified=False,
			)
			frappe.db.commit()
			frappe.clear_document_cache(SETTINGS, SETTINGS)
			out = learned_api.get_learning_settings()["settings"]
			self.assertEqual(out["pattern_window_start"], "01:00:00")
			self.assertEqual(out["pattern_window_end"], "05:00:00")
			self.assertEqual(out["pattern_max_proposals_per_run"], 10)
			self.assertEqual(out["pattern_row_budget_per_night"], 500000)
		finally:
			frappe.db.set_single_value(SETTINGS, orig, update_modified=False)
			frappe.db.commit()
			frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_seed_settings_defaults_fills_only_nulls(self):
		# bootstrap.after_migrate persists the pattern_* defaults when null (Frappe
		# does not backfill Single defaults on migrate) - fix 6.
		from frappe.utils import get_time

		from jarvis.learning import bootstrap

		s = frappe.get_single(SETTINGS)
		orig = {f: s.get(f) for f in learned_api._SETTINGS_FIELDS}
		try:
			frappe.db.set_value(
				SETTINGS,
				SETTINGS,
				{k: None for k in bootstrap._SETTINGS_DEFAULTS},
				update_modified=False,
			)
			# a non-null operator value must NOT be clobbered
			frappe.db.set_value(
				SETTINGS,
				SETTINGS,
				{"pattern_max_proposals_per_run": 3},
				update_modified=False,
			)
			frappe.db.commit()
			frappe.clear_document_cache(SETTINGS, SETTINGS)
			bootstrap._seed_settings_defaults()

			def _hhmmss(field):
				# get_single_value casts Time to timedelta; normalize to HH:MM:SS.
				return str(get_time(str(frappe.db.get_single_value(SETTINGS, field))))

			self.assertEqual(_hhmmss("pattern_window_start"), "01:00:00")
			self.assertEqual(_hhmmss("pattern_window_end"), "05:00:00")
			self.assertEqual(frappe.db.get_single_value(SETTINGS, "pattern_row_budget_per_night"), 500000)
			# left the operator's non-null value alone
			self.assertEqual(frappe.db.get_single_value(SETTINGS, "pattern_max_proposals_per_run"), 3)
		finally:
			frappe.db.set_single_value(SETTINGS, orig, update_modified=False)
			frappe.db.commit()
			frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_naming_series_readiness_uses_series_table(self):
		# The cfg-naming-series detector's antecedent is a DocType name; readiness
		# must measure numbered documents (tabSeries), not recent DocType creations
		# (~0 on a stable site -> permanent false data_starved) - fix 7.
		from jarvis.learning import bootstrap, registry

		series_total = int(frappe.db.sql("select coalesce(sum(`current`), 0) from `tabSeries`")[0][0])
		self.assertEqual(bootstrap._naming_series_doc_count(), series_total)

		naming = registry.get_detector("cfg-naming-series")
		self.assertEqual(
			bootstrap._readiness_count(naming, naming["doctype"], naming.get("window_months") or 12),
			series_total,
		)
		# a normal party/group detector still counts its OWN doctype's recent rows.
		other = registry.get_detector("sell-group-payment-terms")
		self.assertEqual(
			bootstrap._readiness_count(other, other["doctype"], 18),
			bootstrap._count_recent(other["doctype"], 18),
		)

	# ------------------------------------------------------------------ #
	# LLM polish endpoint (plan 5.5 Phase 2)
	# ------------------------------------------------------------------ #
	def test_polish_requires_settings_flag(self):
		name = _mk("po1")
		with _polish_flag(0):
			with self.assertRaises(frappe.ValidationError):
				learned_api.polish_learned_draft(name)

	def test_polish_refuses_non_a_class(self):
		# B/C drafts embed raw party names; the polish prompt must stay
		# A-class-clean by construction (polish.py), so the endpoint gates on
		# effective_sensitivity like approve/batch_approve - and the model turn
		# must never even start for B/C.
		name = _mk("po2", effective_sensitivity="B")
		with _polish_flag(1):
			with mock.patch("jarvis.learning.polish.polish_skill_draft") as turn:
				with self.assertRaises(frappe.ValidationError):
					learned_api.polish_learned_draft(name)
		turn.assert_not_called()

	def test_polish_writes_only_changed_columns(self):
		# The gateway turn is multi-second: a concurrent update_modified=False
		# write landing mid-turn (e.g. the stale-pointer clear in
		# _clear_stale_materialized_pointers) must survive - the old
		# full-document doc.save() of the pre-turn snapshot reverted it.
		name = _mk("po3", status="Stale")
		polished = "- Polished rule. Evidence: 90% of 100 Sales Invoices since 2024-01."

		def _turn(pattern_name, acting_user):
			frappe.db.set_value(
				JLP,
				pattern_name,
				{"materialized_skill": "learned-selling"},
				update_modified=False,
			)
			return {"ok": True, "text": polished, "reason": ""}

		with _polish_flag(1):
			with mock.patch("jarvis.learning.polish.polish_skill_draft", side_effect=_turn):
				out = learned_api.polish_learned_draft(name)
		self.assertTrue(out["ok"])
		row = frappe.db.get_value(
			JLP,
			name,
			["skill_draft", "draft_edited", "draft_polished", "materialized_skill"],
			as_dict=True,
		)
		self.assertEqual(row.skill_draft, polished)
		self.assertEqual(int(row.draft_edited), 0)  # evidence line stays un-frozen
		# ...but the polish marker makes the wording durable across mining/drift.
		self.assertEqual(int(row.draft_polished), 1)
		self.assertEqual(row.materialized_skill, "learned-selling")  # not reverted

	def test_polish_discarded_when_status_changes_mid_turn(self):
		# A concurrent decision during the turn wins: the polished text is
		# dropped instead of resurrecting a Proposed/Stale state.
		name = _mk("po4")
		orig_draft = frappe.db.get_value(JLP, name, "skill_draft")

		def _turn(pattern_name, acting_user):
			frappe.db.set_value(JLP, pattern_name, {"status": "Rejected"}, update_modified=False)
			return {"ok": True, "text": "- Should be discarded.", "reason": ""}

		with _polish_flag(1):
			with mock.patch("jarvis.learning.polish.polish_skill_draft", side_effect=_turn):
				out = learned_api.polish_learned_draft(name)
		self.assertFalse(out["ok"])
		self.assertIn("Rejected", out["reason"])
		self.assertEqual(frappe.db.get_value(JLP, name, "skill_draft"), orig_draft)

	def test_polish_not_ok_writes_nothing(self):
		name = _mk("po5")
		orig_draft = frappe.db.get_value(JLP, name, "skill_draft")
		with _polish_flag(1):
			with mock.patch(
				"jarvis.learning.polish.polish_skill_draft",
				return_value={
					"ok": False,
					"text": None,
					"reason": "monthly polish budget exhausted",
				},
			):
				out = learned_api.polish_learned_draft(name)
		self.assertFalse(out["ok"])
		self.assertEqual(out["reason"], "monthly polish budget exhausted")
		self.assertEqual(frappe.db.get_value(JLP, name, "skill_draft"), orig_draft)
		self.assertEqual(int(frappe.db.get_value(JLP, name, "draft_polished") or 0), 0)

	# ------------------------------------------------------------------ #
	# correction loop (plan 6.5): flag_learned_default
	# ------------------------------------------------------------------ #
	def test_flag_requires_system_user(self):
		name = _mk("fl1", status="Active")
		web = _ensure_website_user(WEB_USER)
		with _as(web):
			with self.assertRaises(frappe.PermissionError):
				learned_api.flag_learned_default(name, note="wrong here")
		with _as("Guest"):
			with self.assertRaises(frappe.PermissionError):
				learned_api.flag_learned_default(name)
		self.assertEqual(int(frappe.db.get_value(JLP, name, "flags_count") or 0), 0)

	def test_flag_allowed_for_non_sm_system_user(self):
		# Deliberately NOT SM-gated: the flag comes from the chat user who just
		# watched a learned default misfire.
		name = _mk("fl2", status="Active", strength_band="High")
		with _as(self.non_sm):
			out = learned_api.flag_learned_default(name, note="wrong for dealer X")
		self.assertTrue(out["ok"])
		self.assertEqual(out["flags_count"], 1)
		self.assertEqual(out["distinct_users"], 1)
		self.assertFalse(out["demoted"])

	def test_flag_only_on_active_or_approved(self):
		proposed = _mk("fl4")  # Proposed
		with self.assertRaises(frappe.ValidationError):
			learned_api.flag_learned_default(proposed)
		stale = _mk("fl5", status="Stale")
		with self.assertRaises(frappe.ValidationError):
			learned_api.flag_learned_default(stale)

	def test_flag_dedupes_per_user_and_counts_once(self):
		name = _mk("fl6", status="Active", strength_band="High")
		learned_api.flag_learned_default(name, note="first")
		out = learned_api.flag_learned_default(name, note="second")
		self.assertEqual(out["flags_count"], 1)  # one count PER USER, not per event
		self.assertEqual(out["distinct_users"], 1)
		self.assertFalse(out["demoted"])
		entries = json.loads(frappe.db.get_value(JLP, name, "counter_evidence"))
		self.assertEqual(len(entries), 1)  # updated in place, never stacked
		self.assertEqual(entries[0]["user"], "Administrator")
		self.assertEqual(entries[0]["note"], "second")

	def test_flag_single_user_never_demotes(self):
		# The old `flags_count >= 3` OR-branch let ONE desk user ratchet a shared
		# default down (and spam every SM); demotion now requires the genuine
		# >= 2 DISTINCT-user quorum, however often one user re-flags.
		name = _mk("fl7", status="Active", strength_band="High")
		out = None
		with mock.patch("jarvis.learning.lifecycle.notify_system_managers") as notify:
			for _i in range(4):
				out = learned_api.flag_learned_default(name)
		self.assertFalse(out["demoted"])
		self.assertEqual(out["flags_count"], 1)
		row = frappe.db.get_value(JLP, name, ["strength_band", "stale_reason", "status"], as_dict=True)
		self.assertEqual(row.strength_band, "High")
		self.assertFalse(row.stale_reason)
		self.assertEqual(row.status, "Active")
		notify.assert_not_called()

	def test_flag_two_distinct_users_demotes_and_notifies(self):
		name = _mk("fl8", status="Approved", strength_band="High")
		learned_api.flag_learned_default(name, note="admin flag")
		try:
			with mock.patch("frappe.utils.user.get_users_with_role", return_value=[self.non_sm]):
				with _as(self.non_sm):
					out = learned_api.flag_learned_default(name, note="also wrong")
			self.assertTrue(out["demoted"])
			self.assertEqual(out["distinct_users"], 2)
			row = frappe.db.get_value(
				JLP,
				name,
				["strength_band", "flag_band_cap", "stale_reason", "status"],
				as_dict=True,
			)
			self.assertEqual(row.strength_band, "Medium")
			# durable cap (shared contract): mining/drift band writes clamp to it
			self.assertEqual(row.flag_band_cap, "Medium")
			self.assertIn("flagged by 2 users", row.stale_reason)
			self.assertEqual(row.status, "Approved")  # High->Medium: still served
			subjects = frappe.get_all("Notification Log", filters={"for_user": self.non_sm}, pluck="subject")
			self.assertTrue(any("flag" in (s or "").lower() for s in subjects))
		finally:
			frappe.db.delete("Notification Log", {"for_user": self.non_sm})
			frappe.db.commit()

	def test_flag_quorum_on_low_band_stales_the_pattern(self):
		# The Low rung has nowhere left to demote, so the quorum flips the row
		# to Stale: the compiler's existing stale exclusion stops serving it on
		# the next Apply (flag demotion must affect what chat actually says).
		name = _mk("fl9", status="Active", strength_band="Low")
		with mock.patch("jarvis.learning.lifecycle.notify_system_managers") as notify:
			learned_api.flag_learned_default(name, note="wrong")
			with _as(self.non_sm):
				out = learned_api.flag_learned_default(name, note="also wrong")
		self.assertTrue(out["demoted"])
		self.assertEqual(out["status"], "Stale")
		row = frappe.db.get_value(
			JLP,
			name,
			["status", "strength_band", "flag_band_cap", "stale_reason"],
			as_dict=True,
		)
		self.assertEqual(row.status, "Stale")
		self.assertEqual(row.strength_band, "Low")
		self.assertEqual(row.flag_band_cap, "Low")
		self.assertIn("flagged by 2 users", row.stale_reason)
		notify.assert_called_once()
		# Stale rows cannot be re-flagged: the loop terminates here...
		with self.assertRaises(frappe.ValidationError):
			learned_api.flag_learned_default(name)
		# ...and an SM re-approve restores it, clearing the cap + flag reason.
		learned_api.approve_learned_pattern(name)
		row = frappe.db.get_value(JLP, name, ["status", "flag_band_cap", "stale_reason"], as_dict=True)
		self.assertEqual(row.status, "Approved")
		self.assertFalse(row.flag_band_cap)
		self.assertFalse(row.stale_reason)

	def test_flag_cooldown_blocks_redemote_until_expiry(self):
		# After the quorum demotes High -> Medium, an immediate re-flag by one
		# of the same users is inside the per-user cooldown: no second rung, no
		# second SM notification. Once that user's previous flag ages out, a
		# re-flag re-evaluates the quorum (Medium -> Low -> Stale).
		name = _mk("fl11", status="Active", strength_band="High")
		with mock.patch("jarvis.learning.lifecycle.notify_system_managers") as notify:
			learned_api.flag_learned_default(name, note="admin flag")
			with _as(self.non_sm):
				out = learned_api.flag_learned_default(name, note="user flag")
			self.assertTrue(out["demoted"])
			self.assertEqual(out["strength_band"], "Medium")
			self.assertEqual(notify.call_count, 1)

			# immediate re-flag by the same user: cooldown -> no ratchet
			with _as(self.non_sm):
				out = learned_api.flag_learned_default(name, note="again")
			self.assertFalse(out["demoted"])
			self.assertEqual(out["strength_band"], "Medium")
			self.assertEqual(out["status"], "Active")
			self.assertEqual(notify.call_count, 1)

			# age the user's entry past the cooldown -> the re-flag demotes again
			entries = json.loads(frappe.db.get_value(JLP, name, "counter_evidence"))
			for e in entries:
				if e["user"] == self.non_sm:
					e["ts"] = str(add_days(now_datetime(), -2))
			frappe.db.set_value(
				JLP,
				name,
				{"counter_evidence": json.dumps(entries)},
				update_modified=False,
			)
			frappe.db.commit()
			with _as(self.non_sm):
				out = learned_api.flag_learned_default(name, note="still wrong")
			self.assertTrue(out["demoted"])
			self.assertEqual(out["strength_band"], "Low")
			self.assertEqual(out["status"], "Stale")
			self.assertEqual(notify.call_count, 2)

	def test_flag_note_sanitized_and_truncated(self):
		name = _mk("fl10", status="Active")
		learned_api.flag_learned_default(name, note="<b>bold</b> " + "x" * 400)
		entries = json.loads(frappe.db.get_value(JLP, name, "counter_evidence"))
		self.assertNotIn("<b>", entries[0]["note"])
		self.assertLessEqual(len(entries[0]["note"]), 280)

	# ------------------------------------------------------------------ #
	# stale board surface (drift re-validation, plan 6.5)
	# ------------------------------------------------------------------ #
	def test_list_exposes_stale_and_flag_fields(self):
		name = _mk(
			"st1",
			status="Stale",
			surfaced=1,
			flags_count=2,
			stale_reason="confidence dropped 96% -> 71% (window 18 months)",
		)
		# db.set_value: the pointer references a managed skill row that need not
		# exist in this fixture (Link validation is an insert-time concern).
		frappe.db.set_value(JLP, name, {"materialized_skill": "learned-selling"}, update_modified=False)
		frappe.db.commit()

		out = learned_api.list_learned_patterns_page(status="Stale", surfaced="all")
		row = next(r for r in out["rows"] if r["name"] == name)
		self.assertIn("confidence dropped", row["stale_reason"])
		self.assertEqual(row["flags_count"], 2)
		self.assertIn("last_validated_at", row)
		self.assertEqual(row["materialized_skill"], "learned-selling")
		# Stale-still-compiled surfaces as "will be removed on Apply": its own
		# counter AND the pending-apply bar count.
		self.assertGreaterEqual(out["stale_pending_removal"], 1)
		self.assertGreaterEqual(out["pending_apply_count"], 1)

	def test_get_exposes_validation_and_counter_evidence(self):
		name = _mk("st2", status="Active")
		learned_api.flag_learned_default(name, note="off for these")
		out = learned_api.get_learned_pattern(name)
		self.assertEqual(out["flags_count"], 1)
		self.assertEqual(out["counter_evidence"][0]["note"], "off for these")
		self.assertEqual(out["counter_evidence"][0]["user"], "Administrator")
		self.assertIn("last_validated_at", out)


class TestLearnedSkillsPush(unittest.TestCase):
	"""The dedicated learned-skills push chain (Phase-2 namespace, plan 13 Q5):
	sync-status lifecycle (pending -> terminal ok/failed) with a mocked
	admin_client, the deduped enqueue contract, and the after-restart resync."""

	SKILL = "Jarvis Custom Skill"

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		frappe.set_user("Administrator")
		self._sync = (
			frappe.db.get_value(
				SETTINGS,
				SETTINGS,
				["learned_skills_synced_at", "learned_skills_sync_status"],
				as_dict=True,
			)
			or frappe._dict()
		)
		self._wipe_managed()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._wipe_managed()
		frappe.db.set_value(
			SETTINGS,
			SETTINGS,
			{
				"learned_skills_synced_at": self._sync.get("learned_skills_synced_at"),
				"learned_skills_sync_status": self._sync.get("learned_skills_sync_status"),
			},
			update_modified=False,
		)
		frappe.db.commit()

	def _wipe_managed(self):
		frappe.db.delete(self.SKILL, {"managed_by_learning": 1})
		frappe.db.commit()

	def _mk_managed(self, slug="learned-selling"):
		frappe.flags.jarvis_pattern_engine = True
		try:
			doc = frappe.get_doc(
				{
					"doctype": self.SKILL,
					"skill_name": slug,
					"description": "Learned selling habits for this org.",
					"instructions": "# Learned selling habits\n- test rule",
					"enabled": 1,
					"user_invocable": 0,
					"managed_by_learning": 1,
				}
			)
			doc.flags.ignore_permissions = True
			doc.insert(ignore_permissions=True)
		finally:
			frappe.flags.jarvis_pattern_engine = False
		frappe.db.commit()
		return doc.name

	# ------------------------------------------------------------------ #
	# enqueue contract
	# ------------------------------------------------------------------ #
	def test_enqueue_marks_pending_and_dedupes(self):
		from jarvis.chat import learned_skills_api

		self._mk_managed()
		with mock.patch("frappe.enqueue") as enq:
			out = learned_skills_api.enqueue_learned_skills_push()
		self.assertTrue(out["ok"])
		self.assertEqual(out["learned_skills_sync_status"], "pending: applying learned skills")
		self.assertEqual(out["count"], 1)
		st = learned_skills_api.get_learned_skills_sync_status()
		self.assertTrue(st["pending"])
		self.assertEqual(
			enq.call_args.args[0],
			"jarvis.chat.learned_skills_api._enqueued_push_learned_skills",
		)
		kwargs = enq.call_args.kwargs
		self.assertEqual(kwargs["job_id"], "jarvis_learned_skills_push")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["timeout"], 180)

	# ------------------------------------------------------------------ #
	# worker lifecycle: pending -> terminal ok / failed (mocked admin)
	# ------------------------------------------------------------------ #
	def test_push_lifecycle_terminal_ok(self):
		from jarvis.chat import learned_skills_api

		self._mk_managed()
		# in_test -> the deduped worker runs INLINE inside enqueue.
		with mock.patch("jarvis.admin_client.post_push_learned_skills", return_value={}) as post:
			learned_skills_api.enqueue_learned_skills_push()
		st = learned_skills_api.get_learned_skills_sync_status()
		self.assertFalse(st["pending"])
		self.assertEqual(st["last_sync_status"], "ok (1 installed via admin)")
		self.assertTrue(st["last_sync_at"])
		post.assert_called_once()
		# the wire body key + item shape match the admin/fleet contract.
		payload = post.call_args.kwargs["learned_skills"]
		self.assertEqual(len(payload), 1)
		self.assertEqual(payload[0]["slug"], "learned-selling")
		self.assertEqual(sorted(payload[0].keys()), ["body", "description", "slug"])

	def test_push_lifecycle_terminal_failed(self):
		from jarvis.chat import learned_skills_api
		from jarvis.exceptions import AdminUnreachableError

		self._mk_managed()
		with mock.patch(
			"jarvis.admin_client.post_push_learned_skills",
			side_effect=AdminUnreachableError("boom"),
		):
			learned_skills_api.enqueue_learned_skills_push()
		st = learned_skills_api.get_learned_skills_sync_status()
		self.assertFalse(st["pending"])  # try/finally backstop: never wedged pending
		self.assertTrue(st["last_sync_status"].startswith("failed: admin unreachable"))

	def test_apply_status_proxies_learned_sync(self):
		# get_learned_apply_status now reads the LEARNED pair, not the custom one.
		frappe.db.set_value(
			SETTINGS,
			SETTINGS,
			{"learned_skills_sync_status": "pending: applying learned skills"},
			update_modified=False,
		)
		frappe.db.commit()
		out = learned_api.get_learned_apply_status()
		self.assertTrue(out["pending"])
		self.assertEqual(out["last_sync_status"], "pending: applying learned skills")
		# ...and the un-approve gate keys off the same pair.
		self.assertTrue(learned_api._apply_pending())
		# While the push is pending, the cutover's custom reconcile pair rides
		# the envelope so a failed stale-dir cleanup is visible on the board.
		self.assertIsInstance(out["custom_sync"], dict)
		self.assertIn("last_sync_status", out["custom_sync"])

	def test_apply_status_custom_sync_recency_window(self):
		# A recent terminal learned sync can belong to the cutover Apply: the
		# custom pair is included so its reconcile outcome is observable.
		frappe.db.set_value(
			SETTINGS,
			SETTINGS,
			{
				"learned_skills_sync_status": "ok (1 installed via admin)",
				"learned_skills_synced_at": now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()
		out = learned_api.get_learned_apply_status()
		self.assertFalse(out["pending"])
		self.assertIsInstance(out["custom_sync"], dict)
		self.assertIn("pending", out["custom_sync"])

		# An old sync cannot: the envelope stays lean (custom_sync None).
		frappe.db.set_value(
			SETTINGS,
			SETTINGS,
			{"learned_skills_synced_at": add_days(now_datetime(), -3)},
			update_modified=False,
		)
		frappe.db.commit()
		out = learned_api.get_learned_apply_status()
		self.assertIsNone(out["custom_sync"])

		# Never-pushed (empty pair) -> None as well.
		frappe.db.set_value(
			SETTINGS,
			SETTINGS,
			{"learned_skills_sync_status": "", "learned_skills_synced_at": None},
			update_modified=False,
		)
		frappe.db.commit()
		out = learned_api.get_learned_apply_status()
		self.assertIsNone(out["custom_sync"])

	# ------------------------------------------------------------------ #
	# after-restart resync (jarvis_settings.py)
	# ------------------------------------------------------------------ #
	def test_resync_after_restart_repushes_learned(self):
		settings = frappe.get_doc(SETTINGS)
		# no managed rows -> no-op (no extra restart for customers without them)
		with mock.patch("frappe.enqueue") as enq:
			settings._resync_learned_skills_after_restart()
		enq.assert_not_called()
		# with a managed row -> pending status + the deduped learned job
		self._mk_managed()
		with mock.patch("frappe.enqueue") as enq:
			settings._resync_learned_skills_after_restart()
		enq.assert_called_once()
		self.assertEqual(
			enq.call_args.args[0],
			"jarvis.chat.learned_skills_api._enqueued_push_learned_skills",
		)
		self.assertEqual(enq.call_args.kwargs["job_id"], "jarvis_learned_skills_push")
		from jarvis.chat.learned_skills_api import get_learned_skills_sync_status

		self.assertTrue(get_learned_skills_sync_status()["pending"])


class TestLearnedDecidedView(unittest.TestCase):
	"""``view="decided"`` (Skills-page IA v2): the Review tab's Decided log.
	Every human-touched terminal/parked status rides it (Approved / Active /
	Rejected - incl. the Acknowledged and applied-to-skill notes - / Superseded /
	Archived / Snoozed), ordered reviewed_at DESC with nulls last (``sort=
	"oldest"`` flips it), ignoring the surfaced filter, with a decided-only
	``disposition`` facet; the default view stays byte-identical.

	Every list assertion scopes with ``search=`` on the fixture statements:
	``_wipe()`` only clears ``la-test-*`` rows, deliberately tolerating
	pre-existing decided rows on a dev site."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	def _names(self, out) -> list[str]:
		return [r["name"] for r in out["rows"]]

	def test_decided_includes_every_human_disposition(self):
		now = now_datetime()
		approved = _mk("dv1", status="Approved", reviewed_at=add_days(now, -1))
		active = _mk("dv2", status="Active", reviewed_at=add_days(now, -2))
		rejected = _mk(
			"dv3",
			status="Rejected",
			review_note="not a real habit",
			reviewed_at=add_days(now, -3),
		)
		acked = _mk(
			"dv4",
			status="Rejected",
			review_note=learned_api.ACK_NOTE,
			effective_sensitivity="B",
			reviewed_at=add_days(now, -4),
		)
		applied = _mk(
			"dv5",
			status="Rejected",
			review_note=learned_api.APPLIED_NOTE_PREFIX + "shipping-defaults",
			effective_sensitivity="B",
			reviewed_at=add_days(now, -5),
		)
		snoozed = _mk("dv6", status="Snoozed", reviewed_at=add_days(now, -6))
		superseded = _mk("dv7", status="Superseded", reviewed_at=add_days(now, -7))
		proposed = _mk("dv8")  # pending, not decided
		stale = _mk("dv9", status="Stale")  # machine-parked, not a decision

		out = learned_api.list_learned_patterns_page(
			view="decided", search="Statement for dv", page_length=100
		)
		names = self._names(out)
		for n in (approved, active, rejected, acked, applied, snoozed, superseded):
			self.assertIn(n, names)
		self.assertNotIn(proposed, names)
		self.assertNotIn(stale, names)

		# The disposition badges have what they need on the CARD: review_note
		# distinguishes Acknowledged / applied-to-skill from a plain Reject, and
		# reviewed_by/reviewed_at feed the who/when line.
		by_name = {r["name"]: r for r in out["rows"]}
		self.assertEqual(by_name[acked]["review_note"], learned_api.ACK_NOTE)
		self.assertTrue(by_name[applied]["review_note"].startswith(learned_api.APPLIED_NOTE_PREFIX))
		self.assertIn("reviewed_by", by_name[approved])
		self.assertTrue(by_name[approved]["reviewed_at"])

	def test_decided_orders_reviewed_at_desc_nulls_last(self):
		now = now_datetime()
		newest = _mk("do1", status="Approved", reviewed_at=now)
		older = _mk("do2", status="Rejected", review_note="x", reviewed_at=add_days(now, -5))
		oldest = _mk("do3", status="Snoozed", reviewed_at=add_days(now, -9))
		undated = _mk("do4", status="Archived")  # no reviewed_at -> sorts last

		# Scope to this fixture's rows via search (which keeps working under the
		# view) so pre-existing decided rows on the site cannot interleave.
		out = learned_api.list_learned_patterns_page(
			view="decided", search="Statement for do", page_length=100
		)
		self.assertEqual(self._names(out), [newest, older, oldest, undated])

	def test_decided_ignores_surfaced_filter(self):
		unsurfaced = _mk("ds1", status="Approved", surfaced=0, reviewed_at=now_datetime())
		out = learned_api.list_learned_patterns_page(view="decided", surfaced=1, page_length=100)
		self.assertIn(unsurfaced, self._names(out))

	def test_decided_overrides_status_filter(self):
		proposed = _mk("dov1")  # would match status="Proposed"
		decided = _mk("dov2", status="Approved", reviewed_at=now_datetime())
		out = learned_api.list_learned_patterns_page(view="decided", status="Proposed", page_length=100)
		names = self._names(out)
		self.assertIn(decided, names)
		self.assertNotIn(proposed, names)

	def test_default_view_unchanged(self):
		proposed = _mk("dd1")
		decided = _mk("dd2", status="Approved", surfaced=1, reviewed_at=now_datetime())
		# No view param; search-scoped so a dev site's own surfaced Proposed
		# rows cannot push dd1 off the default 20-row first page.
		out = learned_api.list_learned_patterns_page(search="Statement for dd")
		names = self._names(out)
		self.assertIn(proposed, names)
		self.assertNotIn(decided, names)
		for key in (
			"rows",
			"total",
			"has_more",
			"start",
			"page_length",
			"facets",
			"queued_count",
			"pending_apply_count",
			"review_activity",
		):
			self.assertIn(key, out)
		# reviewed_at now rides the card in EVERY view (string-normalized, empty
		# when undecided) without displacing the existing fields.
		row = next(r for r in out["rows"] if r["name"] == proposed)
		self.assertIn("reviewed_at", row)
		self.assertEqual(row["reviewed_at"], "")
		for present in ("pattern_statement", "strength_band", "domain", "status"):
			self.assertIn(present, row)

	def test_decided_disposition_filters(self):
		now = now_datetime()
		approved = _mk("dp1", status="Approved", reviewed_at=add_days(now, -1))
		active = _mk("dp2", status="Active", reviewed_at=add_days(now, -2))
		rejected = _mk(
			"dp3",
			status="Rejected",
			review_note="not a real habit",
			reviewed_at=add_days(now, -3),
		)
		acked = _mk(
			"dp4",
			status="Rejected",
			review_note=learned_api.ACK_NOTE,
			effective_sensitivity="B",
			reviewed_at=add_days(now, -4),
		)
		applied = _mk(
			"dp5",
			status="Rejected",
			review_note=learned_api.APPLIED_NOTE_PREFIX + "shipping-defaults",
			effective_sensitivity="B",
			reviewed_at=add_days(now, -5),
		)
		snoozed = _mk("dp6", status="Snoozed", reviewed_at=add_days(now, -6))

		def names_for(disposition):
			return set(
				self._names(
					learned_api.list_learned_patterns_page(
						view="decided",
						disposition=disposition,
						search="Statement for dp",
						page_length=100,
					)
				)
			)

		# Each disposition returns EXACTLY its rows (set-equal, so a leak from
		# any sibling disposition fails, not just a missing row).
		self.assertEqual(names_for("approved"), {approved, active})
		self.assertEqual(names_for("applied"), {applied})
		self.assertEqual(names_for("acknowledged"), {acked})
		# "rejected" = a plain human Reject: the Acknowledged / applied-to-skill
		# terminal notes store as Rejected but must NOT ride this facet.
		self.assertEqual(names_for("rejected"), {rejected})
		self.assertEqual(names_for("snoozed"), {snoozed})

	def test_decided_sort_oldest_flips_ordering(self):
		now = now_datetime()
		newest = _mk("dso1", status="Approved", reviewed_at=now)
		older = _mk("dso2", status="Rejected", review_note="x", reviewed_at=add_days(now, -5))
		oldest = _mk("dso3", status="Snoozed", reviewed_at=add_days(now, -9))
		undated = _mk("dso4", status="Archived")  # no reviewed_at

		out = learned_api.list_learned_patterns_page(
			view="decided", sort="oldest", search="Statement for dso", page_length=100
		)
		# reviewed_at ASC, but the undated row STILL sorts last (nulls last in
		# both directions - it has no date to be "oldest" by).
		self.assertEqual(self._names(out), [oldest, older, newest, undated])

	def test_invalid_view_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			learned_api.list_learned_patterns_page(view="bogus")

	def test_decided_invalid_disposition_and_sort_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			learned_api.list_learned_patterns_page(view="decided", disposition="bogus")
		with self.assertRaises(frappe.ValidationError):
			learned_api.list_learned_patterns_page(view="decided", sort="bogus")

	def test_disposition_outside_decided_view_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			learned_api.list_learned_patterns_page(disposition="approved")


if __name__ == "__main__":
	unittest.main()
