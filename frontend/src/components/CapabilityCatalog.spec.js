import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Stub frappe-ui to a bare Badge: importing the real module pulls its whole
// resource graph, which doesn't resolve under vitest. We only use Badge, and we
// assert on its rendered label text.
vi.mock("frappe-ui", () => ({
	Badge: {
		props: ["label", "theme"],
		template: '<span class="badge-stub" :data-theme="theme">{{ label }}</span>',
	},
}));
vi.mock("@/api", () => ({
	getCapabilityCatalog: vi.fn(),
	logCapabilityPick: vi.fn(() => Promise.resolve({ ok: true })),
}));
import { getCapabilityCatalog, logCapabilityPick } from "@/api";
import CapabilityCatalog from "./CapabilityCatalog.vue";

const mountIt = () => mount(CapabilityCatalog);

const OK = {
	ok: true,
	data: {
		cards: [
			{ group: "Look things up", title: "Owed?", prompt: "owed?", badge: "reads_only" },
			{
				group: "Send & submit",
				title: "Email it",
				prompt: "email it",
				badge: "always_asks",
			},
		],
	},
};

describe("CapabilityCatalog", () => {
	beforeEach(() => vi.clearAllMocks());

	it("is lazy: fetches only on first expand, groups, shows badge labels, forwards select + logs pick", async () => {
		getCapabilityCatalog.mockResolvedValueOnce(OK);
		const w = mountIt();
		expect(getCapabilityCatalog).not.toHaveBeenCalled();

		await w.find(".jv-catalog-toggle").trigger("click");
		await flushPromises();
		expect(getCapabilityCatalog).toHaveBeenCalledTimes(1);
		expect(w.text()).toContain("Look things up");
		expect(w.text()).toContain("Send & submit");
		expect(w.text()).toContain("Always asks you");

		await w.find(".jv-cap-card").trigger("click");
		expect(w.emitted().select[0][0]).toMatchObject({ title: "Owed?", prompt: "owed?" });
		expect(logCapabilityPick).toHaveBeenCalledWith("Owed?");
	});

	it("shows a retryable error state on fetch failure (never a silent blank)", async () => {
		getCapabilityCatalog.mockRejectedValueOnce(new Error("boom"));
		const w = mountIt();
		await w.find(".jv-catalog-toggle").trigger("click");
		await flushPromises();
		expect(w.find(".jv-catalog-error").exists()).toBe(true);
		expect(w.text().toLowerCase()).toContain("couldn't load");

		getCapabilityCatalog.mockResolvedValueOnce(OK);
		await w.find(".jv-catalog-retry").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Look things up");
	});

	it("shows an empty state when the catalog returns no cards", async () => {
		getCapabilityCatalog.mockResolvedValueOnce({ ok: true, data: { cards: [] } });
		const w = mountIt();
		await w.find(".jv-catalog-toggle").trigger("click");
		await flushPromises();
		expect(w.find(".jv-catalog-empty").exists()).toBe(true);
	});
});
