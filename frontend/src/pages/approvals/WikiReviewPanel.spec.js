import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("frappe-ui", () => ({
	Badge: { props: ["label"], template: "<span>{{ label }}</span>" },
	Button: {
		props: ["label", "disabled"],
		emits: ["click"],
		template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api", () => ({
	listWikiWriteProposals: vi.fn(),
	approveWikiWrite: vi.fn(),
	rejectWikiWrite: vi.fn(),
	retryWikiWrite: vi.fn(),
}));
import * as api from "@/api";
import WikiReviewPanel from "./WikiReviewPanel.vue";

const row = (append_md, wiki_digest) => ({
	name: "AR-1",
	title: "Fake Co",
	status: "Pending",
	can_approve: 1,
	needs_retry: 0,
	wiki_digest,
	preview: { slug: "party-fake-co", append_md },
});

describe("WikiReviewPanel", () => {
	beforeEach(() => vi.clearAllMocks());

	it("reloads the proposal when approve is refused because it changed", async () => {
		api.listWikiWriteProposals
			.mockResolvedValueOnce({ rows: [row("old note", "d1")] })
			.mockResolvedValueOnce({ rows: [row("new note", "d2")] });
		api.approveWikiWrite.mockRejectedValueOnce(
			Object.assign(new Error("This proposal changed since you opened it"), {
				exc_type: "PermissionError",
			})
		);
		const w = mount(WikiReviewPanel);
		await flushPromises();
		expect(w.text()).toContain("old note");

		await w
			.findAll("button")
			.find((b) => b.text() === "Approve")
			.trigger("click");
		await flushPromises();
		expect(api.approveWikiWrite).toHaveBeenCalledWith("AR-1", "d1");
		expect(api.listWikiWriteProposals).toHaveBeenCalledTimes(2);
		expect(w.text()).toContain("new note");
	});
});
