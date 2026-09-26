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
						method: "jarvis.diagnostics.ping_agent",
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
			__("Agent Recovery")
		)?.attr(
			"title",
			__(
				"Use when the agent rejects the connection (e.g. a device/token mismatch) and chat will not connect. Re-pairs this device; instant, no container downtime."
			)
		);

		// Rotates the plugin agent_token (X-Jarvis-Token / Boundary 6). Distinct
		// from "Reset Agent Pairing" above, which resets the chat-device pairing
		// (Boundary 5). Use this when tool calls fail plugin-auth with
		// "invalid X-Jarvis-Token" or "agent_token expired".
		frm.add_custom_button(
			__("Rotate Agent Token"),
			() => {
				frappe.confirm(
					__(
						"Rotate the agent token? A fresh token is pushed to the container, which is briefly recreated (~10–30s downtime). Chat history and the device pairing are preserved. Use this if tool calls fail with an 'invalid X-Jarvis-Token' or 'agent_token expired' error."
					),
					() => {
						frappe
							.call({
								method: "jarvis.api.rotate_agent_token",
								freeze: true,
								freeze_message: __(
									"Rotating the agent token and recreating the container…"
								),
							})
							.then((r) => {
								const m = r.message || {};
								if (m.ok) {
									frappe.msgprint({
										title: __("Agent Token Rotated"),
										message: __("Rotated at: {0}", [
											(m.data && m.data.rotated_at) || "(no timestamp)",
										]),
										indicator: "green",
									});
									frm.reload_doc();
								} else {
									const err = m.error || {};
									frappe.msgprint({
										title: __("Rotate Failed ({0})", [err.code || "error"]),
										message: err.message || "unknown",
										indicator: "red",
									});
								}
							});
					}
				);
			},
			__("Agent Recovery")
		)?.attr(
			"title",
			__(
				"Use when tool calls fail with 'invalid X-Jarvis-Token' or 'agent_token expired', or after a suspected token leak. Issues a NEW token and recreates the container (~10–30s). System Manager only."
			)
		);

		// Developer mode only (owner directive 2026-09-11): this is an operator
		// recovery tool for staging/local benches, not a customer-facing control.
		// The endpoint itself stays System Manager only regardless of this gate.
		if (frappe.boot.developer_mode && frappe.user.has_role("System Manager")) {
			// Reset onboarding: the bench-side half of a fresh start. Clears this
			// bench's admin connection + LLM credentials (and, by default, all
			// workspace content) so the setup wizard runs from step 1 again. Needed
			// when the customer was purged on admin and the bench still holds the
			// old credentials, which otherwise loops the wizard. The admin-side
			// purge is separate and stays on admin. System Manager only (the
			// endpoint enforces it); the dialog requires typing RESET so a stray
			// click cannot wipe a workspace.
			frm.add_custom_button(
				__("Reset Onboarding"),
				() => {
					const d = new frappe.ui.Dialog({
						title: __("Reset Onboarding"),
						fields: [
							{
								fieldname: "warning",
								fieldtype: "HTML",
								options: `<p>${__(
									"This disconnects the workspace from the admin plane and removes the saved AI credentials. The setup wizard starts from step 1. Billing on the admin plane is not changed."
								)}</p>`,
							},
							{
								fieldname: "wipe_data",
								fieldtype: "Check",
								label: __("Also delete all workspace content"),
								default: 0,
								description: __(
									"Chats, skills, macros, triggers, learning data, wiki and dashboards. Off by default: only the connection and AI credentials are cleared."
								),
							},
							{
								fieldname: "confirm_text",
								fieldtype: "Data",
								label: __("Type RESET to confirm"),
								reqd: 1,
							},
						],
						primary_action_label: __("Reset Now"),
						primary_action(values) {
							if ((values.confirm_text || "").trim() !== "RESET") {
								frappe.msgprint({
									title: __("Not Reset"),
									message: __("Type RESET (all capitals) to confirm."),
									indicator: "orange",
								});
								return;
							}
							d.hide();
							frappe
								.call({
									method: "jarvis.onboarding.reset_onboarding",
									args: { wipe_data: values.wipe_data ? 1 : 0 },
									freeze: true,
									freeze_message: __("Resetting the workspace…"),
								})
								.then((r) => {
									// The endpoint raises on failure (frappe.call shows the server
									// message); a resolved call is always {ok: true}.
									const data = (r.message && r.message.data) || {};
									const wiped = (data.wiped_doctypes || []).length;
									frappe.msgprint({
										title: __("Onboarding Reset"),
										message: wiped
											? __(
													"Connection cleared and {0} content types deleted. Open Jarvis to run setup again.",
													[wiped]
											  )
											: __(
													"Connection cleared; workspace content kept. Open Jarvis to run setup again."
											  ),
										indicator: "green",
										primary_action: {
											label: __("Open Jarvis"),
											action() {
												window.location.href = "/jarvis";
											},
										},
									});
									frm.reload_doc();
								})
								.catch(() => {
									// Request-level failure (network, timeout, or a server
									// exception mid-teardown): say so, and reload so the form
									// shows whatever state the reset reached.
									frappe.msgprint({
										title: __("Reset Failed"),
										message: __(
											"The reset did not complete. Reload the page and check the connection fields before trying again."
										),
										indicator: "red",
									});
									frm.reload_doc();
								});
						},
					});
					d.show();
				},
				__("Agent Recovery")
			)?.attr(
				"title",
				__(
					"Start onboarding from step 1 on this bench: clears the admin connection and AI credentials, optionally all workspace content. Use after the customer was purged on admin. System Manager only."
				)
			);
		}

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
								"reload = re-push the current config (hot-swap the key where possible). restart = also restore skills and bounce the container when the config drifted or it is unhealthy."
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
								// state (applied/applying/failed/skipped) is authoritative;
								// fall back to the status string for an older backend. A pool
								// resync converges async, so "applying" is normal, not a failure.
								const state =
									m.state ||
									((m.last_sync_status || "").startsWith("ok")
										? "applied"
										: "failed");
								const meta = {
									applied: { title: __("Resync OK"), indicator: "green" },
									applying: { title: __("Resync Queued"), indicator: "blue" },
									skipped: {
										title: __("Nothing to Resync"),
										indicator: "orange",
									},
									failed: {
										title: __("Resync Reported a Problem"),
										indicator: "red",
									},
								}[state] || {
									title: __("Resync Reported a Problem"),
									indicator: "red",
								};
								frappe.msgprint({
									title: meta.title,
									message: __("Action: {0}<br>At: {1}<br>Status: {2}", [
										m.action || "?",
										m.last_sync_at || "(no timestamp)",
										m.last_sync_status || "(no status)",
									]),
									indicator: meta.indicator,
								});
								frm.reload_doc();
							});
						d.hide();
					},
				});
				d.show();
			},
			__("Agent Recovery")
		)?.attr(
			"title",
			__(
				"Use when a Settings or LLM-key change has not taken effect, or setup is stuck on 'Applying your AI configuration'. Re-pushes the current config to the container; changes no secrets."
			)
		);
	},
});
