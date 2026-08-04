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

from jarvis.chat import agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
ACTIVITY = "Jarvis Agent Activity"
FINDING = "Jarvis Agent Finding"
PROVENANCE = "Jarvis Agent Provenance Event"
SETTINGS = "Jarvis Settings"

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
		frappe.db.value_cache[SETTINGS]["agent_catalog_version"] = 5
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
