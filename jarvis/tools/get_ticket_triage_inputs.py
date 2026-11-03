"""Serve a Ticket Triage input only through the server-issued private mode."""

from jarvis.chat import ticket_triage
from jarvis.tools._agent_run_ctx import get_session_key


def get_ticket_triage_inputs() -> dict:
	"""The model supplies no scope or record argument for this private source."""
	run = ticket_triage.run_from_session(get_session_key())
	return ticket_triage.capture_private_inputs(run)
