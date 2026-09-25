"""Row state for ``Jarvis Pending Action``: reads, locks and the only status writers.

Transitions are raw conditional UPDATEs (the controller refuses every ORM save).
``_terminal_update`` is the ONLY writer of a terminal status and always NULLs
``open_key``; ``claim`` is the only ``Pending -> Executing`` writer and
``release_sheet`` the only way back (a sheet apply that rolled back). A grep test
pins all three. Nothing here commits: callers own their transactions."""

from __future__ import annotations

import frappe

PA = "Jarvis Pending Action"
WAITER = "Jarvis Pending Action Waiter"
CONV = "Jarvis Conversation"

SHEET = "file_box_sheet"
KINDS = ("chat", "file_box_held", SHEET)
PENDING, EXECUTING = "Pending", "Executing"
EXECUTED, FAILED, DISCARDED, CANCELLED, SUPERSEDED = (
	"Executed",
	"Failed",
	"Discarded",
	"Cancelled",
	"Superseded",
)
TERMINAL = (EXECUTED, FAILED, DISCARDED, CANCELLED, SUPERSEDED)

# Fixed bench text per reason code; the row never carries free text (§4.1).
REASON_TEXT = {
	"failed": "The action could not be applied; nothing was changed.",
	"stale": "The record changed after this action was proposed.",
	"target_missing": "The record this action targets no longer exists.",
	"unverifiable": "This action can no longer be verified on this site.",
	"tampered": "This action failed an integrity check.",
	"interrupted": "The action was interrupted; its outcome is unknown.",
	"stuck": "The approval sheet got stuck while it was still being listed; nothing was changed.",
	"partial": "The action failed after part of it was saved.",
	"owner_disabled": "The user who owns this action is disabled or missing.",
	"cancelled": "The action was cancelled.",
	"archived": "The conversation was archived.",
	"discarded": "The action was discarded.",
	"superseded": "A newer proposal replaced this action.",
	# Only rows PR-3a cancelled for age still carry it (cards no longer expire).
	"expired": "The action expired.",
	"use_existing": "An existing record was used instead; nothing new was created.",
}

# Extra columns a terminal write may set (keys are interpolated, so allowlisted).
_TERMINAL_COLS = frozenset(
	{
		"decided_by",
		"result_doctype",
		"result_name",
		"sealed_settlement",
		"batch_id",
		"conversation",
		"adopted_conversation",
		"edited",
		"sheet_outcome",
	}
)
_TERMINAL_JSON = frozenset({"sheet_outcome"})


# What a sheet append may rewrite besides its envelope (keys are interpolated).
_SHEET_COLS = frozenset({"record_count", "sheet_counts", "needs_input", "needs_fix", "dedup_keys"})
_SHEET_JSON = frozenset({"sheet_counts", "needs_input", "needs_fix"})


def rowcount() -> int:
	"""Affected rows of the last statement, read before any commit resets it."""
	cursor = getattr(frappe.db, "_cursor", None)
	return int(cursor.rowcount) if cursor else 0


# One new column per doctype of the File Box routing + sheets migrate.
_FILEBOX_COLUMNS = (
	(PA, "collecting"),
	("Jarvis Approval Request", "sheet"),
	(CONV, "filebox_sheet_count"),
	("Jarvis Custom Skill", "file_box_creates"),
	("Jarvis Settings", "file_box_sheets"),
)


def filebox_migrated() -> bool:
	"""The File Box routing + sheets migrate ran. Code served ahead of it never names
	those columns: File Box runs as it did before them. Cached per request."""
	ready = getattr(frappe.local, "jarvis_filebox_migrated", None)
	if ready is None:
		ready = table_ready() and all(frappe.get_meta(dt).has_field(f) for dt, f in _FILEBOX_COLUMNS)
		frappe.local.jarvis_filebox_migrated = ready
	return ready


def table_ready() -> bool:
	"""False before migrate creates the table (hooks must no-op then)."""
	return bool(frappe.db.table_exists(PA))


def apply_ready() -> bool:
	"""The sheet-apply columns are migrated (``filebox_migrated``: the same migrate)."""
	return filebox_migrated()


