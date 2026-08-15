"""Personalise-question materialization + the immediate single-note ingest seam
(Skills-area rework, Wave B1 pipeline reroute — DESIGN.md sections 3, 5b, 6b).

Three writers into ``Jarvis Personalise Question`` plus the immediate
note-ingest seam:

  * ``maybe_materialize_for_pattern(pattern_name)`` — fired best-effort from
    ``jarvis.learning.lifecycle.upsert_candidate`` the moment a
    ``Jarvis Learned Pattern`` row is created, so an identifiable user sees the
    finding as a question within minutes (the daily scan is the backstop).
  * ``materialize_questions_daily()`` — hooks ``daily`` backstop: scans Proposed
    learned-pattern rows that never got a linked question and materializes the
    missing ones.
  * ``materialize_rule_questions(rule_name=None)`` — hooks ``daily`` AND the
    rule doctype's ``on_update`` (wired via hooks doc_events): upserts one
    admin-authored question per in-scope user. UNCAPPED (org policy, not a
    learning finding).
  * ``enqueue_note_ingest(note_name)`` — the immediate-ingest seam the
    Personalise API calls when a user answers a question or free-captures a
    note; runs the SAME extraction the daily voice sweep runs, for ONE note
    (delegates to ``jarvis.learning.voice_facts.process_single_note``).

Cap / dedupe (learning + chat-pattern origins only):
  * per-user-per-day cap = ``Jarvis Settings.personalise_daily_question_cap``
    (default 5); an overflow row is simply left for a later run (no loss — the
    daily scan retries). Org-rule and reviewer questions are NOT capped.
  * dedupe key = (``source_pattern``/``source_config``, ``user``): never a
    second question for the pair, and because a soft-Deleted row is never
    hard-removed, a Deleted question permanently suppresses re-minting
    ("stop asking me this").

Nothing here raises into its caller: the learning engine, the daily scheduler
and the note-ingest worker all treat this module as best-effort.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, today

QUESTION = "Jarvis Personalise Question"
RULE = "Jarvis Personalise Question Rule"
JLP = "Jarvis Learned Pattern"
NOTE = "Jarvis Voice Note"
SETTINGS = "Jarvis Settings"

# Origins that count against the per-user daily cap (learning findings). Org-rule
# ("From your organisation") and reviewer ("From your reviewer") questions are
# authored deliberately, so they are uncapped and excluded from the count.
LEARNING_ORIGINS = ("Behavioural Learning", "From your chat patterns")
ORG_ORIGIN = "From your organisation"

DEFAULT_DAILY_CAP = 5
MAX_QUESTION_LEN = 500
MAX_CONTEXT_LEN = 4000
# Bound the daily backstop's per-run work; genuinely-unmaterialized patterns
# beyond this wait for the next run (no loss).
_DAILY_SCAN_CANDIDATES = 1000
_DAILY_SCAN_LIMIT = 200

_NOTE_INGEST_JOB_PREFIX = "jarvis_personalise_ingest"
_NOTE_INGEST_TIMEOUT_S = 300

_EXCLUDE_USERS = ("Administrator", "Guest")


# --------------------------------------------------------------------------- #
# settings gates (row-existence NULL=ON / cap default, mirroring voice_facts)
# --------------------------------------------------------------------------- #
def _enabled() -> bool:
	"""``personalise_enabled`` — NULL=ON via row-existence probe (v16 coerces an
	unset Check to 0, indistinguishable from operator-off, so we probe the
	tabSingles row directly the same way ``voice_facts._flag_on`` does)."""
	try:
		rows = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field=%s",
			(SETTINGS, "personalise_enabled"),
		)
	except Exception:
		return True
	if not rows or rows[0][0] is None:
		return True
	return bool(cint(rows[0][0]))


def _daily_cap() -> int:
	"""``personalise_daily_question_cap``; an absent row reads as the default 5,
	an explicit value (including 0 = disable learning questions) is honored."""
	try:
		rows = frappe.db.sql(
			"select value from tabSingles where doctype=%s and field=%s",
			(SETTINGS, "personalise_daily_question_cap"),
		)
	except Exception:
		return DEFAULT_DAILY_CAP
	if not rows or rows[0][0] is None:
		return DEFAULT_DAILY_CAP
	return cint(rows[0][0])


# --------------------------------------------------------------------------- #
# pattern -> question materialization (immediate hook + daily backstop)
# --------------------------------------------------------------------------- #
def maybe_materialize_for_pattern(pattern_name: str) -> dict:
	"""Immediate materialization when a JLP row is freshly created (the thin
	wrapper ``lifecycle.upsert_candidate`` calls). Cap + dedupe enforced; never
	raises. Returns ``{created}``."""
	stats = {"created": 0}
	try:
		if not _enabled():
			return stats
		pattern = _load_pattern(pattern_name)
		if not pattern:
			return stats
		users = _materialize_for_pattern(pattern)
		stats["created"] = len(users)
		# Immediate path: the caller (engine unit / voice sweep) owns the commit;
		# a fire-and-forget badge event is safe (it re-reads on the client).
		for user in set(users):
			_publish_question_event(user)
	except Exception:
		frappe.log_error(
			title="jarvis personalise: pattern question materialization failed",
			message=frappe.get_traceback(),
		)
	return stats


def materialize_questions_daily() -> dict:
	"""Hooks ``daily`` backstop: scan Proposed learned-pattern rows that never got
	a linked question and materialize the missing ones (same cap + dedupe as the
	immediate hook). Never raises."""
	stats = {"created": 0, "patterns": 0}
	try:
		if not _enabled():
			return stats
		# Coarse filter (DESIGN.md 3): only patterns with NO linked question yet.
		minted = {
			r.source_pattern
			for r in frappe.get_all(
				QUESTION, filters={"source_pattern": ["is", "set"]}, fields=["source_pattern"]
			)
			if r.source_pattern
		}
		rows = frappe.get_all(
			JLP,
			filters={"status": "Proposed"},
			fields=[
				"name",
				"detector_id",
				"pattern_statement",
				"evidence",
				"support_n",
				"confidence_pct",
			],
			order_by="creation desc",
			limit_page_length=_DAILY_SCAN_CANDIDATES,
		)
		touched: list[str] = []
		scanned = 0
		for pattern in rows:
			if pattern.name in minted:
				continue
			scanned += 1
			if scanned > _DAILY_SCAN_LIMIT:
				break
			users = _materialize_for_pattern(pattern)
			if users:
				stats["patterns"] += 1
				stats["created"] += len(users)
				touched.extend(users)
		if stats["created"]:
			frappe.db.commit()
			for user in set(touched):
				_publish_question_event(user)
	except Exception:
		frappe.log_error(
			title="jarvis personalise: daily question scan failed",
			message=frappe.get_traceback(),
		)
	return stats


def _load_pattern(pattern_name: str):
	return frappe.db.get_value(
		JLP,
		pattern_name,
		["name", "detector_id", "pattern_statement", "evidence", "support_n", "confidence_pct"],
		as_dict=True,
	)


def _materialize_for_pattern(pattern) -> list[str]:
	"""Create one question per identifiable target user, cap + dedupe enforced.
	Does NOT commit (the caller decides). Returns the users a question was
	created for."""
	users, origin = _pattern_targets(pattern)
	if not users:
		return []
	cap = _daily_cap()
	q_text = _question_for_pattern(pattern)
	ctx = _pattern_context_md(pattern)
	created: list[str] = []
	for user in users:
		# Dedupe + Deleted-suppression: ANY existing question for (pattern, user)
		# — non-Deleted means "already asked", Deleted means "never re-mint".
		if frappe.db.exists(QUESTION, {"source_pattern": pattern.name, "user": user}):
			continue
		if cap <= 0 or _learning_questions_today(user) >= cap:
			continue  # overflow: left for a later run (no loss)
		name = _insert_question(
			user=user,
			question=q_text,
			context_md=ctx,
			origin=origin,
			source_pattern=pattern.name,
		)
		if name:
			created.append(user)
	return created


def _pattern_targets(pattern) -> tuple[list[str], str]:
	"""(target users, origin) for a learned-pattern row.

	Voice/chat-sourced facts carry the note owners in ``evidence.users`` — those
	are the identifiable targets. A detector row with no identifiable user is an
	org-aggregate finding, so it routes to the admins' banks (enabled Jarvis
	Admin holders, falling back to System Managers). Origin is "From your chat
	patterns" for the voice detector or chat-provenance evidence, else
	"Behavioural Learning" (DESIGN.md 3)."""
	evidence = _evidence_dict(pattern.get("evidence"))
	detector_id = (pattern.get("detector_id") or "").strip()
	chat_provenance = evidence.get("source") in ("voice", "chat") or bool(evidence.get("chat"))
	origin = (
		"From your chat patterns"
		if detector_id == "voice-context" or chat_provenance
		else "Behavioural Learning"
	)
	users = [u for u in (evidence.get("users") or []) if u and u not in _EXCLUDE_USERS]
	if users:
		return sorted(set(users)), origin
	return _admin_bank_users(), origin


def _admin_bank_users() -> list[str]:
	"""Enabled Jarvis Admin holders (the people who can contextualize an
	org-aggregate finding); fall back to System Managers. Administrator/Guest
	are never a real bank."""
	users = _users_with_role("Jarvis Admin")
	if not users:
		users = _users_with_role("System Manager")
	return users


def _users_with_role(role: str) -> list[str]:
	"""Enabled holders of ``role``, minus service identities and Website users.

	The user_type filter is deliberate and not redundant with the role check: a
	stale "Jarvis User" Has Role row can outlive the gate on any bench that
	granted the role before ``has_jarvis_access`` began requiring a System User.
	Filtering here keeps every targeting set aligned with the gate itself, rather
	than depending on a one-time revoke patch having cleaned the rows up."""
	from frappe.utils.user import get_users_with_role

	try:
		found = set(get_users_with_role(role))
		candidates = sorted(u for u in found if u and u not in _EXCLUDE_USERS)
		if not candidates:
			return []
		# Indexed primary-key IN lookup, and the caller already runs a per-user
		# db.exists in its own loop, so this is not a meaningful added cost.
		system_users = set(
			frappe.get_all(
				"User",
				filters={"name": ["in", candidates], "user_type": "System User"},
				pluck="name",
			)
		)
	except Exception:
		return []
	return [u for u in candidates if u in system_users]


def _learning_questions_today(user: str) -> int:
	"""Count of learning/chat-origin questions MINTED for ``user`` today (any
	status — a soft-Deleted row was still minted, so it counts toward the cap)."""
	rows = frappe.get_all(
		QUESTION,
		filters={
			"user": user,
			"origin": ["in", list(LEARNING_ORIGINS)],
			"creation": [">=", today()],
		},
		pluck="name",
	)
	return len(rows)


# --------------------------------------------------------------------------- #
# rule -> question materialization (daily + on_update, uncapped)
# --------------------------------------------------------------------------- #
def materialize_rule_questions(rule_name: str | None = None) -> dict:
	"""Upsert one question per in-scope user for each active
	``Jarvis Personalise Question Rule`` (origin "From your organisation").
	UNCAPPED. Dedupe key (source_config, user) with the same Deleted-suppression.
	Called daily and from the rule doctype's on_update. Never raises."""
	stats = {"created": 0, "rules": 0}
	try:
		if not _enabled():
			return stats
		filters: dict = {"active": 1}
		if rule_name:
			filters["name"] = rule_name
		rules = frappe.get_all(
			RULE,
			filters=filters,
			fields=["name", "question", "context_md", "scope", "target_role", "target_user"],
		)
		touched: list[str] = []
		for rule in rules:
			q_text = (rule.question or "").strip()[:MAX_QUESTION_LEN]
			users = _rule_in_scope_users(rule)
			if not q_text or not users:
				continue
			made = False
			for user in users:
				if frappe.db.exists(QUESTION, {"source_config": rule.name, "user": user}):
					continue  # dedupe + Deleted-suppression
				name = _insert_question(
					user=user,
					question=q_text,
					context_md=(rule.context_md or ""),
					origin=ORG_ORIGIN,
					source_config=rule.name,
				)
				if name:
					stats["created"] += 1
					touched.append(user)
					made = True
			if made:
				stats["rules"] += 1
		if stats["created"]:
			frappe.db.commit()
			for user in set(touched):
				_publish_question_event(user)
	except Exception:
		frappe.log_error(
			title="jarvis personalise: rule question materialization failed",
			message=frappe.get_traceback(),
		)
	return stats


