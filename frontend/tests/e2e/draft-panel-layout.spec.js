import { test, expect } from "@playwright/test";

// jarvis-admin-v2#776: on a resized window the record draft panels (the
// read-only preview and the editable draft) ran under the side navbar. Both
// are side-overs inside the chat column, which sits BESIDE the in-flow
// sidebar, but they sized themselves against the viewport. This opens each
// over a synthetic record whose nowrap child grid is wider than the column
// (the case that cannot flex-shrink) and checks both stay inside the column.
// Mocking mirrors draft-link-menu.spec.js; no document writes are made.
async function bootDraft(page) {
	await page
		.context()
		.addCookies([
			{ name: "user_id", value: "review%40example.test", url: "http://localhost:8080" },
		]);
	await page.addInitScript(() =>
		Object.assign(window, {
			csrf_token: "test",
			time_zone: "Asia/Kolkata",
			agent_name: "Jarvis",
			is_system_manager: false,
			is_jarvis_admin: false,
			support_available: false,
			has_support_access: false,
			release_notice: "",
		})
	);
	const action = {
		kind: "doc",
		verb: "create",
		doctype: "Payment Entry",
		fields: [
			{ label: "party", value: "Yukato Japan Industrial Development Limited" },
			{ label: "paid_from", value: "HDFC Bank Current Account - NPM" },
		],
		tables: [
			{
				fieldname: "deductions",
				rows: [
					{
						account: "Bank Charges - NPM",
						cost_center: "Main - NPM",
						amount: 7474.86,
						description: "HDFC bank charges, debit advice 014BC20232290013.",
					},
				],
			},
		],
	};
	const column = (fieldname, label, fieldtype, options = "") => ({
		fieldname,
		label,
		fieldtype,
		options,
		read_only: 0,
	});
	await page.route("**/api/method/**", async (route) => {
		const method = new URL(route.request().url()).pathname.split("/api/method/")[1];
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) message = { time_zone: "Asia/Kolkata" };
		else if (method.endsWith("list_conversations"))
			message = [{ name: "issue-776", title: "Synthetic Payment Entry" }];
		else if (method.endsWith("get_conversation"))
			message = {
				conversation: {
					name: "issue-776",
					title: "Synthetic Payment Entry",
					status: "Active",
				},
				messages: [
					{
						name: "a1",
						role: "assistant",
						seq: 1,
						content: "```jarvis-action\n" + JSON.stringify(action) + "\n```",
					},
				],
			};
		else if (method.endsWith("get_doctype_form_meta"))
			message = {
				ok: true,
				doctype: "Payment Entry",
				fields: [
					column("party", "Party", "Data"),
					column("paid_from", "Account Paid From", "Link", "Account"),
					column("deductions", "Deductions or Loss", "Table", "Payment Entry Deduction"),
				],
				tables: {
					deductions: {
						child_doctype: "Payment Entry Deduction",
						label: "Deductions or Loss",
						columns: [
							column("account", "Account", "Link", "Account"),
							column("cost_center", "Cost Center", "Link", "Cost Center"),
							column("amount", "Amount (Company Currency)", "Currency"),
							column("description", "Description", "Small Text"),
						],
					},
				},
			};
		else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	await page.goto("/?nosocket");
	await expect(page.getByRole("button", { name: "Preview", exact: true })).toBeVisible();
}

// The panel slides in on a transform transition (translateX(100%) → none);
// measure only once the transition classes are gone and no transform is left.
async function settled(page, selector) {
	await page.waitForFunction((sel) => {
		const el = document.querySelector(sel);
		return !!el && getComputedStyle(el).transform === "none";
	}, selector);
	return page.locator(selector).boundingBox();
}

// 918px: the issue's width — wider than the 820px rail auto-collapse, so the
// 220px sidebar is open and in flow and the chat column is only ~698px.
test("draft panels stay inside the chat column beside the open sidebar", async ({ page }) => {
	await page.setViewportSize({ width: 918, height: 1050 });
	await bootDraft(page);
	const column = await page.locator(".jv-root").boundingBox();
	expect(column.x).toBeGreaterThan(200); // the sidebar really is beside it

	await page.getByRole("button", { name: "Preview", exact: true }).click();
	const preview = await settled(page, ".dp-panel");
	expect(preview.x).toBeGreaterThanOrEqual(column.x);
	expect(preview.x + preview.width).toBeLessThanOrEqual(column.x + column.width + 1);
	// The field labels on the panel's left edge are what the sidebar hid.
	await expect(page.locator(".dp-fields dt").first()).toBeInViewport({ ratio: 1 });
	await page.locator(".dp-close").click();

	await page.getByRole("button", { name: "Edit", exact: true }).click();
	const draft = await settled(page, ".jv-draft-panel");
	expect(draft.x).toBeGreaterThanOrEqual(column.x);
	expect(draft.x + draft.width).toBeLessThanOrEqual(column.x + column.width + 1);
});
