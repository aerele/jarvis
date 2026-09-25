"""Apply a File Box sheet (T3, S2). ``apply`` checks everything, then claims the sheet
and enqueues ``run_apply`` (RQ ``long``): every record written as the dropper in ONE
transaction, all or nothing, then one resume. Every exit is a CAS on the apply token.

Ops: one site-wide lock runs one apply at a time (a waiting job holds a long worker
up to ``APPLY_LOCK_WAIT_S``, then hands the sheet back: busy, apply again); the
transaction holds its naming-series rows until it commits."""

from __future__ import annotations

import pickle
from collections import deque
from contextlib import contextmanager

import frappe

from jarvis._session import authenticated_user, impersonate
from jarvis.chat import filebox_skills, held_edit, held_parties, held_sheets, held_writes
from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._execute import (
	_ARMED,
	_BUSY,
	_IDENTITY,
	_NOT_FOUND,
	_PROVENANCE,
	_refusal,
	_stale_code,
	authorize,
	handled,
	value_free,
)
from jarvis.chat.pending_actions._settle import settle
from jarvis.chat.pending_actions._store import (
	CONV,
	DISCARDED,
	EXECUTED,
	EXECUTING,
	FAILED,
	PENDING,
	REASON_TEXT,
	SHEET,
	TERMINAL,
	_terminal_update,
	apply_ready,
	claim,
	get_row,
	lock_conversation,
	release_sheet,
	restamp_apply,
	rowcount,
)
from jarvis.exceptions import JarvisError
from jarvis.permissions import is_valid_unattended_owner

AR = "Jarvis Approval Request"
JOB = "jarvis.chat.pending_actions._sheet.run_apply"
JOB_TIMEOUT_S = 1500
APPLY_LOCK_WAIT_S = 30  # another sheet's apply, then busy (never a worker parked for minutes)
RECORD_LOCK_WAIT_S = 30
LEGACY_LOCK_WAIT_S = 10  # a board click: busy rather than a long wait
REAP_AFTER_S = 120
ANSWER_CAP = 1000
ERROR_CAP = 300
PROGRESS_EVERY = 10
PROGRESS_TTL_S = 1800
_ALIVE = frozenset({"queued", "started", "deferred", "scheduled"})
_ACTIONS = {"create": ("create", "edit", "use_existing", "skip"), "update": ("apply", "edit", "skip")}
_DEFAULT = {"create": "create", "update": "apply"}
_KNOWN = (JarvisError, frappe.PermissionError, frappe.ValidationError, frappe.DuplicateEntryError)
_ABORT = (frappe.QueryDeadlockError, frappe.QueryTimeoutError)
_QUEUES = ("before_commit", "after_commit", "after_rollback")

_CHANGED = ("changed", "This sheet changed since you opened it: reload it and apply again.")
_COLLECTING = ("collecting", "This sheet is still being listed: apply it once the run pauses.")
_INVALID = "Some records or answers need attention: nothing was applied."
_BUSY_TEXT = "These records are busy: nothing was created. Apply again in a moment."
_CONFLICT_TEXT = "The apply hit a database conflict: nothing was created. Apply again."
_CRASHED_TEXT = "The apply hit an unexpected error: nothing was created. Apply again."
_QUESTIONS_TEXT = "The questions on this sheet changed: nothing was created. Reload it and apply again."
_IDENTITY_TEXT = "This can no longer run as the person who dropped the file: nothing was created."
INTERRUPTED_TEXT = "The apply was interrupted: nothing was created. Apply again."


class LockBusy(Exception):
	"""A record lock stayed taken past its wait."""


class _Failed(Exception):
	"""One record's error (the message is for the approver)."""

	def __init__(self, message: str):
		super().__init__(message)
		self.message = message


class _Changed(Exception):
	"""The linked questions moved under the apply (``errors``: why)."""

	def __init__(self, errors: dict | None = None):
		super().__init__()
		self.errors = errors or {"sheet": _QUESTIONS_TEXT}


# --------------------------------------------------------------------------- #
# Locks (MariaDB user locks: session-wide, untouched by commit / rollback)
# --------------------------------------------------------------------------- #
def _lock_name(key: str) -> str:
	return "jarvis:sheet_rec:" + _seal.open_key("sheet-lock", key)[:40]


def _apply_lock() -> str:
	return "jarvis:sheet_apply:" + _seal.open_key("sheet-lock", "*")[:16]


def lock_keys(items) -> list[str]:
	"""The identity locks of the creates among ``items`` (sheet records or held
	items): one record takes the same locks on either path."""
	keys = set()
	for item in items:
		if item.get("op") != "create" or not item.get("doctype"):
			continue
		doctype, values = item["doctype"], item.get("values") or {}
		party = held_parties.party_of({**item, "values": values})
		if party["gstin"]:
			keys.add(f"gstin:{doctype}:{party['gstin']}")
		title = held_parties.norm_name(held_sheets.deterministic_name(doctype, values) or party["title"])
		if title:
			keys.add(f"name:{doctype}:{title}")
	return sorted(keys)


