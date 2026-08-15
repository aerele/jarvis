"""File Box: drop an inbound business document, get a draft.

The SPA's File Box pane uploads the file (standard Frappe upload_file),
then calls ``drop_file``. That creates a conversation named after the
file and sends ONE directed prompt through the normal send_message +
attachments machinery: process via the ocr-data-entry skill, decide by
convention, queue Jarvis Approval rows for real ambiguities, at most one
consolidated question. The chat stays the execution surface - the File
Box is just the directed entry point, so streaming, drafts, approvals
and recovery all behave exactly like a hand-typed turn.
"""

from __future__ import annotations

import json

import frappe

from jarvis.permissions import require_jarvis_user

INBOUND_PROMPT = (
	"This file arrived through the File Box - process it as an inbound "
	"business document using the ocr-data-entry skill (read "
	"skills/ocr-data-entry/SKILL.md first and follow its decision policy "
	"exactly). Classify the document type, extract it fully - every line "
	"item verbatim, never a lump-sum balancing line - resolve ambiguities "
	"by the skill's convention ladder, and create the draft. Do NOT ask "
	"me anything in this chat: I am not here. EVERY decision that needs "
	"a human - including what document this is, or that the file is "
	"unreadable - goes to a Jarvis Approval Request row (empty document_type for "
	"classification decisions), then end the turn with a one-line summary."
)


@frappe.whitelist()
@require_jarvis_user
def drop_file(file_url: str, file_name: str | None = None) -> dict:
	"""Create a conversation for an uploaded file and kick off processing."""
	file_url = (file_url or "").strip()
	if not file_url:
		frappe.throw("file_url is required")
	# The URL must be a real uploaded File of this site (no external URLs).
	fdoc = frappe.db.get_value(
		"File",
		{"file_url": file_url},
		["name", "file_name", "file_size", "is_private"],
		as_dict=True,
	)
	if not fdoc:
		frappe.throw("Unknown file - upload it first")
	# Object-level auth: a caller must not be able to feed SOMEONE ELSE'S
	# private file_url and have the agent OCR its contents back to them.
	if not frappe.has_permission("File", doc=fdoc.name):
		frappe.throw("Not permitted", frappe.PermissionError)

	from jarvis.chat.api import create_conversation, send_message

	conv_id = create_conversation()
	title = (file_name or fdoc.file_name or "Inbound document")[:60]
	# file_box: this is an unattended directed run - nobody is present to
	# click a write-confirmation card, so reversible create/update commit
	# directly (destructive ops still park to the approval board). Set via
	# db.set_value to bypass the controller's admin-gate on enabling it.
	frappe.db.set_value(
		"Jarvis Conversation",
		conv_id,
		{"title": f"File: {title}", "file_box": 1},
		update_modified=False,
	)
	# Durable exact link: attach the File to the conversation so resumes
	# re-attach THIS file (not a fragile filename-prefix LIKE match).
	frappe.db.set_value(
		"File",
		fdoc.name,
		{"attached_to_doctype": "Jarvis Conversation", "attached_to_name": conv_id},
		update_modified=False,
	)
	frappe.db.commit()

	attachments = json.dumps([{"file_url": file_url, "file_name": file_name or fdoc.file_name}])
	# background=1 (requires the dedicated-chat-queue PR): a batch of
	# drops drains FIFO and never jumps ahead of a human's typed question.
	res = send_message(
		conversation=conv_id,
		message=INBOUND_PROMPT,
		attachments=attachments,
		background=1,
	)
	return {
		"ok": bool(res.get("ok")),
		"conversation_id": conv_id,
		"run_id": res.get("run_id"),
		"reason": res.get("reason"),
	}


