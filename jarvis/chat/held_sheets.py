"""File Box sheets: one approval sheet per document (T3, S1).

While a File Box run works on a document, its master proposals and questions
collect on ONE sealed ``Jarvis Pending Action`` (kind ``file_box_sheet``) per
conversation instead of one held row each (``held_writes._classify`` calls in here):

- records (creates, and ``update_doc`` holds as ``op=update``) append under the
  conversation lock; the envelope is rebuilt only from a verified unseal and
  written by ``_store.reseal_sheet`` (a CAS on the old envelope);
- each append is dry-run in ONE sandbox after replaying the sheet records it links
  to (transitively, names remapped), so an Address of a held Supplier validates; a
  record's first failure goes back to the model, the second joins flagged
  (``needs_input`` / ``needs_fix``);
- the run's Approval Requests link to it (``sheet``): frozen, answered with it.

``open_key`` = HMAC(owner, "sheet:" + conversation) keeps one live sheet per
conversation, and ``filebox_sheet_count`` caps a document at two. The
``file_box_sheets`` switch gates new sheets only.

Sealing and ended sheets: ``held_sheet_seal``; the board: ``held_sheet_board``."""

from __future__ import annotations

import copy
import json
import re

import frappe
from frappe.model import table_fields

from jarvis.chat import filebox_skills, held_edit, held_parties, held_writes
from jarvis.chat.pending_actions import _seal
from jarvis.chat.pending_actions._store import (
	FAILED,
	PENDING,
	SHEET,
	_terminal_update,
	filebox_migrated,
	get_row,
	lock_conversation,
	reseal_sheet,
	rowcount,
)
from jarvis.exceptions import JarvisError

SHEET_TOOL = "file_box_sheet"
SHEET_MAX_RECORDS = 250
MAX_SHEETS = 2
FIX_CAP = 300
AR = "Jarvis Approval Request"
CONV = "Jarvis Conversation"
_WIKI_SOURCE = "File Box Wiki"
# Masters named after a field when the site's naming setting says so.
_BY_NAME = {
	"Supplier": ("supp_master_name", "Supplier Name", "supplier_name"),
	"Customer": ("cust_master_name", "Customer Name", "customer_name"),
}
_KNOWN = (JarvisError, frappe.PermissionError, frappe.ValidationError, frappe.DuplicateEntryError)
# Always the model's to fix: never joins a sheet flagged.
_HARD = (JarvisError, frappe.PermissionError, frappe.DoesNotExistError, frappe.DuplicateEntryError)
_LINKS = ("Link", "Dynamic Link")
_MANDATORY = re.compile(r"^\[[^\]]*\]:\s*(.+)$")  # frappe's "[Item, X]: stock_uom, ..."

_KEEP_GOING = (
	" Keep going now: resolve EVERY other master this document needs and propose the missing ones "
	"(batches of up to 20 are fastest), and file every question you need answered as a Jarvis "
	"Approval Request. Do NOT create the draft yet. When nothing else is missing, END your turn: the "
	"run resumes once the sheet is applied."
)
_ADDED = "Added to this document's approval sheet ({count} records so far); nothing was created."
_ALREADY = "Already on this document's approval sheet ({count} records so far); nothing was added."
_FLAGGED = " A human fills or fixes these on the sheet: {records}."
_QUESTION_NOTE = (
	"Filed on this document's approval sheet: a human answers it with the sheet, not on its own."
	+ _KEEP_GOING
)
_FIRST_TRY = (
	"Nothing was added to the approval sheet: {problems}. Fix them from the document (look up valid "
	"values first) and call again. If the document doesn't give them, call again unchanged: those "
	"records then join the sheet for a human to fill or fix."
)
_DRAFT_WAITS = (
	"Nothing was created: the masters and questions for this document are waiting on its approval "
	"sheet, so the draft can't be created yet. Make no more changes: END your turn now. The run "
	"resumes by itself once the sheet is applied; then create the draft."
)
_NEEDS_ATTENTION = (
	"This file already paused for a human twice. Stop and summarize: this file needs attention. Make "
	"no more changes and END your turn with a one-line summary."
)
_STOPPED = "This run was stopped. Make no more changes: END your turn now."
_STOPPED_NOTE = "(run stopped - no action taken)"
_FULL = (
	"This document's approval sheet is full ({cap} records): nothing was added. Stop and summarize: "
	"this file needs attention. END your turn with a one-line summary."
)
_BROKEN = (
	"This document's approval sheet failed an integrity check: nothing was added. Stop and summarize: "
	"this file needs attention. END your turn with a one-line summary."
)
_IDENTITY = (
	"This run can't add to the approval sheet as this user: nothing was added. Stop and summarize: "
	"this file needs attention. END your turn with a one-line summary."
)
_OTHER_QUESTION = (
	"That Approval Request belongs to another conversation: a File Box run may only change its own. "
	"Nothing was changed."
)
_TOO_MANY = (
	"too many records in one call ({count}); the max is {cap}. Split them into batches of {cap}: each "
	"batch joins this document's approval sheet."
)


class _Retry(Exception):
	"""The sheet moved between the read and the write: re-lock and re-read."""


