"""PP-5 — launch-time provenance is STAMPED at run creation, not just guarded.

The Round-4 panel found that ``_launch_audit`` created the ``Jarvis Agent Run``
without ever setting the three immutable launch facts
(``bundle_version`` / ``preparation_mode`` / ``initiating_human``), so they were
always empty in production: ``preparation_mode`` never snapshotted the
installation's ``activation_state``, ``initiating_human`` was unrecoverable on
manual runs, and the controller's set-once guard could never engage because the
stored value was always empty.

These tests launch a REAL run through the shared ``_launch_audit`` path (the
exact path the scheduler and ``run_agent_now`` take) and assert the stamped
values — not merely that the controller guard fires when a value is pre-injected
(that is covered by ``test_platform_writeback``). ``admin_client.post_agent_run``
is stubbed so the dispatch returns without a live fleet, mirroring
``test_agent_identity``.

JF-021 extends this: the stamped ``initiating_human`` was read from
``frappe.session.user`` INSIDE the launch, i.e. AFTER both paths impersonate the
installation's ``run_as_user`` — so a manual run triggered by a System Manager on
someone else's install permanently recorded the run-as user as the person who
asked for it. The identity is now passed in explicitly and verified against the
authenticated (pre-impersonation) session user, so the three identities a run has
— triggerer / row owner / run-as — stay distinct and correct.
"""

import unittest
from unittest import mock

import frappe

from jarvis._session import authenticated_user, impersonate
from jarvis.chat import agent_catalog, agent_scheduler, agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
CONV = "Jarvis Conversation"
SESSION = "Jarvis Chat Session"
AGENT = "close-auditor"


def _ensure_user(email: str, extra_roles: tuple = ()) -> str:
	from jarvis.permissions import ensure_jarvis_user_role

	ensure_jarvis_user_role()
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
		u.flags.ignore_permissions = True
		u.insert()
	frappe.db.set_value("User", email, "enabled", 1, update_modified=False)
	have = set(frappe.get_roles(email))
	want = {"Jarvis User", *extra_roles}
	missing = [r for r in want if r not in have]
	if missing:
		frappe.get_doc("User", email).add_roles(*missing)
	# close-auditor declares doctypes_required (GL Entry / Account / Company); the
	# run-as A12 gate needs the run-as user to hold those reads. Accounts User grants
	# them without conferring Jarvis roles.
	if frappe.db.exists("Role", "Accounts User") and "Accounts User" not in have:
		frappe.get_doc("User", email).add_roles("Accounts User")
	frappe.clear_cache(user=email)
	frappe.db.commit()
	return email


def _install_as(owner: str) -> str:
	original = frappe.session.user
	frappe.set_user(owner)
	try:
		return agents_api.install_agent(AGENT)["data"]["name"]
	finally:
		frappe.set_user(original)