@frappe.whitelist()
@require_jarvis_user
def list_inbound(limit: int = 30) -> list[dict]:
	"""DEPRECATED (superseded by ``list_inbound_page``): kept for one release for
	back-compat. Recent File-Box conversations for the pane's inbound list, with a
	coarse status derived from live chat state - no extra bookkeeping
	doctype to drift out of sync."""
	rows = frappe.get_all(
		"Jarvis Conversation",
		# Exclude Archived: archiving (a.k.a. "delete") a File-Box chat is the
		# way to clear it from the File Box - otherwise the soft-deleted row keeps
		# showing here because we only match on the "File: " title.
		filters={
			"title": ["like", "File: %"],
			"owner": frappe.session.user,
			"status": ["!=", "Archived"],
		},
		fields=["name", "title", "creation"],
		order_by="creation desc",
		limit_page_length=int(limit),
	)
	conv_ids = [r.name for r in rows]
	# Batched: one grouped query for pending approvals, one window query for
	# each conversation's latest assistant message (was a 2xN loop).
	pending_by_conv = {}
	if conv_ids:
		# Raw SQL: v16's get_all rejects SQL-function strings in fields
		# ("count(name) as n" -> ValidationError), which 500'd this whole
		# endpoint for anyone with a File-Box conversation.
		for row in frappe.db.sql(
			"""select conversation, count(name) n
			from `tabJarvis Approval Request`
			where conversation in %(convs)s and status='Pending'
			group by conversation""",
			{"convs": conv_ids},
			as_dict=True,
		):
			pending_by_conv[row.conversation] = row.n
	last_by_conv = {}
	if conv_ids:
		for row in frappe.db.sql(
			"""select m.conversation, m.streaming, m.error, m.recovering
			from `tabJarvis Chat Message` m
			join (select conversation, max(seq) mseq from `tabJarvis Chat Message`
			      where conversation in %(convs)s and role='assistant'
			      group by conversation) x
			  on x.conversation = m.conversation and x.mseq = m.seq
			where m.role='assistant'""",
			{"convs": conv_ids},
			as_dict=True,
		):
			last_by_conv[row.conversation] = row
	for r in rows:
		last = [last_by_conv[r.name]] if r.name in last_by_conv else []
		pending = pending_by_conv.get(r.name, 0)
		if pending:
			r["status"] = "needs_approval"
		elif last and (last[0].streaming or last[0].recovering):
			r["status"] = "processing"
		elif last and last[0].error:
			r["status"] = "error"
		elif not last:
			# Dropped but the worker hasn't inserted the assistant
			# placeholder yet - queued, not done.
			r["status"] = "processing"
		else:
			r["status"] = "done"
		r["pending_approvals"] = pending
	return rows


# --------------------------------------------------------------------------- #
# Paginated list (frozen envelope) + FB-1 cascade delete —
# chat-features-page-migration-design §2.4 + orchestrator Q4.
# ADDITIVE: list_inbound (above) STAYS (deprecated). The derived status moves
# INTO SQL so it can be filtered server-side without breaking pagination.
# --------------------------------------------------------------------------- #
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
APPROVAL = "Jarvis Approval Request"

_INBOUND_STATUSES = {"done", "processing", "needs_approval", "error"}
_INBOUND_SORTABLE = {"creation": "creation", "title": "title"}

# The CASE precedence replicates list_inbound's Python ladder exactly:
# pending > no-assistant-message > streaming/recovering > error > done.
# `%%` is the literal LIKE percent under pyformat params; `{extra}` is the only
# str.format hole (server-built AND-clauses; never user text).
# VISIBILITY: a conversation is visible when the caller OWNS it, OR is TAGGED
# on it — a `Jarvis Approval Request` of that conversation carries a DocShare
# read grant for them (docmeta_api.toggle_share / assignment). EXISTS keeps the
# probe boolean, so a row matching both arms never duplicates. Tagged users
# view/preview only; delete/clear stay owner-gated (`_delete_one`).
# Both grouped subqueries (pa: pending approvals; lm: latest assistant message)
# are CORRELATED to the caller's VISIBLE conversations — the same visibility
# filter is pushed inside via a JOIN so they never aggregate the whole global
# tables (which scaled with everyone's data, not the caller's), and so shared
# rows keep a correct status + pending count instead of being dropped by an
# owner-only inner scope. The outer WHERE keeps only visible rows anyway, so
# results are identical.