class _NotOpened(Exception):
	"""park opened nothing: the run was stopped, or its questions went away."""


def _refuse(code: str, message: str) -> dict:
	return held_writes._refuse(code, message)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def enabled() -> bool:
	"""The ``file_box_sheets`` switch (off before its migrate)."""
	if not filebox_migrated():
		return False
	return bool(frappe.utils.cint(frappe.db.get_single_value("Jarvis Settings", "file_box_sheets")))


# What the gates read (never the envelope: ``get_row`` when appending).
_GATE_COLS = (
	"name, status, collecting, collecting_turn, conversation, owner_user, exec_user, summary,"
	" record_count, question_count, sheet_counts"
)


def live_sheet(conversation: str):
	"""The conversation's undecided sheet (Pending or Executing), or None: its gate columns."""
	if not filebox_migrated():
		return None
	rows = frappe.db.sql(
		f"SELECT {_GATE_COLS} FROM `tabJarvis Pending Action` WHERE conversation=%(c)s AND kind=%(k)s"
		" AND status IN ('Pending', 'Executing') ORDER BY creation DESC LIMIT 1",
		{"c": conversation, "k": SHEET},
		as_dict=True,
	)
	return rows[0] if rows else None


def paused(row) -> bool:
	"""Sealed (or being applied): the run waits for a human."""
	return row.status != PENDING or not row.get("collecting")


def _sheet_count(conversation: str) -> int:
	return frappe.utils.cint(frappe.db.get_value(CONV, conversation, "filebox_sheet_count"))


def _archived(conversation: str) -> bool:
	"""The run-cancel signal an archive sets expires in 120s; the conversation's own
	status never does, so a later write still refuses."""
	return frappe.db.get_value(CONV, conversation, "status") == "Archived"


def live_turn(conversation: str) -> str:
	"""The in-flight turn (``collecting_turn``); "" without one (the legacy path)."""
	from jarvis.chat.turn_state import NONTERMINAL_STATES

	running = [s for s in NONTERMINAL_STATES if s != "queued"]
	return (
		frappe.db.get_value(
			"Jarvis Chat Turn",
			{"conversation": conversation, "state": ["in", running]},
			"name",
			order_by="creation desc",
		)
		or ""
	)


def user_stopped(conversation: str, turn: str | None) -> bool:
	"""A user Stop / archive: the run-cancel signal, or ``turn``'s cancel intent (still
	set at the legacy end, which runs before admission clears it)."""
	from jarvis.chat.turn_message_binding import is_run_cancel_requested

	if is_run_cancel_requested(conversation):
		return True
	return bool(turn and frappe.utils.cint(frappe.db.get_value("Jarvis Chat Turn", turn, "cancel_requested")))


def _identity_ok(conversation: str, owner: str, sheet) -> bool:
	"""Every sheet write runs as the conversation owner, the sheet's owner and exec_user."""
	user = frappe.session.user
	if user == owner and (not sheet or sheet.exec_user == owner == sheet.owner_user):
		return True
	frappe.log_error(
		title="jarvis.file_box.sheet_identity_refused",
		message=f"{conversation}: session {user}, owner {owner}, sheet {sheet.name if sheet else '-'}",
	)
	return False


# --------------------------------------------------------------------------- #
# Rule (a) with sheets (held_writes._classify, under the conversation lock)
# --------------------------------------------------------------------------- #
def _gate(conversation: str, sheet) -> dict | None:
	if user_stopped(conversation, live_turn(conversation)) or _archived(conversation):
		return _refuse("FileBoxRefusedError", _STOPPED)
	if not sheet and _sheet_count(conversation) >= MAX_SHEETS:
		return _refuse("FileBoxRefusedError", _NEEDS_ATTENTION)
	return None


def _foreign(items: list[dict], conversation: str) -> dict | None:
	"""Refused before dispatch: a question of, or moved to, another conversation."""
	for item in items:
		if item["doctype"] != AR:
			continue
		given = item["values"].get("conversation")
		if item["op"] == "update":
			foreign = "conversation" in item["values"] and given != conversation
			foreign = foreign or frappe.db.get_value(AR, item["name"], "conversation") != conversation
		else:
			foreign = bool(given) and given != conversation
		if foreign:
			return _refuse("FileBoxRefusedError", _OTHER_QUESTION)
	return None


def classify_questions(tool: str, items: list[dict], conversation: str, sheet) -> tuple[str, object]:
	"""An all-Approval-Request write: applied then linked (``question``); only this
	conversation's own questions."""
	refusal = _gate(conversation, sheet) or _foreign(items, conversation)
	return ("refuse", refusal) if refusal else ("question", None)


def classify_masters(items: list[dict], conversation: str, sheet) -> tuple[str, object]:
	"""Master holds collect on the sheet (``(masters, sheet)``); a batch mixing in
	Approval Requests is split."""
	refusal = _gate(conversation, sheet) or _foreign(items, conversation)
	if refusal:
		return "refuse", refusal
	masters = [i for i in items if i["doctype"] != AR]
	return ("split" if len(masters) < len(items) else "collect"), (masters, sheet)


