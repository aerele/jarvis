frappe.ui.form.on("Jarvis Settings", {
	refresh(frm) {
		if (frm.is_new()) return;

		// Diagnostics now return only a redacted connectivity verdict
		// ({ok, kind, connected}) — the admin_url / customer status / agent_url
		// details were removed (PART 4 REVISED TASK 34-R redaction). Show a clean
		// pass/fail verdict with no URL/status detail.
		frm.add_custom_button(
			__("Test Admin Connection"),
			() => {
				frappe
					.call({
						method: "jarvis.diagnostics.ping_admin",
					})
					.then((r) => {
						const m = r.message || {};
						if (m.ok) {
							frappe.show_alert({
								message: __("Admin reachable"),
								indicator: "green",
							});
						} else {
							frappe.msgprint({
								title: __("Admin Connection Failed ({0})", [m.kind || "error"]),
								message: m.error || "unknown",
								indicator: "red",
							});
						}
					});
			},
			__("Diagnostics")
		);

		frm.add_custom_button(
			__("Test Agent Connection"),
			() => {
				frappe
					.call({
						method: "jarvis.diagnostics.ping_openclaw",
					})
					.then((r) => {
						const m = r.message || {};
						if (m.ok) {
							frappe.show_alert({
								message: __("Agent reachable"),
								indicator: "green",
							});
						} else {
							frappe.msgprint({
								title: __("Agent Connection Failed ({0})", [m.kind || "error"]),
								message: m.error || "unknown",
								indicator: "red",
							});
						}
					});
			},
			__("Diagnostics")
		);

		frm.add_custom_button(
			__("Reset Agent Pairing"),
			() => {
				frappe.confirm(
					__(
						"Clear the cached agent pairing and re-pair from scratch? Use this if chat fails with a device/token mismatch error."
					),
					() => {
						frappe
							.call({
								method: "jarvis.diagnostics.reset_agent_pairing",
								freeze: true,
								freeze_message: __(
									"Clearing pairing and reconnecting to the agent…"
								),
							})
							.then((r) => {
								const m = r.message || {};
								if (m.ok) {
									frappe.msgprint({
										title: __("Reconnected"),
										message: m.message || __("Re-paired with the agent."),
										indicator: "green",
									});
									frm.reload_doc();
								} else {
									frappe.msgprint({
										title: __("Re-pair Failed ({0})", [m.kind || "error"]),
										message: m.error || "unknown",
										indicator: "red",
									});
								}
							});
					}
				);
			},
			__("Diagnostics")
		);

		// Reset onboarding moved to the `bench reset-onboarding` CLI command
		// (jarvis.commands); a destructive dev reset no longer belongs on the
		// HTTP form.

		frm.add_custom_button(
			__("Force Resync"),
			() => {
				const d = new frappe.ui.Dialog({
					title: __("Force Resync"),
					fields: [
						{
							fieldname: "action",
							fieldtype: "Select",
							label: "Action",
							options: "reload\nrestart",
							default: "reload",
							reqd: 1,
							description: __(
								"reload = hot-swap LLM key only. restart = re-render config and bounce the container."
							),
						},
					],
					primary_action_label: __("Resync Now"),
					primary_action(values) {
						frappe
							.call({
								method: "jarvis.diagnostics.force_resync",
								args: { action: values.action },
							})
							.then((r) => {
								const m = r.message || {};
								const ok = (m.last_sync_status || "").startsWith("ok");
								frappe.msgprint({
									title: ok ? __("Resync OK") : __("Resync Reported a Problem"),
									message: __("Action: {0}<br>At: {1}<br>Status: {2}", [
										m.action || "?",
										m.last_sync_at || "(no timestamp)",
										m.last_sync_status || "(no status)",
									]),
									indicator: ok ? "green" : "red",
								});
								frm.reload_doc();
							});
						d.hide();
					},
				});
				d.show();
			},
			__("Diagnostics")
		);
	},
});
