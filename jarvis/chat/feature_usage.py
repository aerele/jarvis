"""Which of the 7 tracked Jarvis features a user has actually used in a given
window. Backs the periodic pulse survey's dynamic feature-chip question.

Every source is read from data that already exists for its own reason - there
is no new event-log doctype and no new write on any hot path. Six sources are
plain owner/user-scoped column reads; Skills is reconstructed from stored
message content using the SAME validated slug matching
``jarvis.api._resolve_approve_run_offer`` uses to recover a past invocation
(``invoked_skill_slugs`` is the single source of truth, so the two can never
drift); Wiki is a JSON-array scan because ``frappe.qb`` has no JSON-containment
filter.

Two accuracy caveats are deliberate and carried from the design:

* **Skills** resolves against the *current* ``Jarvis Custom Skill`` state, so a
  since-disabled skill will not match a historical ``/slug`` mention (false
  negative) and a since-created skill reusing a slug matches retroactively
  (false positive). Fine for a "what do you use" prompt; NOT a historically
  accurate record.
* **Triggers** attributes via ``Jarvis Trigger Activity.event_user`` - the user
  whose document event FIRED the trigger, which is not necessarily the user who
  authored it. Same axis the triggers activity list already reports on.

Identity: ``user`` is an explicit parameter, never ``frappe.session.user``. The
reads therefore use the permission-skipping ``frappe.get_all`` /
``frappe.db.get_value`` / ``frappe.qb`` family with an explicit owner filter,
exactly as ``jarvis.chat.custom_skills.invoked_skill_slugs`` does and for the
same reason: the pulse survey may be computed for a user other than whoever is
logged in (a background continuation), where ``get_list`` would silently answer
"no features used". Nothing but a per-feature timestamp leaves this module - no
row content is returned to the caller.

See docs/superpowers/specs/2026-09-08-session-feedback-design.md.
"""

from __future__ import annotations

import json

import frappe

from jarvis.chat.custom_skills import invoked_skill_slugs

CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
MACRO_RUN = "Jarvis Macro Run"
TRIGGER_ACTIVITY = "Jarvis Trigger Activity"
CONNECTOR_LOG = "Jarvis Connector Log"
VOICE_NOTE = "Jarvis Voice Note"
WIKI = "Jarvis Wiki Page"

# The durable ``role=tool`` receipt for a jarvis__* tool is written by
# ``jarvis.api._persist_and_publish_tool_call`` from the name ``call_tool``
# received, and ``call_tool`` is dispatched on BARE registry ids (the worker
# explicitly skips persisting jarvis__* rows - see turn_handler's ``is_jarvis``
# branch). So the bare form is what is actually in the table; the prefixed form
# is kept only so a runtime that ever persists the agent-side name still counts.
_DASHBOARD_TOOLS = (
	"save_dashboard",
	"save_agent_dashboard",
	"jarvis__save_dashboard",
	"jarvis__save_agent_dashboard",
)

# Bounded: one user's own recent messages, not fleet-wide. A user whose only
# skill invocation in the window is older than this many slug-shaped messages
# reads as "no skills used" - acceptable for a survey prompt.
_SKILLS_SCAN_LIMIT = 200
_WIKI_SCAN_LIMIT = 500

#: Feature keys this module can report, in the order they are computed.
FEATURES = (
	"file_box",
	"dashboard_builder",
	"skills",
	"macros",
	"triggers_connectors",
	"voice_chat",
	"wiki",
)


def get_used_features(user: str, since) -> dict[str, str]:
	"""``{feature: last_used}`` for every one of the 7 tracked features ``user``
	has used since ``since``. A feature absent from the dict was not used in the
	window, so an empty dict means "hide the question".

	One source's failure must drop that one feature's entry, never blank the
	whole survey - each source runs inside its own guard.
	"""
	since_dt = frappe.utils.get_datetime(since) if since else None
	if not user or not since_dt:
		return {}
	out: dict[str, str] = {}
	# Looked up through module globals on every call so a caller (or a test)
	# that swaps one source function out is honoured.
	for name in FEATURES:
		try:
			last = globals()[f"_{name}"](user, since_dt)
		except Exception:
			frappe.log_error(
				title=f"get_used_features: {name} source failed",
				message=frappe.get_traceback(),
			)
			last = None
		if last:
			out[name] = last
	return out


