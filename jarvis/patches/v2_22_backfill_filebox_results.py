"""Backfill ``filebox_result_*`` on existing File Box conversations (PR-1 §5).

The File Box list reads "Draft created" from these stamps, which only new runs
write. Stamp older conversations from their completed single ``create_doc`` tool
rows of SUBMITTABLE doctypes: the first draft is the result, the rest count.
Idempotent (unstamped conversations only) and batched.
"""

import frappe

_BATCH = 500


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
			"""SELECT conversation, ref_doctype, ref_name FROM `tabJarvis Chat Message`
			WHERE conversation IN %(convs)s AND role = 'tool' AND tool_name = 'create_doc'
			  AND tool_status = 'completed' AND COALESCE(ref_name, '') != ''
			ORDER BY conversation, seq""",
			{"convs": convs},
			as_dict=True,
		)
		drafts: dict[str, list] = {}
		for r in rows:
			if r.ref_doctype and is_submittable(r.ref_doctype):
				drafts.setdefault(r.conversation, []).append(r)
		for conv, found in drafts.items():
			frappe.db.set_value(
				"Jarvis Conversation",
				conv,
				{
					"filebox_result_doctype": found[0].ref_doctype,
					"filebox_result_name": found[0].ref_name,
					"filebox_result_count": len(found),
				},
				update_modified=False,
			)
		frappe.db.commit()