class TestPlatformLaunchProvenance(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		agent_catalog.sync_agent_listings()
		cls.owner = _ensure_user("plp-owner@example.com")
		# JF-021 three-identity cast: A triggers (a System Manager, so it may trigger
		# someone else's install), B owns the install, C is the run-as identity.
		cls.triggerer = _ensure_user("plp-triggerer@example.com", ("System Manager",))
		cls.runas = _ensure_user("plp-runas@example.com")
		cls.listing_version = frappe.db.get_value(LISTING, AGENT, "version")

	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup()
		# Stub the fleet dispatch so _launch_audit completes without a live fleet.
		import jarvis.admin_client as admin_client

		self._orig_post = admin_client.post_agent_run
		admin_client.post_agent_run = lambda **kw: {"run_id": kw.get("run_id"), "status": "queued"}

	def tearDown(self):
		import jarvis.admin_client as admin_client

		admin_client.post_agent_run = self._orig_post
		frappe.set_user("Administrator")
		frappe.db.rollback()
		self._cleanup()

	def _cleanup(self):
		frappe.db.delete(
			"Jarvis Agent Allowed Role",
			{"parenttype": LISTING, "parentfield": "allowed_roles"},
		)
		# Rows land on the owner (reassigned) but a run-as launch mints its per-run
		# session under the RUN-AS user, so every identity in the cast is swept.
		cast = (self.owner, self.triggerer, self.runas)
		for dt in (RUN, INSTALLATION):
			for n in frappe.get_all(dt, filters={"owner": ["in", cast]}, pluck="name"):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		for n in frappe.get_all(SESSION, filters={"user": ["in", cast]}, pluck="name"):
			frappe.delete_doc(SESSION, n, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ------------------------------------------------------------------ #
	# A MANUAL launch stamps all three immutable facts (PP-5 acceptance 1)
	# ------------------------------------------------------------------ #
	def test_manual_launch_stamps_all_three_facts(self):
		inst_name = _install_as(self.owner)  # self-map: run_as == owner == triggerer
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		frappe.set_user(self.owner)
		try:
			result = agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
		run = result["run"]

		vals = frappe.db.get_value(
			RUN, run, ["bundle_version", "preparation_mode", "initiating_human"], as_dict=True
		)
		# bundle_version is the SNAPSHOT of the installed version (== listing.version
		# at install), stamped even though listing/installation versions are mutable.
		self.assertEqual(vals.bundle_version, inst.installed_version)
		self.assertEqual(vals.bundle_version, self.listing_version)
		# preparation_mode snapshots the installation's activation_state (fresh
		# install is always shadow — PP-4).
		self.assertEqual(vals.preparation_mode, "shadow")
		# initiating_human is the human who triggered the manual run (self-mapped, so
		# the run-as session user IS the triggerer here).
		self.assertEqual(vals.initiating_human, self.owner)

	# ------------------------------------------------------------------ #
	# A SCHEDULED launch stamps bundle/prep but NO initiating_human
	# ------------------------------------------------------------------ #
	def test_scheduled_launch_has_no_initiating_human(self):
		inst_name = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		# Mirror the scheduler's S1 hinge: the audit is created inside set_user(run_as).
		frappe.set_user(self.owner)
		try:
			result = agent_scheduler._launch_audit(inst, trigger="scheduled")
		finally:
			frappe.set_user("Administrator")
		run = result["run"]

		vals = frappe.db.get_value(
			RUN, run, ["bundle_version", "preparation_mode", "initiating_human"], as_dict=True
		)
		self.assertEqual(vals.bundle_version, inst.installed_version)
		self.assertEqual(vals.preparation_mode, "shadow")
		# No human initiated a cron run.
		self.assertFalse(vals.initiating_human)

	# ------------------------------------------------------------------ #
	# The STAMPED value now engages the controller's set-once guard
	# (the finding: the guard "never even engages because stored is empty")
	# ------------------------------------------------------------------ #
	def test_stamped_bundle_version_is_immutable(self):
		inst_name = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		frappe.set_user(self.owner)
		try:
			result = agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
		run = result["run"]

		# stored is now NON-empty, so the PP-5 guard engages on an ORM re-save.
		doc = frappe.get_doc(RUN, run)
		doc.bundle_version = "tampered-9.9.9"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)

	# ------------------------------------------------------------------ #
	# preparation_mode is a SNAPSHOT: a later promotion does not rewrite it
	# ------------------------------------------------------------------ #
	def test_preparation_mode_snapshots_shadow_even_when_installed_state_changes(self):
		inst_name = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		frappe.set_user(self.owner)
		try:
			result = agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
		run = result["run"]

		self.assertEqual(frappe.db.get_value(RUN, run, "preparation_mode"), "shadow")
		# Even if the installation is later flipped live, the run's stamped snapshot
		# stays shadow (raw set_value on the install; the run field is immutable).
		frappe.db.set_value(INSTALLATION, inst_name, "activation_state", "live", update_modified=False)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(RUN, run, "preparation_mode"), "shadow")

	# ------------------------------------------------------------------ #
	# JF-021 — THE DEFECT: triggerer A, owner B, run-as C on a MANUAL run
	# ------------------------------------------------------------------ #
	def test_manual_run_records_the_triggerer_not_the_run_as_user(self):
		inst_name = _install_as(self.owner)  # B owns the install
		frappe.set_user("Administrator")
		agents_api.set_run_as_user(inst_name, self.runas)  # C is the executing identity
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()

		import jarvis.admin_client as admin_client

		captured = {}

		def _cap(**kw):
			captured["user"] = frappe.session.user
			return {"run_id": kw.get("run_id"), "status": "queued"}

		admin_client.post_agent_run = _cap
		frappe.set_user(self.triggerer)  # A (a System Manager) triggers B's install
		try:
			result = agents_api.run_agent_now(inst_name)
			# The caller's own session is intact after the impersonated launch.
			self.assertEqual(frappe.session.user, self.triggerer)
		finally:
			frappe.set_user("Administrator")
		run = result["data"]["run"]

		# THE FIX: the immutable provenance names the human who asked for the run...
		self.assertEqual(frappe.db.get_value(RUN, run, "initiating_human"), self.triggerer)
		# ...not the run-as user the session had been switched to (the old behaviour),
		# and not the row owner either.
		self.assertNotEqual(frappe.db.get_value(RUN, run, "initiating_human"), self.runas)
		self.assertNotEqual(frappe.db.get_value(RUN, run, "initiating_human"), self.owner)

		# The other two identities are UNCHANGED by the fix: the turn still dispatches
		# under the run-as user and the rows still belong to the human owner.
		self.assertEqual(captured.get("user"), self.runas)
		sk = frappe.db.get_value(RUN, run, "session_key")
		self.assertEqual(frappe.db.get_value(SESSION, {"session_key": sk}, "user"), self.runas)
		self.assertEqual(frappe.db.get_value(RUN, run, "owner"), self.owner)
		self.assertEqual(frappe.db.get_value(CONV, result["data"]["conversation"], "owner"), self.owner)

	# ------------------------------------------------------------------ #
	# JF-021 — an attribution that is not the authenticated triggerer is
	# REFUSED (never trusted), and refused before any row exists
	# ------------------------------------------------------------------ #
	def test_manual_launch_refuses_an_attribution_that_is_not_the_triggerer(self):
		inst_name = _install_as(self.owner)  # self-mapped: run_as == owner
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)
		convs_before = frappe.db.count(CONV)

		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.PermissionError):
				agent_scheduler._launch_audit(inst, trigger="manual", initiating_human=self.runas)
		finally:
			frappe.set_user("Administrator")

		# Refused BEFORE the conversation/run were inserted, so nothing is orphaned.
		self.assertEqual(frappe.get_all(RUN, filters={"installation": inst_name}, pluck="name"), [])
		self.assertEqual(frappe.db.count(CONV), convs_before)

	def test_manual_launch_refuses_an_unattributable_identity(self):
		"""Guest / a deleted user / a disabled user is not a human this run can be
		attributed to — the launch refuses rather than stamping it."""
		disabled = _ensure_user("plp-disabled@example.com")
		frappe.db.set_value("User", disabled, "enabled", 0, update_modified=False)
		frappe.db.commit()
		try:
			for who in ("Guest", "plp-ghost@example.com", disabled):
				with mock.patch.object(agent_scheduler, "authenticated_user", return_value=who):
					with self.assertRaises(frappe.ValidationError):
						agent_scheduler._resolve_initiating_human("manual", who)
		finally:
			frappe.db.set_value("User", disabled, "enabled", 1, update_modified=False)
			frappe.db.commit()

	# ------------------------------------------------------------------ #
	# JF-021 — scheduled attribution semantics are UNCHANGED
	# ------------------------------------------------------------------ #
	def test_scheduled_launch_refuses_an_initiating_human(self):
		inst_name = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		frappe.set_user(self.owner)
		try:
			# A cron run is unattended: claiming a human initiated it is refused, not
			# quietly stamped. (The scheduler itself always passes None.)
			with self.assertRaises(frappe.ValidationError):
				agent_scheduler._launch_audit(inst, trigger="scheduled", initiating_human=self.owner)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.get_all(RUN, filters={"installation": inst_name}, pluck="name"), [])
		self.assertIsNone(agent_scheduler._resolve_initiating_human("scheduled", None))

	# ------------------------------------------------------------------ #
	# JF-021 — the corrected value is still IMMUTABLE once stamped
	# ------------------------------------------------------------------ #
	def test_stamped_initiating_human_is_immutable(self):
		inst_name = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		frappe.set_user(self.owner)
		try:
			result = agent_scheduler._launch_audit(inst, trigger="manual", initiating_human=self.owner)
		finally:
			frappe.set_user("Administrator")
		run = result["run"]
		self.assertEqual(frappe.db.get_value(RUN, run, "initiating_human"), self.owner)

		doc = frappe.get_doc(RUN, run)
		doc.initiating_human = self.runas
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value(RUN, run, "initiating_human"), self.owner)


