"""Stage typed Ticket Triage proposals for a named human reviewer."""

from jarvis.chat import ticket_triage
from jarvis.tools._agent_run_ctx import get_session_key


def stage_ticket_triage_proposals(capture_id: str, rows: list) -> dict:
	"""Accept only all-ticket typed rows; no text, record path, or write field."""
	run = ticket_triage.run_from_session(get_session_key(), allow_staged_retry=True)
	return ticket_triage.stage_proposals(run, capture_id, rows)

