"""File Box: drop an inbound business document, get a draft.

The SPA's File Box pane uploads the file (standard Frappe upload_file),
then calls ``drop_file``. That creates a conversation named after the
file and sends ONE directed prompt through the normal send_message +
attachments machinery: classify the file, follow a matching processing
skill (a Jarvis Custom Skill / persona skill found by find_skills, else
ocr-data-entry), consult the wiki before drafting and record findings
back to it after, and queue Jarvis Approval rows for real ambiguities.
The drafts-only / never-ask / ambiguity-to-Approval safety rules OVERRIDE
any skill the run follows. The chat stays the execution surface - the
File Box is just the directed entry point, so streaming, drafts,
approvals and recovery all behave exactly like a hand-typed turn.
"""

from __future__ import annotations

import json
import re

import frappe

from jarvis.permissions import message_origin, refuse_in_tool_dispatch, require_jarvis_user

# The persona fallback skill (SHARED_CORE_SKILLS, role_profiles.py) - always
# available, and NOT a Jarvis Custom Skill row, so it is validated by literal
# match, never through get_skill. Defined once so the pin validation and the
# prompt fallback reference the same string.
OCR_DATA_ENTRY = "ocr-data-entry"

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
APPROVAL = "Jarvis Approval Request"

INBOUND_PROMPT = (
	"This file arrived through the File Box - process it as an inbound business "
	"document, unattended, in this order.\n"
	"1. Classify what the document is.\n"
	"2. Look for a processing skill that fits this KIND of document: call "
	"find_skills with the document kind and scan available_skills. Only follow a "
	"skill whose description CLEARLY names this kind of document (find_skills is a "
	"keyword match, so a loose match is not a match); read it with get_skill (or "
	"cat its SKILL.md) and use it for classification, field mapping and routing. If "
	"nothing clearly fits, use the ocr-data-entry skill (read "
	"skills/ocr-data-entry/SKILL.md first and follow its decision policy exactly).\n"
	"3. Before drafting, check the wiki for what we already know: read_wiki for the "
	"party and the topic, and build on / reference existing content instead of "
	"starting from scratch. If the wiki is unavailable, skip this step and carry on.\n"
	"4. BEFORE drafting, resolve the party and every master the draft links to "
	"(supplier or customer, items, addresses...) with read tools. If any are missing, "
	"create ALL of them in ONE create_doc batch (docs=[...]). The bench holds that batch "
	"for a human on the Approval Board - do NOT also file a Jarvis Approval Request for "
	"it. When a write comes back held (status pending_confirmation), END the turn right "
	"away: the run resumes by itself once it is decided.\n"
	"5. Extract the document fully - every line item verbatim, never a lump-sum "
	"balancing line - resolve ambiguities by the skill's convention ladder, and "
	"create the draft on its own (never in a batch with masters).\n"
	"6. AFTER the draft exists, record what you learned back to the wiki with "
	"update_wiki, extending the page from step 3 rather than duplicating it. Your "
	"wiki note is PROPOSED for a Jarvis reviewer to approve before it lands - it does "
	"not go live from this run, so do not re-submit or wait on it. This is "
	"best-effort: if the proposal is refused or the wiki is off, note it and move on - "
	"never fail or retry the run over a wiki write.\n"
	"These rules OVERRIDE any skill you follow and are non-negotiable: do NOT ask me "
	"anything in this chat (I am not here); only ever create Drafts of documents, never "
	"submit; new or changed master records wait for a human on the Approval Board as "
	"above; every other action (submit, cancel, delete, email, share, assign, comments, "
	"imports...) is refused in this run; and EVERY decision that needs a "
	"human - including what document this is, or that the file is unreadable - goes "
	"to a Jarvis Approval Request row (empty document_type for classification "
	"decisions). End the turn with a one-line summary."
)


def build_inbound_prompt(skill: str | None = None) -> str:
	"""The File Box directed prompt. ``skill=None`` returns ``INBOUND_PROMPT``
	VERBATIM (the auto-discovery flow), so a tag can never drift the default path.
	A tagged ``skill`` (a canonical skill_name) prepends a directive that applies
	that skill ALONGSIDE the system's chosen skill (ADDITIVE, not a replacement):
	the normal classify + base-extraction flow below still runs, and the tagged
	skill layers its own rules on top. Every step and the safety envelope below
	apply unchanged."""
	if not skill:
		return INBOUND_PROMPT
	directive = (
		f"This file also carries a TAGGED skill: '{skill}'. Run the normal flow below "
		"(classify, and process with the system's chosen skill - a discovered skill, "
		f"else {OCR_DATA_ENTRY} - as the base extraction) AND, IN ADDITION, read the "
		f"'{skill}' skill (get_skill '{skill}', or cat skills/{skill}/SKILL.md) and apply "
		"its instructions on top: its field mappings, validation and routing LAYER onto "
		"the base, they do not replace it. If the tagged skill cannot be loaded, carry on "
		"with the base flow alone. The safety rules below still OVERRIDE both.\n\n"
	)
	return directive + INBOUND_PROMPT


