import { test, expect } from "@playwright/test";

const BOOT_TIMEOUT_NOTICE = "Chat is taking longer than usual to load";

// Mocks the Frappe boot calls. `handlers` maps a method-name suffix to an async
// (route) => message override; returning undefined means the handler fulfilled
// (or deliberately never fulfils) the route itself.
async function mockBoot(page, { handlers = {}, bootTimeoutMs } = {}) {
	const sends = [];
	await page
		.context()
		.addCookies([
			{ name: "user_id", value: "review%40example.test", url: "http://localhost:8080" },
		]);
	await page.addInitScript((timeoutMs) => {
		Object.assign(window, {
			csrf_token: "test",
			time_zone: "Asia/Kolkata",
			agent_name: "Jarvis",
			is_system_manager: false,
			is_jarvis_admin: false,
			support_available: false,
			has_support_access: false,
			release_notice: "",
		});
		if (timeoutMs) window.__JARVIS_BOOT_TIMEOUT_MS = timeoutMs;
	}, bootTimeoutMs);
	await page.route("**/api/method/**", async (route) => {
		const method = new URL(route.request().url()).pathname.split("/api/method/")[1];
		const override = Object.keys(handlers).find((suffix) => method.endsWith(suffix));
		if (override) return handlers[override](route);
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) message = { time_zone: "Asia/Kolkata" };
		else if (method.endsWith("list_conversations")) {
			message = [{ name: "restored-chat", title: "Existing conversation" }];
		} else if (method.endsWith("get_conversation")) {
			message = {
				conversation: {
					name: "restored-chat",
					title: "Existing conversation",
					status: "Active",
				},
				messages: [],
			};
		} else if (method.endsWith("send_message")) {
			sends.push(route.request().postDataJSON());
			message = { ok: true, run_id: "test-run", conversation_id: "restored-chat" };
		} else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	return sends;
}

async function sendAndExpect(page, sends, text) {
	const input = page.locator(".jv-composer-wrap textarea");
	await input.fill(text);
	await page.locator(".jv-sendbtn").click();
	await expect.poll(() => sends.length).toBe(1);
	expect(sends[0].conversation).toBe("restored-chat");
	expect(sends[0].message).toBe(text);
}

test("waits for conversation restoration before accepting a message", async ({ page }) => {
	let releaseSettings;
	const settingsReady = new Promise((resolve) => (releaseSettings = resolve));
	const sends = await mockBoot(page, {
		handlers: {
			get_chat_ui_settings: async (route) => {
				await settingsReady;
				await route.fulfill({ json: { message: { time_zone: "Asia/Kolkata" } } });
			},
		},
	});
	await page.goto("/?nosocket");
	const input = page.locator(".jv-composer-wrap textarea");
	await expect(input).toBeVisible({ timeout: 20000 });
	try {
		await expect(input).toBeDisabled();
		expect(sends).toHaveLength(0);
	} finally {
		releaseSettings();
	}
	await expect(input).toBeEnabled();
	await sendAndExpect(page, sends, "Continue in this conversation");
});

test("a boot settings call that never settles releases the composer after the deadline", async ({
	page,
}) => {
	const sends = await mockBoot(page, {
		bootTimeoutMs: 1500,
		handlers: { get_chat_ui_settings: () => new Promise(() => {}) },
	});
	await page.goto("/?nosocket");
	const input = page.locator(".jv-composer-wrap textarea");
	await expect(input).toBeVisible({ timeout: 20000 });
	await expect(input).toBeEnabled({ timeout: 10000 });
	await expect(page.getByText(BOOT_TIMEOUT_NOTICE)).toBeVisible();
	await sendAndExpect(page, sends, "Still works after a stalled boot");
});

test("a conversation load that never settles keeps the restored destination", async ({ page }) => {
	const sends = await mockBoot(page, {
		bootTimeoutMs: 1500,
		handlers: { get_conversation: () => new Promise(() => {}) },
	});
	await page.goto("/?nosocket");
	const input = page.locator(".jv-composer-wrap textarea");
	await expect(input).toBeVisible({ timeout: 20000 });
	await expect(input).toBeEnabled({ timeout: 10000 });
	await expect(page.getByText(BOOT_TIMEOUT_NOTICE)).toBeVisible();
	await sendAndExpect(page, sends, "Send into the restored chat");
});

test("a failing boot settings call recovers the composer without a timeout notice", async ({
	page,
}) => {
	const sends = await mockBoot(page, {
		handlers: {
			get_chat_ui_settings: (route) =>
				route.fulfill({ status: 500, json: { exc_type: "Exception" } }),
		},
	});
	await page.goto("/?nosocket");
	const input = page.locator(".jv-composer-wrap textarea");
	await expect(input).toBeVisible({ timeout: 20000 });
	await expect(input).toBeEnabled();
	await expect(page.getByText(BOOT_TIMEOUT_NOTICE)).toHaveCount(0);
	await sendAndExpect(page, sends, "Works after a failed settings call");
});
