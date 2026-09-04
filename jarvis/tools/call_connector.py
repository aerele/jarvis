"""call_connector - invoke one action on a configured MCP connector (GitHub,
Atlassian, Linear, Stripe, or a custom gateway) on the calling user's behalf.

Consequential by default: it can create, change or delete data in the external
service, so ``jarvis.api`` keeps it in ``_WRITE_TOOLS`` (audited) and
``_GATED_WRITES`` (a write parks for a human confirmation, never auto-applies)
and never sandbox-previews it (the outbound call is real and cannot be rolled
back like a DB write, so a parked card gets a described-intent summary, not a
dry run - see ``run_method``'s own docstring for the same reasoning). The confirm
gate is ACTION-AWARE, though (``jarvis.api._connector_call_is_safe_read``): a
connector action the user has explicitly enabled that is marked read-only and
non-destructive runs WITHOUT a card, like an ordinary read - still audited.
Writes, destructive, not-yet-enabled and unknown actions still park.

Runs under the calling user's identity: by the time this fires,
``jarvis.api._dispatch_from_session`` has already impersonated the real end
user, so ``frappe.session.user`` IS that user and the connector row
resolution (a Personal row wins over a Shared one of the same key),
credential decrypt, allowed-actions gate, argument validation, SSRF guard,
circuit breaker and audit log all apply as them - none of that lives here,
it is ``jarvis.connectors.broker.call``'s job, and that function never raises
into this turn. This tool adds exactly two things the broker does not own,
both checked FIRST so a call that cannot succeed never reaches it at all: the
site-wide kill switch (``Jarvis Settings.connectors_enabled``, checked via
``jarvis.tools._connector_gate``), and a readiness check (an enabled
connector whose configuration has never passed a connection test - the SPA
clears that status on every credential/base_url edit - gets a clear
``connector_not_ready`` error instead of a confusing failure deeper in the
broker or the MCP client).

A delegate / marketplace-agent run must have ``jarvis__call_connector`` named
explicitly in its snapshotted capability contract to reach this tool at all -
see ``jarvis.tools._delegate_capability`` for why that holds even for a
legacy (pre-snapshot) run: using a user's personal connector credential from a
marketplace agent is a privilege escalation, not an ordinary tool call.
"""

from __future__ import annotations

from jarvis.connectors import broker
from jarvis.tools._agent_run_ctx import get_session_key
from jarvis.tools._connector_gate import connectors_enabled

_NOT_READY_ERROR = {
	"ok": False,
	"error": {
		"code": "connector_not_ready",
		"message": "This connector needs to be tested in Settings before it can be used.",
	},
}


def call_connector(connector: str, action: str, args: dict | None = None) -> dict:
	"""Call ``action`` on the connector named ``connector`` (its key, e.g.
	``"github"``) with keyword ``args``.

	Confirm-first for anything that changes state: a connector action that can
	create, modify or delete data in an external service under the caller's own
	credential is parked for a human click. A read-only, non-destructive action
	the user has explicitly enabled runs directly with no card (the bench decides
	this per call from the connector's allowed-actions configuration; unknown or
	not-yet-enabled actions confirm too). ``preview`` is not supported - there is
	nothing to dry-run against a live third-party API.

	Returns the broker's structured result verbatim: ``{"ok": True, "result":
	<the MCP tools/call result>}`` on success, or ``{"ok": False, "error":
	{"code", "message"}}`` on any failure (unknown connector, disabled
	connector, connector not yet tested (``connector_not_ready``), action not
	allowed, bad arguments, SSRF/egress block, transport failure, circuit
	open, at capacity, or the connector's own tool-execution error). Never
	raises.
	"""
	if not connectors_enabled():
		return {
			"ok": False,
			"error": {
				"code": "connectors_disabled",
				"message": "Connectors are not enabled for this workspace.",
			},
		}
	row = broker.resolve_for_status(connector)
	# Only intercept an ENABLED-but-untested row with the more specific
	# connector_not_ready error. A row that is unknown or explicitly disabled
	# falls through to broker.call unchanged, so the model sees THAT error
	# (connector_not_found / connector_disabled) rather than a misleading
	# "needs to be tested" for a connector an admin turned off on purpose.
	if row is not None and row.get("enabled") and not _has_passed_test(row):
		return _NOT_READY_ERROR
	return broker.call(connector, action, args, run_id=get_session_key())


def _has_passed_test(row) -> bool:
	"""An enabled connector may only be called once it has an actual PASSING
	connection test on file: ``last_test_status`` and ``tools_cache`` both go
	blank the moment the SPA's ``update_connector`` sees a credential/base_url
	edit (a stale cache would otherwise validate arguments - or not - against
	a schema that no longer describes what is actually listening at that
	URL), so either being empty means the connector's current configuration
	has never been proven to work."""
	if (row.get("last_test_status") or "") != "Passed":
		return False
	return bool(row.get("tools_cache"))
