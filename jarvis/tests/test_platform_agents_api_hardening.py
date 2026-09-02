"""Panel-hardening tests for ``jarvis.chat.agents_api`` (PP-1 / PP-4 / PP-6).

Covers the three adversarial-panel findings fixed on the agents SPA endpoints:

  * PP-1 read-path strong-verb gate (findings #2/#5): ``list_findings`` serves the
    stored authored ``title``/``detail_md`` through the SAME shared helper the
    fallback dashboard uses, so a "saved/recovered/prevented" token on any row that
    is NOT a ``confirmed_outcome`` with a resolving provenance link is neutralised
    server-side — no read surface can emit an unearned strong verb — and every row
    carries its ``result_class`` + class-conditional metadata for the SPA badge.
  * PP-6 per-customer ceiling (finding #3): the activation-ceiling raise is bound to
    ONE named customer and system-verifies that customer's reviewer covers two packs;
    a raise for customer A never unlocks a second live module for customer B.
  * PP-6 promotion TOCTOU (finding #1): promotion serializes on a per-customer redis
    lock; when the lock is unavailable the promotion refuses rather than racing the
    budget check.

Run:
  bench --site patterntest.localhost run-tests --app jarvis \
    --module jarvis.tests.test_platform_agents_api_hardening
"""

import json
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jarvis.chat import agent_scheduler, agents_api
from jarvis.tests._agent_access import allow_listing_for, clear_listing_access

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
ACTIVITY = "Jarvis Agent Activity"
FINDING = "Jarvis Agent Finding"
PROVENANCE = "Jarvis Agent Provenance Event"
SETTINGS = "Jarvis Settings"

# The reviewing role promote/demote demands (JARVIS_REVIEWER_ROLES). Created by
# DocType sync - one DocType names it - so it exists on a fresh CI site.
_REVIEWER_ROLE = "Jarvis Skill Reviewer"

PREFIX = "hardening-h-"


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


def _mk_listing(slug: str) -> str:
	# One synthetic pack per agent: the two-pack ceiling preflight counts distinct
	# non-empty rule_pack values (R5-P1-02 — the agent-slug fallback is gone), so
	# the "reviewer covers two packs" scaffolding these tests rely on must declare
	# real pack ids. Distinct-per-slug preserves the original intent exactly.
	if not frappe.db.exists(LISTING, slug):
		frappe.get_doc(
			{
				"doctype": LISTING,
				"agent_slug": slug,
				"title": f"Hardening {slug}",
				"rule_tokens": json.dumps(["tok"]),
				"doctypes_required": json.dumps([]),
				"rule_pack": f"pack-{slug}",
			}
		).insert(ignore_permissions=True)
	else:
		# A listing left by an earlier run may predate the rule_pack field.
		frappe.db.set_value(LISTING, slug, "rule_pack", f"pack-{slug}", update_modified=False)
	# jarvis#1062: a listing is CLOSED until somebody is allowed, and every fixture
	# user in this module is a plain Jarvis User. Granted to the ROLE, not by name,
	# so _restrict() below - which REPLACES allowed_roles - still takes the grant
	# away when a test means to lock the agent down. Granting by name would leave a
	# residue that made those gate assertions pass for the wrong reason.
	allow_listing_for(slug, roles=["Jarvis User"])
	return slug


def _mk_install(owner: str, slug: str, reviewer: str, activation_state: str = "shadow") -> object:
	_mk_listing(slug)
	name = frappe.db.get_value(INSTALLATION, {"agent": slug, "owner": owner}, "name")
	if name:
		return frappe.get_doc(INSTALLATION, name)
	doc = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": slug,
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


def _mk_finding(owner: str, slug: str, **over) -> object:
	f = {
		"doctype": FINDING,
		"agent": slug,
		"rule_id": "R1",
		"severity": "note",
		"result_class": "observed_fact",
		"title": "a plain title",
		"detail_md": "a plain detail",
		"state": "open",
	}
	f.update(over)
	doc = frappe.get_doc(f)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(FINDING, doc.name, "owner", owner, update_modified=False)
	return frappe.get_doc(FINDING, doc.name)


def _mk_run(installation: str, slug: str, owner: str) -> str:
	doc = frappe.get_doc(
		{
			"doctype": RUN,
			"agent": slug,
			"installation": installation,
			"trigger": "manual",
			"status": "completed",
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(RUN, doc.name, "owner", owner, update_modified=False)
	return doc.name


def _wipe(slugs) -> None:
	for n in frappe.get_all(FINDING, filters={"agent": ["in", slugs]}, pluck="name", ignore_permissions=True):
		frappe.delete_doc(FINDING, n, force=True, ignore_permissions=True)
	for n in frappe.get_all(RUN, filters={"agent": ["in", slugs]}, pluck="name", ignore_permissions=True):
		frappe.delete_doc(RUN, n, force=True, ignore_permissions=True)
	# ``uninstall_agent`` writes an ``uninstalled`` activity row and COMMITS, so the
	# test-case rollback cannot reclaim it — same reason test_platform_capability_contract
	# wipes ACTIVITY explicitly. Without this the suite leaks rows on every run.
	for n in frappe.get_all(
		ACTIVITY, filters={"agent": ["in", slugs]}, pluck="name", ignore_permissions=True
	):
		frappe.delete_doc(ACTIVITY, n, force=True, ignore_permissions=True)
	for n in frappe.get_all(
		INSTALLATION, filters={"agent": ["in", slugs]}, pluck="name", ignore_permissions=True
	):
		frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
	frappe.db.set_single_value(SETTINGS, "activation_module_ceiling", 1)
	frappe.db.commit()


@contextmanager
def _lock_denied(*a, **k):
	"""Stand-in for ``redis_lock`` that never grants the lock (simulates a concurrent
	activation change already holding it)."""
	yield False


# --------------------------------------------------------------------------- #
# PP-1 — list_findings read-path strong-verb gate + class metadata
# --------------------------------------------------------------------------- #
class TestListFindingsStrongVerbGate(FrappeTestCase):
	SLUG = PREFIX + "lf"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.user = _mk_user("h-lf-owner@example.com")
		_mk_listing(cls.SLUG)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])

	def _rows(self):
		frappe.set_user(self.user)
		try:
			return agents_api.list_findings()["rows"]
		finally:
			frappe.set_user("Administrator")

	def test_strong_verb_neutralised_on_observed_fact(self):
		"""An observed_fact whose authored note says 'recovered X' is delivered to the
		SPA with the strong verb NEUTRALISED (never intact) on both title and detail."""
		_mk_finding(
			self.user,
			self.SLUG,
			result_class="observed_fact",
			title="recovered 5,00,000 from vendor",
			detail_md="We recovered 500000 and saved 20000 in duplicate payments.",
		)
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		r = rows[0]
		self.assertEqual(r["result_class"], "observed_fact")
		for field in ("title", "detail_md"):
			self.assertNotIn("recovered", r[field].lower())
			self.assertNotIn("saved", r[field].lower())
			self.assertIn("[unverified]", r[field])

	def test_confirmed_outcome_with_provenance_keeps_verb(self):
		"""Control: a genuine confirmed_outcome row WITH a resolving outcome_provenance
		link keeps the strong verb — the gate neutralises only UNEARNED claims."""
		ev = agents_api._append_provenance_event(event_type="transaction_posted", agent=self.SLUG)
		_mk_finding(
			self.user,
			self.SLUG,
			result_class="confirmed_outcome",
			outcome_provenance=ev,
			title="recovered 5,00,000",
			detail_md="recovered 500000 confirmed by ledger",
		)
		r = self._rows()[0]
		self.assertEqual(r["result_class"], "confirmed_outcome")
		self.assertIn("recovered", r["detail_md"].lower())
		self.assertNotIn("[unverified]", r["detail_md"])

	def test_class_conditional_metadata_rides_on_each_row(self):
		"""A derived_candidate is no longer visually indistinguishable from an
		observed_fact: its confidence / match_basis / false_positive_path /
		confirmation_status ride on the read row for the SPA badge."""
		_mk_finding(
			self.user,
			self.SLUG,
			result_class="derived_candidate",
			confidence=80,
			match_basis="2B-match",
			false_positive_path="vendor alias",
			confirmation_status="unconfirmed",
			title="candidate mismatch",
			detail_md="a possible duplicate",
		)
		r = self._rows()[0]
		self.assertEqual(r["result_class"], "derived_candidate")
		self.assertEqual(r["match_basis"], "2B-match")
		self.assertEqual(r["false_positive_path"], "vendor alias")
		self.assertEqual(r["confirmation_status"], "unconfirmed")
		self.assertIn("confidence", r)