def _validated_pinned_skill(skill: str | None) -> str | None:
	"""Resolve a client-supplied skill slug to the CANONICAL skill_name to pin, or
	None to fall back to auto-discovery. The client string is untrusted, so the
	slug is validated through ``get_skill`` itself - the same resolver the agent
	will use (system-user + enabled + own-row-wins) - so drop-time acceptance and
	run-time load can never disagree, and only a validated slug (SLUG_RE, so no
	newline / '<' / '_' / quote) is ever embedded in the prompt or stored.
	Fallback is UNIFORM: a nonexistent slug and an inaccessible one both return
	None (no enumeration oracle), and the note is server-side only."""
	slug = (skill or "").strip()
	if not slug:
		return None
	if slug == OCR_DATA_ENTRY:  # persona skill, no Custom Skill row - accept by literal
		return OCR_DATA_ENTRY
	from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
	from jarvis.tools.get_skill import get_skill

	try:
		row = get_skill(slug)
	except (InvalidArgumentError, PermissionDeniedError):
		# UNIFORM fallback: unknown slug AND inaccessible skill both land here -> None
		# (no enumeration oracle). A transient/unexpected error is NOT caught - it
		# propagates so the drop fails loudly rather than silently dropping the pin.
		# Server-side note only (never in the return payload) so the client can't
		# probe another user's private-skill existence by watching whether a pin stuck.
		frappe.logger("jarvis").info(
			"file_box pin fell back to auto: skill %r not usable by %s", slug, frappe.session.user
		)
		return None
	return row.get("skill_name") or None


def _resolve_drop_file(file: str | None, file_url: str | None):
	"""The File to process: by docname (the upload response's ``name``), else by
	``file_url`` preferring an unattached row. An identical re-upload shares the
	file_url, so the url alone could pick an earlier drop's File."""
	fields = ["name", "file_name", "file_url", "attached_to_doctype", "attached_to_name", "owner"]
	if file:
		return frappe.db.get_value("File", file, fields, as_dict=True)
	file_url = (file_url or "").strip()
	if not file_url:
		frappe.throw("file is required")
	rows = frappe.get_all(
		"File", filters={"file_url": file_url}, fields=fields, order_by="creation desc", limit=20
	)
	# The caller's own unattached upload first, then any unattached row.
	rows.sort(key=lambda r: (bool(r.attached_to_name), r.owner != frappe.session.user))
	return rows[0] if rows else None


def _refuse_foreign_attachment(fdoc) -> None:
	"""M13: re-parent only the caller's own unattached upload or a File already on
	their own conversation - never another document's attachment, nor another
	user's unattached File (a public branding asset would be cascade-deleted)."""
	if not fdoc.attached_to_name:
		if fdoc.owner == frappe.session.user:
			return
		frappe.throw(
			"This file was uploaded by someone else - upload your own copy to process it.",
			frappe.PermissionError,
		)
	if fdoc.attached_to_doctype == CONV and (
		frappe.db.get_value(CONV, fdoc.attached_to_name, "owner") == frappe.session.user
	):
		return
	frappe.throw(
		"This file is attached to another document - upload it again to process it.",
		frappe.PermissionError,
	)


def _source_file(conv: str):
	"""The dropped File (``file_url``, ``file_name``) a resume re-attaches: the one
	stamped at drop, else the conversation's earliest attachment."""
	fields = ["file_url", "file_name"]
	name = frappe.db.get_value(CONV, conv, "filebox_source_file")
	row = frappe.db.get_value("File", name, fields, as_dict=True) if name else None
	if row:
		return row
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": CONV, "attached_to_name": conv},
		fields=fields,
		order_by="creation asc",
		limit=1,
	)
	return rows[0] if rows else None


