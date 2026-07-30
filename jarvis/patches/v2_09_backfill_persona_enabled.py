import frappe


def execute():
	"""Turn the persona picker ON for benches whose Jarvis Settings Single predates
	the persona_enabled field. A new Check added to an existing Single is not
	auto-defaulted, so the column reads 0; fresh installs get 1 from the field
	default and never run patches, so both paths converge on ON. An admin can still
	set it to 0 afterward to disable the persona feature fleet-wide (N7)."""
	# Jarvis Settings is a Single (data in tabSingles, no tab<Doctype> table), so
	# gate on the meta field, not a column check.
	if not frappe.get_meta("Jarvis Settings").has_field("persona_enabled"):
		return
	frappe.db.set_single_value("Jarvis Settings", "persona_enabled", 1)
