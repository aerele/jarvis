"""Out-of-band push of tenant errors to the admin control plane.

Modeled on ``jarvis.chat.usage_push``: self-gating (skip un-onboarded),
best-effort, and it NEVER raises into the scheduler. Runs on the
``*/5`` cron so the admin's Errors feed and Fleet Console badge stay fresh
without ever touching the chat hot path.

Two sources feed one batch:
  - unpushed ``Jarvis Client Error`` rows  (UI errors captured by the reporters)
  - new ``Error Log`` rows past a durable watermark  (code-level exceptions,
    filtered to the jarvis app by ``api_errors.collect_error_log``)

Only scrubbed text is ever sent; scrubbing already happened at capture time.

Unlike usage_push (a daily job), this runs every 5 min, so an admin outage
would fire the failure path 288x/day. Two guards keep that from becoming noise:
transient admin errors are swallowed silently, and any unexpected failure is
logged at most once an hour (see ``push_error_rollup``). The reporter's own
modules are also excluded from lane-2 forwarding (``api_errors.is_jarvis_error``)
so a push failure can never be forwarded back to admin about its own outage.
"""

from __future__ import annotations

import time

import frappe

from jarvis import api_errors
from jarvis.chat.usage_push import _admin_configured
from jarvis.exceptions import AdminAuthError, AdminRateLimitedError, AdminUnreachableError

DT = "Jarvis Client Error"

#: Durable cursor (global DefaultValue) on Error Log.creation, so a lost Redis
#: cache can't make us re-forward exceptions we already sent.
_WATERMARK_KEY = "jarvis_error_log_watermark"

#: Throttle key: hour bucket of the last unexpected-failure log.
_LOG_THROTTLE_KEY = "jarvis_error_push_last_log_hour"

#: Bounds one push. UI rows + the jarvis subset of one Error Log scan.
UI_CAP = 200

#: Delete forwarded local rows older than this (daily prune).
PUSHED_RETENTION_DAYS = 7

#: Delete undeliverable (still unpushed) rows older than this - if admin has been
#: unreachable this long the buffer must not grow forever.
UNPUSHED_RETENTION_DAYS = 30

#: Hard ceiling on the unpushed buffer between daily prunes; the oldest beyond it
#: are dropped so a failing push can't grow the table without bound.
MAX_BUFFER_ROWS = 5000


def push_error_rollup() -> None:
	"""``*/5`` scheduler entry. Self-gating + best-effort; NEVER raises."""
	try:
		if not _admin_configured():
			return
		_do_push()
	except (AdminAuthError, AdminUnreachableError, AdminRateLimitedError):
		# Not onboarded, admin down, or throttled. The claimed rows were already
		# reverted in _do_push and the watermark did not advance, so nothing is
		# lost. Stay silent: at */5 a logged outage is 288 rows/day.
		return
	except Exception:
		# A genuine bug in the push path. Log it - but at most once an hour, so a
		# persistent fault can't itself flood the local Error Log every 5 min.
		_log_push_failure_throttled()


def _do_push() -> None:
	# Delivery is at-least-once, by design. The claim is NOT committed before the
	# HTTP push, so if the worker crashes mid-push the transaction rolls back, the
	# rows revert to pushed=0, and the next cycle re-sends. Admin accumulates with
	# `count = count + VALUES(count)`, so a crash after admin received the batch
	# double-counts that batch. This is the deliberate trade: re-sending (a small
	# count inflation) beats committing the claim first, where the same crash would
	# instead LOSE the batch. Telemetry-grade either way.
	ui_names, ui_rows = _collect_ui_errors()  # CLAIMS the rows (pushed 0 -> 1)
	watermark = frappe.db.get_default(_WATERMARK_KEY) or None
	log_result = api_errors.collect_error_log(watermark)
	errors = ui_rows + log_result["rows"]
	if not errors:
		# Nothing to send. Still advance the watermark if the scan moved it past
		# framework noise, so we don't re-scan those rows next cycle.
		_advance_watermark(watermark, log_result["watermark"])
		frappe.db.commit()
		return

	from jarvis import admin_client

	try:
		admin_client.push_error_rollup(errors)
	except Exception:
		# Push failed: un-claim the UI rows so the next cycle re-sends them, and
		# leave the watermark where it was. Re-raise for push_error_rollup to
		# classify (transient -> silent, unexpected -> throttled log).
		_revert_claim(ui_names)
		frappe.db.commit()
		raise

	# Success: the UI rows are already marked pushed (the claim WAS the mark).
	# Advance the Error Log watermark past what we forwarded.
	_advance_watermark(watermark, log_result["watermark"])
	frappe.db.commit()


