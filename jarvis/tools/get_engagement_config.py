"""Return THIS run's immutable engagement-configuration snapshot.

NO arguments. A delegate never has to know, name, or fumble the internal
``Jarvis Agent Installation`` doctype or its opaque row id: the bench resolves a RUNNING
``Jarvis Agent Run`` from the caller's ``session_key`` (NEVER a model-supplied id), so a
delegate can only ever read its OWN server-stamped launch snapshot. The snapshot contains
only declared/derived keys. It never falls back to mutable installation configuration.

This exists because handing a weak model the doctype name + row hash and asking it to
compose ``get_doc`` made it fumble the args on most calls; a zero-arg tool removes the
whole failure class.
"""

from __future__ import annotations

import frappe

from jarvis.exceptions import InvalidArgumentError

RUN = "Jarvis Agent Run"


def get_engagement_config() -> dict:
	"""Return this active run's launch snapshot (``{}`` when no values were stamped)."""
	from jarvis.tools._agent_run_ctx import get_session_key

	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError(
			"get_engagement_config must be called by an agent delegate over its run "
			"session (no session_key in context)"
		)
	run_row = frappe.db.get_value(
		RUN,
		{"session_key": session_key, "status": "running"},
		["name", "engagement_config_json"],
		as_dict=True,
	)
	if not run_row:
		raise InvalidArgumentError("no active agent run is bound to this session")
	# A legacy/empty/malformed snapshot is an intentional fail-closed {}.  In
	# particular, NEVER read Installation.config here: an operator may edit it after
	# launch and that edit must not change this run's evidence or gate decisions.
	raw = run_row.engagement_config_json
	try:
		cfg = frappe.parse_json(raw) if raw else {}
	except Exception:
		# The immutable field should always be JSON, but a bad legacy/manual row must
		# degrade to unset rather than inheriting mutable installation settings.
		frappe.log_error(
			title=f"jarvis get_engagement_config: bad launch snapshot on {run_row.name}",
			message=frappe.get_traceback(),
		)
		cfg = {}
	return cfg if isinstance(cfg, dict) else {}
