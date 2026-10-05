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
from jarvis.tests._agent_access import allow_listing_for, clear_listing_access

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
		# jarvis#1062 inverted the default: wiping the allow rows no longer leaves the
		# listing "unrestricted", it CLOSES it. So this resets to a known state and
		# then re-grants, rather than relying on emptiness to mean access. Granted to
		# the users by NAME because this module's cast deliberately holds a mix of
		# roles (plain, Jarvis Admin, System Manager, no-Jarvis-role) and a role grant
		# would quietly change which of them the identity tests are exercising.
		clear_listing_access()
		for u in self.users:
			allow_listing_for(AGENT, user=u)
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
		# Not in cls.users, so _cleanup's blanket grant does not cover it.
		allow_listing_for(AGENT, user=disabled)
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
	# (c2) A12 read-gate understands child tables (readability rides the parent)
	# ------------------------------------------------------------------ #
	def test_run_as_read_gate_child_table_rides_parent(self):
		"""A child DocType (``istable``) carries no DocPerm rows of its own, so a bare
		``has_permission`` on it is False for every non-Administrator user. The A12
		read-gate must instead check the child through a parent that embeds it: a run-as
		user who can read the parent passes; one who cannot is refused; the non-child
		path is unchanged."""
		doc = frappe.new_doc(INSTALLATION)
		child, parent = "Sales Invoice Item", "Sales Invoice"
		reader = self.owner  # holds Accounts User (setUpClass) -> reads Sales Invoice

		# The bare check the gate USED to do is False even for a legitimate reader —
		# this is the Frappe behavior the fix works around, asserted as a canary.
		self.assertTrue(frappe.has_permission(parent, "read", user=reader))
		self.assertFalse(frappe.has_permission(child, "read", user=reader))
		# The fixed check rides the parent: readable.
		self.assertTrue(doc._run_as_can_read(child, reader))

		# Negative: a bare System User who cannot read the parent is refused the child
		# (the gate is not silently relaxed to "any child is readable").
		noreader = _ensure_plain_user("aid-noaccounts@example.com")
		try:
			self.assertFalse(frappe.has_permission(parent, "read", user=noreader))
			self.assertFalse(doc._run_as_can_read(child, noreader))
			# Non-child path unchanged: an unreadable normal DocType stays refused.
			self.assertFalse(doc._run_as_can_read("GL Entry", noreader))
		finally:
			frappe.delete_doc("User", noreader, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_run_as_read_gate_child_branches(self):
		"""Cover _run_as_can_read's child-table branches: an orphan child (no embedding
		parent -> fail-closed refuse), a STALE discovered parent (no longer a DocType ->
		dropped by the existence guard -> refuse, never an uncaught DoesNotExistError/500),
		and the any()-over-parents semantics (readable via at least ONE parent allows,
		which an all() regression would wrongly refuse)."""
		from unittest import mock

		doc = frappe.new_doc(INSTALLATION)
		reader, child = self.owner, "Sales Invoice Item"
		HELP = "jarvis.tools.get_list._child_table_parents"

		# Orphan: nothing embeds it -> no parent to ride -> refuse.
		with mock.patch(HELP, return_value=[]):
			self.assertFalse(doc._run_as_can_read(child, reader))

		# Stale parent: a discovered name that is no longer a DocType is dropped by the
		# existence guard, so the call refuses cleanly instead of raising a 500.
		with mock.patch(HELP, return_value=["No Such Parent DocType ZZZ"]):
			self.assertFalse(doc._run_as_can_read(child, reader))

		# any(): readable via ONE embedding parent allows, even when another discovered
		# parent is unreadable — pinning any() (an all() regression would flip this False).
		def _perm(*a, **kw):
			return kw.get("parent_doctype") == "Sales Invoice"

		with (
			mock.patch(HELP, return_value=["Sales Invoice", "Sales Order"]),
			mock.patch("frappe.has_permission", side_effect=_perm),
		):
			self.assertTrue(doc._run_as_can_read(child, reader))

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

	# ------------------------------------------------------------------ #
	# (d2) launch refuses a blank-reviewer install (bench-authoritative gate)
	# ------------------------------------------------------------------ #
	def test_launch_refuses_blank_reviewer_no_orphan_run(self):
		# The accountable-reviewer invariant is enforced at launch: a blank-reviewer install
		# (constructed by clearing the auto-backfilled reviewer) is refused BEFORE any row is
		# created, so the container never has to mirror the gate and no orphan run is left.
		inst_name = _install_as(self.owner)
		frappe.db.set_value(INSTALLATION, inst_name, {"enabled": 1, "reviewer": ""}, update_modified=False)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)
		self.assertEqual((inst.reviewer or "").strip(), "")  # precondition: reviewer blank
		before = frappe.db.count(RUN, {"installation": inst_name})
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.ValidationError):
				agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
		# the refused launch left NO orphan run.
		self.assertEqual(frappe.db.count(RUN, {"installation": inst_name}), before)

	# ------------------------------------------------------------------ #
	# (d2) jarvis#1063: declared config_keys are DELIVERED in the run message
	# ------------------------------------------------------------------ #
	def test_audit_prompt_hands_declared_config_in_message(self):
		# Run.scope_json is a bench-side field that never reaches the container; the run MESSAGE
		# is the only bench->delegate channel. So an agent's DECLARED config_keys values must ride
		# the message as EXPLICIT CONFIG, filtered to declared namespaces (never the whole config),
		# so the evaluator gets them without the delegate reading its own installation.
		listing = frappe._dict(
			{"name": "vl", "config_keys": ["ageing.stale_floor_days", "ageing.band_edges"]}
		)
		inst = frappe._dict(
			{
				"name": "INST-1",
				"config": frappe.as_json(
					{
						"ageing": {"stale_floor_days": 45},
						"materiality": {"percentage": 5},  # NOT in this agent's config_keys
						"company": "X",  # a scope key, not a tunable
					}
				),
			}
		)
		msg = agent_scheduler._audit_prompt(listing, inst, trigger="manual", scope={})
		self.assertIn("EXPLICIT CONFIG", msg)
		self.assertIn('"stale_floor_days": 45', msg)  # declared ageing tunable handed
		self.assertNotIn("percentage", msg)  # non-declared namespace filtered out
		self.assertNotIn('"company"', msg.split("EXPLICIT CONFIG", 1)[1])  # non-declared filtered
		# an agent declaring NO config_keys gets no EXPLICIT CONFIG (points to its installation).
		bare = frappe._dict({"name": "y", "config_keys": []})
		bare_msg = agent_scheduler._audit_prompt(bare, inst, trigger="manual", scope={})
		self.assertNotIn("EXPLICIT CONFIG", bare_msg)
		# A6/hallucination fix: the pointer NAMES the real doctype so a weak model cannot invent
		# one (e.g. "Jarvis Engagement Configuration"). Per the non-leak control the bench prompt
		# names the doctype + the installation ROW but NO tool (the SKILL owns get_doc); the old
		# doctype-less "read it there" phrasing is gone.
		self.assertIn("Jarvis Agent Installation", bare_msg)
		self.assertIn("INST-1", bare_msg)  # the installation ROW name is still handed
		self.assertNotIn("read it there", bare_msg)
		self.assertNotIn("jarvis__", bare_msg)  # non-leak control: the bench prompt names NO tool
		# fix #1: when the agent's tools_allow includes the zero-arg tool, the prompt PREFERS it
		# (nothing for a weak model to fumble) but KEEPS the named-doctype get_doc as a fallback,
		# so a deploy-skew window (tool not yet in the container / tenant Apply) degrades to the
		# working A' path, never a dead end.
		from unittest import mock

		with mock.patch(
			"jarvis.chat.agent_catalog.registry_tools_allow",
			return_value=["jarvis__get_engagement_config", "jarvis__get_doc"],
		) as _rta:
			tool_msg = agent_scheduler._audit_prompt(bare, inst, trigger="manual", scope={})
		_rta.assert_called_once_with("y")  # looked up by the resolved slug, not the listing dict
		self.assertIn("jarvis__get_engagement_config", tool_msg)  # preferred path
		self.assertIn("NO arguments", tool_msg)  # zero-arg steering present
		self.assertIn("jarvis__get_doc", tool_msg)  # fallback retained (deploy-skew safety)
		self.assertIn("Jarvis Agent Installation", tool_msg)  # fallback names the real doctype

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