def draft_refusal(conversation: str, sheet) -> dict | None:
	"""A draft waits while the collecting sheet holds records, or holds questions and
	no draft exists yet."""
	if not sheet or paused(sheet):
		return None
	if sheet.record_count or (
		sheet.question_count and not frappe.db.get_value(CONV, conversation, "filebox_result_name")
	):
		return _refuse("ApprovalPendingError", _DRAFT_WAITS)
	return None


# --------------------------------------------------------------------------- #
# Records: identity, references, dependencies
# --------------------------------------------------------------------------- #
def _key(value: str) -> str:
	return value.strip().casefold()


def naming_field(doctype: str) -> str | None:
	"""The value an insert takes its name from, when the doctype names by a field or
	prompt (``name``)."""
	auto = (frappe.get_meta(doctype).autoname or "").strip()
	if auto.startswith("field:"):
		return auto[6:].strip() or None
	if auto.lower().startswith("prompt"):
		return "name"
	if doctype in _BY_NAME:
		setting, by_name, field = _BY_NAME[doctype]
		if frappe.defaults.get_global_default(setting) == by_name:
			return field
	return None


def deterministic_name(doctype: str, values: dict) -> str:
	"""The name an insert will get, when the doctype names by a field or prompt."""
	field = naming_field(doctype)
	value = values.get(field) if field else None
	return value.strip() if isinstance(value, str) else ""


def address_spot(values: dict) -> str:
	"""Where an Address is: its normalised street and pincode (its city without one)."""
	pin = "".join(str(values.get("pincode") or "").split()).casefold()
	street = held_parties.norm_name(values.get("address_line1"))
	return f"{street}:{pin or held_parties.norm_name(values.get('city'))}"


def _address_keys(values: dict) -> list[str]:
	"""An Address: its type + each linked party + its spot (a corrected one replaces it;
	another street, or a party's billing and shipping, stay two) + its own GSTIN when
	set (two GST registrations at one spot stay two records)."""
	kind = _key(str(values.get("address_type") or ""))
	spot = address_spot(values)
	gstin = held_parties.norm_gstin(values.get("gstin"))
	suffix = f":{gstin}" if gstin else ""
	rows = values.get("links") if isinstance(values.get("links"), list) else []
	return [
		f"addr:{kind}:{r['link_doctype']}:{_key(r['link_name'])}:{spot}{suffix}"
		for r in rows
		if isinstance(r, dict)
		and isinstance(r.get("link_doctype"), str)
		and isinstance(r.get("link_name"), str)
		and r["link_name"].strip()
	]


def identities(item: dict) -> list[str]:
	"""A record's dedup keys, scoped by doctype: the deterministic name (else the
	normalised title) and the tax id; an Address its party keys; an update keys on
	its target."""
	doctype, values = item["doctype"], item["values"]
	if item["op"] == "update":
		return [f"doc:{doctype}:{item['name']}"]
	args_key = "args:" + held_parties._digest({"doctype": doctype, "values": values})
	if doctype == "Address":
		return _address_keys(values) or [args_key]
	keys = []
	name = deterministic_name(doctype, values)
	if name:
		keys.append(f"dn:{doctype}:{_key(name)}")
	party = held_parties.party_of(item)
	if party["gstin"]:
		keys.append(f"gstin:{doctype}:{party['gstin']}")
	if not name and held_parties.norm_name(party["title"]):
		keys.append(f"name:{doctype}:{held_parties.norm_name(party['title'])}")
	return keys or [args_key]


def category(doctype: str) -> str:
	"""The sheet section: party · address · item · other."""
	if doctype in ("Address", "Contact"):
		return "address"
	if doctype == "Item":
		return "item"
	return "party" if held_parties.is_party(doctype) else "other"


def _record(item: dict) -> dict:
	from jarvis.chat.pending_actions import snapshot_targets

	record = {
		"doctype": item["doctype"],
		"op": item["op"],
		"values": item["values"],
		"identity": identities(item),
		"category": category(item["doctype"]),
		"depends_on": [],
	}
	if item["op"] == "update":
		# The stale check at apply (the held_edit / execute D2 snapshot).
		snap = snapshot_targets("update_doc", {"doctype": item["doctype"], "name": item["name"]})
		record.update(name=item["name"], snapshot=snap[0] if snap else [])
	return record


def _ref_names(record: dict) -> set[str]:
	"""The names other records may link this one by (``held_edit._identities`` plus
	the deterministic name)."""
	if record["op"] != "create":
		return set()
	values = record["values"]
	field = held_parties.title_field(frappe.get_meta(record["doctype"]))
	found = (
		values.get(field) if field else None,
		values.get("name"),
		deterministic_name(record["doctype"], values),
	)
	return {_key(v) for v in found if isinstance(v, str) and v.strip()}


def _ref_value(record: dict) -> str:
	"""The name a link to this create uses: its deterministic name, else its title."""
	values = record["values"]
	field = held_parties.title_field(frappe.get_meta(record["doctype"]))
	for v in (deterministic_name(record["doctype"], values), values.get(field) if field else None):
		if isinstance(v, str) and v.strip():
			return v.strip()
	return ""


