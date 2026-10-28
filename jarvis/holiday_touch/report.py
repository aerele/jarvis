"""One place the holiday touch writes an Error Log row.

Two rules, both learnt from incidents on request paths:

* The row is written with ``defer_insert=True``. The page boot and ``get_today`` are GET
  requests, and Frappe rolls a GET back at the end of the request (``frappe/app.py``
  ``sync_database``). On MariaDB the Error Log table is MyISAM, which a rollback cannot
  touch, but on any transactional engine a row inserted the normal way would be rolled back
  while the hourly marker below stays set, muting the fault for an hour with nothing logged.
  A deferred insert goes through Redis and is written by the scheduler, outside the
  request's transaction, so it is safe on every engine and adds no database write to the
  request.
* At most one row per ``kind`` per hour per site. The marker is one atomic ``SET NX EX``,
  so of several workers failing together exactly one logs. If Redis cannot be used, log
  anyway: a repeated error is better than a lost one. Each kind has its own marker, so a
  noisy fault cannot hide a different one.

``message`` is always passed to Frappe (the traceback without local variables by default):
without it Frappe appends the frame locals, which in this feature can hold a date of birth.
Never raises.
"""

from __future__ import annotations

import frappe

PREFIX = "jarvis:holiday:log_hour:"
INTERVAL = 3600  # seconds between two rows of the same kind


def report(kind: str, title: str, message: str | None = None) -> None:
	"""Log the current exception (or ``message``) under ``title``, at most once an hour per
	``kind``. ``message`` is for faults that are not an exception in flight and must never
	carry payload content."""
	try:
		if not _due(kind):
			return
		frappe.log_error(
			title=title,
			message=message or frappe.get_traceback(with_context=False),
			defer_insert=True,
		)
	except Exception:
		pass


def _due(kind: str) -> bool:
	try:
		cache = frappe.cache()
		return bool(cache.set(name=cache.make_key(PREFIX + kind), value=1, nx=True, ex=INTERVAL))
	except Exception:
		return True
