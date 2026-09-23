"""Reconciler (``*/5``) and purge (daily) for ``Jarvis Pending Action`` (§4.6).

Each reconciler step is isolated (one failing step never blocks the rest), and
every flip goes through ``_store``'s single terminal writer."""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, now_datetime

from jarvis.chat.pending_actions._settle import MAX_SETTLE_ATTEMPTS, settle, settle_batch
from jarvis.chat.pending_actions._store import (
	CANCELLED,
	EXECUTING,
	FAILED,
	PENDING,
	TERMINAL,
	_terminal_update,
	_transition,
	rowcount,
	table_ready,
)

INTERRUPT_AFTER_S = 600  # Executing longer than this: the worker died mid-call
SETTLE_AFTER_S = 120  # terminal but unsettled longer than this: the settle was lost
WAITER_MAX_ATTEMPTS = 5
OLD_PENDING_DAYS = 30
CARD_AGE_ALERT_DAYS = 7
CARD_AGE_ALERT = 25
PURGE_AFTER_DAYS = 7
PURGE_BATCH = 500
PURGE_MAX_BATCHES = 20
_SCAN = 200
_AGE_ALERT_TITLE = "jarvis.pending_action.cards_aged"
_AGE_ALERT_DEDUPE_HOURS = 24

# Held resume, as a dotted path: ``fn(pa_name)`` re-drives that row's ``due``
# waiters. None = no retry (they still fail past the budget).
HELD_RESUME: str | None = "jarvis.chat.held_writes.resume_waiters"
# ``fn(conversations)``: stamp waiters that failed past the budget on their
# conversations (after the flips commit: conversation before waiter everywhere).
HELD_FAILED: str | None = "jarvis.chat.held_writes.mark_resume_failed"


def _log(title: str, message: str) -> None:
	frappe.log_error(title=f"jarvis.pending_action.{title}", message=message)
	frappe.db.commit()


def _reap_interrupted() -> int:
	cutoff = add_to_date(now_datetime(), seconds=-INTERRUPT_AFTER_S)
	rows = frappe.db.sql(
		"SELECT name, batch_id FROM `tabJarvis Pending Action` WHERE status='Executing' AND executing_at < %(c)s"
		" ORDER BY executing_at LIMIT %(lim)s",
		{"c": cutoff, "lim": _SCAN},
		as_dict=True,
	)
	flipped = [r for r in rows if _terminal_update(r.name, [EXECUTING], FAILED, reason_code="interrupted")]
	frappe.db.commit()
	if flipped:
		_log(
			"interrupted",
			"Outcome unknown (interrupted mid-execution): " + ", ".join(r.name for r in flipped),
		)
	for r in flipped:
		if not r.batch_id:
			settle(r.name)
	# A crashed typed batch settles with its other cards: ONE continuation.
	for batch_id in dict.fromkeys(r.batch_id for r in flipped if r.batch_id):
		if not frappe.db.exists("Jarvis Pending Action", {"batch_id": batch_id, "status": EXECUTING}):
			settle_batch(batch_id)
	return len(flipped)


def _settle_unsettled() -> int:
	# ``modified`` = the terminal write (``decided_at`` is the claim: a deferred
	# batch's early rows would otherwise settle while the batch still runs).
	cutoff = add_to_date(now_datetime(), seconds=-SETTLE_AFTER_S)
	rows = frappe.db.sql(
		"SELECT name, batch_id FROM `tabJarvis Pending Action` WHERE settled=0 AND status IN %(t)s"
		" AND settle_attempts <= %(max)s AND modified < %(c)s"
		" ORDER BY modified LIMIT %(lim)s",
		{"t": TERMINAL, "max": MAX_SETTLE_ATTEMPTS, "c": cutoff, "lim": _SCAN},
		as_dict=True,
	)
	settled, batches = 0, []
	for r in rows:
		if r.batch_id:
			if r.batch_id not in batches:
				batches.append(r.batch_id)
		elif settle(r.name):
			settled += 1
	for batch_id in batches:
		# A batch still running a card settles once it lands: ONE continuation.
		if not frappe.db.exists("Jarvis Pending Action", {"batch_id": batch_id, "status": EXECUTING}):
			settled += settle_batch(batch_id)
	return settled


