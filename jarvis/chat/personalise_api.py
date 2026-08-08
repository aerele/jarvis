"""Personalise-tab SPA endpoints (Skills-area rework, DESIGN.md sections 2, 4,
5c, 6, 6b).

Sibling of ``voice_notes_api.py`` (the Business tab this replaces) and
``custom_skills_api.py``/``learned_api.py`` (the pagination-envelope idiom
every list endpoint below clones): questions and notes are owner-scoped rows
a desk user reads/writes about THEMSELVES, gated by the exact same
``_require_system_user`` idiom as the Business tab it replaces (any desk
user; Guest/portal rejected). Two endpoint groups are ADMIN-gated instead
(System Manager | Jarvis Admin | Administrator - DESIGN.md section 1):
Personalisation Settings and Question Rules, mirroring
``learned_api.set_learning_settings``'s "own copy of everything" idiom.

Mental model (DESIGN.md section 6): Questions (Jarvis asks) -> answering one
creates a Note (your answer) -> the pipeline (``jarvis.learning.questions``,
Wave B1's PIPELINE agent, a SIBLING module built this same wave - imported
lazily/defensively below, never assumed to exist yet) turns that into your
personal Wiki. This module owns the Questions/Notes half of that loop; it
does NOT own materialization, ingest extraction, or wiki writes - those are
``jarvis.learning.questions``/``jarvis.chat.wiki`` (PIPELINE agent's files).

Kind derivation (``answer_question``/``save_note``, DESIGN.md 6b, exact
frozen order): a URL makes it a Link; failing that, an attachment makes it
an Attachment; failing that, a positive ``duration_s`` makes it a Voice note
(the caller already ran client-side STT via ``jarvis.chat.voice.
transcribe_audio`` and passes the resulting transcript as ``text``); anything
else is a plain Text note. At least one of text/url/attachment is required -
a bare ``duration_s`` with nothing else is not a valid capture.

Link/attachment extraction are both explicitly BEST-EFFORT: a failure never
fails the note save (DESIGN.md section 4: "failure - keep note, extracted_text
empty, log via frappe.log_error"). The immediate-ingest enqueue at the end of
every save is the same story for the SAME reason (never let a broken/missing
pipeline module turn an answer into a lost note) - it degrades to the daily
sweep, which already reads ``extracted_text or transcript`` for every kind.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from jarvis import compat
from jarvis.permissions import (
	has_jarvis_admin_access,
	is_skill_reviewer,
	require_jarvis_admin,
	require_jarvis_user,
)

QUESTION = "Jarvis Personalise Question"
RULE = "Jarvis Personalise Question Rule"
NOTE = "Jarvis Voice Note"
WIKI = "Jarvis Wiki Page"
SETTINGS = "Jarvis Settings"

# Admin + reviewer sets now live in jarvis.permissions (PART 4 REVISED, TASK 50 —
# one definition, no drift). Admin = require_jarvis_admin/has_jarvis_admin_access
# (Jarvis Admin | System Manager | Administrator); reviewer = is_skill_reviewer
# (Jarvis Skill Reviewer | Jarvis Admin | System Manager). A Skill Reviewer must
# NOT gain billing-admin reach, so the two tiers stay distinct.

# Question status filter values a caller may ask for explicitly. "Deleted" is
# a legal doctype value (soft-delete) but is NEVER a valid filter - every list
# endpoint excludes it unconditionally, matching the controller's own
# docstring contract.
_QUESTION_STATUSES = ("Unanswered", "Answered", "Ignored")
_NOTE_KINDS = ("Text", "Voice", "Attachment", "Link")
_NOTE_STATUSES = ("New", "Processed", "Archived")
_NOTE_SOURCES = ("Business Tab", "Chat Nudge", "Personalise")

_SEARCH_MAX = 140
_EXCERPT_LEN = 300
_MAX_WIKI_PAGES = 5

# Attachment-note text extraction (v1 scope only - research/processing.md
# gap #1 / DESIGN.md section 4: "text extraction for text-like files only
# (bounded), else filename+caption only"). PDFs/images/binaries are left with
# an empty extracted_text; the richer turn_handler._prepare_attachments
# PDF/vision path is deliberately NOT reused here.
_ATTACHMENT_EXTRACT_MAX_BYTES = 200 * 1024
_ATTACHMENT_BINARY_EXT = {
	"pdf",
	"doc",
	"docx",
	"xls",
	"xlsx",
	"ppt",
	"pptx",
	"png",
	"jpg",
	"jpeg",
	"gif",
	"bmp",
	"webp",
	"tiff",
	"svg",
	"ico",
	"zip",
	"tar",
	"gz",
	"rar",
	"7z",
	"mp3",
	"mp4",
	"wav",
	"webm",
	"ogg",
	"m4a",
	"mov",
	"avi",
	"exe",
	"bin",
	"dll",
	"so",
}


# --------------------------------------------------------------------------- #
# guards (self-contained copies; every *_api module keeps its own - see
# voice_notes_api._require_system_user / learned_api._guard / _lk)
# --------------------------------------------------------------------------- #
def _require_system_user() -> None:
	"""Chat-surface gate: Guest rejected, Administrator always allowed, else the
	caller must be a System User AND hold Jarvis access (the Jarvis User role or
	System Manager - TASK 6/8). Mirrors
	``voice_notes_api._require_system_user`` - Personalise is Desk-SPA only,
	same as the Business tab it replaces."""
	from jarvis.permissions import require_jarvis_access

	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	if user == "Administrator":
		return
	if frappe.db.get_value("User", user, "user_type") != "System User":
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	require_jarvis_access(user)


def _admin_guard() -> None:
	"""Personalisation Settings / question-rule gate: Jarvis Admin / System
	Manager (Administrator implicit). PART 4 REVISED, TASK 50 — delegates to the
	one definition in jarvis.permissions."""
	require_jarvis_admin()


def _lk(s: str) -> str:
	"""Escape LIKE wildcards in user search input (self-contained copy)."""
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


def _parse_json(raw, default):
	"""Tolerant str-or-dict payload parse (learned_api._parse_json's idiom,
	self-contained copy)."""
	if raw in (None, ""):
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except Exception:
		try:
			return frappe.parse_json(raw)
		except Exception:
			return default


def _single_bool(field: str, default: bool) -> bool:
	"""NULL=ON Check-field idiom (``voice._voice_features_enabled``,
	``learning.roles._seed_personalise_settings_defaults``): probe
	``tabSingles`` row-existence directly rather than
	``frappe.db.get_single_value``, which coerces an unset Check field to 0
	via ``cint()`` - indistinguishable from an admin explicitly disabling it.
	``jarvis.learning.roles.after_migrate`` backfills a real row for
	``personalise_enabled`` on every migrate, so this fallback is a defensive
	mid-deploy safety net, not the normal-case path."""
	row = frappe.db.sql(
		"select value from tabSingles where doctype=%s and field=%s",
		(SETTINGS, field),
	)
	if not row:
		return default
	return bool(cint(row[0][0]))


def _single_int(field: str, default: int) -> int:
	"""Same NULL-coerces-to-0 gotcha as ``_single_bool``, but for an Int
	Single field: ``learning.roles``'s own module comment spells this out -
	"BOTH an unset Check ... and an unset Int ... silently read back as 0" -
	so ``personalise_daily_question_cap`` needs the identical tabSingles
	row-existence probe, not ``frappe.db.get_single_value`` (which already
	coerces a missing row to 0 before this function ever sees it)."""
	row = frappe.db.sql(
		"select value from tabSingles where doctype=%s and field=%s",
		(SETTINGS, field),
	)
	if not row:
		return default
	try:
		return int(row[0][0])
	except (TypeError, ValueError):
		return default


# --------------------------------------------------------------------------- #
# probe / status
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_skills_area_caps() -> dict:
	"""Single tab-gating probe for the whole Skills area (DESIGN.md section 6:
	"single get_skills_area_caps probe ... replaces boolean pair"). Any desk
	user passes the gate (403 for Guest/portal, mirroring the old
	Business-tab probe); the four booleans then tell the SPA which tabs to
	render - the SAME mechanism ``SkillsPage.vue`` already uses
	(``Promise.allSettled`` fulfilled-vs-rejected on cheap probes), just
	collapsed into one call instead of N.

	NOTE (deviation from DESIGN.md section 4's draft list, reconciled against
	the FROZEN section 6b contract this function actually implements):
	section 4 additionally names a standalone ``get_personalisation_admin_
	status`` probe, but section 6's "single get_skills_area_caps probe"
	decision and section 6b's frozen endpoint list both make that redundant -
	Personalisation Settings uses the EXACT SAME admin gate as the Analysis
	tab (section 1: both "System Manager | Administrator | Jarvis Admin"), so
	the SPA gates the Settings gear off this same ``analysis`` boolean rather
	than a second probe. Not implemented as a separate endpoint; flagged here
	for the integrator/F1-F4 frontend agents."""
	_require_system_user()
	me = frappe.session.user

	stt_enabled = False
	# WHY it is off (ok|off|unconfigured|error): only `unconfigured` is an actionable
	# gap worth showing the user as a faded recorder; the rest stay hidden.
	stt_state = "error"
	try:
		from jarvis.chat import voice

		stt_enabled = bool(voice.stt_config())
		stt_state = voice.stt_state()
	except Exception:
		stt_enabled = False
		stt_state = "error"

	unanswered_count = frappe.db.count(QUESTION, {"user": me, "status": "Unanswered"})
	# Any non-Deleted question, any status - the SPA uses this to tell a
	# brand-new user (never had a question at all) apart from a caught-up one
	# (has questions, none Unanswered) in the Questions empty state.
	questions_total = frappe.db.count(QUESTION, {"user": me, "status": ["!=", "Deleted"]})

	return {
		"personalise": True,
		"wiki": True,
		"analysis": has_jarvis_admin_access(),
		"review": is_skill_reviewer(),
		"stt_enabled": stt_enabled,
		"stt_state": stt_state,
		"unanswered_count": unanswered_count,
		"questions_total": questions_total,
		"personalise_enabled": _single_bool("personalise_enabled", True),
	}


# --------------------------------------------------------------------------- #
# questions
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_questions_page(
	status: str = "Unanswered",
	search: str = "",
	sort: str = "newest",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""The caller's own questions (owner-scoped via the ``user`` field, which
	the controller keeps in lockstep with ``owner`` regardless of which
	identity materialized the row). ``status`` blank returns all three live
	states; "Deleted" is never returned or acceptable as a filter value
	(soft-deleted rows are gone from every list, per the controller's own
	docstring contract). ``search`` is a wildcard-escaped LIKE on question
	text; ``sort`` is ``newest|oldest|origin``. Envelope:
	``{rows, total, has_more, start, page_length}``."""
	_require_system_user()
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)

	filters: dict = {"user": me, "status": ["!=", "Deleted"]}
	status = (status or "").strip()
	if status:
		if status not in _QUESTION_STATUSES:
			frappe.throw(_("Invalid status filter."))
		filters["status"] = status

	search = (search or "").strip()[:_SEARCH_MAX]
	if search:
		filters["question"] = ["like", f"%{_lk(search)}%"]

	order_by = {
		"oldest": "creation asc, name asc",
		"origin": "origin asc, creation desc, name asc",
	}.get(sort, "creation desc, name asc")

	total = frappe.db.count(QUESTION, filters)
	rows = frappe.get_all(
		QUESTION,
		filters=filters,
		fields=[
			"name",
			"question",
			"origin",
			"status",
			"context_md",
			"creation as created",
			"answered_at",
			"source_pattern",
			"answer_note",
		],
		order_by=order_by,
		limit_start=start,
		limit_page_length=pl,
	)
	for r in rows:
		r["has_answer"] = bool(r.pop("answer_note", None))
		r["created"] = str(r.get("created") or "")
		r["answered_at"] = str(r["answered_at"]) if r.get("answered_at") else None

	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


