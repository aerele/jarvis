"""File Box sheets: sealing, ended sheets and the reconciler backstop (T3, S1b).

A sheet seals (``collecting`` 1 -> 0) once, when the turn that opened it ends: the
``file_box_sheet_seal`` finalize effect, the errored / legacy turn ends
(``after_turn``), a later turn's first write (``seal_if_stale``) and the reconciler
(``backstop``). Only the CAS winner notifies the owner; an empty sheet is dropped
quietly. Every ended sheet closes its still-Pending questions in its settle
continuation (``on_settled``). Apply (S2) builds on this."""

from __future__ import annotations

import frappe

from jarvis.chat import held_sheets, held_writes
from jarvis.chat.held_sheet_board import counts_line
from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._store import (
	DISCARDED,
	FAILED,
	PENDING,
	SHEET,
	TERMINAL,
	_terminal_update,
	lock_conversation,
	rowcount,
	seal_sheet,
	table_ready,
)

PA = "Jarvis Pending Action"
TURN = "Jarvis Chat Turn"


# --------------------------------------------------------------------------- #
# Sealing: once, when the turn that opened the sheet ends
# --------------------------------------------------------------------------- #
BACKSTOP_AFTER_S = 300  # idle this long, with no live turn or reply: the reconciler seals it
COLLECTING_MAX_S = 7200  # still collecting after this: failed, with an admin alert
# A streaming/recovering latest-assistant row older than this never blocks the seal.
# Own constant, not the ladder's 3h ORPHAN_MAX_AGE_SECONDS: that one exceeds
# COLLECTING_MAX_S, so it would never let a genuinely stuck sheet seal gracefully
# before _fail_stuck hard-fails it at the 2h mark.
STREAMING_FRESH_S = 1800
ROUTING_BACKLOG_HOURS = 24
ROUTING_BACKLOG_ALERT = 10
_SCAN = 200
CLOSED_NOTE = "(sheet discarded - no action taken)"
EMPTY_OUTCOME = {"records": [], "answers": []}


def _collecting(conversation: str):
	rows = frappe.db.sql(
		"SELECT * FROM `tabJarvis Pending Action` WHERE conversation=%(c)s AND kind=%(k)s"
		" AND status='Pending' AND collecting=1 ORDER BY creation DESC LIMIT 1",
		{"c": conversation, "k": SHEET},
		as_dict=True,
	)
	return rows[0] if rows else None


def _running(turn: str) -> bool:
	from jarvis.chat.turn_state import NONTERMINAL_STATES

	return frappe.db.get_value(TURN, turn, "state") in NONTERMINAL_STATES


def _ended_by(sheet, turn: str | None, legacy: bool) -> bool:
	"""Whether this turn end owns the sheet: the turn that opened it; with ``legacy``
	(pump off) also one opened with no Turn row, or, with no ``turn`` (recovery), one
	whose turn no longer runs (never a Turn-less one: it may be another run's)."""
	opened = sheet.collecting_turn or ""
	if turn and opened == turn:
		return True
	if not legacy:
		return False
	return not opened if turn else bool(opened) and not _running(opened)


def seal_turn(conversation: str, turn: str | None, *, legacy: bool = False) -> str | None:
	"""Turn end: seal the sheet this turn collected, or discard it when the user
	stopped the turn. Idempotent, and never another turn's sheet. The sealed name."""
	if not conversation or not table_ready():
		return None
	if not frappe.db.exists(
		PA, {"conversation": conversation, "kind": SHEET, "status": PENDING, "collecting": 1}
	):
		return None
	frappe.db.commit()
	lock_conversation(conversation)
	sheet = _collecting(conversation)
	if not sheet or not _ended_by(sheet, turn, legacy):
		frappe.db.commit()
		return None
	if held_sheets.user_stopped(conversation, sheet.collecting_turn or turn):
		_discard(sheet, "cancelled", by=sheet.owner_user)
		return None
	return _seal_locked(sheet)