def _iso(value) -> str | None:
	"""A Datetime/Date column value (or ``None``) as a sortable string."""
	return str(value) if value else None


def _latest(*values) -> str | None:
	"""The newest of several already-stringified timestamps, ignoring blanks.
	ISO-shaped strings sort chronologically, so ``max`` is the right pick."""
	present = [v for v in values if v]
	return max(present) if present else None


def _file_box(user: str, since) -> str | None:
	"""A File-Box drop creates its conversation with ``file_box=1`` at drop time
	(``jarvis.chat.filebox.drop_file``), so ``creation`` IS the usage moment -
	no later toggle to chase."""
	# get_all/get_value here read under the explicit `user` argument, not the
	# session (see module docstring).
	return _iso(
		frappe.db.get_value(
			CONV,
			{"owner": user, "file_box": 1, "creation": [">=", since]},
			"creation",
			order_by="creation desc",
		)
	)


def _dashboard_builder(user: str, since) -> str | None:
	"""Creating a dashboard, never viewing one. Attribution is by CONVERSATION
	owner: Jarvis Chat Message carries no user field and a tool receipt's own
	``owner`` is whichever server identity wrote it, so this joins exactly the
	way ``jarvis.chat.usage_push`` already attributes tool calls."""
	msg = frappe.qb.DocType(MSG)
	conv = frappe.qb.DocType(CONV)
	rows = (
		frappe.qb.from_(msg)
		.inner_join(conv)
		.on(msg.conversation == conv.name)
		.select(msg.creation)
		.where(
			(conv.owner == user)
			& (msg.role == "tool")
			& (msg.tool_name.isin(_DASHBOARD_TOOLS))
			& (msg.creation >= since)
		)
		.orderby(msg.creation, order=frappe.qb.desc)
		.limit(1)
	).run(as_dict=True)
	return _iso(rows[0].creation) if rows else None


def _skills(user: str, since) -> str | None:
	"""Re-resolve ``/slug`` invocations out of the user's own stored messages.

	``invoked_skill_slugs`` stays the single authority on "did this message
	invoke a skill", but its owner/shared/role lookups depend only on ``user``,
	never on the message - so calling it once per candidate row would be a query
	loop. Instead: ONE resolution pass over the joined candidate text tells us
	which slugs resolve at all (the regex is ``(?:^|\\s)/slug``, and joining on
	"\\n" keeps every row's leading boundary intact, so the joined match set is
	exactly the union of the per-row match sets). Rows are then walked
	newest-first with a cheap substring prefilter, and the canonical function
	makes the final per-row call - typically two round-trips in total.

	``include_org_wide=False`` (code review on #580): an unrestricted Org-scope
	skill is invocable by every user in the tenant, so a mention of one is not
	evidence THIS user set up or personalized anything - it would otherwise
	make "Skills" read as used for someone who only ever typed a slug everyone
	shares, inflating the pulse survey's per-user signal.
	"""
	rows = frappe.get_all(
		MSG,
		filters={
			"owner": user,
			"role": "user",
			"hidden": 0,
			"content": ["like", "%/%"],
			"creation": [">=", since],
		},
		fields=["content", "creation"],
		order_by="creation desc",
		limit_page_length=_SKILLS_SCAN_LIMIT,
	)
	contents = [(row.content or "") for row in rows]
	resolved = invoked_skill_slugs("\n".join(contents), user=user, include_org_wide=False)
	if not resolved:
		return None
	tokens = tuple(f"/{slug}" for slug in resolved)
	for row, content in zip(rows, contents, strict=True):
		if not any(token in content for token in tokens):
			continue
		if invoked_skill_slugs(content, user=user, include_org_wide=False):
			return _iso(row.creation)
	return None


