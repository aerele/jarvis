"""A macro run and the prepared reports its step waits for.

Only this module knows whether a ``run_report`` call comes from a macro run's own
step; the report tools ask it and otherwise behave as they always did. See
``.claude/workflow/plans/macro-report-wait.md`` for the design.

``macro_context`` is the gate. It is true only for the step's own turn of the OWNER's
live run, never for a message the owner typed into the run's chat, a confirmation
card's follow-up or another user, so a freshness bound meant for an unattended step
cannot leak into an ordinary conversation.
"""

import json

import frappe

from jarvis.chat.events import publish_to_user

RUN = "Jarvis Macro Run"
TURN = "Jarvis Chat Turn"
CONV = "Jarvis Conversation"
MSG = "Jarvis Chat Message"
PREPARED = "Prepared Report"
_TOOL = "run_report"
_IN_FLIGHT = ("started", "generating")
_LABEL_MAX = 140

_OFF = ("", "0", "false", "no", "off")


def enabled() -> bool:
	"""The kill switch (``jarvis_macro_report_wait`` in site config, on unless set to
	0 / false) AND a bench on the turn machine: a continuation needs a Turn row to
	end its step, so without one the feature is off, exactly as with the switch."""
	flag = frappe.conf.get("jarvis_macro_report_wait", 1)
	if isinstance(flag, str):
		flag = flag.strip().lower() not in _OFF
	if not flag:
		return False
	from jarvis.chat import admission

	return bool(admission.turn_machine_enabled())


def conversation_for_session() -> str | None:
	"""The conversation of the plugin call being served, resolved from its session key
	the way ``jarvis.api`` resolves it; None outside a plugin call."""
	from jarvis.tools import _agent_run_ctx

	session_key = _agent_run_ctx.get_session_key()
	if not session_key:
		return None
	return frappe.db.get_value(CONV, {"session_key": session_key}, "name") or None


def current_context():
	"""``macro_context`` for the plugin call being served: the session key, the
	conversation and the gate in one guarded step, so ``run_report`` can never be
	broken by it. None on any error (fail closed)."""
	try:
		return macro_context(conversation_for_session())
	except Exception:
		return None


def macro_context(conversation: str | None):
	"""The live macro run this call belongs to, as ``{name, started_at, current_step,
	resume_turn}``, or None: the call is not from that run's own step turn.

	All of: the switch is on; the conversation has a ``running`` run owned by the
	calling user, with a start time; the running turn (``held_sheets.live_turn``) is
	the current step's turn (``current_step`` counts steps SENT, so the step is index
	``current_step - 1``), or the run's ``resume_turn``, or, while no resume turn is
	set, a turn on the current step's message (the owner's Retry of the step); and the
	run is not ``waiting_failed``. Fails closed: any lookup error is None."""
	try:
		return _macro_context(conversation)
	except Exception:
		return None


def _macro_context(conversation: str | None):
	if not conversation or not enabled():
		return None
	from jarvis.chat import held_sheets, macros

	run = frappe.db.get_value(
		RUN,
		{"conversation": conversation, "status": "running", "owner": frappe.session.user},
		["name", "started_at", "current_step", "resume_turn", "waiting_failed"],
		as_dict=True,
		order_by="creation desc",
	)
	if not run or not run.started_at or frappe.utils.cint(run.waiting_failed):
		return None
	sent = frappe.utils.cint(run.current_step)
	if sent < 1:
		return None
	live = held_sheets.live_turn(conversation)
	if not live:
		return None
	is_step = live == macros._step_turn_id(run.name, sent - 1) or (
		bool(run.resume_turn) and live == run.resume_turn
	)
	if not is_step and not run.resume_turn:
		seed = frappe.db.get_value(TURN, live, "seed_message")
		is_step = bool(seed) and seed == macros._current_step_message(conversation)
	if not is_step:
		return None
	return frappe._dict(
		name=run.name,
		started_at=run.started_at,
		current_step=sent,
		resume_turn=run.resume_turn or "",
	)


