"""Phase-C runtime tests for the platform prerequisites PP-4 + PP-6.

  * PP-4 — preview/shadow activation. A freshly installed capability is ``shadow``;
    its runs execute and write findings/dashboards, but those are visible ONLY to
    the named reviewer (the general owner surface cannot read them — enforced on the
    finding read path, not just the SPA filter); no outward clean/compliant
    attestation renders in shadow even when the coverage verdict is
    ``evaluated_clean``. Promotion to live is by the named reviewer's explicit
    sign-off (recorded with who/when + a PP-5 provenance event); a non-authorised
    user is refused; after promotion the owner surface opens. A demotion/kill path
    returns it to shadow.
  * PP-6 — global activation budget. The default ceiling is 1: with one live module
    a second promotion is refused. A Jarvis Admin may raise the ceiling to 2 (the
    stage maximum) with a recorded reviewer-capacity justification + an audited
    provenance event; a non-admin raise is refused; a value above 2 is rejected.
  * PP-6 — meter anti-cherry-picking (bench scope): a saved dashboard carries the
    coverage/integrity digest in-body (screenshot-safe); no meter output renders a
    strong verb (saved/recovered/prevented).

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_activation
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_permissions, agent_runs, agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
PROVENANCE = "Jarvis Agent Provenance Event"
SETTINGS = "Jarvis Settings"

SLUG = "platform-activation-test-agent"
TOKEN = "tok_pa_1"

# The test customers whose per-customer activation-ceiling grants must be cleared
# between runs. The grant lives on the APPEND-ONLY provenance ledger (a Jarvis
# Admin's ``activation_ceiling_raised`` event keyed to the customer), which the
# controller refuses to delete — so a prior run's ceiling raise would otherwise
# PERMANENTLY lift this customer's ceiling to 2 and defeat the default-ceiling
# budget assertion (``test_budget_refuses_second_live_at_ceiling_one``). Raw
# ``frappe.db.delete`` bypasses the append-only ``on_trash`` guard, which is
# legitimate for test teardown of the module's own residue.
_TEST_CUSTOMERS = ("pa-owner@example.com", "pa-owner2@example.com")


def _mk_user(email: str) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"enabled": 1,
				"user_type": "System User",
			}
		)
		u.insert(ignore_permissions=True)
		u.add_roles("Jarvis User")
	return email


def _mk_listing() -> str:
	if not frappe.db.exists(LISTING, SLUG):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": SLUG,
				"title": "Platform Activation Test Agent",
				"rule_tokens": json.dumps([TOKEN]),
				"doctypes_required": json.dumps([]),
				# R5-J11(c): a CANONICAL pack id. The reviewer two-pack ceiling gate now
				# counts distinct non-empty rule_pack values (never the agent slug), so the
				# activation-ceiling tests must give SLUG and SLUG-b DISTINCT canonical packs
				# to legitimately reach a two-pack reviewer competency.
				"rule_pack": "pack-activation-a",
			}
		).insert(ignore_permissions=True)
	# The listing persists across runs (never wiped); ensure the canonical pack is set
	# even on a row created by a pre-R5-J11(c) run so the two-pack gate can qualify.
	frappe.db.set_value(LISTING, SLUG, "rule_pack", "pack-activation-a", update_modified=False)
	return SLUG


def _mk_installation(owner: str, reviewer: str, activation_state: str = "shadow") -> object:
	name = frappe.db.get_value(INSTALLATION, {"agent": SLUG, "owner": owner}, "name")
	if name:
		return frappe.get_doc(INSTALLATION, name)
	doc = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": SLUG,
			"run_as_user": owner,
			"reviewer": reviewer,
			"activation_state": activation_state,
		}
	)
	doc.owner = owner
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(INSTALLATION, doc.name, "owner", owner, update_modified=False)
	return frappe.get_doc(INSTALLATION, doc.name)


def _mk_run(owner: str, inst) -> object:
	doc = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": SLUG,
			"installation": inst.name,
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


def _finding(**over) -> dict:
	f = {
		"token": TOKEN,
		"ref_doctype": "Company",
		"ref_name": over.pop("ref_name", "PA-Ref"),
		"amount": 100,
		"severity": "note",
		"note": "an authored finding note",
		"result_class": "observed_fact",
	}
	f.update(over)
	return f


def _wipe():
	slugs = [SLUG, SLUG + "-b"]
	for dt in (FINDING, RUN):
		for n in frappe.get_all(dt, filters={"agent": ["in", slugs]}, pluck="name", ignore_permissions=True):
			frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
	for n in frappe.get_all(
		INSTALLATION, filters={"agent": ["in", slugs]}, pluck="name", ignore_permissions=True
	):
		frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
	# Clear this module's per-customer ceiling grants from the append-only ledger so
	# every test starts at the default ceiling of 1, independent of prior test runs
	# or sibling-test order (raw delete bypasses the append-only on_trash guard).
	frappe.db.delete(
		PROVENANCE,
		{"event_type": "activation_ceiling_raised", "result_link_name": ["in", _TEST_CUSTOMERS]},
	)
	frappe.db.set_single_value(SETTINGS, "activation_module_ceiling", 1)
	frappe.db.commit()


# --------------------------------------------------------------------------- #
# PP-4 — shadow visibility (the read-path gating is the security property)
# --------------------------------------------------------------------------- #
class TestPP4ShadowVisibility(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("pa-owner@example.com")
		cls.reviewer = _mk_user("pa-reviewer@example.com")
		_mk_listing()
		cls.company = frappe.db.get_value("Company", {}, "name")
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		self.inst = _mk_installation(self.owner, self.reviewer, activation_state="shadow")

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	def _record(self, inst, findings, **kw):
		run = _mk_run(inst.owner, inst)
		return agent_runs.record_delegate_run(
			run,
			inst,
			findings,
			coverage={TOKEN: "evaluated"},
			scope={"company": self.company},
			**kw,
		)

	def test_shadow_finding_hidden_from_owner_visible_to_reviewer(self):
		"""The core PP-4 security property: a shadow run's findings are re-homed to the
		reviewer, so the finding READ PATH (the permission-query + has_permission
		hooks) hides them from the general owner and shows them to the named reviewer.
		A non-reviewer user cannot read shadow findings."""
		self._record(self.inst, [_finding()])
		fname = frappe.db.get_value(FINDING, {"agent": SLUG}, "name")
		self.assertIsNotNone(fname)
		self.assertEqual(frappe.db.get_value(FINDING, fname, "owner"), self.reviewer)

		# The OWNER (not the reviewer) cannot read it on the finding read path.
		frappe.set_user(self.owner)
		self.assertEqual(frappe.get_list(FINDING, filters={"name": fname}, pluck="name"), [])
		self.assertFalse(
			agent_permissions.has_finding_permission(frappe.get_doc(FINDING, fname), "read", self.owner)
		)

		# The named REVIEWER can.
		frappe.set_user(self.reviewer)
		self.assertEqual(frappe.get_list(FINDING, filters={"name": fname}, pluck="name"), [fname])
		self.assertTrue(
			agent_permissions.has_finding_permission(frappe.get_doc(FINDING, fname), "read", self.reviewer)
		)

	def test_shadow_run_hidden_from_owner_visible_to_reviewer(self):
		run = self._record(self.inst, [_finding()])
		self.assertEqual(frappe.db.get_value(RUN, run.name, "owner"), self.reviewer)
		frappe.set_user(self.owner)
		self.assertEqual(frappe.get_list(RUN, filters={"name": run.name}, pluck="name"), [])
		frappe.set_user(self.reviewer)
		self.assertEqual(frappe.get_list(RUN, filters={"name": run.name}, pluck="name"), [run.name])

	def test_no_clean_attestation_in_shadow_even_when_evaluated_clean(self):
		"""A shadow run with an empty, fully-evaluated coverage set computes
		result_state == evaluated_clean, but the rendered artifact issues NO outward
		clean/compliant attestation."""
		run = self._record(self.inst, [])
		self.assertEqual(frappe.db.get_value(RUN, run.name, "result_state"), "evaluated_clean")
		html = agent_runs._fallback_dashboard_html(
			"T", [], {"blocker": 0, "warning": 0, "note": 0}, "", result_state="evaluated_clean", shadow=True
		)
		self.assertNotIn("No exceptions were found", html)
		self.assertNotIn("Evaluated — clean coverage", html)
		self.assertIn("Preview (shadow)", html)
		self.assertIn('data-result-state="shadow"', html)

	def test_live_finding_visible_to_owner(self):
		"""Control: a LIVE installation's findings stay on the owner surface."""
		inst = frappe.get_doc(INSTALLATION, self.inst.name)
		inst.activation_state = "live"
		inst.flags.promoting = True
		inst.save(ignore_permissions=True)
		frappe.db.commit()
		self._record(inst, [_finding()])
		fname = frappe.db.get_value(FINDING, {"agent": SLUG}, "name")
		self.assertEqual(frappe.db.get_value(FINDING, fname, "owner"), self.owner)
		frappe.set_user(self.owner)
		self.assertEqual(frappe.get_list(FINDING, filters={"name": fname}, pluck="name"), [fname])


