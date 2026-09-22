"""Return THIS run's engagement configuration - the per-installation tunables the
operator set (materiality floors, engagement_risk_level, rounding_step, ...) - resolved
server-side from the caller's run session.

NO arguments. A delegate never has to know, name, or fumble the internal
``Jarvis Agent Installation`` doctype or its opaque row id: the bench resolves the run's
installation from the caller's ``session_key`` (NEVER a model-supplied id), so a delegate
can only ever read its OWN config. Only the installation's ``config`` JSON is returned -
never the row's other fields (agent_url, device tokens, run-as identity, ...).

This exists because handing a weak model the doctype name + row hash and asking it to
compose ``get_doc`` made it fumble the args on most calls; a zero-arg tool removes the
whole failure class.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError

INSTALLATION = "Jarvis Agent Installation"
RUN = "Jarvis Agent Run"


def get_engagement_config() -> dict:
	"""Return the current run's engagement config as a dict (``{}`` when none is set)."""
	from jarvis.tools._agent_run_ctx import get_session_key

	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError(
			"get_engagement_config must be called by an agent delegate over its run "
			"session (no session_key in context)"
		)
	run_row = frappe.db.get_value(RUN, {"session_key": session_key}, ["name", "installation"], as_dict=True)
	if not run_row:
		raise InvalidArgumentError("no agent run is bound to this session")
	if not run_row.installation:
		raise InvalidArgumentError("run has no installation")
	# Only the config JSON field, resolved from the trusted session - never the whole
	# installation row (which carries agent_url / device tokens / run-as identity).
	raw = frappe.db.get_value(INSTALLATION, run_row.installation, "config")
	try:
		cfg = frappe.parse_json(raw) if raw else {}
	except Exception:
		cfg = {}
	return cfg if isinstance(cfg, dict) else {}