# --------------------------------------------------------------------------- #
# what a step is waiting for, read at the step's end
# --------------------------------------------------------------------------- #
def step_waits(conversation: str, turn) -> tuple[list[dict], list[dict]]:
	"""``(open_waits, failed_reads)`` of the step turn that just ended, from its
	``run_report`` rows (committed before the turn's reply). Never raises: a row that
	cannot be read gives ``([], [])``, which is how the step ended before this existed.

	The turn's rows are the ``run_report`` rows after its seed message and before the
	next user message (a typed follow-up, a continuation), so another turn's rows never
	count. Nothing depends on the turn's last row: a model that calls openclaw's
	``sessions_yield`` after the waiting answer still ends the same turn.

	Same supersede rule as the chat's report card (``report_runs._open_runs``): a later
	row for the same Prepared Report, or for the same report and filters, replaces an
	earlier one. A wait is open only if its row's envelope says ``waiting: true``, which
	only a step inside a run is ever told; a bare ``started`` (the switch off, an
	ordinary chat) never parks. A failed read is the newest row of its report and
	filters being ``failed``.

	``open_waits``: ``{prepared_report, report_name, filters}`` with the filters the
	Prepared Report STORED, which is what the continuation tells the model to pass.
	``failed_reads``: ``{report_name, run, reason}``.

	A row is judged on its own: one that cannot be read is skipped (and logged once),
	never the whole answer. A row with no status (a tool error) says nothing about the
	report and supersedes nothing.

	Contract with the caller (``macros._apply_step_end``): the turn's own rows are all
	this knows. It does not tell a step's first turn from a continuation; the caller
	ends the run on a failed read only for a first turn (no ``resume_turn``). What a
	continuation's failed read means is the resume's to decide."""
	try:
		return _step_waits(conversation, turn)
	except Exception:
		frappe.log_error(title="jarvis macro report wait read failed", message=frappe.get_traceback())
		return [], []


def _step_waits(conversation: str, turn):
	from jarvis.chat import report_runs

	seed = turn.get("seed_message") if turn else None
	if not seed:
		return [], []
	after = frappe.db.get_value(MSG, seed, "seq")
	if after is None:
		return [], []
	# A retry is a second Turn on the same seed: its rows come after the failed
	# attempt's, which are in the seq range too. Only rows made since THIS turn began.
	since = frappe.db.get_value(TURN, turn.get("name"), "creation")
	owner = frappe.db.get_value(CONV, conversation, "owner")
	waits: dict = {}  # in-flight reads that said `waiting`, by Prepared Report
	newest: dict = {}  # the newest answer per report + filters
	defaults: dict = {}
	unreadable = None
	for row in _rows(conversation, after, since):
		try:
			_note(row, waits, newest, defaults)
		except Exception:
			unreadable = unreadable or frappe.get_traceback()
	if unreadable:
		frappe.log_error(title="jarvis macro report wait row skipped", message=unreadable)
	return (
		[_wait(name, w, owner) for name, w in waits.items()],
		[
			{"report_name": r["report_name"], "run": r["run"] or "", "reason": r["error"] or r["message"]}
			for r in newest.values()
			if r["status"] == "failed"
		],
	)


def _note(row: dict, waits: dict, newest: dict, defaults: dict) -> None:
	"""Fold one row, in seq order, into ``waits`` and ``newest`` (see ``step_waits``)."""
	from jarvis.chat import report_runs

	if not row["status"]:
		return  # a tool error: no envelope, so no answer for any report
	args = report_runs._json(row["tool_args"])
	report = row["report_name"] or args.get("report_name")
	if not report:
		return
	run = row["run"] or ""
	# Unreadable filters have no meaning to compare: that read stands alone.
	key = report_runs._key(report, args.get("filters"), defaults) or f"{report}\n{run}"
	for name, wait in list(waits.items()):
		if name == run or wait["key"] == key:
			del waits[name]
	newest[key] = {**row, "report_name": report}
	if run and row["status"] in _IN_FLIGHT and row["waiting"]:
		waits[run] = {"key": key, "report_name": report, "args": args.get("filters")}


def _wait(name: str, wait: dict, owner: str | None) -> dict:
	"""One awaited report with the filters its Prepared Report stored (read by name,
	and only if the run's owner made it). A report that cannot be read back (deleted
	already, not the owner's) keeps the filters the model sent: the wake-up reads it by
	name and owner again and ends the wait there if it is gone."""
	row = frappe.db.get_value(
		PREPARED, {"name": name, "owner": owner}, ["report_name", "filters"], as_dict=True
	)
	filters = wait["args"]
	if row:
		stored = _loads(row.filters)
		filters = stored if isinstance(stored, dict) else filters
	return {
		"prepared_report": name,
		"report_name": (row.report_name if row else None) or wait["report_name"],
		"filters": filters if isinstance(filters, dict) else {},
	}