def _send_inbound(conv: str, file_doc, pinned: str | None, preamble: str | None = None) -> dict:
	"""Send the directed File Box prompt for ``file_doc`` into ``conv`` and record the
	outcome on the conversation: a refused or raising send stamps
	``filebox_last_error`` (the row reads Failed with the reason), a successful one
	clears it. Never raises, so a bulk drop keeps going. The caller commits first."""
	from jarvis.chat.api import send_message

	attachments = json.dumps([{"file_url": file_doc.file_url, "file_name": file_doc.file_name}])
	message = build_inbound_prompt(pinned)
	if preamble:
		message = f"{preamble}\n\n{message}"
	try:
		# background=1: a batch of drops drains FIFO behind a human's typed question.
		with message_origin("file_box"):
			res = send_message(conversation=conv, message=message, attachments=attachments, background=1)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="jarvis.file_box.send_failed", message=frappe.get_traceback())
		res = {"ok": False, "reason": frappe._("something went wrong - try again")}
	if res.get("ok"):
		error = {"filebox_last_error": None, "filebox_last_error_at": None}
	else:
		reason = res.get("reason") or frappe._("the run could not be queued")
		error = {
			"filebox_last_error": f"Couldn't start: {reason}"[:500],
			"filebox_last_error_at": frappe.utils.now_datetime(),
		}
	frappe.db.set_value(CONV, conv, error, update_modified=False)
	frappe.db.commit()
	return res


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def drop_file(
	file_url: str | None = None,
	file_name: str | None = None,
	skill: str | None = None,
	file: str | None = None,
) -> dict:
	"""Create a conversation for an uploaded file and kick off processing.

	``file`` is the uploaded File's docname (``file_url`` is the legacy fallback).
	``skill`` (optional) tags ONE extra processing skill for this file: a Jarvis
	Custom Skill's skill_name. It is validated server-side (the client string is
	untrusted) and, if usable, the agent applies it ALONGSIDE the system's chosen
	skill (additive - the base extraction still runs, the tagged skill layers on
	top). An unusable / absent skill just leaves the normal auto-discovery flow."""
	refuse_in_tool_dispatch()
	# Must be a real uploaded File of this site (no external URLs).
	fdoc = _resolve_drop_file(file, file_url)
	if not fdoc:
		frappe.throw("Unknown file - upload it first")
	# Object-level auth: a caller must not be able to feed SOMEONE ELSE'S
	# private file and have the agent OCR its contents back to them.
	if not frappe.has_permission("File", doc=fdoc.name):
		frappe.throw("Not permitted", frappe.PermissionError)
	_refuse_foreign_attachment(fdoc)

	from jarvis.chat.api import create_conversation

	# Untrusted client slug -> canonical skill_name to pin, or None (auto).
	pinned = _validated_pinned_skill(skill)

	conv_id = create_conversation()
	title = (file_name or fdoc.file_name or "Inbound document")[:60]
	# file_box: this is an unattended directed run - nobody is present to click
	# a write-confirmation card, so held_writes decides every write: drafts
	# commit directly, masters wait on the Approval Board, the rest is refused.
	# Set via db.set_value to bypass the controller's admin-gate on enabling it.
	# filebox_pinned_skill: the validated pin (or "" for auto-discovery).
	frappe.db.set_value(
		CONV,
		conv_id,
		{
			"title": f"File: {title}",
			"file_box": 1,
			"filebox_pinned_skill": pinned or "",
			"filebox_source_file": fdoc.name,
		},
		update_modified=False,
	)
	# Durable exact link: attach the File to the conversation so resumes
	# re-attach THIS file (not a fragile filename-prefix LIKE match).
	frappe.db.set_value(
		"File",
		fdoc.name,
		{"attached_to_doctype": CONV, "attached_to_name": conv_id},
		update_modified=False,
	)
	frappe.db.commit()

	if file_name:
		fdoc.file_name = file_name
	res = _send_inbound(conv_id, fdoc, pinned)
	return {
		"ok": bool(res.get("ok")),
		"conversation_id": conv_id,
		"run_id": res.get("run_id"),
		"reason": res.get("reason"),
	}


# --------------------------------------------------------------------------- #
# Paginated list (frozen envelope) + FB-1 cascade delete —
# chat-features-page-migration-design §2.4 + orchestrator Q4. The derived status
# is computed IN SQL so it can be filtered server-side without breaking pagination.
# --------------------------------------------------------------------------- #
_STATUSES = ("processing", "needs_approval", "draft_created", "failed", "no_draft")
# Pre-PR-1 filter values, accepted for one release (old clients / bookmarks).
_STATUS_ALIASES = {"done": ("draft_created", "no_draft"), "error": ("failed",)}
_CLEARABLE = ("draft_created", "no_draft")
_INBOUND_SORTABLE = {"creation": "creation", "title": "title"}

