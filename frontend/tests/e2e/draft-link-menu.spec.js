import { test, expect } from "@playwright/test";

// Exercise the actual ChatView grid, not a popup mounted outside its original
// scroll container. All records are synthetic; no document writes are made.
async function openDraft(page) {
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
		doctype: "Sales Order",
		fields: [{ label: "customer", value: "Synthetic Customer" }],
		tables: [{ fieldname: "items", rows: [{ item_code: "TEST-ITEM", qty: 20, rate: 100 }] }],
	};
	const column = (fieldname, label, fieldtype, options = "", read_only = 0) => ({
		fieldname,
		label,
		fieldtype,
		options,
		read_only,
	});
	await page.route("**/api/method/**", async (route) => {
		const request = route.request();
		const method = new URL(request.url()).pathname.split("/api/method/")[1];
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) message = { time_zone: "Asia/Kolkata" };
		else if (method.endsWith("list_conversations"))
			message = [{ name: "issue-651", title: "Synthetic Sales Order" }];
		else if (method.endsWith("get_conversation"))
			message = {
				conversation: {
					name: "issue-651",
					title: "Synthetic Sales Order",
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
				doctype: "Sales Order",
				fields: [
					column("customer", "Customer", "Link", "Customer"),
					column("items", "Items", "Table", "Sales Order Item"),
				],
				tables: {
					items: {
						child_doctype: "Sales Order Item",
						label: "Items",
						columns: [
							column("item_code", "Item", "Link", "Item"),
							column("delivery_date", "Delivery Date", "Date"),
							column("qty", "Quantity", "Float"),
							column("rate", "Rate", "Currency"),
							column("amount", "Amount", "Currency", "", 1),
							column("warehouse", "Warehouse", "Link", "Warehouse"),
						],
					},
				},
			};
		else if (method.endsWith("search_link")) {
			const text = request.postData() || request.url();
			message = text.includes("NO-MATCH")
				? []
				: Array.from({ length: 8 }, (_, i) => ({
						value: `TEST-ITEM-${i + 1}`,
						description: `Synthetic selectable inventory item ${i + 1}`,
				  }));
		} else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	await page.goto("/?nosocket");
	await page.getByRole("button", { name: "Edit", exact: true }).click();
	return page.locator(".jv-grid tbody tr").first().locator("input").first();
}

for (const viewport of [
	{ width: 1280, height: 900 },
	{ width: 390, height: 844 },
	{ width: 640, height: 360 },
]) {
	test(`Item choices escape table clipping at ${viewport.width}x${viewport.height}`, async ({
		page,
	}, testInfo) => {
		await page.setViewportSize(viewport);
		const input = await openDraft(page);
		await input.fill("TEST");
		const menu = page.locator(".jv-action-linkmenu");
		await expect(menu.locator("button")).toHaveCount(8);
		const bounds = await menu.boundingBox();
		expect(bounds.width).toBeGreaterThanOrEqual(300);
		expect(bounds.x).toBeGreaterThanOrEqual(8);
		expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width - 8);
		expect(bounds.y).toBeGreaterThanOrEqual(8);
		expect(bounds.y + bounds.height).toBeLessThanOrEqual(viewport.height - 8);
		await testInfo.attach("Item choices", {
			body: await page.screenshot(),
			contentType: "image/png",
		});
		// Real pointer hit testing: toBeVisible alone would miss clipping.
		await menu.locator("button").nth(2).click();
		await expect(input).toHaveValue("TEST-ITEM-3");
		await expect(menu).toHaveCount(0);
		await input.fill("NO-MATCH");
		await expect(menu).toHaveCount(0);
	});
}

test("header links still select outside the panel clipping container", async ({ page }) => {
	await openDraft(page);
	const menu = page.locator(".jv-action-linkmenu");
	const customer = page.locator(".jv-draft-fields input").first();
	await customer.fill("TEST");
	await menu.locator("button").first().click();
	await expect(customer).toHaveValue("TEST-ITEM-1");
});

test("scrolling and closing cannot leave an orphaned menu", async ({ page }) => {
	await page.setViewportSize({ width: 390, height: 844 });
	const input = await openDraft(page);
	const menu = page.locator(".jv-action-linkmenu");
	await input.fill("TEST");
	await expect(menu.locator("button")).toHaveCount(8);
	await menu.evaluate((el) => {
		el.scrollTop = el.scrollHeight;
	});
	await menu.locator("button").last().click();
	await expect(input).toHaveValue("TEST-ITEM-8");
	await input.fill("TEST");
	await expect(menu).toBeVisible();
	await page.locator(".jv-draft-gridwrap").evaluate((el) => {
		el.scrollLeft = el.scrollWidth;
	});
	await expect(menu).not.toBeVisible();
	await input.scrollIntoViewIfNeeded();
	await input.fill("TEST-I");
	await expect(menu).toBeVisible();
	await page.getByRole("button", { name: "Close", exact: true }).click();
	await expect(menu).toHaveCount(0);
});
