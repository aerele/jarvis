import { test, expect } from "@playwright/test";

test("shows report scope without model narration and survives reload", async ({ page }) => {
	const sends = [];
	await page
		.context()
		.addCookies([
			{ name: "user_id", value: "review%40example.test", url: "http://localhost:8080" },
		]);
	await page.addInitScript(() => {
		localStorage.setItem("jarvis-activity-detail", "0");
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
				messages: [
					{ name: "u1", role: "user", content: "What is payable?", seq: 1 },
					{ name: "a1", role: "assistant", content: "No matching rows.", seq: 2 },
					{
						name: "t1",
						role: "tool",
						tool_name: "run_report",
						tool_status: "completed",
						seq: 3,
						tool_result: JSON.stringify({
							ok: true,
							data: {
								result: [],
								report_scope: {
									version: 1,
									report_name: "Accounts Payable Summary",
									company: "Example Company",
									report_date: "2026-09-24",
									currency_mode: "company",
									currency: "INR",
								},
							},
						}),
					},
					{ name: "u2", role: "user", content: "Hello", seq: 4 },
					{ name: "a2", role: "assistant", content: "Hello!", seq: 5 },
				],
			};
		} else if (method.endsWith("send_message")) {
			sends.push(route.request().postDataJSON());
			message = { ok: true, run_id: "test-run", conversation_id: "restored-chat" };
		} else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	await page.goto("/?nosocket");

	const scope = page.locator(".report-scope");
	await expect(scope).toHaveCount(1);
	await expect(scope).toContainText("Example Company · As of 2026-09-24 · INR");
	await expect(page.locator(".jv-activity-body")).toHaveCount(0);
	await page.reload();
	await expect(scope).toHaveCount(1);
	await expect(scope).toContainText("Example Company");
	await page.setViewportSize({ width: 390, height: 844 });
	await expect(scope).toBeVisible();
	expect(await scope.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
});
