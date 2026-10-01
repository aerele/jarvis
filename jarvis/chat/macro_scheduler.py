"""Scheduled macro runs.

A five-minute cron (``jarvis.hooks.scheduler_events``) calls :func:`run_due_macros`,
which fires every enabled macro whose ``next_run_at`` has passed — running it as
the macro's owner (so the result conversation is theirs) and advancing
``next_run_at`` for the next occurrence. The sweep was hourly, so a macro set for
10:15 started at 11:00 and read as "never triggered" (admin-v2#675). A slot that
FAILED is still retried only about hourly (``_retry_later``). Modeled on
``jarvis.chat.stale_scan.scan_and_mark_errored``, hardened to match
``jarvis.chat.agent_scheduler``:

* **#469** — a fail-closed identity guard: an unattended turn never binds to
  ``Administrator``, ``Guest`` or a disabled user.
* **#471** — every outcome is DURABLE and honest. A slot that did not produce a
  run writes a ``failed`` ``Jarvis Macro Run`` the owner can see (and, when the
  owner can act on it, a Notification Log), instead of an Error Log only a System
  Manager can read; ``last_run_at`` is stamped only when the slot was actually
  consumed; and a dispatch that RAISED does not consume the slot: it is retried
  about an hour later rather than looking like a success.
* **#472** — the sweep is per-macro fault-isolated, and ``compute_next_run`` is
  TOTAL: no ``schedule_time`` value, however malformed, can make it raise. Both
  limbs matter because the schedule arithmetic used to run OUTSIDE the per-macro
  try/except, so one bad row aborted the sweep for every other macro on the bench.
"""

import calendar
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
_DISPATCH_FAILED = "Scheduled run could not be started. It will retry in about an hour."

# admin-v2#675: how long a slot that should be retried waits before the sweep tries it
# again. The sweep runs every five minutes; retrying on every sweep would record a
# failed run and notify the owner twelve times an hour instead of once.
#
# The retry time is written to ``next_run_at`` ON THE ROW, never held in the cache.
# ``frappe.clear_cache()`` deletes every site cache key outside
# ``persistent_cache_keys``, and ``pump.watchdog`` triggers one every five minutes
# (see the note on ``persistent_cache_keys`` in hooks.py), so a cache-held retry time
# would not survive a single sweep.
_RETRY_AFTER_S = 60 * 60

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
		"retry in about an hour."
	),
	"workspace_resetting": (
		"Scheduled run deferred: the workspace is being rebuilt. It will retry in about an hour."
	),
	"maintenance": (
		"Scheduled run deferred: this workspace is being upgraded. It will retry in about an hour."
	),
	BLOCK_STEP_BUDGET: (
		"Scheduled run skipped: this month's budget for scheduled macro runs is used up. "
		"Runs resume next month, or ask an admin to raise the budget in Jarvis Settings."
	),
	BLOCK_DISPATCH_FAILED: (
		"Scheduled run could not be started: the agent turn was not dispatched. It will "
		"retry in about an hour."
	),
}

# #468: a refusal that CLEARS ON ITS OWN within minutes does not consume the slot: it
# is retried about an hour later (the sibling's O4 shape). Everything else is an
# entitlement decision that cannot clear inside the hour — consume the slot, or the
# cadence relogs the same dead end 24 times a day.
_TRANSIENT_BLOCKS = {
	"release_update_required",
	"workspace_resetting",
	"maintenance",
	BLOCK_DISPATCH_FAILED,
}