def _waiters_where(where: str, params: dict) -> list:
	"""Select first, then flip by primary key (no range locks over the waiter table)."""
	return frappe.db.sql(
		"SELECT name, conversation FROM `tabJarvis Pending Action Waiter`"
		f" WHERE parenttype='Jarvis Pending Action' AND {where} LIMIT %(lim)s",
		{**params, "lim": _SCAN},
		as_dict=True,
	)


def _retry_waiters() -> dict:
	now = now_datetime()
	stale = add_to_date(now, seconds=-INTERRUPT_AFTER_S)
	# A resume that claimed a waiter and died before its send committed (``done``
	# rides that commit) never ran: count the attempt and make it due again.
	for w in _waiters_where("resume_state='claimed' AND modified < %(stale)s", {"stale": stale}):
		frappe.db.sql(
			"UPDATE `tabJarvis Pending Action Waiter` SET resume_state='due',"
			" resume_attempts=resume_attempts+1, modified=%(now)s"
			" WHERE name=%(n)s AND resume_state='claimed' AND modified < %(stale)s",
			{"n": w.name, "now": now, "stale": stale},
		)
	failed_convs = []
	for w in _waiters_where(
		"resume_state='due' AND resume_attempts >= %(max)s", {"max": WAITER_MAX_ATTEMPTS}
	):
		frappe.db.sql(
			"UPDATE `tabJarvis Pending Action Waiter` SET resume_state='failed', modified=%(now)s"
			" WHERE name=%(n)s AND resume_state='due' AND resume_attempts >= %(max)s",
			{"n": w.name, "now": now, "max": WAITER_MAX_ATTEMPTS},
		)
		if rowcount():
			failed_convs.append(w.conversation)
	failed = len(failed_convs)
	frappe.db.commit()
	if failed_convs and HELD_FAILED:
		frappe.get_attr(HELD_FAILED)(failed_convs)
		frappe.db.commit()
	retried = 0
	if HELD_RESUME:
		resume = frappe.get_attr(HELD_RESUME)
		parents = frappe.db.sql_list(
			"SELECT DISTINCT parent FROM `tabJarvis Pending Action Waiter` WHERE parenttype='Jarvis Pending Action'"
			" AND resume_state='due' AND resume_attempts < %(max)s AND modified < %(c)s LIMIT %(lim)s",
			{"max": WAITER_MAX_ATTEMPTS, "c": add_to_date(now, seconds=-SETTLE_AFTER_S), "lim": _SCAN},
		)
		for parent in parents:
			try:
				resume(parent)
				retried += 1
			except Exception:
				frappe.db.rollback()
				_log("resume_failed", frappe.get_traceback())
	return {"failed": failed, "retried": retried}


def _cancel_disabled_owners() -> int:
	names = frappe.db.sql_list(
		"SELECT pa.name FROM `tabJarvis Pending Action` pa LEFT JOIN `tabUser` u ON u.name = pa.owner_user"
		" WHERE pa.status='Pending' AND (u.name IS NULL OR u.enabled = 0) LIMIT %(lim)s",
		{"lim": _SCAN},
	)
	moved = [n for n in names if _transition(n, [PENDING], CANCELLED, reason_code="owner_disabled") == "ok"]
	frappe.db.commit()
	if moved:
		_log("owner_disabled", "Cancelled (owner disabled or missing): " + ", ".join(moved))
	for name in moved:
		settle(name)
	return len(moved)


def _cancel_orphaned_chat() -> int:
	"""Pending chat cards whose conversation was deleted without ``on_trash``."""
	names = frappe.db.sql_list(
		"SELECT pa.name FROM `tabJarvis Pending Action` pa LEFT JOIN `tabJarvis Conversation` c"
		" ON c.name = pa.conversation WHERE pa.kind='chat' AND pa.status='Pending'"
		" AND IFNULL(pa.conversation, '') != '' AND c.name IS NULL LIMIT %(lim)s",
		{"lim": _SCAN},
	)
	moved = [n for n in names if _transition(n, [PENDING], CANCELLED, reason_code="cancelled") == "ok"]
	frappe.db.commit()
	if moved:
		_log("orphan_cancel", "Cancelled (conversation deleted): " + ", ".join(moved))
	for name in moved:
		settle(name)
	return len(moved)


