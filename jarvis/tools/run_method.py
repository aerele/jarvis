"""run_method - call a whitelisted Frappe server method by name.

The generic escape hatch for whitelisted RPCs the dedicated tools do not
wrap - e.g. ERPNext's ``make_*`` document mappers
(``make_sales_invoice``, ``make_delivery_note`` ...) and doctype-specific
whitelisted actions. Runs under the calling user's identity
(``jarvis.api.call_tool`` has already ``set_user``), so the method's own
``@frappe.whitelist()`` gate and permission checks apply.

Consequential by default: it can mutate. The persona confirms before
calling it and ``api._run_tool`` audits it. ``preview`` is NOT honored for
this tool: it is one of ``api._GATED_WRITES``, so ``preview=True`` is always
rejected with an InvalidArgumentError rather than dry-run - the call always
parks for human confirmation instead.
"""

import fnmatch
import inspect

import frappe

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError


def run_method(method: str, args: dict | None = None) -> dict:
	"""Call a whitelisted server method ``method`` with keyword ``args``.

	``method`` is a dotted path, e.g.
	``erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice``.
	Only ``@frappe.whitelist()`` methods are callable; anything else raises
	PermissionDeniedError. An optional site-config allowlist
	(``jarvis_run_method_allowlist``: a list of fnmatch patterns) further
	narrows what may be called - RECOMMENDED in production.

	SECURITY / operator hardening (security review PART 4 REVISED, TASK 42 —
	FLAGGED for owner decision): the default is deliberately ``None`` =
	unrestricted. This is a defense-in-depth gap (a prompt-injected agent running
	as a Jarvis User could invoke ANY whitelisted method the user can reach),
	tempered by two existing controls: run_method is one of
	``api._GATED_WRITES`` (ALWAYS parks for human confirmation, never
	auto-applies) and runs under the caller's identity (each target self-checks
	its own ``@frappe.whitelist`` gate + permissions). A hardcoded in-code default
	was deliberately NOT shipped: run_method is the generic escape hatch for BOTH
	the ``make_*`` mappers AND arbitrary doctype-specific whitelisted actions, so
	a narrow default (e.g. ``["*make_*"]``) would silently break legitimate agent
	flows across every tenant. Managed tenants SHOULD set a conservative
	``jarvis_run_method_allowlist`` in site_config.json scoped to their known-good
	method set, e.g.::

	    "jarvis_run_method_allowlist": [
	        "erpnext.*.make_*",
	        "erpnext.accounts.*"
	    ]

	Returns the method's return value verbatim (often a document dict).
	"""
	if not method:
		raise InvalidArgumentError("method is required")
	if args is not None and not isinstance(args, dict):
		raise InvalidArgumentError("args must be a dict")

	# An explicit empty allowlist ([]) means "block everything"; only a missing
	# (None) allowlist means "no restriction". `if allow and ...` would treat []
	# as unset and silently allow all - the opposite of the operator's intent.
	allow = frappe.conf.get("jarvis_run_method_allowlist")
	if allow is not None and not any(fnmatch.fnmatch(method, pat) for pat in allow):
		raise PermissionDeniedError(f"method {method!r} is not in jarvis_run_method_allowlist")

	# Resolve the method. Only a genuinely missing module/attribute means
	# "unknown method"; an error raised *during* the target module's import is a
	# real bug and must surface (don't mask it behind "unknown method").
	try:
		fn = frappe.get_attr(method)
	except (AttributeError, ModuleNotFoundError, frappe.AppNotInstalledError):
		raise InvalidArgumentError(f"unknown method: {method}")

	# Enforce @frappe.whitelist(): raises frappe.PermissionError if not.
	try:
		frappe.is_whitelisted(fn)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"method {method} is not whitelisted") from e

	# Surface a mistyped/extra arg name instead of letting frappe.call silently
	# drop it (get_newargs filters to the signature), which would run the method
	# with the arg missing and return a wrong/empty result the user then trusts.
	_reject_unknown_args(fn, method, args)

	try:
		return frappe.call(fn, **(args or {}))
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to call {method}") from e


def _reject_unknown_args(fn, method: str, args: dict | None) -> None:
	if not args:
		return
	try:
		params = inspect.signature(fn).parameters
	except (TypeError, ValueError):
		return  # can't introspect (builtin/C func) - let frappe.call handle it
	if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
		return  # method takes **kwargs - it accepts anything
	unknown = sorted(k for k in args if k not in params)
	if unknown:
		raise InvalidArgumentError(f"method {method} does not accept argument(s): {', '.join(unknown)}")
