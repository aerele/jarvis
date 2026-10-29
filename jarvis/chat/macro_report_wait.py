"""A macro run and the prepared reports its step waits for.

Only this module knows whether a ``run_report`` call comes from a macro run's own
step; the report tools ask it and otherwise behave as they always did. See
``.claude/workflow/plans/macro-report-wait.md`` for the design.

``macro_context`` is the gate. It is true only for the step's own turn of the OWNER's
live run, never for a message the owner typed into the run's chat, a confirmation
card's follow-up or another user, so a freshness bound meant for an unattended step
cannot leak into an ordinary conversation.
"""

import frappe

RUN = "Jarvis Macro Run"
TURN = "Jarvis Chat Turn"
CONV = "Jarvis Conversation"

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