def run_due_macros() -> None:
	"""Run every enabled macro whose next_run_at is due. Runs as Administrator
	(the scheduler user); each macro executes as its own owner."""
	now = now_datetime()
	due = frappe.get_all(
		MACRO,
		filters={"schedule_enabled": 1, "next_run_at": ["<=", now]},
		fields=[
			"name",
			"macro_name",
			"owner",
			"enabled",
			"schedule_frequency",
			"schedule_time",
			"schedule_weekday",
			"schedule_day_of_month",
			"next_run_at",
		],
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
		# down. A failure never CONSUMES the slot (#471): if this one blew up before the
		# slot was claimed, it is retried about an hour later, with a failed run the
		# owner can see. If it blew up after the claim, the macro was already
		# dispatched and the schedule has moved on, so there is nothing to retry.
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
			if _retry_later(m, now, expected=m.next_run_at):
				_record_failed(m, _DISPATCH_FAILED)
				_notify_owner(m, _DISPATCH_FAILED)


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
	claimed = _claim_slot(m, now)
	if not claimed:
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
		# #471: do NOT consume the slot, and record the failure where the OWNER can
		# see it. Previously the schedule advanced regardless, so a run that never
		# happened was indistinguishable from one that did. The retry is scheduled
		# FIRST: the record and the notification below promise it, and if either of
		# them failed first the claim would stand and nothing would retry.
		_retry_later(m, now, expected=claimed)
		_record_failed(m, _DISPATCH_FAILED)
		_notify_owner(m, _DISPATCH_FAILED)
		return
	finally:
		if frappe.session.user != original_user:
			frappe.set_user(original_user)
	_settle(m, now, out, claimed)


def _settle(m, now, out: dict, claimed) -> None:
	"""Apply the outcome ``run_macro`` reported. A refusal it returns rather than
	raises (``{"ok": False, "reason": ...}``) used to be dropped on the floor here,
	so the slot was consumed and stamped as if the macro had run.

	The claim already moved ``next_run_at`` on. What is left to decide is whether the
	slot counts as run (stamp ``last_run_at``) or must be retried (``_retry_later``)."""
	if out.get("ok"):
		_stamp_last_run(m, now)
		return
	reason = str(out.get("reason") or "").strip()
	if reason == "macro disabled":
		# Raced: disabled between the due query and the dispatch. Same handling as
		# the pre-dispatch branch — no failure, nothing ran, nothing stamped.
		return
	sentence = _BLOCK_SENTENCE.get(reason) or f"Scheduled run was refused: {reason or 'unknown'}."
	if reason == BLOCK_DISPATCH_FAILED:
		# run_macro already terminalized the run row it had created — the one that
		# carries the conversation link — so recording a second one here would show the
		# customer two failures for one missed slot. Schedule the retry, and notify.
		_retry_later(m, now, expected=claimed)
		_notify_owner(m, sentence)
		return
	if reason in _TRANSIENT_BLOCKS:
		_retry_later(m, now, expected=claimed)
		_record_failed(m, sentence)
		_notify_owner(m, sentence)
		return
	_record_failed(m, sentence)
	_notify_owner(m, sentence)
	_stamp_last_run(m, now)


def _claim_slot(m, now):
	"""Take this slot BEFORE dispatching it, by moving ``next_run_at`` on to the next
	occurrence. Returns that occurrence, or None when the slot is no longer ours to run.

	The sweep used to dispatch first and move the schedule on afterwards. A worker
	killed in between left the slot due with a run already executing, so the next
	sweep ran the macro again: duplicate ERP writes when it is armed. Claiming first
	trades that for a missed run if the worker dies before the dispatch, which is the
	safe side, and is what ``agent_scheduler._claim_slot`` already does (#672).

	Compare-and-set under a ROW LOCK: the sweep's ``get_all`` does not lock, so the
	row in hand can be stale. Re-reading ``next_run_at`` for update and confirming it
	is still due is what makes exactly one dispatcher run a slot, and stops a sweep
	overwriting a schedule the owner saved a moment ago."""
	frappe.db.commit()  # REPEATABLE-READ discipline: the FOR UPDATE read goes first
	current = frappe.db.get_value(MACRO, m.name, "next_run_at", for_update=True)
	if not current or get_datetime(current) > now:
		frappe.db.commit()  # release the row lock
		return None
	claimed = _next_occurrence(m, now)
	frappe.db.set_value(MACRO, m.name, "next_run_at", claimed, update_modified=False)
	frappe.db.commit()  # releases the row lock
	return claimed


def _retry_later(m, now, *, expected) -> bool:
	"""Schedule ONE retry of this slot about an hour from now. True if it did.

	Compare-and-set on ``next_run_at``: the write lands only while the row still
	holds ``expected`` (the value this sweep last knew it to hold), so a schedule the
	owner saved in the meantime is never overwritten, and a slot that was already
	claimed and dispatched is never re-armed.

	Never later than the next natural occurrence, so a retry cannot push a run past
	its own next slot.

	Never raises: it runs on failure paths, and failing to schedule a retry must not
	abort the sweep for the macros behind this one. What that costs is the retry, so
	it is logged."""
	try:
		retry_at = min(add_to_date(now, seconds=_RETRY_AFTER_S), _next_occurrence(m, now))
		frappe.db.set_value(
			MACRO,
			{"name": m.name, "next_run_at": expected},
			"next_run_at",
			retry_at,
			update_modified=False,
		)
		frappe.db.commit()
		return get_datetime(frappe.db.get_value(MACRO, m.name, "next_run_at")) == retry_at
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title=f"jarvis macro scheduler: could not schedule a retry: {m.name}",
			message=frappe.get_traceback(),
		)
		return False