@contextmanager
def record_locks(keys, *, wait: float | None = None, apply_lock: bool = False):
	"""Hold ``keys``' locks (the site's apply lock first when asked), in sorted
	order; ``LockBusy`` when one stays taken. Always released."""
	wait = RECORD_LOCK_WAIT_S if wait is None else wait
	names = sorted({_lock_name(k) for k in keys})
	if apply_lock:
		names.insert(0, _apply_lock())
	held = []
	try:
		for i, name in enumerate(names):
			timeout = APPLY_LOCK_WAIT_S if apply_lock and i == 0 else wait
			try:
				got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (name, timeout))[0][0]
			except Exception:
				got = None  # a user-lock deadlock: busy
			if got != 1:
				raise LockBusy(name)
			held.append(name)
		yield
	finally:
		for name in reversed(held):
			try:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))
			except Exception:
				pass


# --------------------------------------------------------------------------- #
# Identity and the decisions (the preflight)
# --------------------------------------------------------------------------- #
def _runnable(exec_user: str, approver: str | None) -> bool:
	return bool(frappe.db.get_value("User", exec_user, "enabled")) and (
		approver == exec_user or is_valid_unattended_owner(exec_user)
	)


def _identity_refusal(row, approver: str) -> dict | None:
	"""Held's identity preflight: the records are created as the dropper
	(``exec_user``), who must still own the live conversation."""
	from jarvis.chat.approvals_api import _may_act_on

	conv = (
		frappe.db.get_value(CONV, row.conversation, ["owner", "skip_confirmation"], as_dict=True)
		if row.conversation
		else None
	)
	if not conv or not _may_act_on(row.conversation):
		return _refusal(*_NOT_FOUND)
	if conv.skip_confirmation:
		return _refusal(*_ARMED)
	if not (conv.owner == row.exec_user == row.owner_user) or not _runnable(row.exec_user, approver):
		return _refusal(*_IDENTITY)
	return None


def _questions(row, *, lock: bool = False) -> dict:
	"""The sheet's Pending questions of its own conversation, by name."""
	rows = frappe.db.sql(
		"SELECT name, title, options, routing, conversation FROM `tabJarvis Approval Request`"
		" WHERE sheet=%(s)s AND conversation=%(c)s AND status='Pending' ORDER BY creation, name"
		+ (" FOR UPDATE" if lock else ""),
		{"s": row.name, "c": row.conversation or ""},
		as_dict=True,
	)
	return {r.name: r for r in rows}


def _answers(row, raw, errors: dict) -> tuple[dict, bool]:
	"""``(answers, skip_all)``: keyed by EXACTLY the sheet's Pending questions, each
	checked against its own options (a routing one: still eligible now)."""
	questions = _questions(row)
	given = raw if isinstance(raw, dict) else {}
	for key in set(map(str, given)) - set(questions):
		errors[key] = "Not a question on this sheet."
	out, skip_all = {}, False
	for name, q in questions.items():
		answer = given.get(name)
		text = str(answer.get("text") or "").strip() if isinstance(answer, dict) else ""
		if not text:
			errors[name] = "Answer this question."
			continue
		if len(text) > ANSWER_CAP:
			errors[name] = f"Keep the answer under {ANSWER_CAP} characters."
			continue
		approve = 1 if q.routing else int(bool(frappe.utils.cint(answer.get("approve", 1))))
		if q.routing:
			problem = _routing_error(q, text)
			if problem:
				errors[name] = problem
				continue
			skip_all = skip_all or text == filebox_skills.SKIP
		out[name] = {"text": text, "approve": approve}
	return out, skip_all


def _routing_error(q, text: str) -> str:
	"""Why ``text`` can't answer the routing question ``q`` now ('' when it can)."""
	try:
		filebox_skills.validate_answer(q, text)
	except frappe.ValidationError as e:
		frappe.clear_messages()
		return frappe.utils.strip_html(str(e)) or "Choose one of the offered answers."
	return ""


