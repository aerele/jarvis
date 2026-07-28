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

		// ---- Self-Hosted openclaw -----------------------------------------
		// Self-host connect funnels into save_self_hosted / test_connection, which
		// stay System-Manager-ONLY (owner trust-boundary decision #3). A
		// Jarvis-Admin-not-SM now reaches this form (TASK 46 grants the admin tier
		// a Jarvis Settings perm row) but must NOT see a connect button that
		// dead-ends in a 403 — gate the entry point on System Manager so self-host
		// config is SM-only end-to-end. ("Switch to Managed" below stays visible:
		// switch_to_managed is require_jarvis_admin per TASK 45.)
		if (frappe.user.has_role("System Manager")) {
			frm.add_custom_button(
				__("Configure Self-Hosted openclaw"),
				() => {
					openSelfHostDialog(frm);
				},
				__("Deployment")
			);
		}

		if ((frm.doc.deployment_mode || "Managed") === "Self-Hosted") {
			frm.add_custom_button(
				__("Switch to Managed"),
				() => {
					frappe.confirm(
						__(
							"Switch back to Aerele-managed openclaw? This re-syncs the managed connection."
						),
						() => {
							frappe
								.call({ method: "jarvis.selfhost.switch_to_managed" })
								.then(() => {
									frappe.show_alert({
										message: __("Switched to Managed."),
										indicator: "green",
									});
									frm.reload_doc();
								});
						}
					);
				},
				__("Deployment")
			);
		}
	},
});

function renderSelfHostResults(d, result) {
	const checks = result.checks || [];
	const rows = checks
		.map(
			(c) =>
				`<li>${c.ok ? "✅" : "❌"} <b>${frappe.utils.escape_html(
					c.check
				)}</b> — ${frappe.utils.escape_html(c.detail || "")}</li>`
		)
		.join("");
	const overall = result.ok
		? `<div style="color:#1f8a3b;font-weight:600">All required checks passed.</div>`
		: `<div style="color:#b00020;font-weight:600">Some checks failed.</div>`;
	d.fields_dict.results.$wrapper.html(
		`${overall}<ul style="padding-left:18px;margin-top:6px">${
			rows || "<li>(no checks)</li>"
		}</ul>`
	);
}

function openSelfHostDialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Connect Self-Hosted openclaw"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "intro",
				options: `<p>Point Jarvis at <b>your own openclaw server</b>. You bring openclaw and your
				LLM; Jarvis connects over HTTP with a bearer token (no Aerele persona/skills).
				Validate first, then connect.</p>`,
			},
			{
				fieldtype: "Data",
				fieldname: "base_url",
				label: __("openclaw URL"),
				reqd: 1,
				default:
					(frm.doc.deployment_mode === "Self-Hosted" ? frm.doc.agent_url : "") || "",
				description: __(
					"e.g. http://host.docker.internal:19060 or https://openclaw.example.com"
				),
			},
			{ fieldtype: "Password", fieldname: "token", label: __("Gateway Token"), reqd: 1 },
			{
				fieldtype: "Check",
				fieldname: "stream",
				label: __("Stream responses token-by-token"),
				default:
					frm.doc.deployment_mode === "Self-Hosted" && frm.doc.selfhost_stream === 0
						? 0
						: 1,
				description: __(
					"Off = full reply appears at once; use if a proxy in front of your openclaw buffers SSE."
				),
			},
			{
				fieldtype: "Check",
				fieldname: "deep",
				label: __("Run deep chat test (slower — sends one message)"),
				default: 0,
			},
			{ fieldtype: "Button", fieldname: "test_btn", label: __("Test connection") },
			{ fieldtype: "HTML", fieldname: "results" },
		],
		primary_action_label: __("Connect"),
		primary_action(values) {
			d.disable_primary_action();
			frappe
				.call({
					method: "jarvis.selfhost.save_self_hosted",
					args: {
						base_url: values.base_url,
						token: values.token,
						deep: values.deep ? 1 : 0,
						stream: values.stream ? 1 : 0,
					},
				})
				.then((r) => {
					const m = r.message || {};
					if (m.ok) {
						d.hide();
						frappe.show_alert({
							message: __("Connected to self-hosted openclaw."),
							indicator: "green",
						});
						frm.reload_doc();
					} else {
						renderSelfHostResults(d, m.result || {});
						frappe.msgprint({
							title: __("Validation failed"),
							message: __("Fix the failing checks below, then retry."),
							indicator: "red",
						});
						d.enable_primary_action();
					}
				})
				.catch(() => d.enable_primary_action());
		},
	});
	d.fields_dict.test_btn.$input.on("click", () => {
		const v = d.get_values(true);
		if (!v.base_url) {
			frappe.msgprint(__("Enter the openclaw URL first."));
			return;
		}
		d.fields_dict.results.$wrapper.html(`<div class="text-muted">${__("Testing…")}</div>`);
		frappe
			.call({
				method: "jarvis.selfhost.test_connection",
				args: { base_url: v.base_url, token: v.token || "", deep: v.deep ? 1 : 0 },
			})
			.then((r) => renderSelfHostResults(d, r.message || {}))
			.catch(() =>
				d.fields_dict.results.$wrapper.html(
					`<div style="color:#b00020">Test call failed.</div>`
				)
			);
	});
	d.show();
}