def on_rule_update(doc, method: str | None = None) -> None:
	"""``Jarvis Personalise Question Rule`` on_update handler (wired via hooks
	doc_events). Enqueues a deduped after-commit materialize for THIS rule so the
	save stays fast and never re-enters. Inactive rules no-op (turning a rule off
	does not retract questions already materialized)."""
	try:
		if not cint(getattr(doc, "active", 0)):
			return
		frappe.enqueue(
			"jarvis.learning.questions.materialize_rule_questions",
			queue="long",
			job_id=f"jarvis_rule_questions::{doc.name}",
			deduplicate=True,
			enqueue_after_commit=True,
			rule_name=doc.name,
		)
	except Exception:
		frappe.log_error(
			title="jarvis personalise: rule on_update enqueue failed",
			message=frappe.get_traceback(),
		)


def _rule_in_scope_users(rule) -> list[str]:
	"""In-scope users for a rule: Org = every enabled desk person who can use
	Jarvis (Jarvis User, Jarvis Admin, or System Manager), Role = holders of
	target_role, User = target_user only. Administrator/Guest are always excluded
	(service identities, filtered by ``_users_with_role``)."""
	scope = rule.scope or "Org"
	if scope == "User":
		u = rule.target_user
		if u and u not in _EXCLUDE_USERS and _is_enabled_user(u):
			return [u]
		return []
	if scope == "Role":
		if not rule.target_role:
			return []
		return _users_with_role(rule.target_role)
	# Org: every enabled person with Jarvis access. Jarvis Admin is included so an
	# admin who authors an org-wide question is also asked it (they are part of the
	# org too) — otherwise a Jarvis-Admin-only account never receives its own org
	# questions. Administrator/Guest stay excluded via _users_with_role.
	users = (
		set(_users_with_role("Jarvis User"))
		| set(_users_with_role("Jarvis Admin"))
		| set(_users_with_role("System Manager"))
	)
	return sorted(users)


