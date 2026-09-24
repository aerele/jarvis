import { test, expect } from "@playwright/test";

// Exercise the real shell, canvas and composer; no provider or live Frappe calls.
async function openBuilder(page, baseURL, split = 60) {
	await page
		.context()
		.addCookies([{ name: "user_id", value: "review%40example.test", url: baseURL }]);
	await page.addInitScript((width) => {
		Object.assign(window, {
			csrf_token: "test",
			time_zone: "Asia/Kolkata",
			agent_name: "Jarvis",
			is_system_manager: false,
			is_jarvis_admin: false,
			support_available: false,
			has_support_access: false,
		});
		// Reload must preserve a width selected by dragging, not reset the fixture.
		if (!localStorage.getItem("jarvis-dash-panel-w")) {
			localStorage.setItem("jarvis-dash-panel-w", String(width));
		}
		localStorage.setItem("jarvis-dash-conv-review@example.test", "layout-chat");
	}, split);
	await page.route("**/api/method/**", async (route) => {
		const method = new URL(route.request().url()).pathname.split("/api/method/")[1];
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) {
			message = { time_zone: "Asia/Kolkata", llm_model: "A-long-provider-model-name" };
		} else if (method.endsWith("get_dashboards_caps")) {
			message = { ok: true, data: { creatable_scopes: ["User"] } };
		} else if (method.endsWith("get_dashboard")) {
			message = {
				ok: true,
				data: {
					name: "layout-dashboard",
					source_conversation: "layout-chat",
					can_edit: true,
					scope: "User",
					dashboard_title:
						"Sales and customer invoices across every region — a very long dashboard title",
					html: "<section><h1>Sales</h1><p>Responsive test canvas</p></section>",
				},
			};
		} else if (method.endsWith("get_conversation")) {
			message = {
				conversation: { name: "layout-chat" },
				messages: [
					{ name: "long-message", role: "user", content: "longword".repeat(150) },
				],
			};
		} else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	await page.goto("/jarvis/dashboards?edit=layout-dashboard&nosocket");
	await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();
	await expect(page.getByText("longword".repeat(150), { exact: true })).toBeVisible();
}

async function inViewport(page, locator) {
	await expect(locator).toBeVisible();
	await expect
		.poll(async () => {
			const r = await locator.boundingBox();
			const v = page.viewportSize();
			return (
				!!r &&
				r.x >= -1 &&
				r.y >= -1 &&
				r.x + r.width <= v.width + 1 &&
				r.y + r.height <= v.height + 1
			);
		})
		.toBe(true);
}

async function composerFits(page) {
	for (const name of ["Send", "Hide chat", "New chat", "Dashboard chats"]) {
		await inViewport(page, page.getByRole("button", { name, exact: true }));
	}
	await inViewport(page, page.getByPlaceholder("Describe the dashboard…"));
	await expect
		.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
		.toBe(true);
}

const sizes = [
	[320, 320],
	[320, 400],
	[375, 667],
	[768, 600],
	[1024, 600],
	[1280, 800],
	[1440, 900],
	[1920, 1080],
	[844, 390],
	[1024, 320],
];
for (const split of [24, 34, 60]) {
	for (const [width, height] of sizes) {
		test(`dashboard fits ${width}x${height}, saved split ${split}%`, async ({
			page,
			baseURL,
		}) => {
			await page.setViewportSize({ width, height });
			await openBuilder(page, baseURL, split);
			await composerFits(page);
			// Growing a draft must not push Send below a short viewport.
			await page.getByPlaceholder("Describe the dashboard…").fill("Draft line\n".repeat(30));
			await composerFits(page);
			await page.getByRole("button", { name: "Hide chat", exact: true }).click();
			await inViewport(page, page.getByRole("button", { name: "Show chat", exact: true }));
			await inViewport(
				page,
				page.getByRole("button", { name: "Save dashboard", exact: true })
			);
			await inViewport(
				page,
				page.getByRole("link", { name: "View dashboard", exact: true })
			);
			await page.getByRole("button", { name: "Show chat", exact: true }).click();
			await composerFits(page);
		});
	}
}

test("drag extremes, reload and viewport changes keep both panes inside the builder", async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await openBuilder(page, baseURL);
	const handle = page.locator('[role="separator"][aria-orientation="vertical"]');
	for (const x of [0, 1279]) {
		const r = await handle.boundingBox();
		await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2);
		await page.mouse.down();
		await page.mouse.move(x, r.y + r.height / 2, { steps: 12 });
		await page.mouse.up();
		await composerFits(page);
		await inViewport(page, page.getByRole("button", { name: "Save dashboard", exact: true }));
	}
	await page.reload();
	await composerFits(page);
	for (const [width, height] of [
		[1024, 600],
		[375, 400],
		[1280, 600],
	]) {
		await page.setViewportSize({ width, height });
		await composerFits(page);
	}
	await handle.dblclick();
	await expect
		.poll(() => page.evaluate(() => localStorage.getItem("jarvis-dash-panel-w")))
		.toBe("34");
});
