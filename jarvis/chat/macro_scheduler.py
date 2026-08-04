"""Scheduled macro runs.

An hourly cron (``jarvis.hooks.scheduler_events``) calls :func:`run_due_macros`,
which fires every enabled macro whose ``next_run_at`` has passed — running it as
the macro's owner (so the result conversation is theirs) and advancing
``next_run_at`` for the next occurrence. Modeled on
``jarvis.chat.stale_scan.scan_and_mark_errored``.
"""

import datetime

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

MACRO = "Jarvis Macro"

_DEFAULT_SECONDS = 9 * 3600  # 09:00 when no schedule_time set


def run_due_macros() -> None:
	"""Run every enabled macro whose next_run_at is due. Runs as Administrator
	(the scheduler user); each macro executes as its own owner."""
	now = now_datetime()
	due = frappe.get_all(
		MACRO,
		filters={"schedule_enabled": 1, "next_run_at": ["<=", now]},
		fields=["name", "owner", "schedule_frequency", "schedule_time"],
	)
	if not due:
		return

	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	original_user = frappe.session.user
	for m in due:
		# MAC-1 (security review PART 3, TASK 23): never run a macro whose owner
		# has lost Jarvis access (demoted System User, or a Website/portal owner) —
		# the scheduled turn would otherwise execute jarvis__* tools as an identity
		# categorically barred from Jarvis.
		#
		# #469: has_jarvis_access alone is NOT that guarantee. It returns True for
		# Administrator before any other check, and NOTHING in permissions.py (nor
		# frappe.get_roles) reads User.enabled — so on its own it admitted an
		# unattended, fully perm-bypassing Administrator turn, and kept an
		# offboarded employee's macros firing with live ERP access forever. Add the
		# fail-closed identity guard the sibling agent scheduler has always applied
		# (agent_scheduler._valid_owner). Both checks are required: this one refuses
		# Administrator/Guest/disabled, has_jarvis_access refuses portal users and
		# role-less System Users.
		#
		# Advance the schedule so it does not busy re-fire, but skip the run.
		if not is_valid_unattended_owner(m.owner) or not has_jarvis_access(m.owner):
			_consume_slot(m, now)
			continue
		try:
			frappe.set_user(m.owner)
			from jarvis.chat import macros

			macros.run_macro(m.name, trigger="scheduled")
		except Exception:
			frappe.log_error(
				title=f"jarvis scheduled macro failed: {m.name}",
				message=frappe.get_traceback(),
			)
		finally:
			frappe.set_user(original_user)
		_consume_slot(m, now)


def _consume_slot(m, now) -> None:
	"""Consume this macro's schedule slot: stamp ``last_run_at`` and compute the
	next occurrence with a raw set_value (no re-validate, which would otherwise
	recompute ``next_run_at`` itself). ``compute_next_run`` is taken from *now*, so
	even a long outage yields ONE next slot rather than a backfill storm."""
	frappe.db.set_value(
		MACRO,
		m.name,
		{
			"last_run_at": now,
			"next_run_at": compute_next_run(m.schedule_frequency, m.schedule_time, from_dt=now),
		},
		update_modified=False,
	)
	frappe.db.commit()


def compute_next_run(frequency: str, schedule_time, from_dt=None) -> datetime.datetime:
	"""Next fire time strictly after ``from_dt`` (default now) at ``schedule_time``
	on the given ``frequency`` (daily/weekly/monthly)."""
	base = get_datetime(from_dt) if from_dt else now_datetime()
	secs = _time_to_seconds(schedule_time)
	cand = base.replace(hour=secs // 3600, minute=(secs % 3600) // 60, second=0, microsecond=0)
	while cand <= base:
		cand = _advance(cand, frequency)
	return cand


def _advance(dt: datetime.datetime, frequency: str) -> datetime.datetime:
	if frequency == "weekly":
		return add_to_date(dt, days=7)
	if frequency == "monthly":
		return add_to_date(dt, months=1)
	return add_to_date(dt, days=1)


def _time_to_seconds(t) -> int:
	if not t:
		return _DEFAULT_SECONDS
	if isinstance(t, datetime.timedelta):
		return int(t.total_seconds())
	parts = str(t).split(":")
	try:
		h = int(parts[0])
		m = int(parts[1]) if len(parts) > 1 else 0
		s = int(parts[2]) if len(parts) > 2 else 0
		return h * 3600 + m * 60 + s
	except (ValueError, IndexError):
		return _DEFAULT_SECONDS