def _entries(records: list[dict], raw, errors: dict) -> dict:
	"""``{index: {action, values?, existing?}}`` for every record (create / apply by
	default)."""
	given = {str(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}
	for key in given:
		if not (key.isascii() and key.isdigit() and int(key) < len(records)):  # never "²" / "١"
			errors[key] = "Unknown record."
	out = {}
	for i, record in enumerate(records):
		d = given.get(str(i)) if isinstance(given.get(str(i)), dict) else {}
		action = str(d.get("action") or _DEFAULT[record["op"]])
		entry = {"action": action}
		if action not in _ACTIONS[record["op"]]:
			errors[str(i)] = "Choose " + ", ".join(_ACTIONS[record["op"]]).replace("_", " ") + "."
		elif action == "edit":
			if isinstance(d.get("values"), dict):
				entry["values"] = d["values"]
			else:
				errors[str(i)] = "Edit needs the values to set."
		elif action == "use_existing":
			entry["existing"] = str(d.get("existing") or "").strip()
		out[str(i)] = entry
	return out


def _label(record: dict, error: dict) -> str:
	try:
		label = held_writes.needs_input_label({**error, "doctype": record["doctype"]})
	except Exception:
		label = error.get("fieldname") or ""
	return f"{label}: {error['message']}" if label else str(error["message"])


def _merged(records: list[dict], plan: dict, exec_user: str) -> tuple[list[dict], dict]:
	"""The records with the approver's edits (the ``held_edit`` rules, against
	``exec_user``'s access: the records are created as them), their dependencies
	recomputed from the final values. ``(records, {index: error})``."""
	merged, errors = [dict(r) for r in records], {}
	for key, entry in (plan.get("records") or {}).items():
		if entry.get("action") != "edit":
			continue
		i, found = int(key), []
		merged[i] = held_edit.patch_one(records, i, entry.get("values") or {}, exec_user, found)
		if found:
			errors[key] = _label(records[i], found[0])
	held_sheets.link_deps(merged, before=records)
	return merged, errors


def _kinds(plan: dict, count: int) -> list[str]:
	"""Per record: ``skip`` / ``use_existing`` / ``write``."""
	if plan.get("skip_all"):
		return ["skip"] * count
	out = []
	for i in range(count):
		action = (plan.get("records") or {}).get(str(i), {}).get("action")
		out.append(action if action in ("skip", "use_existing") else "write")
	return out


def _cascade(records: list[dict], kinds: list[str]) -> set[int]:
	"""Records to write that need a skipped one, transitively (a used existing
	record needs nothing)."""
	dependents: dict = {}
	for i, r in enumerate(records):
		for d in r["depends_on"]:
			dependents.setdefault(d, []).append(i)
	out, stack = set(), [i for i, k in enumerate(kinds) if k == "skip"]
	while stack:
		for j in dependents.get(stack.pop(), ()):
			if j not in out and kinds[j] == "write":
				out.add(j)
				stack.append(j)
	return out


def _still_missing(record: dict, entries: list[dict]) -> list[dict]:
	out = []
	for e in entries:
		value = record["values"].get(e.get("parentfield") or e["fieldname"])
		if e.get("parentfield"):
			rows = value if isinstance(value, list) else []
			idx = int(e.get("idx") or 0)
			value = (
				rows[idx - 1].get(e["fieldname"])
				if 0 < idx <= len(rows) and isinstance(rows[idx - 1], dict)
				else None
			)
		if value in (None, "", []):
			out.append(e)
	return out


def _record_errors(row, records: list[dict], merged: list[dict], plan: dict, errors: dict) -> None:
	"""Per record to write (never one a skip cascades to): a writable doctype
	(``_denied`` on the final values), a readable existing target, and every flagged
	record fixed, filled, used or skipped."""
	fix = held_sheets.json_dict(row.needs_fix)
	missing: dict = {}
	for e in held_writes.parse_needs_input(row.needs_input):
		missing.setdefault(e.get("doc_index"), []).append(e)
	kinds = _kinds(plan, len(records))
	cascade = _cascade(merged, kinds)
	for i, (record, kind) in enumerate(zip(merged, kinds, strict=True)):
		key = str(i)
		if key in errors or kind == "skip" or i in cascade:
			continue
		entry = plan["records"][key]
		denied = held_writes._denied(record["doctype"])
		if denied:
			errors[key] = denied["error"]["message"]
		elif kind == "use_existing":
			errors[key] = _existing_error(record, entry.get("existing"), row.exec_user)
		elif key in fix and entry["action"] != "edit":
			errors[key] = "This record needs a fix: edit it, use an existing one, or skip it."
		elif missing.get(i):
			left = missing[i] if entry["action"] != "edit" else _still_missing(record, missing[i])
			if left:
				labels = held_writes.missing_summary(held_writes.missing_labels(left))
				errors[key] = f"Fill {labels} first: use Edit."
		if not errors.get(key):
			errors.pop(key, None)


def _existing_error(record: dict, target: str | None, exec_user: str) -> str:
	doctype = record["doctype"]
	if not target:
		return "Pick the existing record to use."
	if not (
		frappe.db.exists(doctype, target)
		and frappe.has_permission(doctype, "read", doc=target, user=exec_user)
	):
		return f"That {doctype} was not found, or the person who dropped the file can't open it."
	return ""


def _plan(row, records: list[dict], decisions: dict) -> tuple[dict, dict, dict]:
	"""``(plan, record errors, answer errors)``: the validated decisions."""
	errors, answer_errors = {}, {}
	entries = _entries(records, decisions.get("records"), errors)
	answers, skip_all = _answers(row, decisions.get("answers"), answer_errors)
	plan = {"records": entries, "answers": answers, "skip_all": int(skip_all)}
	if not skip_all:
		merged, edit_errors = _merged(records, plan, row.exec_user)
		for key, message in edit_errors.items():
			errors.setdefault(key, message)
		_record_errors(row, records, merged, plan, errors)
	return plan, errors, answer_errors


# --------------------------------------------------------------------------- #
# apply / discard (the endpoints' bodies)
# --------------------------------------------------------------------------- #
def _decisions(raw) -> dict | None:
	if isinstance(raw, str):
		try:
			raw = frappe.parse_json(raw)
		except ValueError:
			return None
	return raw if isinstance(raw, dict) else None


def _actionable(name, approver: str) -> tuple:
	"""``(row, refusal)``: a sealed sheet ``approver`` may act on."""
	name = str(name or "").strip()
	row = get_row(name) if name else None
	if not authorize(row, approver, SHEET, acting=False)[0]:
		return None, _refusal(*_NOT_FOUND)
	if row.collecting:
		return None, _refusal(*_COLLECTING)
	return row, None


def _lock_sheet(row) -> tuple:
	"""Commit, then the conversation lock and the row lock (no wait): ``(row, refusal)``."""
	frappe.db.commit()
	lock_conversation(row.conversation)
	try:
		locked = get_row(row.name, lock="nowait")
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		frappe.db.rollback()
		return None, _refusal(*_BUSY)
	if not locked:
		frappe.db.rollback()
		return None, _refusal(*_NOT_FOUND)
	if locked.status != PENDING:
		frappe.db.rollback()
		if locked.status in TERMINAL and not locked.settled:
			_settle(locked.name)
		return None, handled(locked, "This sheet was already handled.")
	refused = _identity_refusal(locked, authenticated_user())
	if refused:
		frappe.db.rollback()
		return None, refused
	return locked, None


def apply(name, decisions) -> dict:
	"""``apply_sheet``: every check first (nothing claimed on a refusal), then the
	claim and the job. ``{ok, applying}``."""
	approver = authenticated_user()
	row, refused = _actionable(name, approver)
	if refused:
		return refused
	parsed = _decisions(decisions)
	if parsed is None:
		return _refusal("invalid", "decisions must be a JSON object.")
	return value_free(_apply_locked, "sheet_apply_crashed", row, approver, parsed)


def _apply_locked(row, approver: str, decisions: dict) -> dict:
	locked, refused = _lock_sheet(row)
	if refused:
		return refused
	try:
		records = _seal.unseal_call(locked)["args"]["records"]
	except _seal.SealError as e:
		return _fail_sealed(locked, e, decided_by=approver)
	if (
		decisions.get("expected_card_sha256") != _seal.card_sha256(locked)
		or frappe.utils.cint(decisions.get("record_count")) != len(records)
		or len(records) != frappe.utils.cint(locked.record_count)
	):
		frappe.db.rollback()
		return _refusal(*_CHANGED)
	plan, errors, answer_errors = _plan(locked, records, decisions)
	if errors or answer_errors:
		frappe.db.rollback()
		return _refusal("invalid", _INVALID, errors=errors, answer_errors=answer_errors)
	token = frappe.generate_hash(length=24)
	if not claim(locked.name, approver, apply_token=token, apply_request={**plan, "approver": approver}):
		frappe.db.rollback()
		return _refusal(*_BUSY)
	frappe.db.commit()
	try:
		frappe.enqueue(JOB, queue="long", timeout=JOB_TIMEOUT_S, job_id=token, name=locked.name, token=token)
	except Exception:
		frappe.log_error(title="jarvis.file_box.sheet_apply_enqueue_failed", message=frappe.get_traceback())
		_hand_back(locked.name, token, {"sheet": INTERRUPTED_TEXT})
		return _refusal("unavailable", "The apply couldn't start: nothing was created. Try again.")
	_progress(locked, approver, 0, len(records), first=True)
	return {"ok": True, "applying": True, "reason_code": "applying", "pa_status": EXECUTING}


def discard(name) -> dict:
	"""``discard_sheet``: Skip all. Discarded (every record skipped, its questions
	closed at settle) and the run resumed once, "skipped"."""
	approver = authenticated_user()
	row, refused = _actionable(name, approver)
	if refused:
		return refused
	return value_free(_discard_locked, "sheet_discard_crashed", row, approver)


def _discard_locked(row, approver: str) -> dict:
	locked, refused = _lock_sheet(row)
	if refused:
		return refused
	shown = held_sheets.json_dict(locked.card).get("records") or []
	outcome = {
		"records": [
			{
				"index": r.get("index"),
				"doctype": r.get("doctype"),
				"op": r.get("op"),
				"title": r.get("title") or r.get("name") or "",
				"action": "skipped",
			}
			for r in shown
		],
		"answers": [],
		"keys": [],
	}
	_terminal_update(
		locked.name, [PENDING], DISCARDED, reason_code="discarded", decided_by=approver, sheet_outcome=outcome
	)
	frappe.db.commit()
	_settle(locked.name)
	return {"ok": True, "reason_code": "discarded", "pa_status": DISCARDED}


def _fail_sealed(row, e: _seal.SealError, *, decided_by: str | None = None, token: str | None = None) -> dict:
	"""A sheet whose seal no longer verifies ends Failed, nothing applied."""
	cols = {"decided_by": decided_by} if decided_by else {}
	moved = _terminal_update(row.name, [row.status], FAILED, reason_code=e.reason_code, token=token, **cols)
	frappe.log_error(
		title=f"jarvis.file_box.sheet_{e.reason_code}", message=f"{row.name}: failed binding {e.binding}"
	)
	frappe.db.commit()
	if moved:
		_settle(row.name)
	return _refusal(e.reason_code, REASON_TEXT[e.reason_code], pa_status=FAILED, outcome="failed")


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
def _mine(row, token: str) -> bool:
	return bool(row) and row.status == EXECUTING and (row.apply_token or "") == (token or "")


def run_apply(name: str, token: str) -> None:
	"""The background apply (RQ ``long``, ``job_id`` = ``token``)."""
	row = get_row(name)
	if not _mine(row, token) or not restamp_apply(name, token):
		frappe.db.rollback()
		frappe.log_error(
			title="jarvis.file_box.sheet_apply_stale", message=f"{name}: this job's claim is gone"
		)
		frappe.db.commit()
		return
	frappe.db.commit()
	try:
		_run(row, token)
	except Exception:
		_crashed(row, token, frappe.get_traceback())
	finally:
		_clear_progress(name)


def _run(row, token: str) -> None:
	plan = held_sheets.json_dict(row.apply_request)
	try:
		records = _seal.unseal_call(row)["args"]["records"]
	except _seal.SealError as e:
		_fail_sealed(row, e, token=token)
		return
	if not _runnable(row.exec_user, plan.get("approver")):
		_bounce(row, token, {"sheet": _IDENTITY_TEXT})
		return
	# The whole file skipped: nothing is written, so no edit is checked.
	merged, errors = (records, {}) if plan.get("skip_all") else _merged(records, plan, row.exec_user)
	if errors:
		_bounce(row, token, errors)
		return
	kinds = _kinds(plan, len(records))
	keys = lock_keys([r for r, k in zip([*records, *merged], kinds * 2, strict=True) if k == "write"])
	try:
		with record_locks(keys, apply_lock=True):
			frappe.db.commit()  # a fresh snapshot: whatever applied under these locks before us
			_walk(row, token, plan, records, merged, kinds)
	except LockBusy:
		_bounce(row, token, {"sheet": _BUSY_TEXT})
	except _ABORT:
		_bounce(row, token, {"sheet": _CONFLICT_TEXT})


@contextmanager
def _dispatching():
	"""As a tool body runs: hooks can't re-enter a gate (the S6 nesting guard)."""
	frappe.local.jarvis_dispatch_depth = getattr(frappe.local, "jarvis_dispatch_depth", 0) + 1
	try:
		yield
	finally:
		frappe.local.jarvis_dispatch_depth -= 1
		frappe.clear_messages()


def _walk(row, token: str, plan: dict, records: list[dict], merged: list[dict], kinds: list[str]) -> None:
	"""ONE ordered pass as ``exec_user``: every record's outcome, or every error."""
	cascade = _cascade(merged, kinds)
	outcome: list = [None] * len(merged)
	failures, roots, crashes, remap = {}, {}, [], {}
	approver, total, done = plan.get("approver"), len(merged), 0
	with impersonate(row.exec_user), _dispatching():
		for i in held_sheets.order(merged, range(total)):
			record = merged[i]
			if kinds[i] == "skip" or i in cascade:
				outcome[i] = _entry(i, record, "skipped", cascade=i in cascade)
				continue
			root = next((roots[d] for d in record["depends_on"] if d in roots), None)
			if root is not None:
				roots[i] = root
				failures[str(i)] = f"Blocked by #{root + 1}: it needs that record."
				continue
			try:
				outcome[i] = _one(i, record, records[i], kinds[i], plan["records"][str(i)], remap, crashes)
			except _Failed as e:
				roots[i] = i
				failures[str(i)] = e.message
			done += 1
			if done % PROGRESS_EVERY == 0:
				_progress(row, approver, done, total)
	if failures:
		_bounce(
			row, token, failures, failed=[merged[int(k)] for k, v in roots.items() if k == v], crashes=crashes
		)
		return
	_finish(row, token, plan, merged, outcome)


def _entry(
	i: int, record: dict, action: str, name: str = "", *, auto: bool = False, cascade: bool = False
) -> dict:
	"""One record's outcome: what happened and the name it ended with, never its values."""
	entry = {
		"index": i,
		"doctype": record["doctype"],
		"op": record["op"],
		"title": held_sheets.name_of(record),
		"action": action,
	}
	if name:
		entry["name"] = name
	if auto:
		entry["auto"] = 1
	if cascade:
		entry["cascade"] = 1
	return entry


def _learn(record: dict, original: dict, name: str, remap: dict) -> None:
	held_sheets.learn(record, name, remap)
	held_sheets.learn(original, name, remap)


def _one(i: int, record: dict, original: dict, kind: str, entry: dict, remap: dict, crashes: list) -> dict:
	values = held_sheets.remapped(record, remap)
	if record["op"] == "update":
		return _update(i, record, values, crashes)
	if kind == "use_existing":
		target = entry["existing"]
		if not (
			frappe.db.exists(record["doctype"], target)
			and frappe.has_permission(record["doctype"], "read", doc=target)
		):
			raise _Failed(
				f"That {record['doctype']} was not found, or the person who dropped the file can't open it."
			)
		_learn(record, original, target, remap)
		return _entry(i, record, "existing", target)
	found = _resolve(record, values)
	if found:
		_learn(record, original, found, remap)
		return _entry(i, record, "existing", found, auto=True)
	return _create(i, record, original, values, remap, crashes)


def _savepoint() -> tuple[str, dict]:
	queues = {q: tuple(getattr(frappe.db, q)._functions) for q in _QUEUES if hasattr(frappe.db, q)}
	sp = "jsa_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(sp)
	return sp, queues


def _undo(sp: str, queues: dict) -> None:
	"""Back to the savepoint, and nothing that record queued runs at the commit."""
	frappe.db.rollback(save_point=sp)
	for q, functions in queues.items():
		getattr(frappe.db, q)._functions = deque(functions)
	frappe.clear_messages()


def _message(e: Exception) -> str:
	from jarvis import api

	if isinstance(e, frappe.DuplicateEntryError):
		return "A record with this name already exists."
	text = frappe.utils.strip_html(api._preview_error(e)["error"]["message"])
	return filebox_skills._safe(text, ERROR_CAP) or "This record can't be saved."


def _guarded(fn, crashes: list, *, duplicate=None):
	"""``fn()`` in its own savepoint. A deadlock aborts the pass; a duplicate goes to
	``duplicate(e)`` when given; any other error is the record's (an unexpected one
	logged after the pass)."""
	sp, queues = _savepoint()
	try:
		return fn()
	except _ABORT:
		raise
	except Exception as e:
		tb = "" if isinstance(e, _KNOWN) else frappe.get_traceback()
		_undo(sp, queues)
		if duplicate and isinstance(e, frappe.DuplicateEntryError):
			return duplicate(e)
		if tb:
			crashes.append(tb)
			raise _Failed("This record hit an unexpected error.") from None
		raise _Failed(_message(e)) from None


def _update(i: int, record: dict, values: dict, crashes: list) -> dict:
	from jarvis.tools.update_doc import _update_one

	stale = _stale_code([record["snapshot"]]) if record.get("snapshot") else None
	if stale:
		raise _Failed(REASON_TEXT[stale])
	_guarded(lambda: _update_one(record["doctype"], record["name"], values), crashes)
	return _entry(i, record, "updated", record["name"])


def _create(i: int, record: dict, original: dict, values: dict, remap: dict, crashes: list) -> dict:
	from jarvis.tools.create_doc import _insert_one, _validate_create_args

	def insert():
		_validate_create_args(record["doctype"], values)
		return _insert_one(record["doctype"], values).name, False

	def again(e):
		found = _resolve(record, values)  # created since the check: re-resolve, never assume
		if not found:
			raise _Failed(_message(e))
		return found, True

	name, auto = _guarded(insert, crashes, duplicate=again)
	_learn(record, original, name, remap)
	return _entry(i, record, "existing" if auto else "created", name, auto=auto)


def _existing(record: dict, values: dict) -> str | None:
	"""A record with this create's identity that exists now: an Address by its spot,
	else its name (field / prompt / supplier-name naming), tax id, or a party's name.
	The running user's own match first; one they can't read only after (``_resolve``
	makes it the record's error), so a readable twin is used, never refused."""
	doctype = record["doctype"]
	if doctype == "Address":
		return _existing_address(values)
	name = held_sheets.deterministic_name(doctype, values)
	found = frappe.db.exists(doctype, name) if name else None
	if found:
		return found
	if not held_parties.is_party(doctype):
		return None
	party = held_parties.party_of({"doctype": doctype, "values": values})
	for hidden in (False, True):
		seen = held_parties.find_existing(
			doctype, title=party["title"], gstin=party["gstin"], limit=1, ignore_permissions=hidden
		)
		if seen:
			return seen[0]["name"]
	return None


def _existing_address(values: dict) -> str | None:
	"""The party's address of this type at this spot (street + pincode, its city
	without one) and, when the site has the field, this GSTIN: never the GSTIN alone."""
	links = [
		r
		for r in values.get("links") or []
		if isinstance(r, dict) and r.get("link_doctype") and r.get("link_name")
	]
	if not (links and held_parties.norm_name(values.get("address_line1"))):
		return None
	has_gstin = frappe.get_meta("Address").has_field("gstin")
	gstin = held_parties.norm_gstin(values.get("gstin")) if has_gstin else ""
	# The type in SQL too (ci collation), so LIMIT 200 counts only this type.
	rows = frappe.db.sql(
		"SELECT a.name, a.address_type, a.address_line1, a.pincode, a.city"
		+ (", a.gstin" if has_gstin else "")
		+ " FROM `tabAddress` a JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'"
		" WHERE dl.link_doctype=%(dt)s AND dl.link_name=%(dn)s AND TRIM(IFNULL(a.address_type, ''))=%(type)s"
		" ORDER BY a.creation LIMIT 200",
		{
			"dt": links[0]["link_doctype"],
			"dn": links[0]["link_name"],
			"type": str(values.get("address_type") or "").strip(),
		},
		as_dict=True,
	)
	kind, spot = _type_key(values.get("address_type")), held_sheets.address_spot(values)
	return next(
		(
			r.name
			for r in rows
			if _type_key(r.address_type) == kind
			and held_sheets.address_spot(r) == spot
			and (not has_gstin or held_parties.norm_gstin(r.get("gstin")) == gstin)
		),
		None,
	)


def _type_key(value) -> str:
	"""An address type as the sheet keys it (``held_sheets._address_keys``): any case."""
	return str(value or "").strip().casefold()


def _resolve(record: dict, values: dict) -> str | None:
	"""The existing record for this create, read as the running user (``exec_user``);
	one that exists but they can't open is the record's error."""
	name = _existing(record, values)
	if name and not frappe.has_permission(record["doctype"], "read", doc=name):
		raise _Failed(
			f"This {record['doctype']} exists already, but the person who dropped the file can't open it."
		)
	return name


# --------------------------------------------------------------------------- #
# Exits: every one a compare-and-set on the token
# --------------------------------------------------------------------------- #
def _rollback() -> None:
	"""The whole pass, and what it queued (the execute path's full rollback)."""
	frappe.db.rollback()
	frappe.local._webhook_queue = []
	if hasattr(frappe.local, "_link_count"):
		del frappe.local._link_count
	frappe.clear_messages()


def _write_answers(row, plan: dict) -> list[dict]:
	"""Each linked question answered (a conditional UPDATE: still Pending and on this
	sheet), decided by the approver; a routing answer's effect with it. No commit."""
	questions = _questions(row, lock=True)
	answers = plan.get("answers") or {}
	if set(questions) != set(answers):
		raise _Changed
	now, out = frappe.utils.now_datetime(), []
	for name, q in questions.items():
		answer = answers[name]
		problem = _routing_error(q, answer["text"]) if q.routing else ""
		if problem:
			raise _Changed({name: problem})
		status = "Approved" if answer["approve"] else "Rejected"
		frappe.db.sql(
			"UPDATE `tabJarvis Approval Request` SET status=%(st)s, decision=%(d)s, decided_by=%(by)s,"
			" decided_at=%(now)s, modified=%(now)s WHERE name=%(n)s AND status='Pending' AND sheet=%(s)s"
			" AND conversation=%(c)s",
			{
				"st": status,
				"d": answer["text"],
				"by": plan.get("approver"),
				"now": now,
				"n": name,
				"s": row.name,
				"c": row.conversation,
			},
		)
		if rowcount() != 1:
			raise _Changed
		if q.routing:
			filebox_skills.sheet_answer(row.conversation, q.routing, answer["text"])
		out.append(
			{
				"name": name,
				"title": q.title or "",
				"status": status,
				"answer": answer["text"],
				"routing": q.routing or "",
			}
		)
	return out


def _recent_keys(owner: str, merged: list[dict], outcome: list[dict]) -> list[str]:
	"""HMACs of what this apply CREATED, for ``held_writes._find_match``'s "recent"
	(never a skipped or existing record)."""
	keys = set()
	for entry in outcome:
		if entry["action"] != "created":
			continue
		record = merged[entry["index"]]
		party = held_parties.party_of(record)
		found = [f"gstin:{record['doctype']}:{party['gstin']}"] if party["gstin"] else []
		# The held key's name: its naming field (an Item's item_code), else its title.
		title = held_parties.norm_name(
			held_sheets.deterministic_name(record["doctype"], record["values"]) or party["title"]
		)
		if title:
			found.append(f"name:{record['doctype']}:{title}")
		keys.update(_seal.open_key(owner, k) for k in found or [held_parties.item_key(record)])
	return sorted(keys)


def _audit(row, entries, outcome: str) -> None:
	from jarvis import agent_audit

	for e in entries:
		agent_audit.record_write(
			actor=row.exec_user,
			tool="update_doc" if e.get("op") == "update" else "create_doc",
			args={"doctype": e["doctype"], "name": e.get("name") or ""},
			result=None,
			outcome=outcome,
			provenance=_PROVENANCE[SHEET],
			provenance_name=row.name,
		)


def _finish(row, token: str, plan: dict, merged: list[dict], outcome: list[dict]) -> None:
	"""Success: the records, the answers, ``sheet_outcome`` and the terminal state
	commit together (Discarded only when a routing answer skipped the whole file:
	answered questions resume as applied, even with every record skipped), then
	settle resumes the run once. A stop that landed meanwhile keeps the records,
	never resumes."""
	lock_conversation(row.conversation)  # Stop's order: the conversation, then the row
	fresh = get_row(row.name, lock="update")  # the latest stop_requested
	try:
		answers = _write_answers(row, plan)
	except _Changed as e:
		_bounce(row, token, e.errors)
		return
	written = [e for e in outcome if e["action"] in ("created", "updated")]
	skipped = bool(plan.get("skip_all"))
	if fresh and fresh.stop_requested:
		held_sheets.drop_waiters(row.name)
	first = next((e for e in outcome if e["action"] == "created"), None) or {}
	done = _terminal_update(
		row.name,
		[EXECUTING],
		DISCARDED if skipped else EXECUTED,
		reason_code="discarded" if skipped else None,
		token=token,
		result_doctype=first.get("doctype") or "",
		result_name=first.get("name") or "",
		sheet_outcome={
			"records": outcome,
			"answers": answers,
			"keys": _recent_keys(row.owner_user, merged, outcome),
		},
	)
	if not done:
		_lost(row.name)
		return
	_audit(row, written, "applied")
	frappe.db.commit()
	_progress(row, plan.get("approver"), len(outcome), len(outcome), state="applied")
	_settle(row.name)


def _settle(name: str) -> None:
	"""After the commit: a failed settle is the reconciler's to retry, never the apply's."""
	try:
		settle(name)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="jarvis.file_box.sheet_apply_settle_failed", message=f"{name}\n{frappe.get_traceback()}"
		)
		frappe.db.commit()