# VISIBILITY: a conversation is visible when the caller OWNS it, OR is TAGGED
# on it — a `Jarvis Approval Request` of that conversation carries a DocShare
# read grant for them (docmeta_api.toggle_share / assignment). EXISTS keeps the
# probe boolean, so a row matching both arms never duplicates. Tagged users
# view/preview only; delete/clear stay owner-gated (`_delete_one`).
# The grouped subqueries are CORRELATED to the caller's VISIBLE File Box
# conversations (the same filter pushed inside via a JOIN), so they never
# aggregate the whole global tables.


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


# What makes a row "Needs approval": a Pending decision the dropper owes - an
# Approval Request, or an undecided held write (Pending Action) it waits on. Wiki
# proposals are the reviewer's lane, never the dropper's (AC7). `src` routes the
# result link (an approval, or the board's held lane).
_PENDING_WAITS = f"""
	SELECT ar.conversation, ar.name AS item, ar.title, ar.creation, 'ar' AS src, NULL AS needs_input
	FROM `tabJarvis Approval Request` ar
	JOIN `tabJarvis Conversation` ac
	  ON ac.name = ar.conversation AND ac.file_box = 1 AND {_conv_visible("ac")}
	WHERE ar.status = 'Pending' AND COALESCE(ar.source, '') != 'File Box Wiki'
	UNION ALL
	SELECT w.conversation, pa.name AS item, pa.summary AS title, pa.creation, 'pa' AS src, pa.needs_input
	FROM `tabJarvis Pending Action Waiter` w
	JOIN `tabJarvis Pending Action` pa
	  ON pa.name = w.parent AND pa.kind = 'file_box_held' AND pa.status IN ('Pending', 'Executing')
	JOIN `tabJarvis Conversation` wc
	  ON wc.name = w.conversation AND wc.file_box = 1 AND {_conv_visible("wc")}
	WHERE w.parenttype = 'Jarvis Pending Action'
"""


def _held_open(alias: str) -> str:
	"""SQL predicate: conversation ``alias`` waits on an undecided held write."""
	return (
		"EXISTS (SELECT 1 FROM `tabJarvis Pending Action Waiter` hw "
		"JOIN `tabJarvis Pending Action` hp ON hp.name = hw.parent "
		f"WHERE hw.conversation = {alias}.name AND hw.parenttype = 'Jarvis Pending Action' "
		"AND hp.kind = 'file_box_held' AND hp.status IN ('Pending', 'Executing'))"
	)


def _decided_waiter(state: str) -> str:
	"""SQL predicate: a decided held write's waiter on ``c`` matches ``state``."""
	return (
		"EXISTS (SELECT 1 FROM `tabJarvis Pending Action Waiter` rw "
		"JOIN `tabJarvis Pending Action` rp ON rp.name = rw.parent "
		"WHERE rw.conversation = c.name AND rw.parenttype = 'Jarvis Pending Action' "
		f"AND rp.kind = 'file_box_held' AND rp.status NOT IN ('Pending', 'Executing') AND ({state}))"
	)


# After a decision, a resume still on its way reads processing: queued (`due`) until
# the reconciler's retry cutoff, sending (`claimed`) until its reclaim, not yet queued
# ('': the settle is still retrying). Past those, or failed past its retries:
# "Approved, but the run couldn't continue" (AC13). Neither is ever clearable.
_RESUME_WAITING = (
	"(rw.resume_state = 'due' AND rw.modified >= %(resume_fresh)s)"
	" OR (rw.resume_state = 'claimed' AND rw.modified >= %(claim_fresh)s)"
	" OR (IFNULL(rw.resume_state, '') = '' AND rp.settle_attempts < %(max_settle)s)"
)
_RESUMING = _decided_waiter(_RESUME_WAITING)
_RESUME_FAILED = _decided_waiter(f"IFNULL(rw.resume_state, '') != 'done' AND NOT ({_RESUME_WAITING})")


def _latest(where: str, alias: str, cols: str) -> str:
	"""Latest (max seq) message matching ``where`` per visible File Box conversation."""
	return f"""(SELECT {cols}
	           FROM `tabJarvis Chat Message` m
	           JOIN (SELECT m.conversation, MAX(m.seq) mseq
	                 FROM `tabJarvis Chat Message` m
	                 JOIN `tabJarvis Conversation` {alias}
	                   ON {alias}.name = m.conversation AND {alias}.file_box = 1 AND {_conv_visible(alias)}
	                 WHERE {where}
	                 GROUP BY m.conversation) x
	             ON x.conversation = m.conversation AND x.mseq = m.seq
	           WHERE {where})"""


