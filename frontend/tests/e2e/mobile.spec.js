// Real-browser mobile smoke for the /jarvis SPA. Boots the chat welcome screen
// hermetically (window boot injected, Frappe `call()`s mocked) and asserts, at
// several phone widths, that nothing scrolls horizontally and that the nav
// drawer opens/closes. This is the layout half of the mobile guard; the static
// half is tests/mobile-responsive.spec.js.
import { test, expect } from "@playwright/test";

// Frappe injects these on window at /jarvis in production (www/jarvis.py). The
// dev server does not, so seed the minimum the shell reads before app code runs.
const BOOT = {
	csrf_token: "test",
	time_zone: "Asia/Kolkata",
	agent_name: "Jarvis",
	is_system_manager: false,
	is_jarvis_admin: false,
	support_available: false,
	has_support_access: false,
	release_notice: "",
};

// One reply for every frappe-ui call(): {message: <payload>} shaped by method
// name. Just enough to reach the chat welcome screen without a bench.
function mockPayload(method) {
	if (method.includes("is_ready_for_chat")) return { ready: true, reason: null };
	if (method.includes("get_chat_ui_settings")) return { time_zone: "Asia/Kolkata" };
	if (method.includes("count")) return 0;
	if (method.includes("list_conversations") || method.includes("list_tools")) return [];
	if (method.includes("list")) return [];
	return {};
}

async function bootChat(page, width) {
	await page.addInitScript((boot) => Object.assign(window, boot), BOOT);
	await page.route("**/api/method/**", async (route) => {
		const method = decodeURIComponent(
			route.request().url().split("/api/method/")[1].split("?")[0]
		);
		await route.fulfill({
			status: 200,
			contentType: "application/json",
			body: JSON.stringify({ message: mockPayload(method) }),
		});
	});
	await page.setViewportSize({ width, height: 780 });
	await page.goto("/?nosocket");
	// The composer is the last thing to paint on the welcome screen; once it is
	// up the shell + chat have rendered.
	await expect(page.locator(".jv-composer-wrap")).toBeVisible({ timeout: 20000 });
}

test.describe("chat is usable at phone widths", () => {
	for (const width of [320, 375, 414]) {
		test(`no horizontal overflow @ ${width}px`, async ({ page }) => {
			await bootChat(page, width);
			const overflow = await page.evaluate(
				() => document.documentElement.scrollWidth - window.innerWidth
			);
			expect(overflow, "document must not scroll horizontally").toBeLessThanOrEqual(0);
			// The composer must sit within the viewport, not off the right edge.
			const box = await page.locator(".jv-composer-wrap").boundingBox();
			expect(box.x + box.width).toBeLessThanOrEqual(width + 1);
		});
	}

	test("hamburger opens the nav drawer and the scrim closes it @ 375px", async ({ page }) => {
		await bootChat(page, 375);
		const drawer = page.getByTestId("mobile-drawer");

		// Closed: translated off the left edge.
		expect((await drawer.boundingBox()).x).toBeLessThan(0);

		// Open via the chat header hamburger.
		await page.locator(".jv-hamburger").click();
		await expect.poll(async () => (await drawer.boundingBox()).x).toBe(0);

		// Close via the scrim.
		await page.getByTestId("drawer-scrim").click({ position: { x: 360, y: 400 } });
		await expect.poll(async () => (await drawer.boundingBox()).x).toBeLessThan(0);
	});
});
