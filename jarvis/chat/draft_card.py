"""A parked ``create_doc`` / ``update_doc`` call, marked as a DRAFT card.

When the agent proposes ONE record, the card the server parks is a draft the person
can read as a form: ``card["draft"]`` carries the call's values by field name and
``card["title"]`` names it. Both go on the card BEFORE it is sealed and parked
(``api._run_tool``), so the seal's card hash covers them.

Dark behind the site flag ``jarvis_draft_cards`` (``enabled``, default off): with it
off nothing here changes a card.

``is_draft_call`` and ``payload_from`` are pure (the site's field lookups are passed
in), so their cases run without a site; ``payload`` and ``attach`` read the site.
"""

from __future__ import annotations

from jarvis.chat import live_draft

_TOOLS = ("create_doc", "update_doc")
_TABLE_TYPES = ("Table", "Table MultiSelect")


def enabled() -> bool:
	"""The site flag. Off (0) unless ``jarvis_draft_cards`` is set in the site config."""
	import frappe

	return bool(frappe.utils.cint(frappe.conf.get("jarvis_draft_cards", 0)))


def _is_batch(args: dict, key: str) -> bool:
	"""A non-empty list under ``key``: the same test ``confirm_card._bulk`` uses."""
	value = args.get(key)
	return isinstance(value, list) and bool(value)


def is_draft_call(tool: str, args, *, must_card: bool) -> bool:
	"""A single-record ``create_doc`` / ``update_doc`` that is not ``must_card`` (a
	sensitive, structural or forced card keeps its own shape), whose doctype name is
	fit to show."""
	if must_card or tool not in _TOOLS or not isinstance(args, dict):
		return False
	if not live_draft.safe_doctype(args.get("doctype")):
		return False
	if tool == "create_doc":
		return not _is_batch(args, "docs") and isinstance(args.get("values"), dict)
	name = args.get("name")
	return (
		not _is_batch(args, "updates")
		and isinstance(name, str)
		and bool(name.strip())
		and isinstance(args.get("changes"), dict)
	)


def wanted(tool: str, args, *, must_card: bool) -> bool:
	"""Whether this park builds a draft card: the flag is on and the call is one."""
	return is_draft_call(tool, args, must_card=must_card) and enabled()


def payload_from(
	verb: str,
	doctype: str,
	name: str,
	values: dict,
	would,
	*,
	has_field,
	child_has,
	skip,
	cont: bool = False,
) -> dict:
	"""The ``draft`` payload from plain inputs.

	``has_field(key)``: whether ``key`` is a field of the form. ``child_has(key)``: for a
	table field, a predicate on its row keys, else None. ``skip``: bookkeeping keys that
	are never fields and never ignored (``_record_summary.row_bookkeeping_keys``).
	A key that is not a field is listed in ``ignored`` and left out of ``values`` (a
	child row's as "table.field"); a key the trial dropped (``would`` is a dict that
	lacks it: a permission filter) is left out of ``values`` too, as the card's rows do."""
	ignored: list[str] = []
	out: dict = {}
	for key, value in values.items():
		if key in skip:
			out[key] = value
			continue
		if not has_field(key):
			ignored.append(key)
			continue
		if isinstance(would, dict) and key not in would:
			continue
		row_has = child_has(key) if isinstance(value, list) else None
		if row_has is not None:
			value = [_clean_row(key, row, row_has, skip, ignored) for row in value]
		out[key] = value
	return {
		"verb": verb,
		"doctype": doctype,
		"name": name,
		"values": out,
		"ignored": ignored,
		"continue": bool(cont),
	}


def _clean_row(table: str, row, row_has, skip, ignored: list) -> object:
	if not isinstance(row, dict):
		return row
	kept = {}
	for key, value in row.items():
		if key in skip or row_has(key):
			kept[key] = value
			continue
		label = f"{table}.{key}"
		if label not in ignored:
			ignored.append(label)
	return kept


def _site_lookups(doctype: str):
	import frappe

	meta = frappe.get_meta(doctype)

	def has_field(key):
		return meta.get_field(key) is not None

	def child_has(key):
		df = meta.get_field(key)
		if df is None or df.fieldtype not in _TABLE_TYPES or not df.options:
			return None
		child = frappe.get_meta(df.options)
		return lambda k: child.get_field(k) is not None

	return has_field, child_has


def payload(tool: str, args: dict, would) -> dict:
	"""The ``draft`` payload of a draft call, read as the write will STORE it
	(``confirm_card._stored_args``) against the site's field list."""
	from jarvis.chat import _record_summary, confirm_card

	stored = confirm_card._stored_args(tool, args)
	doctype = live_draft.real_doctype(args.get("doctype")) or live_draft.safe_doctype(args.get("doctype"))
	has_field, child_has = _site_lookups(doctype)
	skip = _record_summary.row_bookkeeping_keys()
	cont = args.get("continue") is True
	if tool == "update_doc":
		return payload_from(
			"update",
			doctype,
			args["name"],
			stored["changes"],
			would,
			has_field=has_field,
			child_has=child_has,
			skip=skip,
			cont=cont,
		)
	import frappe

	prompt = frappe.get_meta(doctype).autoname == "Prompt"
	name = stored["values"].get("name") if prompt else ""
	return payload_from(
		"create",
		doctype,
		name if isinstance(name, str) else "",
		stored["values"],
		would,
		has_field=has_field,
		child_has=child_has,
		skip=skip,
		cont=cont,
	)


def title_of(verb: str, doctype: str, name: str) -> str:
	""" "New {doctype}" for a create, "{doctype} {name}" for an update."""
	return f"New {doctype}" if verb == "create" else f"{doctype} {name}"


def attach(card: dict, tool: str, args: dict, would) -> bool:
	"""Put ``draft`` and ``title`` on ``card`` (before it is sealed). A create card
	carries no trial-allocated name: a prompt-named DocType keeps the name the call
	typed, any other gets none. False, and ``card`` untouched, when the doctype is not
	a real one on this site."""
	import frappe

	doctype = live_draft.real_doctype(args.get("doctype"))
	if not doctype:
		return False
	try:
		draft = payload(tool, args, would)
	except Exception:
		frappe.log_error(title="draft payload failed", message=frappe.get_traceback())
		return False
	card["draft"] = draft
	card["title"] = title_of(draft["verb"], doctype, draft["name"])
	if draft["verb"] == "create":
		card["name"] = draft["name"] or None
	return True
