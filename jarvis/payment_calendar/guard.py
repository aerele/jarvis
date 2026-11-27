"""The one error policy of the cache's housekeeping steps.

A step that is not the main work (a purge hook, an alert row, a tick step, a company hook) must not stop the
caller for any small reason, and must never hide a reason that is big. So, for every such step:

- a job deadline (`TIMEOUTS`) propagates;
- a database error that may have rolled the transaction back or ended the connection propagates
  (`routing.may_have_rolled_back`): the caller's transaction is no longer what it was;
- with `all_database_errors` every other database error propagates too (the company hooks, which run in the
  transaction of someone else's delete and must fail it as one unit);
- anything else is logged by class (never a message, never a name) and the caller goes on.
"""

import frappe

from jarvis.payment_calendar import routing
from jarvis.payment_calendar.errors import TIMEOUTS

# The driver errors of one failed statement, by their names on `frappe.db`.
_DRIVER_ERRORS = (
	"OperationalError",
	"ProgrammingError",
	"InternalError",
	"DataError",
	"InterfaceError",
	"IntegrityError",
)


def database_errors() -> tuple[type, ...]:
	"""Every database error: the driver's classes and the two Frappe wraps for a deadlock and a lock wait."""
	driver = (getattr(frappe.db, name, None) for name in _DRIVER_ERRORS)
	return (*(error for error in driver if error), frappe.QueryDeadlockError, frappe.QueryTimeoutError)


def propagates(exc: BaseException, *, all_database_errors: bool = False) -> bool:
	"""True when a housekeeping step must not swallow `exc`."""
	if isinstance(exc, TIMEOUTS) or routing.may_have_rolled_back(exc):
		return True
	return all_database_errors and isinstance(exc, database_errors())


def guarded(step, label: str, *, all_database_errors: bool = False, on_swallow=None) -> None:
	"""Run `step`; raise what `propagates`, log any other error as `<label> failed class=<name>`.

	`on_swallow(exc)` is called for an error that was logged, so that a caller can count it.
	"""
	try:
		step()
	except Exception as exc:
		if propagates(exc, all_database_errors=all_database_errors):
			raise
		frappe.logger("payment_calendar").warning("%s failed class=%s", label, type(exc).__name__)
		if on_swallow:
			on_swallow(exc)
