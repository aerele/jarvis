"""Resolve the Link-field values of a doc you are about to create.

Before a create, the agent calls this with the intended values (header fields
AND child-table rows). For every Link field carrying a value it reports whether
that value already exists (``exact``), has near-matches worth reusing
(``candidates``), is genuinely absent (``missing``), or cannot be checked
because the user lacks read on the target (``unchecked``). It only searches; the
agent decides whether a candidate is really the same entity and creates the
``missing`` ones (batched via create_docs). See jarvis-persona AGENTS.md.
"""

import frappe

from jarvis.exceptions import InvalidArgumentError

_MAX_LIMIT = 20


def resolve_links(doctype: str, values: dict, limit: int = 5) -> dict:
	"""Report the resolution status of every Link value in ``values``.

	``values`` mirrors what will be passed to create_doc, including child-table
	lists (``{"customer": "Acme", "items": [{"item_code": "Widget"}]}``). Decides
	nothing — the agent reuses/creates from the statuses.
	"""
	if not doctype:
		raise InvalidArgumentError("doctype is required")
	if not frappe.db.exists("DocType", doctype):
		raise InvalidArgumentError(f"unknown doctype: {doctype}")
	if not isinstance(values, dict) or not values:
		raise InvalidArgumentError("values must be a non-empty dict")
	if limit <= 0 or limit > _MAX_LIMIT:
		raise InvalidArgumentError(f"limit must be between 1 and {_MAX_LIMIT}")

	meta = frappe.get_meta(doctype)
	links: list = []

	for df in meta.fields:
		if df.fieldtype != "Link" or not df.options:
			continue
		for v in _iter_values(values.get(df.fieldname)):
			links.append(_resolve_one(df.fieldname, df.options, v, limit))

	for df in meta.fields:
		if df.fieldtype != "Table" or not df.options:
			continue
		rows = values.get(df.fieldname)
		if not isinstance(rows, list):
			continue
		child_meta = frappe.get_meta(df.options)
		child_links = [c for c in child_meta.fields if c.fieldtype == "Link" and c.options]
		for idx, row in enumerate(rows):
			if not isinstance(row, dict):
				continue
			for c in child_links:
				for v in _iter_values(row.get(c.fieldname)):
					rec = _resolve_one(c.fieldname, c.options, v, limit)
					rec["table"] = df.fieldname
					rec["row"] = idx
					links.append(rec)

	return {"doctype": doctype, "links": links, "note": _summary_note(links)}


def _iter_values(val):
	"""Yield each non-empty scalar link value (a link field may hold one value,
	or a list for table-multiselect)."""
	if val in (None, ""):
		return
	if isinstance(val, (list, tuple, set)):
		for v in val:
			if isinstance(v, str) and v.strip():
				yield v
		return
	if isinstance(val, str) and val.strip():
		yield val


def _resolve_one(field: str, target_dt: str, value: str, limit: int) -> dict:
	rec = {
		"field": field,
		"target_doctype": target_dt,
		"value": value,
		"status": "missing",
		"candidates": [],
	}
	# Don't turn this into an existence oracle over doctypes the user can't read.
	if not frappe.has_permission(target_dt, ptype="read"):
		rec["status"] = "unchecked"
		return rec
	try:
		# frappe.db.exists is permission-unaware; it would confirm existence of
		# records the user cannot actually read (if_owner / user-permission
		# restricted doctypes). Use get_list, like _candidates, so an existing
		# but unreadable record falls through to candidates/missing instead of
		# leaking as "exact".
		if frappe.get_list(target_dt, filters={"name": value}, limit=1):
			rec["status"] = "exact"
			return rec
	except Exception:
		rec["status"] = "unchecked"
		return rec
	rec["candidates"] = _candidates(target_dt, value, limit)
	if rec["candidates"]:
		rec["status"] = "candidates"
	return rec


def _candidates(target_dt: str, value: str, limit: int) -> list:
	"""Permission-safe near-match search on name + title_field. get_list (never
	get_all) so row-level perms + permlevel are respected."""
	meta = frappe.get_meta(target_dt)
	title_field = meta.get("title_field")
	has_title = bool(title_field) and title_field != "name"
	fields = ["name"] + ([title_field] if has_title else [])
	or_filters = [["name", "like", f"%{value}%"]]
	if has_title:
		or_filters.append([title_field, "like", f"%{value}%"])
	try:
		rows = frappe.get_list(
			target_dt,
			or_filters=or_filters,
			fields=fields,
			limit=limit,
			order_by="modified desc",
		)
	except Exception:
		return []
	out = []
	for r in rows:
		entry = {"name": r.get("name")}
		if has_title and r.get(title_field):
			entry["title"] = r.get(title_field)
		out.append(entry)
	return out


def _summary_note(links: list) -> str:
	missing = [lk for lk in links if lk["status"] == "missing"]
	cand = [lk for lk in links if lk["status"] == "candidates"]
	parts = []
	if missing:
		parts.append(f"{len(missing)} missing (create these)")
	if cand:
		parts.append(f"{len(cand)} with near-matches (reuse or create)")
	if not parts:
		return "all links resolved to existing records"
	return "; ".join(parts)
