"""Approvals pane API: pending-decision queue + decide-and-resume.

The agent queues decisions it cannot take autonomously as ``Jarvis Approval Request`` rows (see the ocr-data-entry skill contract) and ends the
turn. The customer decides from the SPA Approvals pane (or the Desk
list); deciding posts the decision back into the linked conversation
through the normal ``send_message`` path, so the agent resumes the flow
in the same chat with full context - no separate notification plumbing.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import frappe

from jarvis._session import impersonate
from jarvis.permissions import (
	JARVIS_REVIEWER_ROLES,
	message_origin,
	refuse_in_tool_dispatch,
	require_jarvis_user,
	require_skill_reviewer,
)

APPROVAL = "Jarvis Approval Request"
# The `source` value of a HELD file-box wiki write-back proposal (review-before-
# landing). Kept in sync with jarvis.api.FILE_BOX_WIKI_SOURCE (a drift test in
# test_filebox asserts they match); duplicated here to avoid importing the heavy
# jarvis.api module at load time. Wiki-write rows are decided ONLY through the
# reviewer-gated endpoints below - never the customer decide/dismiss board.
FILE_BOX_WIKI_SOURCE = "File Box Wiki"


def _is_wiki_write(doc) -> bool:
	return (doc.get("source") or "") == FILE_BOX_WIKI_SOURCE


def _refuse_if_wiki_write(doc) -> None:
	"""A wiki-write proposal must transition ONLY through the reviewer-gated
	endpoints (SoD + the fenced replay live there). Refuse it on the customer
	decide/dismiss/restore path even for a System Manager - defense in depth so no
	path lands an org-wide wiki write without the reviewer controls."""
	if _is_wiki_write(doc):
		frappe.throw(
			"This is a wiki-write proposal - use the reviewer approve/reject controls.",
			frappe.PermissionError,
		)


def _tenant_reviewer_count() -> int:
	"""Distinct enabled tenant users holding a Jarvis reviewer role, EXCLUDING the
	framework Administrator/Guest. Drives the separation-of-duties single-reviewer
	fallback: when a tenant has exactly one reviewer, that reviewer may approve a
	wiki write they dropped (else it could never land)."""
	rows = frappe.db.sql(
		"""SELECT COUNT(DISTINCT hr.parent)
		FROM `tabHas Role` hr
		JOIN `tabUser` u ON u.name = hr.parent
		WHERE hr.parenttype = 'User' AND hr.role IN %(roles)s
		  AND u.enabled = 1 AND u.name NOT IN ('Administrator', 'Guest')""",
		{"roles": tuple(JARVIS_REVIEWER_ROLES)},
	)
	return int((rows and rows[0][0]) or 0)


def _decode_wiki_preview(payload: str | None) -> dict:
	"""Decode the held ``wiki_payload`` JSON into the human-readable page fields a
	reviewer reads before approving. Tolerant of a malformed/empty payload."""
	try:
		a = json.loads(payload or "{}")
	except Exception:
		a = {}
	if not isinstance(a, dict):
		a = {}
	return {
		"slug": a.get("slug") or "",
		"title": a.get("title") or "",
		"page_type": a.get("page_type") or "",
		"summary": a.get("summary") or "",
		"append_md": a.get("append_md") or a.get("replace_body_md") or "",
		"scope": a.get("scope") or "Org",
	}


def _parse_options(raw: str | None) -> list[str]:
	try:
		out = json.loads(raw or "[]")
		return [str(o) for o in out] if isinstance(out, list) else []
	except Exception:
		# The agent sometimes hands create_doc a real list, which Frappe
		# coerces to str(list) with single quotes - salvage it.
		try:
			import ast

			out = ast.literal_eval(raw or "[]")
			return [str(o) for o in out] if isinstance(out, list) else []
		except Exception:
			return []


def _may_act_on(conversation: str | None) -> bool:
	# SM, or the owner of the linked conversation.
	if "System Manager" in frappe.get_roles():
		return True
	if not conversation:
		return False
	return frappe.db.get_value("Jarvis Conversation", conversation, "owner") == frappe.session.user


@frappe.whitelist()
@require_jarvis_user
def list_approvals(status: str = "Pending", limit: int = 50) -> list[dict]:
	"""Approvals visible to the current user (owner of the linked
	conversation, or any System Manager)."""
	if status not in ("Pending", "Approved", "Rejected", "Answered", "Dismissed", "Decided", "All"):
		frappe.throw("Invalid status filter")
	if status == "All":
		filters = {}
	elif status == "Decided":
		filters = {"status": ["in", ["Approved", "Rejected"]]}
	else:
		filters = {"status": status}
	# Non-SM: filter by ownership IN the query so `limit` applies to the
	# caller's rows, not to a mixed page that later shrinks (and no N+1).
	if "System Manager" not in frappe.get_roles():
		my_convs = frappe.get_all(
			"Jarvis Conversation",
			filters={"owner": frappe.session.user},
			pluck="name",
		)
		if not my_convs:
			return []
		filters["conversation"] = ["in", my_convs]
	rows = frappe.get_all(
		APPROVAL,
		filters=filters,
		fields=[
			"name",
			"title",
			"status",
			"document_type",
			"question",
			"context_md",
			"options",
			"conversation",
			"ref_doctype",
			"ref_name",
			"decision",
			"decided_by",
			"decided_at",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=int(limit),
	)
	for r in rows:
		r["options"] = _parse_options(r.get("options"))
	conversation_names = [r.get("conversation") for r in rows if r.get("conversation")]
	conversation_origins = {}
	if conversation_names:
		conversation_origins = {
			r.name: r.origin_page or ""
			for r in frappe.get_all(
				"Jarvis Conversation",
				filters={"name": ["in", conversation_names]},
				fields=["name", "origin_page"],
			)
		}
	for r in rows:
		r["origin_page"] = conversation_origins.get(r.get("conversation"), "")
	return rows


# --------------------------------------------------------------------------- #
# Paginated list (frozen envelope + facets) —
# chat-features-page-migration-design §2.5. ADDITIVE: list_approvals (above)
# STAYS (deprecated). Non-SM scoping is a pair of correlated EXISTS probes —
# owner of the linked conversation OR an approval-level DocShare read grant
# (tagging, docmeta_api.toggle_share) — not an IN(all my convs) list, which
# explodes at 1000s of conversations, and not a JOIN, which would duplicate a
# row that matches both arms.
# --------------------------------------------------------------------------- #
_APPROVALS_SORTABLE = {"creation": "creation", "status": "status", "document_type": "document_type"}
_APPROVALS_FILTERS = {"status", "document_type", "conversation"}
# "Answered" = a chat-sourced ask the user answered IN CHAT (chat_asks.
# resolve_on_user_message); it stays out of "Decided" (Approved/Rejected are
# board decisions) and out of the Pending badge, but is filterable for audit.
# "Dismissed" = cleared off the board with no action (dismiss_approval) —
# reversible, never resumes the agent; also out of Decided and the badge.
_APPROVAL_STATUSES = {"Pending", "Approved", "Rejected", "Answered", "Dismissed", "Decided", "All"}


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
def list_approvals_page(
	search: str = "",
	filters: str | dict | None = None,
	sort_field: str = "",
	sort_dir: str = "",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""Approvals visible to the caller (SM: all; non-SM: rows whose conversation
	they own, OR that are tagged to them via a DocShare read grant on the
	approval itself), server-side search/filter/sort/paginate + a
	``document_type`` facet for the triage tabs. Envelope ``{rows, total,
	has_more, start, page_length, facets}`` (+ ``awaiting_reply`` on the first
	page only — see ``_awaiting_reply``). Each row carries ``source`` ("File
	Box"|"Chat"; NULL rows predate the field and read as File Box) and
	``shared`` (0|1):
	for a non-SM caller it is 1 when they do NOT own the linked conversation
	(the row is visible only via a DocShare tag), else 0; for SM always 0.
	Tagged users VIEW here; only the conversation owner (or SM) ACTS — see
	``get_approval``/``decide``."""
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)
	f = _load_filters(filters, _APPROVALS_FILTERS)
	is_sm = "System Manager" in frappe.get_roles()

	from_sql = "`tabJarvis Approval Request` a"

	params: dict = {"me": me, "start": start, "page_length": pl}

	# `base` applies to BOTH the main query and the facet query; the
	# document_type filter applies ONLY to the main query (facets drop it).
	base: list[str] = []
	# Wiki-write proposals live on the reviewer lane only (SoD) - keep them OFF
	# the customer decide board and its facet/badge counts. NULL-safe so legacy
	# rows (source NULL = File Box) still show.
	params["wiki_src"] = FILE_BOX_WIKI_SOURCE
	base.append("(a.source IS NULL OR a.source <> %(wiki_src)s)")
	if not is_sm:
		# Owner OR tagged: the caller owns the linked conversation, or holds a
		# DocShare read grant on the approval (docmeta_api.toggle_share /
		# assignment). EXISTS, not JOIN: a row matching BOTH arms must still
		# appear exactly once in rows/total/facets.
		base.append(
			"(EXISTS (SELECT 1 FROM `tabJarvis Conversation` c "
			"WHERE c.name = a.conversation AND c.owner = %(me)s) "
			"OR EXISTS (SELECT 1 FROM `tabDocShare` ds "
			"WHERE ds.share_doctype = 'Jarvis Approval Request' "
			"AND ds.share_name = a.name "
			"AND ds.user = %(me)s AND ds.`read` = 1))"
		)
	status = f.get("status") or "Pending"
	if status not in _APPROVAL_STATUSES:
		frappe.throw("Invalid status filter")
	if status == "Decided":
		base.append("a.status IN ('Approved', 'Rejected')")
	elif status != "All":
		params["status"] = status
		base.append("a.status = %(status)s")
	if "conversation" in f:
		params["conv"] = f["conversation"]
		base.append("a.conversation = %(conv)s")
	if search:
		params["q"] = f"%{_lk(search)}%"
		base.append("(a.title LIKE %(q)s OR a.question LIKE %(q)s OR a.ref_name LIKE %(q)s)")

	doc_cond = None
	if "document_type" in f:
		dt = f["document_type"]
		if dt == "Unclassified":
			doc_cond = "TRIM(COALESCE(a.document_type, '')) = ''"
		else:
			params["dt"] = dt
			doc_cond = "a.document_type = %(dt)s"

	main_where = " AND ".join(base + ([doc_cond] if doc_cond else [])) or "1=1"
	base_where = " AND ".join(base) or "1=1"
	order = _order_by(sort_field, sort_dir, _APPROVALS_SORTABLE, "creation", "desc", prefix="a.")

	total = frappe.db.sql(f"SELECT COUNT(*) FROM {from_sql} WHERE {main_where}", params)[0][0]
	# `shared` (contract): non-SM rows the caller sees WITHOUT owning the linked
	# conversation (i.e. only via a DocShare tag) carry 1; SM sees all rows by
	# role, so shared is hardcoded 0 there.
	shared_expr = (
		"0 AS shared"
		if is_sm
		else "(NOT EXISTS (SELECT 1 FROM `tabJarvis Conversation` c "
		"WHERE c.name = a.conversation AND c.owner = %(me)s)) AS shared"
	)
	rows = frappe.db.sql(
		f"""SELECT a.name, a.title, a.status, a.source, a.document_type, a.question,
		a.context_md, a.options, a.conversation, a.ref_doctype, a.ref_name,
		a.decision, a.decided_by, a.decided_at, a.creation,
		COALESCE((SELECT oc.origin_page FROM `tabJarvis Conversation` oc
		          WHERE oc.name = a.conversation), '') AS origin_page,
		{shared_expr}
		FROM {from_sql}
		WHERE {main_where}
		ORDER BY {order}
		LIMIT %(page_length)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)
	for r in rows:
		r["options"] = _parse_options(r.get("options"))
		r["shared"] = int(r.get("shared") or 0)
		# NULL = a row from before the source field existed = File Box.
		r["source"] = r.get("source") or "File Box"

	facet_rows = frappe.db.sql(
		f"""SELECT COALESCE(NULLIF(TRIM(a.document_type), ''), 'Unclassified') AS dtv,
		COUNT(*) AS n
		FROM {from_sql}
		WHERE {base_where}
		GROUP BY dtv
		ORDER BY n DESC""",
		params,
		as_dict=True,
	)
	facets = {"document_type": [{"value": x.dtv, "count": x.n} for x in facet_rows]}

	out = {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
		"facets": facets,
	}
	# Awaiting-reply lane (notify-approvals design Part 2): prose questions
	# never create rows, so the board derives them. First page only — the
	# lane sits above the rail and pagination never re-renders it.
	if start == 0:
		out["awaiting_reply"] = _awaiting_reply(me)
	return out


def _awaiting_reply(me: str) -> list[dict]:
	"""Conversations OWNED by ``me`` whose ball is in the user's court:
	the last message is an assistant turn that ENDED cleanly (not
	streaming/recovering/errored — the filebox.py SQL-ladder semantics)
	and looks like a question (carries a ```jarvis-ask fence, or a "?" in
	the last 200 chars), excluding conversations that already have a
	Pending chat-sourced approval row (those ARE board rows). Newest
	first, capped at 10. Rows: {conversation, title, question_excerpt,
	last_at}."""
	from jarvis.chat.chat_asks import question_excerpt

	rows = frappe.db.sql(
		"""SELECT c.name AS conversation, c.title, c.origin_page,
		m.content, m.creation AS last_at
		FROM `tabJarvis Conversation` c
		JOIN (SELECT mm.conversation, MAX(mm.seq) mseq
		      FROM `tabJarvis Chat Message` mm
		      JOIN `tabJarvis Conversation` mc
		        ON mc.name = mm.conversation AND mc.owner = %(me)s
		      GROUP BY mm.conversation) x ON x.conversation = c.name
		JOIN `tabJarvis Chat Message` m
		  ON m.conversation = c.name AND m.seq = x.mseq
		WHERE c.owner = %(me)s
		AND c.status != 'Archived'
		AND m.role = 'assistant'
		AND m.streaming = 0
		AND COALESCE(m.recovering, 0) = 0
		AND COALESCE(m.error, '') = ''
		-- a structured jarvis-ask fence always qualifies (a deliberate agent
		-- question). A bare "?" only qualifies with real work depth — a tool
		-- call or a genuine back-and-forth (>=2 user turns) — otherwise every
		-- opener greeting ("...what would you like to work on?") floods the lane.
		AND (
		    m.content LIKE %(fence)s
		    OR (
		        RIGHT(m.content, 200) LIKE %(qmark)s
		        AND (
		            EXISTS (SELECT 1 FROM `tabJarvis Chat Message` tm
		                    WHERE tm.conversation = c.name AND tm.role = 'tool')
		            OR (SELECT COUNT(*) FROM `tabJarvis Chat Message` um
		                WHERE um.conversation = c.name AND um.role = 'user') >= 2
		        )
		    )
		)
		AND NOT EXISTS (SELECT 1 FROM `tabJarvis Approval Request` ar
		                WHERE ar.conversation = c.name
		                AND ar.status = 'Pending' AND ar.source = 'Chat')
		ORDER BY m.creation DESC
		LIMIT 10""",
		{"me": me, "fence": "%```jarvis-ask%", "qmark": "%?%"},
		as_dict=True,
	)
	return [
		{
			"conversation": r.conversation,
			"title": r.title or "",
			"origin_page": r.origin_page or "",
			"question_excerpt": question_excerpt(r.content),
			"last_at": str(r.last_at or ""),
		}
		for r in rows
	]


@frappe.whitelist()
@require_jarvis_user
def get_approval(name: str) -> dict:
	"""One approval with every field the detail page renders (DESIGN-V3 §8.3).

	Read gate: ``_may_act_on`` (System Manager or linked-conversation owner) —
	additionally a DocShare-read holder (an assignee, §13 risk 8 / §14 F1) may
	READ but gets ``can_act=0`` so the UI hides the decide controls. ``can_act``
	mirrors what ``decide()`` would permit; the UI combines it with status."""
	doc = frappe.get_doc(APPROVAL, name)
	can_act = _may_act_on(doc.conversation)
	if not can_act and not frappe.db.exists(
		"DocShare",
		{"share_doctype": APPROVAL, "share_name": name, "user": frappe.session.user, "read": 1},
	):
		frappe.throw("Not permitted", frappe.PermissionError)
	return {
		"name": doc.name,
		"title": doc.title,
		"status": doc.status,
		# .get(): tolerant of a not-yet-migrated site; NULL = File Box.
		"source": doc.get("source") or "File Box",
		"document_type": doc.document_type or "",
		"conversation": doc.conversation,
		"origin_page": (
			frappe.db.get_value("Jarvis Conversation", doc.conversation, "origin_page") or ""
			if doc.conversation
			else ""
		),
		"question": doc.question,
		"context_md": doc.context_md or "",
		"options": _parse_options(doc.options),
		"ref_doctype": doc.ref_doctype or "",
		"ref_name": doc.ref_name or "",
		"decision": doc.decision or "",
		"decided_by": doc.decided_by or "",
		"decided_by_name": (
			(frappe.db.get_value("User", doc.decided_by, "full_name") or doc.decided_by)
			if doc.decided_by
			else ""
		),
		"decided_at": str(doc.decided_at or ""),
		"creation": str(doc.creation or ""),
		"owner": doc.owner,
		"can_act": int(can_act),
	}


@frappe.whitelist()
@require_jarvis_user
def pending_count() -> int:
	# Scoped like list_approvals_page: non-SM users count rows whose
	# conversation they own OR that carry an approval-level DocShare read grant
	# for them (tagging) — the same EXISTS semantics as the list, never by
	# materializing the pending list. EXISTS also keeps the COUNT exact when a
	# row matches both arms.
	# Exclude wiki-write proposals (reviewer lane, not the customer badge).
	# Raw SQL, NULL-safe: a `!=` filter would drop legacy rows (source NULL).
	if "System Manager" in frappe.get_roles():
		return frappe.db.sql(
			"""SELECT COUNT(*) FROM `tabJarvis Approval Request`
			WHERE status = 'Pending' AND (source IS NULL OR source <> %(wiki_src)s)""",
			{"wiki_src": FILE_BOX_WIKI_SOURCE},
		)[0][0]
	return frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabJarvis Approval Request` a
		WHERE a.status = 'Pending'
		AND (a.source IS NULL OR a.source <> %(wiki_src)s)
		AND (EXISTS (SELECT 1 FROM `tabJarvis Conversation` c
		             WHERE c.name = a.conversation AND c.owner = %(me)s)
		     OR EXISTS (SELECT 1 FROM `tabDocShare` ds
		                WHERE ds.share_doctype = 'Jarvis Approval Request'
		                AND ds.share_name = a.name
		                AND ds.user = %(me)s AND ds.`read` = 1))""",
		{"me": frappe.session.user, "wiki_src": FILE_BOX_WIKI_SOURCE},
	)[0][0]


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def decide(name: str, decision: str, approve: int = 1) -> dict:
	"""Record the decision and resume the linked conversation.

	``decision`` is the human's answer (an option or free text). The
	resume message is a plain user message through send_message, so the
	agent sees it exactly like any chat turn - same session, same context.
	"""
	refuse_in_tool_dispatch()
	decision = (decision or "").strip()
	if not decision:
		frappe.throw("Decision text is required")
	doc = frappe.get_doc(APPROVAL, name)
	_refuse_if_wiki_write(doc)
	if not _may_act_on(doc.conversation):
		frappe.throw("Not permitted", frappe.PermissionError)
	if doc.status != "Pending":
		frappe.throw(f"Approval {name} is already {doc.status}")
	new_status = "Approved" if int(approve) else "Rejected"
	# Conditional flip closes the double-decide race: only ONE concurrent
	# caller wins the Pending -> decided transition.
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status=%s, decision=%s, decided_by=%s, decided_at=%s
		where name=%s and status='Pending'""",
		(new_status, decision, frappe.session.user, frappe.utils.now_datetime(), name),
	)
	if not frappe.db.sql(
		"select 1 from `tabJarvis Approval Request` where name=%s and status=%s and decided_by=%s",
		(name, new_status, frappe.session.user),
	):
		frappe.throw(f"Approval {name} was decided concurrently")
	frappe.db.commit()
	doc.reload()
	# fire the trace-comment hook (db update bypasses on_update)
	doc.run_method("on_update")

	resumed = False
	if doc.conversation and frappe.db.exists("Jarvis Conversation", doc.conversation):
		from jarvis.chat.api import send_message

		verdict = "APPROVED" if doc.status == "Approved" else "REJECTED"
		msg = (
			f"[Approval {doc.name} - {doc.title}] {verdict}: {decision}\n"
			f"Continue the flow with this decision; do not re-ask."
		)
		# File-Box conversations: re-attach the source document. Attachments
		# ride only the message they were sent with, so a resumed turn can
		# no longer SEE the pages - observed live: the agent (correctly)
		# refused to draft rather than fabricate lines it couldn't see.
		# Chat-sourced asks (chat_asks.materialize_from_turn) have no source
		# document to re-show - the question came from prose, and blindly
		# re-attaching whatever File happens to hang off the conversation
		# would bolt an unrelated upload onto the answer. Guard on source
		# (NULL = a pre-field row = File Box, so legacy rows keep the
		# re-attach).
		attachments = None
		if (doc.get("source") or "File Box") != "Chat":
			f = frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": "Jarvis Conversation",
					"attached_to_name": doc.conversation,
				},
				fields=["file_url", "file_name"],
				order_by="creation desc",
				limit_page_length=1,
			)
			if f:
				attachments = json.dumps([{"file_url": f[0].file_url, "file_name": f[0].file_name}])
		# The decision is durably recorded above; a resume failure must
		# not 500 the endpoint (the SPA would re-show a decided row).
		# send_message is owner-only (SEC-002). A System Manager may decide
		# another user's approval (``_may_act_on`` gates that above), so the
		# resume runs AS the conversation owner - the same identity hinge
		# agents_api.run_agent_now uses. The decision itself stays attributed
		# to the approver via decided_by on the Approval Request row.
		from jarvis.chat.agent_scheduler import _valid_owner

		conv_owner = frappe.db.get_value("Jarvis Conversation", doc.conversation, "owner")
		original_user = frappe.session.user
		# Fail-closed identity guard (mirrors agents_api.run_agent_now): never
		# resume AS Administrator / Guest / a disabled user on someone else's
		# behalf - that would run an unattended agent turn with elevated
		# rights. A self-owned resume (owner == approver) always proceeds.
		switch_to = conv_owner if (conv_owner and conv_owner != original_user) else None
		if switch_to and not _valid_owner(switch_to):
			frappe.log_error(
				title="approval resume skipped (owner not an eligible run identity)",
				message=(
					f"Approval {doc.name}: conversation owner {switch_to!r} is not an "
					f"eligible run identity (Administrator/Guest/disabled); the decision "
					f"is recorded but the turn was not resumed."
				),
			)
		else:
			try:
				# impersonate is session-safe (a bare frappe.set_user in this
				# HTTP path would gut the approver's cookie session and log them
				# out) and no-ops when switch_to is None (self-owned resume).
				# delegated_send() marks this as a trusted server re-entry so the
				# resume clears send_message's Jarvis-access gate and the now
				# role-gated Message create perm even when the conversation owner
				# does not hold the Jarvis User role.
				from jarvis.permissions import delegated_send

				with impersonate(switch_to), delegated_send(), message_origin("board_answer"):
					res = send_message(conversation=doc.conversation, message=msg, attachments=attachments)
				resumed = bool(res.get("ok"))
			except Exception:
				# impersonate's finally already restored the approver, so the
				# Error Log is attributed to them, not the conversation owner.
				frappe.log_error(title="approval resume failed", message=frappe.get_traceback())
	return {"ok": True, "status": doc.status, "resumed": resumed}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def dismiss_approval(name: str) -> dict:
	"""Clear a Pending request off the board WITHOUT acting on it — no
	verdict, no chat resume. For requests the user simply doesn't want to
	handle (a stale ask, a question they'll ignore). Terminal but reversible
	via ``restore_approval``; unlike Reject it never tells the agent anything.
	"""
	refuse_in_tool_dispatch()
	doc = frappe.get_doc(APPROVAL, name)
	_refuse_if_wiki_write(doc)
	if not _may_act_on(doc.conversation):
		frappe.throw("Not permitted", frappe.PermissionError)
	if doc.status != "Pending":
		frappe.throw(f"Approval {name} is already {doc.status}")
	# conditional flip: only one concurrent caller wins Pending -> Dismissed
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status='Dismissed', decision=%s, decided_by=%s, decided_at=%s
		where name=%s and status='Pending'""",
		("(dismissed - no action taken)", frappe.session.user, frappe.utils.now_datetime(), name),
	)
	if not frappe.db.sql(
		"select 1 from `tabJarvis Approval Request` where name=%s and status='Dismissed'",
		(name,),
	):
		frappe.throw(f"Approval {name} was decided concurrently")
	frappe.db.commit()
	return {"ok": True, "status": "Dismissed"}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def restore_approval(name: str) -> dict:
	"""Put a Dismissed request back on the board (Pending). The undo for an
	accidental dismiss."""
	refuse_in_tool_dispatch()
	doc = frappe.get_doc(APPROVAL, name)
	_refuse_if_wiki_write(doc)
	if not _may_act_on(doc.conversation):
		frappe.throw("Not permitted", frappe.PermissionError)
	if doc.status != "Dismissed":
		frappe.throw(f"Only a dismissed request can be restored (this is {doc.status})")
	frappe.db.set_value(
		APPROVAL,
		name,
		{"status": "Pending", "decision": None, "decided_by": None, "decided_at": None},
	)
	frappe.db.commit()
	return {"ok": True, "status": "Pending"}


# --------------------------------------------------------------------------- #
# Wiki proposal digest (PR-1 §2): an HMAC over the exact stored (name,
# conversation, wiki_payload), minted post-name in the propose transaction and
# verified before any landing, so a forged or swapped proposal never lands.
# --------------------------------------------------------------------------- #
_WIKI_DIGEST_CONTEXT = b"jarvis-wiki-v1"
_WIKI_TAMPERED = "This wiki proposal was changed after it was proposed - reject it."
_WIKI_CHANGED = "This proposal changed since you opened it - reload and review it again."


def wiki_digest(name: str, conversation: str | None, payload: str | None) -> str:
	from frappe.utils.password import get_encryption_key

	key = hmac.new(get_encryption_key().encode(), _WIKI_DIGEST_CONTEXT, hashlib.sha256).digest()
	msg = json.dumps([name or "", conversation or "", payload or ""], separators=(",", ":"))
	return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def wiki_digest_ok(name: str, conversation: str | None, payload: str | None, digest: str | None) -> bool:
	return bool(digest) and hmac.compare_digest(wiki_digest(name, conversation, payload), str(digest))


def _refuse_unless_digest_ok(name: str) -> dict:
	"""Read the stored proposal and refuse it unless its digest verifies."""
	row = frappe.db.get_value(APPROVAL, name, ["conversation", "wiki_payload", "wiki_digest"], as_dict=True)
	if not (row and wiki_digest_ok(name, row.conversation, row.wiki_payload, row.wiki_digest)):
		frappe.throw(_WIKI_TAMPERED, frappe.PermissionError)
	return row


# --------------------------------------------------------------------------- #
# Wiki write-back review lane (review-before-landing) — reviewer-gated.
#
# A file-box run's ``update_wiki`` is HELD as a Pending proposal (source
# ``File Box Wiki``, api._propose_file_box_wiki_write) instead of landing. These
# endpoints are the ONLY way that proposal transitions: a Jarvis reviewer (NOT
# the dropper — separation of duties) approves it, and only then does the fenced
# write replay through api._file_box_wiki_write under the DROPPER's provenance.
# Reviewer visibility is by ROLE (require_skill_reviewer), independent of who
# owns the linked conversation — the customer decide board never shows these.
# --------------------------------------------------------------------------- #
def _wiki_proposal_or_throw(name: str):
	"""Load a proposal, reviewer-gated, asserting it really is a wiki-write row."""
	require_skill_reviewer()
	doc = frappe.get_doc(APPROVAL, name)
	if not _is_wiki_write(doc):
		frappe.throw(f"{name} is not a wiki-write proposal", frappe.DoesNotExistError)
	return doc


def _dropper_of(doc) -> str | None:
	"""The file-box dropper = owner of the linked conversation (the AR owner is
	stamped to them too, but the conversation is the authoritative source)."""
	if doc.conversation:
		return frappe.db.get_value("Jarvis Conversation", doc.conversation, "owner")
	return doc.owner


def _sod_blocks_self_approval(dropper: str | None) -> bool:
	"""Strict separation of duties WITH a single-reviewer fallback: the dropper may
	approve their OWN wiki write only when they are the tenant's sole reviewer."""
	return frappe.session.user == dropper and _tenant_reviewer_count() > 1


@frappe.whitelist()
@require_jarvis_user
def list_wiki_write_proposals(status: str = "Actionable", start: int = 0, page_length: int = 20) -> dict:
	"""Wiki-write proposals for the reviewer lane, newest first. Reviewer-gated:
	these are org-wide wiki writes, visible to the reviewer set regardless of who
	dropped the file. Each row carries a decoded ``preview``, ``can_approve`` (the
	SoD gate, so the UI hides Approve where it would be refused) and ``needs_retry``
	(an approved write that did not land - the reconciliation affordance).

	``status`` = ``Actionable`` (default) returns the rows a reviewer must act on:
	Pending (approve/reject) OR Approved-but-not-landed (retry). The explicit
	``Pending``/``Approved``/``Rejected``/``All`` values scope to that status.
	Envelope ``{rows, total, has_more, start, page_length}``."""
	require_skill_reviewer()
	if status not in ("Actionable", "Pending", "Approved", "Rejected", "All"):
		frappe.throw("Invalid status filter")
	start, pl = _clamp_page(start, page_length)
	params: dict = {"src": FILE_BOX_WIKI_SOURCE, "start": start, "pl": pl}
	where = ["a.source = %(src)s"]
	if status == "Actionable":
		# Needs a reviewer's hand: awaiting decision, OR approved but the fenced
		# write has not landed (Failed, or an interrupted Pending) — re-drivable.
		where.append(
			"(a.status = 'Pending' OR (a.status = 'Approved' "
			"AND COALESCE(a.apply_status, 'Pending') <> 'Applied'))"
		)
	elif status != "All":
		params["status"] = status
		where.append("a.status = %(status)s")
	where_sql = " AND ".join(where)
	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabJarvis Approval Request` a WHERE {where_sql}", params)[
		0
	][0]
	rows = frappe.db.sql(
		f"""SELECT a.name, a.title, a.status, a.apply_status, a.apply_reason,
		a.wiki_payload, a.wiki_digest, a.ref_name, a.conversation, a.owner, a.decision,
		a.decided_by, a.decided_at, a.creation
		FROM `tabJarvis Approval Request` a
		WHERE {where_sql}
		ORDER BY a.creation DESC
		LIMIT %(pl)s OFFSET %(start)s""",
		params,
		as_dict=True,
	)
	me = frappe.session.user
	multi = _tenant_reviewer_count() > 1
	for r in rows:
		r["preview"] = _decode_wiki_preview(r.pop("wiki_payload", None))
		# The AR owner was stamped to the dropper (conversation owner) at propose
		# time - use it directly to avoid an N+1 conversation lookup per row. This
		# feeds only the display + the can_approve HINT; approve_wiki_write re-checks
		# SoD against the LIVE conversation owner server-side.
		dropper = r.get("owner")
		r["dropper"] = dropper
		r["apply_status"] = r.get("apply_status") or "Pending"
		# Approve is offered only for a still-Pending row the caller may land.
		r["can_approve"] = int(r["status"] == "Pending" and not (me == dropper and multi))
		# An approved write that did not land (Failed / interrupted) — the panel
		# offers Retry; any reviewer may re-drive (the approve decision already stood).
		r["needs_retry"] = int(r["status"] == "Approved" and r["apply_status"] != "Applied")
		r["decided_at"] = str(r.get("decided_at") or "")
		r["creation"] = str(r.get("creation") or "")
	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