def reseal_sheet(row, *, args: dict, card, **cols) -> bool:
	"""The collecting-sheet append (the only writer of a live sheet's envelope):
	re-seal ``args`` over the new card and write it only while ``row`` is still
	Pending and collecting AND its envelope is the one read (a CAS). The card is
	stored as the canonical text its hash covers. True iff written."""
	from jarvis.chat.pending_actions import _seal

	bad = set(cols) - _SHEET_COLS
	if bad:
		raise ValueError(f"not a sheet column: {sorted(bad)}")
	fresh = frappe._dict(row, card=_seal.json_text(card))
	params = {
		"n": row.name,
		"old": row.sealed_call,
		"new": _seal.seal_call(fresh, args, []),
		"card": fresh.card,
		"now": frappe.utils.now_datetime(),
		**{f"c_{k}": _seal.json_text(v) if k in _SHEET_JSON else v for k, v in cols.items()},
	}
	extra = "".join(f", `{k}`=%(c_{k})s" for k in cols)
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET sealed_call=%(new)s, card=%(card)s, modified=%(now)s"
		+ extra
		+ " WHERE name=%(n)s AND status='Pending' AND collecting=1 AND sealed_call=%(old)s",
		params,
	)
	return rowcount() == 1


def seal_sheet(name: str) -> bool:
	"""Collecting -> sealed (``collecting`` 1 -> 0) while Pending: the only writer of
	that flip (a CAS). True iff this call sealed it."""
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET collecting=0, modified=%(now)s"
		" WHERE name=%(n)s AND kind=%(k)s AND status='Pending' AND collecting=1",
		{"n": name, "k": SHEET, "now": frappe.utils.now_datetime()},
	)
	return rowcount() == 1


def get_row(name: str, *, lock: str | None = None) -> frappe._dict | None:
	"""One row as a dict. ``lock``: ``None``, ``"update"``, ``"nowait"`` or ``"skip"``."""
	suffix = {
		None: "",
		"update": " FOR UPDATE",
		"nowait": " FOR UPDATE NOWAIT",
		"skip": " FOR UPDATE SKIP LOCKED",
	}[lock]
	rows = frappe.db.sql(
		"SELECT * FROM `tabJarvis Pending Action` WHERE name=%(n)s" + suffix, {"n": name}, as_dict=True
	)
	return rows[0] if rows else None


def lock_conversation(conversation: str | None) -> None:
	"""Canonical lock order everywhere: conversation first, then the PA row."""
	if conversation:
		frappe.db.sql(
			"SELECT name FROM `tabJarvis Conversation` WHERE name=%(c)s FOR UPDATE", {"c": conversation}
		)


def _terminal_update(
	name: str,
	from_statuses,
	to: str,
	*,
	reason_code: str | None = None,
	reason: str | None = None,
	token: str | None = None,
	**cols,
) -> bool:
	"""Move ``name`` from one of ``from_statuses`` to the terminal ``to``, NULLing
	``open_key`` (and ending a sheet's collecting, dropping its apply decisions and
	errors) in the same statement; with ``token``, only while it is that sheet
	apply's. True iff this call made the transition."""
	if to not in TERMINAL:
		raise ValueError(f"not a terminal status: {to}")
	bad = set(cols) - _TERMINAL_COLS
	if bad:
		raise ValueError(f"not a terminal column: {sorted(bad)}")
	now = frappe.utils.now_datetime()
	params = {
		"n": name,
		"to": to,
		"from": tuple(from_statuses),
		"rc": reason_code or "",
		"reason": reason if reason is not None else REASON_TEXT.get(reason_code or "", ""),
		"now": now,
		"tok": token or "",
		**{f"c_{k}": _json_col(k, v) for k, v in cols.items()},
	}
	extra = "".join(f", `{k}`=%(c_{k})s" for k in cols)
	if filebox_migrated():
		extra += ", collecting=0, apply_request=NULL, apply_errors=NULL"
	mine = " AND IFNULL(apply_token, '')=%(tok)s" if token is not None else ""
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET status=%(to)s, open_key=NULL, reason_code=%(rc)s,"
		" reason=%(reason)s, decided_at=IFNULL(decided_at, %(now)s), modified=%(now)s" + extra + " "
		"WHERE name=%(n)s AND status IN %(from)s" + mine,
		params,
	)
	return rowcount() == 1


def _json_col(col: str, value):
	from jarvis.chat.pending_actions import _seal

	return _seal.json_text(value) if col in _TERMINAL_JSON else value


def _transition(name: str, from_statuses, to: str, **cols) -> str:
	"""Lock-aware terminal transition: ``ok``, ``locked`` (another actor holds the
	row: the caller's "running" / ConfirmationPendingError), ``missing`` or ``state``
	(the row is no longer in ``from_statuses``)."""
	row = get_row(name, lock="skip")
	if not row:
		return "locked" if frappe.db.exists(PA, name) else "missing"
	if row.status not in from_statuses:
		return "state"
	return "ok" if _terminal_update(name, from_statuses, to, **cols) else "state"