# --------------------------------------------------------------------------- #
# PP-6 — per-customer activation ceiling (finding #3)
# --------------------------------------------------------------------------- #
class TestPerCustomerCeiling(FrappeTestCase):
	A1 = PREFIX + "a1"
	A2 = PREFIX + "a2"
	B1 = PREFIX + "b1"
	B2 = PREFIX + "b2"
	C1 = PREFIX + "c1"
	ALL = (A1, A2, B1, B2, C1)

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.cust_a = _mk_user("h-cust-a@example.com")
		cls.rev_a = _mk_user("h-rev-a@example.com")
		cls.cust_b = _mk_user("h-cust-b@example.com")
		cls.rev_b = _mk_user("h-rev-b@example.com")
		cls.cust_c = _mk_user("h-cust-c@example.com")
		cls.rev_c = _mk_user("h-rev-c@example.com")
		# jarvis#1062: promoting is the reviewer set's act, not the named reviewer's
		# own. Unconditional (outside _mk_user's exists-guard) - on a shared site the
		# user may already exist without the role.
		for r in (cls.rev_a, cls.rev_b, cls.rev_c):
			frappe.get_doc("User", r).add_roles(_REVIEWER_ROLE)
			frappe.clear_cache(user=r)
		for s in cls.ALL:
			_mk_listing(s)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe(list(self.ALL))
		# customer A + B each cover two packs (a reviewer of record on two distinct
		# agents); customer C covers only one pack.
		_mk_install(self.cust_a, self.A1, self.rev_a)
		_mk_install(self.cust_a, self.A2, self.rev_a)
		self.b1 = _mk_install(self.cust_b, self.B1, self.rev_b)
		self.b2 = _mk_install(self.cust_b, self.B2, self.rev_b)
		_mk_install(self.cust_c, self.C1, self.rev_c)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe(list(self.ALL))

	def test_raise_for_a_does_not_unlock_b(self):
		"""The core isolation property: raising customer A's ceiling to 2 leaves
		customer B at the default 1 — B's second promotion is still refused."""
		frappe.set_user("Administrator")
		agents_api.raise_activation_ceiling(self.cust_a, "A's reviewer covers two packs")

		self.assertEqual(agents_api._activation_ceiling(self.cust_a), 2)
		self.assertEqual(agents_api._activation_ceiling(self.cust_b), 1)

		# B promotes its first module (ok) but the second is refused — the A-grant
		# never leaked to B.
		frappe.set_user(self.rev_b)
		agents_api.promote_installation(self.b1.name)
		with self.assertRaises(frappe.ValidationError):
			agents_api.promote_installation(self.b2.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, self.b2.name, "activation_state"), "shadow")

	def test_reviewer_capacity_gate_blocks_single_pack_customer(self):
		"""A raise for a customer whose reviewer covers only ONE pack is refused by
		the system-verified capacity gate (not just a free-text justification)."""
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			agents_api.raise_activation_ceiling(self.cust_c, "please just trust me")
		self.assertEqual(agents_api._activation_ceiling(self.cust_c), 1)

	def test_raise_requires_a_real_named_customer(self):
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			agents_api.raise_activation_ceiling("", "no customer named")


# --------------------------------------------------------------------------- #
# PP-6 — promotion budget TOCTOU serialization (finding #1)
# --------------------------------------------------------------------------- #
class TestPromotionLockSerialization(FrappeTestCase):
	SLUG = PREFIX + "lock"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-lock-owner@example.com")
		cls.reviewer = _mk_user("h-lock-rev@example.com")
		# jarvis#1062: see TestPerCustomerCeiling - promote authority is the reviewer
		# set now, so the lock tests need their actor to hold it or they never reach
		# the lock they are about.
		frappe.get_doc("User", cls.reviewer).add_roles(_REVIEWER_ROLE)
		frappe.clear_cache(user=cls.reviewer)
		_mk_listing(cls.SLUG)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		self.inst = _mk_install(self.owner, self.SLUG, self.reviewer)

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])

	def test_promotion_refused_when_activation_lock_unavailable(self):
		"""The budget check is serialized on a per-customer lock; when the lock is held
		elsewhere the promotion refuses rather than racing the read-check-flip window,
		and the row stays shadow."""
		frappe.set_user(self.reviewer)
		with patch("jarvis._redis_lock.redis_lock", _lock_denied):
			with self.assertRaises(frappe.ValidationError):
				agents_api.promote_installation(self.inst.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, self.inst.name, "activation_state"), "shadow")

	def test_promotion_holds_the_per_owner_lock(self):
		"""The lock is keyed on the OWNER (customer), so all of a customer's activation
		changes serialize against each other."""
		seen = {}

		@contextmanager
		def _spy(name, **k):
			seen["name"] = name
			yield True

		frappe.set_user(self.reviewer)
		with patch("jarvis._redis_lock.redis_lock", _spy):
			agents_api.promote_installation(self.inst.name)
		frappe.set_user("Administrator")
		self.assertEqual(seen["name"], f"jarvis_agent_activation:{self.owner}")
		self.assertEqual(frappe.db.get_value(INSTALLATION, self.inst.name, "activation_state"), "live")


