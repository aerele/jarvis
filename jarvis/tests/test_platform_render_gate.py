"""Regression tests for the R5-J1 two-condition "no exceptions" render gate
(``jarvis/chat/agent_runs.py``).

``run_state`` is a COVERAGE verdict: a completed close that surfaced exceptions is
still ``evaluated_clean`` (``coverage_reasons.RUN_STATES``). The absence-of-exceptions
sentence ("No exceptions were found") is a stronger, separate claim and may render
ONLY when BOTH hold:

  1. ``result_state == evaluated_clean`` (every required check evaluated), AND
  2. the run persisted ZERO findings.

These tests prove that a run with FULL coverage but findings PRESENT resolves
``evaluated_clean`` (legal — close-auditor's golden shape) yet its truthful fallback
dashboard never emits the clean sentence — closing the false-clean render
R4-P0-03/R5 targeted — while the clean coverage HEADING (the coverage verdict) still
renders honestly alongside the findings.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_render_gate
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_runs
from jarvis.chat import coverage_reasons as cr

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
PROVENANCE = "Jarvis Agent Provenance Event"
DASHBOARD = "Jarvis Dashboard"

SLUG = "platform-render-gate-test-agent"
TOK_A = "tok_rg_a"
TOK_B = "tok_rg_b"

CLEAN_SENTENCE = "No exceptions were found"
CLEAN_HEADING = "Evaluated — clean coverage"


def _mk_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	return email


def _mk_listing() -> str:
	if not frappe.db.exists(LISTING, SLUG):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": SLUG,
				"title": "Platform Render-Gate Test Agent",
				# DECLARED required manifest: two tokens (the authoritative bar).
				"rule_tokens": json.dumps([TOK_A, TOK_B]),
				# Empty required set: the installation preflight then needs no run-as read
				# grant. record_delegate_run persists findings as-passed (the tool, not
				# this path, gates ref doctypes), so a Company-ref finding still lands.
				"doctypes_required": json.dumps([]),
			}
		).insert(ignore_permissions=True)
	return SLUG


def _mk_installation(owner: str) -> object:
	name = frappe.db.get_value(INSTALLATION, {"agent": SLUG, "owner": owner}, "name")
	if name:
		return frappe.get_doc(INSTALLATION, name)
	doc = frappe.get_doc({"doctype": INSTALLATION, "agent": SLUG, "run_as_user": owner, "reviewer": owner})
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(INSTALLATION, doc.name, "owner", owner, update_modified=False)
	# Promote to live so the fallback dashboard issues an outward attestation (shadow
	# would suppress the clean sentence for a different, PP-4 reason — we test the
	# R5-J1 findings-count gate, not the shadow gate, here).
	frappe.db.set_value(INSTALLATION, doc.name, "activation_state", "live", update_modified=False)
	return frappe.get_doc(INSTALLATION, doc.name)


def _mk_run(owner: str) -> object:
	doc = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": SLUG,
			"trigger": "manual",
			"status": "running",
			"started_at": frappe.utils.now(),
			"session_key": frappe.generate_hash(length=24),
		}
	)
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc


def _wipe_residue() -> None:
	"""Slug-scoped teardown (mirrors test_platform_false_clean._wipe_residue): remove
	every Run / Finding / Provenance / Dashboard row this module persisted under its
	test-agent slug. Raw delete + commit (also bypasses the append-only Provenance
	on_trash guard, legitimate for this module's OWN test residue)."""
	dashboards = [
		d
		for d in frappe.get_all(RUN, filters={"agent": SLUG}, pluck="dashboard", ignore_permissions=True)
		if d
	]
	frappe.db.delete(RUN, {"agent": SLUG})
	frappe.db.delete(FINDING, {"agent": SLUG})
	frappe.db.delete(PROVENANCE, {"agent": SLUG})
	if dashboards:
		frappe.db.delete(DASHBOARD, {"name": ["in", dashboards]})
	frappe.db.commit()


# --------------------------------------------------------------------------- #
# Unit — the two-condition predicate itself
# --------------------------------------------------------------------------- #
class TestCleanAttestationPredicate(FrappeTestCase):
	def test_clean_and_zero_findings_allows_sentence(self):
		self.assertTrue(agent_runs._clean_attestation_allowed("evaluated_clean", 0, shadow=False))

	def test_clean_but_findings_present_suppresses_sentence(self):
		# THE R5-J1 gate: full coverage (evaluated_clean) but findings > 0 => no claim.
		self.assertFalse(agent_runs._clean_attestation_allowed("evaluated_clean", 4, shadow=False))
		self.assertFalse(agent_runs._clean_attestation_allowed("evaluated_clean", 1, shadow=False))

	def test_non_clean_state_never_allows_sentence(self):
		for state in ("partial", "not_evaluable", "failed"):
			self.assertFalse(agent_runs._clean_attestation_allowed(state, 0, shadow=False), state)

	def test_shadow_never_allows_sentence(self):
		self.assertFalse(agent_runs._clean_attestation_allowed("evaluated_clean", 0, shadow=True))


# --------------------------------------------------------------------------- #
# Unit — the render itself (the truthful fallback dashboard path)
# --------------------------------------------------------------------------- #
class TestFallbackDashboardRenderGate(FrappeTestCase):
	def _finding(self):
		return {
			"token": TOK_A,
			"ref_doctype": "Company",
			"ref_name": "Acme",
			"amount": 100.0,
			"severity": "blocker",
			"result_class": "observed_fact",
			"note": "Trial balance does not balance.",
		}

	def test_clean_state_zero_findings_renders_sentence(self):
		html = agent_runs._fallback_dashboard_html(
			"T",
			[],
			{"blocker": 0, "warning": 0, "note": 0},
			"",
			result_state="evaluated_clean",
		)
		self.assertIn(CLEAN_SENTENCE, html)
		self.assertIn(CLEAN_HEADING, html)

	def test_clean_state_with_findings_suppresses_sentence(self):
		# FULL coverage verdict (evaluated_clean) but findings PRESENT: the clean
		# coverage HEADING still renders (the coverage verdict is honest — R5-J1),
		# but the "No exceptions were found" SENTENCE must NOT.
		html = agent_runs._fallback_dashboard_html(
			"T",
			[self._finding()],
			{"blocker": 1, "warning": 0, "note": 0},
			"",
			result_state="evaluated_clean",
		)
		self.assertNotIn(CLEAN_SENTENCE, html)
		self.assertIn(CLEAN_HEADING, html)  # coverage verdict heading is legal

	def test_partial_zero_findings_never_clean_sentence(self):
		html = agent_runs._fallback_dashboard_html(
			"T",
			[],
			{"blocker": 0, "warning": 0, "note": 0},
			"",
			result_state="partial",
		)
		self.assertNotIn(CLEAN_SENTENCE, html)


# --------------------------------------------------------------------------- #
# Integration — through record_delegate_run: full coverage + findings present
# resolves evaluated_clean, but the persisted dashboard omits the clean sentence.
# --------------------------------------------------------------------------- #
class TestRenderGateEndToEnd(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("rg-owner@example.com")
		_mk_listing()
		cls.company = frappe.db.get_value("Company", {}, "name")
		cls.inst = _mk_installation(cls.owner)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_residue()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def _dashboard_html(self, run) -> str:
		run.reload()
		name = frappe.db.get_value(RUN, run.name, "dashboard")
		self.assertTrue(name, "run produced no dashboard")
		return frappe.db.get_value(DASHBOARD, name, "html") or ""

	def test_full_coverage_with_findings_is_clean_verdict_but_no_sentence(self):
		run = _mk_run(self.owner)
		finding = {
			"token": TOK_A,
			"ref_doctype": "Company",
			"ref_name": self.company,
			"amount": 100.0,
			"severity": "blocker",
			"result_class": "observed_fact",
			"note": "Trial balance does not balance.",
		}
		agent_runs.record_delegate_run(
			run,
			self.inst,
			[finding],
			coverage={TOK_A: "evaluated", TOK_B: "evaluated"},
			scope={"company": self.company},
		)
		run.reload()
		# COVERAGE verdict is clean (every declared check evaluated) — legal per R5-J1.
		self.assertEqual(run.result_state, "evaluated_clean")
		# A finding persisted.
		persisted = frappe.get_all(FINDING, filters={"run": run.name}, ignore_permissions=True)
		self.assertEqual(len(persisted), 1)
		# The truthful fallback dashboard shows the findings table, NEVER the clean
		# "No exceptions were found" sentence (condition 2 — zero findings — fails).
		html = self._dashboard_html(run)
		self.assertNotIn(CLEAN_SENTENCE, html)

	def test_full_coverage_zero_findings_is_clean_and_renders_sentence(self):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run,
			self.inst,
			[],
			coverage={TOK_A: "evaluated", TOK_B: "evaluated"},
			scope={"company": self.company},
		)
		run.reload()
		self.assertEqual(run.result_state, "evaluated_clean")
		html = self._dashboard_html(run)
		# BOTH conditions met (clean verdict + zero findings) => sentence renders.
		self.assertIn(CLEAN_SENTENCE, html)
