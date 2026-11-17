// Filter UI for the "Jarvis User Activity" Script Report. The default opens on the
// last 30 days so a normal open is bounded; execute() in jarvis_user_activity.py
// reads these fieldnames. "Summary" ignores Activity and Outcome on purpose: a
// summary that hid rows by outcome would hide the very flags it exists to raise.
//
// Select options are {value, label} pairs: the value is what the server filters on (it
// never changes), the label is a plain, translatable phrase instead of an internal key
// such as "foreign_session". Each label is a literal __("...") at its own call site, so
// translation extraction finds it (it cannot see through __(variable)).
const option = (value, label) => ({ value, label });

frappe.query_reports["Jarvis User Activity"] = {
	// The in-app twin of this report (Settings > Administration > User activity): the same
	// two views with a person drill-down. The SPA route is /jarvis/admin/activity.
	onload(report) {
		report.page.add_inner_button(__("Open in Jarvis"), () => {
			window.open("/jarvis/admin/activity", "_blank");
		});
	},
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
			fieldname: "user",
			label: __("Person"),
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "activity",
			label: __("Activity"),
			fieldtype: "Select",
			options: [
				option("", ""),
				option("read", __("Read")),
				option("export", __("Export")),
				option("action", __("App action")),
				option("denied_call", __("Refused call")),
				option("auth_rejected", __("Rejected sign-in")),
				option("foreign_session", __("Unknown session")),
				option("create", __("Create")),
				option("update", __("Update")),
				option("submit", __("Submit")),
				option("cancel", __("Cancel")),
				option("amend", __("Amend")),
				option("delete", __("Delete")),
				option("run", __("Run method")),
				option("import", __("Import")),
				option("workflow", __("Workflow action")),
				option("email", __("Email")),
				option("comment", __("Comment")),
				option("share", __("Share")),
				option("unshare", __("Unshare")),
				option("assign", __("Assign")),
				option("unassign", __("Unassign")),
				option("tag", __("Tag")),
				option("untag", __("Untag")),
				option("attach", __("Attach")),
				option("write", __("Other change")),
			],
		},
		{
			fieldname: "outcome",
			label: __("Outcome"),
			fieldtype: "Select",
			options: [
				option("", ""),
				option("ok", __("OK")),
				option("denied", __("Denied")),
				option("failed", __("Failed")),
				option("applied", __("Applied")),
				option("refused", __("Refused")),
				option("discarded", __("Discarded")),
				option("partial", __("Partial")),
			],
		},
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: [option("Events", __("Events")), option("Summary", __("Summary"))],
			default: "Events",
		},
	],
};