# --------------------------------------------------------------------------- #
# #455 — the uninstall cascade is scoped to ITS OWN installation
# --------------------------------------------------------------------------- #
class TestUninstallCascadeScope(FrappeTestCase):
	"""``uninstall_agent`` used to select findings by ``(agent, owner in [owner,
	reviewer])`` and hard-delete the lot with ``force=True``. A reviewer may be the
	reviewer-of-record for MANY installations (PP-6 institutionalises exactly that),
	and PP-4 re-homes a shadow installation's findings TO that reviewer, so one
	customer's uninstall irreversibly destroyed other customers' audit history.
	Membership now comes from the run the finding was created on."""

	SLUG = PREFIX + "unin"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner_a = _mk_user("h-unin-a@example.com")
		cls.owner_b = _mk_user("h-unin-b@example.com")
		cls.reviewer = _mk_user("h-unin-rev@example.com")
		_mk_listing(cls.SLUG)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		# Three installations of the SAME agent. A and B both name R as reviewer;
		# R also runs their own install of it. Every finding below is therefore
		# owned by R (PP-4 shadow re-homing) and carries the same agent — the exact
		# shape the old (agent, owner) filter swept.
		self.inst_a = _mk_install(self.owner_a, self.SLUG, self.reviewer)
		self.inst_b = _mk_install(self.owner_b, self.SLUG, self.reviewer)
		self.inst_r = _mk_install(self.reviewer, self.SLUG, self.owner_a)
		self.run_a = _mk_run(self.inst_a.name, self.SLUG, self.reviewer)
		self.run_b = _mk_run(self.inst_b.name, self.SLUG, self.reviewer)
		self.run_r = _mk_run(self.inst_r.name, self.SLUG, self.reviewer)
		self.f_a = self._finding(self.run_a)
		self.f_b = self._finding(self.run_b)
		self.f_r = self._finding(self.run_r)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])

	def _finding(self, run: str) -> str:
		return _mk_finding(self.reviewer, self.SLUG, run=run, first_seen_run=run, last_seen_run=run).name

	def _uninstall_a(self):
		frappe.set_user(self.owner_a)
		try:
			agents_api.uninstall_agent(self.inst_a.name)
		finally:
			frappe.set_user("Administrator")

	def test_uninstall_spares_a_co_reviewed_owners_findings(self):
		"""THE regression: owner A uninstalls; owner B's and the shared reviewer's
		own findings survive, while A's own history is still fully cascaded."""
		self._uninstall_a()
		# A's own history is gone — the cascade still does its job.
		self.assertFalse(frappe.db.exists(FINDING, self.f_a))
		self.assertFalse(frappe.db.exists(RUN, self.run_a))
		self.assertFalse(frappe.db.exists(INSTALLATION, self.inst_a.name))
		# Nobody else's is.
		self.assertTrue(frappe.db.exists(FINDING, self.f_b))
		self.assertTrue(frappe.db.exists(FINDING, self.f_r))
		self.assertTrue(frappe.db.exists(RUN, self.run_b))
		self.assertTrue(frappe.db.exists(RUN, self.run_r))
		self.assertTrue(frappe.db.exists(INSTALLATION, self.inst_b.name))
		self.assertTrue(frappe.db.exists(INSTALLATION, self.inst_r.name))

	def test_cross_install_recurrence_bump_is_detached_not_deleted(self):
		"""``last_seen_run`` is not membership. #454's dedupe can bump another
		owner's finding onto THIS install's run; the row must survive, and its
		recurrence pointer must fall back to its own first_seen_run rather than
		dangle at a force-deleted run."""
		frappe.db.set_value(FINDING, self.f_b, "last_seen_run", self.run_a, update_modified=False)
		frappe.db.commit()
		self._uninstall_a()
		self.assertTrue(frappe.db.exists(FINDING, self.f_b))
		self.assertEqual(frappe.db.get_value(FINDING, self.f_b, "last_seen_run"), self.run_b)

	def test_either_creation_stamp_alone_establishes_membership(self):
		"""``run`` and ``first_seen_run`` are written to the same value today, but the
		cascade ORs them so a row carrying only ONE of the two still resolves. Both
		half-stamped shapes belong to A and must go; neither may drag in B's row."""
		only_run = _mk_finding(self.reviewer, self.SLUG, run=self.run_a).name
		only_first = _mk_finding(self.reviewer, self.SLUG, first_seen_run=self.run_a).name
		frappe.db.commit()
		self._uninstall_a()
		self.assertFalse(frappe.db.exists(FINDING, only_run))
		self.assertFalse(frappe.db.exists(FINDING, only_first))
		self.assertTrue(frappe.db.exists(FINDING, self.f_b))

	def test_finding_with_no_run_pointer_is_left_alone(self):
		"""A row with no run pointer cannot be attributed to any installation, so
		the cascade leaves it. An orphan is recoverable from Desk; a wrongly
		destroyed finding is not."""
		orphan = _mk_finding(self.reviewer, self.SLUG).name
		frappe.db.commit()
		self._uninstall_a()
		self.assertTrue(frappe.db.exists(FINDING, orphan))

	def test_on_trash_refuses_a_foreign_finding_inside_a_cascade(self):
		"""Defence in depth: even a caller that hand-rolls the old broad selection
		cannot destroy a foreign finding, because force + ignore_permissions do not
		bypass on_trash."""
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(
				FINDING,
				self.f_b,
				ignore_permissions=True,
				force=True,
				flags={"jarvis_uninstall_installation": self.inst_a.name},
			)
		self.assertTrue(frappe.db.exists(FINDING, self.f_b))

	def test_on_trash_guard_is_inert_outside_a_cascade(self):
		"""No cascade flag, no guard — an ordinary admin/Desk delete is unaffected."""
		frappe.delete_doc(FINDING, self.f_b, ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.exists(FINDING, self.f_b))


# --------------------------------------------------------------------------- #
# #458 — the agent_catalog_dirty TOCTOU guard
# --------------------------------------------------------------------------- #
_TOCTOU_KEYS = (
	"agent_catalog_dirty",
	"agent_catalog_version",
	"agent_skills_sync_status",
	"agent_skills_synced_at",
)


def _single(field: str):
	"""Read a Jarvis Settings field bypassing frappe.db.value_cache — the very
	cache this section is about."""
	return frappe.db.get_single_value(SETTINGS, field, cache=False)


def _second_connection():
	"""A SEPARATE DB connection, so a write can be committed while the push
	worker's own transaction is open.

	This is what makes the #458 regression faithful: a same-connection write
	would be visible to the worker's re-read (read-your-own-writes) and a
	same-connection commit would clear ``frappe.db.value_cache`` for it, so
	neither could ever reproduce the bug.
	"""
	from frappe.database import get_db

	conf = frappe.conf
	return get_db(
		socket=conf.db_socket,
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user or conf.db_name,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)


def _concurrent_catalog_mutation() -> None:
	"""Model a user's ``set_enabled`` landing mid-push: another connection bumps
	the catalog version and re-marks the catalog dirty, and COMMITS."""
	conn = _second_connection()
	try:
		conn.sql(
			"""UPDATE `tabSingles`
			SET `value` = CAST(COALESCE(NULLIF(`value`, ''), '0') AS UNSIGNED) + 1
			WHERE `doctype` = %(dt)s AND `field` = 'agent_catalog_version'""",
			{"dt": SETTINGS},
		)
		conn.sql(
			"""UPDATE `tabSingles` SET `value` = '1'
			WHERE `doctype` = %(dt)s AND `field` = 'agent_catalog_dirty'""",
			{"dt": SETTINGS},
		)
		conn.commit()
	finally:
		conn.close()


class TestAgentCatalogPushTOCTOU(FrappeTestCase):
	"""#458: the push worker's version recheck must be able to SEE a mutation that
	landed after its snapshot. Before the fix it re-read its own
	``frappe.db.value_cache`` entry (and, under REPEATABLE READ, its own
	transaction snapshot), so the comparison was tautologically equal and a
	mid-push mutation was silently marked applied."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._saved = {k: _single(k) for k in _TOCTOU_KEYS}

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for k, v in cls._saved.items():
			frappe.db.set_single_value(SETTINGS, k, v)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value(SETTINGS, "agent_catalog_version", 7)
		frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 1)
		frappe.db.set_single_value(SETTINGS, "agent_skills_sync_status", "pending: applying agents")
		# Commit the seed so the worker below opens a CLEAN transaction holding no
		# row locks the second connection would block on.
		frappe.db.commit()

	def _push(self, side_effect=None):
		from jarvis import admin_client

		def _fake_push(agent_skills):
			if side_effect:
				side_effect()
			return {"ok": True}

		with patch.object(admin_client, "post_push_agent_skills", side_effect=_fake_push):
			agents_api._enqueued_push_agent_skills()

	def test_mid_push_mutation_is_detected_not_marked_applied(self):
		"""A mutation committed by another connection WHILE the push is in flight
		leaves ``agent_catalog_dirty`` set, so the SPA keeps showing "Apply
		pending" and the change is not silently lost."""
		self._push(side_effect=_concurrent_catalog_mutation)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_version")), 8)
		self.assertEqual(
			frappe.utils.cint(_single("agent_catalog_dirty")),
			1,
			"a mid-push mutation must NOT have its dirty flag cleared by that push",
		)
		self.assertTrue(str(_single("agent_skills_sync_status")).startswith("ok ("))

	def test_clean_push_still_clears_the_dirty_flag(self):
		"""Control: with no mid-push mutation the version is unchanged, so the push
		clears the flag exactly as before. The fix must not make the flag sticky."""
		self._push()
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_version")), 7)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 0)
		self.assertTrue(str(_single("agent_skills_sync_status")).startswith("ok ("))

	def test_failed_push_leaves_the_dirty_flag_set(self):
		"""Control: a terminal failure never clears the flag, and never leaves the
		status stuck on ``pending:``."""
		from jarvis import admin_client

		with patch.object(
			admin_client,
			"post_push_agent_skills",
			side_effect=admin_client.AdminUnreachableError("nope"),
		):
			agents_api._enqueued_push_agent_skills()
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 1)
		self.assertTrue(str(_single("agent_skills_sync_status")).startswith("failed: admin unreachable"))

	def test_version_bump_does_not_depend_on_a_previously_read_value(self):
		"""#458 lost update: the bump is one statement, so it increments whatever
		the row currently holds. The old read-modify-write took the value from
		``get_single_value`` (cached / snapshot) and wrote that + 1, which silently
		discards a concurrent increment. Poisoning the cache with the stale value
		reproduces exactly that."""
		frappe.db.set_single_value(SETTINGS, "agent_catalog_version", 5)
		frappe.db.commit()
		# The DB row moves to 9 without touching any cache — stand-in for the
		# increment a concurrent request already committed.
		frappe.db.sql(
			"""UPDATE `tabSingles` SET `value` = '9'
			WHERE `doctype` = %(dt)s AND `field` = 'agent_catalog_version'""",
			{"dt": SETTINGS},
		)
		# Seed a stale cached single value. The singles cache is keyed
		# value_cache[doctype][fieldname] on both majors, but the doctype
		# sub-dict is created lazily by the first get_single_value; on Frappe 15
		# nothing has populated it here yet, so index into it via setdefault.
		frappe.db.value_cache.setdefault(SETTINGS, {})["agent_catalog_version"] = 5
		agents_api._bump_catalog_version()
		self.assertEqual(
			frappe.utils.cint(_single("agent_catalog_version")),
			10,
			"the increment must be applied to the ROW, never to a stale read",
		)


