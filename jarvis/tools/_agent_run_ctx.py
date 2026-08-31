"""Request-scoped carrier for the plugin caller's ``session_key``.

The plugin auth path (``jarvis.api.call_tool`` → ``_dispatch_from_session``)
resolves the agent ``X-Jarvis-Session`` header to a ``session_key`` and then
runs the tool under the mapped user. A couple of tools need that same
``session_key`` to correlate the call back to the row the bench minted for it —
notably ``record_agent_run`` (Phase 3), which resolves its ``Jarvis Agent Run``
from ``Run.session_key == <the caller's session_key>``.

``session_key`` is NOT a tool argument (the LLM must never author it — it is the
delegate's opaque bearer, delivered over the HTTPS header). So the dispatcher
stashes it on ``frappe.local`` for the duration of the request and the tool reads
it here. Absent request context (direct-Python / tests) the getter returns None;
tests set it explicitly via :func:`set_session_key`.
"""

import frappe

_ATTR = "jarvis_plugin_session_key"


def set_session_key(session_key: str | None) -> None:
	"""Record the plugin caller's session_key for this request (dispatcher only)."""
	setattr(frappe.local, _ATTR, (session_key or "") or None)


def get_session_key() -> str | None:
	"""The plugin caller's session_key for this request, or None when unset
	(standard Frappe auth, direct-Python, or a test that did not set it)."""
	return getattr(frappe.local, _ATTR, None)


def clear_session_key() -> None:
	"""Drop the stashed session_key at the end of a dispatch (defensive: a
	request object is reused across calls in some worker configurations)."""
	if hasattr(frappe.local, _ATTR):
		try:
			delattr(frappe.local, _ATTR)
		except Exception:
			setattr(frappe.local, _ATTR, None)


_ARMED_ATTR = "jarvis_armed_by_macro"


def set_armed_by_macro(macro: str | None) -> None:
	"""Record that the write just dispatched ran uncarded under an armed macro's
	skip_confirmation (the gate's armed branch sets this). Read once by the receipt
	persist so the row is labelled ``auto_applied`` with this macro as provenance."""
	setattr(frappe.local, _ARMED_ATTR, (macro or "") or None)


def take_armed_by_macro() -> str | None:
	"""Read AND clear the armed-by-macro marker (consume-once), so an armed write's
	receipt is labelled exactly once and the marker can never leak onto the next,
	unrelated tool call's receipt in a reused request/worker."""
	val = getattr(frappe.local, _ARMED_ATTR, None)
	if hasattr(frappe.local, _ARMED_ATTR):
		try:
			delattr(frappe.local, _ARMED_ATTR)
		except Exception:
			setattr(frappe.local, _ARMED_ATTR, None)
	return val