class TestAuthenticatedUserSeam(unittest.TestCase):
	"""JF-021 — ``impersonate`` must keep the AUTHENTICATED identity recoverable.

	This is the seam the launch validates against: provenance can only be checked
	server-side if "who is this request really" survives an identity switch.
	"""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.a = _ensure_user("plp-seam-a@example.com")
		cls.b = _ensure_user("plp-seam-b@example.com")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_authenticated_user_is_the_pre_impersonation_user(self):
		frappe.set_user(self.a)
		self.assertEqual(authenticated_user(), self.a)  # nothing impersonating
		with impersonate(self.b):
			self.assertEqual(frappe.session.user, self.b)
			self.assertEqual(authenticated_user(), self.a)
			with impersonate("Administrator"):
				# Nested switches never re-anchor the identity: the OUTERMOST wins.
				self.assertEqual(frappe.session.user, "Administrator")
				self.assertEqual(authenticated_user(), self.a)
			self.assertEqual(authenticated_user(), self.a)
		self.assertEqual(frappe.session.user, self.a)
		self.assertEqual(authenticated_user(), self.a)

	def test_marker_does_not_leak_when_the_block_raises(self):
		frappe.set_user(self.a)
		with self.assertRaises(RuntimeError):
			with impersonate(self.b):
				raise RuntimeError("boom")
		self.assertEqual(frappe.session.user, self.a)
		# A stale marker would keep answering "a" for every later caller in this
		# request/worker — including the next user's manual run.
		frappe.set_user(self.b)
		self.assertEqual(authenticated_user(), self.b)
