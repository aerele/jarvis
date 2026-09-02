"""Phase-B runtime tests for the platform prerequisites (PP-1/2/3/5).

These assert the WRITEBACK enforcement the schema (Phase A) only declared:

  * PP-1 — ``record_agent_run`` requires a valid ``result_class`` per finding,
    rejects the reserved ``confirmed_outcome`` from the evaluator path, rejects a
    candidate/legal row missing its class-conditional metadata; ``result_class`` is
    set-once at the controller; strong verbs are unreachable in rendered output
    unless the row is ``confirmed_outcome`` with resolving outcome provenance.
  * PP-2 — a run resolves EXACTLY one coverage-verdict ``result_state`` at
    writeback; the clean "No exceptions were found" sentence is UNREACHABLE unless
    ``evaluated_clean``; the state renders in-body (screenshot-safe).
  * PP-3 — typed reason codes round-trip through the coverage manifest, an unknown
    code is coerced (never dropped), and each code's remediation text surfaces in
    the rendered degradation section.
  * PP-5 — run launch-time facts (bundle_version / preparation_mode /
    initiating_human) are immutable once stamped; A16 auto-resolve stamps
    ``resolution_kind = auto_coverage`` + ``resolved_at``; provenance events are
    append-only.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_writeback
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_runs
from jarvis.chat import coverage_reasons as cr
from jarvis.tools.record_agent_run import _validate_findings

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
PROVENANCE = "Jarvis Agent Provenance Event"
DASHBOARD = "Jarvis Dashboard"

SLUG = "platform-writeback-test-agent"
TOKEN = "tok_pw_1"
WB_COMPANY = "Jarvis Writeback Test Co"


def _ensure_company() -> str:
	"""A bare Company row (a fresh CI site — restored from a frappe+erpnext+hrms
	dump — never ran the erpnext setup wizard, so it has NO Company). We only need a
	row that exists() so a Company-keyed finding ref verifies (Company is
	existence-checked, not read-gated), and a non-empty company string so the A16
	coverage-scoped auto-resolve is in-scope. Mirrors
	test_agent_dashboard_wire._ensure_company."""
	if not frappe.db.exists("Company", WB_COMPANY):
		c = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": WB_COMPANY,
				"abbr": "JWBTC",
				"default_currency": "INR",
				"country": "India",
			}
		)
		c.name = WB_COMPANY
		c.flags.ignore_links = True
		c.flags.ignore_mandatory = True
		c.db_insert()
		frappe.db.commit()
	return WB_COMPANY


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
				"title": "Platform Writeback Test Agent",
				"rule_tokens": json.dumps([TOKEN]),
				"doctypes_required": json.dumps([]),
			}
		).insert(ignore_permissions=True)
	return SLUG


def _mk_installation(owner: str, reviewer: str | None = None) -> object:
	"""``reviewer`` defaults to ``owner`` for the common single-identity case.
	Pass a DISTINCT ``reviewer`` to exercise the PP-4 shadow visibility_owner
	read paths (#454): a default install where reviewer == owner can never catch
	a dedupe/auto-resolve query that filters on plain ``owner`` instead of
	``_visibility_owner(inst)``, because the two values collapse to the same
	string."""
	reviewer = reviewer or owner
	name = frappe.db.get_value(INSTALLATION, {"agent": SLUG, "owner": owner}, "name")
	if name:
		return frappe.get_doc(INSTALLATION, name)
	doc = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": SLUG,
			"run_as_user": owner,
			"reviewer": reviewer,
		}
	)
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(INSTALLATION, doc.name, "owner", owner, update_modified=False)
	return frappe.get_doc(INSTALLATION, doc.name)


def _mk_run(owner: str, **extra) -> object:
	vals = {
		"doctype": RUN,
		"agent": SLUG,
		"trigger": "manual",
		"status": "running",
		"started_at": frappe.utils.now(),
		"session_key": frappe.generate_hash(length=24),
	}
	vals.update(extra)
	doc = frappe.get_doc(vals)
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc


def _finding(**over) -> dict:
	f = {
		"token": TOKEN,
		"ref_doctype": "Company",
		"ref_name": "PW-Ref",
		"amount": 100,
		"severity": "note",
		"note": "an authored finding note",
		"result_class": "observed_fact",
	}
	f.update(over)
	return f


def _wipe_residue() -> None:
	"""Slug-scoped teardown: remove every Run / Finding / Provenance / Dashboard row
	this module persisted under its test-agent slug, so run-persistence residue never
	accrues on the shared site. Belt-and-suspenders alongside the ``frappe.flags.in_test``
	commit gate — this module's ``setUp`` commits per test, which would otherwise make a
	prior test's in-transaction run/dashboard durable past the class-end rollback. Mirrors
	``test_platform_activation._wipe``: raw ``frappe.db.delete`` + commit (which also
	bypasses the append-only Provenance ``on_trash`` guard, legitimate for deleting this
	module's OWN test residue)."""
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
# PP-1 — result-class validation (the reject paths) at the tool boundary
# --------------------------------------------------------------------------- #
class TestPP1ResultClassValidation(FrappeTestCase):
	TOKENS = {TOKEN}
	REFS = {"Company", "Account"}

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The ref-existence gate runs before the result_class gate (a nonexistent
		# ref is dropped first), so use a real, existence-exempt Company ref to
		# isolate the class-validation branch under test. Create the row ourselves —
		# a fresh CI site has NO Company, and `get_value("Company", {})` would return
		# None (every _ok_ref would then drop as "ref does not exist" before the
		# result_class branch is ever reached).
		cls.company = _ensure_company()

	@classmethod
	def tearDownClass(cls):
		frappe.db.sql("delete from `tabCompany` where name=%s", WB_COMPANY)
		frappe.db.commit()
		super().tearDownClass()

	def _ok_ref(self, **over):
		return _finding(ref_doctype="Company", ref_name=self.company, **over)

	def _drop_reason(self, f):
		valid, dropped = _validate_findings([f], self.TOKENS, self.REFS)
		self.assertEqual(valid, [], "expected the row to be dropped")
		self.assertEqual(len(dropped), 1)
		return dropped[0]["reason"]

	def test_missing_result_class_is_dropped(self):
		f = self._ok_ref()
		f.pop("result_class")
		self.assertIn("result_class", self._drop_reason(f))

	def test_invalid_result_class_is_dropped(self):
		self.assertIn("result_class", self._drop_reason(self._ok_ref(result_class="wat")))

	def test_confirmed_outcome_from_evaluator_is_dropped(self):
		reason = self._drop_reason(self._ok_ref(result_class="confirmed_outcome"))
		self.assertIn("may not emit confirmed_outcome", reason)

	def test_derived_candidate_missing_metadata_is_dropped(self):
		reason = self._drop_reason(self._ok_ref(result_class="derived_candidate", confidence=90))
		self.assertIn("derived_candidate missing", reason)
		self.assertIn("match_basis", reason)
		self.assertIn("false_positive_path", reason)

	def test_legal_scenario_missing_metadata_is_dropped(self):
		reason = self._drop_reason(self._ok_ref(result_class="legal_scenario", rule_version="v1"))
		self.assertIn("legal_scenario missing", reason)
		self.assertIn("source", reason)
		self.assertIn("reviewer", reason)

	def test_complete_rows_pass(self):
		obs = self._ok_ref()
		cand = self._ok_ref(
			result_class="derived_candidate",
			confidence=90,
			match_basis="2B-match",
			false_positive_path="vendor alias",
		)
		legal = self._ok_ref(
			result_class="legal_scenario",
			rule_version="GST-2024",
			source="Sec 16(2)",
			reviewer="Administrator",
		)
		valid, dropped = _validate_findings([obs, cand, legal], self.TOKENS, self.REFS)
		self.assertEqual(dropped, [])
		self.assertEqual(len(valid), 3)


# --------------------------------------------------------------------------- #
# PP-1 — strong-verb gating (a shared helper refuses the token)
# --------------------------------------------------------------------------- #
class TestPP1StrongVerbHelper(FrappeTestCase):
	def test_verb_stripped_for_non_confirmed_class(self):
		out = cr.render_value_text("We saved 5,000 and recovered 200", "observed_fact")
		self.assertNotIn("saved", out)
		self.assertNotIn("recovered", out)
		self.assertIn(cr.STRONG_VERB_REPLACEMENT, out)

	def test_verb_allowed_only_for_confirmed_outcome_with_provenance(self):
		txt = "recovered 5,000"
		# confirmed_outcome + resolving provenance → the verb renders verbatim
		self.assertEqual(cr.render_value_text(txt, "confirmed_outcome", outcome_provenance="EVENT-1"), txt)
		# confirmed_outcome WITHOUT provenance → still gated
		self.assertNotIn("recovered", cr.render_value_text(txt, "confirmed_outcome"))

	def test_strict_mode_raises(self):
		with self.assertRaises(ValueError):
			cr.render_value_text("prevented a duplicate payment", "observed_fact", strict=True)

	def test_no_verb_text_passes_through(self):
		self.assertEqual(cr.render_value_text("a plain note", "observed_fact"), "a plain note")


# --------------------------------------------------------------------------- #
# PP-2 — the one coverage-verdict resolved at writeback + render gating
# --------------------------------------------------------------------------- #
class TestPP2RunStateResolution(FrappeTestCase):
	def test_evaluated_clean_only_when_complete_and_no_gate(self):
		self.assertEqual(
			cr.resolve_run_state(required_tokens={"a"}, evaluated_tokens={"a"}, partial=False),
			"evaluated_clean",
		)

	def test_all_required_not_evaluable_is_not_evaluable(self):
		self.assertEqual(
			cr.resolve_run_state(required_tokens={"a", "b"}, evaluated_tokens=set(), partial=True),
			"not_evaluable",
		)

	def test_some_evaluated_incomplete_is_partial(self):
		self.assertEqual(
			cr.resolve_run_state(required_tokens={"a", "b"}, evaluated_tokens={"a"}, partial=True),
			"partial",
		)

	def test_exec_failure_is_failed(self):
		self.assertEqual(
			cr.resolve_run_state(required_tokens={"a"}, evaluated_tokens=set(), partial=True, failed=True),
			"failed",
		)


class TestPP2FalseCleanUnreachable(FrappeTestCase):
	def _html(self, result_state, notes=None):
		return agent_runs._fallback_dashboard_html(
			"T",
			[],
			{"blocker": 0, "warning": 0, "note": 0},
			"",
			result_state=result_state,
			coverage_notes=notes,
		)

	def test_clean_sentence_only_for_evaluated_clean(self):
		self.assertIn("No exceptions were found", self._html("evaluated_clean"))

	def test_partial_hides_clean_sentence(self):
		html = self._html("partial")
		self.assertNotIn("No exceptions were found", html)
		self.assertIn("absence of findings is not a clean result", html)

	def test_not_evaluable_hides_clean_sentence(self):
		html = self._html("not_evaluable")
		self.assertNotIn("No exceptions were found", html)
		self.assertIn("could not evaluate the required checks", html)

	def test_failed_hides_clean_sentence(self):
		self.assertNotIn("No exceptions were found", self._html("failed"))

	def test_state_survives_banner_strip(self):
		"""PP-2 export/screenshot safety: the state is in the document body, not only
		in the detachable partial banner. Strip the banner and the state persists."""
		html = agent_runs._fallback_dashboard_html(
			"T",
			[],
			{"blocker": 0, "warning": 0, "note": 0},
			"scoped visibility — findings not auto-resolved",
			result_state="partial",
		)
		# remove the banner substring entirely
		banner_marker = "Partial run - "
		self.assertIn(banner_marker, html)
		stripped = html.split(banner_marker)[0] + html.split(banner_marker)[1].split("</div>", 1)[1]
		self.assertIn('data-result-state="partial"', stripped)
		self.assertIn("Partial coverage", stripped)


# --------------------------------------------------------------------------- #
# PP-3 — typed reason codes at writeback + remediation surfacing
# --------------------------------------------------------------------------- #
class TestPP3TypedReasonCodes(FrappeTestCase):
	def test_all_nine_codes_round_trip(self):
		coverage = {
			f"tok_{i}": {"state": "not_evaluable", "reason_code": code}
			for i, code in enumerate(cr.REASON_CODES)
		}
		evaluated, notes = agent_runs._coverage_summary(coverage)
		self.assertEqual(evaluated, set())
		by_code = {n["reason_code"] for n in notes}
		self.assertEqual(by_code, set(cr.REASON_CODES))
		for n in notes:
			# PP-3: the surfaced remediation is customer-facing text with NO literal
			# ``{app}``/``{setting}`` placeholder brace leaked (the round-trip supplies no
			# detail, so a neutral noun fills any placeholder).
			self.assertTrue(n["remediation"])
			self.assertNotIn("{", n["remediation"])
			self.assertEqual(n["retryable"], cr.is_retryable(n["reason_code"]))
			self.assertEqual(n["routing"], cr.routing_for(n["reason_code"]))

	def test_unknown_code_coerced_never_dropped(self):
		_, notes = agent_runs._coverage_summary(
			{"tokX": {"state": "not_evaluable", "reason_code": "novel gibberish"}}
		)
		self.assertEqual(len(notes), 1)
		self.assertEqual(notes[0]["reason_code"], "unsupported_customisation")
		self.assertEqual(notes[0]["detail"], "novel gibberish")

	def test_legacy_free_string_parsed(self):
		evaluated, notes = agent_runs._coverage_summary(
			{"tokA": "evaluated", "tokB": "not_evaluable(source_stale)", "tokC": "truncated"}
		)
		self.assertEqual(evaluated, {"tokA"})
		codes = {n["token"]: n["reason_code"] for n in notes}
		self.assertEqual(codes["tokB"], "source_stale")
		self.assertEqual(codes["tokC"], "run_truncated_watermark")

	def test_remediation_renders_in_degradation_section(self):
		_, notes = agent_runs._coverage_summary(
			{"tokZ": {"state": "not_evaluable", "reason_code": "configuration_missing"}}
		)
		html = agent_runs._fallback_dashboard_html(
			"T",
			[],
			{"blocker": 0, "warning": 0, "note": 0},
			"x",
			result_state="not_evaluable",
			coverage_notes=notes,
		)
		self.assertIn("Coverage gaps", html)
		self.assertIn("configuration_missing", html)
		self.assertIn("Configure", html)  # from the remediation template
		self.assertNotIn("{setting}", html)  # PP-3: the placeholder brace never leaks


# --------------------------------------------------------------------------- #
# PP-1/2/5 — end-to-end writeback + controller immutability guards
# --------------------------------------------------------------------------- #
class TestWritebackIntegration(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("pw-owner@example.com")
		_mk_listing()
		# Real, non-empty company: the A16 auto-resolve is company-scoped and skips a
		# scopeless/empty-company run, so a fresh CI site's None here would leave the
		# open finding unresolved. Create the row so the scope matches.
		cls.company = _ensure_company()
		cls.inst = _mk_installation(cls.owner)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_residue()
		frappe.db.sql("delete from `tabCompany` where name=%s", WB_COMPANY)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		for n in frappe.get_all(FINDING, filters={"agent": SLUG}, pluck="name"):
			frappe.delete_doc(FINDING, n, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ---- PP-1 persistence + set-once -------------------------------------- #
	def test_result_class_persisted_and_set_once(self):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run,
			self.inst,
			[_finding(result_class="observed_fact")],
			coverage={TOKEN: "evaluated"},
			scope={"company": self.company},
		)
		name = frappe.db.get_value(FINDING, {"agent": SLUG}, "name")
		self.assertEqual(frappe.db.get_value(FINDING, name, "result_class"), "observed_fact")

		# set-once: a second save that changes result_class raises (controller guard)
		fd = frappe.get_doc(FINDING, name)
		fd.result_class = "legal_scenario"
		with self.assertRaises(frappe.PermissionError):
			fd.save(ignore_permissions=True)

	# ---- PP-2 result_state resolved + persisted --------------------------- #
	def test_evaluated_clean_persisted_when_complete_and_empty(self):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run, self.inst, [], coverage={TOKEN: "evaluated"}, scope={"company": self.company}
		)
		self.assertEqual(frappe.db.get_value(RUN, run.name, "result_state"), "evaluated_clean")

	def test_not_evaluable_persisted_when_all_required_unevaluated(self):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run,
			self.inst,
			[],
			coverage={TOKEN: {"state": "not_evaluable", "reason_code": "configuration_missing"}},
			scope={"company": self.company},
		)
		self.assertEqual(frappe.db.get_value(RUN, run.name, "result_state"), "not_evaluable")

	def test_truncated_empty_run_is_partial_not_clean(self):
		run = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run, self.inst, [], coverage={TOKEN: "evaluated"}, truncated=True, scope={"company": self.company}
		)
		self.assertEqual(frappe.db.get_value(RUN, run.name, "result_state"), "partial")
		blob = json.loads(frappe.db.get_value(RUN, run.name, "coverage_json"))
		self.assertEqual(blob["result_state"], "partial")
		self.assertEqual(blob["required_tokens"], [TOKEN])

	# ---- PP-5 auto-resolve provenance + launch immutability --------------- #
	def test_auto_resolve_stamps_resolution_provenance(self):
		# run 1 raises an open finding under the company scope
		run1 = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run1,
			self.inst,
			[_finding(result_class="observed_fact")],
			coverage={TOKEN: "evaluated"},
			scope={"company": self.company},
		)
		name = frappe.db.get_value(FINDING, {"agent": SLUG, "state": "open"}, "name")
		self.assertIsNotNone(name)
		frappe.db.set_value(FINDING, name, "company", self.company, update_modified=False)

		# run 2 evaluates the same token with the finding no longer present → A16
		# coverage-scoped auto-resolve closes it and stamps machine provenance.
		run2 = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run2, self.inst, [], coverage={TOKEN: "evaluated"}, scope={"company": self.company}
		)
		fd = frappe.get_doc(FINDING, name)
		self.assertEqual(fd.state, "resolved")
		self.assertEqual(fd.resolution_kind, "auto_coverage")
		self.assertTrue(fd.resolved_at)

	def test_run_launch_fields_are_immutable(self):
		run = _mk_run(
			self.owner,
			bundle_version="1.0.0",
			preparation_mode="shadow",
			initiating_human=self.owner,
		)
		self.assertEqual(frappe.db.get_value(RUN, run.name, "bundle_version"), "1.0.0")
		run.reload()
		run.bundle_version = "2.0.0"
		with self.assertRaises(frappe.PermissionError):
			run.save(ignore_permissions=True)


