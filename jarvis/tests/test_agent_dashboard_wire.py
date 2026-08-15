"""Phase-4 tests: delegate run → saved Jarvis Dashboard + A8 session teardown.

Covers the completion wire end-to-end WITHOUT a live model turn (the delegate
output is mocked — findings passed straight to the writeback, and the
``jarvis__save_agent_dashboard`` tool driven exactly as the plugin path would):

  (1) a completed run with findings but NO delegate-authored canvas still yields
      ONE saved Jarvis Dashboard (the server-generated A2-safe floor), linked on
      Run.dashboard, owned + scoped to the human owner, and the disclosure
      surface never leaks the opaque token / rule id;
  (2) a delegate-authored dashboard (via save_agent_dashboard) WINS and is not
      duplicated by the floor — exactly one dashboard per run;
  (3) the per-run Jarvis Chat Session bearer is torn down on finalize (A8);
  (4) the stale-run reaper fails a run stuck ``running`` and deletes its orphaned
      session row (A8 backstop).

Run: bench --site patterntest.localhost run-tests --module
     jarvis.tests.test_agent_dashboard_wire
"""

import json
import unittest

import frappe

from jarvis.chat import agent_catalog

RUN = "Jarvis Agent Run"
FINDING = "Jarvis Agent Finding"
INSTALLATION = "Jarvis Agent Installation"
LISTING = "Jarvis Agent Listing"
DASHBOARD = "Jarvis Dashboard"
SESSION = "Jarvis Chat Session"

AGENT = "close-auditor"
COMPANY = "Jarvis Dash Test Co"
FY = "2026-2027"
# The close-auditor TB-balance blocker token (Company-keyed; opaque, A2).
TB_TOKEN = "ca-cl-7f31"


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
	if "Jarvis User" not in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).add_roles("Jarvis User")
	frappe.db.commit()
	return email


def _ensure_company() -> str:
	"""A bare Company row (patterntest never ran the erpnext setup wizard). We only
	need a row that exists() so a Company-keyed finding ref verifies."""
	if not frappe.db.exists("Company", COMPANY):
		c = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": "JDTC",
				"default_currency": "INR",
				"country": "India",
			}
		)
		c.name = COMPANY
		c.flags.ignore_links = True
		c.flags.ignore_mandatory = True
		c.db_insert()
		frappe.db.commit()
	return COMPANY