# The final reply; a hidden assistant row is a discarded placeholder, not a reply.
_LM = _latest(
	"m.role = 'assistant' AND COALESCE(m.hidden, 0) = 0",
	"mc",
	"m.conversation, m.name, m.seq, m.streaming, m.recovering, m.error, m.stopped, m.modified, "
	"CASE WHEN COALESCE(m.content, '') REGEXP '[^[:space:]]' THEN 0 ELSE 1 END AS empty",
)
# The last prompt (hidden continuations included) + the state of its latest turn
# (NULL on the legacy dispatch path, which has no Turn rows).
_LU = _latest(
	"m.role = 'user'",
	"uc",
	"m.conversation, m.name, m.seq, m.creation, "
	"(SELECT ct.state FROM `tabJarvis Chat Turn` ct WHERE ct.seed_message = m.name "
	"ORDER BY ct.creation DESC LIMIT 1) AS turn_state",
)

# A send failure stamped after the last drop/resume outranks the unanswered arm (M8).
_ERR_NEWER = (
	"(c.filebox_last_error_at IS NOT NULL AND (lu.creation IS NULL OR c.filebox_last_error_at > lu.creation))"
)
_UNANSWERED = "(lm.seq IS NULL OR lm.seq < lu.seq)"

# The status ladder (AC7), first match wins:
#   needs_approval > processing > failed (a held resume that couldn't continue)
#   > draft_created > failed > no_draft.
# `live` = a reply is streaming (and fresh), a Jarvis Chat Turn is non-terminal, a
# held-write resume is on its way, or (legacy dispatch path, no Turn row) the last
# user row is unanswered but younger than the orphan threshold. `fail_code` is why a
# finished run failed (NULL = ended cleanly); it is computed even for a drafted run
# ("· ended with an error").
# `{extra}` is the only str.format hole (server-built AND-clauses; never user text).
_INBOUND_INNER = f"""
	SELECT r.*,
	       CASE
	         WHEN r.pending_approvals > 0 THEN 'needs_approval'
	         WHEN r.live = 1              THEN 'processing'
	         WHEN r.fail_code = 'resume_failed' THEN 'failed'
	         WHEN r.has_draft = 1         THEN 'draft_created'
	         WHEN r.fail_code IS NOT NULL THEN 'failed'
	         ELSE 'no_draft'
	       END AS status
	FROM (
	  SELECT c.name, c.title, c.creation, c.filebox_result_doctype, c.filebox_result_name,
	         c.filebox_result_count, c.filebox_last_error, lm.name AS lm_name,
	         COALESCE(pa.n, 0) AS pending_approvals,
	         COALESCE(bt.behind, 0) AS behind_chat,
	         CASE WHEN COALESCE(c.filebox_result_name, '') != '' THEN 1 ELSE 0 END AS has_draft,
	         CASE
	           WHEN (lm.streaming = 1 OR lm.recovering = 1) AND lm.modified >= %(fresh)s THEN 1
	           WHEN EXISTS (SELECT 1 FROM `tabJarvis Chat Turn` lt
	                        WHERE lt.conversation = c.name AND lt.state IN %(live_states)s) THEN 1
	           WHEN {_RESUMING} THEN 1
	           WHEN lu.name IS NOT NULL AND lu.turn_state IS NULL AND {_UNANSWERED}
	                AND lu.creation >= %(fresh)s
	                AND NOT {_ERR_NEWER} THEN 1
	           ELSE 0
	         END AS live,
	         CASE
	           WHEN {_RESUME_FAILED} THEN 'resume_failed'
	           WHEN {_ERR_NEWER} THEN 'send_failed'
	           WHEN lu.name IS NULL THEN 'no_start'
	           WHEN lu.turn_state = 'cancelled' THEN 'cancelled'
	           WHEN {_UNANSWERED} THEN 'no_start'
	           WHEN COALESCE(lm.error, '') != '' THEN 'error'
	           WHEN lm.stopped = 1 THEN 'stopped'
	           WHEN lm.streaming = 1 OR lm.recovering = 1 THEN 'stale'
	           WHEN lm.empty = 1 THEN 'empty'
	         END AS fail_code
	  FROM `tabJarvis Conversation` c
	  LEFT JOIN (SELECT w.conversation, COUNT(*) n FROM ({_PENDING_WAITS}) w
	             GROUP BY w.conversation) pa ON pa.conversation = c.name
	  LEFT JOIN {_LM} lm ON lm.conversation = c.name
	  LEFT JOIN {_LU} lu ON lu.conversation = c.name
	  LEFT JOIN (SELECT bT.conversation, 1 AS behind
	             FROM `tabJarvis Chat Turn` bT
	             WHERE bT.turn_class = 'background' AND bT.state = 'queued'
	             GROUP BY bT.conversation) bt ON bt.conversation = c.name
	  WHERE {_conv_visible("c")} AND c.file_box = 1 AND c.status != 'Archived'
	  {{extra}}
	) r
"""


