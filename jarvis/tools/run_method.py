"""run_method - call a whitelisted Frappe server method OR a Server Script
API method by name.

The generic escape hatch for RPCs the dedicated tools do not wrap - ERPNext's
``make_*`` document mappers (``make_sales_invoice``, ``make_delivery_note``
...), other doctype-specific ``@frappe.whitelist()`` actions, AND
tenant-authored **Server Script** API endpoints (``script_type = "API"``).

``run_method`` dispatches by shape - it does not try one kind and fall back to
another:

* when ``doctype`` (and usually ``name``) is given, ``method`` is a whitelisted
  **controller method** on that doctype's class, run against the (doctype, name)
  document - the same dispatch Desk uses for a doc action;
* otherwise, a name registered as a Server Script API method (matched on its
  bare ``api_method`` in Frappe's server-script map) runs via the server-script
  executor - ``args`` reach it through ``frappe.form_dict``, the way the
  ``/api/method`` HTTP handler feeds them;
* otherwise it is a dotted path to a module-level ``@frappe.whitelist()`` method,
  called directly.

Absent ``doctype``, the server-script map is the source of truth for "is this a
server script"; a whitelisted dotted path can never collide with a bare
``api_method``, so the classification is unambiguous.

Runs under the calling user's identity (``jarvis.api.call_tool`` has already
``set_user``). The whitelisted path enforces ``frappe.is_whitelisted`` plus the
target method's own permission checks. A Server Script API runs through Frappe's
own executor - the ``allow_guest`` gate, then the operator-authored body in the
``safe_exec`` sandbox - i.e. exactly what a direct ``/api/method`` call would run,
but here behind run_method's confirmation gate and blocklist.

Consequential by default: it can mutate. The persona confirms before calling
it and ``api._run_tool`` audits it. ``preview`` is NOT honored for this tool:
it is one of ``api._GATED_WRITES``, so ``preview=True`` is always rejected with
an InvalidArgumentError rather than dry-run - the call always parks for human
confirmation instead.

An operator blocklist (``Jarvis Settings.run_method_blocklist``: comma/newline
fnmatch patterns) can categorically refuse targets by name - a whitelisted
method's dotted path or a Server Script API method's name - before dispatch.
"""

import fnmatch
import inspect
import re

import frappe
from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError


def run_method(
	method: str,
	args: dict | None = None,
	doctype: str | None = None,
	name: str | int | None = None,
) -> object:
	"""Call a controller method, Server Script API, or whitelisted server
	``method`` with keyword ``args``.

	With ``doctype`` set, ``method`` is a whitelisted **controller method** run
	against the ``(doctype, name)`` document (``name`` defaults to the doctype for
	a Single). Otherwise ``method`` is either the bare ``api_method`` of a Server
	Script API endpoint, or a dotted path to a ``@frappe.whitelist()`` method, e.g.
	``erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice``. Only
	registered API server scripts / whitelisted methods are callable; an
	unresolvable name raises InvalidArgumentError.

	``Jarvis Settings.run_method_blocklist`` (comma/newline-separated fnmatch
	patterns) refuses any matching target - a whitelisted dotted path, a
	server-script name, or ``<doctype>.<method>`` for a controller method - with
	PermissionDeniedError. It is a categorical hardstop layered on
	top of the always-on human confirmation gate (run_method is one of
	``api._GATED_WRITES``) and the per-target identity + whitelist checks. An
	empty or unreadable blocklist blocks nothing (fail-open); the confirmation
	gate remains the real boundary.

	Server Script API methods receive ``args`` through ``frappe.form_dict``
	(they have no Python signature) and their output - the ``frappe.flags`` the
	script set, else its ``frappe.response['message']`` - is returned as a dict.
	Whitelisted methods return their value verbatim (often a document dict).
	"""
	if not method:
		raise InvalidArgumentError("method is required")
	if args is not None and not isinstance(args, dict):
		raise InvalidArgumentError("args must be a dict")

	# Doc-bound path: `method` is a bare controller method on `doctype`, run
	# against the (doctype, name) document. The blocklist target is the composed
	# ``<doctype>.<method>`` so operators can scope by doctype or by method.
	if doctype is not None:
		if not isinstance(doctype, str) or not doctype:
			raise InvalidArgumentError("doctype must be a non-empty string")
		if name is not None and not isinstance(name, str | int):
			raise InvalidArgumentError("name must be a string")
		_enforce_blocklist(f"{doctype}.{method}")
		return _run_doc_method(method, doctype, name, args)

	_enforce_blocklist(method)

	# Classify (not fall back): the server-script map is authoritative for
	# whether a bare name is a Server Script API method.
	server_script = get_server_script_map().get("_api", {}).get(method)
	if server_script:
		return _run_server_script(server_script, args)
	return _run_whitelisted(method, args)