class TestAgentDashboardWire(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		agent_catalog.sync_agent_listings()
		_ensure_company()
		cls.owner = _ensure_user("adw-owner@example.com")
		cls._ensure_installation()
		# The token the wire validates against must be in the synced listing.
		tokens = json.loads(frappe.db.get_value(LISTING, AGENT, "rule_tokens") or "[]")
		assert TB_TOKEN in tokens, f"{TB_TOKEN} not in synced rule_tokens {tokens}"
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._reset()
		for n in frappe.get_all(INSTALLATION, filters={"owner": cls.owner}, pluck="name"):
			frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
		frappe.db.sql("delete from `tabCompany` where name=%s", COMPANY)
		frappe.db.commit()

	@classmethod
	def _ensure_installation(cls):
		for n in frappe.get_all(INSTALLATION, filters={"owner": cls.owner}, pluck="name"):
			frappe.delete_doc(INSTALLATION, n, force=True, ignore_permissions=True)
		name = frappe.generate_hash(length=12)
		doc = frappe.get_doc(
			{
				"doctype": INSTALLATION,
				"name": name,
				"agent": AGENT,
				"enabled": 1,
				"run_as_user": cls.owner,
				# PP-4: these wire tests assert the LIVE owner-facing attestation surface
				# ("No exceptions were found", owner-owned dashboard). A shadow install
				# would (correctly) suppress the clean attestation and re-home the output
				# to the reviewer, so activate live for this attestation fixture.
				"reviewer": cls.owner,
				"activation_state": "live",
				"schedule_enabled": 0,
				"config": json.dumps({"company": COMPANY, "fiscal_year": FY}),
			}
		)
		doc.flags.ignore_permissions = True
		doc.db_insert()
		frappe.db.set_value(INSTALLATION, name, "owner", cls.owner, update_modified=False)
		cls.inst_name = name

	def setUp(self):
		frappe.set_user("Administrator")
		self._reset()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._reset()

	@classmethod
	def _reset(cls):
		for dt in (FINDING, RUN, DASHBOARD):
			for n in frappe.get_all(dt, filters={"owner": cls.owner}, pluck="name"):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		for n in frappe.get_all(SESSION, filters={"user": cls.owner}, pluck="name"):
			frappe.delete_doc(SESSION, n, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ------------------------------------------------------------------ #
	# helpers
	# ------------------------------------------------------------------ #
	def _mk_run(self, session_key: str) -> str:
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"agent": AGENT,
				"installation": self.inst_name,
				"trigger": "manual",
				"status": "running",
				"session_key": session_key,
				"started_at": frappe.utils.now(),
				"scope_json": json.dumps({"company": COMPANY, "fiscal_year": FY}),
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		frappe.db.set_value(RUN, run.name, "owner", self.owner, update_modified=False)
		frappe.db.commit()
		return run.name

	def _mint_session(self, session_key: str) -> None:
		frappe.get_doc(
			{
				"doctype": SESSION,
				"session_key": session_key,
				"user": self.owner,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	def _call_record(self, session_key, **kwargs):
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools.record_agent_run import record_agent_run

		frappe.set_user(self.owner)
		_agent_run_ctx.set_session_key(session_key)
		try:
			return record_agent_run(**kwargs)
		finally:
			_agent_run_ctx.clear_session_key()
			frappe.set_user("Administrator")

	def _call_save_dashboard(self, session_key, **kwargs):
		from jarvis.tools import _agent_run_ctx
		from jarvis.tools.save_agent_dashboard import save_agent_dashboard

		frappe.set_user(self.owner)
		_agent_run_ctx.set_session_key(session_key)
		try:
			return save_agent_dashboard(**kwargs)
		finally:
			_agent_run_ctx.clear_session_key()
			frappe.set_user("Administrator")

	@staticmethod
	def _blocker_finding():
		return {
			"token": TB_TOKEN,
			"ref_doctype": "Company",
			"ref_name": COMPANY,
			"amount": 1000.0,
			"severity": "blocker",
			# PP-1: every evaluator finding carries its epistemic result_class or it is
			# dropped at the tool boundary (a TB imbalance is a direct record fact).
			"result_class": "observed_fact",
			"note": "Trial balance does not balance: debits vs credits out by 1000.00.",
		}

	# ------------------------------------------------------------------ #
	# (1) completion wire — server-generated floor dashboard
	# ------------------------------------------------------------------ #
	def test_completion_wire_creates_minimal_dashboard_and_links_run(self):
		sk = "agent:agent-close-auditor:dash-floor"
		run = self._mk_run(sk)
		out = self._call_record(
			sk,
			findings=[self._blocker_finding()],
			coverage={TB_TOKEN: "evaluated"},
			scope={"company": COMPANY, "fiscal_year": FY},
		)

		self.assertEqual(out["status"], "completed")
		self.assertEqual(out["findings_count"], 1)
		dash = out["dashboard"]
		self.assertTrue(dash, "a findings run must produce a saved dashboard")
		self.assertEqual(frappe.db.get_value(RUN, run, "dashboard"), dash)

		d = frappe.get_doc(DASHBOARD, dash)
		self.assertEqual(d.owner, self.owner)  # row-owned by the human owner
		self.assertEqual(d.scope, "User")
		self.assertEqual(d.target_user, self.owner)  # visible in the owner's list
		self.assertEqual(d.dashboard_type, "Static")  # no live sources
		self.assertIn(FY, d.dashboard_title)
		# The authored outcome text is rendered...
		self.assertIn("Trial balance does not balance", d.html)
		# ...but the opaque token / rule id is NEVER on the disclosure surface (A2).
		self.assertNotIn(TB_TOKEN, d.html)

	def test_all_clear_run_still_gets_a_dashboard(self):
		sk = "agent:agent-close-auditor:dash-clear"
		self._mk_run(sk)
		# PP-2: a genuine all-clear must evaluate EVERY declared required check (the
		# authoritative bar is the listing's rule_tokens). A subset-coverage run is
		# correctly partial and suppresses the "No exceptions" attestation.
		declared = json.loads(frappe.db.get_value(LISTING, AGENT, "rule_tokens") or "[]")
		out = self._call_record(
			sk,
			findings=[],
			coverage={t: "evaluated" for t in declared},
			scope={"company": COMPANY, "fiscal_year": FY},
		)
		self.assertEqual(out["status"], "completed")
		self.assertTrue(out["dashboard"])
		self.assertIn("No exceptions", frappe.db.get_value(DASHBOARD, out["dashboard"], "html"))

	# ------------------------------------------------------------------ #
	# (2) delegate-authored dashboard wins; no duplicate floor
	# ------------------------------------------------------------------ #
	def test_delegate_authored_dashboard_is_kept_and_not_duplicated(self):
		sk = "agent:agent-close-auditor:dash-authored"
		run = self._mk_run(sk)
		html = (
			"<!doctype html><html><head><title>Close</title></head><body>Authored close summary</body></html>"
		)
		saved = self._call_save_dashboard(sk, html=html, title="My Close Summary")
		d1 = saved["dashboard"]
		self.assertTrue(d1)
		# save_agent_dashboard links it on the run immediately (survives a crash).
		self.assertEqual(frappe.db.get_value(RUN, run, "dashboard"), d1)

		out = self._call_record(
			sk,
			findings=[self._blocker_finding()],
			coverage={TB_TOKEN: "evaluated"},
			scope={"company": COMPANY, "fiscal_year": FY},
			dashboard=d1,
		)
		self.assertEqual(out["dashboard"], d1)
		self.assertEqual(frappe.db.get_value(RUN, run, "dashboard"), d1)
		# EXACTLY ONE dashboard from this run — the authored one, no server floor.
		owned = frappe.get_all(DASHBOARD, filters={"owner": self.owner}, pluck="name")
		self.assertEqual(owned, [d1])
		self.assertIn("Authored close summary", frappe.db.get_value(DASHBOARD, d1, "html"))

	def test_save_dashboard_rejects_non_run_session(self):
		from jarvis.exceptions import InvalidArgumentError

		with self.assertRaises(InvalidArgumentError):
			self._call_save_dashboard(
				"agent:agent-close-auditor:no-such-run", html="<html><body>x</body></html>"
			)

	# ------------------------------------------------------------------ #
	# (3) A8 session teardown on finalize
	# ------------------------------------------------------------------ #
	def test_session_row_torn_down_on_finalize(self):
		sk = "agent:agent-close-auditor:dash-teardown"
		run = self._mk_run(sk)
		self._mint_session(sk)
		self.assertTrue(frappe.db.exists(SESSION, {"session_key": sk}))

		self._call_record(sk, findings=[], coverage={}, scope={"company": COMPANY})
		self.assertFalse(
			frappe.db.exists(SESSION, {"session_key": sk}),
			"the per-run session bearer must be deleted at finalize (A8)",
		)
		# The session_key string stays stamped on the Run for audit (harmless now).
		self.assertEqual(frappe.db.get_value(RUN, run, "session_key"), sk)

	# ------------------------------------------------------------------ #
	# (4) A8 stale-run reaper
	# ------------------------------------------------------------------ #
	def test_reaper_fails_stuck_run_and_removes_session(self):
		from jarvis.chat.agent_scheduler import STALE_RUN_AFTER_SECONDS, reap_stale_agent_runs

		sk = "agent:agent-close-auditor:dash-reap"
		run = self._mk_run(sk)
		self._mint_session(sk)
		# Backdate the run well beyond the stale threshold so the reaper catches it.
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-(STALE_RUN_AFTER_SECONDS + 3600))
		frappe.db.set_value(RUN, run, "started_at", old, update_modified=False)
		frappe.db.commit()

		reaped = reap_stale_agent_runs()
		self.assertGreaterEqual(reaped, 1)
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "failed")
		self.assertTrue(frappe.db.get_value(RUN, run, "finished_at"))
		self.assertIn("reaped", (frappe.db.get_value(RUN, run, "error") or "").lower())
		self.assertFalse(
			frappe.db.exists(SESSION, {"session_key": sk}),
			"the reaper must tear down the orphaned session bearer (A8)",
		)

	def test_reaper_leaves_a_fresh_running_run_alone(self):
		from jarvis.chat.agent_scheduler import reap_stale_agent_runs

		sk = "agent:agent-close-auditor:dash-fresh"
		run = self._mk_run(sk)  # started_at = now
		reap_stale_agent_runs()
		self.assertEqual(frappe.db.get_value(RUN, run, "status"), "running")