def _bounce(row, token: str, errors: dict, *, failed=(), crashes=()) -> None:
	"""Any error: nothing from this apply is kept; the sheet goes back to Pending
	with ``errors`` (its decisions kept)."""
	_rollback()
	_log_crashes(row.name, crashes)
	if not _hand_back(row.name, token, errors, failed=failed):
		_lost(row.name)
		return
	plan = held_sheets.json_dict(row.apply_request)
	_progress(row, plan.get("approver"), 0, frappe.utils.cint(row.record_count), state="returned")


def _hand_back(name: str, token: str, errors: dict, *, failed=()) -> str | None:
	"""Under the row lock, while the claim is still this apply's: Pending with
	``errors``, or Discarded when its run was stopped meanwhile (no resume). Commits.
	The new status, or None (the claim moved on)."""
	fresh = get_row(name, lock="update")  # the latest stop_requested
	if fresh and fresh.stop_requested:
		status = DISCARDED
		moved = _terminal_update(name, [EXECUTING], DISCARDED, reason_code="cancelled", token=token)
	else:
		status, moved = PENDING, release_sheet(name, token, errors)
	if not moved:
		frappe.db.rollback()
		return None
	if status == DISCARDED:
		held_sheets.drop_waiters(name)
	_audit(
		fresh,
		[{"doctype": r["doctype"], "op": r["op"], "name": r.get("name") or ""} for r in failed],
		"failed",
	)
	frappe.db.commit()
	if status == DISCARDED:
		_settle(name)
	return status


