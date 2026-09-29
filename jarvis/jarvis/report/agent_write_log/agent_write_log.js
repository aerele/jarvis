// Filter UI for the "Agent Write Log" Script Report. The default opens on the
// last 30 days so a normal open is bounded (the whole-table pull only happens
// when an admin deliberately widens the range for a full compliance export);
// execute() in agent_write_log.py reads these fieldnames.
frappe.query_reports["Agent Write Log"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "actor",
			label: __("Person"),
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "outcome",
			label: __("Outcome"),
			fieldtype: "Select",
			options: ["", "applied", "failed", "discarded"].join("\n"),
		},
	],
};