def after_turn(conversation: str, run_id: str | None = None, *, legacy: bool = False) -> None:
	"""``seal_turn`` for turn ends that bypass settlement (the errored paths, the
	pump-off completion and recovery). Best-effort: logged, never raised."""
	try:
		seal_turn(conversation, run_id, legacy=legacy)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="jarvis.file_box.sheet_seal_failed",
			message=f"{conversation} {run_id or ''}\n{frappe.get_traceback()}",
		)
		frappe.db.commit()


def seal_if_stale(conversation: str, sheet):
	"""A write that finds a collecting sheet an OLDER turn opened seals it first: a
	new turn never appends to it. Holds the conversation lock again on return. The
	sheet to use now."""
	if not sheet or held_sheets.paused(sheet) or not sheet.collecting_turn:
		return sheet
	if sheet.collecting_turn == held_sheets.live_turn(conversation):
		return sheet
	_seal_locked(sheet)
	lock_conversation(conversation)
	return held_sheets.live_sheet(conversation)


def _seal_locked(sheet) -> str | None:
	"""Under the conversation lock: seal (only the CAS winner notifies), or drop an
	empty sheet quietly. Commits."""
	if not seal_sheet(sheet.name):
		frappe.db.commit()
		return None
	if not (sheet.record_count or sheet.question_count):
		_discard(sheet, "discarded", empty=True)
		return None
	frappe.db.commit()
	line = counts_line(sheet.sheet_counts, sheet.question_count)
	title = held_writes._clean_title(f"{sheet.summary} ({line})" if line else sheet.summary)
	held_writes._notify(sheet.owner_user, sheet.conversation, title, sheet.name)
	return sheet.name


def _discard(sheet, reason: str, *, by: str | None = None, empty: bool = False) -> bool:
	"""Pending -> Discarded with nobody to resume (a stopped run, an empty sheet);
	settling closes its questions. An empty sheet gives its pause back. Commits."""
	from jarvis.chat.pending_actions import settle

	held_sheets._drop_waiters(sheet.name)
	cols = {"decided_by": by} if by else {}
	moved = _terminal_update(sheet.name, [PENDING], DISCARDED, reason_code=reason, **cols)
	if moved and empty:
		frappe.db.sql(
			"UPDATE `tabJarvis Conversation` SET filebox_sheet_count=GREATEST(IFNULL(filebox_sheet_count, 0)-1, 0)"
			" WHERE name=%(c)s",
			{"c": sheet.conversation},
		)
	frappe.db.commit()
	if moved:
		settle(sheet.name)
	return moved


# --------------------------------------------------------------------------- #
# Ended sheets: the settle continuation
# --------------------------------------------------------------------------- #
def on_settled(conversation, items) -> None:
	"""``_settle.CONTINUATIONS["file_box_sheet"]``: in settle's transaction, close the
	ended sheets' questions and queue their run's resume (a stopped run's sheet has no
	waiter left: nothing resumes)."""
	from jarvis.chat.pending_actions import claim_settled

	won = claim_settled([i["name"] for i in items])
	if won:
		close_ended(won)
		held_writes.queue_resumes(won)


def close_ended(names) -> int:
	"""Dismiss the still-Pending questions of these ended sheets (they never resume on
	their own) and leave each a valid, possibly empty, ``sheet_outcome``. No commit.
	The questions closed."""
	names = tuple(names)
	if not names:
		return 0
	now = frappe.utils.now_datetime()
	frappe.db.sql(
		"UPDATE `tabJarvis Approval Request` SET status='Dismissed', decision=%(d)s, decided_at=%(now)s,"
		" modified=%(now)s WHERE sheet IN %(n)s AND status='Pending'",
		{"d": CLOSED_NOTE, "now": now, "n": names},
	)
	closed = rowcount()
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET sheet_outcome=%(o)s WHERE name IN %(n)s AND kind=%(k)s"
		" AND sheet_outcome IS NULL",
		{"o": _seal.json_text(EMPTY_OUTCOME), "n": names, "k": SHEET},
	)
	return closed


