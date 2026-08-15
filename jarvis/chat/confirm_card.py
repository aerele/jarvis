"""Build the render-ready "what will change" summary for a confirmation card (F9).

The write-safety gate (``jarvis.api._run_tool``) parks a gated write and shows the
user a card. Today that card renders a raw-JSON dump of the dry-run ``would`` doc
and a one-line ``_describe_call`` target string - it names the record but not what
the write does to it. ``build_card`` turns the tool + args + park-time preview into
a structured, human-readable summary the SPA renders like the model-authored draft
card (a field list for a create, a from->to diff for an update, a per-record
from->to diff for a bulk update, a verb line for submit/cancel/delete/amend,
to/subject/body for an email, method + args for run_method).

It is attached ONCE at park as ``preview["card"]`` (see the gate). Phase-1 F2 stores
the park-time preview in the token record and the resync endpoint returns it
verbatim, so the card rides the ``action:pending`` event, the record, and every
resync identically - it is never rebuilt at resync (which would let it diverge from
the stored preview and re-load the doc on each reconnect).

Safety: values come from the ALREADY perm-filtered ``would`` (create/update tools
call ``apply_fieldlevel_read_permissions`` before returning), and the update
``from`` values are read from a freshly-fetched doc that is permission-CHECKED
(``has_permission("read")``) and then perm-filtered - ``frappe.get_doc`` checks no
permission on its own unless passed a ``check_permission`` kwarg (which defaults to
None), and the fieldlevel filter is permlevel-only, so the explicit check is what
keeps an unreadable record's old values off the card. Long values are truncated,
rows capped, and obviously-secret ``run_method`` arg keys masked. The card carries
no material the owner would not already see in the post-confirm receipt.

Field selection, doc reads, value formatting and no-op detection all live in
``jarvis.chat._record_summary``; this module owns card SHAPE only. The dependency
runs one way (confirm_card -> _record_summary) and must stay that way.

Every GATED write now has a card: phase 4 added the last five kinds (bulk_email,
share, assign, skill, wiki), so share_doc, assign_to, create_custom_skill,
update_wiki and a bulk mail-merge no longer fall back to a bare tool name.
``None`` is still returned for a shape without a bespoke card and for any token
minted before this existed - the SPA then falls back to the summary + raw-preview
rendering. Each new kind ALSO needs an entry in the frontends' ``CARD_KINDS``
whitelist (``frontend/src/lib/actionSummary.js``): ``pendingCardOf`` returns null
for a kind not in that set, so a kind added here alone renders as if it never
shipped.
"""

from __future__ import annotations

import frappe

from jarvis.chat._record_summary import (
	_MAX_BODY,
	_MAX_BULK_BODY,
	_MAX_TABLES,
	_MAX_VAL,
	fmt,
	is_secret,
	same_value,
	summary_rows,
	table_rows,
	values_rows,
)

_MAX_ROWS = 20  # cap fields / diff rows / batch bullets / targets shown
_BULK_KEYS = ("names", "updates", "docs", "messages")

# tool -> present-tense verb for the "will <verb> this <doctype> <name>" card.
_VERB = {
	"submit_doc": "submit",
	"cancel_doc": "cancel",
	"delete_doc": "delete",
	"amend_doc": "amend",
	"apply_workflow_action": "apply",
}


