"""Phase 1 (identity foundation) tests for agents-as-delegates.

Covers the verified identity contract wired at trigger:
  (a) the A13 backfill patch stamps run_as_user = owner on legacy rows (and
      skips-or-logs an invalid owner without aborting);
  (b) validate() (the authoritative escalation guard) rejects a non-System-Manager
      mapping run_as_user to any other user (Jarvis Admin included), while an SM
      may map cross-user;
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
from unittest import mock

import frappe

from jarvis.chat import agent_catalog, agent_scheduler, agents_api
from jarvis.exceptions import InvalidArgumentError
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
		# jadmin holds Jarvis Admin but is NOT a System Manager -> a cross-user
		# mapping (here to an SM) is refused.
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
		# `_launch_audit` resolves this once before it inserts the Run. The prompt
		# receives that same object; it must not read `inst.config` itself because an
		# owner can change the installation after launch.
		snapshot = agent_scheduler._explicit_config(listing, inst)
		msg = agent_scheduler._audit_prompt(
			listing, inst, trigger="manual", scope={}, engagement_config=snapshot
		)
		self.assertIn("EXPLICIT CONFIG", msg)
		self.assertIn('"stale_floor_days": 45', msg)  # declared ageing tunable handed
		self.assertNotIn("percentage", msg)  # non-declared namespace filtered out
		self.assertNotIn('"company"', msg.split("EXPLICIT CONFIG", 1)[1])  # non-declared filtered
		# An agent declaring NO config_keys gets no EXPLICIT CONFIG and must preserve
		# the missing state. A run must never fall back to mutable installation config.
		bare = frappe._dict({"name": "y", "config_keys": []})
		bare_msg = agent_scheduler._audit_prompt(bare, inst, trigger="manual", scope={}, engagement_config={})
		self.assertNotIn("EXPLICIT CONFIG", bare_msg)
		self.assertIn("No declared engagement settings were stamped", bare_msg)
		self.assertIn("NEVER read mutable installation configuration", bare_msg)
		self.assertNotIn("Jarvis Agent Installation", bare_msg)
		self.assertNotIn("INST-1", bare_msg)
		self.assertNotIn("jarvis__", bare_msg)  # non-leak control: the bench prompt names NO tool
		# A config-tool agent must use its zero-argument, server-bound snapshot. It
		# must never receive a mutable Installation fallback after launch.
		from unittest import mock

		with mock.patch(
			"jarvis.chat.agent_catalog.registry_tools_allow",
			return_value=["jarvis__get_engagement_config", "jarvis__get_doc"],
		) as _rta:
			tool_msg = agent_scheduler._audit_prompt(
				bare, inst, trigger="manual", scope={}, engagement_config={}
			)
		_rta.assert_called_once_with("y")  # looked up by the resolved slug, not the listing dict
		self.assertIn("jarvis__get_engagement_config", tool_msg)  # only config path
		self.assertIn("NO arguments", tool_msg)  # zero-arg steering present
		self.assertIn("NEVER read mutable installation configuration", tool_msg)
		self.assertNotIn("jarvis__get_doc", tool_msg)
		self.assertNotIn("Jarvis Agent Installation", tool_msg)

	def test_negative_stock_record_scope_is_server_bound_and_never_broadens(self):
		"""A Warehouse root is delivered only after server-side validation.

		The inventory evaluator needs this narrow boundary to verify its configured
		leaf set.  Invalid config must not turn into an all-Warehouse scope.
		"""
		from jarvis.chat import agent_scope

		base = {
			"company": "Scope Co",
			"fiscal_year": None,
			"from_date": "2026-04-01",
			"to_date": "2027-03-31",
			"report_date": "2026-10-30",
		}

		def resolve(config, warehouse_company="Scope Co", warehouse_readable=True, company_readable=True):
			inst = frappe._dict(
				agent="negative-stock-valuation-auditor", name="NSV-INST", config=frappe.as_json(config)
			)
			with (
				mock.patch.object(agent_scope, "_resolve", return_value=dict(base)),
				mock.patch.object(
					agent_scope.frappe.db,
					"get_value",
					return_value=frappe._dict(company=warehouse_company, disabled=0),
				),
				mock.patch.object(
					agent_scope.frappe,
					"has_permission",
					side_effect=(warehouse_readable, company_readable),
				),
			):
				return agent_scope.resolve_scope(inst)

		good = {
			"negative_stock": {
				"record_scope": {"doctype": "Warehouse", "name": "WH-Scope"},
				"warehouse_names": ["WH-Scope"],
				"reviewer": "scope-reviewer@example.invalid",
			}
		}
		scope = resolve(good)
		self.assertEqual(scope["record_scope"], {"doctype": "Warehouse", "name": "WH-Scope"})

		listing = frappe._dict(
			{"name": "negative-stock-valuation-auditor", "config_keys": ["negative_stock"]}
		)
		inst = frappe._dict(name="NSV-INST", config=frappe.as_json(good))
		snapshot = agent_scheduler._sanitize_delegated_engagement_config(
			listing, agent_scheduler._explicit_config(listing, inst)
		)
		self.assertNotIn("record_scope", snapshot["negative_stock"])
		prompt = agent_scheduler._audit_prompt(
			listing, inst, trigger="manual", scope=scope, engagement_config=snapshot
		)
		self.assertIn('record_scope={"doctype": "Warehouse", "name": "WH-Scope"}', prompt)
		self.assertIn('"reviewer": "scope-reviewer@example.invalid"', prompt)
		self.assertNotIn('"record_scope"', prompt.split("EXPLICIT CONFIG", 1)[1])

		raw_scope_only = {"negative_stock": {"record_scope": {"doctype": "Warehouse", "name": "WH-Scope"}}}
		empty_scope = resolve(raw_scope_only)
		self.assertIn("record_scope", empty_scope)
		empty_inst = frappe._dict(name="NSV-INST", config=frappe.as_json(raw_scope_only))
		empty_snapshot = agent_scheduler._sanitize_delegated_engagement_config(
			listing, agent_scheduler._explicit_config(listing, empty_inst)
		)
		self.assertEqual(empty_snapshot, {"negative_stock": {}})
		empty_prompt = agent_scheduler._audit_prompt(
			listing, empty_inst, "manual", empty_scope, engagement_config=empty_snapshot
		)
		self.assertIn("EXPLICIT CONFIG (your tenant's engagement tunables, authoritative)", empty_prompt)
		self.assertIn('{"negative_stock": {}}', empty_prompt)
		self.assertNotIn('"record_scope"', empty_prompt.split("EXPLICIT CONFIG", 1)[1])
		self.assertNotIn("Jarvis Agent Installation", empty_prompt)

		for bad, warehouse_company, warehouse_readable, company_readable in (
			(
				{"negative_stock": {"warehouse_names": ["WH-Scope"]}},
				"Scope Co",
				True,
				True,
			),  # warehouse_names is never a fallback root
			({"negative_stock": {"record_scope": {"doctype": "Warehouse"}}}, "Scope Co", True, True),
			(
				{"negative_stock": {"record_scope": {"doctype": "Company", "name": "Scope Co"}}},
				"Scope Co",
				True,
				True,
			),
			(
				{
					"negative_stock": {
						"record_scope": {"doctype": "Warehouse", "name": "WH-Scope", "all": True}
					}
				},
				"Scope Co",
				True,
				True,
			),
			(good, "Other Co", True, True),
			(good, "Scope Co", False, True),
			(good, "Scope Co", True, False),
		):
			with self.subTest(
				bad=bad,
				warehouse_company=warehouse_company,
				warehouse_readable=warehouse_readable,
				company_readable=company_readable,
			):
				with self.assertRaises(InvalidArgumentError):
					resolve(bad, warehouse_company, warehouse_readable, company_readable)

	def test_negative_stock_launch_stamps_canonical_root_and_sanitized_config(self):
		"""The real launch stores the safe root and never forwards owner JSON."""
		import jarvis.admin_client as admin_client
		from jarvis.chat import agent_models, agent_scope

		negative_stock_agent = agent_scope.NEGATIVE_STOCK_AGENT
		allow_listing_for(negative_stock_agent, user=self.owner)
		stock_roles = [
			role
			for role in ("Stock User", "Stock Manager")
			if frappe.db.exists("Role", role) and role not in frappe.get_roles(self.owner)
		]
		if stock_roles:
			frappe.get_doc("User", self.owner).add_roles(*stock_roles)
			frappe.clear_cache(user=self.owner)
			frappe.db.commit()

		company = _ensure_test_company()
		inst_name = _install_as(self.owner, negative_stock_agent)
		raw_root = {"doctype": "Warehouse", "name": "Raw Owner Warehouse"}
		canonical_root = {"doctype": "Warehouse", "name": "Canonical Warehouse Group"}
		stamped_scope = {
			"company": company,
			"fiscal_year": None,
			"from_date": "2026-04-01",
			"to_date": "2027-03-31",
			"report_date": "2026-10-30",
			"record_scope": canonical_root,
		}
		frappe.db.set_value(
			INSTALLATION,
			inst_name,
			{
				"enabled": 1,
				"config": frappe.as_json(
					{
						"company": company,
						"from_date": stamped_scope["from_date"],
						"to_date": stamped_scope["to_date"],
						"negative_stock": {"record_scope": raw_root},
					}
				),
			},
			update_modified=False,
		)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)
		captured = {}

		def dispatch(**kwargs):
			captured.update(kwargs)
			return {"run_id": kwargs["run_id"], "status": "queued"}

		frappe.set_user(self.owner)
		try:
			with (
				mock.patch.object(agent_scope, "resolve_scope", return_value=stamped_scope),
				mock.patch.object(
					agent_models, "gate_run", return_value={"ok": True, "model_source": "choice"}
				),
				mock.patch.object(admin_client, "post_agent_run", side_effect=dispatch),
			):
				result = agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")

		run = result["run"]
		self.assertEqual(
			frappe.parse_json(frappe.db.get_value(RUN, run, "scope_json"))["record_scope"], canonical_root
		)
		self.assertEqual(
			frappe.parse_json(frappe.db.get_value(RUN, run, "engagement_config_json")),
			{"negative_stock": {}},
		)
		message = captured["message"]
		self.assertIn('record_scope={"doctype": "Warehouse", "name": "Canonical Warehouse Group"}', message)
		self.assertNotIn('"record_scope"', message.split("EXPLICIT CONFIG", 1)[1])
		self.assertNotIn("Raw Owner Warehouse", message)

	def test_negative_stock_missing_root_rolls_back_run_session_and_dispatch(self):
		"""A bound agent must not leave an unscoped launch behind."""
		import jarvis.admin_client as admin_client
		from jarvis.chat import agent_models, agent_scope

		negative_stock_agent = agent_scope.NEGATIVE_STOCK_AGENT
		allow_listing_for(negative_stock_agent, user=self.owner)
		stock_roles = [
			role
			for role in ("Stock User", "Stock Manager")
			if frappe.db.exists("Role", role) and role not in frappe.get_roles(self.owner)
		]
		if stock_roles:
			frappe.get_doc("User", self.owner).add_roles(*stock_roles)
			frappe.clear_cache(user=self.owner)
			frappe.db.commit()

		company = _ensure_test_company()
		inst_name = _install_as(self.owner, negative_stock_agent)
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
						"negative_stock": {},
					}
				),
			},
			update_modified=False,
		)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)
		before_runs = frappe.db.count(RUN, {"installation": inst_name})
		before_sessions = frappe.db.count(SESSION, {"user": self.owner})

		frappe.set_user(self.owner)
		try:
			with (
				mock.patch.object(
					agent_models, "gate_run", return_value={"ok": True, "model_source": "choice"}
				),
				mock.patch.object(admin_client, "post_agent_run") as dispatch,
				mock.patch.object(frappe, "log_error") as log_error,
			):
				with self.assertRaises(InvalidArgumentError):
					agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(frappe.db.count(RUN, {"installation": inst_name}), before_runs)
		self.assertEqual(frappe.db.count(SESSION, {"user": self.owner}), before_sessions)
		dispatch.assert_not_called()
		self.assertEqual(
			log_error.call_args.kwargs["title"],
			"jarvis agent: Negative stock scope resolution refused launch",
		)

	def test_fefo_record_scope_is_leaf_bound_or_the_run_fails_closed(self):
		"""FEFO may be company-wide only when the owner declares no root."""
		from jarvis.chat import agent_scope
		from jarvis.exceptions import InvalidArgumentError

		base = {
			"company": "Scope Co",
			"fiscal_year": None,
			"from_date": "2026-04-01",
			"to_date": "2027-03-31",
			"report_date": "2026-10-30",
		}

		def resolve(config, warehouse=None, warehouse_readable=True):
			inst = frappe._dict(
				agent="fefo-expiry-compliance-auditor", name="FEFO-INST", config=frappe.as_json(config)
			)
			warehouse = warehouse or {"company": "Scope Co", "is_group": 0}
			with (
				mock.patch.object(agent_scope, "_resolve", return_value=dict(base)),
				mock.patch.object(agent_scope.frappe.db, "get_value", return_value=warehouse),
				mock.patch.object(agent_scope.frappe, "has_permission", return_value=warehouse_readable),
			):
				return agent_scope.resolve_scope(inst)

		policy = {
			"reviewer": "scope-reviewer@example.invalid",
			"near_expiry_days": 90,
			"test_declaration": "Fake records only.",
		}
		good = {
			"fefo": {
				**policy,
				"record_scope": {"doctype": "Warehouse", "name": "WH-Scope"},
			}
		}
		scope = resolve(good)
		self.assertEqual(scope["record_scope"], {"doctype": "Warehouse", "name": "WH-Scope"})

		listing = frappe._dict(
			{
				"name": "fefo-expiry-compliance-auditor",
				"config_keys": [
					"fefo.reviewer",
					"fefo.near_expiry_days",
					"fefo.test_declaration",
				],
			}
		)
		inst = frappe._dict(name="FEFO-INST", config=frappe.as_json(good))
		snapshot = agent_scheduler._sanitize_delegated_engagement_config(
			listing, agent_scheduler._explicit_config(listing, inst)
		)
		self.assertEqual(snapshot, {"fefo": policy})
		prompt = agent_scheduler._audit_prompt(
			listing, inst, trigger="manual", scope=scope, engagement_config=snapshot
		)
		self.assertIn('record_scope={"doctype": "Warehouse", "name": "WH-Scope"}', prompt)
		self.assertIn('"near_expiry_days": 90', prompt)
		self.assertNotIn('"record_scope"', prompt.split("EXPLICIT CONFIG", 1)[1])

		# A root is optional. With no declaration, FEFO keeps its intentional
		# company-wide mode and retains only the declared policy values.
		company_scope = resolve({"fefo": policy})
		self.assertNotIn("record_scope", company_scope)
		company_inst = frappe._dict(name="FEFO-INST", config=frappe.as_json({"fefo": policy}))
		company_snapshot = agent_scheduler._sanitize_delegated_engagement_config(
			listing, agent_scheduler._explicit_config(listing, company_inst)
		)
		self.assertEqual(company_snapshot, {"fefo": policy})
		company_prompt = agent_scheduler._audit_prompt(
			listing, company_inst, "manual", company_scope, engagement_config=company_snapshot
		)
		self.assertNotIn("record_scope", company_prompt)

		# A scope-only configuration proves that the raw owner declaration is
		# stripped even when another preflight later rejects the missing policy.
		scope_only = {"fefo": {"record_scope": {"doctype": "Warehouse", "name": "WH-Scope"}}}
		scope_only_inst = frappe._dict(name="FEFO-INST", config=frappe.as_json(scope_only))
		scope_only_snapshot = agent_scheduler._sanitize_delegated_engagement_config(
			listing, agent_scheduler._explicit_config(listing, scope_only_inst)
		)
		self.assertEqual(scope_only_snapshot, {"fefo": {}})

		for bad, warehouse, warehouse_readable in (
			({"fefo": {**policy, "record_scope": None}}, None, True),
			({"fefo": {**policy, "record_scope": {"doctype": "Warehouse"}}}, None, True),
			(
				{"fefo": {**policy, "record_scope": {"doctype": "Company", "name": "Scope Co"}}},
				None,
				True,
			),
			(
				{
					"fefo": {
						**policy,
						"record_scope": {"doctype": "Warehouse", "name": "WH-Scope", "all": True},
					}
				},
				None,
				True,
			),
			(good, {"company": "Other Co", "is_group": 0}, True),
			(good, {"company": "Scope Co", "is_group": 1}, True),
			(good, {"company": "Scope Co", "is_group": 0}, False),
		):
			with self.subTest(bad=bad, warehouse=warehouse, warehouse_readable=warehouse_readable):
				with self.assertRaises(agent_scope.InvalidFefoRecordScopeError):
					resolve(bad, warehouse, warehouse_readable)
				invalid_inst = frappe._dict(name="FEFO-INST", config=frappe.as_json(bad))
				invalid_snapshot = agent_scheduler._sanitize_delegated_engagement_config(
					listing, agent_scheduler._explicit_config(listing, invalid_inst)
				)
				self.assertNotIn("record_scope", invalid_snapshot["fefo"])
				invalid_prompt = agent_scheduler._audit_prompt(
					listing, invalid_inst, "manual", {}, engagement_config=invalid_snapshot
				)
				self.assertNotIn("record_scope", invalid_prompt)
				self.assertNotIn("Jarvis Agent Installation", invalid_prompt)

		# A database failure is not a bad declaration. It remains a retryable
		# verification failure, preserves its exception cause, and never falls back
		# to a company-wide scope.
		db_failure = RuntimeError("Warehouse lookup unavailable")
		inst = frappe._dict(
			agent=agent_scope.FEFO_AGENT, name="FEFO-INST", config=frappe.as_json(good)
		)
		with (
			mock.patch.object(agent_scope, "_resolve", return_value=dict(base)),
			mock.patch.object(agent_scope.frappe.db, "get_value", side_effect=db_failure),
			mock.patch.object(agent_scope.frappe, "has_permission") as has_permission,
		):
			with self.assertRaises(InvalidArgumentError) as raised:
				agent_scope.resolve_scope(inst)
		self.assertNotIsInstance(raised.exception, agent_scope.InvalidFefoRecordScopeError)
		self.assertIs(raised.exception.__cause__, db_failure)
		has_permission.assert_not_called()

	def test_fefo_near_expiry_window_rejects_only_invalid_supplied_values(self):
		"""Advanced JSON and direct API saves share the evaluator's strict integer rule."""
		from jarvis.chat import agent_scope
		from jarvis.exceptions import InvalidArgumentError

		for value in (True, 0, -1, 1.5, 3651, "30", None):
			with self.subTest(value=value):
				with self.assertRaises(InvalidArgumentError):
					agent_scope.validate_fefo_near_expiry_days({"fefo": {"near_expiry_days": value}})
		self.assertIsNone(agent_scope.validate_fefo_near_expiry_days({"fefo": {}}))
		self.assertIsNone(agent_scope.validate_fefo_near_expiry_days({"fefo": {"near_expiry_days": 30}}))

	def test_fefo_near_expiry_window_is_checked_at_the_installation_save_boundary(self):
		"""A direct config save cannot bypass the browser's FEFO integer range check."""
		from jarvis.chat.agent_scope import FEFO_AGENT
		from jarvis.jarvis.doctype.jarvis_agent_installation.jarvis_agent_installation import (
			JarvisAgentInstallation,
		)

		doc = mock.Mock(agent=FEFO_AGENT, config=frappe.as_json({"fefo": {"near_expiry_days": 0}}))
		doc.is_new.return_value = False
		doc.has_value_changed.return_value = True
		with self.assertRaises(frappe.ValidationError) as raised:
			JarvisAgentInstallation._validate_agent_config(doc)
		self.assertIn("FEFO near_expiry_days must be an integer from 1 to 3650", str(raised.exception))

		doc.config = frappe.as_json({"fefo": {"near_expiry_days": 30}})
		JarvisAgentInstallation._validate_agent_config(doc)

		# Do not strand an owner who needs to repair a legacy invalid row by saving
		# another field first. The changed config itself is still rejected above.
		doc.has_value_changed.return_value = False
		doc.config = frappe.as_json({"fefo": {"near_expiry_days": 0}})
		JarvisAgentInstallation._validate_agent_config(doc)

	def test_fefo_scope_resolution_error_refuses_the_launch_path(self):
		"""A declared invalid FEFO root cannot turn into a broad unscoped run."""
		from jarvis.chat import agent_scope, operator_review

		inst = frappe._dict(agent="fefo-expiry-compliance-auditor", name="FEFO-INST")
		with (
			mock.patch.object(operator_review, "backend", return_value=None),
			mock.patch.object(
				agent_scope,
				"resolve_scope",
				side_effect=agent_scope.InvalidFefoRecordScopeError(
					"FEFO record_scope must name one Warehouse leaf"
				),
			),
			mock.patch.object(agent_scheduler.frappe, "log_error") as log_error,
			mock.patch.object(agent_scheduler.frappe.db, "set_value") as set_value,
		):
			with self.assertRaises(agent_scope.InvalidFefoRecordScopeError):
				agent_scheduler._stamp_scope_and_watermark("FEFO-RUN", inst, self.owner)

		set_value.assert_not_called()
		log_error.assert_not_called()

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