def _loads(raw):
	try:
		return json.loads(raw) if isinstance(raw, str) else raw
	except ValueError:
		return None


def _rows(conversation: str, after: int, since=None) -> list[dict]:
	"""The ``run_report`` rows after seq ``after``, made at/after ``since`` (when given)
	and before the next user message, in seq order, each as ``{tool_args, status, run, report_name, waiting, error,
	message}``. On MariaDB the envelope is read in SQL (a ``ready`` answer carries up to
	2000 rows and none of them is needed here)."""
	from jarvis.chat import report_runs

	m = frappe.qb.DocType(MSG)
	until = (
		frappe.qb.from_(m)
		.select(m.seq)
		.where((m.conversation == conversation) & (m.role == "user") & (m.seq > after))
	)
	next_user = until.orderby(m.seq).limit(1).run()
	query = (
		frappe.qb.from_(m)
		.select(m.tool_args)
		.where((m.conversation == conversation) & (m.role == "tool") & (m.tool_name == _TOOL))
		.where(m.seq > after)
		.orderby(m.seq)
	)
	if since is not None:
		query = query.where(m.creation >= since)
	if next_user:
		query = query.where(m.seq < next_user[0][0])
	fields = ("status", "run", "report_name", "waiting", "error", "message")
	mariadb = report_runs._json_functions_available()
	if mariadb:
		query = query.select(*(report_runs._JsonValue(m.tool_result, f"$.data.{k}").as_(k) for k in fields))
	else:
		query = query.select(m.tool_result)
	rows = query.run(as_dict=True)
	for row in rows:
		if not mariadb:
			data = report_runs._data(row.pop("tool_result"))
			row.update({k: data.get(k) for k in fields})
		# A JSON true reads as "1" through MariaDB's JSON_VALUE and as True from the
		# Python fallback.
		row["waiting"] = str(row["waiting"]).lower() in ("true", "1")
		row["error"] = row["error"] or ""
		row["message"] = row["message"] or ""
	return rows


# --------------------------------------------------------------------------- #
# parking and failing the run at a step's end
# --------------------------------------------------------------------------- #
def failed_read_message(what: str, failed: dict) -> str:
	"""Why a run ends because a report its step read had failed: ``Step 2 stopped:
	<report> failed: <reason>``. The reason is the failed answer's own text, cut to one
	bounded, scrubbed line (it is stored on the run row and shown in a notification)."""
	from jarvis.chat import macros

	reason = macros._one_line(failed["reason"]) or "no reason was given"
	return f"{what} stopped: {failed['report_name']} failed: {reason}"


def park(run, macro_doc, waits: list[dict]) -> bool:
	"""Park ``run`` in ``waiting_report`` awaiting ``waits``: ONE fenced UPDATE of
	``running`` -> ``waiting_report`` that also stores the wait (``waiting_on``,
	``waiting_since``, ``waited``), so a run is never parked without its waits or the
	other way round. Committed at once (the wake-up runs in another worker) and
	announced. False when the run was no longer ``running`` (a stop won); the caller
	then does nothing more, not even send the next step."""
	from jarvis.chat import macros

	parked = macros._cas_run_status(
		run.name,
		"running",
		"waiting_report",
		waiting_on=json.dumps(waits),
		waiting_since=frappe.utils.now(),
		waited=1,
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- seen by other workers
	if not parked:
		return False
	run.status = "waiting_report"
	names = list(dict.fromkeys(w["report_name"] for w in waits))
	label = "Waiting for report: " + ", ".join(names)
	if len(label) > _LABEL_MAX:
		label = label[: _LABEL_MAX - 1].rstrip() + "…"
	total = frappe.utils.cint(run.total_steps)
	try:
		publish_to_user(
			macro_doc.owner,
			{
				"kind": "macro:progress",
				"macro_run": run.name,
				"macro": macro_doc.name,
				"conversation": run.conversation,
				"step": min(frappe.utils.cint(run.current_step) or 1, total or 1),
				"total": total,
				"label": label,
				"status": "waiting_report",
			},
		)
	except Exception:
		pass
	return True
