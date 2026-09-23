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

from jarvis.chat import agent_runs, agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
PROVENANCE = "Jarvis Agent Provenance Event"
DASHBOARD = "Jarvis Dashboard"
ACTIVITY = "Jarvis Agent Activity"

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


def _mk_installation(owner: str, slug: str = SLUG) -> object:
	name = frappe.db.get_value(INSTALLATION, {"agent": slug, "owner": owner}, "name")
	if name:
		return frappe.get_doc(INSTALLATION, name)
	doc = frappe.get_doc({"doctype": INSTALLATION, "agent": slug, "run_as_user": owner, "reviewer": owner})
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(INSTALLATION, doc.name, "owner", owner, update_modified=False)
	return frappe.get_doc(INSTALLATION, doc.name)


def _mk_run(owner: str, slug: str = SLUG) -> object:
	doc = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": slug,
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


def _wipe_residue(slug: str = SLUG) -> None:
	"""Slug-scoped teardown: remove every Run / Finding / Provenance / Dashboard row
	this module persisted under its test-agent slug, so run-persistence residue never
	accrues on the shared site. Belt-and-suspenders alongside the ``frappe.flags.in_test``
	commit gate. Mirrors ``test_platform_activation._wipe``: raw ``frappe.db.delete`` +
	commit (which also bypasses the append-only Provenance ``on_trash`` guard, legitimate
	for deleting this module's OWN test residue)."""
	dashboards = [
		d
		for d in frappe.get_all(RUN, filters={"agent": slug}, pluck="dashboard", ignore_permissions=True)
		if d
	]
	frappe.db.delete(RUN, {"agent": slug})
	frappe.db.delete(FINDING, {"agent": slug})
	frappe.db.delete(PROVENANCE, {"agent": slug})
	frappe.db.delete(ACTIVITY, {"agent": slug})
	if dashboards:
		frappe.db.delete(
			"Notification Log", {"document_type": DASHBOARD, "document_name": ["in", dashboards]}
		)
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
# Data-gap runs read status ``completed`` with the coverage warning still shown.
# The lifecycle ``status`` and the coverage ``result_state`` are separate axes: a run
# that finished executing but only lacks INPUT DATA reads ``completed`` while its verdict
# stays non-clean (so the false-clean gate is untouched). Execution problems and
# attention-needed coverage gaps (permanent / permission / truncation) stay ``partial``.
# --------------------------------------------------------------------------- #
def _nev(code):
	return {"state": "not_evaluable", "reason_code": code}


