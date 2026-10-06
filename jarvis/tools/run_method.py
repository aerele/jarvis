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
import json
import re

import frappe
from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

from jarvis.exceptions import InvalidArgumentError, PermissionDeniedError
from jarvis.tools import _write_risk

# S6: Jarvis's own gate, turn-entry and decision endpoints are never a tool target
# (``call_tool`` is allow_guest and ``frappe.call`` skips the HTTP-method check).
# Matched on the RESOLVED function (whitelisted and controller-method routes), so an
# alias cannot slip past, and on the name before a Server Script API lookup.
_DENIED_PREFIXES = (
	"jarvis.api.",
	"jarvis.chat.actions_api.",
	"jarvis.chat.approvals_api.",
	"jarvis.chat.pending_actions.",
	"jarvis.chat.macros_api.",
	# The Jarvis Admin's view of every user's macros. An admin's own assistant must
	# not reach it: one call would read every user's prompts into the model's context.
	"jarvis.chat.macros_admin_api.",
)
_DENIED = frozenset(
	{
		"jarvis.chat.api.send_message",
		"jarvis.chat.api.retry_message",
		"jarvis.chat.api.stop_run",
		"jarvis.chat.api.archive_conversation",
		"jarvis.chat.api.clear_chat_history",
		# Reviewer sign-offs, sharing and org-wide pushes: a human decision, never the agent's.
		"jarvis.chat.custom_skills_api.decide_skill_promotion",
		"jarvis.chat.custom_skills_api.apply_custom_skills",
		"jarvis.chat.custom_skills_api.share_custom_skill",
		"jarvis.chat.learned_api.decide_promotion",
		"jarvis.chat.learned_api.approve_learned_pattern",
		"jarvis.chat.learned_api.batch_approve",
		"jarvis.chat.learned_api.reject_learned_pattern",
		"jarvis.chat.learned_api.unapprove_learned_pattern",
		"jarvis.chat.learned_api.acknowledge_learned_pattern",
		"jarvis.chat.learned_api.restore_rejected_pattern",
		"jarvis.chat.learned_api.snooze_learned_pattern",
		"jarvis.chat.learned_api.apply_insight_skill_update",
		"jarvis.chat.learned_api.apply_learned_skills",
		"jarvis.chat.agents_api.promote_installation",
		"jarvis.chat.agents_api.demote_installation",
		"jarvis.chat.agents_api.raise_activation_ceiling",
	}
)


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

	# A site can replace a whitelisted method (the override_whitelisted_methods
	# hook, e.g. with an approval-gated make_stock_entry). Desk's handler always
	# calls the replacement, so the agent must too, never the original.
	resolved = resolve_method(method)
	for name in dict.fromkeys((method, resolved)):
		_enforce_blocklist(name)
		# By name too, so a Server Script API cannot shadow a denied endpoint's path.
		if _is_denied_path(name):
			raise PermissionDeniedError(f"method {name!r} cannot be called from a tool")

	# Classify (not fall back): the server-script map is authoritative for
	# whether a bare name is a Server Script API method.
	server_script = get_server_script_map().get("_api", {}).get(resolved)
	if server_script:
		return _run_server_script(server_script, args)
	return _run_whitelisted(resolved, args)


def resolve_method(method: str) -> str:
	"""The dotted path Desk would actually run for ``method``: the site's
	``override_whitelisted_methods`` replacement if any (frappe/handler.py)."""
	return frappe.override_whitelisted_method(method)


# The same verbs as the braked delete / cancel tools (api._BRAKE), which never run
# without a confirmation card, not even in an armed macro or an approved skill
# run. Reached through run_method they must not either.
_BRAKED_METHODS = frozenset(
	{
		"frappe.client.delete",
		"frappe.client.cancel",
		"frappe.desk.form.save.cancel",
		"frappe.desk.form.save.discard",
		"frappe.desk.reportview.delete_items",
		"frappe.desk.reportview.delete_report",
		# Delete a File, whose own on_trash removes it from disk.
		"frappe.desk.form.utils.remove_attach",
		"frappe.core.api.file.unzip_file",
	}
)
# Methods that delete or cancel only for some actions: path -> (arg name, actions).
_BRAKED_ACTIONS = {
	"frappe.desk.form.save.savedocs": ("action", frozenset({"cancel"})),
	"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs": (
		"action",
		frozenset({"cancel", "delete"}),
	),
}
# Whitelisted Document methods run through the doc-bound form (doctype + name).
_BRAKED_DOC_METHODS = frozenset({"cancel", "discard"})


