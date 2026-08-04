"""DEPRECATED shim - bulk create now lives in ``create_doc(docs=[...])``.

Kept as a thin, behaviour-preserving delegate so already-shipped agent
plugins / containers that still advertise ``jarvis__create_docs`` keep
resolving on a newer bench (no ToolNotFoundError during rollout). New callers
should use ``create_doc(docs=[...])`` - the persona points there. This module
+ its ``_TOOL_NAMES`` / plugin entry can be removed once no plugin references
``create_docs``.
"""

from jarvis.tools.create_doc import create_doc


def create_docs(docs: list, notes: list | None = None) -> dict:
	"""Delegate to ``create_doc``'s batch path (identical behaviour + return:
	``{"created":[{"doctype","name"}], "notes":[...]}``, one atomic savepoint)."""
	return create_doc(docs=docs, notes=notes)
