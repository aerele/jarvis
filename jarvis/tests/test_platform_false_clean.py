"""Regression tests for the three coverage-truthfulness defects fixed in
``jarvis/chat/agent_runs.py`` (PP-2 / PP-3):

  * The PP-2 false-clean gate — the required-check set is the agent's DECLARED
    rule-token manifest (authoritative), NOT the writeback-supplied coverage keys.
    A delegate under-reporting (empty / narrow / fabricated) coverage can never
    earn ``evaluated_clean``.
  * The PP-3 placeholder leak — coverage-gap remediation text substitutes the
    typed manifest ``detail`` (or a neutral noun) into the ``{app}`` / ``{setting}``
    placeholder, so a literal brace-string never reaches the customer.
  * The PP-3 coverage_note — the amber "Partial scan" banner string carries the
    customer remediation sentence, not the raw internal reason-code enum slug.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_false_clean
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_runs

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
PROVENANCE = "Jarvis Agent Provenance Event"
DASHBOARD = "Jarvis Dashboard"

SLUG = "platform-false-clean-test-agent"
TOK_A = "tok_fc_a"
TOK_B = "tok_fc_b"


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
				"title": "Platform False-Clean Test Agent",
				# DECLARED required manifest: TWO tokens (the authoritative bar).
				"rule_tokens": json.dumps([TOK_A, TOK_B]),
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
	"""Slug-scoped teardown: remove every Run / Finding / Provenance / Dashboard row
	this module persisted under its test-agent slug, so run-persistence residue never
	accrues on the shared site. Belt-and-suspenders alongside the ``frappe.flags.in_test``
	commit gate. Mirrors ``test_platform_activation._wipe``: raw ``frappe.db.delete`` +
	commit (which also bypasses the append-only Provenance ``on_trash`` guard, legitimate
	for deleting this module's OWN test residue)."""
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
# PP-3 — the placeholder is filled, never leaked (unit, via _coverage_summary)
# --------------------------------------------------------------------------- #
class TestPP3PlaceholderNeverLeaks(FrappeTestCase):
	def test_detail_fills_placeholder(self):
		_, notes = agent_runs._coverage_summary(
			{
				TOK_A: {
					"state": "not_evaluable",
					"reason_code": "app_absent_or_ineligible",
					"detail": "India Compliance",
				}
			}
		)
		rem = notes[0]["remediation"]
		self.assertIn("India Compliance", rem)
		self.assertNotIn("{app}", rem)
		self.assertNotIn("{", rem)

	def test_missing_detail_uses_neutral_noun(self):
		_, notes = agent_runs._coverage_summary(
			{TOK_A: {"state": "not_evaluable", "reason_code": "configuration_missing"}}
		)
		rem = notes[0]["remediation"]
		self.assertNotIn("{setting}", rem)
		self.assertIn("the required setting", rem)

	def test_no_placeholder_code_passes_through(self):
		_, notes = agent_runs._coverage_summary(
			{TOK_A: {"state": "not_evaluable", "reason_code": "rule_expired"}}
		)
		# ``rule_expired`` has no placeholder — text is unchanged and brace-free.
		self.assertNotIn("{", notes[0]["remediation"])
		self.assertIn("pending review", notes[0]["remediation"])


# --------------------------------------------------------------------------- #
# PP-2 — the required bar is the DECLARED manifest, not the coverage keys
# --------------------------------------------------------------------------- #
class TestPP2FalseCleanGate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("fc-owner@example.com")
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

	def _record(self, coverage, findings=None):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run, self.inst, findings or [], coverage=coverage, scope={"company": self.company}
		)
		run.reload()
		return run

	def test_full_declared_coverage_is_clean(self):
		run = self._record({TOK_A: "evaluated", TOK_B: "evaluated"})
		self.assertEqual(run.result_state, "evaluated_clean")
		# The authoritative required set is the DECLARED manifest, not the coverage keys.
		blob = json.loads(run.coverage_json)
		self.assertEqual(blob["required_tokens"], sorted([TOK_A, TOK_B]))

	def test_empty_manifest_is_not_evaluable_never_clean(self):
		# The exact false-clean PP-2 closes: an evaluator emits NO coverage + zero
		# findings — it must not earn ``evaluated_clean``.
		run = self._record({})
		self.assertNotEqual(run.result_state, "evaluated_clean")
		self.assertEqual(run.result_state, "not_evaluable")

	def test_narrow_manifest_is_partial_never_clean(self):
		# Only ONE of the two declared checks came back evaluated; zero findings.
		run = self._record({TOK_A: "evaluated"})
		self.assertNotEqual(run.result_state, "evaluated_clean")
		self.assertEqual(run.result_state, "partial")

	def test_fabricated_coverage_key_cannot_define_its_own_bar(self):
		# An evaluator reports a token OUTSIDE the declared manifest as evaluated. It
		# does not cover any declared required check → not_evaluable, never clean.
		run = self._record({"tok_not_declared": "evaluated"})
		self.assertNotEqual(run.result_state, "evaluated_clean")
		self.assertEqual(run.result_state, "not_evaluable")
		blob = json.loads(run.coverage_json)
		self.assertEqual(blob["required_tokens"], sorted([TOK_A, TOK_B]))


# --------------------------------------------------------------------------- #
# PP-3 — the coverage_note banner carries remediation text, not the enum slug
# --------------------------------------------------------------------------- #
class TestPP3CoverageNoteIsCustomerText(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("fc-owner@example.com")
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

	def test_coverage_note_shows_remediation_not_reason_code(self):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run,
			self.inst,
			[],
			coverage={
				TOK_A: {"state": "not_evaluable", "reason_code": "configuration_missing"},
				TOK_B: "evaluated",
			},
			scope={"company": self.company},
		)
		run.reload()
		note = run.coverage_note or ""
		# the customer remediation sentence, NOT the internal enum slug
		self.assertIn("Configure", note)
		self.assertNotIn("configuration_missing", note)