def _set_value_cancels(args: dict) -> bool:
	# frappe.client.set_value refuses "docstatus" by name; a dict of values is not checked.
	return not args.get("value") and _write_risk.sets_docstatus_2(args.get("fieldname"))


def _bulk_update_cancels(args: dict) -> bool:
	docs = args.get("docs")
	if isinstance(docs, str):
		try:
			docs = json.loads(docs)
		except ValueError:
			return False
	return isinstance(docs, list) and any(_write_risk.sets_docstatus_2(d) for d in docs)


def _bulk_action_cancels(args: dict) -> bool:
	return str(args.get("action") or "").strip().lower() == "update" and _write_risk.sets_docstatus_2(
		args.get("data")
	)


def _merges(args: dict) -> bool:
	# A merge rename deletes the old record. Read as Frappe reads it (sbool).
	return bool(frappe.utils.sbool(args.get("merge")))


def _workflow_cancels(args: dict) -> bool:
	doctype = args.get("doctype")
	if not doctype:
		doc = args.get("doc")
		if isinstance(doc, str):
			try:
				doc = json.loads(doc)
			except ValueError:
				doc = None
		doctype = doc.get("doctype") if isinstance(doc, dict) else None
	return _write_risk.workflow_action_cancels(doctype, args.get("action"))


# Methods that cancel when their payload says so: path -> check on the call's args.
_BRAKED_PAYLOADS = {
	"frappe.client.save": lambda args: _write_risk.sets_docstatus_2(args.get("doc")),
	"frappe.client.set_value": _set_value_cancels,
	"frappe.client.bulk_update": _bulk_update_cancels,
	"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs": _bulk_action_cancels,
	"frappe.model.workflow.apply_workflow": _workflow_cancels,
	"frappe.model.workflow.bulk_workflow_approval": _workflow_cancels,
	"frappe.rename_doc": _merges,
	"frappe.client.rename_doc": _merges,
	"frappe.model.rename_doc.update_document_title": _merges,
}


def needs_brake(tool_args: dict | None) -> bool:
	"""True when a ``run_method`` call (its tool args) would delete or cancel, so
	it must park a confirmation card in every mode."""
	tool_args = tool_args if isinstance(tool_args, dict) else {}
	method = tool_args.get("method")
	if not isinstance(method, str) or not method:
		return False
	call_args = tool_args.get("args") if isinstance(tool_args.get("args"), dict) else {}
	if tool_args.get("doctype"):
		return method in _BRAKED_DOC_METHODS or (method == "rename" and _merges(call_args))
	for path in _call_paths(method):
		if path in _BRAKED_METHODS:
			return True
		arg, actions = _BRAKED_ACTIONS.get(path, (None, ()))
		if arg and str(call_args.get(arg) or "").strip().lower() in actions:
			return True
		if path in _BRAKED_PAYLOADS and _BRAKED_PAYLOADS[path](call_args):
			return True
	return False


def _call_paths(method: str) -> set[str]:
	"""Every name a call goes by: as asked, after the site's override, and the
	resolved function's own module path (so an alias can't dodge the brake)."""
	resolved = resolve_method(method)
	paths = {method, resolved}
	try:
		fn = frappe.get_attr(resolved)
	except Exception:
		return paths  # unresolvable: run_method refuses it on its own
	paths.add(f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', '')}")
	return paths


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
	if _is_denied(fn):
		raise PermissionDeniedError(f"method {method!r} cannot be called from a tool")

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


def _is_denied(fn) -> bool:
	return _is_denied_path(f"{getattr(fn, '__module__', '') or ''}.{getattr(fn, '__qualname__', '') or ''}")


def _is_denied_path(path: str) -> bool:
	return path in _DENIED or path.startswith(_DENIED_PREFIXES)


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
		# Explicit read-permission check rather than get_doc(check_permission=True):
		# the kwarg only exists on newer Frappe, and check_permission works on all.
		doc = frappe.get_doc(doctype, name or doctype)
		doc.check_permission("read")
	except frappe.PermissionError as e:
		raise PermissionDeniedError(str(e) or f"no permission to read {doctype} {name}") from e

	try:
		bound = getattr(doc, method)
	except AttributeError:
		raise InvalidArgumentError(f"unknown method: {method} on {doctype}")

	func = getattr(bound, "__func__", bound)
	if _is_denied(func):
		raise PermissionDeniedError(f"method {doctype}.{method} cannot be called from a tool")

	# Enforce @frappe.whitelist() on the underlying (unbound) controller function.
	try:
		frappe.is_whitelisted(func)
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
