"""Rename ``fields`` to ``select`` in stored query-tool dashboard sources.

The query tool now rejects spec keys outside its allowlist, and ``fields`` is
get_list's column key, not query's. A saved Jarvis Dashboard Source with
tool == "query" and a ``fields`` list (an LLM-authored get_list/query mix-up)
used to render silently wrong (the translator only reads ``select``, so it fell
back to ``["name"]``); after the allowlist it errors per tile. Renaming the key
restores the columns the author asked for.

Scope is deliberately narrow:
  * only tool == "query" rows (``fields`` is valid for get_list sources);
  * only when ``fields`` is a list and ``select`` is absent (a spec carrying both
    is ambiguous and is left for the author to fix);
  * every other key is kept as-is.
Rows whose spec is not a JSON object are skipped and logged, never rewritten.
Idempotent: a migrated row has no ``fields`` left to rename.
"""

import json

import frappe

SOURCE = "Jarvis Dashboard Source"


def execute():
	if not frappe.db.table_exists(SOURCE):
		return
	skipped = []
	for row in frappe.get_all(SOURCE, filters={"tool": "query"}, fields=["name", "spec"]):
		try:
			spec = json.loads(row.spec or "")
		except ValueError:
			skipped.append(row.name)
			continue
		if not isinstance(spec, dict):
			skipped.append(row.name)
			continue
		if "select" in spec or not isinstance(spec.get("fields"), list):
			continue
		renamed = {("select" if key == "fields" else key): value for key, value in spec.items()}
		frappe.db.set_value(SOURCE, row.name, "spec", frappe.as_json(renamed), update_modified=False)

	if skipped:
		frappe.log_error(
			title="jarvis v2_22: query dashboard sources skipped (spec is not a JSON object)",
			message=(
				"These Jarvis Dashboard Source rows could not be parsed, so fields was NOT "
				"renamed to select; the tile keeps showing its error until the dashboard is "
				"re-saved:\n  " + "\n  ".join(skipped)
			),
		)