def _links(doctype: str, values: dict):
	"""``(target doctype, value, path)`` per Link / Dynamic Link value, child rows included."""
	meta = frappe.get_meta(doctype)
	for df in meta.fields:
		value = values.get(df.fieldname)
		if df.fieldtype in _LINKS:
			target = df.options if df.fieldtype == "Link" else values.get(df.options)
			if isinstance(target, str) and isinstance(value, str) and value.strip():
				yield target, value, (df.fieldname,)
		elif df.fieldtype in table_fields and isinstance(value, list):
			child = frappe.get_meta(df.options)
			for i, row in enumerate(value):
				for cdf in child.fields if isinstance(row, dict) else ():
					cell = row.get(cdf.fieldname)
					if cdf.fieldtype not in _LINKS or not (isinstance(cell, str) and cell.strip()):
						continue
					target = cdf.options if cdf.fieldtype == "Link" else row.get(cdf.options)
					if isinstance(target, str):
						yield target, cell, (df.fieldname, i, cdf.fieldname)


def link_deps(records: list[dict], before: list[dict] | None = None) -> None:
	"""``depends_on`` = the other records each one links to (by doctype + name, or by
	the name it had in ``before``: the same records before an edit)."""
	known: dict = {}
	for j, r in enumerate(records):
		for version in (r, *(before[j : j + 1] if before else ())):
			for key in _ref_names(version):
				known.setdefault((version["doctype"], key), j)
	for i, r in enumerate(records):
		deps = {known.get((t, _key(v)), i) for t, v, _p in _links(r["doctype"], r["values"])}
		r["depends_on"] = sorted(deps - {i})


def _closure(records: list[dict], targets: list[int]) -> list[int]:
	"""Every record ``targets`` depend on, transitively (the targets themselves excluded)."""
	seen, stack = set(), list(targets)
	while stack:
		for j in records[stack.pop()]["depends_on"]:
			if j not in seen:
				seen.add(j)
				stack.append(j)
	return sorted(seen - set(targets))


def order(records: list[dict], idxs) -> list[int]:
	"""Dependencies first, else by index (a cycle breaks at its lowest index)."""
	left, out = sorted(set(idxs)), []
	while left:
		rest = set(left)
		ready = [i for i in left if not any(d != i and d in rest for d in records[i]["depends_on"])]
		out.append((ready or left)[0])
		left.remove(out[-1])
	return out


def _match(records: list[dict], record: dict) -> int | None:
	mine = set(record["identity"])
	for j, r in enumerate(records):
		if r["doctype"] == record["doctype"] and mine & set(r["identity"]):
			return j
	return None


def _same(a: dict, b: dict) -> bool:
	return (a["op"], a.get("name")) == (b["op"], b.get("name")) and held_parties._digest(
		a["values"]
	) == held_parties._digest(b["values"])


def _merge(records: list[dict], new: list[dict]) -> tuple[list[dict], list[int]]:
	"""``(records, targets)``: a new record appends; a re-proposal matching ANY
	identity of a record of its doctype is a no-op when unchanged, else it REPLACES
	that record in place (updates of one target merge their changes, re-snapshotted)
	and the links to its old name follow it. ``targets`` = the records to dry-run."""
	records = [dict(r) for r in records]
	targets = []
	for record in new:
		j = _match(records, record)
		if j is None:
			records.append(record)
			targets.append(len(records) - 1)
			continue
		old = records[j]
		if old["op"] == record["op"] == "update":
			record = {**record, "values": {**old["values"], **record["values"]}}
		if _same(old, record):
			continue
		records[j] = record
		for i in [j, *_follow(records, old, record)]:
			if i not in targets:
				targets.append(i)
	link_deps(records)
	return records, targets


def _follow(records: list[dict], old: dict, new: dict) -> list[int]:
	"""A replaced record renamed (a same-tax-id rename): links to its old names now
	name it. The records changed."""
	doctype, to = new["doctype"], _ref_value(new)
	gone = _ref_names(old) - _ref_names(new)
	gone -= {k for r in records if r is not new and r["doctype"] == doctype for k in _ref_names(r)}
	if not (gone and to):
		return []
	changed = []
	for i, r in enumerate(records):
		values, moved = _relinked(r, lambda t, v: to if t == doctype and _key(v) in gone else None)
		if moved:
			records[i] = r = {**r, "values": values}
			r["identity"] = identities(r)
			changed.append(i)
	return changed


# --------------------------------------------------------------------------- #
# The dependency-aware dry-run (replaces ``api._run_preview`` for a sheet)
# --------------------------------------------------------------------------- #
def _savepoint() -> str:
	sp = "jsh_" + frappe.generate_hash(length=10)
	frappe.db.savepoint(sp)
	return sp


def _relinked(record: dict, rename) -> tuple[dict, bool]:
	"""A copy of the record's values with each link renamed to ``rename(target,
	value)`` (when truthy), and whether any was."""
	values, moved = copy.deepcopy(record["values"]), False
	for target, value, path in list(_links(record["doctype"], values)):
		name = rename(target, value)
		if name and name != value:
			if len(path) == 1:
				values[path[0]] = name
			else:
				values[path[0]][path[1]][path[2]] = name
			moved = True
	return values, moved


