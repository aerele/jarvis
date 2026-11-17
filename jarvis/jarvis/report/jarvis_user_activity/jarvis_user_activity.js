// Filter UI for the "Jarvis User Activity" Script Report. The default opens on the
// last 30 days so a normal open is bounded; execute() in jarvis_user_activity.py
// reads these fieldnames. "Summary" ignores Activity and Outcome on purpose: a
// summary that hid rows by outcome would hide the very flags it exists to raise.
//
// Select options are {value, label} pairs: the value is what the server filters on (it
// never changes), the label is a plain, translatable phrase instead of an internal key
// such as "foreign_session".
const option = (value, label) => ({ value, label: label === "" ? "" : __(label) });

frappe.query_reports["Jarvis User Activity"] = {
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
				option("read", "Read"),
				option("export", "Export"),
				option("action", "App action"),
				option("denied_call", "Refused call"),
				option("auth_rejected", "Rejected sign-in"),
				option("foreign_session", "Unknown session"),
				option("create", "Create"),
				option("update", "Update"),
				option("submit", "Submit"),
				option("cancel", "Cancel"),
				option("amend", "Amend"),
				option("delete", "Delete"),
				option("run", "Run method"),
				option("import", "Import"),
				option("workflow", "Workflow action"),
				option("email", "Email"),
				option("comment", "Comment"),
				option("share", "Share"),
				option("unshare", "Unshare"),
				option("assign", "Assign"),
				option("unassign", "Unassign"),
				option("tag", "Tag"),
				option("untag", "Untag"),
				option("attach", "Attach"),
				option("write", "Other change"),
			],
		},
		{
			fieldname: "outcome",
			label: __("Outcome"),
			fieldtype: "Select",
			options: [
				option("", ""),
				option("ok", "OK"),
				option("denied", "Denied"),
				option("failed", "Failed"),
				option("applied", "Applied"),
				option("refused", "Refused"),
				option("discarded", "Discarded"),
				option("partial", "Partial"),
			],
		},
		{
			fieldname: "view",
			label: __("View"),
			fieldtype: "Select",
			options: [option("Events", "Events"), option("Summary", "Summary")],
			default: "Events",
		},
	],
};