# --------------------------------------------------------------------------- #
# #460 — (owner, agent) uniqueness is a DB constraint, not check-then-act
# --------------------------------------------------------------------------- #
def _owner_agent_index() -> list[dict]:
	"""The (owner, agent) index as the DATABASE reports it.

	Read from ``information_schema`` on purpose: ``bench migrate`` exits 0 whether
	or not ``on_doctype_update`` ever fired, so its exit code proves nothing about
	whether the constraint exists.
	"""
	return frappe.db.sql(
		"""
		SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX
		FROM information_schema.STATISTICS
		WHERE TABLE_SCHEMA = DATABASE()
		  AND TABLE_NAME = 'tabJarvis Agent Installation'
		  AND INDEX_NAME = 'owner_agent'
		ORDER BY SEQ_IN_INDEX
		""",
		as_dict=True,
	)


def _drop_owner_agent_index() -> None:
	if _owner_agent_index():
		frappe.db.sql_ddl("ALTER TABLE `tabJarvis Agent Installation` DROP INDEX `owner_agent`")


def _add_owner_agent_index() -> None:
	frappe.db.add_unique(INSTALLATION, ["owner", "agent"], constraint_name="owner_agent")


class TestAgentInstallUniqueIndex(FrappeTestCase):
	"""#460: the constraint must EXIST, and the racing install must lose to it
	with the friendly message rather than a 500."""

	SLUG = PREFIX + "uniq"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-uniq-owner@example.com")
		_mk_listing(cls.SLUG)
		frappe.db.set_value(LISTING, cls.SLUG, "status", "Published", update_modified=False)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])

	def test_owner_agent_unique_index_exists(self):
		"""A fresh install gets the index from ``on_doctype_update``; an existing
		site gets it from the v2_13 patch. Either way the DB must report it."""
		cols = _owner_agent_index()
		self.assertEqual([c["COLUMN_NAME"] for c in cols], ["owner", "agent"])
		self.assertEqual({c["NON_UNIQUE"] for c in cols}, {0})

	def test_racing_install_produces_one_row_and_a_friendly_error(self):
		"""Two concurrent installs of the same agent by one owner: both
		``frappe.db.exists`` checks return None (exactly what the race achieves,
		since neither transaction has committed), so the DB constraint is the only
		thing left. It must yield ONE row and the ordinary "already installed"
		ValidationError, not an unhandled IntegrityError."""
		real_exists = frappe.db.exists

		def blind_to_installs(dt, *a, **kw):
			# Only the (owner, agent) clash lookup is blinded; every other existence
			# check the insert makes (link validation, etc.) runs for real.
			if dt == INSTALLATION:
				return None
			return real_exists(dt, *a, **kw)

		frappe.set_user(self.owner)
		try:
			agents_api.install_agent(self.SLUG)
			with patch.object(frappe.db, "exists", side_effect=blind_to_installs):
				with self.assertRaises(frappe.ValidationError) as ctx:
					agents_api.install_agent(self.SLUG)
		finally:
			frappe.set_user("Administrator")
		self.assertNotIsInstance(ctx.exception, frappe.UniqueValidationError)
		self.assertIn("already installed", str(ctx.exception))
		rows = frappe.get_all(
			INSTALLATION, filters={"owner": self.owner, "agent": self.SLUG}, ignore_permissions=True
		)
		self.assertEqual(len(rows), 1)

	def test_ordinary_double_install_still_gets_the_friendly_message(self):
		"""Control: the non-racing path is unchanged — the check-then-act guard
		still answers first, so the constraint is never reached."""
		frappe.set_user(self.owner)
		try:
			agents_api.install_agent(self.SLUG)
			with self.assertRaises(frappe.ValidationError) as ctx:
				agents_api.install_agent(self.SLUG)
		finally:
			frappe.set_user("Administrator")
		self.assertIn("already installed", str(ctx.exception))


class TestAgentInstallDuplicateMerge(FrappeTestCase):
	"""#460 migration trap: a bench already carrying duplicate rows from the race
	would hard-fail the ALTER TABLE, so the patch merges first. What it keeps and
	what it discards is the part that must not be got wrong."""

	SLUG = PREFIX + "dupe"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-dupe-owner@example.com")
		_mk_listing(cls.SLUG)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		# The duplicates this patch exists to clean up predate the constraint, so
		# seeding them requires it to be absent.
		frappe.db.commit()
		_drop_owner_agent_index()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.commit()
		_add_owner_agent_index()

	def _seed(self, *, enabled=0, activation_state="shadow", runs=0):
		# Deliberately NOT via _mk_install: that reuses an existing row, and the
		# point here is to produce the SECOND row the race would have produced.
		# ignore_validate blinds validate(), where the check-then-act guard lives —
		# which is what the race achieves in production.
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": self.SLUG,
				"run_as_user": self.owner,
				"reviewer": self.owner,
			}
		)
		doc.flags.ignore_permissions = True
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			INSTALLATION,
			doc.name,
			{"owner": self.owner, "enabled": enabled, "activation_state": activation_state},
			update_modified=False,
		)
		for _ in range(runs):
			_mk_run(doc.name, self.SLUG, self.owner)
		return doc.name

	def test_merge_keeps_the_live_enabled_row_and_rehomes_the_history(self):
		"""The keeper is the row whose state is most expensive to reconstruct (live
		beats shadow, enabled beats disabled), and the loser's runs are re-pointed
		at it — so promoting/enabling is never silently undone AND no run history
		is destroyed."""
		from jarvis.patches import v2_13_unique_agent_installation as patch_mod

		loser = self._seed(enabled=0, activation_state="shadow", runs=2)
		keeper = self._seed(enabled=1, activation_state="live", runs=0)
		frappe.db.commit()

		merged = patch_mod._merge_duplicate_installs()
		frappe.db.commit()

		self.assertEqual(merged, 1)
		self.assertFalse(frappe.db.exists(INSTALLATION, loser))
		self.assertTrue(frappe.db.exists(INSTALLATION, keeper))
		survivor = frappe.db.get_value(INSTALLATION, keeper, ["enabled", "activation_state"], as_dict=True)
		self.assertEqual(frappe.utils.cint(survivor.enabled), 1)
		self.assertEqual(survivor.activation_state, "live")
		# Both of the loser's runs now hang off the keeper: nothing was destroyed.
		self.assertEqual(frappe.db.count(RUN, {"installation": keeper}), 2)
		self.assertEqual(frappe.db.count(RUN, {"installation": loser}), 0)

	def test_merge_is_idempotent(self):
		"""Re-running the cleanup is a no-op: nothing left to merge, and the
		surviving row is untouched."""
		from jarvis.patches import v2_13_unique_agent_installation as patch_mod

		self._seed(enabled=0, activation_state="shadow", runs=1)
		self._seed(enabled=1, activation_state="shadow", runs=1)
		frappe.db.commit()

		self.assertEqual(patch_mod._merge_duplicate_installs(), 1)
		frappe.db.commit()
		before = frappe.get_all(
			INSTALLATION,
			filters={"owner": self.owner, "agent": self.SLUG},
			fields=["name", "enabled", "activation_state"],
			ignore_permissions=True,
		)
		self.assertEqual(len(before), 1)

		self.assertEqual(patch_mod._merge_duplicate_installs(), 0)
		frappe.db.commit()
		after = frappe.get_all(
			INSTALLATION,
			filters={"owner": self.owner, "agent": self.SLUG},
			fields=["name", "enabled", "activation_state"],
			ignore_permissions=True,
		)
		self.assertEqual(after, before)
		self.assertEqual(frappe.db.count(RUN, {"installation": before[0]["name"]}), 2)

	def test_the_alter_table_would_fail_without_the_merge(self):
		"""Why the ordering is mandatory: with live duplicates present the unique
		index cannot be created at all, so a patch that skipped the cleanup would
		hard-fail migrate on exactly the benches that hit the race."""
		self._seed()
		self._seed()
		frappe.db.commit()
		with self.assertRaises(Exception):
			_add_owner_agent_index()
		frappe.db.rollback()

		from jarvis.patches import v2_13_unique_agent_installation as patch_mod

		patch_mod._merge_duplicate_installs()
		frappe.db.commit()
		_add_owner_agent_index()
		self.assertTrue(_owner_agent_index())


