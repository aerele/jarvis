"""Phase 1 (identity foundation) tests for agents-as-delegates.

Covers the verified identity contract wired at trigger:
  (a) the A13 backfill patch stamps run_as_user = owner on legacy rows (and
      skips-or-logs an invalid owner without aborting);
  (b) validate() (the authoritative escalation guard) rejects a non-admin
      mapping run_as_user to another non-SM user, and rejects a non-SM binding
      run_as_user to a System Manager — while an SM may map cross-user;
  (c) scoped_visibility is stamped when the run-as user carries a Cost Center
      User Permission (a GL-dimension slice);
  (d) _launch_audit mints a per-run Jarvis Chat Session bound to the run-as user
      and stamps session_key + the A17 GL watermark + the A6 scope on the Run;
  (e) a run executes AS the run-as user (impersonate), not the row owner — the
      minted session binds run_as_user, while Run/Conversation row ownership
      stays the human owner.

Run ONLY on a throwaway bench:
  bench --site patterntest.localhost run-tests --module jarvis.tests.test_agent_identity
"""

import unittest

import frappe

from jarvis.chat import agent_catalog, agent_scheduler, agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
SESSION = "Jarvis Chat Session"
AGENT = "close-auditor"

TEST_COMPANY = "Jarvis Ident Test Co"


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
	if not frappe.db.get_value("User", email, "enabled"):
		frappe.db.set_value("User", email, "enabled", 1, update_modified=False)
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
	have = set(frappe.get_roles(email))
	want = {"Jarvis User", *extra_roles}
	missing = [r for r in want if r not in have]
	if missing:
		frappe.get_doc("User", email).add_roles(*missing)
	frappe.clear_cache(user=email)
	frappe.db.commit()
	return email


def _ensure_plain_user(email: str) -> str:
	"""An enabled System User with NO Jarvis roles (a valid run-as target that is
	never itself an installer)."""
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
	frappe.db.commit()
	return email


def _install_as(owner: str, agent_slug: str = AGENT) -> str:
	original = frappe.session.user
	frappe.set_user(owner)
	try:
		return agents_api.install_agent(agent_slug)["data"]["name"]
	finally:
		frappe.set_user(original)


def _ensure_test_company() -> str:
	"""A BARE Company row via raw db_insert — bench patterntest never ran the
	erpnext setup wizard, so a real Company.insert() dies creating default
	warehouses. All we need is a row that `exists()` and can key the GL watermark
	query (which returns 0 rows on this empty ledger)."""
	if not frappe.db.exists("Company", TEST_COMPANY):
		c = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": TEST_COMPANY,
				"abbr": "JITC",
				"default_currency": "INR",
				"country": "India",
			}
		)
		c.name = TEST_COMPANY
		c.flags.ignore_links = True
		c.flags.ignore_mandatory = True
		c.db_insert()
		frappe.db.commit()
	return TEST_COMPANY