class TestDataGapCompletes(FrappeTestCase):
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

	def _record(self, coverage, findings=None, **kw):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run, self.inst, findings or [], coverage=coverage, scope={"company": self.company}, **kw
		)
		run.reload()
		return run

	def _activity_action(self, run):
		return frappe.db.get_value(ACTIVITY, {"run": run.name}, "action")

	def _bell_subject(self, run):
		dash = frappe.db.get_value(RUN, run.name, "dashboard")
		if not dash:
			return None
		return frappe.db.get_value(
			"Notification Log", {"document_type": DASHBOARD, "document_name": dash}, "subject"
		)

	# ---- data gaps only -> completed, verdict stays non-clean ------------- #
	def test_data_gap_partial_verdict_reads_completed(self):
		# one required check evaluated, the other blocked by stale input data.
		run = self._record({TOK_A: "evaluated", TOK_B: _nev("source_stale")})
		self.assertEqual(run.status, "completed")
		self.assertEqual(run.result_state, "partial")  # verdict non-clean
		self.assertTrue(run.coverage_note)  # the warning still fires
		# F1: the activity feed keeps the amber "with issues" action, not a lone green check.
		self.assertEqual(self._activity_action(run), "run_partial")
		# the owner bell must not read identical to a clean run's bell.
		self.assertIn("completed with issues", self._bell_subject(run) or "")

	def test_all_data_gap_not_evaluable_verdict_reads_completed(self):
		run = self._record({TOK_A: _nev("source_stale"), TOK_B: _nev("configuration_missing")})
		self.assertEqual(run.status, "completed")
		self.assertEqual(run.result_state, "not_evaluable")
		self.assertTrue(run.coverage_note)

	# ---- attention-needed coverage gaps stay partial --------------------- #
	def test_permission_slice_stays_partial(self):
		# retryable, but a permissions problem -> needs attention, not a data gap.
		run = self._record({TOK_A: "evaluated", TOK_B: _nev("permission_slice")})
		self.assertEqual(run.status, "partial")

	def test_permanent_reason_stays_partial(self):
		run = self._record({TOK_A: "evaluated", TOK_B: _nev("rule_expired")})
		self.assertEqual(run.status, "partial")

	def test_mixed_data_gap_and_permanent_stays_partial(self):
		# any non-data-gap note anywhere -> the whole run stays partial.
		run = self._record({TOK_A: _nev("source_stale"), TOK_B: _nev("rule_expired")})
		self.assertEqual(run.status, "partial")

	def test_unknown_reason_code_stays_partial(self):
		# coerced to unsupported_customisation (data_gap False) -> fail-safe partial.
		run = self._record({TOK_A: "evaluated", TOK_B: _nev("some novel gibberish")})
		self.assertEqual(run.status, "partial")

	# ---- execution problems dominate (data-gap note present too) --------- #
	def test_truncated_with_data_gap_stays_partial(self):
		run = self._record({TOK_A: "evaluated", TOK_B: _nev("source_stale")}, truncated=True)
		self.assertEqual(run.status, "partial")

	def test_dropped_row_with_data_gap_stays_partial(self):
		# a rejected writeback row is an evaluator-integrity failure, never a data gap.
		run = self._record(
			{TOK_A: "evaluated", TOK_B: _nev("source_stale")},
			dropped=[{"ref_doctype": "Purchase Invoice", "ref_name": "PI-DROP", "reason": "invalid"}],
		)
		self.assertEqual(run.status, "partial")

	# ---- clean run unchanged --------------------------------------------- #
	def test_clean_run_completed_and_clean(self):
		run = self._record({TOK_A: "evaluated", TOK_B: "evaluated"})
		self.assertEqual(run.status, "completed")
		self.assertEqual(run.result_state, "evaluated_clean")
		self.assertFalse(run.coverage_note)  # no warning on a truly clean run
		# a truly clean run logs the plain green "Run completed" action + a plain bell.
		self.assertEqual(self._activity_action(run), "run_completed")
		subj = self._bell_subject(run) or ""
		self.assertIn("run completed:", subj)
		self.assertNotIn("with issues", subj)

	# ---- the false-clean BLOCKER: a data-gap note on a token OUTSIDE the -- #
	# ---- declared manifest must still block evaluated_clean -------------- #
	def test_data_gap_outside_manifest_never_reads_clean(self):
		# both required tokens evaluated (required_unevaluated empty), but an extra token
		# carries a data-gap note. status=completed, yet the verdict MUST stay non-clean --
		# result_state's ``partial`` arg keys off bool(coverage_notes), never the status bool.
		run = self._record({TOK_A: "evaluated", TOK_B: "evaluated", "tok_extra": _nev("source_stale")})
		self.assertEqual(run.status, "completed")
		self.assertNotEqual(run.result_state, "evaluated_clean")
		# the outward clean attestation is refused for this run (0 findings notwithstanding).
		self.assertFalse(agent_runs._clean_attestation_allowed(run.result_state, 0, shadow=False))


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


# --------------------------------------------------------------------------- #
# Advisory findings — non-attesting worklist signals (the token split + the
# advisory-exempt clean gate). advisory_tokens is the SUBSET of rule_tokens that
# is valid for findings but NOT required-coverage, so it never gates the verdict
# and is exempt from the "no exceptions" attestation.
# --------------------------------------------------------------------------- #
SLUG_ADV = "platform-advisory-test-agent"
TOK_ADV = "tok_adv_x"


def _mk_listing_adv() -> str:
	if not frappe.db.exists(LISTING, SLUG_ADV):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": SLUG_ADV,
				"title": "Platform Advisory Test Agent",
				# rule_tokens = coverage ∪ advisory; TOK_ADV is the advisory (non-gating) subset.
				"rule_tokens": json.dumps([TOK_A, TOK_B, TOK_ADV]),
				"advisory_tokens": json.dumps([TOK_ADV]),
				"doctypes_required": json.dumps([]),
			}
		).insert(ignore_permissions=True)
	else:
		frappe.db.set_value(LISTING, SLUG_ADV, "advisory_tokens", json.dumps([TOK_ADV]))
	return SLUG_ADV