# --------------------------------------------------------------------------- #
# #457 — push gate and dispatch gate agree on identity and status
# --------------------------------------------------------------------------- #
GATE_ROLE = "Jarvis Hardening Gate Role"


def _mk_role(name: str) -> str:
	if not frappe.db.exists("Role", name):
		doc = frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1})
		doc.flags.ignore_permissions = True
		doc.insert()
	return name


def _set_roles(email: str, role: str, *, held: bool) -> None:
	user = frappe.get_doc("User", email)
	has = any(r.role == role for r in user.roles)
	if held and not has:
		user.append("roles", {"role": role})
	elif not held and has:
		user.set("roles", [r for r in user.roles if r.role != role])
	else:
		return
	user.flags.ignore_permissions = True
	user.save()
	frappe.clear_cache(user=email)


def _restrict(slug: str, roles: list[str]) -> None:
	"""Narrow the listing to exactly ``roles``, and to no named user.

	Both halves (jarvis#1062): leaving allowed_users populated would mean a test
	that restricts an agent to a role nobody holds still admits everyone named
	there, and the gate it is asserting would never fire."""
	doc = frappe.get_doc(LISTING, slug)
	doc.set("allowed_roles", [{"role": r} for r in roles])
	doc.set("allowed_users", [])
	doc.flags.ignore_permissions = True
	doc.save()


def _mk_gate_install(owner: str, slug: str, run_as: str) -> str:
	doc = frappe.get_doc(
		{
			"doctype": INSTALLATION,
			"agent": slug,
			"run_as_user": run_as,
			"reviewer": owner,
			"enabled": 1,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(INSTALLATION, doc.name, "owner", owner, update_modified=False)
	return doc.name


class TestAgentPushGateIdentity(FrappeTestCase):
	"""#457: the push must advertise exactly the set the dispatch gates will run.
	Gotcha #8 settles which identity decides — the EXECUTING one — so the push now
	gates on ``run_as_user``, matching ``agent_scheduler.run_due_agent_audits`` and
	``agents_api.run_agent_now``."""

	SLUG = PREFIX + "pushid"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-pushid-owner@example.com")
		cls.runner = _mk_user("h-pushid-runner@example.com")
		_mk_role(GATE_ROLE)
		_mk_listing(cls.SLUG)
		frappe.db.set_value(LISTING, cls.SLUG, "status", "Published", update_modified=False)
		_restrict(cls.SLUG, [GATE_ROLE])
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.set_value(LISTING, self.SLUG, "status", "Published", update_modified=False)
		_restrict(self.SLUG, [GATE_ROLE])

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		for u in (self.owner, self.runner):
			_set_roles(u, GATE_ROLE, held=False)
		frappe.db.commit()

	def _slugs(self):
		from jarvis.chat.agent_catalog import build_agent_push_payload

		return [e["slug"] for e in build_agent_push_payload(owner=self.owner)]

	def test_pushed_when_the_run_as_user_holds_the_role_and_the_owner_does_not(self):
		"""D1 in the issue: the owner lost the restricting role while the run-as
		user kept it. Dispatch has always allowed this; the push used to drop it, so
		the bench ran an agent its own roster did not list."""
		_set_roles(self.owner, GATE_ROLE, held=False)
		_set_roles(self.runner, GATE_ROLE, held=True)
		_mk_gate_install(self.owner, self.SLUG, self.runner)
		frappe.db.commit()
		self.assertIn(f"agent-{self.SLUG}", self._slugs())

	def test_not_pushed_when_the_owner_holds_the_role_and_the_run_as_user_does_not(self):
		"""The mirror case: dispatch refuses this install at every cadence, so
		advertising it would seat a delegate the bench will never run."""
		_set_roles(self.owner, GATE_ROLE, held=True)
		_set_roles(self.runner, GATE_ROLE, held=False)
		_mk_gate_install(self.owner, self.SLUG, self.runner)
		frappe.db.commit()
		self.assertNotIn(f"agent-{self.SLUG}", self._slugs())

	def test_self_mapped_install_is_unaffected(self):
		"""Control: run_as_user defaults to the installer, so for every ordinary
		install owner == run-as user and the change is a no-op."""
		_set_roles(self.owner, GATE_ROLE, held=True)
		_mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.commit()
		self.assertIn(f"agent-{self.SLUG}", self._slugs())

	def test_unpublished_listing_is_never_pushed(self):
		"""Unchanged, and the reason the dispatch gate had to learn the same rule."""
		_set_roles(self.owner, GATE_ROLE, held=True)
		_mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.set_value(LISTING, self.SLUG, "status", "Deprecated", update_modified=False)
		frappe.db.commit()
		self.assertNotIn(f"agent-{self.SLUG}", self._slugs())


class TestAgentDispatchStatusGate(FrappeTestCase):
	"""#457: an unpublished listing must not dispatch. Before this, ``status`` was
	read only by ``install_agent``, so deprecating a live installed agent left its
	schedule firing into a container that no longer had the delegate — three hours
	of ``running`` per cadence, terminalized by the stale-run sweep as a duration
	timeout it never hit."""

	SLUG = PREFIX + "gate"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-gate-owner@example.com")
		_mk_listing(cls.SLUG)
		frappe.db.set_value(
			LISTING, cls.SLUG, {"status": "Published", "nature": "Auditor"}, update_modified=False
		)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.set_value(
			LISTING, self.SLUG, {"status": "Published", "nature": "Auditor"}, update_modified=False
		)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.commit()

	def _due_install(self) -> str:
		from frappe.utils import add_days, now_datetime

		name = _mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.set_value(
			INSTALLATION,
			name,
			{
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"next_run_at": add_days(now_datetime(), -1),
			},
			update_modified=False,
		)
		frappe.db.commit()
		return name

	def _run_cron(self):
		"""Run the hourly cron with only OUR install due (this is a shared dev site,
		and other modules leave due rows behind), and with the launch stubbed."""
		from frappe.utils import add_days, now_datetime

		now = now_datetime()
		parked = {
			r.name: r.next_run_at
			for r in frappe.get_all(
				INSTALLATION,
				filters={
					"enabled": 1,
					"schedule_enabled": 1,
					"next_run_at": ["<=", now],
					"agent": ["!=", self.SLUG],
				},
				fields=["name", "next_run_at"],
				ignore_permissions=True,
			)
		}
		for n in parked:
			frappe.db.set_value(INSTALLATION, n, "next_run_at", add_days(now, 2), update_modified=False)
		frappe.db.commit()
		calls = []
		try:
			with patch.object(
				agent_scheduler,
				"_launch_audit",
				side_effect=lambda inst, **kw: calls.append((inst.name, frappe.session.user)),
			):
				agent_scheduler.run_due_agent_audits()
		finally:
			for n, ts in parked.items():
				frappe.db.set_value(INSTALLATION, n, "next_run_at", ts, update_modified=False)
			frappe.db.commit()
		return calls

	def test_deprecated_listing_does_not_dispatch_on_the_cron(self):
		"""The reported phantom-run path (D2): an admin deprecates an installed,
		enabled, scheduled auditor. No turn is dispatched, the reason is recorded on
		a ``failed`` run the customer can see, and the slot is CONSUMED so the
		cadence does not busy-retry hourly."""
		from frappe.utils import now_datetime

		now = now_datetime()
		inst = self._due_install()
		frappe.db.set_value(LISTING, self.SLUG, "status", "Deprecated", update_modified=False)
		frappe.db.commit()

		self.assertEqual(self._run_cron(), [])
		runs = frappe.get_all(
			RUN,
			filters={"installation": inst, "status": "failed"},
			fields=["owner", "error"],
			ignore_permissions=True,
		)
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0]["owner"], self.owner)
		self.assertIn("no longer published", runs[0]["error"])
		row = frappe.db.get_value(INSTALLATION, inst, ["next_run_at", "last_run_at"], as_dict=True)
		self.assertIsNotNone(row.last_run_at)
		self.assertGreater(row.next_run_at, now)

	def test_published_listing_still_dispatches_as_the_run_as_user(self):
		"""Control: the legitimate case is untouched, and still launches under the
		RUN-AS identity (the S1 hinge), not the scheduler's Administrator."""
		inst = self._due_install()
		calls = self._run_cron()
		self.assertEqual(calls, [(inst, self.owner)])
		self.assertEqual(
			frappe.get_all(RUN, filters={"installation": inst, "status": "failed"}, ignore_permissions=True),
			[],
		)

	def test_launch_audit_refuses_a_deprecated_listing_and_creates_no_rows(self):
		"""The choke point both dispatch paths funnel through: no caller can get a
		run started for an unpublished agent, and a refused launch leaves no orphan
		conversation or run behind."""
		inst = _mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.set_value(LISTING, self.SLUG, "status", "Deprecated", update_modified=False)
		frappe.db.commit()
		doc = frappe.get_doc(INSTALLATION, inst)
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				agent_scheduler._launch_audit(doc, trigger="scheduled")
		finally:
			frappe.set_user("Administrator")
		self.assertIn("no longer published", str(ctx.exception))
		self.assertEqual(frappe.get_all(RUN, filters={"installation": inst}, ignore_permissions=True), [])

	def test_launch_audit_lets_a_published_listing_past_the_status_gate(self):
		"""Control + ordering: with the listing Published the launch proceeds to the
		JF-017 bundle check, proving the new gate is not refusing everything and
		that it sits BEFORE the bundle-configuration diagnosis."""
		inst = _mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.commit()
		doc = frappe.get_doc(INSTALLATION, inst)
		frappe.set_user(self.owner)
		try:
			with (
				patch("jarvis.chat.agent_catalog.registry_tools_allow", return_value=[]),
				self.assertRaises(frappe.ValidationError) as ctx,
			):
				agent_scheduler._launch_audit(doc, trigger="scheduled")
		finally:
			frappe.set_user("Administrator")
		self.assertIn("declares no tools", str(ctx.exception))

	def test_run_agent_now_refuses_a_deprecated_listing(self):
		"""The manual path answers with the availability reason too, before it
		spends a budget slot or persists anything."""
		inst = _mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.set_value(LISTING, self.SLUG, "status", "Deprecated", update_modified=False)
		frappe.db.commit()
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				agents_api.run_agent_now(inst)
		finally:
			frappe.set_user("Administrator")
		self.assertIn("no longer published", str(ctx.exception))