def claim(
	name: str,
	approver: str,
	*,
	batch_id: str | None = None,
	adopted_conversation: str | None = None,
	edited: bool = False,
	apply_token: str | None = None,
	apply_request: dict | None = None,
) -> bool:
	"""``Pending -> Executing`` with ``executing_at`` + ``decided_by`` (claim-first);
	``edited`` stamps an Edit & create; a sheet apply stores its token and the
	validated decisions (and clears the last errors and any stop)."""
	from jarvis.chat.pending_actions import _seal

	now = frappe.utils.now_datetime()
	sheet = (
		", apply_token=%(tok)s, apply_request=%(req)s, apply_errors=NULL, stop_requested=0"
		if apply_token
		else ""
	)
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET status='Executing', executing_at=%(now)s, decided_at=%(now)s,"
		" decided_by=%(by)s, batch_id=IFNULL(%(batch)s, batch_id),"
		" adopted_conversation=IFNULL(%(adopt)s, adopted_conversation), edited=%(edited)s,"
		" modified=%(now)s" + sheet + " WHERE name=%(n)s AND status='Pending'",
		{
			"n": name,
			"now": now,
			"by": approver,
			"batch": batch_id,
			"adopt": adopted_conversation,
			"edited": int(edited),
			"tok": apply_token,
			"req": _seal.json_text(apply_request),
		},
	)
	return rowcount() == 1


def release_sheet(name: str, token: str, errors: dict) -> bool:
	"""``Executing -> Pending`` for a sheet apply that rolled back cleanly (a CAS on
	its token): the claim's decider and times cleared, ``errors`` stored, the
	decisions (``apply_request``) kept. True iff released."""
	from jarvis.chat.pending_actions import _seal

	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET status='Pending', decided_by=NULL, decided_at=NULL,"
		" executing_at=NULL, apply_token=NULL, apply_errors=%(e)s, modified=%(now)s"
		" WHERE name=%(n)s AND kind=%(k)s AND status='Executing' AND IFNULL(apply_token, '')=%(t)s",
		{
			"n": name,
			"k": SHEET,
			"t": token or "",
			"e": _seal.json_text(errors),
			"now": frappe.utils.now_datetime(),
		},
	)
	return rowcount() == 1


def restamp_apply(name: str, token: str) -> bool:
	"""The apply job started: ``executing_at`` = now, while the claim is still its own."""
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET executing_at=%(now)s WHERE name=%(n)s"
		" AND status='Executing' AND apply_token=%(t)s",
		{"n": name, "t": token, "now": frappe.utils.now_datetime()},
	)
	return rowcount() == 1


def request_stop(name: str) -> bool:
	"""A Stop / archive / delete while the sheet is being applied: its job ends
	without a resume (a bounce discards it)."""
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET stop_requested=1, modified=%(now)s"
		" WHERE name=%(n)s AND kind=%(k)s AND status='Executing'",
		{"n": name, "k": SHEET, "now": frappe.utils.now_datetime()},
	)
	return rowcount() == 1


def claim_settled(names) -> list[str]:
	"""The ``settled`` compare-and-set: the names THIS call flipped. A continuation
	runs it inside the transaction that commits its effect (D5) and acts only for
	what it returns, so a settlement's effect happens at most once."""
	won = []
	now = frappe.utils.now_datetime()
	for name in names:
		frappe.db.sql(
			"UPDATE `tabJarvis Pending Action` SET settled=1, settled_at=%(now)s, modified=%(now)s"
			" WHERE name=%(n)s AND settled=0",
			{"n": name, "now": now},
		)
		if rowcount() == 1:
			won.append(name)
	return won


def null_sealed(names) -> None:
	"""Drop both envelopes once a row is settled (the seal is not an audit record)."""
	if names:
		frappe.db.sql(
			"UPDATE `tabJarvis Pending Action` SET sealed_call=NULL, sealed_settlement=NULL"
			" WHERE name IN %(n)s AND settled=1",
			{"n": tuple(names)},
		)


def bump_settle_attempts(name: str) -> None:
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET settle_attempts=settle_attempts+1 WHERE name=%(n)s",
		{"n": name},
	)


def waiters(name: str) -> list[frappe._dict]:
	return frappe.db.sql(
		"SELECT name, conversation, role, resume_state, resume_attempts FROM `tabJarvis Pending Action Waiter`"
		" WHERE parent=%(p)s AND parenttype='Jarvis Pending Action' ORDER BY idx",
		{"p": name},
		as_dict=True,
	)
