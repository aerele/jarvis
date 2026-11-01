"""Capture immutable AR evidence for this active delegate; no ERP writes."""

import frappe

from jarvis.chat.receivables_review import get_inputs
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._agent_run_ctx import get_session_key
from jarvis.tools._private_output import require_source_transport, source_envelope


def get_receivables_review_inputs() -> dict:
	require_source_transport("get_receivables_review_inputs")
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError("AR evidence requires a delegate run session")
	name = frappe.db.get_value("Jarvis Agent Run", {"session_key": session_key}, "name")
	if not name:
		raise InvalidArgumentError("No agent run is bound to this session")
	return source_envelope(get_inputs(frappe.get_doc("Jarvis Agent Run", name)))