class TestSetListingStatusMarksCatalogDirty(FrappeTestCase):
	"""#457: ``set_listing_status`` changes what the push emits, so it must mark an
	Apply pending like every other push-visible mutation. Without this the SPA
	showed "synced" while the container roster still carried the deprecated slug."""

	SLUG = PREFIX + "statusdirty"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-statusdirty-owner@example.com")
		_mk_listing(cls.SLUG)
		cls._saved = {k: _single(k) for k in ("agent_catalog_dirty", "agent_catalog_version")}

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for k, v in cls._saved.items():
			frappe.db.set_single_value(SETTINGS, k, v)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.set_value(LISTING, self.SLUG, "status", "Published", update_modified=False)
		frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 0)
		frappe.db.set_single_value(SETTINGS, "agent_catalog_version", 3)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.commit()

	def test_deprecating_an_enabled_agent_marks_an_apply_pending(self):
		_mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.commit()
		agents_api.set_listing_status(self.SLUG, "Deprecated")
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 1)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_version")), 4)

	def test_status_change_with_no_enabled_install_is_not_dirty(self):
		"""The payload is identical either way, so do not manufacture a pending
		Apply (and a container restart) out of nothing.

		The listing must carry NO grant either (jarvis#1062): a granted listing is
		in the roster whether or not anyone installed it, so a status flip on one
		DOES change the payload - see the sibling test below. _mk_listing grants
		Jarvis User so the module's other tests can install, hence the reset here."""
		clear_listing_access(self.SLUG)
		agents_api.set_listing_status(self.SLUG, "Deprecated")
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 0)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_version")), 3)

	def test_status_change_with_a_grant_but_no_install_is_dirty(self):
		"""jarvis#1062: the roster is no longer "enabled installs" alone.

		An allowed Published listing ships with zero installations, so
		publishing/deprecating one really does move the container roster and must
		show as a pending Apply - the exact #457 class of bug (roster and DB
		silently disagreeing) in its new shape."""
		allow_listing_for(self.SLUG, roles=["Jarvis User"])
		agents_api.set_listing_status(self.SLUG, "Deprecated")
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 1)

	def test_setting_the_same_status_is_not_dirty(self):
		_mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.commit()
		agents_api.set_listing_status(self.SLUG, "Published")
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 0)


class TestSetEnabledMarksCatalogDirtyOnlyOnAChange(FrappeTestCase):
	"""jarvis#1062 polish: a no-op ``set_enabled`` call (re-enabling an
	already-enabled row, or disabling an already-disabled one) was dirtying the
	catalog unconditionally, unlike every other push-visible mutation here
	(``set_listing_status`` above) - the SPA's leave-guard then nagged with a
	native beforeunload prompt even though nothing the next Apply would push
	had actually changed."""

	SLUG = PREFIX + "enableddirty"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-enableddirty-owner@example.com")
		_mk_listing(cls.SLUG)
		cls._saved = {k: _single(k) for k in ("agent_catalog_dirty", "agent_catalog_version")}

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for k, v in cls._saved.items():
			frappe.db.set_single_value(SETTINGS, k, v)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.set_value(LISTING, self.SLUG, "status", "Published", update_modified=False)
		frappe.db.set_single_value(SETTINGS, "agent_catalog_dirty", 0)
		frappe.db.set_single_value(SETTINGS, "agent_catalog_version", 3)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.commit()

	def test_re_enabling_an_already_enabled_install_is_not_dirty(self):
		inst = _mk_gate_install(self.owner, self.SLUG, self.owner)  # starts enabled=1
		frappe.db.commit()
		agents_api.set_enabled(inst, 1)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 0)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_version")), 3)

	def test_disabling_an_already_disabled_install_is_not_dirty(self):
		inst = _mk_gate_install(self.owner, self.SLUG, self.owner)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 0, update_modified=False)
		frappe.db.commit()
		agents_api.set_enabled(inst, 0)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 0)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_version")), 3)

	def test_an_actual_flip_still_marks_dirty(self):
		"""Control: the fix must not have swallowed the real case."""
		inst = _mk_gate_install(self.owner, self.SLUG, self.owner)  # starts enabled=1
		frappe.db.commit()
		agents_api.set_enabled(inst, 0)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_dirty")), 1)
		self.assertEqual(frappe.utils.cint(_single("agent_catalog_version")), 4)