def _stamp_last_run(m, now) -> None:
	"""Record that this slot was consumed. ``next_run_at`` is left alone: the claim
	already moved it, and writing it again here would overwrite a schedule the owner
	saved while the macro was dispatching."""
	frappe.db.set_value(MACRO, m.name, "last_run_at", now, update_modified=False)
	frappe.db.commit()


def _next_occurrence(m, now) -> datetime.datetime:
	"""The next slot strictly after ``now``. Taken from *now*, not from the slot being
	handled, so even a long outage yields ONE next slot rather than a backfill storm."""
	return compute_next_run(
		m.schedule_frequency,
		m.schedule_time,
		from_dt=now,
		weekday=m.schedule_weekday,
		day_of_month=m.schedule_day_of_month,
	)


def _consume_slot(m, now, *, stamp_last_run: bool = True) -> None:
	"""Consume a slot that is NOT being dispatched (a macro switched off, a barred
	owner): move the schedule on with a raw set_value (no re-validate, which would
	otherwise recompute ``next_run_at`` itself). A slot that IS dispatched goes
	through ``_claim_slot`` instead.

	``stamp_last_run=False`` moves the schedule on WITHOUT claiming a run happened:
	``last_run_at`` is what the SPA renders as "last run", so stamping it for a slot
	nothing executed is the #471 "the UI reports success" limb."""
	values = {"next_run_at": _next_occurrence(m, now)}
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


_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def compute_next_run(
	frequency: str,
	schedule_time,
	from_dt=None,
	weekday: str | int | None = None,
	day_of_month: int | None = None,
) -> datetime.datetime:
	"""Next fire time strictly after ``from_dt`` (default now) at ``schedule_time``
	on the given ``frequency`` (daily/weekly/monthly).

	``weekday`` (weekly) and ``day_of_month`` (monthly) are optional anchors
	(#653): when given, the next occurrence lands ON that weekday / day of month
	rather than simply +7 days / +1 month from ``from_dt``. Omitted or invalid
	entirely (either argument), the function falls back to the original plain
	advance - every existing caller that does not pass them keeps its old
	behaviour verbatim.

	TOTAL by construction (#472, extended by #653 to the two new anchors):
	``_time_to_seconds`` can only return a real time of day, and
	``_normalize_weekday``/``_normalize_day_of_month`` can only return a valid
	index or ``None`` - never raise - so a garbage anchor is treated the same as
	an absent one. That matters because the function is called from the cron's
	bookkeeping, where a raise skipped every macro/install after the offending
	one."""
	base = get_datetime(from_dt) if from_dt else now_datetime()
	secs = _time_to_seconds(schedule_time)
	hour, minute = secs // 3600, (secs % 3600) // 60
	freq = str(frequency or "daily").strip().lower()
	weekday_idx = _normalize_weekday(weekday) if freq == "weekly" else None
	day = _normalize_day_of_month(day_of_month) if freq == "monthly" else None

	if weekday_idx is not None:
		cand = _weekly_candidate(base, hour, minute, weekday_idx)
		while cand <= base:
			cand = _weekly_candidate(cand + datetime.timedelta(days=1), hour, minute, weekday_idx)
		return cand

	if day is not None:
		cand = _monthly_candidate(base, hour, minute, day)
		while cand <= base:
			anchor = add_to_date(cand.replace(day=1), months=1)
			cand = _monthly_candidate(anchor, hour, minute, day)
		return cand

	cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
	while cand <= base:
		cand = _advance(cand, freq)
	return cand


