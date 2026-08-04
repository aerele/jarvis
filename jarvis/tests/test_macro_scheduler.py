"""Scheduled-macro dispatch: identity, caps and failure handling.

Covers the three defects the hourly ``run_due_macros`` cron shipped with:

* **#469** — it gated only on ``has_jarvis_access``, which returns True for
  ``Administrator`` and never reads ``User.enabled``, so an unattended turn could
  bind to a fully perm-bypassing identity or to an offboarded employee.
* **#468** — macro steps never passed the entitlement gate (``validate_can_send``)
  and had no run budget of any kind, so they drained the owner's quota without
  ever being refused by it.
* **#471** — a failed run advanced its schedule anyway, recorded nothing the owner
  could see, and could strand a run in ``running`` forever (no reaper).

``jarvis.chat.macros.run_macro`` is patched in every dispatch test: this suite is
about the SCHEDULER's decisions, and a real dispatch would need a live gateway.
``run_due_macros`` sweeps every due macro on the site, so assertions are always
scoped to this suite's own rows by name.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from jarvis.chat import macro_scheduler, macros

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"

PFX = "msched"
OWNER_OK = "msched-owner@example.com"
OWNER_OFF = "msched-disabled@example.com"


def _ensure_user(email: str, *, enabled: int = 1) -> str:
	from jarvis.permissions import JARVIS_USER_ROLE, ensure_jarvis_user_role

	ensure_jarvis_user_role()
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": PFX,
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	if JARVIS_USER_ROLE not in set(frappe.get_roles(email)):
		frappe.get_doc("User", email).add_roles(JARVIS_USER_ROLE)
	# enabled is set LAST and with a raw write: User.validate refuses some edits on
	# a disabled row, and add_roles on a disabled user is a no-op.
	frappe.db.set_value("User", email, {"enabled": enabled, "user_type": "System User"})
	return email


def _mk_macro(owner: str, tag: str, *, due: bool = True, enabled: int = 1, steps: int = 1):
	doc = frappe.get_doc(
		{
			"doctype": MACRO,
			"macro_name": f"{PFX}-{tag}",
			"enabled": enabled,
			"schedule_enabled": 1,
			"schedule_frequency": "daily",
			"steps": [{"prompt": f"step {i}"} for i in range(1, steps + 1)],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.set_value(MACRO, doc.name, "owner", owner, update_modified=False)
	frappe.db.set_value(
		MACRO,
		doc.name,
		"next_run_at",
		add_to_date(now_datetime(), hours=-1) if due else add_to_date(now_datetime(), days=1),
		update_modified=False,
	)
	frappe.db.commit()
	return doc


def _purge() -> None:
	"""Drop this suite's macros + their runs. Several paths under test COMMIT, so
	the per-test rollback does not clean up after them, and leaked macros count
	against ``MAX_MACROS_PER_OWNER`` (25) until every later insert throws."""
	for n in frappe.get_all(MACRO, filters={"macro_name": ["like", f"{PFX}-%"]}, pluck="name"):
		for run in frappe.get_all(RUN, filters={"macro": n}, pluck="name"):
			frappe.delete_doc(RUN, run, force=True, ignore_permissions=True)
		frappe.delete_doc(MACRO, n, force=True, ignore_permissions=True)
	for n in frappe.get_all(
		"Notification Log", filters={"subject": ["like", f"%{PFX}-%"]}, pluck="name"
	):
		frappe.delete_doc("Notification Log", n, force=True, ignore_permissions=True)
	frappe.db.commit()


def _runs_for(macro_name: str) -> list:
	return frappe.get_all(
		RUN,
		filters={"macro": macro_name},
		fields=["name", "status", "error", "owner", "trigger"],
		order_by="creation asc",
	)


class MacroSchedulerBase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure_user(OWNER_OK, enabled=1)
		_ensure_user(OWNER_OFF, enabled=0)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		_purge()

	def tearDown(self):
		frappe.db.rollback()
		_purge()
		super().tearDown()

	def _run_due(self):
		"""Run the cron with dispatch stubbed; returns the macro names it dispatched."""
		with patch("jarvis.chat.macros.run_macro", return_value={"ok": True}) as mock_run:
			macro_scheduler.run_due_macros()
		return [c.args[0] for c in mock_run.call_args_list]


# --------------------------------------------------------------------------- #
# #469 — the unattended-identity guard
# --------------------------------------------------------------------------- #
class TestScheduledMacroIdentity(MacroSchedulerBase):
	def test_administrator_owned_macro_is_refused(self):
		m = _mk_macro("Administrator", "admin-owned")
		self.assertNotIn(m.name, self._run_due(), "scheduler bound an unattended turn to Administrator")

	def test_disabled_owner_macro_is_refused(self):
		m = _mk_macro(OWNER_OFF, "disabled-owner")
		# The premise of the defect: the OLD gate still says this identity is fine.
		from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

		self.assertTrue(has_jarvis_access(OWNER_OFF), "premise changed: has_jarvis_access now filters")
		self.assertFalse(is_valid_unattended_owner(OWNER_OFF))
		self.assertNotIn(m.name, self._run_due(), "offboarding did not revoke scheduled ERP access")

	def test_enabled_jarvis_owner_still_runs(self):
		m = _mk_macro(OWNER_OK, "good-owner")
		self.assertIn(m.name, self._run_due(), "the guard over-reached and refused a legitimate owner")

	def test_refused_macro_does_not_busy_refire(self):
		m = _mk_macro("Administrator", "admin-advance")
		self._run_due()
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime(), "a refused macro stayed due and would re-fire hourly")

	def test_refusal_is_recorded_as_a_failed_run(self):
		m = _mk_macro(OWNER_OFF, "disabled-recorded")
		self._run_due()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"])
		self.assertEqual(runs[0].owner, OWNER_OFF, "the failed row is not visible to the owner")
		self.assertIn("unattended", runs[0].error)


# --------------------------------------------------------------------------- #
# #471 — failures are durable, honest, and terminalized
# --------------------------------------------------------------------------- #
class TestScheduledMacroFailures(MacroSchedulerBase):
	def test_dispatch_failure_does_not_advance_the_schedule(self):
		m = _mk_macro(OWNER_OK, "raises")
		before = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		after = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertEqual(after, before, "a failed run advanced its schedule and lost the slot")
		self.assertGreater(now_datetime(), after, "the missed slot is no longer due, so it cannot retry")

	def test_dispatch_failure_is_visible_as_failed_not_successful(self):
		m = _mk_macro(OWNER_OK, "raises-visible")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		runs = _runs_for(m.name)
		self.assertEqual([r.status for r in runs], ["failed"], "the failure left no owner-visible trace")
		self.assertEqual(runs[0].trigger, "scheduled")
		self.assertEqual(runs[0].owner, OWNER_OK)
		# The UI reads last_run_at as "last run"; a run that never happened must not
		# stamp it (the "actively misleading" limb of #471).
		self.assertFalse(
			frappe.db.get_value(MACRO, m.name, "last_run_at"),
			"last_run_at was stamped for a run that never executed",
		)

	def test_dispatch_failure_notifies_the_owner(self):
		m = _mk_macro(OWNER_OK, "raises-notify")
		with patch("jarvis.chat.macros.run_macro", side_effect=RuntimeError("gateway down")):
			macro_scheduler.run_due_macros()
		notes = frappe.get_all(
			"Notification Log",
			filters={"for_user": OWNER_OK, "subject": ["like", f"%{PFX}-raises-notify%"]},
			pluck="name",
		)
		self.assertTrue(notes, "the owner got no signal at all")

	def test_successful_run_stamps_last_run_at(self):
		m = _mk_macro(OWNER_OK, "success")
		self._run_due()
		self.assertTrue(frappe.db.get_value(MACRO, m.name, "last_run_at"))
		self.assertEqual(_runs_for(m.name), [], "a successful dispatch wrote a spurious failed row")

	def test_disabled_macro_consumes_the_slot_without_claiming_a_run(self):
		m = _mk_macro(OWNER_OK, "switched-off", enabled=0)
		self.assertNotIn(m.name, self._run_due(), "a disabled macro was dispatched")
		nxt = get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at"))
		self.assertGreater(nxt, now_datetime(), "a disabled macro stayed due and re-fires hourly forever")
		self.assertFalse(
			frappe.db.get_value(MACRO, m.name, "last_run_at"),
			"a disabled macro stamped last_run_at, so the UI claims it ran",
		)
		self.assertEqual(_runs_for(m.name), [], "a deliberately disabled macro is not a failure")


# --------------------------------------------------------------------------- #
# #471 — the stale-run reaper, and what it must NOT reap
# --------------------------------------------------------------------------- #
class TestStaleMacroRunReaper(MacroSchedulerBase):
	def _mk_run(self, tag: str, status: str, *, age_s: int):
		m = _mk_macro(OWNER_OK, tag, due=False)
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": m.name,
				"status": status,
				"current_step": 0,
				"total_steps": 1,
				"trigger": "scheduled",
				"started_at": frappe.utils.now(),
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		stamp = add_to_date(now_datetime(), seconds=-age_s)
		frappe.db.sql(
			"UPDATE `tabJarvis Macro Run` SET modified=%(t)s, owner=%(o)s WHERE name=%(n)s",
			{"t": stamp, "o": OWNER_OK, "n": run.name},
		)
		frappe.db.commit()
		return run.name

	def test_stranded_running_run_is_terminalized(self):
		name = self._mk_run("stranded", "running", age_s=macros.STALE_RUN_AFTER_SECONDS + 600)
		self.assertEqual(macros.reap_stale_macro_runs(), 1)
		row = frappe.db.get_value(RUN, name, ["status", "error", "finished_at"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertTrue(row.error, "a reaped run must say WHY, not carry an empty error")
		self.assertTrue(row.finished_at)

	def test_parked_waiting_capacity_run_is_never_reaped(self):
		# #470 lets a run sit in waiting_capacity for ~100 min legitimately; the
		# reaper must not confuse deliberately parked with stranded. Aged far past
		# the cutoff so only the STATUS allowlist can be saving it.
		name = self._mk_run("parked", "waiting_capacity", age_s=macros.STALE_RUN_AFTER_SECONDS * 4)
		self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "waiting_capacity")

	def test_recently_progressing_run_is_never_reaped(self):
		name = self._mk_run("progressing", "running", age_s=macros.STALE_RUN_AFTER_SECONDS // 2)
		self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "running")

	def test_run_that_advanced_after_the_scan_is_left_alone(self):
		# The candidate snapshot is inherently stale: a step can advance between the
		# scan and the transition. The re-read under the lock must catch that.
		name = self._mk_run("raced", "running", age_s=macros.STALE_RUN_AFTER_SECONDS + 600)
		with patch.object(macros, "_stale_run_candidates", return_value=[name]):
			frappe.db.sql(
				"UPDATE `tabJarvis Macro Run` SET modified=%(t)s WHERE name=%(n)s",
				{"t": now_datetime(), "n": name},
			)
			frappe.db.commit()
			self.assertEqual(macros.reap_stale_macro_runs(), 0)
		self.assertEqual(frappe.db.get_value(RUN, name, "status"), "running")