def _conv_visible(alias: str) -> str:
	"""SQL predicate: conversation ``alias`` is visible to ``%(me)s`` — they own
	it, or a related Jarvis Approval Request is DocShared-read to them."""
	return (
		f"({alias}.owner = %(me)s OR EXISTS ("
		"SELECT 1 FROM `tabJarvis Approval Request` sa "
		"JOIN `tabDocShare` sds "
		"ON sds.share_doctype = 'Jarvis Approval Request' "
		"AND sds.share_name = sa.name "
		"AND sds.user = %(me)s AND sds.`read` = 1 "
		f"WHERE sa.conversation = {alias}.name))"
	)


_INBOUND_INNER = f"""
	SELECT c.name, c.title, c.creation,
	       COALESCE(pa.n, 0) AS pending_approvals,
	       COALESCE(bt.behind, 0) AS behind_chat,
	       CASE
	         WHEN COALESCE(pa.n, 0) > 0                 THEN 'needs_approval'
	         WHEN lm.conversation IS NULL               THEN 'processing'
	         WHEN lm.streaming = 1 OR lm.recovering = 1 THEN 'processing'
	         WHEN COALESCE(lm.error, '') != ''          THEN 'error'
	         ELSE 'done'
	       END AS status
	FROM `tabJarvis Conversation` c
	LEFT JOIN (SELECT ar.conversation, COUNT(*) n
	           FROM `tabJarvis Approval Request` ar
	           JOIN `tabJarvis Conversation` ac
	             ON ac.name = ar.conversation AND {_conv_visible("ac")}
	           WHERE ar.status = 'Pending' GROUP BY ar.conversation) pa
	  ON pa.conversation = c.name
	LEFT JOIN (SELECT m.conversation, m.streaming, m.recovering, m.error
	           FROM `tabJarvis Chat Message` m
	           JOIN (SELECT mm.conversation, MAX(mm.seq) mseq
	                 FROM `tabJarvis Chat Message` mm
	                 JOIN `tabJarvis Conversation` mc
	                   ON mc.name = mm.conversation AND {_conv_visible("mc")}
	                 WHERE mm.role = 'assistant' GROUP BY mm.conversation) x
	             ON x.conversation = m.conversation AND x.mseq = m.seq
	           WHERE m.role = 'assistant') lm ON lm.conversation = c.name
	LEFT JOIN (SELECT bT.conversation, 1 AS behind
	           FROM `tabJarvis Chat Turn` bT
	           WHERE bT.turn_class = 'background' AND bT.state = 'queued'
	           GROUP BY bT.conversation) bt ON bt.conversation = c.name
	WHERE {_conv_visible("c")} AND c.title LIKE 'File: %%' AND c.status != 'Archived'
	{{extra}}
"""


def _lk(s: str) -> str:
	"""Escape LIKE wildcards in user search input (``\\`` is the default escape)."""
	return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp_page(start, page_length) -> tuple[int, int]:
	try:
		start = max(0, int(start or 0))
	except (TypeError, ValueError):
		start = 0
	try:
		pl = int(page_length or 20)
	except (TypeError, ValueError):
		pl = 20
	return start, max(1, min(pl, 100))


def _load_filters(filters, allowed: set) -> dict:
	if isinstance(filters, str):
		if filters.strip():
			try:
				raw = frappe.parse_json(filters)
			except Exception:
				raw = {}
		else:
			raw = {}
	else:
		raw = filters or {}
	if not isinstance(raw, dict):
		raw = {}
	out: dict = {}
	for k, v in raw.items():
		if k not in allowed:
			frappe.throw(f"Unknown filter: {k}")
		if v in (None, ""):
			continue
		out[k] = v
	return out


def _order_by(sort_field, sort_dir, sortable: dict, default_field, default_dir, prefix="") -> str:
	col = sortable.get(sort_field or "")
	if not col:
		return f"{prefix}`{sortable[default_field]}` {default_dir}, {prefix}`name` asc"
	d = "desc" if (sort_dir or "").lower() == "desc" else "asc"
	return f"{prefix}`{col}` {d}, {prefix}`name` asc"


