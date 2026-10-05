"""Return the active AP run's permission-checked, bounded matching snapshot."""

import frappe

from jarvis.chat.purchase_match import get_inputs
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._agent_run_ctx import get_session_key


def get_purchase_match_inputs() -> dict:
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError("AP evidence requires a delegate run session")
	name = frappe.db.get_value("Jarvis Agent Run", {"session_key": session_key}, "name")
	if not name:
		raise InvalidArgumentError("No agent run is bound to this session")
	return get_inputs(frappe.get_doc("Jarvis Agent Run", name))
