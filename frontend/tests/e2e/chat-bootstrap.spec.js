import { test, expect } from "@playwright/test";

test("waits for conversation restoration before accepting a message", async ({ page }) => {
	let releaseSettings;
	const settingsReady = new Promise((resolve) => (releaseSettings = resolve));
	const sends = [];
	await page
		.context()
		.addCookies([
			{ name: "user_id", value: "review%40example.test", url: "http://localhost:8080" },
		]);
	await page.addInitScript(() => {
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
	});
	await page.route("**/api/method/**", async (route) => {
		const method = new URL(route.request().url()).pathname.split("/api/method/")[1];
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings")) {
			await settingsReady;
			message = { time_zone: "Asia/Kolkata" };
		} else if (method.endsWith("list_conversations")) {
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
	await input.fill("Continue in this conversation");
	await page.locator(".jv-sendbtn").click();
	await expect.poll(() => sends.length).toBe(1);
	expect(sends[0].conversation).toBe("restored-chat");
	expect(sends[0].message).toBe("Continue in this conversation");
});