class TestAgentIdentity(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from jarvis.permissions import ensure_jarvis_admin_role

		frappe.set_user("Administrator")
		agent_catalog.sync_agent_listings()
		ensure_jarvis_admin_role()
		cls.owner = _ensure_user("aid-owner@example.com")
		cls.peer = _ensure_user("aid-peer@example.com")
		cls.jadmin = _ensure_user("aid-jadmin@example.com", extra_roles=("Jarvis Admin",))
		cls.scoped = _ensure_user("aid-scoped@example.com")
		cls.sm = _ensure_user("aid-sm@example.com", extra_roles=("System Manager",))
		cls.mapped = _ensure_plain_user("aid-mapped@example.com")
		cls.users = (cls.owner, cls.peer, cls.jadmin, cls.scoped, cls.sm, cls.mapped)
		# close-auditor declares doctypes_required (GL Entry / Account / Company);
		# the install/run-as A12-gate needs the run-as user to hold those reads.
		# Accounts User grants them without conferring Jarvis roles, so it does not
		# disturb the identity/escalation semantics these tests exercise.
		if frappe.db.exists("Role", "Accounts User"):
			for u in cls.users:
				frappe.get_doc("User", u).add_roles("Accounts User")
				frappe.clear_cache(user=u)
			frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		self._cleanup()

	def _cleanup(self):
		# A prior test may have left an agent role restriction — clear it so our
		# users (plain Jarvis Users) can install the unrestricted catalog agent.
		frappe.db.delete(
			"Jarvis Agent Allowed Role",
			{"parenttype": LISTING, "parentfield": "allowed_roles"},
		)
		for dt in (FINDING, RUN, INSTALLATION):
			for u in self.users:
				for n in frappe.get_all(dt, filters={"owner": u}, pluck="name"):
					frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		# Per-run session rows + any leftover installs owned by our users.
		for u in self.users:
			for n in frappe.get_all(SESSION, filters={"user": u}, pluck="name"):
				frappe.delete_doc(SESSION, n, force=True, ignore_permissions=True)
			for n in frappe.get_all("User Permission", filters={"user": u}, pluck="name"):
				frappe.delete_doc("User Permission", n, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ------------------------------------------------------------------ #
	# (a) A13 backfill
	# ------------------------------------------------------------------ #
	def test_backfill_sets_run_as_user_to_owner_and_skips_invalid(self):
		from jarvis.patches import v2_02_backfill_agent_run_as_user as patch

		# Legacy row: valid owner, run_as_user cleared (pre-Phase-1 state).
		good = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, good, "run_as_user", "", update_modified=False)

		# Legacy row whose owner is now disabled -> must be skipped, not crash.
		disabled = _ensure_user("aid-disabled@example.com")
		if frappe.db.exists("Role", "Accounts User"):
			frappe.get_doc("User", disabled).add_roles("Accounts User")  # A12 GL read
			frappe.clear_cache(user=disabled)
		bad = _install_as(disabled)
		frappe.db.set_value(INSTALLATION, bad, "run_as_user", "", update_modified=False)
		frappe.db.set_value("User", disabled, "enabled", 0, update_modified=False)
		frappe.db.commit()

		try:
			patch.execute()
			self.assertEqual(frappe.db.get_value(INSTALLATION, good, "run_as_user"), self.owner)
			# invalid owner -> left empty (fails closed at run time), migrate not aborted
			self.assertFalse((frappe.db.get_value(INSTALLATION, bad, "run_as_user") or "").strip())
		finally:
			frappe.db.set_value("User", disabled, "enabled", 1, update_modified=False)
			for n in frappe.get_all(INSTALLATION, filters={"owner": disabled}, pluck="name"):
				frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ------------------------------------------------------------------ #
	# (b) validate() escalation guard (A4)
	# ------------------------------------------------------------------ #
	def test_validate_rejects_non_admin_cross_user_mapping(self):
		inst = _install_as(self.owner)  # run_as_user = owner (self-map)
		frappe.set_user(self.owner)
		try:
			doc = frappe.get_doc(INSTALLATION, inst)
			doc.run_as_user = self.peer
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		finally:
			frappe.db.rollback()
			frappe.set_user("Administrator")
		# The mapping never landed.
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.owner)

	def test_validate_rejects_non_sm_binding_to_system_manager(self):
		# jadmin holds Jarvis Admin (so a cross-user mapping is ALLOWED) but is NOT
		# a System Manager -> binding to an SM must still be refused.
		inst = _install_as(self.jadmin)
		frappe.set_user(self.jadmin)
		try:
			doc = frappe.get_doc(INSTALLATION, inst)
			doc.run_as_user = self.sm
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		finally:
			frappe.db.rollback()
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.jadmin)

	def test_admin_may_map_cross_user_to_non_sm(self):
		inst = _install_as(self.owner)
		frappe.set_user("Administrator")  # a System Manager
		doc = frappe.get_doc(INSTALLATION, inst)
		doc.run_as_user = self.peer
		doc.save()
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.peer)

	# ------------------------------------------------------------------ #
	# (c) scoped_visibility on a GL-dimension User Permission (A12)
	# ------------------------------------------------------------------ #
	def test_scoped_visibility_set_on_cost_center_user_permission(self):
		# ignore_links: a real Cost Center needs a fully set-up Company; the flag
		# only affects the User Permission's for_value link check, not the
		# get_user_permissions read the detector uses.
		up = frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": self.scoped,
				"allow": "Cost Center",
				"for_value": "JITC-Main-CC",
			}
		)
		up.insert(ignore_permissions=True, ignore_links=True)
		frappe.db.commit()
		frappe.clear_cache(user=self.scoped)

		inst = _install_as(self.scoped)  # self-map; validate() detects the CC slice
		self.assertEqual(int(frappe.db.get_value(INSTALLATION, inst, "scoped_visibility") or 0), 1)

		# A control: a user with no GL-dimension User Permission stays unscoped.
		clean = _install_as(self.owner)
		self.assertEqual(int(frappe.db.get_value(INSTALLATION, clean, "scoped_visibility") or 0), 0)

	# ------------------------------------------------------------------ #
	# (d) _launch_audit mints the session + stamps watermark/scope
	# ------------------------------------------------------------------ #
	def test_launch_audit_mints_session_and_stamps_watermark(self):
		company = _ensure_test_company()
		inst_name = _install_as(self.owner)
		frappe.db.set_value(
			INSTALLATION,
			inst_name,
			{
				"enabled": 1,
				"config": frappe.as_json(
					{
						"company": company,
						"from_date": "2026-04-01",
						"to_date": "2027-03-31",
					}
				),
			},
			update_modified=False,
		)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		import jarvis.admin_client as admin_client

		orig = admin_client.post_agent_run
		admin_client.post_agent_run = lambda **kw: {"run_id": kw.get("run_id"), "status": "queued"}
		frappe.set_user(self.owner)
		try:
			result = agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig

		run, sk = result["run"], result["session_key"]
		# session key id-component is the DELEGATE id `agent-<slug>` (matches the
		# fleet delegate id + the gateway agentId), not the bare slug.
		self.assertTrue(sk.startswith(f"agent:agent-{AGENT}:"))
		self.assertEqual(frappe.db.get_value(RUN, run, "session_key"), sk)

		# The minted session binds the run-as user + snapshots the device id.
		cs = frappe.db.get_value(SESSION, {"session_key": sk}, ["user", "chat_device_id"], as_dict=True)
		self.assertIsNotNone(cs)
		self.assertEqual(cs.user, self.owner)  # run_as_user defaults to owner

		# A17 watermark stamped (0 on this empty ledger, but PRESENT not None).
		self.assertEqual(int(frappe.db.get_value(RUN, run, "wm_row_count")), 0)
		# A6 explicit scope stamped + carries the resolved company.
		scope_json = frappe.db.get_value(RUN, run, "scope_json") or ""
		self.assertIn(company, scope_json)
		# A12 permission profile stamped.
		self.assertTrue(frappe.db.get_value(RUN, run, "permission_profile"))
		# Row ownership stays the human owner.
		self.assertEqual(frappe.db.get_value(RUN, run, "owner"), self.owner)

	# ------------------------------------------------------------------ #
	# (e) run executes AS run_as_user (impersonate), not the owner
	# ------------------------------------------------------------------ #
	def test_run_executes_as_run_as_user_not_owner(self):
		inst_name = _install_as(self.owner)
		# An admin retargets the run-as identity to a DIFFERENT (non-owner) user.
		frappe.set_user("Administrator")
		agents_api.set_run_as_user(inst_name, self.mapped)
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1, update_modified=False)
		frappe.db.commit()

		import jarvis.admin_client as admin_client

		captured = {}

		def _cap(**kw):
			captured["user"] = frappe.session.user
			return {"run_id": kw.get("run_id"), "status": "queued"}

		orig = admin_client.post_agent_run
		admin_client.post_agent_run = _cap
		frappe.set_user(self.owner)  # the human owner triggers their own install
		try:
			result = agents_api.run_agent_now(inst_name)
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig

		# The ERP-read identity during the turn is the MAPPED user, not the owner.
		self.assertEqual(captured.get("user"), self.mapped)
		self.assertNotEqual(captured.get("user"), self.owner)

		run = result["data"]["run"]
		sk = frappe.db.get_value(RUN, run, "session_key")
		self.assertEqual(frappe.db.get_value(SESSION, {"session_key": sk}, "user"), self.mapped)
		# Row ownership stays the human owner (if_owner visibility).
		self.assertEqual(frappe.db.get_value(RUN, run, "owner"), self.owner)
		conv = result["data"]["conversation"]
		self.assertEqual(frappe.db.get_value("Jarvis Conversation", conv, "owner"), self.owner)