def build_card(tool: str, args, preview) -> dict | None:
	"""Structured, render-ready confirmation summary, or None to fall back to the
	SPA's summary + raw-preview rendering. Best-effort: never raises (a card is
	UX, not correctness), so a failure just yields None."""
	if not isinstance(args, dict):
		return None
	would = preview.get("would") if isinstance(preview, dict) else None
	try:
		bulk_key, bulk_items = _bulk(args)
		if tool in ("create_doc", "create_docs"):
			if bulk_key == "docs" or tool == "create_docs":
				return _batch_create_card(args, would)
			return _create_card(args, would)
		if tool == "update_doc" and not bulk_key:
			return _update_card(args, would)
		if tool == "update_doc" and bulk_key == "updates":
			return _bulk_update_card(args, bulk_items)
		if tool in _VERB:
			return _verb_card(tool, args, bulk_items)
		if tool == "share_doc":
			return _share_card(args, bulk_items if bulk_key == "names" else None)
		if tool == "assign_to":
			return _assign_card(args, bulk_items if bulk_key == "names" else None)
		if tool == "send_email":
			if args.get("messages") is not None:
				# Key off `is not None`, exactly as the tool does (send_email.py:54):
				# an explicit `messages: null` takes the SINGLE-email path and sends,
				# so `"messages" in args` here would drop that card to raw fallback
				# for a call that really does send one email.
				#
				# _bulk treats an EMPTY list as non-bulk (confirm_card.py:94), so a
				# bare `messages=[]` would otherwise reach _email_card and render a
				# plausible empty single-email card - while the tool raises "messages
				# must be a non-empty list" at confirm (send_email.py:107-109). A card
				# that describes a call that cannot run is a lying card; fall back to
				# raw instead. (`[]` is not None -> this branch -> bulk_key is None ->
				# None.)
				return _bulk_email_card(bulk_items) if bulk_key == "messages" else None
			return _email_card(args)
		if tool == "create_custom_skill":
			return _skill_card(args)
		if tool == "update_wiki":
			return _wiki_card(args)
		if tool == "run_method":
			return _method_card(args)
		if tool == "run_import":
			return _import_card(args, preview)
	except Exception:
		frappe.log_error(title="build_card failed", message=frappe.get_traceback())
	return None


def _bulk(args: dict):
	"""(batch-key, items) for a bulk call, else (None, None). Keys are mutually
	exclusive per the tool contracts."""
	for k in _BULK_KEYS:
		v = args.get(k)
		if isinstance(v, list) and v:
			return k, v
	return None, None


def _meta(doctype):
	try:
		return frappe.get_meta(doctype) if doctype else None
	except Exception:
		return None


def _label(meta, fieldname: str) -> str:
	if meta:
		df = meta.get_field(fieldname)
		if df and df.label:
			return df.label
	return fieldname


def _create_card(args: dict, would) -> dict:
	doctype = args.get("doctype")
	values = args.get("values") if isinstance(args.get("values"), dict) else {}
	meta = _meta(doctype)
	# Child tables FIRST: a list value rendered as a table must not also render as a
	# bare "Items · 1 row" text row below. The ``would`` guard is the same perm check
	# the scalar rows use - a permlevel-dropped table would otherwise show as a write
	# that will not happen.
	tables, table_keys = [], set()
	for key in list(values):
		if len(tables) >= _MAX_TABLES:
			break
		value = values.get(key)
		if not isinstance(value, list) or not value:
			continue
		if isinstance(would, dict) and key not in would:
			continue  # perm-dropped from the resolved doc: the save will not write it
		t = table_rows(meta, key, value)
		if t:
			tables.append(t)
			table_keys.add(key)
	rows = []
	for key in list(values)[: _MAX_ROWS * 2]:
		# Perm guard: only show a field that survived the resolved doc's
		# field-level read-permission filter.
		if isinstance(would, dict) and key not in would:
			continue
		if key in table_keys:
			continue  # rendered as a table below, not as a bare "N rows"
		val = would.get(key) if isinstance(would, dict) else values.get(key)
		if val is None or (not isinstance(val, list) and str(val).strip() == ""):
			continue
		df = meta.get_field(key) if meta else None
		rows.append({"label": _label(meta, key), "value": fmt(val, df)})
		if len(rows) >= _MAX_ROWS:
			break
	name = would.get("name") if isinstance(would, dict) else None
	return {"kind": "create", "doctype": doctype, "name": name, "rows": rows, "tables": tables}


