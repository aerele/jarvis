"""Keep this active count-plan run's source snapshot in a private file."""

import frappe

from jarvis.chat import cycle_count_review
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _private_output
from jarvis.tools._agent_run_ctx import get_session_key


def get_cycle_count_inputs() -> dict:
	if not _private_output.is_private():
		raise InvalidArgumentError("Count-plan evidence requires private audit output.")
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError("Count-plan evidence requires a delegate run session.")
	name = frappe.db.get_value("Jarvis Agent Run", {"session_key": session_key}, "name")
	if not name:
		raise InvalidArgumentError("No agent run is bound to this session.")
	return {"rows": [cycle_count_review.get_inputs(frappe.get_doc("Jarvis Agent Run", name))]}