def _lost(name: str) -> None:
	frappe.db.rollback()
	frappe.log_error(
		title="jarvis.file_box.sheet_apply_lost",
		message=f"{name}: its claim moved on (an operator or the reaper); nothing from this apply was kept",
	)
	frappe.db.commit()


def _log_crashes(name: str, crashes) -> None:
	for tb in crashes:
		frappe.log_error(title="jarvis.file_box.sheet_apply_record_crashed", message=f"{name}\n{tb}")
	if crashes:
		frappe.db.commit()


def _crashed(row, token: str, tb: str) -> None:
	_rollback()
	frappe.log_error(title="jarvis.file_box.sheet_apply_crashed", message=f"{row.name}\n{tb}")
	frappe.db.commit()
	try:
		if not _hand_back(row.name, token, {"sheet": _CRASHED_TEXT}):
			_lost(row.name)
	except Exception:
		frappe.db.rollback()  # the reaper hands it back once the job is gone
	_alert(f"The apply of File Box sheet {row.name} crashed (Error Log jarvis.file_box.sheet_apply_crashed).")


def _alert(message: str) -> None:
	from jarvis.chat import held_sheet_seal

	held_sheet_seal.alert("sheet_apply_failed", message)


# --------------------------------------------------------------------------- #
# Progress (realtime + the File Box line)
# --------------------------------------------------------------------------- #
def _progress_key(name: str) -> str:
	return f"jarvis:sheet_apply_progress:{name}"


