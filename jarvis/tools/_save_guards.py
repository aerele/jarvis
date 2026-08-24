"""Shared entry guards for the agent-session dashboard-save tools (jarvis#887).

``save_dashboard`` (chat/builder) and ``save_agent_dashboard`` (delegate runs)
persist agent-authored HTML through different backends, but share the same three
guards: a non-empty document, a caller ``session_key`` (never a model-supplied
id), and mapping the persistence layer's ``ValidationError`` onto
``InvalidArgumentError`` so the model reads the message and re-saves instead of
dying on a 500. Factored here so the two tools cannot drift.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe

from jarvis.exceptions import InvalidArgumentError


def require_html(html: str | None, tool: str) -> None:
	"""Reject an empty/blank document before any work; ``tool`` names the caller
	in the message the model sees."""
	if not (html or "").strip():
		raise InvalidArgumentError(f"{tool} requires a non-empty html document")


def require_session_key(no_key_message: str) -> str:
	"""The caller's plugin ``session_key``, or ``InvalidArgumentError`` when absent.

	Absent outside a plugin dispatch (standard Frappe auth, direct-Python, or a
	test that did not set it), so a save tool with no session context refuses
	rather than guessing an identity.
	"""
	from jarvis.tools._agent_run_ctx import get_session_key

	session_key = get_session_key()
	if not session_key:
		raise InvalidArgumentError(no_key_message)
	return session_key


@contextmanager
def validation_as_invalid_argument():
	"""Map the persistence layer's ``ValidationError`` (caps, scope, sources
	shape, the theme validator) onto ``InvalidArgumentError`` — the model gets the
	validator's message and re-saves a fixed document instead of dying on a 500."""
	try:
		yield
	except frappe.ValidationError as e:
		raise InvalidArgumentError(str(e))
