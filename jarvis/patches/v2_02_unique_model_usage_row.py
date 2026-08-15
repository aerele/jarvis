"""Add a unique index on (parent, parentfield, model, month_key) to
`tabJarvis User Model Usage`, backing the atomic upsert in
jarvis.chat.usage._atomic_insert_or_merge_model_usage.

Root cause this closes: jarvis.chat.usage._upsert_model_usage did a
SELECT-then-INSERT with no uniqueness guarantee. Two turns in DIFFERENT
conversations racing on a model's FIRST use in a month (the single-flight
guard in jarvis.chat.api is only per-conversation) could both miss the
SELECT and both INSERT, leaving two child rows for the same (user, model,
month). Those duplicates then silently corrupted two readers:
jarvis.chat.policy._over_model_limit read ONE arbitrary row via
frappe.db.get_value (a per-model cap could be exceeded without tripping),
and jarvis.chat.usage_push._build_rollup keyed a dict by model - last row
wins - under-reporting the pushed total. Both readers are now
duplicate-tolerant (they SUM matching rows) as defense in depth, but the
real fix is preventing the duplicate at write time via this index.

Existing duplicates (if any survived from before this fix landed) are
merged - tokens summed, the highest configured cap kept - before the
unique constraint is added, since ALTER TABLE would otherwise fail on
live dupes.
"""

import frappe

MODEL_USAGE = "Jarvis User Model Usage"
USER_SETTINGS = "Jarvis User Settings"


def _dedupe_existing_rows() -> None:
	dupes = frappe.db.sql(
		"""
		SELECT parent, parentfield, model, month_key, COUNT(*) c
		FROM `tabJarvis User Model Usage`
		WHERE parenttype = %(ptype)s
		GROUP BY parent, parentfield, model, month_key
		HAVING c > 1
		""",
		{"ptype": USER_SETTINGS},
		as_dict=True,
	)
	for d in dupes:
		rows = frappe.get_all(
			MODEL_USAGE,
			filters={
				"parent": d.parent,
				"parentfield": d.parentfield,
				"parenttype": USER_SETTINGS,
				"model": d.model,
				"month_key": d.month_key,
			},
			fields=["name", "month_input_tokens", "month_output_tokens", "monthly_token_limit"],
			order_by="idx asc",
		)
		if len(rows) < 2:
			continue
		keep, extras = rows[0], rows[1:]
		total_in = sum(int(r.month_input_tokens or 0) for r in rows)
		total_out = sum(int(r.month_output_tokens or 0) for r in rows)
		cap = max(int(r.monthly_token_limit or 0) for r in rows)
		frappe.db.set_value(
			MODEL_USAGE,
			keep.name,
			{
				"month_input_tokens": total_in,
				"month_output_tokens": total_out,
				"monthly_token_limit": cap,
			},
			update_modified=False,
		)
		for extra in extras:
			frappe.db.delete(MODEL_USAGE, {"name": extra.name})


def execute():
	if not frappe.db.table_exists(MODEL_USAGE):
		return
	_dedupe_existing_rows()
	frappe.db.commit()
	frappe.db.add_unique(
		MODEL_USAGE,
		["parent", "parentfield", "model", "month_key"],
		constraint_name="parent_field_model_month",
	)
	frappe.db.commit()
