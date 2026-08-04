"""Scheduled macro runs.

An hourly cron (``jarvis.hooks.scheduler_events``) calls :func:`run_due_macros`,
which fires every enabled macro whose ``next_run_at`` has passed — running it as
the macro's owner (so the result conversation is theirs) and advancing
``next_run_at`` for the next occurrence. Modeled on
``jarvis.chat.stale_scan.scan_and_mark_errored``, hardened to match
``jarvis.chat.agent_scheduler``:

* **#469** — a fail-closed identity guard: an unattended turn never binds to
  ``Administrator``, ``Guest`` or a disabled user.
* **#471** — every outcome is DURABLE and honest. A slot that did not produce a
  run writes a ``failed`` ``Jarvis Macro Run`` the owner can see (and, when the
  owner can act on it, a Notification Log), instead of an Error Log only a System
  Manager can read; ``last_run_at`` is stamped only when the slot was actually
  consumed; and a dispatch that RAISED does not advance the schedule, so the
  missed slot retries on the next tick rather than looking like a success.
"""

import datetime

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"

_DEFAULT_SECONDS = 9 * 3600  # 09:00 when no schedule_time set

# #471: the owner cannot act on any of these — Administrator and disabled users
# are refused by notify_owner anyway, and a role-less owner can no longer reach
# the SPA — so the failed run row is the whole record. Notifying would relog the
# same dead end every cadence forever.
_BARRED_OWNER = (
	"Scheduled run skipped: this macro's owner may not run unattended work "
	"(Administrator, a disabled account, or no Jarvis access)."
)
_DISPATCH_FAILED = "Scheduled run could not be started. It will retry on the next hourly tick."


def run_due_macros() -> None:
	"""Run every enabled macro whose next_run_at is due. Runs as Administrator
	(the scheduler user); each macro executes as its own owner."""
	now = now_datetime()
	due = frappe.get_all(
		MACRO,
		filters={"schedule_enabled": 1, "next_run_at": ["<=", now]},
		fields=["name", "macro_name", "owner", "enabled", "schedule_frequency", "schedule_time"],
	)
	if not due:
		return

	from jarvis.chat import macros
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	original_user = frappe.session.user
	for m in due:
		# #471: a macro its owner switched OFF is not a failure, it is the state
		# they asked for. run_macro's own `enabled` early return was never inspected
		# here, so the slot was consumed AND last_run_at stamped — leaving the UI
		# reading "last run: today" for a macro that has not run in months. Move the
		# schedule on so it does not busy re-fire, write no failed row, and leave
		# last_run_at alone: nothing ran.
		if not cint(m.enabled):
			_consume_slot(m, now, stamp_last_run=False)
			continue

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
		# Consume the slot so it does not busy re-fire, but skip the run.
		if not is_valid_unattended_owner(m.owner) or not has_jarvis_access(m.owner):
			_record_failed(m, _BARRED_OWNER)
			_consume_slot(m, now)
			continue
		try:
			frappe.set_user(m.owner)
			out = macros.run_macro(m.name, trigger="scheduled") or {}
		except Exception:
			frappe.set_user(original_user)
			frappe.log_error(
				title=f"jarvis scheduled macro failed: {m.name}",
				message=frappe.get_traceback(),
			)
			# #471: record the failure where the OWNER can see it, and do NOT consume
			# the slot — next_run_at stays in the past so the next hourly tick retries
			# it. Previously the schedule advanced regardless, so a run that never
			# happened was indistinguishable from one that did.
			_record_failed(m, _DISPATCH_FAILED)
			_notify_owner(m, _DISPATCH_FAILED)
			continue
		finally:
			if frappe.session.user != original_user:
				frappe.set_user(original_user)
		_settle(m, now, out)


def _settle(m, now, out: dict) -> None:
	"""Apply the outcome ``run_macro`` reported. A refusal it returns rather than
	raises (``{"ok": False, "reason": ...}``) used to be dropped on the floor here,
	so the slot was consumed and stamped as if the macro had run."""
	if out.get("ok"):
		_consume_slot(m, now)
		return
	reason = str(out.get("reason") or "").strip()
	if reason == "macro disabled":
		# Raced: disabled between the due query and the dispatch. Same handling as
		# the pre-dispatch branch — no failure, nothing ran.
		_consume_slot(m, now, stamp_last_run=False)
		return
	sentence = reason or "Scheduled run was refused."
	_record_failed(m, sentence)
	_notify_owner(m, sentence)
	_consume_slot(m, now)


def _consume_slot(m, now, *, stamp_last_run: bool = True) -> None:
	"""Consume this macro's schedule slot: compute the next occurrence with a raw
	set_value (no re-validate, which would otherwise recompute ``next_run_at``
	itself). ``compute_next_run`` is taken from *now*, so even a long outage yields
	ONE next slot rather than a backfill storm.

	``stamp_last_run=False`` moves the schedule on WITHOUT claiming a run happened:
	``last_run_at`` is what the SPA renders as "last run", so stamping it for a slot
	nothing executed is the #471 "the UI reports success" limb."""
	values = {"next_run_at": compute_next_run(m.schedule_frequency, m.schedule_time, from_dt=now)}
	if stamp_last_run:
		values["last_run_at"] = now
	frappe.db.set_value(MACRO, m.name, values, update_modified=False)
	frappe.db.commit()


def _record_failed(m, reason: str) -> None:
	"""Write a ``failed`` Jarvis Macro Run owned by the macro's owner, so a slot
	that did not run is VISIBLE — in the run-history list and the dashboard tiles —
	rather than only in an Error Log the owner cannot read (#471). Mirrors
	``agent_scheduler._record_failed``.

	Never raises: a bookkeeping failure must not abort the sweep for every other
	due macro."""
	try:
		run = frappe.get_doc(
			{
				"doctype": RUN,
				"macro": m.name,
				"status": "failed",
				"trigger": "scheduled",
				"current_step": 0,
				"total_steps": 0,
				"started_at": frappe.utils.now(),
				"finished_at": frappe.utils.now(),
				"error": (reason or "")[:500],
			}
		)
		run.flags.ignore_permissions = True
		run.insert()
		if m.owner and m.owner != frappe.session.user:
			frappe.db.set_value(RUN, run.name, "owner", m.owner, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="jarvis macro scheduler: could not record a failed run",
			message=frappe.get_traceback(),
		)


def _notify_owner(m, reason: str) -> None:
	"""Tell the owner their scheduled macro did not run. A 03:00 failure reaches
	nobody through the realtime channel (no socket is open), which is why #471 calls
	the current behaviour silent."""
	from jarvis.chat import macros

	macros.notify_owner(
		m.owner,
		subject=f"Scheduled macro did not run: {m.get('macro_name') or m.name}",
		body=reason,
	)


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
