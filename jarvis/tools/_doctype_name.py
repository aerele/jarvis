import frappe
from frappe.utils.caching import request_cache


def canonical_doctype(doctype):
	"""``doctype`` as its DocType is named, when it names one in another case or with
	spaces around it; anything else unchanged.

	Frappe finds a DocType by a name in any case (the name column compares
	case-insensitively), but runs the hooks registered for it (row scoping by
	``permission_query_conditions`` and ``has_permission``, doc events) only under its
	exact name, so a read by another spelling would skip its row scoping. A tool reads
	and writes a doctype by this name."""
	if not isinstance(doctype, str) or not doctype.strip():
		return doctype
	try:
		name = _stored_doctype_name(doctype.strip())
	except Exception:
		return doctype  # the lookup is a courtesy: never the reason a tool fails
	if name and name.casefold() == doctype.strip().casefold():
		return name
	return doctype  # not a DocType: the tool says so as before


@request_cache
def _stored_doctype_name(doctype: str) -> str | None:
	"""The DocType's stored name, or None. A plain lookup, once per spelling per
	request: ``frappe.get_meta`` would build (and re-cache) the whole meta for every
	other spelling, and for an unknown name it leaves "not found" in the message log,
	which then shows in the failing tool's error detail."""
	return frappe.db.get_value("DocType", doctype, "name")