def _update_card(args: dict, would) -> dict:
	doctype = args.get("doctype")
	name = args.get("name")
	changes = args.get("changes") if isinstance(args.get("changes"), dict) else {}
	meta = _meta(doctype)
	# OLD values from the current doc (the park dry-run rolled back, so the DB is
	# back to the pre-update state), permission-CHECKED then perm-filtered so
	# neither an unreadable record's nor a restricted field's old value ever leaks.
	# NEW values from ``would`` (already perm-filtered + normalized by the tool).
	old = {}
	doc = None  # must be bound: fmt(..., doc=doc) below needs it for currency/link titles
	try:
		doc = frappe.get_doc(doctype, name)
		# get_doc checks NO permission unless passed a check_permission kwarg
		# (document.py:141-145 -> :336-349), which defaults to None; and the
		# fieldlevel filter below is permlevel-only. Without this, a user who cannot
		# read the record sees its old values on the card.
		if not doc.has_permission("read"):
			raise frappe.PermissionError
		doc.apply_fieldlevel_read_permissions()
		old = doc.as_dict()
	except Exception:
		frappe.clear_messages()  # get_doc's throw leaves an entry that leaks into the turn
		old, doc = {}, None
	diff = []
	for key in list(changes)[: _MAX_ROWS * 2]:
		if isinstance(would, dict) and key not in would:
			continue  # not perm-visible in the resolved doc
		to_val = would.get(key) if isinstance(would, dict) else changes.get(key)
		from_val = old.get(key)
		df = meta.get_field(key) if meta else None
		if same_value(from_val, to_val, df):
			continue  # the save would not change the stored value
		from_s, to_s = fmt(from_val, df, doc), fmt(to_val, df)
		if is_secret(meta, key):
			from_s = to_s = "[hidden]"  # never render a password / secret value
		diff.append({"label": _label(meta, key), "from": from_s, "to": to_s})
		if len(diff) >= _MAX_ROWS:
			break
	title = ""
	if doc is not None and meta:
		try:
			tf = meta.get_title_field()
			if tf and tf != "name" and hasattr(doc, tf):
				title = fmt(doc.get(tf), meta.get_field(tf), doc)
		except Exception:
			title = ""
	return {"kind": "update", "doctype": doctype, "name": name, "title": title, "diff": diff}


def _bulk_update_card(args: dict, updates) -> dict | None:
	"""Per-record from->to diff for a batch ``update_doc(updates=[{name, changes}])``.

	Each rendered record's OLD values come from its current (post-rollback) doc,
	permission-CHECKED then perm-filtered so a restricted field never leaks; the
	NEW values are the caller's requested ``changes``. No-op fields are dropped
	(comparing the values the SAVE would store, never their display forms - see
	``same_value``: fmt_money renders 100.005 and 100.001 identically, so a display
	compare would drop a real change from the card), permlevel-restricted
	fields are skipped (the confirmed save would silently drop them - mirrors
	_update_card's ``would`` guard), and secret / Password values are masked. Only
	the first ``_MAX_ROWS`` records are rendered - each costs one doc read - and the
	rest ride ``extra`` (the raw payload under Details still lists every name).
	``varying`` flags a heterogeneous batch (the requested changes differ across
	records). Each record also carries the changed field labels for its row."""
	doctype = args.get("doctype")
	meta = _meta(doctype)
	first = updates[0].get("changes") if isinstance(updates[0], dict) else None
	varying = any(isinstance(u, dict) and u.get("changes") != first for u in updates)
	records = []
	for u in updates[:_MAX_ROWS]:
		if not isinstance(u, dict):
			continue
		name = u.get("name")
		changes = u.get("changes") if isinstance(u.get("changes"), dict) else {}
		# OLD from the current doc (the park dry-run rolled back), perm-filtered so a
		# restricted field's old value never leaks - same guard as _update_card.
		old, loaded = {}, False
		try:
			doc = frappe.get_doc(doctype, name)
			# get_doc checks NO permission (document.py:141-145 -> :336-349 defaults
			# check_permission to None) and the fieldlevel filter is permlevel-only.
			if not doc.has_permission("read"):
				raise frappe.PermissionError
			doc.apply_fieldlevel_read_permissions()
			old = doc.as_dict()
			loaded = True
		except Exception:
			frappe.clear_messages()
			old = {}
		diff, fields = [], []
		for key in list(changes)[: _MAX_ROWS * 2]:  # over-scan so no-ops don't eat slots
			# A field the user cannot read at their permlevel is delattr'd from the
			# perm-filtered doc; the confirmed save silently skips it too, so don't
			# show a phantom change (mirrors _update_card's ``key not in would``).
			if loaded and key not in old:
				continue
			cdf = meta.get_field(key) if meta else None
			if same_value(old.get(key), changes.get(key), cdf):
				continue  # the save would not change the stored value
			from_s, to_s = fmt(old.get(key), cdf, doc if loaded else None), fmt(changes.get(key), cdf)
			if is_secret(meta, key):
				from_s = to_s = "[hidden]"  # never render a password / secret value
			label = _label(meta, key)
			fields.append(label)
			diff.append({"label": label, "from": from_s, "to": to_s})
			if len(diff) >= _MAX_ROWS:
				break
		row_title = ""
		if loaded and meta:
			try:
				tf = meta.get_title_field()
				if tf and tf != "name" and hasattr(doc, tf):
					row_title = fmt(doc.get(tf), meta.get_field(tf), doc)
			except Exception:
				row_title = ""
		records.append({"name": name, "title": row_title, "fields": fields, "diff": diff})
	if not records:
		return None
	return {
		"kind": "bulk_update",
		"doctype": doctype,
		"count": len(updates),
		"records": records,
		"extra": max(0, len(updates) - len(records)),
		"varying": varying,
	}