def _advance(dt: datetime.datetime, frequency: str) -> datetime.datetime:
	if frequency == "weekly":
		return add_to_date(dt, days=7)
	if frequency == "monthly":
		return add_to_date(dt, months=1)
	return add_to_date(dt, days=1)


def _weekly_candidate(
	base_date: datetime.datetime, hour: int, minute: int, weekday_idx: int
) -> datetime.datetime:
	"""The occurrence of ``weekday_idx`` (0=Monday..6=Sunday, ``datetime.weekday()``
	convention) at ``hour``:``minute`` in the week containing ``base_date`` - which
	may fall BEFORE ``base_date`` itself; the caller's ``while cand <= base`` loop
	is what pushes it into the following week when that happens."""
	delta = (weekday_idx - base_date.weekday()) % 7
	day = base_date + datetime.timedelta(days=delta)
	return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _monthly_candidate(base_date: datetime.datetime, hour: int, minute: int, day: int) -> datetime.datetime:
	"""The occurrence of day-of-month ``day`` (1-31) at ``hour``:``minute`` in
	``base_date``'s own month, clamped to that month's last day (#653: day 31 in
	February lands on the 28th/29th)."""
	last_day = calendar.monthrange(base_date.year, base_date.month)[1]
	return base_date.replace(day=min(day, last_day), hour=hour, minute=minute, second=0, microsecond=0)


def _normalize_weekday(value) -> int | None:
	"""Weekday index (0=Monday..6=Sunday) from a weekday name (case-insensitive,
	matching the Select field's options) or an ISO weekday int (1=Monday..7=Sunday).
	``None`` for anything else - a missing, blank, or garbage value included - so
	the caller falls back to the plain +7-days advance (#653, extending #472's
	TOTAL guarantee to this anchor)."""
	if value is None or value == "":
		return None
	if isinstance(value, str):
		name = value.strip().capitalize()
		if name in _WEEKDAY_NAMES:
			return _WEEKDAY_NAMES.index(name)
		try:
			value = int(value)
		except ValueError:
			return None
	try:
		n = int(value)
	except (TypeError, ValueError):
		return None
	return n - 1 if 1 <= n <= 7 else None


def _normalize_day_of_month(value) -> int | None:
	"""1-31, or ``None`` for anything else - including the ``0`` an unset Int
	field reads as (Frappe coerces a blank Int to 0, not ``None``) and any
	garbage that reached the row before validation existed (#653, same TOTAL
	shape as ``_normalize_weekday``)."""
	try:
		n = int(value)
	except (TypeError, ValueError):
		return None
	return n if 1 <= n <= 31 else None


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


def validate_schedule_day_of_month_or_throw(value) -> None:
	"""Refuse a ``schedule_day_of_month`` that is not 1-31, with the field error the
	SPA renders. Companion to ``validate_schedule_time_or_throw`` - same ONE-definition
	reasoning, called by both ``JarvisMacro`` and ``JarvisAgentInstallation`` (#653).

	``0`` is exempt alongside ``None``/``""``: Frappe coerces a blank Int field to
	``0`` rather than ``None``, so an unset value reaches here as ``0`` on every
	normal save path - that is "not set", not "the 0th day"."""
	if value in (None, "", 0):
		return
	if _normalize_day_of_month(value) is None:
		frappe.throw(
			frappe._("Day of month must be between 1 and 31."),
			title=frappe._("Invalid schedule day"),
		)


def _time_to_seconds(t) -> int:
	"""TOLERANT reader, for the cron. A value the strict parser rejects has already
	been persisted, so refusing it here would only take the schedule arithmetic down
	with it; fall back to the same 09:00 an unset time gets and let the sweep finish."""
	secs = parse_schedule_seconds(t)
	return _DEFAULT_SECONDS if secs is None else secs
