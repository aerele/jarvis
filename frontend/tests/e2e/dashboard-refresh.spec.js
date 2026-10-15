import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

// Real dashboard components with synthetic sales data, isolated from Frappe/providers.
const dashboardHtml = `<section style="padding:24px">
<h1>Sales overview</h1><p>Sales performance for the selected region.</p>
<div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:24px">
<article style="border:1px solid #ddd;border-radius:12px;padding:24px;min-width:180px"><p>Net sales</p><h2 id="sales">Loading…</h2></article>
<article style="border:1px solid #ddd;border-radius:12px;padding:24px;min-width:180px"><p>Invoices</p><h2>48</h2></article>
</div></section>
<script type="application/json" id="jarvis-sources">{"sources":[{"source_name":"sales","tool":"query","spec":{"doctype":"Sales Invoice"}}]}</script>
<script type="application/json" id="jarvis-filters">{"filters":[{"fieldname":"region","label":"Region","fieldtype":"Data","default":"North"}]}</script>
<script>jarvis.data("sales").then(rows => document.getElementById("sales").textContent = rows[0].total);</script>`;

async function setup(page, baseURL) {
	await page
		.context()
		.addCookies([{ name: "user_id", value: "review%40example.test", url: baseURL }]);
	await page.addInitScript(() => {
		Object.assign(window, {
			csrf_token: "test",
			time_zone: "Asia/Kolkata",
			agent_name: "Jarvis",
			is_system_manager: false,
			is_jarvis_admin: false,
			support_available: false,
			has_support_access: false,
		});
	});
	const requests = [];
	let total = "₹125,000";
	let release = null;
	await page.route("**/api/method/**", async (route) => {
		const method = new URL(route.request().url()).pathname.split("/api/method/")[1];
		let message = {};
		if (method.endsWith("is_ready_for_chat")) message = { ready: true };
		else if (method.endsWith("get_chat_ui_settings"))
			message = { time_zone: "Asia/Kolkata", llm_model: "Demo model" };
		else if (method.endsWith("get_dashboards_caps"))
			message = { ok: true, data: { creatable_scopes: ["User"] } };
		else if (method.endsWith("get_dashboard"))
			message = {
				ok: true,
				data: {
					name: "refresh-demo",
					dashboard_title: "Sales overview",
					can_edit: true,
					scope: "User",
					theme: "Jarvis",
					dashboard_type: "Connected",
					html: dashboardHtml,
					description: "Sales performance by region — demonstration data.",
					filters: [
						{
							fieldname: "region",
							label: "Region",
							fieldtype: "Data",
							default: "North",
						},
					],
				},
			};
		else if (
			method.endsWith("run_dashboard_source") ||
			method.endsWith("preview_dashboard_source")
		) {
			requests.push(route.request().postDataJSON());
			if (release) await release;
			message = { ok: true, data: { rows: [{ total }] } };
		} else if (method.endsWith("get_conversation"))
			message = { conversation: {}, messages: [] };
		else if (method.includes("count")) message = 0;
		else if (method.includes("list")) message = [];
		await route.fulfill({ json: { message } });
	});
	return {
		requests,
		setTotal(value) {
			total = value;
		},
		pause() {
			let done;
			release = new Promise((r) => {
				done = r;
			});
			return () => {
				release = null;
				done();
			};
		},
	};
}

for (const surface of ["builder", "viewer"]) {
	for (const [width, height] of [
		[1440, 900],
		[375, 667],
	]) {
		test(`${surface} refresh at ${width}x${height}`, async ({ page, baseURL }) => {
			await page.setViewportSize({ width, height });
			const data = await setup(page, baseURL);
			await page.goto(
				surface === "builder"
					? "/jarvis/dashboards?edit=refresh-demo&nosocket"
					: "/jarvis/dashboards/refresh-demo?nosocket"
			);
			if (surface === "builder" && width < 768)
				await page.getByRole("button", { name: "Hide chat", exact: true }).click();
			const frame = page.frameLocator('iframe[title="Dashboard canvas"]');
			await expect(frame.locator("#sales")).toHaveText("₹125,000");
			const isBefore = process.env.DASHBOARD_SCREENSHOT_PHASE === "before";
			const refresh = page.getByRole("button", { name: "Refresh", exact: true });
			if (isBefore) await expect(refresh).toHaveCount(0);
			else {
				await expect(refresh).toBeEnabled();
				const rect = await refresh.boundingBox();
				expect(rect.x).toBeGreaterThanOrEqual(0);
				expect(rect.x + rect.width).toBeLessThanOrEqual(width);
			}
			if (process.env.DASHBOARD_SCREENSHOTS) {
				fs.mkdirSync(process.env.DASHBOARD_SCREENSHOTS, { recursive: true });
				await page.screenshot({
					animations: "disabled",
					path: path.join(
						process.env.DASHBOARD_SCREENSHOTS,
						`${surface}-${width}-${isBefore ? "before" : "after"}.png`
					),
				});
			}
			if (isBefore) return;
			const previous = data.requests.length;
			const resume = data.pause();
			data.setTotal("₹136,000");
			await refresh.click();
			await expect(refresh).toBeDisabled();
			await expect.poll(() => data.requests.length).toBe(previous + 1);
			resume();
			await expect(frame.locator("#sales")).toHaveText("₹136,000");
			await expect(refresh).toBeEnabled();
			expect(JSON.parse(data.requests.at(-1).filters)).toEqual({ region: "North" });
		});
	}
}