# --------------------------------------------------------------------------- #
# PP-4 — promotion (reviewer sign-off, audit) + PP-6 budget
# --------------------------------------------------------------------------- #
class TestPP4PromotionAndPP6Budget(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("pa-owner2@example.com")
		cls.reviewer = _mk_user("pa-reviewer2@example.com")
		cls.stranger = _mk_user("pa-stranger@example.com")
		_mk_listing()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe()
		self.inst = _mk_installation(self.owner, self.reviewer, activation_state="shadow")

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe()

	def test_activation_state_cannot_flip_via_plain_save(self):
		"""The flag flips ONLY via the promotion path (guarded in the controller)."""
		inst = frappe.get_doc(INSTALLATION, self.inst.name)
		inst.activation_state = "live"
		with self.assertRaises(frappe.PermissionError):
			inst.save(ignore_permissions=True)

	def test_reviewer_promotes_with_audit_and_provenance(self):
		frappe.set_user(self.reviewer)
		res = agents_api.promote_installation(self.inst.name, justification="reviewed 10 samples, all clean")
		self.assertTrue(res["ok"])
		frappe.set_user("Administrator")
		inst = frappe.get_doc(INSTALLATION, self.inst.name)
		self.assertEqual(inst.activation_state, "live")
		self.assertEqual(inst.promoted_by, self.reviewer)
		self.assertTrue(inst.promoted_at)
		ev = frappe.get_all(
			PROVENANCE,
			filters={"installation": self.inst.name, "event_type": "agent_promoted_to_live"},
			fields=["reviewing_human", "preparation_mode"],
		)
		self.assertEqual(len(ev), 1)
		self.assertEqual(ev[0].reviewing_human, self.reviewer)
		self.assertEqual(ev[0].preparation_mode, "live")

	def test_promotion_by_non_authorised_user_refused(self):
		frappe.set_user(self.stranger)
		with self.assertRaises(frappe.PermissionError):
			agents_api.promote_installation(self.inst.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, self.inst.name, "activation_state"), "shadow")

	def test_budget_refuses_second_live_at_ceiling_one(self):
		"""Default ceiling 1: promoting a second module to live for the same customer
		is refused until the ceiling is raised."""
		inst2 = self._second_install()
		frappe.set_user(self.reviewer)
		agents_api.promote_installation(self.inst.name)  # first -> live (ok)
		with self.assertRaises(frappe.ValidationError):
			agents_api.promote_installation(inst2.name)  # second -> refused (budget)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst2.name, "activation_state"), "shadow")

	def test_ceiling_raise_admin_only_audited_then_second_promotes(self):
		inst2 = self._second_install()
		frappe.set_user(self.reviewer)
		agents_api.promote_installation(self.inst.name)

		# a non-admin raise is refused
		frappe.set_user(self.owner)
		with self.assertRaises(frappe.PermissionError):
			agents_api.raise_activation_ceiling(self.owner, "need a second pack")

		# a value above the stage maximum is rejected
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			agents_api.raise_activation_ceiling(self.owner, "go to three", new_ceiling=3)

		# an admin raise to 2 for THIS customer records an audited, customer-bound
		# provenance event. Provenance is append-only (uncleanable between runs), so key
		# the assertion on a unique justification and assert existence, not an absolute
		# count. reviewing_human is now the system-verified capacity reviewer (covering
		# two packs), initiating_human the admin who granted it.
		just = f"reviewer covers a second pack {frappe.generate_hash(length=8)}"
		res = agents_api.raise_activation_ceiling(self.owner, just)
		self.assertEqual(res["data"]["activation_module_ceiling"], 2)
		self.assertEqual(res["data"]["customer"], self.owner)
		ev = frappe.get_all(
			PROVENANCE,
			filters={"event_type": "activation_ceiling_raised", "detail": ["like", f"%{just}%"]},
			fields=["detail", "reviewing_human", "initiating_human", "result_link_name"],
		)
		self.assertEqual(len(ev), 1)
		self.assertEqual(ev[0].reviewing_human, self.reviewer)
		self.assertEqual(ev[0].initiating_human, "Administrator")
		self.assertEqual(ev[0].result_link_name, self.owner)
		self.assertIn(just, ev[0].detail)

		# now the second module promotes within the raised ceiling
		frappe.set_user(self.reviewer)
		agents_api.promote_installation(inst2.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst2.name, "activation_state"), "live")

	def test_promotion_opens_owner_surface(self):
		company = frappe.db.get_value("Company", {}, "name")
		run = _mk_run(self.owner, self.inst)
		agent_runs.record_delegate_run(
			run, self.inst, [_finding()], coverage={TOKEN: "evaluated"}, scope={"company": company}
		)
		fname = frappe.db.get_value(FINDING, {"agent": SLUG}, "name")
		self.assertEqual(frappe.db.get_value(FINDING, fname, "owner"), self.reviewer)  # shadow
		frappe.set_user(self.reviewer)
		agents_api.promote_installation(self.inst.name)
		frappe.set_user("Administrator")
		# re-homed to the owner — the owner surface now sees it
		self.assertEqual(frappe.db.get_value(FINDING, fname, "owner"), self.owner)
		frappe.set_user(self.owner)
		self.assertEqual(frappe.get_list(FINDING, filters={"name": fname}, pluck="name"), [fname])

	def test_demotion_returns_to_shadow_and_frees_budget(self):
		"""The kill path: a live installation is demoted back to shadow, the promotion
		stamp is cleared, and the freed budget slot allows a fresh promotion again."""
		frappe.set_user(self.reviewer)
		agents_api.promote_installation(self.inst.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, self.inst.name, "activation_state"), "live")
		frappe.set_user(self.reviewer)
		res = agents_api.demote_installation(self.inst.name, reason="too many false positives")
		self.assertTrue(res["ok"])
		frappe.set_user("Administrator")
		inst = frappe.get_doc(INSTALLATION, self.inst.name)
		self.assertEqual(inst.activation_state, "shadow")
		self.assertIsNone(inst.promoted_by)
		# the freed slot allows a fresh promotion under the default ceiling of 1
		frappe.set_user(self.reviewer)
		agents_api.promote_installation(self.inst.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, self.inst.name, "activation_state"), "live")

	def _second_install(self) -> object:
		"""A genuinely distinct second installation (a second listing) for the same
		owner, so the per-customer live count reaches two."""
		slug2 = SLUG + "-b"
		if not frappe.db.exists(LISTING, slug2):
			frappe.get_doc(
				{
					"doctype": LISTING,
					"agent_slug": slug2,
					"title": "Platform Activation Test Agent B",
					"rule_tokens": json.dumps([TOKEN]),
					"doctypes_required": json.dumps([]),
					# R5-J11(c): a DISTINCT canonical pack from SLUG so the reviewer covers
					# two distinct non-empty packs (the two-pack ceiling gate no longer
					# infers a pack from the agent slug).
					"rule_pack": "pack-activation-b",
				}
			).insert(ignore_permissions=True)
		# Ensure the canonical pack is set even on a listing left by a pre-R5-J11(c) run.
		frappe.db.set_value(LISTING, slug2, "rule_pack", "pack-activation-b", update_modified=False)
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": slug2,
				"run_as_user": self.owner,
				"reviewer": self.reviewer,
				"activation_state": "shadow",
			}
		)
		doc.owner = self.owner
		doc.flags.ignore_permissions = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(INSTALLATION, doc.name, "owner", self.owner, update_modified=False)
		frappe.db.commit()
		return frappe.get_doc(INSTALLATION, doc.name)