def _batch_create_card(args: dict, would) -> dict | None:
	"""Per-record proposed content for a batch create.

	Values come from ``args.docs[i].values``, NOT from ``would``: the sandbox inserted
	those rows and rolled them back, so ``would.created[i]["name"]`` points at nothing
	and cannot be read. For a create the args are also the MORE truthful source -
	Frappe skips the child-table permlevel reset on new records
	(document.py:1326-1328), so a permlevel-restricted child field the caller set IS
	written, while ``would`` is read-filtered and would under-report it.

	``notes`` is NOT rendered: it is a tool argument the MODEL writes
	(create_doc.py:87 -> :134), and on the card it reads as system truth. The card is
	the human's independent check on the agent; the agent can say what it likes in
	chat, where the claim is attributed to it.
	"""
	if not isinstance(would, dict) or not isinstance(would.get("created"), list):
		return None
	created = would["created"]
	docs = args.get("docs") if isinstance(args.get("docs"), list) else []
	rows = [
		{"doctype": d.get("doctype"), "name": d.get("name")}
		for d in created[:_MAX_ROWS]
		if isinstance(d, dict)
	]
	records = []
	# zip, never filter-then-index: filtering non-dicts out of `created` first would
	# pair docs[i] with the WRONG created entry from the first bad row onwards.
	# strict=False is the DELIBERATE choice: unequal lengths only happen for a
	# direct caller or a stale preview, and the card must degrade to a partial
	# render rather than raise (build_card is best-effort; a raise costs the
	# whole card). Through the gate the lists are always equal and aligned.
	for made, req in list(zip(created, docs, strict=False))[:_MAX_ROWS]:
		if not isinstance(made, dict) or not isinstance(req, dict):
			continue
		doctype = req.get("doctype") or made.get("doctype")
		values = req.get("values") if isinstance(req.get("values"), dict) else {}
		meta = _meta(doctype)  # PER ITEM: a batch can mix doctypes
		# Tables FIRST, then EVERYTHING not rendered as a table goes through
		# values_rows - including lists table_rows REJECTED (unknown doctype -> meta
		# is None -> table_rows returns None; a list on a non-Table field) and the
		# _MAX_TABLES overflow, which the spec promises degrades to "N rows". Those
		# fall to fmt's list branch. A proposed key must NEVER vanish: splitting on
		# isinstance(v, list) up front drops every list table_rows rejects, so a
		# human approves a create without seeing its line items - the exact defect
		# this redesign exists to kill. Mirrors _create_card's table_keys pattern
		# (confirm_card.py:121-141), which already solved this.
		tables, table_keys = [], set()
		for key, value in values.items():
			if len(tables) >= _MAX_TABLES:
				break
			if not isinstance(value, list) or not value:
				continue
			t = table_rows(meta, key, value)
			if t:
				tables.append(t)
				table_keys.add(key)
		body = values_rows(meta, {k: v for k, v in values.items() if k not in table_keys})
		records.append(
			{
				"doctype": doctype,
				"name": made.get("name"),
				"rows": body["rows"],
				"extra": body["extra"],
				"tables": tables,
			}
		)
	return {
		"kind": "batch_create",
		"count": len(created),
		"rows": rows,
		"extra": max(0, len(created) - len(rows)),
		"records": records,
	}


def _target_name(item):
	if isinstance(item, dict):
		return item.get("name") or item.get("recipients") or item.get("doctype")
	return item


