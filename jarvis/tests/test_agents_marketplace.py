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
		# dispatch (admin -> fleet) so no real admin/openclaw is needed.
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
			"schedule_enabled",
			"schedule_frequency",
			"next_run_at",
			"last_run_at",
			"sync_status",
		):
			self.assertIn(key, install_row)

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
			frappe.get_all(
				ALLOWED_ROLE, filters={"parenttype": LISTING, "parent": slug}, pluck="role"
			)
		)
		self.assertEqual(seeded, {"System Manager", "Jarvis Admin"})

		# An admin narrows it; a re-sync (UPDATE branch) must leave the edit intact.
		self._restrict(slug, [ROLE_X])
		agent_catalog.sync_agent_listings()
		after = frappe.get_all(
			ALLOWED_ROLE, filters={"parenttype": LISTING, "parent": slug}, pluck="role"
		)
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