def _enforce_blocklist(method: str) -> None:
	for pattern in _blocklist():
		if fnmatch.fnmatch(method, pattern):
			raise PermissionDeniedError(
				f"method {method!r} is blocked by Jarvis Settings.run_method_blocklist"
			)


def _blocklist() -> tuple[str, ...]:
	"""fnmatch patterns from ``Jarvis Settings.run_method_blocklist``.

	Read via ``get_cached_doc`` (Single-doc cache, no SQL per call). Fail-open:
	a missing/unreadable settings doc or an empty field blocks nothing - the
	confirmation gate and per-target permission checks remain the boundary.
	"""
	try:
		settings = frappe.get_cached_doc("Jarvis Settings")
	except Exception:
		return ()
	raw = (settings.get("run_method_blocklist") or "").strip()
	if not raw:
		return ()
	return tuple(p.strip() for p in re.split(r"[,\n]", raw) if p.strip())


def _run_whitelisted(method: str, args: dict | None) -> dict:
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


def _run_server_script(docname: str, args: dict | None) -> dict:
	"""Execute a Server Script API endpoint, feeding ``args`` the way the
	``/api/method`` HTTP handler does - through ``frappe.form_dict`` - and
	returning its output as a dict: the ``frappe.flags`` the script set, else
	its ``frappe.response['message']``.

	``call_with_form_dict`` (Frappe's own primitive, used by its ``run_script``)
	overlays ``args`` onto ``form_dict`` for the call and restores it in a
	finally, so an injected arg never leaks into unrelated request-scoped code.
	"""
	from frappe.handler import run_server_script
	from frappe.utils.safe_exec import call_with_form_dict

	# Only capture the message THIS execution sets, not a stale prior one.
	frappe.local.response.pop("message", None)
	flags = call_with_form_dict(lambda: run_server_script(docname), args or {})
	if flags:
		return flags
	message = frappe.local.response.pop("message", None)
	return {"message": message} if message is not None else {}


def _run_doc_method(method: str, doctype: str, name: str | int | None, args: dict | None) -> object:
	"""Dispatch a whitelisted controller method against one document.

	Mirrors the dispatch core of ``frappe.handler.run_doc_method`` - load the doc
	under the caller's permission, enforce ``@frappe.whitelist()`` on the
	controller method, then call it via ``doc.run_method`` with the same arg-arity
	rules - but drops that endpoint's HTTP request/response coupling
	(``is_valid_http_method``, ``frappe.response.docs``) so it runs the same in a
	worker or a test as it does over HTTP. A missing ``name`` resolves to the
	doctype itself, i.e. the Single.
	"""
	try:
		doc = frappe.get_doc(doctype, name or doctype, check_permission=True)
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to read {doctype} {name}") from e

	try:
		bound = getattr(doc, method)
	except AttributeError:
		raise InvalidArgumentError(f"unknown method: {method} on {doctype}")

	# Enforce @frappe.whitelist() on the underlying (unbound) controller function.
	try:
		frappe.is_whitelisted(getattr(bound, "__func__", bound))
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"method {doctype}.{method} is not whitelisted") from e

	# Arg-arity handling copied from frappe.handler.run_doc_method: `bound` is a
	# bound method, so its signature excludes ``self``.
	fnargs = list(inspect.signature(bound).parameters)
	try:
		if not fnargs or (len(fnargs) == 1 and fnargs[0] == "self"):
			return doc.run_method(method)
		if "args" in fnargs or not isinstance(args, dict):
			return doc.run_method(method, args)
		return doc.run_method(method, **(args or {}))
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to call {doctype}.{method}") from e


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
