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
* **#472** — the sweep is per-macro fault-isolated, and ``compute_next_run`` is
  TOTAL: no ``schedule_time`` value, however malformed, can make it raise. Both
  limbs matter because the schedule arithmetic used to run OUTSIDE the per-macro
  try/except, so one bad row aborted the sweep for every other macro on the bench.
"""

import datetime

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from jarvis.chat.macros import BLOCK_DISPATCH_FAILED, BLOCK_STEP_BUDGET

MACRO = "Jarvis Macro"
RUN = "Jarvis Macro Run"

_DEFAULT_SECONDS = 9 * 3600  # 09:00 when no schedule_time set
_MAX_SECONDS = 24 * 3600 - 1  # 23:59:59, the last representable time of day

# #471: the owner cannot act on any of these — Administrator and disabled users
# are refused by notify_owner anyway, and a role-less owner can no longer reach
# the SPA — so the failed run row is the whole record. Notifying would relog the
# same dead end every cadence forever.
_BARRED_OWNER = (
	"Scheduled run skipped: this macro's owner may not run unattended work "
	"(Administrator, a disabled account, or no Jarvis access)."
)
_DISPATCH_FAILED = "Scheduled run could not be started. It will retry on the next hourly tick."

# #468: what the owner is told when the entitlement / budget gate refused the run.
# Keyed on the machine codes `policy.validate_can_send` and `macros.entitlement_block`
# report, so the macro and the chat composer never disagree about why a send is barred.
_BLOCK_SENTENCE = {
	"usage_limit": (
		"Scheduled run skipped: this user's monthly usage limit is reached. Runs resume "
		"when the limit resets, or when an admin raises it."
	),
	"subscription_suspended": (
		"Scheduled run skipped: the subscription does not currently include chat. Runs "
		"resume once billing is settled."
	),
	"llm_not_configured": (
		"Scheduled run skipped: no AI model connection is configured. Connect a model and "
		"runs resume on the next scheduled slot."
	),
	"release_update_required": (
		"Scheduled run deferred: a Jarvis update is rolling out on this workspace. It will "
		"retry on the next hourly tick."
	),
	"workspace_resetting": (
		"Scheduled run deferred: the workspace is being rebuilt. It will retry on the next hourly tick."
	),
	BLOCK_STEP_BUDGET: (
		"Scheduled run skipped: this month's budget for scheduled macro runs is used up. "
		"Runs resume next month, or ask an admin to raise the budget in Jarvis Settings."
	),
	BLOCK_DISPATCH_FAILED: (
		"Scheduled run could not be started: the agent turn was not dispatched. It will "
		"retry on the next hourly tick."
	),
}

# #468: a refusal that CLEARS ON ITS OWN within minutes leaves the slot due, so the
# next hourly tick retries it (the sibling's O4 shape). Everything else is an
# entitlement decision that cannot clear inside the hour — consume the slot, or the
# cadence relogs the same dead end 24 times a day.
_TRANSIENT_BLOCKS = {"release_update_required", "workspace_resetting", BLOCK_DISPATCH_FAILED}


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

	original_user = frappe.session.user
	for m in due:
		# #472: fault-isolate each macro. The per-macro try below covers only the
		# DISPATCH; the schedule arithmetic, the identity guard and the bookkeeping all
		# sat outside it, so anything they raised propagated out of the whole sweep and
		# every macro after this one silently missed its slot. The concrete case was a
		# `schedule_time` the arithmetic could not use, but the guarantee wanted here is
		# structural and not specific to that value: no single row can take the sweep
		# down. The slot is left DUE on this path (#471's rule: a failure never advances
		# the schedule), so the next hourly tick retries it.
		try:
			_sweep_one(m, now, original_user)
		except Exception:
			if frappe.session.user != original_user:
				frappe.set_user(original_user)
			frappe.db.rollback()
			frappe.log_error(
				title=f"jarvis scheduled macro sweep failed: {m.name}",
				message=frappe.get_traceback(),
			)


def _sweep_one(m, now, original_user: str) -> None:
	"""Handle ONE due macro. Extracted from the loop so ``run_due_macros`` can wrap it
	whole (#472); every ``return`` here was a ``continue`` in the loop it came from."""
	from jarvis.chat import macros
	from jarvis.permissions import has_jarvis_access, is_valid_unattended_owner

	# #471: a macro its owner switched OFF is not a failure, it is the state
	# they asked for. run_macro's own `enabled` early return was never inspected
	# here, so the slot was consumed AND last_run_at stamped — leaving the UI
	# reading "last run: today" for a macro that has not run in months. Move the
	# schedule on so it does not busy re-fire, write no failed row, and leave
	# last_run_at alone: nothing ran.
	if not cint(m.enabled):
		_consume_slot(m, now, stamp_last_run=False)
		return

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
		return
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
		return
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
	sentence = _BLOCK_SENTENCE.get(reason) or f"Scheduled run was refused: {reason or 'unknown'}."
	if reason == BLOCK_DISPATCH_FAILED:
		# run_macro already terminalized the run row it had created — the one that
		# carries the conversation link — so recording a second one here would show the
		# customer two failures for one missed slot. Notify, and leave the slot due.
		_notify_owner(m, sentence)
		return
	_record_failed(m, sentence)
	_notify_owner(m, sentence)
	if reason in _TRANSIENT_BLOCKS:
		return  # leave next_run_at in the past: the next tick retries the slot
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
	on the given ``frequency`` (daily/weekly/monthly).

	TOTAL by construction (#472): ``_time_to_seconds`` can only return a real time of
	day, so ``base.replace(hour=...)`` can no longer raise ``ValueError`` on a value
	that reached the row before this validation existed. That matters because the
	function is called from the cron's bookkeeping, where a raise skipped every macro
	after the offending one."""
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


def parse_schedule_seconds(t) -> int | None:
	"""Seconds since midnight for a ``schedule_time``, or None when it is not a real
	time of day (#472).

	STRICT on purpose: this is the reader ``JarvisMacro.validate`` uses to REFUSE a
	bad value at save with a field error. It has to be strict because Frappe's own
	Time-field validation runs AFTER the controller (``Document.insert`` calls
	``run_before_save_methods`` and only then ``_validate``), so by the time the
	framework would object the controller has already fed the value to the schedule
	arithmetic.

	MariaDB's TIME column accepts up to 838:59:59, which is why an out-of-range value
	could be persisted at all: the storage layer is not the guard here.

	A trailing fractional-seconds component ("HH:MM:SS.ffffff") IS a real time of day
	and is accepted (the fraction is dropped): on Frappe 15 ``create_new`` stamps EVERY
	Time field of a new doc with ``nowtime()`` unconditionally, and this version formats
	it with microseconds ("%H:%M:%S.%f"), so a freshly inserted Jarvis Agent Installation
	/ Jarvis Macro reaches ``validate`` with a microsecond ``schedule_time`` the caller
	never set. Frappe 16 injects that default only for ``default = "now"`` fields, so the
	value is ``None`` there and this path is Frappe-15-only in practice -- which is also
	why CI (Frappe 16) never caught the install/macro-create break. The ``datetime.time``
	and ``timedelta`` branches already drop sub-second precision; this keeps the string
	branch consistent with them. The out-of-range guards below are unchanged.

	``compute_next_run`` is shared with the AGENT scheduler (``agent_scheduler._advance``,
	``agents_api.set_schedule``, ``agent_runs``), which has the same unguarded shape: the
	agent sweep calls ``_advance`` from ten sites and only one of them sits inside a try.
	Making the arithmetic total therefore closes that cron-wide abort too. The trade is
	that ``agents_api.set_schedule`` no longer 500s on a hand-crafted out-of-range value,
	it saves it and schedules 09:00; the durable fix there is this same check applied in
	the Jarvis Agent Installation controller, which is out of scope for #472.
	"""
	if t is None or t == "":
		return None
	if isinstance(t, datetime.timedelta):
		secs = int(t.total_seconds())
	elif isinstance(t, datetime.time):
		secs = t.hour * 3600 + t.minute * 60 + t.second
	else:
		parts = str(t).strip().split(":")
		if len(parts) > 3:
			return None
		# Tolerate a fractional-seconds component on the SECONDS part only (see
		# docstring): "SS.ffffff" -> "SS". The fraction must be digits, or the whole
		# value is garbage and stays rejected. A fractional part anywhere else
		# (e.g. "12.5:00") still fails the int() below.
		if len(parts) == 3 and "." in parts[2]:
			whole, _, frac = parts[2].partition(".")
			if not frac.isdigit():
				return None
			parts[2] = whole
		try:
			nums = [int(p) for p in parts]
		except ValueError:
			return None
		nums += [0] * (3 - len(nums))
		h, m, s = nums
		if not (0 <= m <= 59 and 0 <= s <= 59):
			return None
		secs = h * 3600 + m * 60 + s
	return secs if 0 <= secs <= _MAX_SECONDS else None


def validate_schedule_time_or_throw(value) -> None:
	"""Refuse a ``schedule_time`` that is not a time of day, with the field error the
	SPA renders.

	ONE definition of the rule, called by ``JarvisMacro`` (#472) and
	``JarvisAgentInstallation`` (#648). Both controllers previously carried their own
	copy, so a change to the range, the message or the empty-value exemption had to be
	made twice, and missing one would let the two DocTypes accept different values,
	which is the drift #472 and #648 were both filed to close.

	An empty value is exempt on purpose: ``schedule_time`` is optional and the
	schedulers already treat an unset time as the 09:00 default."""
	if value in (None, ""):
		return
	if parse_schedule_seconds(value) is None:
		frappe.throw(
			frappe._("Schedule time must be a time of day between 00:00:00 and 23:59:59."),
			title=frappe._("Invalid schedule time"),
		)


def _time_to_seconds(t) -> int:
	"""TOLERANT reader, for the cron. A value the strict parser rejects has already
	been persisted, so refusing it here would only take the schedule arithmetic down
	with it; fall back to the same 09:00 an unset time gets and let the sweep finish."""
	secs = parse_schedule_seconds(t)
	return _DEFAULT_SECONDS if secs is None else secs
