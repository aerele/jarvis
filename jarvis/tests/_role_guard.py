"""Make ``frappe.only_for()`` denial assertions hold on Frappe 15 and 16.

Frappe 15's ``only_for()`` returns early when ``frappe.local.flags.in_test`` is
set, so under the test runner a denial test never exercises the role check and
``assertRaises(PermissionError)`` fails. Frappe 16 removed that bypass.
Production requests never set the flag, so the guard is enforced on both
majors; only the test runner's view differs.

Following the :mod:`jarvis.compat` doctrine, this probes the behavior itself
(does ``only_for``'s source consult the flag?) rather than branching on
``frappe.__version__``, so the shim self-retires wherever the bypass is gone.
"""

import contextlib
import functools
import inspect

import frappe


@contextlib.contextmanager
def enforced_role_guards():
	"""Clear ``flags.in_test`` around a call so ``only_for()`` checks roles.

	Wrap ONLY the denial assertion: the guard raises before the endpoint body
	runs, so nothing else observes the cleared flag.
	"""
	if not _only_for_bypasses_in_test():
		yield
		return
	flags = frappe.local.flags
	prev = flags.get("in_test")
	flags.in_test = False
	try:
		yield
	finally:
		flags.in_test = prev


@functools.cache
def _only_for_bypasses_in_test() -> bool:
	try:
		return "in_test" in inspect.getsource(frappe.only_for)
	except (OSError, TypeError):
		return False