# --------------------------------------------------------------------------- #
# #648 — schedule_time is validated on every surface, and one bad installation
# cannot abort the whole hourly agent sweep (the agent twin of #472)
# --------------------------------------------------------------------------- #
class TestAgentScheduleTimeAndSweepIsolation(FrappeTestCase):
	"""``schedule_time`` was litigated only by ``agents_api.set_schedule``, so a Desk
	edit, a data import or a direct ``doc.save()`` could persist a value the sweep
	could not honour. Separately, ``run_due_agent_audits`` guarded only the DISPATCH,
	so anything the dedupe, the arithmetic or the bookkeeping raised took down every
	installation behind it, permanently, since the next tick died on the same row.

	Two agent slugs, not two installs of one agent: #460 added a unique index on
	(owner, agent), so a second install of the same agent for the same owner is a
	duplicate-key error rather than a second due row."""

	SLUG = PREFIX + "schedtime"
	SLUG2 = PREFIX + "schedtime2"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner = _mk_user("h-schedtime-owner@example.com")
		for slug in (cls.SLUG, cls.SLUG2):
			_mk_listing(slug)
			frappe.db.set_value(
				LISTING, slug, {"status": "Published", "nature": "Auditor"}, update_modified=False
			)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG, self.SLUG2])
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG, self.SLUG2])
		frappe.db.commit()

	def _install(self, slug: str | None = None, **overrides) -> str:
		from frappe.utils import add_days, now_datetime

		name = _mk_gate_install(self.owner, slug or self.SLUG, self.owner)
		values = {
			"schedule_enabled": 1,
			"schedule_frequency": "daily",
			"next_run_at": add_days(now_datetime(), -1),
		}
		values.update(overrides)
		frappe.db.set_value(INSTALLATION, name, values, update_modified=False)
		frappe.db.commit()
		return name

	def _run_cron(self, launch_side_effect=None):
		"""Run the hourly cron with only OUR installs due (shared dev site), launch stubbed."""
		from frappe.utils import add_days, now_datetime

		now = now_datetime()
		parked = {
			r.name: r.next_run_at
			for r in frappe.get_all(
				INSTALLATION,
				filters={
					"enabled": 1,
					"schedule_enabled": 1,
					"next_run_at": ["<=", now],
					"agent": ["not in", [self.SLUG, self.SLUG2]],
				},
				fields=["name", "next_run_at"],
				ignore_permissions=True,
			)
		}
		for n in parked:
			frappe.db.set_value(INSTALLATION, n, "next_run_at", add_days(now, 2), update_modified=False)
		frappe.db.commit()
		calls = []

		def _default(inst, **kw):
			calls.append(inst.name)

		try:
			with patch.object(agent_scheduler, "_launch_audit", side_effect=launch_side_effect or _default):
				agent_scheduler.run_due_agent_audits()
		finally:
			for n, ts in parked.items():
				frappe.db.set_value(INSTALLATION, n, "next_run_at", ts, update_modified=False)
			frappe.db.commit()
		return calls

	# ---- controller validation (gap 1) ---------------------------------------- #

	def test_out_of_range_schedule_time_is_refused_on_every_write_surface(self):
		"""``frappe.ValidationError`` is what the SPA renders as a field error. Before
		this the value persisted silently and the scheduler quietly used 09:00."""
		for bad in ("99:00:00", "-01:00:00", "24:00:00", "12:99:00", "not-a-time", "1:2:3:4"):
			with self.subTest(bad=bad):
				doc = frappe.get_doc(
					{
						"doctype": INSTALLATION,
						"agent": self.SLUG,
						"run_as_user": self.owner,
						"reviewer": self.owner,
						"enabled": 1,
						"schedule_enabled": 1,
						"schedule_frequency": "daily",
						"schedule_time": bad,
					}
				)
				doc.flags.ignore_permissions = True
				with self.assertRaises(frappe.ValidationError):
					doc.insert(ignore_permissions=True)

	def test_a_bad_time_is_refused_even_with_the_schedule_off(self):
		"""The latent limb: with ``schedule_enabled=0`` nothing consumed the value, so
		the garbage persisted and armed the sweep for whenever the schedule was flipped
		on. MariaDB TIME accepts up to 838:59:59, so storage was never the guard."""
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"agent": self.SLUG,
				"run_as_user": self.owner,
				"reviewer": self.owner,
				"enabled": 1,
				"schedule_enabled": 0,
				"schedule_time": "99:00:00",
			}
		)
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)

	def test_valid_schedule_times_are_accepted(self):
		# Compared as SECONDS, not as strings: MariaDB hands a TIME column back as a
		# datetime.timedelta, and str(timedelta(0)) is '0:00:00', not '00:00:00'. The
		# stored value is right either way, so a string compare would only be asserting
		# timedelta's formatting.
		from jarvis.chat.macro_scheduler import parse_schedule_seconds

		for good, secs in (("00:00:00", 0), ("09:30:00", 34200), ("23:59:59", 86399)):
			with self.subTest(good=good):
				_wipe([self.SLUG, self.SLUG2])
				name = self._install(schedule_time=good)
				stored = frappe.db.get_value(INSTALLATION, name, "schedule_time")
				self.assertEqual(parse_schedule_seconds(stored), secs)

	def test_microsecond_schedule_time_parses_as_a_time_of_day(self):
		"""Frappe 15's ``create_new`` stamps EVERY Time field of a new doc with
		``nowtime()`` unconditionally, formatted with microseconds ("%H:%M:%S.%f"),
		so a freshly inserted installation reaches ``validate`` holding a value the
		caller never set. That value IS a real time of day and must parse (the
		fraction dropped), or no agent could be installed on Frappe 15. Garbage is
		still refused. Frappe 16 injects the default only for ``default = "now"``
		fields, so the value is ``None`` there -- which is why CI (Frappe 16) never
		caught the break."""
		from jarvis.chat.macro_scheduler import parse_schedule_seconds

		self.assertEqual(parse_schedule_seconds("12:53:02.208095"), 46382)
		self.assertEqual(parse_schedule_seconds("00:00:00.000000"), 0)
		self.assertEqual(parse_schedule_seconds("23:59:59.999999"), 86399)
		# Still rejected: out of range, a fraction on the wrong component, and a
		# non-digit fraction are all garbage, not a time of day.
		self.assertIsNone(parse_schedule_seconds("99:00:00.5"))
		self.assertIsNone(parse_schedule_seconds("12.5:00"))
		self.assertIsNone(parse_schedule_seconds("12:00:00.abc"))

	def test_install_without_a_schedule_time_is_accepted(self):
		"""End-to-end regression for the Frappe-15 install break: creating an
		installation WITHOUT a ``schedule_time`` must not throw. Pre-fix the
		framework-injected microsecond ``nowtime()`` failed the schedule_time
		validation and NO agent could be installed on a Frappe-15 customer bench.
		The stored value must read back as a real time of day or be empty."""
		from jarvis.chat.macro_scheduler import parse_schedule_seconds

		# _install -> _mk_gate_install -> get_doc(...).insert() with no schedule_time,
		# so the insert exercises the framework default-injection path.
		name = self._install()
		self.assertTrue(frappe.db.exists(INSTALLATION, name))
		stored = frappe.db.get_value(INSTALLATION, name, "schedule_time")
		if stored not in (None, ""):
			self.assertIsNotNone(
				parse_schedule_seconds(stored),
				"a framework-injected schedule_time must read back as a time of day",
			)

	def test_a_row_holding_a_legacy_bad_time_cannot_be_saved_by_the_DB_either(self):
		"""Review asked whether validating on EVERY save strands a row that already
		holds a bad value, since ``set_enabled`` / ``set_config`` go through
		``doc.save()``. It cannot, and this pins why, so nobody adds a
		``has_value_changed`` branch for a state that cannot exist.

		The only values the check rejects that MariaDB will still STORE are
		``>= 24:00:00``. A TIME column hands those back as a ``timedelta`` carrying a
		days component, Frappe writes that back as ``"4 days, 3:00:00"``, and MariaDB
		refuses it with error 1292 before any controller runs. So such a row is
		un-saveable regardless of this validation."""
		from jarvis.chat.macro_scheduler import parse_schedule_seconds

		name = self._install()
		frappe.db.sql(
			"UPDATE `tabJarvis Agent Installation` SET schedule_time='99:00:00' WHERE name=%(n)s",
			{"n": name},
		)
		frappe.db.commit()

		# Read it back: the value is a timedelta with days, not a time of day.
		stored = frappe.db.get_value(INSTALLATION, name, "schedule_time")
		self.assertIsNone(parse_schedule_seconds(stored), "99:00:00 must not read back as a time of day")
		self.assertIn("day", str(stored), "MariaDB returns an out-of-range TIME as a timedelta")

	def test_writing_a_bad_time_onto_an_existing_row_is_refused(self):
		"""An existing, healthy row must not be able to take a bad value either: the
		check runs on every save, not only on insert."""
		name = self._install(schedule_time="09:00:00")
		doc = frappe.get_doc(INSTALLATION, name)
		doc.schedule_time = "99:00:00"
		doc.flags.ignore_permissions = True
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	# ---- sweep fault isolation (gap 2) ---------------------------------------- #

	def test_one_exploding_installation_does_not_abort_the_sweep(self):
		"""The structural guarantee, independent of ``schedule_time``: whatever a single
		row raises, every row behind it still gets its slot. ``_record_failed``,
		``_notify_owner``, a deleted run-as user and a failing ``_launch_audit`` whose own
		handler then raises are all live ways one row can throw."""
		from frappe.utils import add_days, now_datetime

		boom = self._install(self.SLUG)
		good = self._install(self.SLUG2)
		# Force the exploding row to the FRONT: get_all has no explicit order_by here, so
		# pin it by making it the older next_run_at, which is the natural due ordering.
		frappe.db.set_value(
			INSTALLATION, boom, "next_run_at", add_days(now_datetime(), -3), update_modified=False
		)
		frappe.db.commit()

		seen = []

		def _explode(inst, **kw):
			seen.append(inst.name)
			if inst.name == boom:
				raise RuntimeError("poisoned installation")

		self._run_cron(launch_side_effect=_explode)

		self.assertIn(good, seen, "one exploding installation aborted the sweep for every other row")

	def test_a_bad_persisted_time_does_not_abort_the_sweep(self):
		"""Recreate a row saved BEFORE the controller check existed by writing past it,
		then confirm the sweep still reaches the healthy installation behind it."""
		from frappe.utils import add_days, now_datetime

		bad = self._install(self.SLUG)
		good = self._install(self.SLUG2)
		frappe.db.sql(
			"UPDATE `tabJarvis Agent Installation` SET schedule_time='99:00:00', "
			"next_run_at=%(t)s WHERE name=%(n)s",
			{"t": add_days(now_datetime(), -3), "n": bad},
		)
		frappe.db.commit()

		dispatched = self._run_cron()

		self.assertIn(good, dispatched, "a bad persisted time aborted the sweep for every other row")