def remapped(record: dict, remap: dict) -> dict:
	"""The record's values with links to replayed records renamed to their sandbox
	names (a naming-series supplier)."""
	return _relinked(record, lambda t, v: remap.get((t, _key(v))))[0]


def learn(record: dict, name: str, remap: dict) -> None:
	for key in _ref_names(record):
		remap.setdefault((record["doctype"], key), name)


def _stand_in(record: dict, values: dict, remap: dict) -> None:
	"""Insert a create unvalidated, so records linking it are checked on their own merits."""
	if record["op"] != "create":
		return
	sp = _savepoint()
	try:
		doc = frappe.new_doc(record["doctype"])
		for field, value in values.items():
			doc.set(field, value)
		doc.flags.update(ignore_validate=True, ignore_links=True, ignore_mandatory=True)
		doc.insert()
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point=sp)
		return
	learn(record, doc.name, remap)


def _replay(record: dict, remap: dict) -> None:
	"""A dependency: inserted with ``ignore_mandatory`` (it joined already, maybe
	missing fields), else as a stand-in."""
	from jarvis.tools.create_doc import _insert_one

	if record["op"] != "create":
		return
	values = remapped(record, remap)
	sp = _savepoint()
	try:
		doc = _insert_one(record["doctype"], values, ignore_mandatory=True)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point=sp)
		_stand_in(record, values, remap)
		return
	learn(record, doc.name, remap)


def _missing(record: dict, values: dict, index: int, remap: dict) -> list[dict] | None:
	"""Collect mode for one record: the board-fillable mandatory fields it misses,
	when they are its ONLY problem (the placeholder stays for its dependents)."""
	from jarvis.tools.create_doc import _insert_one

	sp = _savepoint()
	try:
		doc = _insert_one(record["doctype"], values, ignore_mandatory=True)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.db.rollback(save_point=sp)
		return None
	found = held_writes.missing_fields(doc, index)
	if not found:
		frappe.db.rollback(save_point=sp)
		return None
	learn(record, doc.name, remap)
	return found


def _try(record: dict, index: int, remap: dict) -> tuple | None:
	"""Dry-run one new record: None, or ``(kind, detail)`` with kind ``missing`` /
	``invalid`` (tries once) or ``refused`` (always the model's)."""
	from jarvis.tools.create_doc import _insert_one, _validate_create_args
	from jarvis.tools.update_doc import _update_one

	values = remapped(record, remap)
	sp = _savepoint()
	try:
		if record["op"] == "update":
			_update_one(record["doctype"], record["name"], values)
			return None
		_validate_create_args(record["doctype"], values)
		doc = _insert_one(record["doctype"], values)
	except frappe.QueryDeadlockError:
		raise
	except _KNOWN as e:
		frappe.db.rollback(save_point=sp)
		if isinstance(e, _HARD) or not isinstance(e, frappe.ValidationError):
			return "refused", e
		missing = _missing(record, values, index, remap) if record["op"] == "create" else None
		if missing:
			return "missing", missing
		_stand_in(record, values, remap)
		return "invalid", e
	learn(record, doc.name, remap)
	return None


def _dry_run(records: list[dict], targets: list[int]) -> dict[int, tuple]:
	"""ONE sandbox: replay what ``targets`` link to (transitively), then try each
	target. ``{index: (kind, detail)}`` for the records that failed."""
	from jarvis.tools._preview_sandbox import preview_sandbox

	problems, remap = {}, {}
	frappe.local.jarvis_dispatch_depth = getattr(frappe.local, "jarvis_dispatch_depth", 0) + 1
	try:
		with preview_sandbox():
			for j in order(records, _closure(records, targets)):
				_replay(records[j], remap)
			for i in order(records, targets):
				problem = _try(records[i], i, remap)
				if problem:
					problems[i] = problem
	finally:
		frappe.local.jarvis_dispatch_depth -= 1
		frappe.clear_messages()
	return problems


# --------------------------------------------------------------------------- #
# Collect (append) and open
# --------------------------------------------------------------------------- #
def name_of(record: dict) -> str:
	if record["op"] != "create":
		return held_writes._clean_title(record.get("name") or "")
	values = record["values"]
	name = held_parties.party_of(record)["title"] or values.get(f"{frappe.scrub(record['doctype'])}_title")
	return held_writes._clean_title(str(name or deterministic_name(record["doctype"], values)))


def _title(record: dict) -> str:
	return held_writes._clean_title(f"{record['doctype']} {name_of(record)}")


def _fix_text(e: Exception) -> str:
	return filebox_skills._safe(str(e) or type(e).__name__, FIX_CAP)


