"""Atomic batch executor shared by the bulk tools.

The bulk tools (`submit_doc(names=[...])`, `cancel_doc`, `update_doc`,
`delete_doc`, `apply_workflow_action`, the collab writes) all want the same
guarantee `create_docs` pioneered: run the single-doc operation over a list in
ONE savepoint-guarded transaction, all-or-nothing, so the user confirms the
whole set with a single gated card.

``run_atomic_batch`` factors out the subtle part so no tool hand-copies it:
on failure it not only ``ROLLBACK``s to the savepoint but also restores the
commit-callback queues to their pre-batch state. Without that, callbacks queued
by a doc that was inserted-then-rolled-back would survive and fire on the
request's real commit (a whitelisted endpoint that catches the error and
returns ``{ok: false}`` still responds HTTP 200, so Frappe commits at request
end). See create_docs.py history for the bug this prevents.
"""

from collections import deque

import frappe

from jarvis.exceptions import InvalidArgumentError

# Shared cap: one bulk call touches at most this many targets. Matches the
# original create_docs limit; the confirmation card and the sandbox dry-run
# both scale with it, so keep it modest.
_MAX_BATCH = 20


def run_atomic_batch(items, fn, *, label=None, max_batch=_MAX_BATCH):
	"""Run ``fn(item)`` over every item in ONE savepoint-guarded transaction.

	All-or-nothing: if any item raises, ``ROLLBACK TO SAVEPOINT`` undoes the
	whole batch, the ``before_commit`` / ``after_commit`` / ``after_rollback``
	callback queues are restored to their pre-batch state, and the ORIGINAL
	exception propagates (so ``api._translate_write_error`` still maps it by
	type) with a note naming the failed item's index. Returns the list of
	per-item ``fn`` results on success.

	``label`` (optional) maps an item to a short display string for that note.
	Callers should validate their items BEFORE calling (like create_docs) so a
	bad argument bounces before a savepoint is opened / a card is shown.
	"""
	if not isinstance(items, list) or not items:
		raise InvalidArgumentError("expected a non-empty list of items")
	if len(items) > max_batch:
		raise InvalidArgumentError(f"too many items in one batch (max {max_batch})")

	sp = "jbb_" + frappe.generate_hash(length=10)
	# frappe.db.rollback(save_point=...) only issues ROLLBACK TO SAVEPOINT - it
	# does NOT clear the callback queues. Snapshot here and restore only on
	# failure; on success leave them alone so the (kept) docs' callbacks fire.
	saved_queues = {
		name: tuple(getattr(frappe.db, name)._functions)
		for name in ("before_commit", "after_commit", "after_rollback")
		if hasattr(frappe.db, name)
	}
	frappe.db.savepoint(sp)
	results = []
	try:
		for index, item in enumerate(items):
			results.append(fn(item))
	except Exception as exc:
		frappe.db.rollback(save_point=sp)
		for name, functions in saved_queues.items():
			getattr(frappe.db, name)._functions = deque(functions)
		# Name the failing item without swallowing the exception's type/message
		# (the single-doc fn already puts the docname in its own error).
		if label is not None:
			try:
				exc.add_note(f"batch rolled back at item {index} ({label(item)}); no records were changed")
			except Exception:
				pass
		raise
	return results