def _collect_ui_errors() -> tuple[list[str], list[dict]]:
	"""Claim and return the current unpushed UI rows.

	Claim = flip ``pushed`` 0 -> 1 up front, THEN read. This closes the
	occurrences-lost-between-collect-and-mark window: once a row is claimed, a new
	report of the same error folds into a fresh ``pushed=0`` row (``_ingest_one``
	filters on ``pushed=0``) instead of a row we're about to mark sent, so its
	count is never silently dropped. The claimed rows' counts are frozen, so what
	we send matches what we mark sent - exactly once. On push failure the caller
	reverts the claim (``_revert_claim``)."""
	names = frappe.get_all(DT, filters={"pushed": 0}, order_by="last_seen asc", limit=UI_CAP, pluck="name")
	if not names:
		return [], []

	now = frappe.utils.now_datetime()
	placeholders = ", ".join(["%s"] * len(names))
	frappe.db.sql(
		f"UPDATE `tab{DT}` SET pushed = 1, pushed_at = %s WHERE name IN ({placeholders}) AND pushed = 0",
		[now, *names],
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
			"kind": "ui",
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
			"severity": "error",
		}
		for r in rows
	]
	return [r.name for r in rows], batch


def _revert_claim(names: list[str]) -> None:
	"""Un-claim rows a failed push had marked sent, so the next cycle retries."""
	if not names:
		return
	placeholders = ", ".join(["%s"] * len(names))
	frappe.db.sql(
		f"UPDATE `tab{DT}` SET pushed = 0, pushed_at = NULL WHERE name IN ({placeholders}) AND pushed = 1",
		names,
	)


def _advance_watermark(old: str | None, new: str | None) -> None:
	if new and new != old:
		frappe.db.set_default(_WATERMARK_KEY, new)


def _log_push_failure_throttled() -> None:
	"""Log an unexpected push failure at most once per hour. Never raises."""
	try:
		hour = int(time.time()) // 3600
		if frappe.cache.get_value(_LOG_THROTTLE_KEY, expires=True) == hour:
			return
		frappe.cache.set_value(_LOG_THROTTLE_KEY, hour, expires_in_sec=7200)
		frappe.log_error(title="jarvis errors: rollup push failed", message=frappe.get_traceback())
	except Exception:
		pass


def prune_pushed_client_errors() -> int:
	"""Daily: keep the local buffer bounded. Deletes (1) forwarded rows past the
	retention window, (2) undeliverable unpushed rows older than a long window,
	and (3) the oldest unpushed rows above a hard ceiling. The durable record
	lives in the admin's Jarvis Tenant Error, so losing an old local row is fine."""
	now = frappe.utils.now_datetime()
	deleted = 0

	pushed_cutoff = frappe.utils.add_days(now, -PUSHED_RETENTION_DAYS)
	deleted += _delete_rows(
		frappe.get_all(DT, filters={"pushed": 1, "pushed_at": ["<", pushed_cutoff]}, pluck="name", limit=1000)
	)

	stale_cutoff = frappe.utils.add_days(now, -UNPUSHED_RETENTION_DAYS)
	deleted += _delete_rows(
		frappe.get_all(DT, filters={"pushed": 0, "last_seen": ["<", stale_cutoff]}, pluck="name", limit=1000)
	)

	overflow = frappe.db.count(DT, {"pushed": 0}) - MAX_BUFFER_ROWS
	if overflow > 0:
		deleted += _delete_rows(
			frappe.get_all(
				DT, filters={"pushed": 0}, order_by="last_seen asc", limit=min(overflow, 2000), pluck="name"
			)
		)

	if deleted:
		frappe.db.commit()
	return deleted


def _delete_rows(names: list[str]) -> int:
	deleted = 0
	for name in names:
		try:
			frappe.delete_doc(DT, name, ignore_permissions=True, force=True, delete_permanently=True)
			deleted += 1
		except Exception:
			frappe.logger("jarvis.client_errors").warning(f"could not prune {name}", exc_info=True)
	return deleted