def _ladder_params(me: str) -> dict:
	from frappe.utils import add_to_date, now_datetime

	from jarvis.chat.pending_actions._reconcile import INTERRUPT_AFTER_S, SETTLE_AFTER_S
	from jarvis.chat.pending_actions._settle import MAX_SETTLE_ATTEMPTS
	from jarvis.chat.stale_scan import ORPHAN_MAX_AGE_SECONDS
	from jarvis.chat.turn_state import NONTERMINAL_STATES

	now = now_datetime()
	return {
		"me": me,
		"fresh": add_to_date(now, seconds=-ORPHAN_MAX_AGE_SECONDS),
		"resume_fresh": add_to_date(now, seconds=-SETTLE_AFTER_S),
		"claim_fresh": add_to_date(now, seconds=-INTERRUPT_AFTER_S),
		"max_settle": MAX_SETTLE_ATTEMPTS,
		"live_states": NONTERMINAL_STATES,
	}


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


_MD_NOISE = (
	(re.compile(r"<[^>]*>"), ""),
	(re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
	(re.compile(r"\*\*|__|`"), ""),
	(re.compile(r"^\s*(#{1,6}|>|[-*+])\s+"), ""),
)


def _first_line(text: str | None, limit: int = 140) -> str:
	"""First non-empty line of an agent-written text, markdown/HTML stripped and
	truncated - rendered as plain text by the list."""
	for line in (text or "").splitlines():
		for pattern, repl in _MD_NOISE:
			line = pattern.sub(repl, line)
		line = " ".join(line.split())
		if line:
			return line if len(line) <= limit else line[:limit].rstrip() + "…"
	return ""


_FAILURES = {
	"resume_failed": "Approved, but the run couldn't continue",
	"no_start": "Couldn't start - the run never began",
	"cancelled": "Cancelled before it finished",
	"stopped": "Stopped before it finished",
	"stale": "Stopped responding",
	"empty": "Ended without a reply",
}


def _draft_line(r: dict) -> str:
	more = int(r.get("filebox_result_count") or 1) - 1
	line = f"Draft {r['filebox_result_doctype']} {r['filebox_result_name']} created"
	return line + (f" and {more} more" if more > 0 else "")


def _result(r: dict, wait, msg) -> tuple[str, str | None]:
	"""The row's one-line result + where it links (``/approvals/..`` in-app, or
	the Desk form of the draft)."""
	from urllib.parse import quote

	status = r["status"]
	if status == "needs_approval":
		n = int(r["pending_approvals"])
		line = f"{n} approval{'' if n == 1 else 's'} waiting" + (f": {wait.title}" if wait else "")
		if r.get("missing"):
			line = f"Needs approval — missing: {r['missing']}"
		if r.get("filebox_result_name"):
			line = f"{_draft_line(r)} · {line}"
		if not wait:
			return line, "/approvals"
		if wait.src == "pa":
			return line, f"/approvals?held={quote(wait.item, safe='')}"
		return line, f"/approvals/{quote(wait.item, safe='')}"
	if status == "draft_created":
		slug = frappe.scrub(r["filebox_result_doctype"]).replace("_", "-")
		line = _draft_line(r)
		if r.get("fail_code") and r["fail_code"] != "empty":
			line += " · ended with an error"
		return line, f"/app/{slug}/{quote(r['filebox_result_name'], safe='')}"
	if status == "failed":
		code = r.get("fail_code")
		if code == "send_failed":
			return (r.get("filebox_last_error") or "Couldn't start"), None
		if code == "error":
			reason = _first_line(msg.error if msg else "")
			return (f"Failed: {reason}" if reason else "Failed"), None
		return _FAILURES.get(code, "Failed"), None
	if status == "no_draft":
		return _first_line(msg.content if msg else ""), None
	return "", None


# Ladder inputs the list computes with but never returns.
_INTERNAL = (
	"lm_name",
	"fail_code",
	"filebox_result_doctype",
	"filebox_result_name",
	"filebox_result_count",
	"filebox_last_error",
)


def _attach_results(rows: list[dict], me: str) -> None:
	"""Post-page enrichment (never in the COUNT / Clear queries): the first
	waiting decision and the final reply are read for this page's rows only."""
	from jarvis.chat import held_writes

	waits: dict = {}
	need = [r["name"] for r in rows if r["status"] == "needs_approval"]
	if need:
		for w in frappe.db.sql(
			f"""SELECT w.conversation, w.item, w.title, w.src, w.needs_input FROM ({_PENDING_WAITS}) w
			WHERE w.conversation IN %(names)s ORDER BY w.creation ASC""",
			{"me": me, "names": need},
			as_dict=True,
		):
			waits.setdefault(w.conversation, w)
	msgs: dict = {}
	lm_names = [r["lm_name"] for r in rows if r["lm_name"] and r["status"] in ("failed", "no_draft")]
	if lm_names:
		for m in frappe.get_all(MSG, filters={"name": ["in", lm_names]}, fields=["name", "content", "error"]):
			msgs[m.name] = m
	for r in rows:
		wait = waits.get(r["name"])
		# Held on missing fields: "Needs approval — missing: A, B, C (+N more)"; the PWA
		# (no board) words it from ``missing`` itself.
		r["missing"] = (
			held_writes.missing_summary(held_writes.missing_labels(wait.needs_input))
			if wait and wait.src == "pa" and wait.needs_input
			else ""
		)
		r["result"], r["result_link"] = _result(r, wait, msgs.get(r["lm_name"]))
		for k in _INTERNAL:
			r.pop(k, None)


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
	Envelope ``{rows, total, has_more, start, page_length}``; each row carries
	``status``, a one-line ``result`` and its ``result_link``. Tagged users can
	view; delete/clear remain owner-only.

	Filters: ``status`` (processing|needs_approval|draft_created|failed|no_draft;
	legacy ``done`` / ``error`` still map), ``from_date`` / ``to_date``
	(YYYY-MM-DD, inclusive; ``to_date`` implemented as ``creation < to_date + 1
	day``). Search matches the title. Sort: ``creation`` (default desc) or ``title``."""
	from frappe.utils import add_days, getdate

	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, {"status", "from_date", "to_date"})

	params: dict = {**_ladder_params(me), "start": start, "page_length": pl}
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
		status = f["status"]
		if status not in _STATUSES and status not in _STATUS_ALIASES:
			frappe.throw("Invalid status filter")
		params["statuses"] = _STATUS_ALIASES.get(status, (status,))
		outer = "WHERE t.status IN %(statuses)s"

	order = _order_by(sort_field, sort_dir, _INBOUND_SORTABLE, "creation", "desc", prefix="t.")

	total = frappe.db.sql(f"SELECT COUNT(*) FROM ({inner}) t {outer}", params)[0][0]
	rows = frappe.db.sql(
		f"""SELECT t.name, t.title, t.creation, t.status, t.pending_approvals, t.behind_chat,
		t.lm_name, t.fail_code, t.filebox_result_doctype, t.filebox_result_name,
		t.filebox_result_count, t.filebox_last_error
		FROM ({inner}) t {outer}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)

	if rows:
		_attach_results(rows, me)
		# Attach the source File so the list can offer an inline preview. Each File-Box
		# conversation carries the dropped document as a File (drop_file, is_private=1);
		# pick the earliest by creation. get_all bypasses File perms, but the rows are
		# already scoped to conversations the caller may see, and the actual byte fetch
		# (iframe/img/read_file) stays permission-gated by Frappe's file handler.
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

		# The pinned skill, OWNER-ONLY: a tagged (non-owner) viewer must not learn
		# which skill the owner pinned. The owner=me filter returns only owned rows,
		# so a tagged row's pinned_skill stays None by construction.
		pin_rows = frappe.get_all(
			CONV,
			filters={"name": ["in", names], "owner": me},
			fields=["name", "filebox_pinned_skill"],
		)
		pins = {p["name"]: (p.get("filebox_pinned_skill") or None) for p in pin_rows}
		for r in rows:
			r["pinned_skill"] = pins.get(r["name"])

	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


