import { describe, expect, it, vi, beforeEach } from "vitest";

const frappeUi = vi.hoisted(() => ({ call: vi.fn(async () => ({})) }));
vi.mock("frappe-ui", () => frappeUi);

import * as api from "./userAgents.js";

const UA = "jarvis.chat.user_agents_api.";
beforeEach(() => frappeUi.call.mockClear());

describe("userAgents API", () => {
	it("creates with the definition and 0/1 flags", async () => {
		await api.createUserAgent({
			agent_name: "A",
			instructions: "do it",
			tools: ["get_list"],
			allow_shell: true,
		});
		expect(frappeUi.call).toHaveBeenCalledWith(UA + "create_user_agent", {
			agent_name: "A",
			instructions: "do it",
			description: "",
			tools: ["get_list"],
			allow_shell: 1,
		});
	});
	it("updates by name", async () => {
		await api.updateUserAgent("UA-1", { agent_name: "B", instructions: "x" });
		expect(frappeUi.call).toHaveBeenCalledWith(UA + "update_user_agent", {
			name: "UA-1",
			agent_name: "B",
			instructions: "x",
			description: "",
			tools: [],
			allow_shell: 0,
		});
	});
	it("sends the schedule day only for its frequency", async () => {
		await api.setUserAgentSchedule("UA-1", {
			enabled: true,
			frequency: "monthly",
			time: "09:00",
			weekday: "Monday",
			day_of_month: "15",
			prompt: "go",
		});
		expect(frappeUi.call).toHaveBeenCalledWith(UA + "set_user_agent_schedule", {
			name: "UA-1",
			schedule_enabled: 1,
			schedule_frequency: "monthly",
			schedule_time: "09:00",
			schedule_weekday: "",
			schedule_day_of_month: 15,
			schedule_prompt: "go",
		});
	});
	it("switches an agent on and off", async () => {
		await api.setUserAgentEnabled("UA-1", false);
		expect(frappeUi.call).toHaveBeenCalledWith(UA + "set_user_agent_enabled", {
			name: "UA-1",
			enabled: 0,
		});
	});
	it("runs now with the saved prompt unless one is given", async () => {
		await api.runUserAgentNow("UA-1");
		expect(frappeUi.call).toHaveBeenLastCalledWith(UA + "run_user_agent_now", {
			name: "UA-1",
		});
		await api.runUserAgentNow("UA-1", "go");
		expect(frappeUi.call).toHaveBeenLastCalledWith(UA + "run_user_agent_now", {
			name: "UA-1",
			prompt: "go",
		});
	});
	it("binds the plain calls", async () => {
		await api.listUserAgents();
		await api.listPickableTools();
		await api.startUserAgentChat("UA-1");
		await api.retryUserAgentSync("UA-1");
		await api.deleteUserAgent("UA-1");
		expect(frappeUi.call.mock.calls.map((c) => c[0])).toEqual(
			[
				"list_user_agents",
				"list_pickable_tools",
				"start_user_agent_chat",
				"retry_user_agent_sync",
				"delete_user_agent",
			].map((n) => UA + n)
		);
	});

	it("sends the browser fields only when the caller has them", async () => {
		await api.createUserAgent({ agent_name: "A", instructions: "x" });
		expect(frappeUi.call.mock.calls[0][1]).not.toHaveProperty("allow_browser");
		await api.createUserAgent({
			agent_name: "A",
			instructions: "x",
			allow_browser: true,
			browser_mode: "public",
			browser_sites: [],
		});
		expect(frappeUi.call.mock.calls[1][1]).toMatchObject({
			allow_browser: 1,
			browser_mode: "public",
			browser_sites: [],
		});
		await api.updateUserAgent("UA-1", {
			agent_name: "A",
			instructions: "x",
			allow_browser: false,
			browser_mode: "listed",
			browser_sites: ["example.com"],
		});
		expect(frappeUi.call.mock.calls[2][1]).toMatchObject({
			name: "UA-1",
			allow_browser: 0,
			browser_mode: "listed",
			browser_sites: ["example.com"],
		});
	});
});
