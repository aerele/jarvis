"""Return the active AP run's permission-checked, bounded matching snapshot."""

import frappe

from jarvis.chat.purchase_match import get_inputs
from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._agent_run_ctx import get_session_key
from jarvis.tools._private_output import is_private


def get_purchase_match_inputs() -> dict:
	# An ordinary agent response would be persisted and published to the chat
	# transcript. This snapshot contains PI, PO and receipt evidence, so an older
	# plugin that cannot use the private local-input contract must stop here.
	if not is_private():
		raise InvalidArgumentError(
			"AP evidence requires private audit transport. Update the Jarvis agent plugin before retrying."
		)
	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError("AP evidence requires a delegate run session")
	name = frappe.db.get_value("Jarvis Agent Run", {"session_key": session_key}, "name")
	if not name:
		raise InvalidArgumentError("No agent run is bound to this session")
	# The row envelope is deliberate. It gives the private transport a bounded,
	# versioned shape without exposing an arbitrary raw snapshot to a transcript.
	return {"rows": [get_inputs(frappe.get_doc("Jarvis Agent Run", name))]}
