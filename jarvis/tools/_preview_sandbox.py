"""Shared dry-run sandbox for the preview paths (api._run_preview, preview_doc).

MariaDB facts this encodes: a real COMMIT releases all savepoints (so commits
are neutralized for the duration); a full-transaction abort (deadlock) also
releases them (so the rollback tolerates 1305); and a savepoint rollback does
NOT clear the before/after-commit callback queues, so webhook/notification
enqueues for the rolled-back doc are dropped here or they would fire on the
request's next real commit. Side effects fired directly inside hooks (inline
HTTP calls) are not sandboxed.
"""

from collections import deque
from contextlib import contextmanager

import frappe


@contextmanager
def preview_sandbox():
	"""Run the body with every DB effect rolled back and nothing queued."""
	db = frappe.db
	real_commit = db.commit
	saved_queues = {
		name: tuple(getattr(db, name)._functions)
		for name in ("before_commit", "after_commit")
		if hasattr(db, name)
	}
	sp = "jps_" + frappe.generate_hash(length=10)
	db.commit = lambda *args, **kwargs: None
	try:
		db.savepoint(sp)  # inside the try: a failure here must still restore commit
		yield
	finally:
		db.commit = real_commit
		try:
			db.rollback(save_point=sp)
		except Exception:
			pass  # savepoint already released by a full-transaction abort
		for name, functions in saved_queues.items():
			getattr(db, name)._functions = deque(functions)
