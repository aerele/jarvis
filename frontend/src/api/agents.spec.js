import { describe, expect, it, vi, beforeEach } from "vitest";

const frappeUi = vi.hoisted(() => ({ call: vi.fn(async () => ({})) }));
vi.mock("frappe-ui", () => frappeUi);

import { installAgent } from "./agents.js";

const INSTALL = "jarvis.chat.agents_api.install_agent";

describe("installAgent", () => {
	beforeEach(() => frappeUi.call.mockClear());

	it("sends the pick with the install request", async () => {
		await installAgent("close-auditor", { provider: "openai", model: "gpt-5" });
		expect(frappeUi.call).toHaveBeenCalledWith(INSTALL, {
			agent_slug: "close-auditor",
			model_provider: "openai",
			model: "gpt-5",
		});
	});

	it("keeps an empty provider (a subscription lane) as an empty string", async () => {
		await installAgent("close-auditor", { provider: "", model: "kimi-sub" });
		expect(frappeUi.call).toHaveBeenCalledWith(INSTALL, {
			agent_slug: "close-auditor",
			model_provider: "",
			model: "kimi-sub",
		});
	});

	it("marks an unchanged shown pick so a row pinned since is kept", async () => {
		await installAgent("close-auditor", {
			provider: "openai",
			model: "gpt-5",
			ifUnpinned: true,
		});
		expect(frappeUi.call).toHaveBeenCalledWith(INSTALL, {
			agent_slug: "close-auditor",
			model_provider: "openai",
			model: "gpt-5",
			pick_if_unpinned: 1,
		});
	});

	it("sends no model params without a pick", async () => {
		await installAgent("close-auditor");
		await installAgent("close-auditor", null);
		for (const args of frappeUi.call.mock.calls) {
			expect(args).toEqual([INSTALL, { agent_slug: "close-auditor" }]);
		}
	});
});
