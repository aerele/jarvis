// A real browser regression for the approval-board split. The sheet data is
// mocked so the test runs without a bench, but it mounts the production route,
// CSS, controls and SheetDetail component.
import { test, expect } from "@playwright/test";

const sheet = {
	name: "PA-RESPONSIVE-1",
	kind: "file_box_sheet",
	status: "Pending",
	summary: "Approval sheet: buying-settings.pdf",
	file_name: "buying-settings.pdf",
	conversation: "",
	for_user: "",
	collecting: 0,
	can_act: 1,
	record_count: 2,
	question_count: 1,
	counts_line: "1 item · 1 setting · 1 question",
	card_sha256: "responsive-test-sha",
	records: [
		{
			index: 0,
			doctype: "Buying Settings",
			op: "update",
			name: "Buying Settings",
			title: "Buying Settings",
			category: "settings",
			depends_on: [],
			values: { maintain_same_rate: 0 },
			secret: [],
			locked: {},
			needs_input: [],
			needs_fix: null,
		},
		{
			index: 1,
			doctype: "Item",
			op: "create",
			name: "",
			title: "Item",
			category: "item",
			depends_on: [],
			values: { item_code: "SKU-001", item_name: "Item" },
			secret: [],
			locked: {},
			needs_input: [],
			needs_fix: null,
		},
	],
	questions: [
		{
			name: "Q-1",
			title: "Confirm",
			question: "Apply the listed records?",
			options: ["Proceed"],
			context_md: "",
			routing: "",
			status: "Pending",
		},
	],
	errors: {},
	decisions: {},
	progress: null,
	outcome: {},
	reason_code: "",
	reason: "",
};

function methodOf(route) {
	const url = new URL(route.request().url());
	return decodeURIComponent(url.pathname.split("/api/method/")[1] || "");
}

function laneRow() {
	return {
		name: sheet.name,
		kind: sheet.kind,
		status: sheet.status,
		file_name: sheet.file_name,
		counts_line: sheet.counts_line,
		counts: { item: 1, settings: 1 },
		collecting: 0,
		created_at: "2026-11-01 12:00:00",
	};
}

async function bootSheet(page, viewport) {
	await page.addInitScript(() => {
		Object.assign(window, {
			csrf_token: "responsive-test-token",
			time_zone: "Asia/Kolkata",
			agent_name: "Jarvis",
			is_system_manager: false,
			is_jarvis_admin: false,
			support_available: false,
			has_support_access: false,
			release_notice: "",
		});
		localStorage.setItem("jarvis-theme", "light");
	});
	await page.route("**/api/method/**", async (route) => {
		const method = methodOf(route);
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) message = { time_zone: "Asia/Kolkata" };
		else if (method.endsWith("list_pending_actions_lane")) message = { rows: [laneRow()] };
		else if (method.endsWith("list_approvals_page"))
			message = {
				rows: [],
				total: 0,
				has_more: false,
				start: 0,
				page_length: 20,
				facets: {},
				awaiting_reply: [],
				open_drafts: [],
				ready_reports: [],
			};
		else if (method.endsWith("list_wiki_write_proposals")) message = { rows: [], total: 0 };
		else if (method.endsWith("pending_badge")) message = { count: 1, next_in: null };
		else if (method.endsWith("get_pending_action")) message = sheet;
		else if (method.includes("count") || method.includes("list")) {
			message = [];
		}
		await route.fulfill({
			status: 200,
			contentType: "application/json",
			body: JSON.stringify({ message }),
		});
	});

	await page
		.context()
		.addCookies([
			{ name: "user_id", value: "review%40example.test", url: "http://localhost:8080" },
		]);
	await page.setViewportSize(viewport);
	await page.goto(`/jarvis/approvals?held=${sheet.name}&nosocket`, {
		waitUntil: "domcontentloaded",
	});
	await expect(page.locator(`#sheet-${sheet.name}-r0`)).toBeVisible({ timeout: 20000 });
}

async function expectInViewport(locator, width) {
	await locator.scrollIntoViewIfNeeded();
	await expect(locator).toBeVisible();
	const box = await locator.boundingBox();
	expect(box).not.toBeNull();
	expect(box.width).toBeGreaterThan(0);
	expect(box.x).toBeGreaterThanOrEqual(-1);
	expect(box.x + box.width).toBeLessThanOrEqual(width + 1);
}

test("approval sheet controls remain reachable at 390px", async ({ page }) => {
	await bootSheet(page, { width: 390, height: 844 });

	const rail = page.getByTestId("approvals-rail");
	const settings = page.locator(`#sheet-${sheet.name}-r0`);
	const item = page.locator(`#sheet-${sheet.name}-r1`);
	const apply = page.getByRole("button", { name: "Apply & continue", exact: true });
	const proceed = page.getByRole("button", { name: "Proceed", exact: true });

	await expectInViewport(settings, 390);
	await expectInViewport(settings.getByRole("button", { name: "Apply", exact: true }), 390);
	await expectInViewport(item, 390);
	await expectInViewport(item.getByRole("button", { name: "Create", exact: true }), 390);
	expect((await rail.boundingBox()).width).toBeCloseTo(390, 0);
	expect(await page.getByText(sheet.counts_line, { exact: true }).count()).toBe(2);
	expect(
		await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
	).toBeLessThanOrEqual(0);

	await proceed.scrollIntoViewIfNeeded();
	await proceed.focus();
	await page.keyboard.press("Space");
	await expect(apply).toBeEnabled();
});

test("approval board keeps its 360px desktop rail", async ({ page }) => {
	await bootSheet(page, { width: 1280, height: 900 });
	const rail = page.getByTestId("approvals-rail");
	const box = await rail.boundingBox();
	expect(box).not.toBeNull();
	expect(box.width).toBeCloseTo(360, 0);
	expect(
		await page.locator('[data-testid="approvals-split-view"]').evaluate((el) => {
			return getComputedStyle(el).flexDirection;
		})
	).toBe("row");
	expect(
		await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
	).toBeLessThanOrEqual(0);
});
