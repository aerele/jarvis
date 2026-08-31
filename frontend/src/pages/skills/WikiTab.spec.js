import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The wiki settings popover printed `wiki_mirror_last_sync_status` verbatim, so a
 * customer could read "failed: fleet returned 502" or be told to go and restart
 * something in Desk by "ok (restart via admin)". This pins the line to the shared
 * translator in @/lib/syncStatus, the same one the pool / skills / agents / billing
 * surfaces use.
 *
 * The former "wiki role-scope create" suite lived here (openCreate/createDialog/
 * doCreate on WikiTab's own state). Create moved to the routed
 * pages/wiki/WikiDetail.vue page (WikiTab now only navigates there); that
 * component isn't unit-mounted here for the same reason SkillDetail.vue and
 * MacroDetail.vue aren't - the codebase has no mount-based spec for the
 * onBeforeRouteLeave-guarded doc-page shape.
 */

vi.mock("frappe-ui", () => ({
	call: vi.fn(async () => ({})),
	dayjs: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	dayjsLocal: () => ({ format: () => "", fromNow: () => "", isValid: () => false }),
	getConfig: () => null,
	toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
	confirmDialog: vi.fn(),
	Badge: {},
	Button: {},
	Popover: {},
	Tooltip: {},
}));
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
	archiveWikiPage: vi.fn(),
	restoreWikiPage: vi.fn(),
	deleteWikiPage: vi.fn(),
	setKnowledgeLanguage: vi.fn(),
	syncWikiMirrorNow: vi.fn(),
	runWikiLintNow: vi.fn(),
}));

import WikiTab from "./WikiTab.vue";

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
