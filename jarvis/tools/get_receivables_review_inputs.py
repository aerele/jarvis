"""Keep immutable AR evidence in a private delegate file; never return source rows to a model."""

import frappe

from jarvis.chat.receivables_review import get_inputs
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools import _private_output
from jarvis.tools._agent_run_ctx import get_session_key


def get_receivables_review_inputs() -> dict:
	if not _private_output.is_private():
		raise InvalidArgumentError("AR evidence requires private audit output.")
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError("AR evidence requires a delegate run session")
	name = frappe.db.get_value("Jarvis Agent Run", {"session_key": session_key}, "name")
	if not name:
		raise InvalidArgumentError("No agent run is bound to this session")
	return {"rows": [get_inputs(frappe.get_doc("Jarvis Agent Run", name))]}
