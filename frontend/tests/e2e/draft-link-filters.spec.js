import { test, expect } from "@playwright/test";

// Drive the real draft editor and inspect requests to the permission-checked
// Frappe search API. All records are synthetic; no document writes are made.
async function openDraft(
	page,
	{
		child = false,
		doctype = "Purchase Invoice",
		company = "Review Company A",
		configured = "",
		delayFirst = false,
	} = {}
) {
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
	const mr = doctype === "Material Request";
	const target = mr ? "Warehouse" : "Account";
	const name = mr ? (child ? "warehouse" : "set_warehouse") : "credit_to";
	const field = (fieldname, fieldtype, options = "") => ({
		fieldname,
		label: fieldname,
		fieldtype,
		options,
	});
	const link = {
		...field(name, "Link", target),
		link_filters: configured,
		link_query_filters: [
			[target, "company", "=", child ? "eval:parent.company" : "eval:doc.company"],
			[target, "is_group", "=", 0],
			...(!mr ? [[target, "account_type", "=", "Payable"]] : []),
		],
	};
	const action = {
		kind: "doc",
		verb: "create",
		doctype,
		fields: [
			{ label: "company", value: company },
			...(!child ? [{ label: name, value: "" }] : []),
		],
		tables: child ? [{ fieldname: "items", rows: [{ [name]: "" }] }] : [],
	};
	const requests = [];
	let releaseFirst;
	const first = new Promise((resolve) => {
		releaseFirst = resolve;
	});
	let delayed = false;
	await page.route("**/api/method/**", async (route) => {
		const request = route.request();
		const method = new URL(request.url()).pathname.split("/api/method/")[1];
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) message = { time_zone: "Asia/Kolkata" };
		else if (method.endsWith("list_conversations"))
			message = [{ name: "filters", title: "Field filters" }];
		else if (method.endsWith("get_conversation"))
			message = {
				conversation: { name: "filters", title: "Field filters", status: "Active" },
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
				doctype,
				fields: [
					field("company", "Link", "Company"),
					...(!child ? [link] : [field("items", "Table", doctype + " Item")]),
				],
				tables: child
					? {
							items: {
								child_doctype: doctype + " Item",
								label: "Items",
								columns: [link],
							},
					  }
					: {},
			};
		else if (method.endsWith("search_link")) {
			const args = JSON.parse(request.postData() || "{}");
			requests.push(args);
			if (args.doctype === target) {
				if (delayFirst && !delayed) {
					delayed = true;
					await first;
				}
				// The service returns what these constraints permit, including the same
				// misleading expense/other-company records reported in the screenshot.
				const records = [
					{
						value: "Payables A",
						company: "Review Company A",
						is_group: 0,
						account_type: "Payable",
					},
					{
						value: "Payables B",
						company: "Review Company B",
						is_group: 0,
						account_type: "Payable",
					},
					{
						value: "Cost of Goods Sold A",
						company: "Review Company A",
						is_group: 0,
						account_type: "Cost of Goods Sold",
					},
					{
						value: "Group A",
						company: "Review Company A",
						is_group: 1,
						account_type: "Payable",
					},
				];
				message = records
					.filter((row) =>
						row.value.toLowerCase().includes((args.txt || "").toLowerCase())
					)
					.filter((row) =>
						(args.filters || []).every(
							([, key, op, value]) => op === "=" && row[key] === value
						)
					)
					.map((row) => ({ value: row.value }));
			} else message = [{ value: "Review Company A" }, { value: "Review Company B" }];
		} else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	await page.goto("/?nosocket");
	await page.getByRole("button", { name: "Edit", exact: true }).click();
	const input = child
		? page.locator(".jv-grid tbody input").first()
		: page.locator(".jv-draft-fields input").nth(1);
	return {
		requests,
		input,
		company: page.locator(".jv-draft-fields input").first(),
		releaseFirst,
	};
}

test("Credit To offers only payable leaf accounts for the current Company", async ({ page }) => {
	const { input, requests } = await openDraft(page);
	await input.fill("cost");
	await expect.poll(() => requests.some((r) => r.txt === "cost")).toBe(true);
	const request = requests.find((r) => r.txt === "cost");
	expect(request.filters).toEqual(
		expect.arrayContaining([
			["Account", "company", "=", "Review Company A"],
			["Account", "is_group", "=", 0],
			["Account", "account_type", "=", "Payable"],
		])
	);
	expect(request.reference_doctype).toBe("Purchase Invoice");
	expect(request.link_fieldname).toBe("credit_to");
	const menu = page.locator(".jv-action-linkmenu");
	await expect(menu).toHaveCount(0);
	await input.fill("Pay");
	await expect(menu.locator("button")).toHaveCount(1);
	await expect(menu).toHaveText("Payables A");
	await menu.locator("button").click();
	await expect(input).toHaveValue("Payables A");
});

for (const child of [false, true]) {
	test(`${
		child ? "child" : "header"
	} Warehouse follows the current Material Request Company`, async ({ page }) => {
		const { input, requests, company } = await openDraft(page, {
			doctype: "Material Request",
			child,
		});
		await input.fill("Stores");
		await expect.poll(() => requests.some((r) => r.txt === "Stores")).toBe(true);
		expect(requests.find((r) => r.txt === "Stores").filters).toEqual([
			["Warehouse", "company", "=", "Review Company A"],
			["Warehouse", "is_group", "=", 0],
		]);
		await company.fill("Review Company B");
		await input.fill("Changed");
		await expect.poll(() => requests.some((r) => r.txt === "Changed")).toBe(true);
		const request = requests.find((r) => r.txt === "Changed");
		expect(request.filters).toContainEqual(["Warehouse", "company", "=", "Review Company B"]);
		expect(request.reference_doctype).toBe("Material Request");
		expect(request.link_fieldname).toBe(child ? "warehouse" : "set_warehouse");
	});
}

test("missing Company blocks Account search and explains the dependency", async ({ page }) => {
	const { input, requests } = await openDraft(page, { company: "" });
	await input.fill("cost");
	await expect(page.getByRole("alert")).toContainText("Set Company");
	expect(requests.some((r) => r.doctype === "Account")).toBe(false);
});

test("configured filters reach the search request", async ({ page }) => {
	const { input, requests } = await openDraft(page, {
		configured: JSON.stringify([["Account", "account_currency", "=", "INR"]]),
	});
	await input.fill("Pay");
	await expect.poll(() => requests.some((r) => r.txt === "Pay")).toBe(true);
	expect(requests.find((r) => r.txt === "Pay").filters).toContainEqual([
		"Account",
		"account_currency",
		"=",
		"INR",
	]);
});

test("unsupported configured expression blocks search instead of losing the filter", async ({
	page,
}) => {
	const { input, requests } = await openDraft(page, {
		configured: JSON.stringify([["Account", "company", "=", "eval:doc.company || 'Any'"]]),
	});
	await input.fill("Pay");
	await expect(page.getByRole("alert")).toContainText("need ERPNext");
	expect(requests.some((r) => r.doctype === "Account")).toBe(false);
});

test("a response for the previous Company cannot restore stale choices", async ({ page }) => {
	const { input, company, requests, releaseFirst } = await openDraft(page, { delayFirst: true });
	await input.focus();
	await expect.poll(() => requests.some((r) => r.doctype === "Account")).toBe(true);
	// Change a dependency while the Link retains focus, as a form default can.
	// Cross-field blur ownership is separately covered by #1550's regressions.
	await company.evaluate((el) => {
		el.value = "Review Company B";
		el.dispatchEvent(new Event("input", { bubbles: true }));
	});
	await input.fill("Pay");
	await expect(page.locator(".jv-action-linkmenu")).toHaveText("Payables B");
	releaseFirst();
	await page.waitForTimeout(250);
	await expect(page.locator(".jv-action-linkmenu")).toHaveText("Payables B");
});