def _is_enabled_user(user: str) -> bool:
	try:
		return bool(cint(frappe.db.get_value("User", user, "enabled")))
	except Exception:
		return False


# --------------------------------------------------------------------------- #
# immediate single-note ingest seam
# --------------------------------------------------------------------------- #
def enqueue_note_ingest(note_name: str) -> None:
	"""Enqueue the immediate single-note ingest (the Personalise answer /
	free-capture seam). Queue ``long``, deduped per note (a re-enqueue while the
	same note's job is still queued/running coalesces)."""
	frappe.enqueue(
		"jarvis.learning.questions._run_note_ingest",
		queue="long",
		timeout=_NOTE_INGEST_TIMEOUT_S,
		job_id=f"{_NOTE_INGEST_JOB_PREFIX}::{note_name}",
		deduplicate=True,
		note_name=note_name,
	)


def _run_note_ingest(note_name: str) -> None:
	"""Queue-``long`` worker: run the same extraction the daily sweep runs for
	ONE note, then mark it Processed + publish the receipt. A note already
	consumed (or gone) is a no-op; failures leave the note New for the daily
	sweep backstop."""
	from jarvis.learning import voice_facts

	if not frappe.db.exists(NOTE, note_name):
		return
	# Claim the note atomically before extraction: re-read its status under a row
	# lock (for_update) so this immediate job and the daily voice sweep can never
	# both process the same New note. The sweep selects New Business notes with a
	# plain read and only marks them Processed AFTER its own LLM call, so a stale
	# pre-check would leave a window where both paths extract and both append the
	# same statement to the wiki. The locking read forces a fresh committed value
	# (not this transaction's snapshot); a lost claim - already consumed, or the
	# sweep marked it Processed first - is a no-op.
	status = frappe.db.get_value(NOTE, note_name, "status", for_update=True)
	if status != "New":
		return  # already ingested (dedupe raced the daily sweep)
	note = frappe.get_doc(NOTE, note_name)
	voice_facts.process_single_note(note)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _insert_question(**fields) -> str | None:
	"""Insert one Jarvis Personalise Question (Unanswered). The controller stamps
	doc.owner from ``user`` for if_owner visibility, so ignore_permissions is
	safe regardless of the inserting identity. Best-effort — a failed insert is
	logged and skipped."""
	try:
		doc = frappe.get_doc({"doctype": QUESTION, "status": "Unanswered", **fields})
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(
			title="jarvis personalise: question insert failed",
			message=frappe.get_traceback(),
		)
		return None