@frappe.whitelist()
@require_jarvis_user
def list_inbound_page(
	search: str = "",
	filters: str | dict | None = None,
	sort_field: str = "",
	sort_dir: str = "",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""File-Box conversations visible to the caller — owned by them, or tagged to
	them via a DocShare read grant on a related Jarvis Approval Request — with the
	derived status computed IN SQL, so ``status`` filters and pagination compose.
	Envelope ``{rows, total, has_more, start, page_length}``. Tagged users can
	view; delete/clear remain owner-only.

	Filters: ``status`` (done|processing|needs_approval|error), ``from_date`` /
	``to_date`` (YYYY-MM-DD, inclusive; ``to_date`` implemented as ``creation <
	to_date + 1 day``). Search matches the title. Sort: ``creation`` (default
	desc) or ``title``."""
	from frappe.utils import add_days, getdate

	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, {"status", "from_date", "to_date"})

	params: dict = {"me": me, "start": start, "page_length": pl}
	extra_parts: list[str] = []
	if search:
		params["q"] = f"%{_lk(search)}%"
		extra_parts.append("AND c.title LIKE %(q)s")
	if "from_date" in f:
		try:
			params["from_date"] = str(getdate(f["from_date"]))
		except Exception:
			frappe.throw("Invalid from_date (expected YYYY-MM-DD)")
		extra_parts.append("AND c.creation >= %(from_date)s")
	if "to_date" in f:
		try:
			params["to_plus"] = str(add_days(getdate(f["to_date"]), 1))
		except Exception:
			frappe.throw("Invalid to_date (expected YYYY-MM-DD)")
		extra_parts.append("AND c.creation < %(to_plus)s")
	inner = _INBOUND_INNER.format(extra=" ".join(extra_parts))

	outer = ""
	if "status" in f:
		if f["status"] not in _INBOUND_STATUSES:
			frappe.throw("Invalid status filter")
		params["status"] = f["status"]
		outer = "WHERE t.status = %(status)s"

	order = _order_by(sort_field, sort_dir, _INBOUND_SORTABLE, "creation", "desc", prefix="t.")

	total = frappe.db.sql(f"SELECT COUNT(*) FROM ({inner}) t {outer}", params)[0][0]
	rows = frappe.db.sql(
		f"""SELECT t.name, t.title, t.creation, t.status, t.pending_approvals, t.behind_chat
		FROM ({inner}) t {outer}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)

	# Attach the source File so the list can offer an inline preview. Each File-Box
	# conversation carries the dropped document as a File (drop_file, is_private=1);
	# pick the earliest by creation. get_all bypasses File perms, but the rows are
	# already scoped to conversations the caller may see, and the actual byte fetch
	# (iframe/img/read_file) stays permission-gated by Frappe's file handler.
	if rows:
		names = [r["name"] for r in rows]
		file_rows = frappe.get_all(
			"File",
			filters={"attached_to_doctype": CONV, "attached_to_name": ["in", names]},
			fields=["attached_to_name", "file_url", "file_name"],
			order_by="creation asc",
		)
		by_conv: dict = {}
		for fr in file_rows:
			by_conv.setdefault(fr["attached_to_name"], fr)  # earliest wins
		for r in rows:
			fr = by_conv.get(r["name"])
			r["file_url"] = fr["file_url"] if fr else None
			r["file_name"] = fr["file_name"] if fr else None
			fn = (fr and (fr.get("file_name") or fr.get("file_url"))) or ""
			r["file_type"] = fn.rsplit(".", 1)[-1].lower() if "." in fn else None

	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


# --------------------------------------------------------------------------- #
# FB-1 cascade delete (owner-gated; refuses while the latest assistant message
# is streaming/recovering — orchestrator Q6).
# --------------------------------------------------------------------------- #
def _latest_assistant(conversation: str):
	rows = frappe.db.sql(
		"""SELECT streaming, recovering FROM `tabJarvis Chat Message`
		WHERE conversation = %s AND role = 'assistant'
		ORDER BY seq DESC LIMIT 1""",
		(conversation,),
		as_dict=True,
	)
	return rows[0] if rows else None


