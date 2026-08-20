"""Integration tests for the Agents Marketplace backend (B3).

The load-bearing test is the scheduler-identity regression (S1): a scheduled
audit's conversation is owned by the installation owner and NEVER Administrator
— the single most important control (a scheduled turn otherwise runs jarvis__*
tools as Administrator, bypassing every DocType permission, silently). The rest
cover mutation authZ (S3), the deterministic Run+Findings persistence with
dedupe (O2), and catalog-sync idempotency.
"""

import unittest

import frappe

from jarvis.chat import agent_catalog, agent_scheduler, agents_api

LISTING = "Jarvis Agent Listing"
INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
ALLOWED_ROLE = "Jarvis Agent Allowed Role"

ROLE_X = "Jarvis Agent Test Role X"
ROLE_Y = "Jarvis Agent Test Role Y"


def _ensure_role(role_name: str) -> str:
	if not frappe.db.exists("Role", role_name):
		r = frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1})
		r.flags.ignore_permissions = True
		r.insert()
		frappe.db.commit()
	return role_name


def _give_role(email: str, role_name: str) -> None:
	u = frappe.get_doc("User", email)
	if not any(r.role == role_name for r in u.roles):
		u.append("roles", {"role": role_name})
		u.flags.ignore_permissions = True
		u.save()
		frappe.db.commit()


def _ensure_user(email: str) -> str:
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
		frappe.db.commit()
	if frappe.db.get_value("User", email, "user_type") != "System User":
		frappe.db.set_value("User", email, "user_type", "System User", update_modified=False)
		frappe.clear_cache(user=email)
	# The agents endpoints are chat-surface: they now require the Jarvis User
	# role (security review TASK 8). Grant it so the fixtures reach the
	# agent-specific allowed_roles / owner gates they actually test.
	if "Jarvis User" not in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).add_roles("Jarvis User")
		frappe.db.commit()
	return email


def _install_as(owner: str, agent_slug: str) -> str:
	"""Create an installation owned by ``owner`` (running as that user so the
	if_owner rows land correctly)."""
	original = frappe.session.user
	frappe.set_user(owner)
	try:
		res = agents_api.install_agent(agent_slug)
		return res["data"]["name"]
	finally:
		frappe.set_user(original)