def _warn_old_pending() -> int:
	count = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabJarvis Pending Action` WHERE status='Pending' AND creation < %(c)s",
		{"c": add_to_date(now_datetime(), days=-OLD_PENDING_DAYS)},
	)[0][0]
	if count:
		frappe.logger("jarvis.pending_action").warning(
			"pending actions older than %d days: %d", OLD_PENDING_DAYS, count
		)
	return count


def _age_alert_deduped(now) -> bool:
	try:
		return bool(
			frappe.db.sql(
				"SELECT name FROM `tabError Log` WHERE method=%(t)s AND creation >= %(s)s LIMIT 1",
				{"t": _AGE_ALERT_TITLE, "s": add_to_date(now, hours=-_AGE_ALERT_DEDUPE_HOURS)},
			)
		)
	except Exception:
		return False


def _cards_health() -> dict:
	"""The ``cards_open`` gauge plus an age alert on long-waiting chat cards."""
	from jarvis.chat.latency import get_logger

	now = now_datetime()
	open_n, aged = frappe.db.sql(
		"SELECT COUNT(*), COALESCE(SUM(creation < %(c)s), 0) FROM `tabJarvis Pending Action`"
		" WHERE kind='chat' AND status='Pending'",
		{"c": add_to_date(now, days=-CARD_AGE_ALERT_DAYS)},
	)[0]
	open_n, aged = int(open_n or 0), int(aged or 0)
	get_logger().info("pending_action_health cards_open=%d aged=%d", open_n, aged)
	if aged > CARD_AGE_ALERT and not _age_alert_deduped(now):
		frappe.log_error(
			title=_AGE_ALERT_TITLE,
			message=(
				f"{aged} chat action cards have waited more than {CARD_AGE_ALERT_DAYS} days "
				f"(alert>{CARD_AGE_ALERT}; cards_open={open_n}). Threshold in "
				"jarvis/chat/pending_actions/_reconcile.py."
			),
		)
		frappe.db.commit()
	return {"cards_open": open_n, "aged": aged}


def reconcile() -> dict:
	"""``*/5`` cron: interrupted, lost settles, waiter retries, disabled owners,
	orphaned chat cards, and the health signals."""
	if not table_ready():
		return {}
	out = {}
	for step in (
		_reap_interrupted,
		_settle_unsettled,
		_retry_waiters,
		_cancel_disabled_owners,
		_cancel_orphaned_chat,
		_warn_old_pending,
		_cards_health,
	):
		try:
			out[step.__name__.lstrip("_")] = step()
		except Exception:
			frappe.db.rollback()
			_log("reconcile_failed", frappe.get_traceback())
	return out


def purge() -> int:
	"""Daily (D3): delete settled terminal rows older than 7 days with their waiters,
	in committed batches. Never a Pending/Executing row, nor a held row with a waiter
	not yet resumed (due, claimed, or failed: a re-run or delete drops that one), nor
	a row another session holds (skipped, retried tomorrow)."""
	if not table_ready():
		return 0
	cutoff = add_to_date(now_datetime(), days=-PURGE_AFTER_DAYS)
	total = 0
	for _ in range(PURGE_MAX_BATCHES):
		names = frappe.db.sql_list(
			"SELECT pa.name FROM `tabJarvis Pending Action` pa WHERE pa.settled=1 AND pa.status IN %(t)s"
			" AND pa.settled_at < %(c)s AND NOT EXISTS (SELECT 1 FROM `tabJarvis Pending Action Waiter` w"
			" WHERE w.parent=pa.name AND w.parenttype='Jarvis Pending Action'"
			" AND IFNULL(w.resume_state, '') != 'done') LIMIT %(lim)s FOR UPDATE SKIP LOCKED",
			{"t": TERMINAL, "c": cutoff, "lim": PURGE_BATCH},
		)
		if not names:
			break
		frappe.db.sql(
			"DELETE FROM `tabJarvis Pending Action Waiter` WHERE parenttype='Jarvis Pending Action'"
			" AND parent IN %(n)s",
			{"n": tuple(names)},
		)
		frappe.db.sql(
			"DELETE FROM `tabJarvis Pending Action` WHERE name IN %(n)s AND settled=1 AND status IN %(t)s",
			{"n": tuple(names), "t": TERMINAL},
		)
		frappe.db.commit()
		total += len(names)
		if len(names) < PURGE_BATCH:
			break
	return total