@frappe.whitelist()
def get_question(name: str) -> dict:
	"""The caller's own single question (owner-only), same field shape as a
	``list_questions_page`` row (incl. ``context_md``) so the SPA can rehydrate
	an "Answering:" panel from a deep link without re-listing. Raises
	``DoesNotExistError`` when the row is missing OR soft-Deleted - the frontend
	treats both as "this question is no longer available" - while a non-owner
	still gets ``PermissionError`` (owner is checked BEFORE the Deleted state so
	one user can't probe another's rows via the not-found channel)."""
	_require_system_user()
	me = frappe.session.user
	row = frappe.db.get_value(
		QUESTION,
		name,
		[
			"name",
			"question",
			"origin",
			"status",
			"context_md",
			"creation",
			"answered_at",
			"source_pattern",
			"answer_note",
			"user",
		],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Question not found."), frappe.DoesNotExistError)
	if row.user != me:
		frappe.throw(_("Not your question."), frappe.PermissionError)
	if row.status == "Deleted":
		frappe.throw(_("This question was deleted."), frappe.DoesNotExistError)

	row["has_answer"] = bool(row.pop("answer_note", None))
	row["created"] = str(row.pop("creation", None) or "")
	row["answered_at"] = str(row["answered_at"]) if row.get("answered_at") else None
	row.pop("user", None)
	return row


@frappe.whitelist()
def answer_question(
	name: str,
	text: str = "",
	url: str = "",
	attachment: str = "",
	duration_s: int = 0,
) -> dict:
	"""Answer a question (owner-only), legal from ANY of the three live
	states (a re-answer just creates a fresh Note and re-links
	``answer_note`` - nothing about a prior answer is destroyed or archived,
	per DESIGN.md section 2.1/6). Same kind-derivation payload as
	``save_note``; the created Note carries ``question=name`` so
	``get_note``/the Notes view can show "answers: <question>"."""
	_require_system_user()
	me = frappe.session.user
	row = frappe.db.get_value(QUESTION, name, ["user", "status"], as_dict=True)
	if not row:
		frappe.throw(_("Question not found."))
	if row.user != me:
		frappe.throw(_("Not your question."), frappe.PermissionError)
	if row.status == "Deleted":
		frappe.throw(_("This question was deleted."))

	note = _create_note(
		text=text,
		url=url,
		attachment=attachment,
		duration_s=duration_s,
		source="Personalise",
		question=name,
	)

	frappe.db.set_value(
		QUESTION,
		name,
		{
			"status": "Answered",
			"answered_at": now_datetime(),
			"answer_note": note.name,
			"ignored_at": None,
		},
		update_modified=False,
	)
	frappe.db.commit()
	return {"ok": True, "note": note.name, "question_status": "Answered"}


@frappe.whitelist()
def ignore_question(name: str) -> dict:
	"""Owner-only "not now": Ignored is snooze-like, NOT terminal - the row
	stays listed and answerable (DESIGN.md section 6). There is no separate
	``unignore_question`` endpoint by design: answering an Ignored question
	is how it comes back (``answer_question`` already works from any of the
	three live states)."""
	_require_system_user()
	me = frappe.session.user
	row = frappe.db.get_value(QUESTION, name, ["user", "status"], as_dict=True)
	if not row:
		frappe.throw(_("Question not found."))
	if row.user != me:
		frappe.throw(_("Not your question."), frappe.PermissionError)
	if row.status == "Deleted":
		frappe.throw(_("This question was deleted."))
	frappe.db.set_value(
		QUESTION,
		name,
		{"status": "Ignored", "ignored_at": now_datetime()},
		update_modified=False,
	)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
def delete_question(name: str) -> dict:
	"""Owner-only soft-delete: status -> "Deleted" (audit trail kept; every
	list/probe endpoint excludes it; also suppresses a generator from ever
	re-minting the same question, per the controller's own docstring)."""
	_require_system_user()
	me = frappe.session.user
	owner_user = frappe.db.get_value(QUESTION, name, "user")
	if not owner_user:
		frappe.throw(_("Question not found."))
	if owner_user != me:
		frappe.throw(_("Not your question."), frappe.PermissionError)
	frappe.db.set_value(QUESTION, name, "status", "Deleted", update_modified=False)
	frappe.db.commit()
	return {"ok": True}


# --------------------------------------------------------------------------- #
# notes (shared create path for answer_question + save_note)
# --------------------------------------------------------------------------- #
def _derive_kind(url: str, attachment: str, duration_s: int) -> str:
	"""Frozen derivation order (DESIGN.md 6b, verbatim): url -> Link;
	attachment -> Attachment; duration_s>0 -> Voice; else Text."""
	if url:
		return "Link"
	if attachment:
		return "Attachment"
	if duration_s > 0:
		return "Voice"
	return "Text"


def _validate_and_resolve_attachment(file_url: str) -> str:
	"""Resolve + object-level-authorize an uploaded ``file_url`` (the
	``filebox.drop_file`` idiom): the caller must not be able to hand back
	someone else's private file_url and have its contents land in their own
	note. Returns the File doc's name."""
	fdoc = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not fdoc:
		frappe.throw(_("Unknown attachment - upload it first."))
	if not frappe.has_permission("File", "read", doc=fdoc):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	return fdoc


def _extract_attachment_text(file_name: str | None) -> str:
	"""Text extraction for Attachment-kind notes, v1 scope only: text-like
	files <=200KB, decoded UTF-8. PDFs/images/binaries are left with an empty
	``extracted_text`` (filename + optional caption only) - see the module
	docstring's v1-scope note. Never raises; extraction failures are
	best-effort, logged, and never fail the note save."""
	if not file_name:
		return ""
	try:
		fdoc = frappe.get_doc("File", file_name)
		if cint(fdoc.file_size) > _ATTACHMENT_EXTRACT_MAX_BYTES:
			return ""
		fname = fdoc.file_name or ""
		ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
		if ext in _ATTACHMENT_BINARY_EXT:
			return ""
		# Raw bytes, on either Frappe major: get_content(encodings=[]) is a
		# TypeError on Frappe 15, and the except below swallowed it, so every
		# note attachment silently extracted to empty text.
		raw = compat.file_bytes(fdoc)
		if len(raw) > _ATTACHMENT_EXTRACT_MAX_BYTES:
			return ""
		# Attachment bytes are just as untrusted as fetched web content, so
		# they pass through the SAME instruction-injection neutralization the
		# Link path applies (link_fetch._neutralize - strip control chars +
		# defang injection-shaped substrings) before this text can reach the
		# fact-extraction LLM. The <untrusted-data> fencing at the LLM boundary
		# itself is the extraction caller's job (voice_facts).
		from jarvis.chat import link_fetch

		return link_fetch._neutralize(raw.decode("utf-8"))
	except UnicodeDecodeError:
		return ""
	except Exception:
		frappe.log_error(
			title="personalise: attachment text extraction failed",
			message=frappe.get_traceback(),
		)
		return ""


def _fetch_link_text(url: str) -> str:
	"""Best-effort server-side link fetch (``jarvis.chat.link_fetch``,
	SSRF-guarded). ANY failure - guard rejection, network error, wrong
	content-type, oversize response - is logged and swallowed here: the note
	is always kept, just with an empty ``extracted_text`` (DESIGN.md section
	4)."""
	try:
		from jarvis.chat import link_fetch

		return link_fetch.fetch_and_extract(url) or ""
	except Exception:
		frappe.log_error(title="personalise: link fetch failed", message=frappe.get_traceback())
		return ""


def _enqueue_immediate_ingest(note_name: str) -> None:
	"""Best-effort immediate ingest so an answer is processed within minutes,
	never the next daily sweep (DESIGN.md section 5b: the "answer-black-hole"
	anti-pattern this whole latency contract exists to avoid).
	``jarvis.learning.questions`` is the PIPELINE agent's module, built in
	this SAME wave - imported lazily so import order between the two agents'
	work never matters, and wrapped defensively so a missing/broken module
	degrades to "the daily voice_facts sweep picks the New note up later",
	never to a failed save."""
	try:
		from jarvis.learning import questions as learning_questions

		learning_questions.enqueue_note_ingest(note_name)
	except Exception:
		frappe.log_error(
			title="personalise: immediate ingest enqueue failed",
			message=frappe.get_traceback(),
		)


def _create_note(
	*,
	text: str,
	url: str,
	attachment: str,
	duration_s,
	source: str,
	question: str | None = None,
):
	"""Shared save path for ``answer_question``/``save_note``: validate the
	payload, derive kind, resolve+reparent an attachment, fetch a Link's
	content (best-effort), insert the Note, and enqueue immediate ingest.
	Returns the inserted ``Jarvis Voice Note`` document."""
	text = (text or "").strip()
	url = (url or "").strip()
	attachment = (attachment or "").strip()
	try:
		duration_s = max(0, int(duration_s or 0))
	except (TypeError, ValueError):
		duration_s = 0

	if not text and not url and not attachment:
		frappe.throw(_("Provide text, a link, or an attachment."))

	kind = _derive_kind(url, attachment, duration_s)

	# Resolve+authorize the attachment BEFORE inserting anything - a bad/
	# unowned file_url is a genuine validation failure (not a best-effort
	# extraction concern), so it must throw, not degrade silently.
	fdoc_name = _validate_and_resolve_attachment(attachment) if attachment else None

	extracted_text = ""
	if kind == "Link":
		extracted_text = _fetch_link_text(url)
	elif kind == "Attachment":
		extracted_text = _extract_attachment_text(fdoc_name)

	doc = frappe.get_doc(
		{
			"doctype": NOTE,
			"kind": kind,
			"transcript": text,
			"duration_s": duration_s,
			"attachment": attachment or None,
			"url": url or None,
			"extracted_text": extracted_text or None,
			"context_type": "Business",
			"question": question,
			"source": source,
			"status": "New",
		}
	)
	doc.insert(ignore_permissions=True)

	if fdoc_name:
		# Durable reparent (filebox.drop_file's idiom): the file now belongs
		# to this note, not a bare upload with no owning record.
		try:
			frappe.db.set_value(
				"File",
				fdoc_name,
				{"attached_to_doctype": NOTE, "attached_to_name": doc.name},
				update_modified=False,
			)
		except Exception:
			pass

	frappe.db.commit()
	_enqueue_immediate_ingest(doc.name)
	return doc


@frappe.whitelist()
def save_note(
	text: str = "",
	url: str = "",
	attachment: str = "",
	duration_s: int = 0,
	source: str = "Personalise",
) -> dict:
	"""Free capture (no question attached) - the composer's default mode.
	Same 4-kind payload/derivation as ``answer_question``.
	``voice_notes_api.save_voice_note`` (the Business tab's endpoint) is left
	completely untouched for chat-nudge compat, per DESIGN.md section 4."""
	_require_system_user()
	if source not in _NOTE_SOURCES:
		frappe.throw(_("Invalid source."))
	note = _create_note(text=text, url=url, attachment=attachment, duration_s=duration_s, source=source)
	return {"ok": True, "note": note.name}


@frappe.whitelist()
def list_notes_page(
	kind: str = "",
	status: str = "",
	search: str = "",
	sort: str = "newest",
	start: int = 0,
	page_length: int = 20,
) -> dict:
	"""The caller's own notes (owner-scoped). ``search`` is a
	wildcard-escaped LIKE on ``transcript`` (matches
	``list_my_voice_notes_page``'s contract exactly - the caption/own-words
	field, not ``extracted_text``); ``sort`` is ``newest|oldest``. Envelope:
	``{rows, total, has_more, start, page_length}``."""
	_require_system_user()
	me = frappe.session.user
	start, pl = _clamp_page(start, page_length)

	filters: dict = {"owner": me}
	kind = (kind or "").strip()
	if kind:
		if kind not in _NOTE_KINDS:
			frappe.throw(_("Invalid kind filter."))
		filters["kind"] = kind
	status = (status or "").strip()
	if status:
		if status not in _NOTE_STATUSES:
			frappe.throw(_("Invalid status filter."))
		filters["status"] = status

	search = (search or "").strip()[:_SEARCH_MAX]
	if search:
		filters["transcript"] = ["like", f"%{_lk(search)}%"]

	order_by = "creation asc, name asc" if sort == "oldest" else "creation desc, name asc"

	total = frappe.db.count(NOTE, filters)
	rows = frappe.get_all(
		NOTE,
		filters=filters,
		fields=[
			"name",
			"kind",
			"status",
			"source",
			"creation",
			"transcript",
			"url",
			"duration_s",
			"question",
		],
		order_by=order_by,
		limit_start=start,
		limit_page_length=pl,
	)
	for r in rows:
		r["excerpt"] = (r.get("transcript") or "")[:_EXCERPT_LEN]

	return {
		"rows": rows,
		"total": total,
		"has_more": start + len(rows) < total,
		"start": start,
		"page_length": pl,
	}


@frappe.whitelist()
def get_note(name: str) -> dict:
	"""Note detail (owner-only): full fields plus the answered question's
	text (if linked) and up to 5 Wiki pages whose ``sources`` JSON trail
	references this note by name (DESIGN.md 6b: "cheap LIKE is acceptable" -
	the sources trail is a small, capped provenance list, not a query
	surface that needs indexing)."""
	_require_system_user()
	me = frappe.session.user
	doc = frappe.db.get_value(
		NOTE,
		name,
		[
			"name",
			"kind",
			"transcript",
			"extracted_text",
			"url",
			"attachment",
			"duration_s",
			"context_type",
			"conversation",
			"question",
			"source",
			"status",
			"creation",
			"processed_at",
			"processed_note",
			"owner",
		],
		as_dict=True,
	)
	if not doc:
		frappe.throw(_("Note not found."))
	if doc.owner != me:
		frappe.throw(_("Not your note."), frappe.PermissionError)

	question_text = None
	if doc.question:
		question_text = frappe.db.get_value(QUESTION, doc.question, "question")

	wiki_pages = frappe.get_all(
		WIKI,
		filters={"sources": ["like", f"%{name}%"]},
		fields=["slug", "title"],
		order_by="modified desc",
		limit_page_length=_MAX_WIKI_PAGES,
	)

	doc["question_text"] = question_text
	doc["wiki_pages"] = wiki_pages
	doc["creation"] = str(doc.get("creation") or "")
	doc["processed_at"] = str(doc["processed_at"]) if doc.get("processed_at") else None
	doc.pop("owner", None)
	return doc


@frappe.whitelist()
@require_jarvis_user
def delete_note(name: str) -> dict:
	"""Owner-only delete. Alias of ``voice_notes_api.delete_voice_note``
	(DESIGN.md 6b: "delete_note exists as delete_voice_note; alias ok") kept
	under the Personalise API surface so the frontend's ``api/personalise.js``
	needs only one module to import from."""
	from jarvis.chat.voice_notes_api import delete_voice_note

	return delete_voice_note(name)


# --------------------------------------------------------------------------- #
# Personalisation Settings (admin set)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_personalisation_settings() -> dict:
	"""Admin-only read of the Personalisation Single fields, plus the read-only
	chat-mining run status so an operator can see the daily sweep is alive."""
	_admin_guard()
	return {
		"daily_question_cap": _single_int("personalise_daily_question_cap", 5),
		"personalise_enabled": _single_bool("personalise_enabled", True),
		"chat_question_mining_enabled": _single_bool("chat_question_mining_enabled", True),
		"chat_mining_last_run_at": frappe.db.get_single_value(SETTINGS, "chat_mining_last_run_at") or None,
		"chat_mining_last_run_status": frappe.db.get_single_value(SETTINGS, "chat_mining_last_run_status")
		or "",
	}


@frappe.whitelist()
def set_personalisation_settings(payload: str | dict | None = None) -> dict:
	"""Admin-only write. Only the two known keys are accepted; unknown keys
	are refused (``learned_api.set_learning_settings``'s idiom). Writes via
	``frappe.db.set_single_value(update_modified=False)`` - a Personalise
	toggle has no business firing ``Jarvis Settings.on_update`` (the LLM-pool
	re-sync side effect that write path exists for)."""
	_admin_guard()
	payload = _parse_json(payload, None)
	if not isinstance(payload, dict) or not payload:
		frappe.throw(_("Provide a settings object to write."))

	known = ("daily_question_cap", "personalise_enabled", "chat_question_mining_enabled")
	unknown = [k for k in payload if k not in known]
	if unknown:
		frappe.throw(_("Unknown settings field(s): {0}.").format(", ".join(unknown)))

	values: dict = {}
	if "daily_question_cap" in payload:
		try:
			cap = int(payload["daily_question_cap"])
		except (TypeError, ValueError):
			frappe.throw(_("Daily question cap must be a number."))
		if cap < 0:
			frappe.throw(_("Daily question cap cannot be negative."))
		values["personalise_daily_question_cap"] = cap
	if "personalise_enabled" in payload:
		values["personalise_enabled"] = 1 if cint(payload["personalise_enabled"]) else 0
	if "chat_question_mining_enabled" in payload:
		values["chat_question_mining_enabled"] = 1 if cint(payload["chat_question_mining_enabled"]) else 0

	frappe.db.set_single_value(SETTINGS, values, update_modified=False)
	frappe.db.commit()
	return get_personalisation_settings()


@frappe.whitelist()
def generate_chat_questions_now() -> dict:
	"""Admin-only: run the chat-transcript question miner immediately over the
	recent backlog (the manual counterpart to the daily sweep). Refuses when
	Personalisation or chat mining is turned off, or when a sweep is already
	queued/running. Returns ``{"ok": bool, "reason"?: str}``."""
	_admin_guard()
	if not _single_bool("personalise_enabled", True):
		return {"ok": False, "reason": "Turn on Personalisation first."}
	if not _single_bool("chat_question_mining_enabled", True):
		return {"ok": False, "reason": "Turn on 'Learn from chats' first."}
	from jarvis.learning import chat_mining

	return chat_mining.enqueue_process_now()


# --------------------------------------------------------------------------- #
# Question Rules (admin set) - materialization itself is
# jarvis.learning.questions's job (PIPELINE agent); this is CRUD only.
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_role_options() -> list[str]:
	"""Admin-only: desk Role names selectable as a question-rule ``target_role``
	(Role scope). Returns every enabled Role that has desk access, minus the
	built-in ``Administrator``/``Guest``/``All`` pseudo-roles (never sensible
	rule targets); sorted. Same admin gate as the settings/rule endpoints, so a
	Jarvis Admin who is NOT a System Manager can still populate the Role picker
	(the persona this Settings screen exists for)."""
	_admin_guard()
	return frappe.get_all(
		"Role",
		filters={
			"disabled": 0,
			"desk_access": 1,
			"name": ["not in", ("Administrator", "Guest", "All")],
		},
		pluck="name",
		order_by="name asc",
	)


@frappe.whitelist()
def list_question_rules() -> list[dict]:
	"""Admin-only: every configured rule. A flat, unpaginated list - the
	Personalisation Settings editor is a small admin-managed table, not a
	high-volume surface."""
	_admin_guard()
	return frappe.get_all(
		RULE,
		fields=[
			"name",
			"question",
			"context_md",
			"scope",
			"target_role",
			"target_user",
			"active",
		],
		order_by="creation desc",
	)


@frappe.whitelist()
def save_question_rule(payload: str | dict | None = None) -> dict:
	"""Admin-only create/update. ``payload["name"]`` present -> update that
	rule; absent -> create a new one. Saved with ``ignore_permissions=True``
	because the DocType permission row stays System-Manager-only by design
	(``jarvis_personalise_question_rule.py``'s own docstring: "Jarvis Admin
	reaches it only via a future code-guarded API, not a DocType grant" -
	this endpoint IS that code-guarded API).

	NOTE on the daily-materialization-sweep's "on-save" trigger (DESIGN.md
	section 2.2: "daily + on-save"): that wiring belongs on the doctype's
	``doc_events`` hook (``hooks.py``, owned by the PIPELINE agent this wave
	alongside ``jarvis.learning.questions`` itself) so it fires identically
	whether a rule is saved from here, from the Desk, or from a test -
	deliberately NOT called from this endpoint directly."""
	_admin_guard()
	payload = _parse_json(payload, None)
	if not isinstance(payload, dict):
		frappe.throw(_("Provide a rule payload."))

	fields = ("question", "context_md", "scope", "target_role", "target_user", "active")
	name = (payload.get("name") or "").strip()
	if name:
		if not frappe.db.exists(RULE, name):
			frappe.throw(_("Question rule not found."))
		doc = frappe.get_doc(RULE, name)
	else:
		doc = frappe.new_doc(RULE)

	for f in fields:
		if f in payload:
			doc.set(f, payload[f])
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True, "name": doc.name}


@frappe.whitelist()
def delete_question_rule(name: str) -> dict:
	"""Admin-only hard delete. Rules have no soft-delete semantics of their
	own (unlike questions): deleting a rule is a real retraction, and any
	questions it already materialized are unaffected (they stand on their
	own once created - DESIGN.md section 2.2's dedupe key is rule+user, so a
	later re-created rule with the same text mints fresh questions rather
	than resurrecting old ones, which is the intended behaviour)."""
	_admin_guard()
	if not frappe.db.exists(RULE, name):
		frappe.throw(_("Question rule not found."))
	frappe.delete_doc(RULE, name, ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}