class TestAdvisoryFindings(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("adv-owner@example.com")
		frappe.get_doc("User", cls.owner).add_roles("System Manager")  # for the list_findings gate
		_mk_listing_adv()
		cls.company = frappe.db.get_value("Company", {}, "name")
		cls.inst = _mk_installation(cls.owner, slug=SLUG_ADV)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_residue(slug=SLUG_ADV)
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_mk_listing_adv()  # restore advisory_tokens (a test may mutate it)
		# isolate each test: clear this slug's open findings so a prior test's finding never
		# dedupes into (and re-homes) this test's finding by fingerprint (agent+token+ref).
		frappe.db.delete(FINDING, {"agent": SLUG_ADV})
		frappe.db.commit()

	def _record(self, coverage, findings=None):
		run = _mk_run(self.owner, slug=SLUG_ADV)
		agent_runs.record_delegate_run(
			run, self.inst, findings or [], coverage=coverage, scope={"company": self.company}
		)
		run.reload()
		return run

	def _finding(self, token, severity="note"):
		return {
			"token": token,
			"ref_doctype": "Company",
			"ref_name": self.company,
			"severity": severity,
			"result_class": "observed_fact",
			"note": f"{token} signal",
		}

	def test_coverage_excludes_advisory_tokens(self):
		cov, adv = agent_runs._listing_token_sets(SLUG_ADV)
		self.assertEqual(cov, {TOK_A, TOK_B})  # coverage = rule_tokens − advisory_tokens
		self.assertEqual(adv, {TOK_ADV})
		self.assertEqual(agent_runs._listing_rule_tokens(SLUG_ADV), {TOK_A, TOK_B})
		self.assertEqual(agent_runs._listing_advisory_tokens(SLUG_ADV), {TOK_ADV})

	def test_advisory_never_forces_partial(self):
		# both COVERAGE tokens evaluated; the advisory token is NOT required, so it never forces
		# partial by being absent from coverage. Zero findings => clean.
		run = self._record({TOK_A: "evaluated", TOK_B: "evaluated"})
		self.assertEqual(run.result_state, "evaluated_clean")
		blob = json.loads(run.coverage_json)
		self.assertEqual(blob["required_tokens"], sorted([TOK_A, TOK_B]))  # advisory excluded

	def test_advisory_finding_stamped_counted_and_exempt(self):
		run = self._record({TOK_A: "evaluated", TOK_B: "evaluated"}, findings=[self._finding(TOK_ADV)])
		# the coverage verdict is UNAFFECTED by the advisory finding:
		self.assertEqual(run.result_state, "evaluated_clean")
		self.assertEqual(run.findings_count, 1)
		self.assertEqual(run.advisory_findings_count, 1)
		fnd = frappe.get_all(FINDING, filters={"run": run.name}, fields=["rule_id", "advisory"])
		self.assertEqual(len(fnd), 1)
		self.assertEqual(fnd[0]["advisory"], 1)
		# the clean sentence is ALLOWED — non-advisory count is 0:
		self.assertTrue(
			agent_runs._clean_attestation_allowed(
				run.result_state,
				run.findings_count,
				advisory_findings_count=run.advisory_findings_count,
				shadow=False,
			)
		)

	def test_real_finding_alongside_advisory_blocks_clean(self):
		run = self._record(
			{TOK_A: "evaluated", TOK_B: "evaluated"},
			findings=[self._finding(TOK_ADV), self._finding(TOK_A, severity="warning")],
		)
		self.assertEqual(run.findings_count, 2)
		self.assertEqual(run.advisory_findings_count, 1)
		# a real (non-advisory) finding present => NO clean sentence:
		self.assertFalse(
			agent_runs._clean_attestation_allowed(
				run.result_state,
				run.findings_count,
				advisory_findings_count=run.advisory_findings_count,
				shadow=False,
			)
		)

	def test_advisory_blocker_severity_is_coerced_off_blocker_count(self):
		run = self._record(
			{TOK_A: "evaluated", TOK_B: "evaluated"},
			findings=[self._finding(TOK_ADV, severity="blocker")],
		)
		self.assertEqual(run.advisory_findings_count, 1)
		self.assertEqual(run.blocker_count, 0)  # an advisory blocker is coerced away
		sev = frappe.db.get_value(FINDING, {"run": run.name}, "severity")
		self.assertNotEqual(sev, "blocker")

	def test_stray_advisory_token_dropped_by_subset_guard(self):
		# an advisory_tokens id NOT in rule_tokens is dropped (⊆ guard), so it can never subtract
		# a coverage token it does not belong to.
		frappe.db.set_value(LISTING, SLUG_ADV, "advisory_tokens", json.dumps([TOK_ADV, "stray_z"]))
		cov, adv = agent_runs._listing_token_sets(SLUG_ADV)
		self.assertEqual(adv, {TOK_ADV})  # stray_z dropped
		self.assertEqual(cov, {TOK_A, TOK_B})

	def test_list_findings_exposes_advisory_flag_and_count(self):
		# the read API the Findings panel calls must carry the ``advisory`` flag per row + the
		# advisory_count, so an advisory-only run never reads like real exceptions in the UI.
		run = self._record(
			{TOK_A: "evaluated", TOK_B: "evaluated"},
			findings=[self._finding(TOK_ADV), self._finding(TOK_A, severity="warning")],
		)
		frappe.set_user(self.owner)
		try:
			res = agents_api.list_findings(run=run.name)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(res["total"], 2)
		self.assertEqual(res["advisory_count"], 1)  # actionable (non-advisory) = total - 1
		adv = [r for r in res["rows"] if r.get("advisory")]
		self.assertEqual(len(adv), 1)
		self.assertEqual(adv[0]["rule_id"], TOK_ADV)
