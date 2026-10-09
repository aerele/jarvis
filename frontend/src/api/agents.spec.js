import { describe, expect, it, vi, beforeEach } from "vitest";

const frappeUi = vi.hoisted(() => ({ call: vi.fn(async () => ({})) }));
vi.mock("frappe-ui", () => frappeUi);

import {
	getAPReviewDefaults,
	getARReviewDefaults,
	getBankReconReviewDefaults,
	installAgent,
} from "./agents.js";

const INSTALL = "jarvis.chat.agents_api.install_agent";

describe("getAPReviewDefaults", () => {
	it("requests suggestions for the selected company without saving configuration", async () => {
		frappeUi.call.mockClear();
		await getAPReviewDefaults("Example");
		expect(frappeUi.call.mock.calls).toEqual([
			["jarvis.chat.agents_api.get_ap_review_defaults", { company: "Example" }],
		]);
	});
});

describe("getARReviewDefaults", () => {
	it("requests only AR suggestions without persisting configuration", async () => {
		frappeUi.call.mockClear();
		await getARReviewDefaults("Example");
		expect(frappeUi.call.mock.calls).toEqual([
			["jarvis.chat.agents_api.get_ar_review_defaults", { company: "Example" }],
		]);
	});
});

describe("getBankReconReviewDefaults", () => {
	it("requests bank reconciliation suggestions for the company", async () => {
		frappeUi.call.mockClear();
		await getBankReconReviewDefaults("Example");
		expect(frappeUi.call.mock.calls).toEqual([
			["jarvis.chat.agents_api.get_bank_recon_review_defaults", { company: "Example" }],
		]);
	});
});

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
