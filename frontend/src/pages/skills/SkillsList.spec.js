import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi } from "vitest";

// A skill File Box follows to a fixed document type carries a "File Box → <type>" chip.
vi.mock("vue-router", () => ({
	useRoute: () => ({ query: {} }),
	useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("frappe-ui", () => ({
	Badge: { props: ["label"], template: '<span class="badge">{{ label }}</span>' },
	Button: { props: ["label"], template: "<button>{{ label }}</button>" },
	Avatar: { template: "<i/>" },
	Tooltip: { template: "<span><slot/></span>" },
	Dropdown: { template: "<div/>" },
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
	confirmDialog: vi.fn(),
}));
const rows = vi.hoisted(() => [
	{ name: "S1", skill_name: "bills", use_in_file_box: 1, file_box_creates: "Purchase Invoice" },
	{ name: "S2", skill_name: "opted-out", use_in_file_box: 0, file_box_creates: "Sales Invoice" },
	{ name: "S3", skill_name: "any-type", use_in_file_box: 1, file_box_creates: null },
	{ name: "S4", skill_name: "held", admin_hold: 1, admin_hold_reason: "Emails customers" },
	{ name: "S5", skill_name: "held-quietly", admin_hold: 1, admin_hold_reason: "" },
]);
// What the list page gave useListPage, so a test can answer as the server would.
const list = vi.hoisted(() => ({ onEnvelope: null }));
vi.mock("@/composables/useListPage", async () => {
	const { ref, reactive } = await import("vue");
	return {
		useListPage: (opts) => {
			list.onEnvelope = opts.onEnvelope;
			return {
				rows: ref(rows),
				total: ref(rows.length),
				hasMore: ref(false),
				loading: ref(false),
				filters: reactive({}),
				sort: reactive({}),
				pageLength: ref(20),
				filterState: ref({}),
				setFilters: vi.fn(),
				setSort: vi.fn(),
				resetLoad: vi.fn(),
				loadMore: vi.fn(),
				setClauses: vi.fn(),
				requestSchema: vi.fn(),
				dismissFilterNotice: vi.fn(),
			};
		},
	};
});
vi.mock("@/components/list/ListPage.vue", () => ({
	default: {
		props: ["rows"],
		template: `<div><slot name="banner" /><div v-for="row in rows" :key="row.name" class="row" :data-name="row.name"><slot name="cell-skill_name" :row="row" /></div></div>`,
	},
}));
vi.mock("@/pages/list/listFetchers", () => ({ skillsListFetch: vi.fn() }));
vi.mock("./SyncPill.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("@/data/session", () => ({ session: { user: "me@example.com" } }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "now", exactDate: () => "" }));
const apiSkills = vi.hoisted(() => ({
	deleteCustomSkillsBulk: vi.fn(),
	dismissSkillNotices: vi.fn(),
}));
vi.mock("@/api/skills", () => apiSkills);

import { toast } from "frappe-ui";
import SkillsList from "./SkillsList.vue";

describe("SkillsList: the File Box chip", () => {
	it("shows File Box → <type> only for a skill File Box uses with a fixed type", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		const chips = (name) => w.findAll(`.row[data-name="${name}"] .badge`).map((b) => b.text());
		expect(chips("S1")).toEqual(["File Box → Purchase Invoice"]);
		expect(chips("S2")).toEqual([]);
		expect(chips("S3")).toEqual([]);
		w.unmount();
	});
});

describe("SkillsList: an administrator's hold", () => {
	it("says on the row that it is on hold, with the reason", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		const held = (name) => w.find(`.row[data-name="${name}"] .jv-skill-held`);
		expect(held("S4").text()).toBe("On hold by an administrator: Emails customers");
		expect(held("S4").attributes("title")).toBe("On hold by an administrator: Emails customers");
		expect(held("S5").text()).toBe("On hold by an administrator");
		expect(held("S1").exists()).toBe(false);
		w.unmount();
	});
});

describe("SkillsList: what an administrator did", () => {
	const notice = (n) => ({
		name: `N${n}`,
		message: `An administrator deleted your skill "s${n}".`,
		creation: "2026-10-01 10:00:00",
	});
	const shown = (w) => w.findAll(".jv-skill-notice").map((n) => n.attributes("data-notice"));

	it("shows nothing until the list says something happened", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		expect(w.find('[data-testid="admin-notices"]').exists()).toBe(false);
		w.unmount();
	});

	it("reads the notice's text from its message, or its subject when that is what the server sends", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		list.onEnvelope({ notices: [{ name: "N9", subject: "Subject only", at: "" }] });
		await flushPromises();
		expect(w.find(".jv-skill-notice").text()).toContain("Subject only");
		w.unmount();
	});

	it("shows each notice's text as a line", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		list.onEnvelope({ notices: [notice(1), notice(2)] });
		await flushPromises();
		expect(shown(w)).toEqual(["N1", "N2"]);
		expect(w.find(".jv-skill-notice").text()).toContain('An administrator deleted your skill "s1".');
		w.unmount();
	});

	it("draws at most five, however many the list sends", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		list.onEnvelope({ notices: [1, 2, 3, 4, 5, 6, 7].map(notice) });
		await flushPromises();
		expect(shown(w)).toEqual(["N1", "N2", "N3", "N4", "N5"]);
		w.unmount();
	});

	it("keeps what it showed when an answer carries no notices key", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		list.onEnvelope({ notices: [notice(1)] });
		list.onEnvelope({ rows: [] });
		await flushPromises();
		expect(shown(w)).toEqual(["N1"]);
		w.unmount();
	});

	it("dismisses through the server, and the line goes when it says so", async () => {
		apiSkills.dismissSkillNotices.mockResolvedValue({ ok: true });
		const w = mount(SkillsList);
		await flushPromises();
		list.onEnvelope({ notices: [notice(1), notice(2)] });
		await flushPromises();
		await w.find('.jv-skill-notice[data-notice="N1"] button').trigger("click");
		await flushPromises();
		expect(apiSkills.dismissSkillNotices).toHaveBeenCalledWith(["N1"]);
		expect(shown(w)).toEqual(["N2"]);
		w.unmount();
	});

	it("keeps the line, and says why, when the dismissal fails", async () => {
		apiSkills.dismissSkillNotices.mockRejectedValue(new Error("Server is busy"));
		const w = mount(SkillsList);
		await flushPromises();
		list.onEnvelope({ notices: [notice(1)] });
		await flushPromises();
		await w.find(".jv-skill-notice button").trigger("click");
		await flushPromises();
		expect(shown(w)).toEqual(["N1"]);
		expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("Server is busy"));
		w.unmount();
	});

	it("shows a sixth when one of the five is dismissed", async () => {
		apiSkills.dismissSkillNotices.mockResolvedValue({ ok: true });
		const w = mount(SkillsList);
		await flushPromises();
		list.onEnvelope({ notices: [1, 2, 3, 4, 5, 6].map(notice) });
		await flushPromises();
		await w.find('.jv-skill-notice[data-notice="N1"] button').trigger("click");
		await flushPromises();
		expect(shown(w)).toEqual(["N2", "N3", "N4", "N5", "N6"]);
		w.unmount();
	});
});