# --------------------------------------------------------------------------- #
# FB-1 cascade delete (owner-gated; refuses while the run is live or a wiki
# note is still with a reviewer — orchestrator Q6, M8).
# --------------------------------------------------------------------------- #
def _wiki_open(alias: str) -> str:
	"""SQL predicate: conversation ``alias`` has a wiki note a reviewer still owes a
	decision or a landing on (Pending, or Approved but not Applied). The cascade
	would delete the proposal, so such a row is never cleared or deleted."""
	return (
		"EXISTS (SELECT 1 FROM `tabJarvis Approval Request` wa "
		f"WHERE wa.conversation = {alias}.name AND wa.source = 'File Box Wiki' "
		"AND (wa.status = 'Pending' OR (wa.status = 'Approved' "
		"AND COALESCE(wa.apply_status, 'Pending') <> 'Applied')))"
	)


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


def _delete_one(conversation: str, live: int | None = None) -> None:
	"""Owner-gated cascade delete of ONE File-Box conversation. Raises on refusal:
	not found / not permitted / not a File-Box row / still live / a wiki note
	still with a reviewer. All checks run BEFORE any delete, so a refusal never
	leaves a partial delete. "Live" is the ladder's own arm, so an orphan past the
	threshold (or a failed send) stays deletable; ``live`` is that arm when the
	caller already computed it."""
	me = frappe.session.user
	doc = frappe.get_doc(CONV, conversation)  # DoesNotExistError if missing
	if doc.owner != me:
		frappe.throw("Not permitted", frappe.PermissionError)
	if not doc.file_box:
		frappe.throw("Not a File Box conversation")
	if live is None:
		row = frappe.db.sql(
			f"SELECT t.live FROM ({_INBOUND_INNER.format(extra='AND c.name = %(one)s')}) t",
			{**_ladder_params(me), "one": conversation},
		)
		live = row[0][0] if row else 0
	if live:
		frappe.throw("Still processing — stop or wait for it to finish before deleting")
	if frappe.db.sql(
		f"SELECT 1 FROM `tabJarvis Conversation` c WHERE c.name = %s AND {_wiki_open('c')}", (conversation,)
	):
		frappe.throw(
			"A wiki note from this file is still awaiting review — try again once a reviewer has handled it"
		)
	if frappe.db.sql(
		f"SELECT 1 FROM `tabJarvis Conversation` c WHERE c.name = %s AND {_held_open('c')}", (conversation,)
	):
		frappe.throw(
			"This file is waiting on an approval — decide or skip it on the Approval Board before deleting"
		)
	_cascade(conversation)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def delete_inbound(conversation: str) -> dict:
	"""Owner-gated cascade delete of a File-Box conversation (FB-1)."""
	refuse_in_tool_dispatch()
	_delete_one(conversation)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def delete_inbound_bulk(conversations: str | list | None = None) -> dict:
	"""Bulk cascade delete (orchestrator Q4). Owner-gated per row, same cascade +
	refusals as ``delete_inbound``. Returns ``{deleted, skipped:[{conversation,
	reason}]}`` — a live/foreign row is skipped, not fatal."""
	refuse_in_tool_dispatch()
	raw = frappe.parse_json(conversations) if isinstance(conversations, str) else (conversations or [])
	convs = [str(c) for c in raw if c] if isinstance(raw, list) else []
	deleted = 0
	skipped: list[dict] = []
	for conv in convs:
		try:
			_delete_one(conv)
		except Exception as e:
			frappe.db.rollback()  # never commit a half-deleted row with the next one
			if isinstance(e, frappe.PermissionError):
				reason = "not permitted"
			elif isinstance(e, frappe.DoesNotExistError):
				reason = "not found"
			else:
				reason = str(e) or "error"
			skipped.append({"conversation": conv, "reason": reason})
			continue
		frappe.db.commit()
		deleted += 1
	return {"deleted": deleted, "skipped": skipped}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def clear_processed_inbound() -> dict:
	"""Bulk-delete the caller's OWN File-Box rows that finished with a result
	(``draft_created`` | ``no_draft``); failed, processing and needs-approval rows,
	and rows with a wiki note still with a reviewer, stay. Owner-only by design:
	the list SQL also surfaces rows merely DocShared to the caller, so the clear
	query pins ``c.owner = %(me)s`` via the ``extra`` hole — and ``_delete_one``
	re-checks ownership as the backstop."""
	refuse_in_tool_dispatch()
	me = frappe.session.user
	extra = f"AND c.owner = %(me)s AND NOT {_wiki_open('c')}"
	rows = frappe.db.sql(
		f"SELECT t.name, t.live FROM ({_INBOUND_INNER.format(extra=extra)}) t WHERE t.status IN %(clearable)s",
		{**_ladder_params(me), "clearable": _CLEARABLE},
		as_dict=True,
	)
	deleted = 0
	for r in rows:
		try:
			_delete_one(r.name, live=r.live)
		except Exception:
			# finished rows are never live; only a concurrent race fails.
			frappe.db.rollback()
			continue
		frappe.db.commit()
		deleted += 1
	return {"ok": True, "deleted": deleted}