# --------------------------------------------------------------------------- #
# #615 - promote/demote must not re-home a foreign owner's finding
# --------------------------------------------------------------------------- #
class TestRehomeMembershipScope(FrappeTestCase):
	"""``_rehome_installation_outputs`` built its membership set from ``run``,
	``first_seen_run`` AND ``last_seen_run``. That third pointer is the only one the
	engine re-points, and the recurrence bump that re-points it matches on
	``(owner, agent, fingerprint)``, so under PP-4 shadow re-homing it can attach
	another owner's finding to one of our runs. Promoting or demoting then rewrote
	that foreign row's visibility ``owner``.

	Same unsafe signal #455/#612 removed from the uninstall cascade, different
	consequence: the cascade DELETED such a row, this path makes a foreign customer's
	finding appear under the wrong owner."""

	SLUG = PREFIX + "rehome"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.owner_a = _mk_user("h-rehome-a@example.com")
		cls.owner_b = _mk_user("h-rehome-b@example.com")
		cls.reviewer = _mk_user("h-rehome-rev@example.com")
		_mk_listing(cls.SLUG)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		# Two installations of the SAME agent sharing one reviewer, so PP-4 re-homing
		# has already put both owners' findings under that reviewer.
		self.inst_a = _mk_install(self.owner_a, self.SLUG, self.reviewer)
		self.inst_b = _mk_install(self.owner_b, self.SLUG, self.reviewer)
		self.run_a = _mk_run(self.inst_a.name, self.SLUG, self.reviewer)
		self.run_b = _mk_run(self.inst_b.name, self.SLUG, self.reviewer)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		_wipe([self.SLUG])
		frappe.db.commit()

	def test_a_foreign_finding_bumped_onto_our_run_is_not_rehomed(self):
		"""THE regression. B's finding was CREATED by B's installation (run +
		first_seen_run both point at B's run) but a recurrence bump re-pointed its
		last_seen_run onto A's run. Re-homing A must leave its owner alone."""
		foreign = _mk_finding(
			self.reviewer,
			self.SLUG,
			run=self.run_b,
			first_seen_run=self.run_b,
			last_seen_run=self.run_a,  # the bump, the only pointer the engine moves
		).name
		frappe.db.commit()

		agents_api._rehome_installation_outputs(self.inst_a, self.owner_a)

		self.assertEqual(
			frappe.db.get_value(FINDING, foreign, "owner"),
			self.reviewer,
			"a finding created by another installation must not be re-homed",
		)

	def test_our_own_findings_are_still_rehomed(self):
		"""Control: the function must still do its job, or the fix is just a break."""
		mine = _mk_finding(
			self.reviewer,
			self.SLUG,
			run=self.run_a,
			first_seen_run=self.run_a,
			last_seen_run=self.run_a,
		).name
		frappe.db.commit()

		agents_api._rehome_installation_outputs(self.inst_a, self.owner_a)

		self.assertEqual(frappe.db.get_value(FINDING, mine, "owner"), self.owner_a)
		self.assertEqual(frappe.db.get_value(RUN, self.run_a, "owner"), self.owner_a, "runs re-home too")

	def test_a_finding_created_on_our_run_but_bumped_elsewhere_is_rehomed(self):
		"""The mirror case: origin is what counts, not where the bump currently points.
		A row A's installation created stays A's even though its last_seen_run has since
		moved onto B's run."""
		mine = _mk_finding(
			self.reviewer,
			self.SLUG,
			run=self.run_a,
			first_seen_run=self.run_a,
			last_seen_run=self.run_b,
		).name
		frappe.db.commit()

		agents_api._rehome_installation_outputs(self.inst_a, self.owner_a)

		self.assertEqual(frappe.db.get_value(FINDING, mine, "owner"), self.owner_a)

	def test_b_findings_are_untouched_when_a_is_rehomed(self):
		"""Nothing belonging to the co-reviewed installation moves at all."""
		theirs = _mk_finding(
			self.reviewer,
			self.SLUG,
			run=self.run_b,
			first_seen_run=self.run_b,
			last_seen_run=self.run_b,
		).name
		frappe.db.commit()

		agents_api._rehome_installation_outputs(self.inst_a, self.owner_a)

		self.assertEqual(frappe.db.get_value(FINDING, theirs, "owner"), self.reviewer)
		self.assertEqual(frappe.db.get_value(RUN, self.run_b, "owner"), self.reviewer)