def _publish_question_event(user: str) -> None:
	"""Realtime badge refresh: publish ``personalise:question`` with the user's
	current unanswered count (DESIGN.md 6b). Best-effort, never raises."""
	try:
		from jarvis.chat.events import publish_to_user

		count = frappe.db.count(QUESTION, {"user": user, "status": "Unanswered"})
		publish_to_user(user, {"kind": "personalise:question", "count": count})
	except Exception:
		pass


def _question_for_pattern(pattern) -> str:
	"""Question text for a learned-pattern row. A miner may author its own
	phrasing in ``evidence.question`` (the chat-transcript miner does) — that
	text is LLM output, so an instruction-shaped or empty override is discarded
	in favour of the generic template."""
	evidence = _evidence_dict(pattern.get("evidence"))
	override = evidence.get("question")
	if isinstance(override, str):
		q = " ".join(override.split())
		if q:
			try:
				from jarvis.learning.sanitizer import scan_instruction_injection

				if not scan_instruction_injection(q):
					return q[:MAX_QUESTION_LEN]
			except Exception:
				pass
	return _question_text(pattern.get("pattern_statement"))


def _question_text(statement: str) -> str:
	"""A generic-tone question carrying the finding's gist (never names a
	reviewer; the full detail lands in context_md)."""
	s = " ".join((statement or "").split())
	if not s:
		s = "Jarvis noticed a pattern in how you work."
	q = f"Jarvis noticed: {s} Is that right, and should it keep this in mind?"
	return q[:MAX_QUESTION_LEN]


