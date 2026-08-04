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

from jarvis.chat import macro_scheduler

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