_ENDED = {
	"cancelled": "this document's approval sheet was withdrawn and nothing on it was created. Stop "
	"and end with a one-line summary saying this file needs attention.",
	"failed": "this document's approval sheet could not be applied and nothing on it was created. "
	"Do not retry it or create its records another way. Stop and end with a one-line summary "
	"saying this file needs attention.",
	"stuck": "this document's approval sheet got stuck while it was still being listed and nothing on "
	"it was created. Do not retry it or create its records another way. Stop and end with a "
	"one-line summary saying this file needs attention.",
	"unknown": "applying this document's approval sheet was interrupted and its outcome is unknown. "
	"Do not retry it. Stop and end with a one-line summary telling the user to check.",
	"skipped": "the approver skipped this document's approval sheet: nothing on it was created. Do "
	"not create those records, or anything that needs them, another way. Stop and end with a "
	"one-line summary saying it was skipped.",
}


def resume_message(row) -> str:
	"""The DATA-quoted resume of an ended sheet's run, from retained columns. S1b
	sheets end unapplied; S2 adds the applied outcomes from ``sheet_outcome``."""
	from jarvis.chat.turn_handler import _safe_label_name

	key = "stuck" if row.reason_code == "stuck" else held_writes._verdict(row)
	verdict = _ENDED.get(key, _ENDED["unknown"])
	return held_writes._SCAFFOLD.format(
		verdict=verdict, data=_safe_label_name(row.summary or "approval sheet")
	)


# --------------------------------------------------------------------------- #
# The reconciler backstop
# --------------------------------------------------------------------------- #
# No live turn, and the latest reply isn't streaming or recovering while fresh (the
# ladder's arm): an orphaned row never holds the seal.
_IDLE = (
	" AND NOT EXISTS (SELECT 1 FROM `tabJarvis Chat Turn` t WHERE t.conversation = pa.conversation"
	" AND t.state IN %(live)s) AND NOT EXISTS (SELECT 1 FROM `tabJarvis Chat Message` m"
	" WHERE m.conversation = pa.conversation AND m.role = 'assistant'"
	" AND (m.streaming = 1 OR m.recovering = 1) AND m.modified >= %(fresh)s"
	" AND NOT EXISTS (SELECT 1 FROM `tabJarvis Chat Message` n WHERE n.conversation = m.conversation"
	" AND n.role = 'assistant' AND n.seq > m.seq))"
)


def _stranded(name: str | None = None) -> list:
	"""Collecting sheets idle past ``BACKSTOP_AFTER_S`` with no live turn and no
	streaming or recovering reply fresher than ``STREAMING_FRESH_S`` (never on age
	alone)."""
	from jarvis.chat.turn_state import NONTERMINAL_STATES

	now = frappe.utils.now_datetime()
	cut = frappe.utils.add_to_date(now, seconds=-BACKSTOP_AFTER_S)
	fresh = frappe.utils.add_to_date(now, seconds=-STREAMING_FRESH_S)
	return frappe.db.sql(
		"SELECT pa.name, pa.conversation FROM `tabJarvis Pending Action` pa WHERE pa.kind=%(k)s"
		" AND pa.status='Pending' AND pa.collecting=1 AND pa.modified < %(cut)s"
		+ (" AND pa.name=%(n)s" if name else "")
		+ _IDLE
		+ " ORDER BY pa.modified LIMIT %(lim)s",
		{"k": SHEET, "cut": cut, "fresh": fresh, "n": name, "live": NONTERMINAL_STATES, "lim": _SCAN},
		as_dict=True,
	)