def _verb_records(doctype, names) -> list[dict]:
	"""A summary per target, capped. ``summary_rows`` returns None for a record that
	is MISSING or that the caller cannot READ - both degrade to name-only, and they
	must stay indistinguishable so the card is not an existence oracle. The title
	only ever comes from summary_rows' permission-checked path.
	"""
	out = []
	for name in names[:_MAX_ROWS]:
		summary = summary_rows(doctype, name) if doctype and name else None
		out.append(
			{
				"name": name,
				"title": summary["title"] if summary else "",
				"rows": summary["rows"] if summary else [],
			}
		)
	return out


def _verb_card(tool: str, args: dict, bulk_items) -> dict:
	verb = _VERB[tool]
	doctype = args.get("doctype")
	action = args.get("action") or ""  # apply_workflow_action only
	if bulk_items:
		targets = [t for t in (_target_name(x) for x in bulk_items[:_MAX_ROWS]) if t]
		return {
			"kind": "verb",
			"verb": verb,
			"action": action,
			"doctype": doctype,
			"count": len(bulk_items),
			"targets": targets,
			"extra": max(0, len(bulk_items) - len(targets)),
			"records": _verb_records(doctype, targets),
		}
	targets = [args["name"]] if args.get("name") else []
	return {
		"kind": "verb",
		"verb": verb,
		"action": action,
		"doctype": doctype,
		"count": 1,
		"targets": targets,
		"extra": 0,
		"records": _verb_records(doctype, targets),
	}


# (key, label, the TOOL'S SIGNATURE DEFAULT). read defaults True (share_doc.py:38);
# everything else False. The default is half the effective value.
_SHARE_FLAGS = (
	("read", "Read", True),
	("write", "Write", False),
	("submit", "Submit", False),
	("share", "Share", False),
)


def _flag_on(args: dict, key: str, default: bool) -> bool:
	"""The value the TOOL will act on, not the value the model typed.

	``bool(args[key])`` when the key is PRESENT - mirroring share_doc's
	``int(bool(...))`` (share_doc.py:92-94), where bool("false") is True, so the
	string "false" GRANTS. The signature default when the key is ABSENT - share_doc
	defaults read=True (share_doc.py:38), so a call that never mentions `read` still
	grants it. Presence, not ``.get()``: absent and explicit-null take different
	branches in the tool.
	"""
	return bool(args[key]) if key in args else default


def _share_card(args: dict, bulk_items) -> dict:
	"""Grantee + permission flags + target summaries.

	Before this, share_doc's card was "share_doc doctype=X name=Y": read-for-one-user
	and everyone+write+share rendered IDENTICALLY - and those grants are the exact
	reason share_doc was pulled into the gate.
	"""
	doctype = args.get("doctype")
	everyone = _flag_on(args, "everyone", False)
	targets = [t for t in (bulk_items or [args.get("name")]) if t][:_MAX_ROWS]
	total = len(bulk_items) if bulk_items else 1
	return {
		"kind": "share",
		"doctype": doctype,
		"grantee": "Everyone" if everyone else fmt(args.get("user") or ""),
		"everyone": everyone,
		"flags": [
			{"label": label, "on": _flag_on(args, key, default)} for key, label, default in _SHARE_FLAGS
		],
		"notify": _flag_on(args, "notify", False),
		"count": total,
		"records": _verb_records(doctype, targets),
		"extra": max(0, total - len(targets)),
	}


def _assign_card(args: dict, bulk_items) -> dict:
	"""Assignee + the description that gets EMAILED to them + target summaries.

	``notify`` defaults True (assign_to.py:40) so an absent arg still sends mail -
	but an EXPLICIT notify=None reaches int(bool(None)) -> 0 and sends none
	(assign_to.py:84). _flag_on distinguishes them; ``.get()`` truthiness would not.
	"""
	doctype = args.get("doctype")
	targets = [t for t in (bulk_items or [args.get("name")]) if t][:_MAX_ROWS]
	total = len(bulk_items) if bulk_items else 1
	return {
		"kind": "assign",
		"doctype": doctype,
		"assignee": fmt(args.get("user") or ""),
		"description": fmt(args.get("description") or "", limit=_MAX_BODY),
		"priority": fmt(args.get("priority") or ""),
		"date": fmt(args.get("date") or ""),
		"notify": _flag_on(args, "notify", True),
		"count": total,
		"records": _verb_records(doctype, targets),
		"extra": max(0, total - len(targets)),
	}


