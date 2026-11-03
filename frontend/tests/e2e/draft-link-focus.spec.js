import { test, expect } from "@playwright/test";

async function openDraft(page) {
	await page
		.context()
		.addCookies([
			{ name: "user_id", value: "review%40example.test", url: "http://localhost:8080" },
		]);
	await page.addInitScript(() =>
		Object.assign(window, {
			csrf_token: "test",
			agent_name: "Jarvis",
			time_zone: "Asia/Kolkata",
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
		doctype: "Sales Order",
		fields: [{ label: "customer", value: "Synthetic Customer" }],
		tables: [{ fieldname: "items", rows: [{ item_code: "TEST-ITEM" }] }],
	};
	await page.route("**/api/method/**", async (route) => {
		const method = new URL(route.request().url()).pathname.split("/api/method/")[1];
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) message = { time_zone: "Asia/Kolkata" };
		else if (method.endsWith("list_conversations"))
			message = [{ name: "focus", title: "Focus regression" }];
		else if (method.endsWith("get_conversation"))
			message = {
				conversation: { name: "focus", title: "Focus regression", status: "Active" },
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
				doctype: "Sales Order",
				fields: [
					{
						fieldname: "customer",
						label: "Customer",
						fieldtype: "Link",
						options: "Customer",
					},
					{
						fieldname: "items",
						label: "Items",
						fieldtype: "Table",
						options: "Sales Order Item",
					},
				],
				tables: {
					items: {
						child_doctype: "Sales Order Item",
						label: "Items",
						columns: [
							{
								fieldname: "item_code",
								label: "Item",
								fieldtype: "Link",
								options: "Item",
							},
						],
					},
				},
			};
		else if (method.endsWith("search_link"))
			message = [{ value: "MATCH", description: "Synthetic choice" }];
		else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	await page.goto("/?nosocket");
	await page.getByRole("button", { name: "Edit", exact: true }).click();
	return {
		customer: page.locator(".jv-draft-fields input").first(),
		item: page.locator(".jv-grid tbody input").first(),
		choices: page.locator(".jv-action-linkmenu button"),
	};
}

test("a previous field's blur cannot close the current Link search", async ({ page }) => {
	const { customer, item, choices } = await openDraft(page);
	await customer.fill("CUSTOMER");
	await expect(choices).toHaveCount(1);
	await item.fill("ITEM");
	await expect(choices).toHaveCount(1);
	// Cross the existing 160ms delayed-close deadline, not a rendering wait.
	await page.waitForTimeout(250);
	await expect(item).toBeFocused();
	await expect(choices).toHaveCount(1);
});

test("refocusing the same field invalidates its pending blur", async ({ page }) => {
	const { customer, choices } = await openDraft(page);
	await customer.fill("CUSTOMER");
	await expect(choices).toHaveCount(1);
	await customer.evaluate((el) => {
		el.blur();
		el.focus();
	});
	await page.waitForTimeout(250);
	await expect(customer).toBeFocused();
	await expect(choices).toHaveCount(1);
});

test("leaving a Link field still closes its choices", async ({ page }) => {
	const { customer, choices } = await openDraft(page);
	await customer.fill("CUSTOMER");
	await expect(choices).toHaveCount(1);
	await customer.evaluate((el) => el.blur());
	await expect(choices).toHaveCount(0);
});

test("a delayed response cannot reopen a blurred field", async ({ page }) => {
	const { customer, choices } = await openDraft(page);
	let release;
	const held = new Promise((resolve) => {
		release = resolve;
	});
	await page.route("**/api/method/**search_link*", async (route) => {
		await held;
		await route.fulfill({ json: { message: [{ value: "LATE" }] } });
	});
	await customer.fill("LATE");
	await customer.evaluate((el) => el.blur());
	await page.waitForTimeout(250);
	const response = page.waitForResponse((r) => r.url().includes("search_link"));
	release();
	await response;
	await page.waitForTimeout(100);
	await expect(choices).toHaveCount(0);
});

test("typing again in the same field rejects an older response", async ({ page }) => {
	const { customer, choices } = await openDraft(page);
	let release, received;
	const held = new Promise((resolve) => {
		release = resolve;
	});
	const requested = new Promise((resolve) => {
		received = resolve;
	});
	await page.route("**/api/method/**search_link*", async (route) => {
		const first = (route.request().postData() || "").includes("FIRST");
		if (first) {
			received();
			await held;
		}
		await route.fulfill({ json: { message: [{ value: first ? "FIRST" : "SECOND" }] } });
	});
	await customer.fill("FIRST");
	await requested;
	await customer.fill("SECOND");
	await expect(choices).toHaveText(["SECOND"]);
	const response = page.waitForResponse((r) => (r.request().postData() || "").includes("FIRST"));
	release();
	await response;
	await page.waitForTimeout(100);
	await expect(choices).toHaveText(["SECOND"]);
});