# --------------------------------------------------------------------------- #
# PP-4 — #454 regression: dedupe + A16 auto-resolve must read on the SAME
# identity the write path re-homes findings to (_visibility_owner: the
# reviewer while shadow), not the plain installer owner. A default
# installation where reviewer == owner (as in ``TestWritebackIntegration``)
# cannot distinguish the two, so this exercises a DISTINCT reviewer.
# --------------------------------------------------------------------------- #
class TestPP4DedupeAutoResolveVisibilityOwner(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("pw-owner2@example.com")
		cls.reviewer = _mk_user("pw-reviewer2@example.com")
		_mk_listing()
		cls.company = _ensure_company()
		# Shadow by default (the doctype's activation_state default) with a
		# reviewer distinct from owner — the shape #454 fell over on.
		cls.inst = _mk_installation(cls.owner, cls.reviewer)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_wipe_residue()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		for n in frappe.get_all(FINDING, filters={"agent": SLUG}, pluck="name"):
			frappe.delete_doc(FINDING, n, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_dedupe_matches_existing_finding_when_reviewer_differs_from_owner(self):
		# run 1 raises an open finding. The write path re-homes it to the reviewer
		# (the shadow visibility_owner), never the installer owner.
		run1 = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run1,
			self.inst,
			[_finding(result_class="observed_fact")],
			coverage={TOKEN: "evaluated"},
			scope={"company": self.company},
		)
		open_findings = frappe.get_all(
			FINDING, filters={"agent": SLUG, "state": "open"}, fields=["name", "owner"]
		)
		self.assertEqual(len(open_findings), 1)
		self.assertEqual(open_findings[0].owner, self.reviewer)

		# run 2 reports the exact same finding again. If dedupe filtered on plain
		# owner (the installer, never granted the row) it would never match the
		# reviewer-owned row and would insert a duplicate — the #454 bug.
		run2 = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run2,
			self.inst,
			[_finding(result_class="observed_fact")],
			coverage={TOKEN: "evaluated"},
			scope={"company": self.company},
		)
		open_findings_after = frappe.get_all(FINDING, filters={"agent": SLUG, "state": "open"}, pluck="name")
		self.assertEqual(
			len(open_findings_after), 1, "dedupe must match the existing reviewer-owned row, not duplicate it"
		)
		self.assertEqual(open_findings_after[0], open_findings[0].name)
		self.assertEqual(frappe.db.get_value(FINDING, open_findings[0].name, "last_seen_run"), run2.name)

	def test_auto_resolve_finds_candidates_when_reviewer_differs_from_owner(self):
		# run 1 raises an open finding under the company scope, re-homed to the
		# reviewer exactly like the dedupe test above.
		run1 = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run1,
			self.inst,
			[_finding(result_class="observed_fact")],
			coverage={TOKEN: "evaluated"},
			scope={"company": self.company},
		)
		name = frappe.db.get_value(FINDING, {"agent": SLUG, "state": "open"}, "name")
		self.assertIsNotNone(name)
		self.assertEqual(frappe.db.get_value(FINDING, name, "owner"), self.reviewer)
		frappe.db.set_value(FINDING, name, "company", self.company, update_modified=False)

		# run 2 evaluates the same token with the finding no longer present. If the
		# A16 candidate query filtered on plain owner it would never see this
		# reviewer-owned row and would leave it open forever — the #454 bug.
		run2 = _mk_run(self.owner)
		agent_runs.record_delegate_run(
			run2, self.inst, [], coverage={TOKEN: "evaluated"}, scope={"company": self.company}
		)
		fd = frappe.get_doc(FINDING, name)
		self.assertEqual(fd.state, "resolved")
		self.assertEqual(fd.resolution_kind, "auto_coverage")
		self.assertTrue(fd.resolved_at)


# --------------------------------------------------------------------------- #
# PP-5 — the provenance ledger is append-only (the confirmed_outcome writer)
# --------------------------------------------------------------------------- #
class TestPP5ProvenanceAppendOnly(FrappeTestCase):
	DT = "Jarvis Agent Provenance Event"

	def test_event_cannot_be_modified_or_deleted(self):
		ev = frappe.get_doc({"doctype": self.DT, "event_type": "run_launched"}).insert(
			ignore_permissions=True
		)
		self.assertTrue(ev.occurred_at)
		ev.detail = "tampered"
		with self.assertRaises(frappe.PermissionError):
			ev.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(self.DT, ev.name, ignore_permissions=True, force=True)