def _fix(doctype: str, e: Exception) -> dict:
	"""A flagged record's ``{field, label, message}``: ``field`` is the main field the
	error names (frappe's mandatory list, else the longest label it quotes), or None."""
	from frappe import _

	from jarvis.chat.api import _NON_EDIT_FIELDTYPES

	message = _fix_text(e)
	fields = [
		df
		for df in frappe.get_meta(doctype).fields
		if df.fieldname and df.fieldtype not in _NON_EDIT_FIELDTYPES
	]
	listed = _MANDATORY.match(message)
	wanted = [n.strip() for n in listed.group(1).split(",")] if listed else []
	df = next((f for n in wanted for f in fields if f.fieldname == n), None)
	if not df:
		text = message.casefold()
		named = [
			f
			for f in fields
			if len(_(f.label or "")) >= 3
			and re.search(rf"(?<![0-9a-z]){re.escape(_(f.label).casefold())}(?![0-9a-z])", text)
		]
		df = max(named, key=lambda f: len(_(f.label)), default=None)
	return {"field": df.fieldname if df else None, "label": _(df.label) if df else None, "message": message}


def _card(records: list[dict]) -> dict:
	"""Per-record display rows (secrets masked, the full list)."""
	rows = []
	for i, r in enumerate(records):
		values, _secret = held_edit._masked(frappe.get_meta(r["doctype"]), r["values"])
		rows.append(
			{
				"index": i,
				"doctype": r["doctype"],
				"op": r["op"],
				"name": r.get("name") or "",
				"title": name_of(r),
				"category": r["category"],
				"values": values,
				"depends_on": r["depends_on"],
			}
		)
	return {"kind": "sheet", "records": rows}


def _summary(conversation: str) -> str:
	from jarvis.chat.filebox import _source_file

	f = _source_file(conversation)
	return held_writes._clean_title(f"Approval sheet: {(f and f.file_name) or conversation}")


def _open(
	conversation: str, owner: str, records: list[dict], needs_input=None, needs_fix=None, questions=()
) -> str:
	"""Park the conversation's sheet with its first records or ``questions`` (one
	primary waiter, never joined): the questions' stamp and the count commit with it.
	Raises a unique-key error when a parallel call opened it first, and
	``_NotOpened`` when a Stop landed while park re-locked or no question is left."""
	from jarvis.chat.held_sheet_board import counts
	from jarvis.chat.pending_actions import park
	from jarvis.chat.turn_message_binding import is_run_cancel_requested

	def _opened(doc):
		if (
			is_run_cancel_requested(conversation)
			or _archived(conversation)
			or (questions and not _stamp(doc.name, conversation, questions))
		):
			raise _NotOpened
		frappe.db.sql(
			"UPDATE `tabJarvis Conversation` SET filebox_sheet_count=IFNULL(filebox_sheet_count, 0)+1"
			" WHERE name=%(c)s",
			{"c": conversation},
		)

	return park(
		kind=SHEET,
		owner_user=owner,
		exec_user=owner,
		tool=SHEET_TOOL,
		args={"records": records},
		card=_card(records),
		summary=_summary(conversation),
		conversation=conversation,
		dedup_key="sheet:" + conversation,
		dedup_keys=[k for r in records for k in r["identity"]],
		waiters=[conversation],
		needs_input=needs_input or None,
		sheet={
			"collecting": 1,
			"collecting_turn": live_turn(conversation),
			"record_count": len(records),
			"sheet_counts": counts(records),
			"needs_fix": needs_fix or None,
		},
		display_row=_opened,
	)


def drop_waiters(name: str) -> None:
	frappe.db.sql(
		"DELETE FROM `tabJarvis Pending Action Waiter` WHERE parent=%(p)s AND parenttype='Jarvis Pending Action'",
		{"p": name},
	)


def _tampered(sheet, e: _seal.SealError) -> dict:
	"""A sheet whose envelope doesn't verify is failed, never re-sealed. Its run is
	told to stop right here, so it is not resumed too."""
	from jarvis.chat.pending_actions import settle

	drop_waiters(sheet.name)
	_terminal_update(sheet.name, [PENDING], FAILED, reason_code=e.reason_code)
	frappe.log_error(
		title=f"jarvis.file_box.sheet_{e.reason_code}", message=f"{sheet.name}: failed binding {e.binding}"
	)
	frappe.db.commit()
	settle(sheet.name)
	return _refuse("FileBoxRefusedError", _BROKEN)


def _precheck(items: list[dict]) -> dict | None:
	from jarvis.tools._bulk import _MAX_BATCH

	if len(items) > _MAX_BATCH:
		return _refuse("InvalidArgumentError", _TOO_MANY.format(count=len(items), cap=_MAX_BATCH))
	if not items:
		return _refuse("InvalidArgumentError", "Nothing to write: give a doctype and values.")
	match = held_writes._existing_party(items)
	if match:
		label = match["title"] or match["name"]
		return _refuse(
			"InvalidArgumentError",
			f"{match['doctype']} {label} already exists (name: {match['name']}). Use it: leave it out "
			"of this call and do not create it again.",
		)
	return None


