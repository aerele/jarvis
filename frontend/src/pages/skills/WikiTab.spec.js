import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The wiki settings popover printed `wiki_mirror_last_sync_status` verbatim, so a
 * customer could read "failed: fleet returned 502" or be told to go and restart
 * something in Desk by "ok (restart via admin)". This pins the line to the shared
 * translator in @/lib/syncStatus, the same one the pool / skills / agents / billing
 * surfaces use.
 */

vi.mock("frappe-ui", () => ({
	call: vi.fn(async () => ({})),
	dayjs: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	dayjsLocal: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
	confirmDialog: vi.fn(),
	Autocomplete: {},
	Badge: {},
	Button: {},
	Dialog: {},
	FormControl: {},
	Popover: {},
	Tooltip: {},
}));
// sessionUser is a FUNCTION (slugPreview calls sessionUser()), not a ref — an
// object mock throws the moment the create dialog computes a User-scope slug.
vi.mock("@/data/session", () => ({ sessionUser: () => "a@x.com" }));
// jsdom here has no usable localStorage, and useListPage's persisted view state is
// beside the point for this test.
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return { ...actual, useStorage: (_key, initial) => ref(initial) };
});
vi.mock("@/api/wiki", () => ({
	listWikiPagesPage: vi.fn(async () => ({ rows: [], total: 0, has_more: false })),
	getWikiCaps: vi.fn(async () => ({})),
	createWikiPage: vi.fn(),
	archiveWikiPage: vi.fn(),
	restoreWikiPage: vi.fn(),
	deleteWikiPage: vi.fn(),
	setKnowledgeLanguage: vi.fn(),
	syncWikiMirrorNow: vi.fn(),
	runWikiLintNow: vi.fn(),
}));

import WikiTab from "./WikiTab.vue";
import { getWikiCaps, createWikiPage } from "@/api/wiki";

async function label(raw) {
	const w = mount(WikiTab, { shallow: true });
	await flushPromises();
	w.vm.caps.wiki_mirror_last_sync_status = raw;
	await flushPromises();
	return w.vm.wikiSyncLabel;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("wiki mirror sync label", () => {
	it("never shows the raw audit string", async () => {
		expect(await label("ok (restart via admin)")).toBe("Up to date");
		expect(await label("pending: mirroring wiki")).toBe("Applying your changes");
	});

	it("keeps the operator's failure reason, without the prefix", async () => {
		expect(await label("failed: fleet returned 502")).toBe(
			"Last sync failed: fleet returned 502"
		);
	});

	it("adds nothing when there is nothing recorded", async () => {
		expect(await label("")).toBe("");
	});

	it("degrades an unrecognised status instead of passing it through", async () => {
		expect(await label("weird_new_backend_state")).toBe("Status unavailable");
	});
});

describe("wiki role-scope create", () => {
	it("defaults to Role and creates a Role page with the picked target_role", async () => {
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["Role", "User"],
			manageable_roles: ["Sales User"],
		});
		createWikiPage.mockResolvedValueOnce({ ok: true, slug: "process--note--r-sales-user" });
		const w = mount(WikiTab, { shallow: true });
		await flushPromises();
		// the role picker is populated straight from caps.manageable_roles
		expect(w.vm.roleSelectOptions).toEqual([{ label: "Sales User", value: "Sales User" }]);
		w.vm.openCreate();
		expect(w.vm.createDialog.scope).toBe("Role");
		w.vm.createDialog.title = "Note";
		w.vm.createDialog.page_type = "Process";
		// a Role page cannot submit until a role is chosen
		expect(w.vm.canCreate).toBe(false);
		w.vm.createDialog.target_role = "Sales User";
		expect(w.vm.canCreate).toBe(true);
		await w.vm.doCreate();
		expect(createWikiPage).toHaveBeenCalledWith(
			expect.objectContaining({
				scope: "Role",
				target_role: "Sales User",
				page_type: "Process",
			})
		);
	});

	it("never offers or defaults to Role when no roles are manageable", async () => {
		// The backend hides Role from creatable_scopes when manageable_roles is
		// empty (e.g. a Jarvis Admin with only blanket roles), so the dialog opens
		// on a usable scope and never strands on an empty role picker.
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["User"],
			manageable_roles: [],
		});
		const w = mount(WikiTab, { shallow: true });
		await flushPromises();
		expect(w.vm.roleSelectOptions).toEqual([]);
		expect(w.vm.scopeSelectOptions.map((o) => o.value)).not.toContain("Role");
		w.vm.openCreate();
		expect(w.vm.createDialog.scope).toBe("User");
	});
});
