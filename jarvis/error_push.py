"""Out-of-band push of tenant errors to the admin control plane.

Modeled on ``jarvis.chat.usage_push``: self-gating (skip un-onboarded),
best-effort, and it NEVER raises into the scheduler. Runs on the
``*/5`` cron so the admin's Errors feed and Fleet Console badge stay fresh
without ever touching the chat hot path.

Error capture lanes (what reaches admin, from where, and how it is read)::

    lane                      source                           surface    severity  cursor
    ------------------------  -------------------------------  ---------  --------  ----------------------------
    browser (SPA/PWA/Desk)    Jarvis Client Error, claimed     <client>   error     pushed 0 -> 1
    failed jarvis API call    Jarvis Client Error (after_req)  api        warning   pushed 0 -> 1
    failed tool ({ok:false})  Jarvis Client Error              tool       warning   pushed 0 -> 1
    rejected call_tool        Jarvis Client Error              call_tool  error     pushed 0 -> 1
    code-level exception      Error Log, jarvis-origin only    server     error     jarvis_error_log_watermark
    failed turn               Jarvis Chat Turn, errored        turn       error     jarvis_failed_turn_watermark

A cursor is ``"<timestamp>|<name>"`` and only ever moves forward. Each scan reads
up to ``SCAN_LAG_S`` behind now, so a row whose transaction commits a moment late
isn't passed by its cursor. That lag is a bound, not a guarantee: a transaction
that commits later than ``SCAN_LAG_S`` after its row was created is skipped. Not
captured: generic Frappe endpoints (``savedocs``, ``/api/resource``) on jarvis
doctypes, and HTTP 200 ``{ok: false}`` replies outside ``call_tool``.

Delivery is at-least-once. The buffer rows of a batch are claimed (pushed = 1) and
the claim is COMMITTED before the HTTP call, so a request folding in a new
occurrence never waits on this job's row locks. A failed call reverts the claim as
a compensating write; a crash between the commit and the call leaves the names in
``jarvis_error_push_inflight``, which the next run reverts. Admin accumulates with
``count = count + VALUES(count)``, so a crash after admin received a batch
double-counts it: telemetry-grade, the lesser evil next to losing it.

A batch that cannot go through does not block the ones after it. Admin's own
payload refusal (``InvalidErrorRollup``) drops it at once. Anything else is
reverted and retried, and a batch that admin ANSWERED with a failure 12 cycles (an
hour) in a row is dropped (cursors advance, the claim stays) with a loud log. A transport
failure, a 401/403/429 or a stopped tenant is never counted: those are outages,
and the batch waits for them to end.

Diagnosing a stalled pipeline (``bench --site <site> console``)::

    d = frappe.db.get_default
    for k in ("last_ok_at", "last_attempt_at", "consecutive_failures", "last_failure_cause", "inflight"):
        print(k, d("jarvis_error_push_" + k))
    print(d("jarvis_error_log_watermark"), d("jarvis_failed_turn_watermark"))  # cursors
    frappe.db.count("Jarvis Client Error", {"pushed": 0})  # waiting in the buffer
    # what the Error Log lane would send, without calling admin:
    from jarvis import api_errors

    api_errors.collect_error_log(d("jarvis_error_log_watermark"), upto=str(frappe.utils.now_datetime()))

Defaults written here: ``jarvis_error_push_last_ok_at``,
``jarvis_error_push_last_attempt_at``, ``jarvis_error_push_consecutive_failures``,
``jarvis_error_push_last_failure_cause`` (plus ``jarvis_error_push_batch`` and
``jarvis_error_push_inflight``). Once the last success is over an hour old and a
push fails, an Error Log row "jarvis errors: rollup push stalled <N>h (<cause>)"
is written at most every 6 hours; a dropped batch writes "jarvis errors: admin
refused rollup batch" (and always a ``jarvis.error_push`` log line). The
site_config flag ``jarvis_error_capture_off`` turns the whole pipeline off.

Only scrubbed text is ever sent; scrubbing already happened at capture time.

Unlike usage_push (a daily job), this runs every 5 min, so an admin outage
would fire the failure path 288x/day. Two guards keep that from becoming noise:
transient admin errors are not logged one by one, and any unexpected failure is
logged at most once an hour (see ``push_error_rollup``). The reporter's own
modules, and the admin HTTP seam, are also excluded from lane-2 forwarding
(``api_errors.is_jarvis_error``) so a push failure can never be forwarded back to
admin about its own outage.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime

import frappe
import requests

from jarvis import api_errors
from jarvis._redis_lock import redis_lock
from jarvis.chat.usage_push import _admin_configured
from jarvis.exceptions import (
	AdminAuthError,
	AdminRateLimitedError,
	AdminUnreachableError,
	AdminValidationError,
)

DT = "Jarvis Client Error"

#: Durable cursor (global DefaultValue) on Error Log.creation, so a lost Redis
#: cache can't make us re-forward exceptions we already sent.
_WATERMARK_KEY = "jarvis_error_log_watermark"

#: Durable cursor on Jarvis Chat Turn.done_at for the failed-turns lane.
_TURN_WATERMARK_KEY = "jarvis_failed_turn_watermark"

#: On its first run the failed-turns lane starts this far back, not at the
#: first turn the bench ever ran.
TURN_BACKFILL_HOURS = 24

#: Each scan stops this many seconds before now (see the module docstring).
SCAN_LAG_S = 120

#: Admin's refusal for a tenant with no running container: it will take the
#: same batch once the tenant runs again, so it is retried, not dropped.
_RETRY_LATER = "NoRunningTenant"

#: Admin's refusal of the payload itself (not a list, over the cap, a row with no
#: fingerprint). It would refuse the same batch again, so the batch is dropped.
_INVALID_BATCH = "InvalidErrorRollup"

#: How long one admin call may take. This job holds a lock while it is out, so it
#: must not wait the 150 s default for an admin that hangs.
PUSH_TIMEOUT_S = 30

#: Held for the whole push, so two pushes never overlap. Longer than any push can
#: run (the timeout above plus the scans); a crashed holder frees it by expiry.
_PUSH_LOCK_TTL_S = 300

#: A batch admin answered with a failure this many cycles in a row (an hour) is dropped.
MAX_BATCH_FAILURES = 12

#: Durable health of the pipeline (global DefaultValues), for an operator.
_OK_KEY = "jarvis_error_push_last_ok_at"
_ATTEMPT_KEY = "jarvis_error_push_last_attempt_at"
_FAILURES_KEY = "jarvis_error_push_consecutive_failures"
_CAUSE_KEY = "jarvis_error_push_last_failure_cause"
#: "<batch id>:<n>": the cycles in a row admin answered this batch with a failure.
_BATCH_KEY = "jarvis_error_push_batch"
#: The buffer rows claimed for a call that has not finished, as JSON.
_INFLIGHT_KEY = "jarvis_error_push_inflight"

#: A failed push is called "stalled" once the last success is this old.
STALLED_AFTER_H = 1

#: Bounds one push. Buffer rows + the folded Error Log and failed-turn scans.
UI_CAP = 200

#: Admin refuses a batch over this many errors (``_MAX_ROLLUP_ERRORS``).
ADMIN_BATCH_LIMIT = 600
assert UI_CAP + api_errors.MAX_GROUPS + api_errors.TURN_MAX_GROUPS <= ADMIN_BATCH_LIMIT

#: Delete forwarded local rows older than this (daily prune).
PUSHED_RETENTION_DAYS = 7

#: Delete undeliverable (still unpushed) rows older than this - if admin has been
#: unreachable this long the buffer must not grow forever.
UNPUSHED_RETENTION_DAYS = 30

#: Hard ceiling on the unpushed buffer between daily prunes; the oldest beyond it
#: are dropped so a failing push can't grow the table without bound.
MAX_BUFFER_ROWS = 5000

#: The prune deletes this many rows per statement, at most PRUNE_MAX_BATCHES times
#: per rule, so a flood is worked off over a few runs and a run cannot spin.
PRUNE_BATCH = 1000
PRUNE_MAX_BATCHES = 100


def _state_get(key: str) -> str:
	"""A pipeline state value ("" when never set), read straight from the table:
	``frappe.db.get_default`` reads through a cache that every write empties."""
	return frappe.db.get_value("DefaultValue", {"parent": "__default", "defkey": key}, "defvalue") or ""


def _state_set(key: str, value: str) -> None:
	"""Store a pipeline state value as a global default, only when it changed.

	Not ``frappe.db.set_default``: on Frappe 15 and 16 that flushes the WHOLE site
	cache on every change, and this job writes state every cycle (and during any
	admin outage). This updates the row and drops only the defaults cache, which is
	all a default's readers use. Inserting a first row goes through the document, not
	``add_default``, so it does not flush either."""
	current = frappe.db.get_value("DefaultValue", {"parent": "__default", "defkey": key}, "defvalue")
	if current == value:
		return
	if current is None:
		frappe.get_doc(
			{
				"doctype": "DefaultValue",
				"parent": "__default",
				"parenttype": "__default",
				"parentfield": "system_defaults",
				"defkey": key,
				"defvalue": value,
			}
		).insert(ignore_permissions=True)
	else:
		table = frappe.qb.DocType("DefaultValue")
		(
			frappe.qb.update(table)
			.set(table.defvalue, value)
			.where((table.parent == "__default") & (table.defkey == key))
			.run()
		)
	frappe.cache_manager.clear_defaults_cache("__default")


def _push_lock_name() -> str:
	"""Site-scoped: the redis key is shared by every site on the bench, and a push
	that skipped because another site held the lock would do so silently."""
	return f"error_push:{frappe.local.site}"


def push_error_rollup() -> None:
	"""``*/5`` scheduler entry. Self-gating + best-effort; NEVER raises."""
	try:
		if api_errors.capture_off() or not _admin_configured():
			return
		with redis_lock(_push_lock_name(), timeout_s=_PUSH_LOCK_TTL_S) as held:
			if held:
				_do_push()
	except (AdminAuthError, AdminUnreachableError, AdminRateLimitedError) as e:
		# Not onboarded, admin down, or throttled. The claimed rows were already
		# reverted in _do_push and the watermark did not advance, so nothing is
		# lost. No log row per cycle (at */5 that is 288 rows/day); the health
		# defaults and the "stalled" log say when it has gone on too long.
		_note_failure(e)
	except AdminValidationError as e:
		_note_failure(e)
		if not _is_retry_later(e):
			_log_push_failure_throttled()  # tenant not running: reverted, re-sent once it is
	except Exception as e:
		# A genuine bug in the push path. Log it - but at most once an hour, so a
		# persistent fault can't itself flood the local Error Log every 5 min.
		_note_failure(e)
		_log_push_failure_throttled()


def _do_push() -> None:
	_recover_inflight()
	upto = str(frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-SCAN_LAG_S))
	log_mark = _state_get(_WATERMARK_KEY) or None
	log_result = api_errors.collect_error_log(log_mark, upto=upto)
	turn_mark = _state_get(_TURN_WATERMARK_KEY)
	if not turn_mark:
		# First run: store the backfill start, or it would be a new "now - 24 h" every
		# cycle (and no cycle's batch would look like the last one's).
		turn_mark = _turn_backfill_start()
		_state_set(_TURN_WATERMARK_KEY, turn_mark)
	turn_result = api_errors.collect_failed_turns(turn_mark, upto=upto)
	_log_scan_problems(error_log=log_result, failed_turns=turn_result)

	def advance() -> None:
		_advance_watermark(_WATERMARK_KEY, log_mark, log_result["watermark"])
		_advance_watermark(_TURN_WATERMARK_KEY, turn_mark, turn_result["watermark"])

	# From the claim to its commit, ANY failure rolls the claim back: the caller's
	# failure handling commits, and would otherwise commit rows marked sent that
	# were never sent.
	try:
		ui_names, ui_rows = _collect_ui_errors()
		errors = [_wire(row) for row in ui_rows + log_result["rows"] + turn_result["rows"]]
		if not errors:
			# Nothing to send. Still advance the cursors if the scans moved them past
			# framework noise, so we don't re-scan those rows next cycle.
			advance()
			frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist watermark
			return

		# Commit the claim now. Held across the call, its row locks would stall every
		# request that reports the same error; the revert below is the compensating
		# write if the call fails, and the inflight record covers a crash.
		_state_set(_INFLIGHT_KEY, json.dumps(ui_names) if ui_names else "")
		_state_set(_ATTEMPT_KEY, str(frappe.utils.now_datetime()))
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- the claim must not wait on the call
	except Exception:
		frappe.db.rollback()
		raise

	from jarvis import admin_client

	try:
		admin_client.push_error_rollup(errors, timeout_s=PUSH_TIMEOUT_S)
	except Exception as e:
		lanes = {
			"buffer": len(ui_rows),
			"error_log": len(log_result["rows"]),
			"turns": len(turn_result["rows"]),
		}
		batch_id = _batch_id(log_mark, turn_mark, ui_names)
		reason = _drop_reason(e, batch_id)
		if reason:
			advance()
			_finish_dropped(len(errors), lanes, reason)
			return
		# Un-claim the rows so the next cycle re-sends them, and leave the cursors
		# where they were. Re-raise for push_error_rollup to classify.
		_revert_claim(ui_names)
		_state_set(_INFLIGHT_KEY, "")
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the revert
		raise

	# Success: the rows are already marked pushed (the claim WAS the mark).
	# Advance the cursors past what we forwarded.
	advance()
	_state_set(_INFLIGHT_KEY, "")
	_note_success()
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist watermark


def _recover_inflight() -> None:
	"""A previous run committed a claim and never finished (the worker died during
	the call). Nothing else holds the push lock, so give those rows back."""
	raw = _state_get(_INFLIGHT_KEY)
	if not raw:
		return
	try:
		names = json.loads(raw)
	except ValueError:
		names = []
	_revert_claim([n for n in names if isinstance(n, str)])
	_state_set(_INFLIGHT_KEY, "")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the revert


def _turn_backfill_start() -> str:
	return str(frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-TURN_BACKFILL_HOURS))


def _refusal_ids(e: Exception) -> tuple[str, str]:
	return (getattr(e, "code", None) or "", getattr(e, "exc_type", None) or "")


def _is_retry_later(e: Exception) -> bool:
	return _RETRY_LATER in _refusal_ids(e)


def _is_invalid_batch(e: Exception) -> bool:
	return _INVALID_BATCH in _refusal_ids(e)


def _counts_toward_drop(e: Exception) -> bool:
	"""True for a failure admin ANSWERED with (or a bug in building the batch): the
	same batch is likely to fail again. An outage (a transport error, 401/403, 429)
	or a stopped tenant says nothing about the batch, so it is waited out."""
	if isinstance(e, (AdminAuthError, AdminRateLimitedError)):
		return False
	if isinstance(e, AdminValidationError):
		return not _is_retry_later(e)
	if isinstance(e, AdminUnreachableError):
		# A network failure is raised `from` the requests error; a 5xx or a
		# non-JSON body (admin answered) has no cause.
		return not isinstance(e.__cause__, requests.RequestException)
	return True


def _batch_id(log_mark, turn_mark, ui_names: list[str]) -> str:
	raw = f"{log_mark}|{turn_mark}|{ui_names[0] if ui_names else ''}"
	return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _drop_reason(e: Exception, batch_id: str) -> str:
	"""Why this failed batch should be dropped, or "" to retry it. Counts the
	cycles in a row the SAME batch (same cursors and first buffer row) failed."""
	if _is_invalid_batch(e):
		return f"admin refused the payload ({_INVALID_BATCH})"
	if not _counts_toward_drop(e):
		return ""
	seen_id, _sep, seen_n = (_state_get(_BATCH_KEY) or "").partition(":")
	n = frappe.utils.cint(seen_n) + 1 if seen_id == batch_id else 1
	_state_set(_BATCH_KEY, f"{batch_id}:{n}")
	if n >= MAX_BATCH_FAILURES:
		return f"failed {n} cycles in a row ({_failure_cause(e)})"
	return ""


def _finish_dropped(size: int, lanes: dict, reason: str) -> None:
	"""The batch stays marked sent and the cursors have moved on: it is gone, so
	say so where an operator will look."""
	_state_set(_BATCH_KEY, "")
	_state_set(_INFLIGHT_KEY, "")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- drop the batch
	detail = f"dropped {size} errors ({lanes}): {reason}"
	frappe.logger("jarvis.error_push").warning(detail)
	_log_throttled("dropped", 1, "jarvis errors: admin refused rollup batch", detail)


def _wire(row: dict) -> dict:
	"""Datetimes as strings, so the batch is plain JSON for admin."""
	return {k: (str(v) if isinstance(v, datetime) else v) for k, v in row.items()}


def _collect_ui_errors() -> tuple[list[str], list[dict]]:
	"""Claim and return the current unpushed UI rows. The claim is NOT committed
	here (see ``_do_push``).

	Claim = flip ``pushed`` 0 -> 1 up front, THEN read. This closes the
	occurrences-lost-between-collect-and-mark window: once a row is claimed, a new
	report of the same error folds into a fresh ``pushed=0`` row (``_bump_row``
	filters on ``pushed = 0``) instead of a row we're about to mark sent, so its
	count is never silently dropped. The claimed rows' counts are frozen, so what
	we send matches what we mark sent - exactly once. On push failure the caller
	reverts the claim (``_revert_claim``)."""
	names = frappe.get_all(DT, filters={"pushed": 0}, order_by="last_seen asc", limit=UI_CAP, pluck="name")
	if not names:
		return [], []

	now = frappe.utils.now_datetime()
	frappe.db.sql(
		"UPDATE `tabJarvis Client Error` SET pushed = 1, pushed_at = %(now)s WHERE name IN %(names)s AND pushed = 0",
		{"now": now, "names": tuple(names)},
	)

	rows = frappe.get_all(
		DT,
		filters={"name": ["in", names], "pushed": 1},
		fields=[
			"name",
			"surface",
			"route",
			"error_code",
			"error_class",
			"message",
			"stack",
			"user",
			"conversation",
			"run_id",
			"fingerprint",
			"count",
			"first_seen",
			"last_seen",
		],
		order_by="last_seen asc",
	)
	batch = [
		{
			**_lane_of(r.surface),
			"surface": r.surface or "unknown",
			"route": r.route or "",
			"error_code": r.error_code or "",
			"error_class": r.error_class or "",
			"message": r.message or "",
			"stack": r.stack or "",
			"user_ref": r.user or "",
			"conversation_ref": r.conversation or "",
			"run_id": r.run_id or "",
			"fingerprint": r.fingerprint,
			"count": int(r.count or 1),
			"first_seen": str(r.first_seen) if r.first_seen else None,
			"last_seen": str(r.last_seen) if r.last_seen else None,
		}
		for r in rows
	]
	return [r.name for r in rows], batch


def _lane_of(surface: str | None) -> dict:
	"""Kind and severity of a buffer row: a server-written row (a failed request
	or tool) is an exception at its surface's severity, a browser row is a UI
	error."""
	severity = api_errors.SERVER_SURFACES.get(surface or "")
	if severity:
		return {"kind": "exception", "severity": severity}
	return {"kind": "ui", "severity": "error"}


def _revert_claim(names: list[str]) -> None:
	"""Un-claim rows a failed push had marked sent, so the next cycle retries."""
	if not names:
		return
	frappe.db.sql(
		"UPDATE `tabJarvis Client Error` SET pushed = 0, pushed_at = NULL WHERE name IN %(names)s AND pushed = 1",
		{"names": tuple(names)},
	)


def _cursor_order(cursor: str) -> tuple:
	ts, _sep, name = str(cursor).partition("|")
	return frappe.utils.get_datetime(ts), name


def _advance_watermark(key: str, old: str | None, new: str | None) -> None:
	"""Move a cursor to ``new``, and only ever forward: a cursor read before a
	slow call must not drag back one that moved meanwhile."""
	if not new or new == old:
		return
	current = _state_get(key)
	if current:
		try:
			if _cursor_order(new) <= _cursor_order(current):
				return
		except Exception:
			pass  # an unreadable stored cursor is replaced
	_state_set(key, new)


# --------------------------------------------------------------------------- #
# Pipeline health
# --------------------------------------------------------------------------- #
def _failure_cause(e: Exception) -> str:
	code = getattr(e, "code", None) or getattr(e, "exc_type", None) or ""
	return f"{type(e).__name__}: {code}" if code else type(e).__name__


def _note_success() -> None:
	"""Uncommitted: rides the commit of the cursors."""
	_state_set(_OK_KEY, str(frappe.utils.now_datetime()))
	_state_set(_FAILURES_KEY, "0")
	_state_set(_CAUSE_KEY, "")
	_state_set(_BATCH_KEY, "")


def _note_failure(e: Exception) -> None:
	"""Record a failed cycle and, once the last success is over an hour old, say so
	in the Error Log (at most every 6 hours). Never raises."""
	try:
		now = frappe.utils.now_datetime()
		cause = _failure_cause(e)
		last_ok = _state_get(_OK_KEY)
		if not last_ok:
			# Never pushed: count the stall from the first failure we saw.
			last_ok = str(now)
			_state_set(_OK_KEY, last_ok)
		_state_set(_FAILURES_KEY, str(frappe.utils.cint(_state_get(_FAILURES_KEY)) + 1))
		_state_set(_CAUSE_KEY, cause)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the health record
		hours = (now - frappe.utils.get_datetime(last_ok)).total_seconds() / 3600
		if hours >= STALLED_AFTER_H:
			failures = _state_get(_FAILURES_KEY)
			_log_throttled(
				"stalled",
				6,
				f"jarvis errors: rollup push stalled {int(hours)}h ({cause})",
				f"No rollup has reached admin since {last_ok}; {failures} failed cycles in a row. "
				"Defaults jarvis_error_push_* hold the detail.",
			)
	except Exception:
		frappe.logger("jarvis.error_push").warning("could not record a failed push", exc_info=True)


def _log_scan_problems(**lanes: dict) -> None:
	"""Rows a scan could not read are skipped, which hides a lane that reads
	nothing: say so, with the first error."""
	for lane, result in lanes.items():
		failed = result.get("failed", 0)
		if not failed:
			continue
		detail = (
			f"{lane}: {failed} of {result.get('scanned', 0)} scanned rows could not be read "
			f"({result.get('matched', 0)} kept); first error: {result.get('first_error', '')}"
		)
		frappe.logger("jarvis.error_push").warning(detail)
		_log_throttled(f"scan_{lane}", 6, "jarvis errors: could not read some rows", detail)


def _log_throttled(key: str, hours: int, title: str, message: str) -> None:
	"""Write an Error Log row at most once per ``hours`` for ``key``. These rows
	come from this module, so the Error Log lane never forwards them. The bucket
	lives in a default, not the cache: every changed default flushes the site
	cache, which every push cycle does, and a cached throttle would not survive
	it. Never raises."""
	try:
		bucket = str(int(time.time()) // (3600 * hours))
		throttle_key = f"jarvis_error_push_log_{key}"
		if _state_get(throttle_key) == bucket:
			return
		_state_set(throttle_key, bucket)
		frappe.log_error(title=title, message=message)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist the throttle with its row
	except Exception:
		pass


def _log_push_failure_throttled() -> None:
	"""Log an unexpected push failure at most once per hour. Never raises."""
	_log_throttled("failed", 1, "jarvis errors: rollup push failed", frappe.get_traceback())


def prune_pushed_client_errors() -> int:
	"""Daily: keep the local buffer bounded. Deletes (1) forwarded rows past the
	retention window, (2) undeliverable unpushed rows older than a long window,
	and (3) the oldest unpushed rows above a hard ceiling, each in batches until
	under its cap. The durable record lives in the admin's Jarvis Tenant Error, so
	losing an old local row is fine; losing one that was never sent is logged."""
	now = frappe.utils.now_datetime()

	pushed_cutoff = frappe.utils.add_days(now, -PUSHED_RETENTION_DAYS)
	deleted = _prune({"pushed": 1, "pushed_at": ["<", pushed_cutoff]})

	stale_cutoff = frappe.utils.add_days(now, -UNPUSHED_RETENTION_DAYS)
	unsent = _prune({"pushed": 0, "last_seen": ["<", stale_cutoff]})
	unsent += _prune_overflow()

	if unsent:
		detail = f"deleted {unsent} errors that were never sent to admin (older than {UNPUSHED_RETENTION_DAYS} days or over {MAX_BUFFER_ROWS} waiting)"
		frappe.logger("jarvis.error_push").warning(detail)
		_log_throttled("pruned_unsent", 24, "jarvis errors: unsent errors pruned", detail)
	return deleted + unsent


def _prune(filters: dict) -> int:
	"""Delete the rows matching ``filters``, oldest first, a batch at a time."""
	deleted = 0
	for _ in range(PRUNE_MAX_BATCHES):
		names = frappe.get_all(DT, filters=filters, order_by="last_seen asc", limit=PRUNE_BATCH, pluck="name")
		deleted += _delete_rows(names)
		if len(names) < PRUNE_BATCH:
			break
	return deleted


def _prune_overflow() -> int:
	deleted = 0
	for _ in range(PRUNE_MAX_BATCHES):
		overflow = frappe.db.count(DT, {"pushed": 0}) - MAX_BUFFER_ROWS
		if overflow <= 0:
			break
		names = frappe.get_all(
			DT,
			filters={"pushed": 0},
			order_by="last_seen asc",
			limit=min(overflow, PRUNE_BATCH),
			pluck="name",
		)
		if not names:
			break
		deleted += _delete_rows(names)
	return deleted


def _delete_rows(names: list[str]) -> int:
	if not names:
		return 0
	frappe.db.delete(DT, {"name": ("in", names)})
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- persist prune
	return len(names)