def _pattern_context_md(pattern) -> str:
	"""The finding shown in the answer panel: the statement plus a compact
	evidence summary."""
	statement = " ".join((pattern.get("pattern_statement") or "").split())
	evidence = _evidence_dict(pattern.get("evidence"))
	lines: list[str] = []
	if statement:
		lines.append(statement)
	if evidence.get("source") == "chat":
		# Chat-mined finding: the stats template below would read as false
		# precision ("100% consistent" for an LLM inference) — give the user a
		# provenance trail (when it was said) instead so they can place it before
		# confirming.
		last_active = str(evidence.get("last_active") or "").strip()
		lines.append("")
		if last_active:
			lines.append(f"_Noticed in a chat conversation on {last_active}._")
		else:
			lines.append("_Noticed in your recent chat conversations._")
		return "\n".join(lines)[:MAX_CONTEXT_LEN]
	parts: list[str] = []
	n = cint(pattern.get("support_n"))
	if n:
		parts.append(f"seen {n} time(s)")
	users = evidence.get("users") or []
	if users:
		parts.append(f"{len(users)} user(s)")
	try:
		conf = float(pattern.get("confidence_pct") or 0)
	except (TypeError, ValueError):
		conf = 0
	if conf:
		parts.append(f"{round(conf)}% consistent")
	if parts:
		lines.append("")
		lines.append("_Evidence: " + ", ".join(parts) + "._")
	return "\n".join(lines)[:MAX_CONTEXT_LEN]


def _evidence_dict(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	if not raw:
		return {}
	try:
		parsed = frappe.parse_json(raw)
	except Exception:
		return {}
	return parsed if isinstance(parsed, dict) else {}
