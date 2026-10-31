"""Trusted, request-local turn identity. Identity is never execution permission."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import frappe

from jarvis._session import impersonate
from jarvis.chat import collaboration as c


@dataclass(frozen=True)
class TurnContext:
	binding: c.ActorBinding
	run_id: str


_current: ContextVar[TurnContext | None] = ContextVar("jarvis_collaboration_turn", default=None)


def current_turn() -> TurnContext | None:
	return _current.get()


def binding_for_run(run_id: str, *, live=False):
	row = frappe.db.get_value(
		"Jarvis Chat Turn", run_id, ["actor", "collaboration_binding", "conversation"], as_dict=True
	)
	if not row:
		c._deny()
	if not row.collaboration_binding:
		if c.is_collaborative(row.conversation):
			c._deny()
		return None
	key = frappe.db.get_value(c.BINDING, row.collaboration_binding, "session_key")
	binding = c.resolve_binding(key) if live else c.resolve_identity(key)
	if (row.actor, row.conversation) != (binding.actor, binding.conversation):
		c._deny()
	return binding


@contextmanager
def new_admission_context():
	"""A child's atomic seed precedes its Turn; never stamp it as its parent.

	This clears output identity only. The explicit admission adapter independently
	checks the live binding and stamps its own seed/Turn inside canonical locks.
	"""
	token = _current.set(None)
	try:
		yield
	finally:
		_current.reset(token)


@contextmanager
def turn_context(binding: c.ActorBinding, run_id: str):
	"""Only server workers/callbacks install this AFTER their independent claim.

	Late output may retain identity after revoke; no tool/confirmation is
	licensed here. Those always take current participant/grant/turn locks.
	"""
	identity = c.resolve_identity(binding.session_key)
	turn = frappe.db.get_value(
		"Jarvis Chat Turn", run_id, ["actor", "collaboration_binding", "conversation"], as_dict=True
	)
	if (
		identity != binding
		or not turn
		or (
			turn.actor != binding.actor
			or turn.collaboration_binding != binding.name
			or turn.conversation != binding.conversation
		)
	):
		c._deny()
	token = _current.set(TurnContext(binding, run_id))
	try:
		with impersonate(binding.actor):
			yield _current.get()
	finally:
		_current.reset(token)


def stamp_trusted_message(doc):
	"""Reuse server transcript writers, never generic REST/tool document writes."""
	context = current_turn()
	if not context or not doc.is_new() or doc.doctype != "Jarvis Chat Message":
		return
	if not doc.flags.ignore_permissions:
		# Normal ORM permission checks stay intact, including inside tool calls.
		return
	binding = context.binding
	if doc.conversation != binding.conversation:
		c._deny()
	for field, expected in (
		("actor", binding.actor),
		("collaboration_binding", binding.name),
		("collaboration_run_id", context.run_id),
	):
		if doc.get(field) not in (None, "", expected):
			c._deny()
	from jarvis.chat.collaboration_bindings import require_live, stamp

	dispatched = frappe.db.get_value("Jarvis Chat Turn", context.run_id, "collaboration_send_started_at")
	settled_run = (
		context.run_id if dispatched and doc.get("role") in {"assistant", "tool", "system"} else None
	)
	if not settled_run:
		require_live(binding)
	stamp(doc, binding, run_id=context.run_id, settled_run_id=settled_run)
	doc.owner = binding.actor