def _recips(value) -> str:
	if isinstance(value, list):
		return ", ".join(str(x) for x in value)
	return "" if value is None else str(value)


def _email_card(args: dict) -> dict | None:
	to = args.get("recipients") or args.get("to") or ""
	return {
		"kind": "email",
		"to": fmt(_recips(to)),
		"subject": fmt(args.get("subject") or ""),
		"cc": fmt(_recips(args.get("cc") or "")),
		"bcc": fmt(_recips(args.get("bcc") or "")),
		"print_format": fmt(args.get("print_format") or ""),
		"body": fmt(args.get("content") or args.get("message") or "", limit=_MAX_BODY),
	}


def _bulk_email_card(messages: list) -> dict | None:
	"""A mail-merge: every message has its OWN recipient, subject and body
	(send_email.py:19-24). The old card returned None on the reasoning that "the
	count is clearer than one body" - true for ONE message to many people, which is
	the SINGLE call's ``recipients`` list, not this shape. send_email is _DESTRUCTIVE
	and always parks; showing the least of any gated tool for the one thing that
	cannot be recalled was the worst gap in the system.
	"""
	shown = []
	for m in messages[:_MAX_ROWS]:
		if not isinstance(m, dict):
			continue
		shown.append(
			{
				"name": fmt(m.get("name") or ""),
				"recipients": fmt(_recips(m.get("recipients") or "")),
				# The batch honours per-message cc/bcc (send_email.py:119). Without these
				# a merge that bcc's a third party on every message renders identical to
				# one that does not - hidden recipients on the one irreversible tool.
				"cc": fmt(_recips(m.get("cc") or "")),
				"bcc": fmt(_recips(m.get("bcc") or "")),
				"subject": fmt(m.get("subject") or ""),
				"body": fmt(m.get("content") or "", limit=_MAX_BULK_BODY),
			}
		)
	if not shown:
		return None
	return {
		"kind": "bulk_email",
		"count": len(messages),
		"messages": shown,
		"extra": max(0, len(messages) - len(shown)),
	}


def _skill_card(args: dict) -> dict:
	"""Persistent agent instructions - the card was the bare tool name.

	``scope`` is the EFFECTIVE value: create_custom_skill computes a `requested`
	scope and then hardcodes "scope": "User" (create_custom_skill.py:44-57), so
	echoing args.scope would claim a bench-wide skill while creating a private one.
	The instructions body gets _MAX_BODY, not the 200-char scalar cap: approving
	text you structurally cannot read is theatre.
	"""
	ui = args.get("user_invocable")
	if isinstance(ui, str):
		ui = ui.strip().lower() in ("1", "true", "yes", "on")
	return {
		"kind": "skill",
		"skill_name": fmt(args.get("skill_name") or ""),
		"scope": "User (private)",  # the tool caps it regardless of the request
		"user_invocable": bool(1 if ui is None else ui),
		"description": fmt(args.get("description") or "", limit=_MAX_VAL),
		"instructions": fmt(args.get("instructions") or "", limit=_MAX_BODY),
	}