def _each_locked(rows, act) -> list[str]:
	"""``act(sheet)`` per row under its conversation lock, on the row re-read there."""
	done = []
	for r in rows:
		try:
			frappe.db.commit()
			lock_conversation(r.conversation)
			sheet = _collecting(r.conversation)
			if sheet and sheet.name == r.name and act(sheet):
				done.append(r.name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="jarvis.file_box.sheet_backstop_failed", message=frappe.get_traceback())
			frappe.db.commit()
	return done


def _seal_stranded() -> list[str]:
	def act(sheet):
		if not _stranded(sheet.name):  # re-checked under the lock
			return False
		_seal_locked(sheet)
		return True

	sealed = _each_locked(_stranded(), act)
	if sealed:
		frappe.log_error(
			title="jarvis.file_box.sheet_seal_backstop",
			message="Sealed by the reconciler (their turn-end seal never ran): " + ", ".join(sealed),
		)
		frappe.db.commit()
	return sealed


def _fail_stuck() -> list[str]:
	from jarvis.chat.pending_actions import settle

	cut = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-COLLECTING_MAX_S)
	rows = frappe.db.sql(
		"SELECT name, conversation FROM `tabJarvis Pending Action` WHERE kind=%(k)s AND status='Pending'"
		" AND collecting=1 AND creation < %(cut)s ORDER BY creation LIMIT %(lim)s",
		{"k": SHEET, "cut": cut, "lim": _SCAN},
		as_dict=True,
	)
	failed = _each_locked(
		rows, lambda sheet: _terminal_update(sheet.name, [PENDING], FAILED, reason_code="stuck")
	)
	for name in failed:
		settle(name)
	if failed:
		_alert(
			"sheet_collecting_stuck",
			f"{len(failed)} File Box sheet(s) still collecting after {COLLECTING_MAX_S // 3600} h were "
			"failed (a stuck turn, or its seal kept failing): " + ", ".join(failed),
		)
	return failed


def _close_orphans() -> int:
	"""Questions still Pending on a sheet that ended and settled without closing them,
	or whose sheet is gone."""
	names = frappe.db.sql_list(
		"SELECT DISTINCT a.sheet FROM `tabJarvis Approval Request` a LEFT JOIN `tabJarvis Pending Action` p"
		" ON p.name = a.sheet WHERE a.status='Pending' AND a.sheet > ''"
		" AND (p.name IS NULL OR (p.settled=1 AND p.status IN %(t)s)) LIMIT %(lim)s",
		{"t": TERMINAL, "lim": _SCAN},
	)
	closed = close_ended(names) if names else 0
	frappe.db.commit()
	return closed


def _routing_backlog() -> int:
	cut = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-ROUTING_BACKLOG_HOURS)
	count = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabJarvis Approval Request` WHERE status='Pending'"
		" AND IFNULL(routing, '') <> '' AND creation < %(cut)s",
		{"cut": cut},
	)[0][0]
	if count > ROUTING_BACKLOG_ALERT:
		_alert(
			"routing_backlog",
			f"{count} File Box routing questions have waited more than {ROUTING_BACKLOG_HOURS} h "
			f"(alert>{ROUTING_BACKLOG_ALERT}). Threshold in jarvis/chat/held_sheet_seal.py.",
		)
	return count


def _alert(event: str, message: str) -> None:
	"""An admin alert ``jarvis.file_box.<event>``, at most once a day (the
	``cards_aged`` pattern)."""
	from jarvis.chat.pending_actions._reconcile import _age_alert_deduped

	if not _age_alert_deduped(frappe.utils.now_datetime(), f"jarvis.file_box.{event}"):
		frappe.log_error(title=f"jarvis.file_box.{event}", message=message)
		frappe.db.commit()


def backstop() -> dict:
	"""Reconciler step: seal stranded sheets, fail stuck ones, close orphaned
	questions, and alert on a routing-question backlog."""
	if not table_ready():
		return {}
	return {
		"sealed": len(_seal_stranded()),
		"failed": len(_fail_stuck()),
		"closed": _close_orphans(),
		"routing_backlog": _routing_backlog(),
	}
