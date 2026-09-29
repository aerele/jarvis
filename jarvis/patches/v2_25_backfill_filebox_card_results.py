"""Stamp File Box rows whose draft came from a confirmed chat card (issue #618).

A draft confirmed from a File Box chat's ``jarvis-action`` card was never stamped, so
the row read "No draft" beside "Created Sales Invoice ...". Its receipt keeps the
record in ``tool_result`` (``ref_*`` stay empty), which ``v2_22`` doesn't read; this
reads both. Idempotent (unstamped conversations only) and batched.
"""

import json

import frappe

_BATCH = 500


def _ref(row) -> tuple[str | None, str | None]:
	if row.ref_doctype and row.ref_name:
		return row.ref_doctype, row.ref_name
	try:
		data = (json.loads(row.tool_result or "{}") or {}).get("data") or {}
	except (ValueError, AttributeError):
		return None, None
	return (data.get("doctype"), data.get("name")) if isinstance(data, dict) else (None, None)


def execute():
	if not frappe.db.has_column("Jarvis Conversation", "filebox_result_name"):
		return
	submittable: dict[str, bool] = {}

	def is_submittable(doctype: str) -> bool:
		if doctype not in submittable:
			submittable[doctype] = bool(frappe.db.get_value("DocType", doctype, "is_submittable"))
		return submittable[doctype]

	after = ""
	while True:
		convs = frappe.db.sql_list(
			"""SELECT name FROM `tabJarvis Conversation`
			WHERE file_box = 1 AND COALESCE(filebox_result_name, '') = '' AND name > %(after)s
			ORDER BY name LIMIT %(n)s""",
			{"after": after, "n": _BATCH},
		)
		if not convs:
			break
		after = convs[-1]
		rows = frappe.db.sql(
			"""SELECT conversation, ref_doctype, ref_name, tool_result FROM `tabJarvis Chat Message`
			WHERE conversation IN %(convs)s AND role = 'tool' AND tool_name = 'create_doc'
			  AND tool_status = 'completed'
			ORDER BY conversation, seq""",
			{"convs": convs},
			as_dict=True,
		)
		drafts: dict[str, list] = {}
		for r in rows:
			doctype, name = _ref(r)
			if doctype and name and is_submittable(doctype) and frappe.db.exists(doctype, name):
				drafts.setdefault(r.conversation, []).append((doctype, name))
		for conv, found in drafts.items():
			frappe.db.set_value(
				"Jarvis Conversation",
				conv,
				{
					"filebox_result_doctype": found[0][0],
					"filebox_result_name": found[0][1],
					"filebox_result_count": len(found),
				},
				update_modified=False,
			)
		frappe.db.commit()