def progress(name: str) -> dict | None:
	"""``{done, total}`` of a running apply, when known."""
	try:
		value = frappe.cache.get_value(_progress_key(name), expires=True)
	except Exception:
		return None
	return value if isinstance(value, dict) else None


def _clear_progress(name: str) -> None:
	try:
		frappe.cache.delete_value(_progress_key(name))
	except Exception:
		pass


def _progress(
	row, approver: str | None, done: int, total: int, *, state: str = "applying", first: bool = False
) -> None:
	"""Best-effort: the File Box line reads it; the owner (and a different approver)
	get ``sheet:progress``. ``first`` (the request's 0/N) never overwrites the job's."""
	from jarvis.chat import events

	if state == "applying":
		value = pickle.dumps({"done": done, "total": total})
		try:
			key = frappe.cache.make_key(_progress_key(row.name))
			if not frappe.cache.set(key, value, ex=PROGRESS_TTL_S, nx=first):
				return
		except Exception:
			pass
	payload = {
		"kind": "sheet:progress",
		"name": row.name,
		"state": state,
		"done": done,
		"total": total,
		"conversation_id": row.conversation or "",
	}
	for user in sorted({row.owner_user, approver} - {None, ""}):
		try:
			events.publish_to_user(user, payload)
		except Exception:
			pass