def _land_and_record(name: str, doc, dropper: str | None) -> dict:
	"""Replay the held fenced write EXACTLY ONCE and record its outcome in
	apply_status / apply_reason. Shared by approve (first landing) and retry
	(re-drive an interrupted / transiently-refused landing).

	Exactly-once under concurrency (two reviewers, a double-click, or a retry
	racing an in-flight approve) is enforced by an atomic LANDING-CLAIM: an
	``UPDATE ... SET apply_status='Applying' WHERE apply_status IN ('Pending',
	'Failed')`` with a per-caller token marker (the ``decide()`` claim-first
	idiom). Only one caller wins the row out of a re-drivable state; the loser
	sees it and returns WITHOUT a second append to the shared wiki. The claim is
	held under the AR row lock through the funnel (no intermediate commit), so a
	concurrent lander blocks here, then re-reads the committed final state and
	finds nothing to claim. On ANY failure - a funnel deadlock (which rolls back
	inside the executor) OR a raise past the executor's guard (its audit sinks sit
	outside the try/except) - request teardown rolls back the WHOLE txn including
	the claim, so the row reverts to Pending/Failed (re-drivable), never stuck in
	'Applying' and never a false 'Applied'. The final status write is itself
	guarded on the token so a rolled-back claim can't clobber a concurrent
	winner's outcome. Import api lazily (heavy module, avoids an import cycle."""
	token = frappe.generate_hash(length=16)
	# Atomic claim: win the transition out of a re-drivable state. apply_reason
	# briefly carries the token as the winner-marker; overwritten with the real
	# reason (or NULL) below. COALESCE keeps the re-drivable set identical to the
	# Actionable filter's, so a (today impossible) NULL apply_status can't show as
	# needs_retry yet fail every claim - a stuck-looking row.
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set apply_status='Applying', apply_reason=%s
		where name=%s and COALESCE(apply_status, 'Pending') in ('Pending', 'Failed')""",
		(token, name),
	)
	if not frappe.db.sql(
		"""select 1 from `tabJarvis Approval Request`
		where name=%s and apply_status='Applying' and apply_reason=%s""",
		(name, token),
	):
		# Lost the landing race (another approve/retry holds the claim) or the row
		# is already Applied - do NOT append to the shared wiki a second time.
		frappe.db.rollback()
		cur = frappe.db.get_value("Jarvis Approval Request", name, "apply_status")
		applied = cur == "Applied"
		return {
			"ok": applied,
			"name": name,
			"applied": applied,
			"apply_status": cur,
			"reason": None if applied else "another reviewer is landing this write",
		}
	# Hold the claim under the row lock through the funnel (no commit here). Land
	# what the locked row holds, and only if its digest verifies (the endpoints'
	# own check ran before the claim).
	from jarvis import api as _api

	try:
		row = _refuse_unless_digest_ok(name)
	except frappe.PermissionError:
		frappe.db.rollback()
		raise
	try:
		args = json.loads(row.wiki_payload)
	except Exception:
		args = {}
	if not isinstance(args, dict):
		args = {}
	result = _api._file_box_wiki_write(args, row.conversation, user=dropper, provenance="reviewer_approved")
	applied = bool(result.get("ok"))
	# Guarded on the token: if the executor's own rollback (a funnel deadlock)
	# discarded our claim, this matches 0 rows and we do NOT clobber whatever a
	# concurrent winner recorded; the row stays re-drivable.
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set apply_status=%s, apply_reason=%s
		where name=%s and apply_status='Applying' and apply_reason=%s""",
		(
			"Applied" if applied else "Failed",
			None if applied else (str(result.get("reason") or "refused"))[:140],
			name,
			token,
		),
	)
	frappe.db.commit()
	return {
		"ok": True,
		"name": name,
		"applied": applied,
		"apply_status": "Applied" if applied else "Failed",
		"reason": None if applied else result.get("reason"),
	}


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def approve_wiki_write(name: str, expected_digest: str | None = None) -> dict:
	"""Reviewer approves a held wiki proposal; the fenced write then LANDS.

	Gate: ``require_skill_reviewer`` + separation of duties (a reviewer OTHER than
	the dropper, with a single-reviewer fallback for a solo-admin tenant). Race:
	claim-first — win the Pending->Approved flip atomically and COMMIT before any
	side effect, so two concurrent approvers can't both execute the write. Then
	replay the write through the SAME fenced funnel as the auto path
	(``api._file_box_wiki_write``) under the DROPPER's provenance (the session is
	the reviewer, but the page belongs to the dropper's file-box namespace), and
	record the outcome in ``apply_status`` / ``apply_reason``.

	``expected_digest`` (M14) is the digest of the proposal the reviewer read; a
	refreshed or swapped proposal no longer matches it and is refused."""
	refuse_in_tool_dispatch()
	doc = _wiki_proposal_or_throw(name)
	if doc.status != "Pending":
		frappe.throw(f"Proposal {name} is already {doc.status}")
	if not doc.get("wiki_payload"):
		frappe.throw("Proposal has no held wiki payload to apply")
	row = _refuse_unless_digest_ok(name)
	if expected_digest and not hmac.compare_digest(str(expected_digest), str(row.wiki_digest)):
		frappe.throw(_WIKI_CHANGED, frappe.PermissionError)
	dropper = _dropper_of(doc)
	if _sod_blocks_self_approval(dropper):
		frappe.throw(
			"A reviewer other than the person who dropped the file must approve this "
			"wiki write (separation of duties).",
			frappe.PermissionError,
		)
	me = frappe.session.user
	# Claim-first: win the transition atomically before the (non-transactional
	# from the AR's view) fenced write, so a double-approve executes the write once.
	# Pinned to the verified digest: a refresh between the check and here re-mints it.
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status='Approved', decision=%s, decided_by=%s, decided_at=%s
		where name=%s and status='Pending' and wiki_digest=%s""",
		("(approved - wiki write applied)", me, frappe.utils.now_datetime(), name, row.wiki_digest),
	)
	if not frappe.db.sql(
		"select 1 from `tabJarvis Approval Request` where name=%s and status='Approved' and decided_by=%s",
		(name, me),
	):
		if frappe.db.get_value(APPROVAL, name, "status") == "Pending":
			frappe.throw(_WIKI_CHANGED, frappe.PermissionError)
		frappe.throw(f"Proposal {name} was decided concurrently")
	frappe.db.commit()
	return _land_and_record(name, doc, dropper)


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def retry_wiki_write(name: str) -> dict:
	"""Re-drive the landing of an already-APPROVED proposal whose write did not
	succeed (apply_status Pending - interrupted between claim and execute - or
	Failed - a transient funnel error). Reviewer-gated; the approve decision +
	its SoD check already stand, so this only re-runs the mechanical write. The
	reconciliation path for the one window approve can't cover atomically."""
	refuse_in_tool_dispatch()
	doc = _wiki_proposal_or_throw(name)
	if doc.status != "Approved":
		frappe.throw(f"Only an approved proposal can be retried (this is {doc.status})")
	if (doc.get("apply_status") or "Pending") == "Applied":
		frappe.throw(f"Proposal {name} already landed")
	if not doc.get("wiki_payload"):
		frappe.throw("Proposal has no held wiki payload to apply")
	_refuse_unless_digest_ok(name)
	return _land_and_record(name, doc, _dropper_of(doc))


@frappe.whitelist(methods=["POST"])
@require_jarvis_user
def reject_wiki_write(name: str) -> dict:
	"""Reviewer rejects a held wiki proposal; nothing is written. Reviewer-gated.
	No SoD gate on reject — declining to land a write is always safe, and a solo
	reviewer must be able to clear their own proposal."""
	refuse_in_tool_dispatch()
	doc = _wiki_proposal_or_throw(name)
	if doc.status != "Pending":
		frappe.throw(f"Proposal {name} is already {doc.status}")
	me = frappe.session.user
	frappe.db.sql(
		"""update `tabJarvis Approval Request`
		set status='Rejected', decision=%s, decided_by=%s, decided_at=%s
		where name=%s and status='Pending'""",
		("(rejected - not written to the wiki)", me, frappe.utils.now_datetime(), name),
	)
	if not frappe.db.sql(
		"select 1 from `tabJarvis Approval Request` where name=%s and status='Rejected' and decided_by=%s",
		(name, me),
	):
		frappe.throw(f"Proposal {name} was decided concurrently")
	frappe.db.commit()
	return {"ok": True, "name": name, "status": "Rejected"}