def collect(conversation: str, items: list[dict], sheet=None) -> dict:
	"""Add master proposals to the conversation's collecting sheet (``sheet``, the
	gate row ``_classify`` read), opening it when there is none. Holds the
	conversation lock (``held_writes.apply``) until its commit; a parallel open or a
	deadlock re-locks, re-runs the gates and re-reads once."""
	refusal = _precheck(items)
	if refusal:
		frappe.db.commit()
		return refusal
	for attempt in range(2):
		try:
			return _collect_locked(conversation, items, sheet)
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
			frappe.clear_messages()
			frappe.db.rollback()
		except (frappe.QueryDeadlockError, _Retry):
			frappe.db.rollback()
		except _NotOpened:
			return _refuse("FileBoxRefusedError", _STOPPED)
		if attempt == 0:
			lock_conversation(conversation)
			sheet = live_sheet(conversation)
			refusal = _regate(conversation, sheet)
			if refusal:
				frappe.db.commit()
				return refusal
	frappe.log_error(
		title="jarvis.file_box.sheet_collect_failed", message=f"{conversation}: not added after a retry"
	)
	return _refuse("ConfirmationUnavailableError", held_writes._UNAVAILABLE)


def _regate(conversation: str, sheet) -> dict | None:
	"""The ``_classify`` gates again, after a re-lock."""
	if held_writes.waiting_on(conversation) or (sheet and paused(sheet)):
		return _refuse("ApprovalPendingError", held_writes._PENDING_REFUSAL)
	return _gate(conversation, sheet) or filebox_skills.routing_refusal(conversation)


def _collect_locked(conversation: str, items: list[dict], sheet) -> dict:
	from jarvis.chat.held_sheet_board import counts

	owner = frappe.db.get_value(CONV, conversation, "owner")
	if not _identity_ok(conversation, owner, sheet):
		frappe.db.commit()
		return _refuse("FileBoxRefusedError", _IDENTITY)
	records, row = [], None
	if sheet:
		row = get_row(sheet.name)  # the envelope, read once (the gate row leaves it out)
		if not row:
			raise _Retry
		try:
			records = _seal.unseal_call(row)["args"]["records"]
		except _seal.SealError as e:
			return _tampered(row, e)
	records, targets = _merge(records, [_record(i) for i in items])
	if not targets:
		frappe.db.commit()
		return _added(records, targets, {})
	if len(records) > SHEET_MAX_RECORDS:
		frappe.db.commit()
		return _refuse("InvalidArgumentError", _FULL.format(cap=SHEET_MAX_RECORDS))
	problems = _dry_run(records, targets)
	refusal = _problem_refusal(conversation, records, problems)
	if refusal:
		frappe.db.commit()
		return refusal
	needs_input = [
		e for e in held_writes.parse_needs_input(row and row.needs_input) if e["doc_index"] not in targets
	]
	needs_fix = {k: v for k, v in json_dict(row and row.needs_fix).items() if int(k) not in targets}
	for i, (kind, detail) in problems.items():
		if kind == "missing":
			needs_input += detail
		else:
			needs_fix[str(i)] = _fix(records[i]["doctype"], detail)
	if row:
		keys = [_seal.open_key(owner, k) for r in records for k in r["identity"]]
		if not reseal_sheet(
			row,
			args={"records": records},
			card=_card(records),
			record_count=len(records),
			sheet_counts=counts(records),
			needs_input=needs_input or None,
			needs_fix=needs_fix or None,
			dedup_keys=_seal.canonical(keys),
		):
			raise _Retry
		frappe.db.commit()
	else:
		_open(conversation, owner, records, needs_input, needs_fix)
	return _added(records, targets, problems)


def json_dict(value) -> dict:
	try:
		out = json.loads(value) if isinstance(value, str) else value
	except ValueError:
		return {}
	return out if isinstance(out, dict) else {}


def _problem_refusal(conversation: str, records: list[dict], problems: dict) -> dict | None:
	"""Refused: a record the model must fix (permission, duplicate, bad argument), or
	a first failure (tries once: the second joins the sheet flagged)."""
	from jarvis import api

	for i, (kind, detail) in sorted(problems.items()):
		if kind == "refused":
			env = api._preview_error(detail)
			env["error"]["message"] = f"Nothing was added: {_title(records[i])}: {env['error']['message']}"
			return env
	first = []
	for i, (kind, detail) in sorted(problems.items()):
		key = held_writes._miss_key(conversation, "sheet:" + records[i]["identity"][0])
		if not held_writes.seen_once([key]):
			what = (
				"missing " + ", ".join(held_writes.missing_labels(detail))
				if kind == "missing"
				else _fix_text(detail)
			)
			first.append(f"{_title(records[i])}: {what}")
	if first:
		return _refuse("InvalidArgumentError", _FIRST_TRY.format(problems="; ".join(first)))
	return None


def _added(records: list[dict], targets: list[int], problems: dict) -> dict:
	note = (_ADDED if targets else _ALREADY).format(count=len(records))
	if problems:
		note += _FLAGGED.format(records="; ".join(_title(records[i]) for i in sorted(problems)))
	data = {
		"status": "pending_confirmation",
		"held_for": "approval_sheet",
		"note": note + _KEEP_GOING,
		"records": len(records),
	}
	missing = [e for kind, detail in problems.values() if kind == "missing" for e in detail]
	labels = held_writes.missing_labels(missing)
	if labels:
		data["needs_input"] = labels
	return {"ok": True, "data": data}


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #
def only_questions(args: dict) -> dict:
	"""The Approval Request part of a mixed create batch."""
	return {
		**args,
		"docs": [d for d in args.get("docs") or [] if isinstance(d, dict) and d.get("doctype") == AR],
	}