# --------------------------------------------------------------------------- #
# The reaper (reconciler step): a sheet whose job is gone goes back to Pending
# --------------------------------------------------------------------------- #
_SCAN = 200


def _job_alive(token: str | None) -> bool | None:
	"""RQ's view of the apply job: None when it can't be asked."""
	if not token:
		return False
	from frappe.utils.background_jobs import get_job_status

	try:
		status = get_job_status(token)
	except Exception:
		return None
	status = getattr(status, "value", status)
	return bool(status) and str(status) in _ALIVE


def reap() -> list[str]:
	"""Executing sheets whose job is missing or ended without an exit (never a
	queued or running one): back to Pending, "interrupted: apply again" (nothing
	was kept: the apply commits in one go). Logged, and an admin alert."""
	if not apply_ready():
		return []
	cut = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-REAP_AFTER_S)
	rows = frappe.db.sql(
		"SELECT name, apply_token FROM `tabJarvis Pending Action` WHERE kind=%(k)s AND status='Executing'"
		" AND (executing_at IS NULL OR executing_at < %(c)s) ORDER BY executing_at LIMIT %(lim)s",
		{"k": SHEET, "c": cut, "lim": _SCAN},
		as_dict=True,
	)
	released = []
	for r in rows:
		if _job_alive(r.apply_token) is not False:
			continue
		frappe.db.commit()
		if _hand_back(r.name, r.apply_token or "", {"sheet": INTERRUPTED_TEXT}):
			released.append(r.name)
	if released:
		frappe.log_error(
			title="jarvis.file_box.sheet_apply_interrupted",
			message="Handed back to Pending (their apply job was gone; nothing was created): "
			+ ", ".join(released),
		)
		frappe.db.commit()
		_alert(f"{len(released)} File Box sheet apply job(s) went missing: " + ", ".join(released))
	return released