def _cascade(conversation: str) -> None:
	"""Delete a File-Box conversation and everything hanging off it, in one call:
	its Approval Requests (any status), Chat Messages, attached File docs (via
	``delete_doc`` so storage is cleaned), then the conversation (force=True)."""
	for n in frappe.get_all(APPROVAL, filters={"conversation": conversation}, pluck="name"):
		frappe.delete_doc(APPROVAL, n, ignore_permissions=True, force=True)
	frappe.db.delete(MSG, {"conversation": conversation})
	for fn in frappe.get_all(
		"File",
		filters={"attached_to_doctype": CONV, "attached_to_name": conversation},
		pluck="name",
	):
		frappe.delete_doc("File", fn, ignore_permissions=True, force=True)
	frappe.delete_doc(CONV, conversation, ignore_permissions=True, force=True)


def _delete_one(conversation: str) -> None:
	"""Owner-gated cascade delete of ONE File-Box conversation. Raises on refusal:
	not found / not permitted / not a File-Box row / still processing. All checks
	run BEFORE any delete, so a refusal never leaves a partial delete."""
	doc = frappe.get_doc(CONV, conversation)  # DoesNotExistError if missing
	if doc.owner != frappe.session.user:
		frappe.throw("Not permitted", frappe.PermissionError)
	if not (doc.title or "").startswith("File: "):
		frappe.throw("Not a File Box conversation")
	last = _latest_assistant(conversation)
	if last and (last.streaming or last.recovering):
		frappe.throw("Still processing — stop or wait for it to finish before deleting")
	_cascade(conversation)


@frappe.whitelist()
@require_jarvis_user
def delete_inbound(conversation: str) -> dict:
	"""Owner-gated cascade delete of a File-Box conversation (FB-1)."""
	_delete_one(conversation)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
@require_jarvis_user
def delete_inbound_bulk(conversations: str | list | None = None) -> dict:
	"""Bulk cascade delete (orchestrator Q4). Owner-gated per row, same cascade +
	streaming refusal as ``delete_inbound``. Returns ``{deleted, skipped:[{
	conversation, reason}]}`` — a bad/streaming/foreign row is skipped, not fatal."""
	raw = frappe.parse_json(conversations) if isinstance(conversations, str) else (conversations or [])
	convs = [str(c) for c in raw if c] if isinstance(raw, list) else []
	deleted = 0
	skipped: list[dict] = []
	for conv in convs:
		try:
			_delete_one(conv)
			deleted += 1
		except frappe.PermissionError:
			skipped.append({"conversation": conv, "reason": "not permitted"})
		except frappe.DoesNotExistError:
			skipped.append({"conversation": conv, "reason": "not found"})
		except Exception as e:
			skipped.append({"conversation": conv, "reason": str(e) or "error"})
	frappe.db.commit()
	return {"deleted": deleted, "skipped": skipped}


@frappe.whitelist()
@require_jarvis_user
def clear_processed_inbound() -> dict:
	"""Bulk-delete the caller's OWN File-Box rows whose derived status is
	done|error (never processing/needs_approval), computed with the same status
	SQL. Owner-only by design: the list SQL now also surfaces rows merely
	DocShared to the caller, so the clear query pins ``c.owner = %(me)s`` via the
	``extra`` hole — and ``_delete_one`` re-checks ownership as the backstop."""
	me = frappe.session.user
	rows = frappe.db.sql(
		f"SELECT t.name FROM ({_INBOUND_INNER.format(extra='AND c.owner = %(me)s')}) t "
		f"WHERE t.status IN ('done', 'error')",
		{"me": me},
		as_dict=True,
	)
	deleted = 0
	for r in rows:
		try:
			_delete_one(r.name)
			deleted += 1
		except Exception:
			# done/error rows are never streaming; only a concurrent race fails.
			continue
	frappe.db.commit()
	return {"ok": True, "deleted": deleted}
