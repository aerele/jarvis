frappe.ui.form.on("Jarvis Trigger Activity", {
	refresh(frm) {
		// Server decides (onload): an Intake row that is Failed or Skipped, not a duplicate,
		// not a permanent skip, with no live draft, and that the user owns or manages.
		if (!(frm.doc.__onload || {}).can_retry_intake) return;

		frm.add_custom_button(__("Retry"), () => {
			frappe
				.call({
					method: "jarvis.chat.filebox.retry_intake",
					args: { activity: frm.doc.name },
				})
				.then(() => {
					frappe.show_alert({ message: __("Queued again"), indicator: "green" });
					frm.reload_doc();
				});
		});
	},
});