def _macros(user: str, since) -> str | None:
	"""Macro runs are attributed by the run row's own ``owner``: every writer
	hands ownership to the macro owner right after insert
	(``jarvis.chat.macros.run_macro``, ``macro_scheduler``), the table carries a
	composite ``(owner, creation)`` index for exactly this filter+sort, and the
	list/stats endpoints already read it this way.

	Deliberately NOT joined through ``Jarvis Macro Run.conversation``: that link
	is nulled when a conversation is deleted (``jarvis/chat/api.py`` -
	``UPDATE tabJarvis Macro Run SET conversation = NULL``), so a join would
	silently lose runs. ``creation`` rather than ``started_at`` for the same
	reason it is indexed - and because ``started_at`` is unset on a run that
	never started.
	"""
	return _iso(
		frappe.db.get_value(
			MACRO_RUN,
			{"owner": user, "creation": [">=", since]},
			"creation",
			order_by="creation desc",
		)
	)


def _triggers_connectors(user: str, since) -> str | None:
	"""One chip for both automation surfaces. ``event_user`` is the user whose
	document event fired the trigger (see the module docstring's caveat);
	``Jarvis Connector Log.user`` is the caller of the connector."""
	trigger = _iso(
		frappe.db.get_value(
			TRIGGER_ACTIVITY,
			{"event_user": user, "creation": [">=", since]},
			"creation",
			order_by="creation desc",
		)
	)
	connector = _iso(
		frappe.db.get_value(
			CONNECTOR_LOG,
			{"user": user, "creation": [">=", since]},
			"creation",
			order_by="creation desc",
		)
	)
	return _latest(trigger, connector)


def _voice_chat(user: str, since) -> str | None:
	"""Voice notes and mic dictation are one chip. Every ``Jarvis Voice Note``
	kind counts (the doctype IS the notes feature; Text/Attachment/Link are the
	same capture surface), matching the design's "filtered by owner" source.

	``via_voice`` is added by the send-time dictation task later in this train,
	so guard on the column: running ahead of that migrate must degrade to the
	Voice Note signal, not raise.
	"""
	note = _iso(
		frappe.db.get_value(
			VOICE_NOTE,
			{"owner": user, "creation": [">=", since]},
			"creation",
			order_by="creation desc",
		)
	)
	dictated = None
	if frappe.db.has_column(MSG, "via_voice"):
		dictated = _iso(
			frappe.db.get_value(
				MSG,
				{"owner": user, "via_voice": 1, "creation": [">=", since]},
				"creation",
				order_by="creation desc",
			)
		)
	return _latest(note, dictated)


def _wiki(user: str, since) -> str | None:
	"""``sources`` is a JSON provenance array per page
	(``jarvis.chat.wiki.append_source`` -> ``{date, kind, ref, user}``, newest
	20 kept) and ``frappe.qb`` has no JSON-containment filter, so this scans in
	Python.

	Bounded twice over: every writer appends a source through ``doc.save()`` /
	``doc.insert()`` (no ``update_modified=False`` path touches ``sources``), so
	a page carrying an entry from the window must itself have been modified in
	the window - the ``modified`` filter is a sound superset, not a heuristic.

	``date`` is day-granular, so the cutoff is compared at day granularity: a
	page edited earlier on the cutoff day still counts. The returned value is
	therefore a ``YYYY-MM-DD`` date, not a datetime, unlike the other sources.
	"""
	since_str = str(since)[:10]
	pages = frappe.get_all(
		WIKI,
		filters={"modified": [">=", since_str]},
		fields=["sources"],
		order_by="modified desc",
		limit_page_length=_WIKI_SCAN_LIMIT,
	)
	latest = None
	for page in pages:
		if not page.sources:
			continue
		try:
			entries = json.loads(page.sources)
		except Exception:
			continue
		if not isinstance(entries, list):
			continue
		for entry in entries:
			if not isinstance(entry, dict) or entry.get("user") != user:
				continue
			date = entry.get("date") or ""
			if date >= since_str:
				latest = _latest(latest, date)
	return latest