def _wiki_card(args: dict) -> dict:
	"""``replace_body_md`` is a FULL REWRITE and says so; ``append_md`` adds a
	section. The tool rejects both together (update_wiki.py:3-4), so mode is
	unambiguous. A diff against the current body is the better card - deferred, it
	needs the current body loaded (spec open question 3).
	"""
	replace = args.get("replace_body_md")
	append = args.get("append_md")
	# MIRROR THE TOOL'S PRECEDENCE EXACTLY. update_wiki.py:143-146 is
	#   if append_md and str(append_md).strip(): ...append...
	#   elif replace_body_md is not None: doc.body_md = str(replace_body_md)
	# and the new-page path (:165) is str(replace_body_md or append_md or "") -
	# APPEND WINS whenever it is non-blank, including over replace_body_md="", which
	# is falsy and so sails through the both-args guard (:96, also truthiness).
	# Checking replace first inverts this: update_wiki(replace_body_md="",
	# append_md="<injected>") would render an EMPTY ERASE card while the tool APPENDS
	# the payload - a phantom action and a hidden real one in the same shape.
	#
	# `is not None` on replace is still right for the lone case: replace_body_md=""
	# sets body_md = str("") - a full ERASE - and must not be mistaken for a metadata
	# edit.
	if append and str(append).strip():
		mode = "append"
		body_src = append
	elif replace is not None:
		mode = "replace"
		body_src = replace
	else:
		mode = "meta"  # the tool no-ops a blank append (update_wiki.py:143)
		body_src = ""
	ref = ""
	if args.get("ref_doctype") and args.get("ref_name"):
		ref = f"{args['ref_doctype']} {args['ref_name']}"
	return {
		"kind": "wiki",
		"slug": fmt((args.get("slug") or "").strip().lower()),  # the tool strips/lowers (:93)
		"title": fmt((args.get("title") or "")[:140]),  # the tool truncates to 140 (:134)
		"scope": fmt((args.get("scope") or "Org").capitalize()),  # the tool capitalizes (:104)
		"page_type": fmt(args.get("page_type") or ""),
		"ref": fmt(ref),
		# `summary` is PERSISTED (update_wiki.py:141-142, :164) - a call setting only
		# summary would otherwise render as an empty "meta" card and the human would
		# approve stored text they never saw.
		"summary": fmt(args.get("summary") or "", limit=_MAX_VAL),
		"mode": mode,
		"body": fmt(body_src, limit=_MAX_BODY),  # the body the TOOL will write
	}


def _method_card(args: dict) -> dict:
	inner = args.get("args") if isinstance(args.get("args"), dict) else {}
	shown = {}
	for k, v in list(inner.items())[:_MAX_ROWS]:
		# No meta for a run_method arg bag, so this is the key-name check only.
		shown[str(k)] = "[hidden]" if is_secret(None, k) else fmt(v)
	return {"kind": "method", "method": fmt(args.get("method") or ""), "args": shown}


_MAX_SAMPLE_COLS = 8  # columns rendered in the import sample table (20 is unusable)


def _import_card(args: dict, preview) -> dict | None:
	"""Rich confirmation card for run_import, rendered from the read-only import preview
	the park stashed at ``preview["import"]`` (already secret-masked, permission-checked).
	Shows WHAT is imported - target doctype, record vs row count, the file, the sample
	rows, the column mapping, advisory warnings - not a function path. Returns None on a
	malformed preview so build_card's caller shows the described-intent floor (the park
	always attaches a ``note``), never a blank card."""
	imp = preview.get("import") if isinstance(preview, dict) else None
	if not isinstance(imp, dict):
		return None

	columns = imp.get("columns") or []
	mapped = [
		(f"{c.get('header')} -> {c.get('label')}" if c.get("label") else c.get("header"))
		for c in columns
		if c.get("mapped")
	]
	unmapped = [c.get("header") for c in columns if not c.get("mapped")]

	sample = imp.get("sample") if isinstance(imp.get("sample"), dict) else {}
	sample_cols = sample.get("columns") or []
	sample_rows = sample.get("rows") or []
	col_cap = min(len(sample_cols), _MAX_SAMPLE_COLS)
	shown_cols = sample_cols[:col_cap]
	shown_rows = [{"cells": (r.get("cells") or [])[:col_cap]} for r in sample_rows]

	advisory = [
		w.get("message") for w in (imp.get("warnings") or {}).get("advisory") or [] if w.get("message")
	]

	return {
		"kind": "import",
		"doctype": imp.get("doctype"),
		"import_type": imp.get("import_type"),
		"file": (imp.get("file") or {}).get("name"),
		"total_rows": imp.get("total_rows"),
		"total_records": imp.get("total_records"),
		# Only claim "will submit" when the target is actually submittable (else the card
		# lies: submit_after_import silently no-ops on a non-submittable doctype).
		"submit_after_import": bool(args.get("submit_after_import")) and bool(imp.get("submittable")),
		"columns": {"mapped": mapped[:_MAX_ROWS], "unmapped": unmapped[:_MAX_ROWS]},
		"sample": {
			"columns": shown_cols,
			"rows": shown_rows,
			"extra_cols": max(0, len(sample_cols) - col_cap),
		},
		"advisory": advisory[:_MAX_ROWS],
	}