class TestAgentsMarketplace(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		agent_catalog.sync_agent_listings()
		cls.owner = _ensure_user("agent-owner@example.com")
		cls.other = _ensure_user("agent-other@example.com")
		cls.admin = _ensure_user("agent-admin@example.com")
		_ensure_role(ROLE_X)
		_ensure_role(ROLE_Y)  # assigned to NOBODY — used to revoke access
		_give_role(cls.owner, ROLE_X)
		_give_role(cls.admin, "System Manager")
		# The shipped delegate agents declare doctypes_required (GL Entry / Account
		# / Company); the install A12-gate needs the run-as user to hold those reads.
		# Accounts User grants them — give it to the non-admin fixtures so a
		# legitimate install/run is not blocked by the read gate (the RBAC tests
		# still gate on the agent's allowed_roles, a separate check).
		if frappe.db.exists("Role", "Accounts User"):
			for u in (cls.owner, cls.other, cls.admin):
				_give_role(u, "Accounts User")
		# State-independence on a shared bench site: the manual/scheduled run budget
		# (_over_run_budget) enforces a TENANT-WIDE monthly ceiling that counts every
		# NON-FAILED Jarvis Agent Run across the whole site — including residue other
		# platform-test modules leave behind (their record_delegate_run commits
		# mid-test, so FrappeTestCase cannot roll those rows back). That aggregate can
		# refuse this module's legitimate run_agent_now dispatch ("Monthly agent-run
		# budget reached") even though this module cleans its OWN runs every setUp.
		# None of these tests exercise the budget-exceeded path, so lift the budget
		# out of the way for the module and restore it in tearDownClass — the tests
		# then assert identity/authZ, not another module's leftover row count.
		cls._orig_run_budget = frappe.db.get_single_value("Jarvis Settings", "agent_run_budget_monthly")
		frappe.db.set_single_value("Jarvis Settings", "agent_run_budget_monthly", 1000000)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_single_value(
			"Jarvis Settings", "agent_run_budget_monthly", getattr(cls, "_orig_run_budget", None)
		)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		# Clean this test's installs/runs/findings so reruns are deterministic.
		for dt in (FINDING, RUN, INSTALLATION):
			for owner in (self.owner, self.other, self.admin):
				for n in frappe.get_all(dt, filters={"owner": owner}, pluck="name"):
					frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		# Clear any role restriction left by a previous test (bench-admin state).
		frappe.db.delete(ALLOWED_ROLE, {"parenttype": LISTING, "parentfield": "allowed_roles"})
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")

	# ------------------------------------------------------------------ #
	# (a) THE scheduler-identity regression test (S1)
	# ------------------------------------------------------------------ #
	def test_scheduled_run_owner_is_installation_owner_never_administrator(self):
		inst_name = _install_as(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		# Drive the scheduler's exact launch path AS the owner (what
		# run_due_agent_audits does inside set_user(owner)). Stub the delegate
		# dispatch (admin -> fleet) so no real admin/agent is needed.
		import jarvis.admin_client as admin_client

		orig_run = admin_client.post_agent_run
		admin_client.post_agent_run = lambda **kw: {"run_id": kw.get("run_id"), "status": "queued"}
		original_user = frappe.session.user
		try:
			frappe.set_user(self.owner)
			result = agent_scheduler._launch_audit(inst, trigger="scheduled")
		finally:
			frappe.set_user(original_user)
			admin_client.post_agent_run = orig_run

		conv = result["conversation"]
		run = result["run"]
		conv_owner = frappe.db.get_value("Jarvis Conversation", conv, "owner")
		run_owner = frappe.db.get_value(RUN, run, "owner")
		self.assertEqual(conv_owner, self.owner)
		self.assertNotEqual(conv_owner, "Administrator")
		self.assertEqual(run_owner, self.owner)
		self.assertNotEqual(run_owner, "Administrator")

	def test_fail_closed_guard_rejects_administrator_owner(self):
		# The identity guard must refuse Administrator / Guest / disabled users.
		self.assertFalse(agent_scheduler._valid_owner("Administrator"))
		self.assertFalse(agent_scheduler._valid_owner("Guest"))
		self.assertTrue(agent_scheduler._valid_owner(self.owner))

	def test_run_agent_now_executes_as_owner_not_triggering_admin(self):
		# A System Manager (Administrator) triggering ANOTHER owner's audit must
		# dispatch the turn under the OWNER's identity — so jarvis__* tool calls
		# are scoped to the owner's permissions, not the admin's (the manual-path
		# analogue of the S1 scheduler hinge).
		inst_name = _install_as(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1)
		frappe.db.commit()

		import jarvis.admin_client as admin_client

		captured = {}
		orig_run = admin_client.post_agent_run

		def _cap(**kw):
			captured["user"] = frappe.session.user
			return {"run_id": kw.get("run_id"), "status": "queued"}

		admin_client.post_agent_run = _cap
		original_user = frappe.session.user
		try:
			frappe.set_user("Administrator")  # a System Manager triggers someone else's audit
			result = agents_api.run_agent_now(inst_name)
		finally:
			frappe.set_user(original_user)
			admin_client.post_agent_run = orig_run

		# The turn was dispatched while the session was the OWNER, not Administrator.
		self.assertEqual(captured.get("user"), self.owner)
		conv_owner = frappe.db.get_value("Jarvis Conversation", result["data"]["conversation"], "owner")
		self.assertEqual(conv_owner, self.owner)
		self.assertNotEqual(conv_owner, "Administrator")

	def test_run_now_on_unapplied_agent_gives_apply_hint(self):
		# An agent ENABLED on the bench but not yet pushed to the container produces
		# the fleet-agent's "not an installed delegate on <container>" 502 (enabling
		# only flags the catalog dirty; the skill reaches the container on APPLY).
		# run_agent_now must translate that into an actionable "Apply catalog changes"
		# message — never a raw 500 — and leave the Run FAILED, not stuck "running".
		inst_name = _install_as(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1)
		agent = frappe.db.get_value(INSTALLATION, inst_name, "agent")
		frappe.db.commit()

		import jarvis.admin_client as admin_client
		from jarvis.exceptions import AdminUnreachableError

		orig_run = admin_client.post_agent_run

		def _not_installed(**kw):
			raise AdminUnreachableError(
				"admin returned a 502 error: invalid_spec: agent_id "
				f"'{kw.get('agent_id')}' is not an installed delegate on jarvis-pool-test"
			)

		admin_client.post_agent_run = _not_installed
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				agents_api.run_agent_now(inst_name)
		finally:
			admin_client.post_agent_run = orig_run

		self.assertIn("Apply catalog changes", str(ctx.exception))
		# The run must be terminal FAILED (never left stuck "running") and carry the
		# same actionable message, not a raw fleet 502.
		run = frappe.get_all(
			RUN,
			filters={"agent": agent, "owner": self.owner},
			fields=["status", "error"],
			order_by="creation desc",
			limit=1,
		)
		self.assertTrue(run)
		self.assertEqual(run[0].status, "failed")
		self.assertIn("Apply catalog changes", run[0].error or "")

	# ------------------------------------------------------------------ #
	# (a2) Phase 2C — delegate dispatch routes through admin, not chat
	# ------------------------------------------------------------------ #
	def test_delegate_dispatch_calls_post_agent_run_not_send_message(self):
		inst_name = _install_as(self.owner, "close-auditor")  # delivery=delegate
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		import jarvis.admin_client as admin_client
		import jarvis.chat.api as chat_api

		captured = {}

		def _cap(**kw):
			captured.update(kw)
			return {"run_id": kw.get("run_id"), "status": "queued"}

		def _no_send(**kw):
			raise AssertionError("send_message must NOT be called for a delegate")

		orig_run, orig_send = admin_client.post_agent_run, chat_api.send_message
		admin_client.post_agent_run = _cap
		chat_api.send_message = _no_send
		original_user = frappe.session.user
		try:
			frappe.set_user(self.owner)
			result = agent_scheduler._launch_audit(inst, trigger="scheduled")
		finally:
			frappe.set_user(original_user)
			admin_client.post_agent_run = orig_run
			chat_api.send_message = orig_send

		run = result["run"]
		self.assertEqual(captured.get("run_id"), run)
		self.assertEqual(captured.get("agent_id"), "agent-close-auditor")
		self.assertEqual(captured.get("session_key"), result["session_key"])
		self.assertTrue(captured.get("session_key").startswith("agent:agent-close-auditor:"))
		# timeout_s sourced from the bundled registry (close-auditor = 2400).
		self.assertEqual(captured.get("timeout_s"), 2400)
		# Async: the Run stays "running" post-dispatch (Phase 3 writeback marks done).
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")
		# The generic prompt is NON-LEAKY (no rule/tool/threshold names).
		msg = captured.get("message") or ""
		for leak in ("jarvis__", "rule_id", "rule_pack", "pl_balance", "bs_balance", "$"):
			self.assertNotIn(leak, msg)
		self.assertIn(inst_name, msg)  # installation pointer for the config

	def test_delegate_dispatch_failure_marks_run_failed_and_reraises(self):
		inst_name = _install_as(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1)
		frappe.db.commit()
		inst = frappe.get_doc(INSTALLATION, inst_name)

		import jarvis.admin_client as admin_client

		def _boom(**kw):
			raise RuntimeError("admin unreachable")

		orig_run = admin_client.post_agent_run
		admin_client.post_agent_run = _boom
		original_user = frappe.session.user
		try:
			frappe.set_user(self.owner)
			with self.assertRaises(RuntimeError):
				agent_scheduler._launch_audit(inst, trigger="scheduled")
		finally:
			frappe.set_user(original_user)
			admin_client.post_agent_run = orig_run

		# The already-created Run is marked failed (never orphaned as "running").
		runs = frappe.get_all(RUN, filters={"installation": inst_name}, fields=["name", "status", "error"])
		self.assertTrue(runs)
		self.assertTrue(all(r.status == "failed" for r in runs))
		self.assertTrue(any("dispatch failed" in (r.error or "") for r in runs))

	def test_generic_prompt_is_non_leaky(self):
		delegate = frappe.get_doc(LISTING, "close-auditor")
		inst_name = _install_as(self.owner, "close-auditor")
		inst = frappe.get_doc(INSTALLATION, inst_name)
		scope = {
			"company": "Test Co",
			"fiscal_year": "2026-2027",
			"from_date": "2026-04-01",
			"to_date": "2027-03-31",
			"prior_fy_start": "2025-04-01",
			"prior_fy_end": "2026-03-31",
		}
		gen = agent_scheduler._audit_prompt(delegate, inst, "scheduled", scope)
		# The bench-injected run message names NO rule/tool/threshold/engine.
		for leak in ("jarvis__", "rule_id", "rule_pack", "pl_balance", "bs_balance", "$"):
			self.assertNotIn(leak, gen)
		self.assertIn("EXPLICIT SCOPE", gen)
		self.assertIn("2026-04-01", gen)  # scope injected verbatim (A6)
		self.assertIn(inst_name, gen)  # installation pointer

	# ------------------------------------------------------------------ #
	# (b) mutation authZ (S3)
	# ------------------------------------------------------------------ #
	def test_non_owner_cannot_set_enabled_another_owners_install(self):
		inst_name = _install_as(self.owner, "close-auditor")
		original_user = frappe.session.user
		frappe.set_user(self.other)
		try:
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_enabled(inst_name, 1)
		finally:
			frappe.set_user(original_user)
		# The install stays disabled — the non-owner write never landed.
		self.assertEqual(int(frappe.db.get_value(INSTALLATION, inst_name, "enabled")), 0)

	# ------------------------------------------------------------------ #
	# (d) catalog sync idempotency
	# ------------------------------------------------------------------ #
	def test_sync_agent_listings_idempotent(self):
		agent_catalog.sync_agent_listings()
		count1 = frappe.db.count(LISTING)
		r2 = agent_catalog.sync_agent_listings()
		count2 = frappe.db.count(LISTING)
		self.assertEqual(count1, count2)  # no dup rows on re-sync
		self.assertEqual(r2["created"], 0)  # nothing created the second time
		# The registry ships exactly the two delegate agents.
		published = set(frappe.get_all(LISTING, filters={"status": "Published"}, pluck="name"))
		self.assertIn("close-auditor", published)
		self.assertIn("bank-recon-operator", published)
		# Every shipped agent is delegate and BODY-FREE: the proprietary SKILL must
		# NEVER be stored in the customer DB (A2) — it lives only in the admin
		# bundle store.
		for slug in ("close-auditor", "bank-recon-operator"):
			row = frappe.db.get_value(LISTING, slug, ["delivery", "skill_bundle"], as_dict=True)
			self.assertEqual(row.delivery, "delegate")
			bundle = frappe.parse_json(row.skill_bundle) or []
			has_body = any((b or {}).get("body", "").strip() for b in bundle)
			self.assertFalse(has_body, f"{slug} (delegate) leaked a skill body into the DB")

	# ------------------------------------------------------------------ #
	# (d2) Phase 0A — delegate agent stub + body-free enablement signal
	# ------------------------------------------------------------------ #
	def test_delegate_agent_ships_stub_and_enablement_signal(self):
		"""A2 / Phase 0A: a delegate agent's SKILL body NEVER enters the customer
		DB, and its push-payload entry is a body-free ENABLEMENT SIGNAL that the
		admin relay (Phase 2C) routes by ``delivery == 'delegate'`` — carrying
		tools_allow / timeout_s / nature / model looked up from the bundled
		registry, no proprietary body."""
		DELEGATE = "close-auditor"
		# The Listing stub carries the metadata but NOT the body.
		row = frappe.db.get_value(LISTING, DELEGATE, ["delivery", "skill_bundle"], as_dict=True)
		self.assertEqual(row.delivery, "delegate")
		bundle = frappe.parse_json(row.skill_bundle) or []
		self.assertFalse(
			any((b or {}).get("body", "").strip() for b in bundle),
			"delegate agent leaked a SKILL body into the customer DB",
		)

		# Install + enable for an owner who can read what it scans (A12), so the
		# enablement signal is emitted rather than skipped. Accounts User grants
		# read on GL Entry / Account / Company.
		if frappe.db.exists("Role", "Accounts User"):
			_give_role(self.owner, "Accounts User")
		inst = _install_as(self.owner, DELEGATE)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1)
		frappe.db.commit()

		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		sig = next(p for p in payload if p["slug"] == f"agent-{DELEGATE}")
		self.assertEqual(sig["delivery"], "delegate")
		self.assertNotIn("body", sig)  # body-free — the whole point
		self.assertEqual(sig["nature"], "auditor")
		self.assertEqual(sig["timeout_s"], 2400)
		self.assertIn("model", sig)  # present (may be None) so 2C can default it
		self.assertIn("exec", sig["tools_allow"])
		self.assertIn("jarvis__get_balance_on", sig["tools_allow"])

	# ------------------------------------------------------------------ #
	# (e) RBAC — role-gated install / run (server-side enforcement)
	# ------------------------------------------------------------------ #
	def _restrict(self, slug: str, roles: list) -> None:
		original = frappe.session.user
		frappe.set_user("Administrator")
		try:
			agents_api.set_agent_roles(slug, roles)
		finally:
			frappe.set_user(original)

	def test_role_gated_install(self):
		self._restrict("close-auditor", [ROLE_X])

		# User WITHOUT the role: server-side PermissionError, no row created.
		frappe.set_user(self.other)
		try:
			with self.assertRaises(frappe.PermissionError):
				agents_api.install_agent("close-auditor")
		finally:
			frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists(INSTALLATION, {"owner": self.other, "agent": "close-auditor"}))

		# User WITH the role installs fine.
		inst = _install_as(self.owner, "close-auditor")
		self.assertTrue(frappe.db.exists(INSTALLATION, inst))

		# A System Manager (who does NOT hold ROLE_X) is always allowed.
		inst_admin = _install_as(self.admin, "close-auditor")
		self.assertTrue(frappe.db.exists(INSTALLATION, inst_admin))

	def test_role_gated_run_agent_now(self):
		# Install + enable while UNRESTRICTED, then restrict — the run gate must
		# catch an owner whose roles no longer permit the agent.
		inst_other = _install_as(self.other, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst_other, "enabled", 1)
		inst_owner = _install_as(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst_owner, "enabled", 1)
		frappe.db.commit()
		self._restrict("close-auditor", [ROLE_X])

		# Delegates dispatch via admin_client.post_agent_run (never chat send).
		import jarvis.admin_client as admin_client

		calls = []
		orig_run = admin_client.post_agent_run
		admin_client.post_agent_run = lambda **kw: (
			calls.append(frappe.session.user) or {"run_id": kw.get("run_id"), "status": "queued"}
		)
		try:
			# self.other lacks ROLE_X -> refused, and NO turn was dispatched.
			frappe.set_user(self.other)
			with self.assertRaises(frappe.PermissionError):
				agents_api.run_agent_now(inst_other)
			self.assertEqual(calls, [])

			# self.owner holds ROLE_X -> runs (dispatched as the run-as user).
			frappe.set_user(self.owner)
			result = agents_api.run_agent_now(inst_owner)
			self.assertTrue(result["ok"])
			self.assertEqual(calls, [self.owner])
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig_run

	def test_list_agents_allowed_flags_and_roles_roundtrip(self):
		res = None
		frappe.set_user("Administrator")
		res = agents_api.set_agent_roles("close-auditor", [ROLE_X])
		self.assertEqual(res["allowed_roles"], [ROLE_X])

		def _row(user):
			frappe.set_user(user)
			try:
				return next(r for r in agents_api.list_agents() if r["name"] == "close-auditor")
			finally:
				frappe.set_user("Administrator")

		blocked = _row(self.other)
		self.assertEqual(blocked["allowed"], 0)
		self.assertEqual(blocked["allowed_roles"], [ROLE_X])
		permitted = _row(self.owner)
		self.assertEqual(permitted["allowed"], 1)
		sm = _row(self.admin)  # System Manager: always allowed
		self.assertEqual(sm["allowed"], 1)

		# [] clears the restriction -> unrestricted for everyone.
		res = agents_api.set_agent_roles("close-auditor", [])
		self.assertEqual(res["allowed_roles"], [])
		self.assertEqual(_row(self.other)["allowed"], 1)
		self.assertEqual(_row(self.other)["allowed_roles"], [])

	# ------------------------------------------------------------------ #
	# (f) RBAC — admin endpoints are System Manager ONLY
	# ------------------------------------------------------------------ #
	def test_admin_endpoints_reject_non_system_manager(self):
		frappe.set_user(self.other)
		try:
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_agent_roles("close-auditor", [ROLE_X])
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_listing_status("close-auditor", "Deprecated")
			with self.assertRaises(frappe.PermissionError):
				agents_api.get_agent_admin_overview()
		finally:
			frappe.set_user("Administrator")
		# Nothing leaked through: listing untouched.
		self.assertEqual(frappe.db.get_value(LISTING, "close-auditor", "status"), "Published")
		self.assertEqual(frappe.get_all(ALLOWED_ROLE, filters={"parent": "close-auditor"}, pluck="role"), [])

	def test_set_listing_status_valid_and_invalid(self):
		frappe.set_user(self.admin)  # a real SM user, not Administrator
		try:
			res = agents_api.set_listing_status("close-auditor", "Coming Soon")
			self.assertEqual(res["status"], "Coming Soon")
			self.assertEqual(frappe.db.get_value(LISTING, "close-auditor", "status"), "Coming Soon")
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_listing_status("close-auditor", "Draft")  # registry-only
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_listing_status("close-auditor", "bogus")
		finally:
			frappe.set_user("Administrator")
			agents_api.set_listing_status("close-auditor", "Published")  # restore

	def test_get_agent_admin_overview_shape(self):
		inst = _install_as(self.owner, "close-auditor")
		frappe.set_user("Administrator")
		agents_api.set_agent_roles("close-auditor", [ROLE_X])

		frappe.set_user(self.admin)
		try:
			out = agents_api.get_agent_admin_overview()
		finally:
			frappe.set_user("Administrator")

		for excluded in ("Administrator", "Guest", "All"):
			self.assertNotIn(excluded, out["roles"])
		self.assertIn(ROLE_X, out["roles"])

		row = next(l for l in out["listings"] if l["agent_slug"] == "close-auditor")
		self.assertEqual(row["allowed_roles"], [ROLE_X])
		self.assertEqual(row["status"], "Published")
		install_row = next(i for i in row["installs"] if i["installation"] == inst)
		self.assertEqual(install_row["owner"], self.owner)
		for key in (
			"enabled",
			"run_as_user",
			"schedule_enabled",
			"schedule_frequency",
			"next_run_at",
			"last_run_at",
			"sync_status",
		):
			self.assertIn(key, install_row)

	def test_admin_overview_surfaces_run_as_user_including_blank(self):
		"""R1-S2: the cross-owner Admin feed carries the EXECUTING identity, so an
		admin reading an Apply/run failure can see WHICH install is misconfigured
		without dropping to raw Desk. A blank one must arrive as a falsy value (the
		badge case), not be omitted."""
		good = self._enable_for(self.owner, "close-auditor")
		legacy = self._make_legacy_enabled(self.other, "close-auditor")

		frappe.set_user(self.admin)  # a System Manager
		try:
			out = agents_api.get_agent_admin_overview()
		finally:
			frappe.set_user("Administrator")

		rows = {i["installation"]: i for lst in out["listings"] for i in lst["installs"]}
		self.assertEqual(rows[good]["run_as_user"], self.owner)
		self.assertIn(legacy, rows)
		self.assertFalse(rows[legacy]["run_as_user"])

	# ------------------------------------------------------------------ #
	# (g) RBAC — scheduler skips an owner whose roles were revoked
	# ------------------------------------------------------------------ #
	def test_scheduler_skips_and_records_when_owner_lost_role(self):
		from frappe.utils import add_days, now_datetime

		inst_name = _install_as(self.owner, "close-auditor")
		now = now_datetime()
		frappe.db.set_value(
			INSTALLATION,
			inst_name,
			{
				"enabled": 1,
				"schedule_enabled": 1,
				"schedule_frequency": "daily",
				"next_run_at": add_days(now, -1),
			},
		)
		frappe.db.commit()
		# ROLE_Y is held by NOBODY -> the owner's roles no longer permit the agent.
		self._restrict("close-auditor", [ROLE_Y])

		# Insulate from any OTHER due installation on this (dev) site: push their
		# slots out and restore afterwards, so the cron run only touches ours.
		parked = {
			r.name: r.next_run_at
			for r in frappe.get_all(
				INSTALLATION,
				filters={
					"enabled": 1,
					"schedule_enabled": 1,
					"next_run_at": ["<=", now],
					"name": ["!=", inst_name],
				},
				fields=["name", "next_run_at"],
			)
		}
		for n in parked:
			frappe.db.set_value(INSTALLATION, n, "next_run_at", add_days(now, 2), update_modified=False)
		frappe.db.commit()

		import jarvis.chat.api as chat_api

		calls = []
		orig_send = chat_api.send_message
		chat_api.send_message = lambda **kw: calls.append(kw) or {"ok": True}
		try:
			agent_scheduler.run_due_agent_audits()
		finally:
			chat_api.send_message = orig_send
			for n, ts in parked.items():
				frappe.db.set_value(INSTALLATION, n, "next_run_at", ts, update_modified=False)
			frappe.db.commit()

		# NO turn was dispatched.
		self.assertEqual(calls, [])
		# A failed run records WHY, owned by the installation owner.
		runs = frappe.get_all(
			RUN,
			filters={"installation": inst_name, "status": "failed"},
			fields=["owner", "error"],
		)
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0]["owner"], self.owner)
		self.assertIn("roles no longer permit", runs[0]["error"])
		# The slot was consumed: next_run_at advanced into the future.
		inst = frappe.db.get_value(INSTALLATION, inst_name, ["next_run_at", "last_run_at"], as_dict=True)
		self.assertIsNotNone(inst.last_run_at)
		self.assertGreater(inst.next_run_at, now)

	# ------------------------------------------------------------------ #
	# (h) RBAC — sync preserves admin roles; push payload excludes blocked
	# ------------------------------------------------------------------ #
	def test_sync_agent_listings_preserves_allowed_roles(self):
		self._restrict("close-auditor", [ROLE_X])
		agent_catalog.sync_agent_listings()  # re-sync from the bundled registry
		roles = frappe.get_all(
			ALLOWED_ROLE,
			filters={"parenttype": LISTING, "parent": "close-auditor"},
			pluck="role",
		)
		self.assertEqual(roles, [ROLE_X])
		# ... while registry-owned fields WERE re-synced (still Published).
		self.assertEqual(frappe.db.get_value(LISTING, "close-auditor", "status"), "Published")

	def test_sync_seeds_default_allowed_roles_on_insert_only(self):
		# The Custom App Learning scribe ships its admin-only restriction ON BY
		# DEFAULT via the manifest ``default_allowed_roles``. sync seeds it on the
		# INSERT branch ONLY, and a re-sync must NEVER clobber an admin's later edit.
		slug = "custom-app-learning"
		_ensure_role("Jarvis Admin")
		# Force the INSERT branch: drop the listing + any allowed_roles rows.
		frappe.db.delete(ALLOWED_ROLE, {"parenttype": LISTING, "parent": slug})
		if frappe.db.exists(LISTING, slug):
			frappe.delete_doc(LISTING, slug, force=True, ignore_permissions=True)
		frappe.db.commit()

		agent_catalog.sync_agent_listings()  # INSERT → seed the default restriction
		seeded = set(
			frappe.get_all(ALLOWED_ROLE, filters={"parenttype": LISTING, "parent": slug}, pluck="role")
		)
		self.assertEqual(seeded, {"System Manager", "Jarvis Admin"})

		# An admin narrows it; a re-sync (UPDATE branch) must leave the edit intact.
		self._restrict(slug, [ROLE_X])
		agent_catalog.sync_agent_listings()
		after = frappe.get_all(ALLOWED_ROLE, filters={"parenttype": LISTING, "parent": slug}, pluck="role")
		self.assertEqual(after, [ROLE_X])  # NOT re-seeded / clobbered

	def test_push_payload_excludes_install_of_blocked_owner(self):
		inst_name = _install_as(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst_name, "enabled", 1)
		frappe.db.commit()

		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		self.assertTrue(any(p["slug"] == "agent-close-auditor" for p in payload))

		self._restrict("close-auditor", [ROLE_Y])  # owner does NOT hold ROLE_Y
		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		self.assertEqual(payload, [])

		self._restrict("close-auditor", [])  # clear -> included again
		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		self.assertTrue(any(p["slug"] == "agent-close-auditor" for p in payload))

	# ------------------------------------------------------------------ #
	# (i) P0-B — the payload is keyed by SLUG, installs are per-(owner, agent)
	#
	# Two users each enabling the same agent used to emit the slug TWICE; admin
	# rejects a duplicate slug outright ("invalid: duplicate agent skill slug"),
	# which killed EVERY agent push from the bench, not just that agent.
	# ------------------------------------------------------------------ #
	def _enable_for(self, owner: str, slug: str) -> str:
		inst = _install_as(owner, slug)
		frappe.db.set_value(INSTALLATION, inst, "enabled", 1)
		frappe.db.commit()
		return inst

	def _count_slug(self, payload: list, slug: str) -> int:
		return sum(1 for p in payload if p["slug"] == slug)

	def test_push_payload_emits_one_entry_per_slug_across_owners(self):
		"""P0-B: two owners, same agent, both enabled -> the slug appears ONCE."""
		self._enable_for(self.owner, "close-auditor")
		self._enable_for(self.other, "close-auditor")

		payload = agent_catalog.build_agent_push_payload()  # bench-global, as the push is
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 1)
		# ...and the payload as a whole carries no duplicate slug at all, which is
		# exactly what admin validates.
		slugs = [p["slug"] for p in payload]
		self.assertEqual(len(slugs), len(set(slugs)), f"duplicate slug in payload: {slugs}")

	def test_push_payload_dedupe_is_union_not_last_wins(self):
		"""A slug ships if ANY enabled install clears the gates — a blocked or
		non-installable sibling row must not suppress a qualifying one."""
		self._enable_for(self.owner, "close-auditor")
		blocked = self._enable_for(self.other, "close-auditor")

		# (a) RBAC: only self.owner holds ROLE_X, so self.other's row is excluded.
		self._restrict("close-auditor", [ROLE_X])
		payload = agent_catalog.build_agent_push_payload()
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 1)

		# ...and once BOTH rows qualify again it is still ONE entry, not two.
		self._restrict("close-auditor", [])
		payload = agent_catalog.build_agent_push_payload()
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 1)

		# (b) installability: self.other's row is reconciled non-installable.
		frappe.db.set_value(
			INSTALLATION,
			blocked,
			{"installable": 0, "not_installable_reason": "app_absent_or_ineligible"},
			update_modified=False,
		)
		frappe.db.commit()
		payload = agent_catalog.build_agent_push_payload()
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 1)

	def test_push_payload_omits_slug_whose_only_install_is_unpublished(self):
		self._enable_for(self.owner, "close-auditor")
		frappe.set_user("Administrator")
		try:
			agents_api.set_listing_status("close-auditor", "Coming Soon")
			payload = agent_catalog.build_agent_push_payload(owner=self.owner)
			self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 0)
		finally:
			agents_api.set_listing_status("close-auditor", "Published")  # restore

	def test_push_payload_order_is_deterministic(self):
		"""The payload is a full reconcile that admin/fleet diffs, so a rebuild of
		unchanged data must produce a byte-identical list."""
		self._enable_for(self.owner, "close-auditor")
		self._enable_for(self.other, "close-auditor")
		self._enable_for(self.owner, "ledger-scrutiny-auditor")

		first = agent_catalog.build_agent_push_payload()
		second = agent_catalog.build_agent_push_payload()
		self.assertEqual(first, second)
		slugs = [p["slug"] for p in first]
		self.assertEqual(slugs, sorted(slugs))

	# ------------------------------------------------------------------ #
	# R1-F2 — the de-dupe short-circuits BEFORE the per-row queries
	# ------------------------------------------------------------------ #
	def test_push_payload_short_circuit_preserves_the_slug_set(self):
		"""The short-circuit skips WORK, never an OUTCOME.

		Oracle: a per-OWNER build can never short-circuit — (owner, agent) is
		unique, so each such build sees at most one row per agent — which makes the
		union of the per-owner builds an independent reference for what the
		bench-global (short-circuiting) build must ship."""
		self._enable_for(self.owner, "close-auditor")
		self._enable_for(self.other, "close-auditor")
		self._enable_for(self.other, "ledger-scrutiny-auditor")
		# A blocked sibling that must not suppress the qualifying row (union).
		blocked = self._enable_for(self.admin, "close-auditor")
		frappe.db.set_value(
			INSTALLATION,
			blocked,
			{"installable": 0, "not_installable_reason": "app_absent_or_ineligible"},
			update_modified=False,
		)
		frappe.db.commit()

		owners = {r.owner for r in frappe.get_all(INSTALLATION, filters={"enabled": 1}, fields=["owner"])}
		reference = set()
		for o in owners:
			reference |= {p["slug"] for p in agent_catalog.build_agent_push_payload(owner=o)}

		payload = agent_catalog.build_agent_push_payload()
		self.assertEqual({p["slug"] for p in payload}, reference)
		# ...and still exactly one entry per slug.
		slugs = [p["slug"] for p in payload]
		self.assertEqual(len(slugs), len(set(slugs)), f"duplicate slug in payload: {slugs}")
		self.assertIn("agent-close-auditor", reference)
		self.assertIn("agent-ledger-scrutiny-auditor", reference)

	def test_push_payload_short_circuit_skips_the_per_row_rbac_query(self):
		"""The RBAC gate (an N+1 on the allowed-role child table) is evaluated once
		per distinct AGENT, not once per install row."""
		self._enable_for(self.owner, "close-auditor")
		self._enable_for(self.other, "close-auditor")
		self._enable_for(self.admin, "close-auditor")

		calls = []
		orig = agents_api._user_allowed_for_agent
		agents_api._user_allowed_for_agent = lambda listing, user=None: (
			calls.append(listing) or orig(listing, user)
		)
		try:
			payload = agent_catalog.build_agent_push_payload()
		finally:
			agents_api._user_allowed_for_agent = orig

		close_calls = [c for c in calls if c == "close-auditor"]
		self.assertEqual(len(close_calls), 1, f"RBAC gate re-run per install row: {calls}")
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 1)

	# ------------------------------------------------------------------ #
	# (j) P0-B part 2 — a legacy install (run_as_user NULL) must be DISABLEABLE
	#
	# Without this there is no UI escape from a bricked push: set_enabled saves
	# the doc, validate() demanded a run-as user, and reqd:1 blocked it too.
	# ------------------------------------------------------------------ #
	def _make_legacy_enabled(self, owner: str, slug: str) -> str:
		"""An install as it exists on benches that predate run_as_user: enabled,
		with a NULL executing identity."""
		inst = _install_as(owner, slug)
		frappe.db.set_value(INSTALLATION, inst, {"enabled": 1, "run_as_user": None}, update_modified=False)
		frappe.db.commit()
		return inst

	def test_legacy_install_without_run_as_user_can_be_disabled(self):
		inst = self._make_legacy_enabled(self.owner, "close-auditor")
		frappe.set_user(self.owner)
		try:
			res = agents_api.set_enabled(inst, 0)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(res["data"]["enabled"], 0)
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "enabled"), 0)
		# The escape actually clears the push: the slug is gone from the payload.
		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 0)

	def test_enabling_without_run_as_user_still_throws(self):
		inst = self._make_legacy_enabled(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst, "enabled", 0, update_modified=False)
		frappe.db.commit()
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.ValidationError):
				agents_api.set_enabled(inst, 1)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "enabled"), 0)

	def test_run_as_escalation_guard_still_blocks_cross_user_mapping(self):
		"""A4: a non-admin owner may map the agent only to THEMSELVES — unchanged
		by the blank-is-ok-while-disabled relaxation."""
		inst = _install_as(self.owner, "close-auditor")
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_run_as_user(inst, self.other)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(INSTALLATION, inst, "run_as_user"), self.owner)

	def test_run_as_escalation_guard_holds_on_a_disabled_legacy_row(self):
		"""The blank short-circuit must not become a staging ground: assigning a
		run_as_user to a DISABLED row is litigated exactly as on an enabled one,
		so a later enable cannot wave an unvetted mapping through."""
		inst = self._make_legacy_enabled(self.owner, "close-auditor")
		frappe.db.set_value(INSTALLATION, inst, "enabled", 0, update_modified=False)
		frappe.db.commit()
		frappe.set_user(self.owner)
		try:
			with self.assertRaises(frappe.PermissionError):
				agents_api.set_run_as_user(inst, self.admin)  # a System Manager
		finally:
			frappe.set_user("Administrator")
		self.assertIsNone(frappe.db.get_value(INSTALLATION, inst, "run_as_user") or None)

	# ------------------------------------------------------------------ #
	# (k) R1-F3 — a blank run_as_user is REFUSED at every DISPATCH entry point
	#
	# ``reqd`` on run_as_user had to go (a disabled legacy row must stay
	# DISABLEABLE) and the controller check that replaced it lives in validate(),
	# which frappe skips WHOLESALE under ``flags.ignore_validate``
	# (run_before_save_methods returns early; ``_validate`` — the reqd enforcer —
	# is the one that always runs). An ENABLED install with no executing identity
	# is therefore persistable by a future seeder/importer, and the old
	# ``run_as_user or owner`` fallback would have silently bound its ERP reads to
	# the row OWNER — an identity ``_validate_run_as_escalation`` never litigated.
	# Every dispatch path must refuse instead, diagnosably.
	# ------------------------------------------------------------------ #
	def _no_dispatch(self, **kw):
		raise AssertionError("an install with no run-as user must never reach dispatch")

	def test_launch_audit_refuses_blank_run_as_user(self):
		"""The shared choke point BOTH dispatch paths funnel through."""
		import jarvis.admin_client as admin_client

		inst_name = self._make_legacy_enabled(self.owner, "close-auditor")
		inst = frappe.get_doc(INSTALLATION, inst_name)
		convs_before = frappe.db.count("Jarvis Conversation")

		orig_run = admin_client.post_agent_run
		admin_client.post_agent_run = self._no_dispatch
		try:
			frappe.set_user(self.owner)
			with self.assertRaises(frappe.ValidationError) as ctx:
				agent_scheduler._launch_audit(inst, trigger="manual")
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig_run

		self.assertIn("run-as user", str(ctx.exception))
		# Refused BEFORE anything is written — no orphan conversation, no orphan Run.
		self.assertEqual(frappe.db.count("Jarvis Conversation"), convs_before)
		self.assertEqual(frappe.get_all(RUN, filters={"installation": inst_name}, pluck="name"), [])

	def test_run_agent_now_refuses_blank_run_as_user(self):
		"""Manual entry point — refused, never silently run as the row owner."""
		import jarvis.admin_client as admin_client

		inst_name = self._make_legacy_enabled(self.owner, "close-auditor")
		orig_run = admin_client.post_agent_run
		admin_client.post_agent_run = self._no_dispatch
		try:
			frappe.set_user(self.owner)
			with self.assertRaises(frappe.ValidationError) as ctx:
				agents_api.run_agent_now(inst_name)
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig_run

		self.assertIn("run-as user", str(ctx.exception))
		self.assertEqual(frappe.get_all(RUN, filters={"installation": inst_name}, pluck="name"), [])

	def test_scheduler_refuses_blank_run_as_user_and_consumes_the_slot(self):
		"""Scheduled entry point — skipped with a RECORDED reason (an operator has
		to be able to diagnose it), and the slot is consumed so a misconfiguration
		does not busy-retry every hour for ever."""
		from frappe.utils import add_days, now_datetime

		import jarvis.admin_client as admin_client

		inst_name = self._make_legacy_enabled(self.owner, "close-auditor")
		now = now_datetime()
		frappe.db.set_value(
			INSTALLATION,
			inst_name,
			{"schedule_enabled": 1, "schedule_frequency": "daily", "next_run_at": add_days(now, -1)},
			update_modified=False,
		)
		frappe.db.commit()

		# Insulate from any OTHER due installation on this (dev) site: park their
		# slots and restore afterwards, so the cron run only touches ours.
		parked = {
			r.name: r.next_run_at
			for r in frappe.get_all(
				INSTALLATION,
				filters={
					"enabled": 1,
					"schedule_enabled": 1,
					"next_run_at": ["<=", now],
					"name": ["!=", inst_name],
				},
				fields=["name", "next_run_at"],
			)
		}
		for n in parked:
			frappe.db.set_value(INSTALLATION, n, "next_run_at", add_days(now, 2), update_modified=False)
		frappe.db.commit()

		orig_run = admin_client.post_agent_run
		admin_client.post_agent_run = self._no_dispatch
		try:
			agent_scheduler.run_due_agent_audits()
		finally:
			admin_client.post_agent_run = orig_run
			for n, ts in parked.items():
				frappe.db.set_value(INSTALLATION, n, "next_run_at", ts, update_modified=False)
			frappe.db.commit()

		runs = frappe.get_all(RUN, filters={"installation": inst_name}, fields=["owner", "status", "error"])
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0]["status"], "failed")  # never "running"
		self.assertEqual(runs[0]["owner"], self.owner)  # the customer sees WHY
		self.assertIn("no run-as user", runs[0]["error"])
		# The slot was consumed: next_run_at advanced into the future.
		row = frappe.db.get_value(INSTALLATION, inst_name, ["next_run_at", "last_run_at"], as_dict=True)
		self.assertIsNotNone(row.last_run_at)
		self.assertGreater(row.next_run_at, now)

	def test_dispatch_paths_are_unchanged_for_a_valid_run_as_user(self):
		"""The refusal must not perturb the normal case: an install carrying a
		valid run-as user still dispatches, under THAT identity."""
		import jarvis.admin_client as admin_client

		inst_name = self._enable_for(self.owner, "close-auditor")
		captured = {}
		orig_run = admin_client.post_agent_run

		def _cap(**kw):
			captured["user"] = frappe.session.user
			return {"run_id": kw.get("run_id"), "status": "queued"}

		admin_client.post_agent_run = _cap
		try:
			frappe.set_user(self.owner)
			res = agents_api.run_agent_now(inst_name)
		finally:
			frappe.set_user("Administrator")
			admin_client.post_agent_run = orig_run

		self.assertTrue(res["ok"])
		self.assertEqual(captured.get("user"), self.owner)
		self.assertEqual(frappe.db.get_value(RUN, res["data"]["run"], "status"), "running")

	# ------------------------------------------------------------------ #
	# (l) CX1-1 — a blank-identity install must not reach the CONTAINER either
	#
	# The gap left between the schema relaxation and global push eligibility:
	# R1-F3 makes every RUN path refuse an enabled install with no run-as user,
	# but the push payload never looked at the field, so the slug still shipped —
	# provisioning the delegate, seating it in the container roster and the
	# tenant's agent_roster, and advertising an agent the bench can never run.
	# ------------------------------------------------------------------ #
	def test_push_payload_omits_slug_whose_only_install_has_no_run_as_user(self):
		"""An enabled-but-blank install is the ONLY install of the slug -> the slug
		is absent from the payload entirely."""
		inst = self._make_legacy_enabled(self.owner, "close-auditor")
		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 0)

		# ...and it is the BLANK IDENTITY that excluded it, not some unrelated gate:
		# give the very same row a run-as user and the same build ships the slug.
		frappe.db.set_value(INSTALLATION, inst, "run_as_user", self.owner, update_modified=False)
		frappe.db.commit()
		payload = agent_catalog.build_agent_push_payload(owner=self.owner)
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 1)

	def test_push_payload_union_survives_a_blank_run_as_user_sibling(self):
		"""UNION semantics: owner A valid + owner B blank, SAME slug -> the slug
		still ships, exactly ONCE. The blank row must disqualify only itself."""
		a = self._enable_for(self.owner, "close-auditor")
		b = self._enable_for(self.other, "close-auditor")
		# Installs are hash-named and the build iterates ``agent asc, name asc``, so
		# blank whichever row sorts FIRST: that is the ordering in which a gate that
		# wrongly suppressed the agent would actually bite (the blank row reached
		# BEFORE the valid one).
		frappe.db.set_value(INSTALLATION, min(a, b), "run_as_user", None, update_modified=False)
		frappe.db.commit()

		payload = agent_catalog.build_agent_push_payload()
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 1)
		slugs = [p["slug"] for p in payload]
		self.assertEqual(len(slugs), len(set(slugs)), f"duplicate slug in payload: {slugs}")

		# The surviving entry is the normal, complete enablement signal.
		entry = next(p for p in payload if p["slug"] == "agent-close-auditor")
		self.assertEqual(entry["delivery"], "delegate")

		# Blanking the OTHER row too (no valid install left) drops the slug — proof
		# the entry above came from the qualifying sibling, not from a leaky gate.
		frappe.db.set_value(INSTALLATION, max(a, b), "run_as_user", None, update_modified=False)
		frappe.db.commit()
		payload = agent_catalog.build_agent_push_payload()
		self.assertEqual(self._count_slug(payload, "agent-close-auditor"), 0)