def file_questions(tool: str, args: dict, conversation: str) -> dict:
	"""Apply the run's Approval Request write (outside the lock), then link what it
	created to the sheet."""
	from jarvis import api

	result = api.dispatch_confirmed(tool, args, provenance="auto_apply")
	if tool in held_parties.CREATE_TOOLS and result.get("ok"):
		names = [n for dt, n in held_edit.created_refs(tool, args, result) if dt == AR and n]
		if names and link_to_sheet(conversation, names) and isinstance(result.get("data"), dict):
			result["data"]["note"] = _QUESTION_NOTE
	return result


def file_split(args: dict, conversation: str, collected: dict) -> dict:
	"""A mixed batch, its masters already collected: file its questions too."""
	result = file_questions("create_doc", only_questions(args), conversation)
	if result.get("ok"):
		collected["data"]["note"] += " The Approval Requests in this batch are on the sheet too."
		return collected
	result["error"]["message"] = (
		"The masters in this batch were added to the approval sheet, but its Approval Requests were "
		f"not filed: {result['error']['message']}"
	)
	return result


def link_to_sheet(conversation: str, names: list[str]) -> str | None:
	"""Link the run's new Approval Requests to its collecting sheet, opening a
	questions-only one when there is none and one may open. Re-takes the
	conversation lock; a new sheet, its stamp and its count commit together. Only
	Pending, unlinked questions of this conversation (or none) link, never a wiki
	proposal. The sheet's name, or None (they stay on the board; a stopped run's
	close, so answering one never resumes it)."""
	for _attempt in range(2):
		try:
			return _link_locked(conversation, names)
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError, frappe.QueryDeadlockError):
			frappe.clear_messages()
			frappe.db.rollback()
		except _NotOpened:
			frappe.db.rollback()
			break
	else:
		frappe.log_error(title="jarvis.file_box.sheet_link_failed", message=f"{conversation}: {names}")
	_close_if_stopped(conversation, names)
	return None


def _close_if_stopped(conversation: str, names: list[str]) -> None:
	"""A stopped or archived run's unlinked questions close (the ``close_ended`` way)."""
	lock_conversation(conversation)
	if names and (user_stopped(conversation, live_turn(conversation)) or _archived(conversation)):
		now = frappe.utils.now_datetime()
		frappe.db.sql(
			"UPDATE `tabJarvis Approval Request` SET status='Dismissed', decision=%(d)s, decided_at=%(now)s,"
			" modified=%(now)s" + _LINKABLE,
			{"d": _STOPPED_NOTE, "now": now, "n": tuple(names), "c": conversation, "wiki": _WIKI_SOURCE},
		)
	frappe.db.commit()


# The questions a sheet may take (``%(n)s`` names, ``%(c)s`` the conversation).
_LINKABLE = (
	" WHERE name IN %(n)s AND status='Pending' AND IFNULL(sheet, '')='' AND IFNULL(source, '') <> %(wiki)s"
	" AND IFNULL(conversation, '') IN ('', %(c)s)"
)


def _link_locked(conversation: str, names: list[str]) -> str | None:
	from jarvis.chat.held_sheet_seal import seal_if_stale

	frappe.db.commit()
	lock_conversation(conversation)
	names = names and frappe.db.sql_list(
		"SELECT name FROM `tabJarvis Approval Request`" + _LINKABLE,
		{"n": tuple(names), "c": conversation, "wiki": _WIKI_SOURCE},
	)
	if not names:
		frappe.db.commit()
		return None
	if user_stopped(conversation, live_turn(conversation)) or _archived(conversation):
		raise _NotOpened
	owner = frappe.db.get_value(CONV, conversation, "owner")
	sheet = seal_if_stale(conversation, live_sheet(conversation))
	closed = not enabled() or _sheet_count(conversation) >= MAX_SHEETS
	if (sheet and paused(sheet)) or (not sheet and closed) or not _identity_ok(conversation, owner, sheet):
		frappe.db.commit()
		return None
	if not sheet:
		return _open(conversation, owner, [], questions=names)
	_stamp(sheet.name, conversation, names)
	frappe.db.commit()
	return sheet.name


def _stamp(sheet: str, conversation: str, names: list[str]) -> int:
	"""Link ``names`` to ``sheet`` as File Box questions of ``conversation`` (a chat
	reply never answers them) and recount it. No commit. The questions linked."""
	now = frappe.utils.now_datetime()
	frappe.db.sql(
		"UPDATE `tabJarvis Approval Request` SET sheet=%(s)s, conversation=%(c)s, source='File Box',"
		" modified=%(now)s" + _LINKABLE,
		{"s": sheet, "c": conversation, "now": now, "n": tuple(names), "wiki": _WIKI_SOURCE},
	)
	linked = rowcount()
	frappe.db.sql(
		"UPDATE `tabJarvis Pending Action` SET question_count=(SELECT COUNT(*) FROM"
		" `tabJarvis Approval Request` WHERE sheet=%(s)s), modified=%(now)s"
		" WHERE name=%(s)s AND status='Pending'",
		{"s": sheet, "now": now},
	)
	return linked