# --------------------------------------------------------------------------- #
# PP-6 — meter anti-cherry-picking (bench scope): digest + strong-verb gate
# --------------------------------------------------------------------------- #
class TestPP6MeterBenchScope(FrappeTestCase):
	def test_integrity_digest_is_stamped_in_body(self):
		html = agent_runs._fallback_dashboard_html(
			"Meter",
			[],
			{"blocker": 0, "warning": 0, "note": 0},
			"3/6/12-month windows complete",
			result_state="evaluated_clean",
			integrity_digest="abc123def456",
		)
		self.assertIn("data-digest-block", html)
		self.assertIn("Coverage &amp; integrity digest", html)
		self.assertIn("abc123def456", html)
		self.assertIn("computed server-side", html)

	def test_no_strong_verb_in_meter_output(self):
		"""A meter/evaluator finding is never confirmed_outcome, so a strong verb in
		its authored note is neutralised in the rendered artifact."""
		findings = [
			{
				"severity": "note",
				"result_class": "observed_fact",
				"note": "this run saved 5,000 and recovered 200",
				"ref_doctype": "Company",
				"ref_name": "X",
				"amount": 0,
			}
		]
		html = agent_runs._fallback_dashboard_html(
			"Meter", findings, {"blocker": 0, "warning": 0, "note": 1}, "", result_state="partial"
		)
		self.assertNotIn("saved 5,000", html)
		self.assertNotIn("recovered 200", html)
