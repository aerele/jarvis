import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * WikiDetail.vue is where wiki create/scope/role governance now lives (moved
 * out of WikiTab.vue by the routed-page refactor - see WikiTab.spec.js's
 * header comment). The old "wiki role-scope create" suite that covered this
 * logic on WikiTab was deleted with the move and never re-created here, so
 * canCreate/doCreate/resetCreateForm had zero coverage. This mirrors that
 * deleted suite's approach almost exactly: `shallow: true` so DocPage,
 * DocSection, the promotion components and the frappe-ui controls never run
 * their own setup (no vue-router/@/api/skills wiring needed), then drive
 * wrapper.vm.form / wrapper.vm.canCreate / wrapper.vm.doCreate() /
 * wrapper.vm.resetCreateForm() directly - no DOM queries.
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
	Breadcrumbs: {},
	Button: {},
	Dialog: {},
	Dropdown: {},
	FeatherIcon: {},
	FormControl: {},
	Tooltip: {},
}));
// WikiDetail calls onBeforeRouteLeave (a dirty-guard) and useRouter() (doCreate's
// router.replace) at setup time - both must exist as plain no-ops, not a real
// router: mounting outside an actual route context is exactly why WikiTab's own
// spec says "no mount-based spec for the onBeforeRouteLeave-guarded doc-page
// shape" for THIS component - shallow-mounting it directly, as this file does,
// is what sidesteps that (DocPage's own useRouter never runs, and this mock
// covers WikiDetail's own two direct vue-router calls).
vi.mock("vue-router", () => ({
	useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
	onBeforeRouteLeave: vi.fn(),
}));
// sessionUser is a FUNCTION (slugPreview calls sessionUser()), not a ref - same
// trap the deleted WikiTab suite noted.
vi.mock("@/data/session", () => ({ sessionUser: () => "a@x.com" }));
vi.mock("@/api/wiki", () => ({
	getWikiCaps: vi.fn(async () => ({})),
	getWikiPage: vi.fn(),
	createWikiPage: vi.fn(),
	saveWikiPage: vi.fn(),
	archiveWikiPage: vi.fn(),
	restoreWikiPage: vi.fn(),
	deleteWikiPage: vi.fn(),
	requestWikiPromotion: vi.fn(),
	myWikiPromotion: vi.fn(),
}));

import WikiDetail from "./WikiDetail.vue";
import { getWikiCaps, createWikiPage } from "@/api/wiki";

// init() (watch(..., { immediate: true })) fires loadCaps() -> resetCreateForm()
// as soon as the component is created, so caps must be flushed BEFORE a test
// sets its own form fields - otherwise resetCreateForm() blows the fields away
// after the fact.
async function mountCreate() {
	const w = mount(WikiDetail, { props: { isNew: true, slug: "" }, shallow: true });
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("WikiDetail create-mode canCreate (Role scope gating)", () => {
	it("stays false in Role scope until target_role is set, true once title+type+role are present", async () => {
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["Org", "Role", "User"],
			manageable_roles: ["Sales User"],
		});
		const w = await mountCreate();

		w.vm.form.title = "Note";
		w.vm.form.page_type = "Process";
		w.vm.form.scope = "Role";
		// title/type alone are not enough once scope is Role (WikiDetail.vue ~401-407)
		expect(w.vm.canCreate).toBe(false);

		w.vm.form.target_role = "Sales User";
		expect(w.vm.canCreate).toBe(true);
	});

	it("does not require a role for a non-Role scope", async () => {
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["Org"],
			manageable_roles: [],
		});
		const w = await mountCreate();

		w.vm.form.title = "Note";
		w.vm.form.page_type = "Process";
		w.vm.form.scope = "Org";
		expect(w.vm.canCreate).toBe(true);
	});
});

describe("WikiDetail doCreate payload (scope/target_role)", () => {
	it("sends target_role for Role scope and never leaks a stale one once scope changes away from Role", async () => {
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["Org", "Role"],
			manageable_roles: ["Sales User"],
		});
		createWikiPage.mockResolvedValueOnce({ ok: true, slug: "process--note--r-sales-user" });
		const w = await mountCreate();

		w.vm.form.title = "Note";
		w.vm.form.page_type = "Process";
		w.vm.form.scope = "Role";
		w.vm.form.target_role = "Sales User";
		await w.vm.doCreate();
		expect(createWikiPage).toHaveBeenCalledWith(
			expect.objectContaining({ scope: "Role", target_role: "Sales User", page_type: "Process" })
		);

		// scope flips back to Org WITHOUT clearing form.target_role - doCreate's own
		// ternary (WikiDetail.vue ~424: `form.scope === "Role" ? form.target_role : ""`)
		// is what must drop it, not some upstream reset.
		createWikiPage.mockResolvedValueOnce({ ok: true, slug: "process--note" });
		w.vm.form.scope = "Org";
		await w.vm.doCreate();
		expect(createWikiPage).toHaveBeenCalledWith(
			expect.objectContaining({ scope: "Org", target_role: "" })
		);
	});

	it("also sends an empty target_role for User scope", async () => {
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["User"],
			manageable_roles: [],
		});
		createWikiPage.mockResolvedValueOnce({ ok: true, slug: "process--note--u-me" });
		const w = await mountCreate();

		w.vm.form.title = "Note";
		w.vm.form.page_type = "Process";
		w.vm.form.scope = "User";
		await w.vm.doCreate();
		expect(createWikiPage).toHaveBeenCalledWith(
			expect.objectContaining({ scope: "User", target_role: "" })
		);
	});
});

describe("WikiDetail create-mode scope defaults (resetCreateForm)", () => {
	it("defaults form.scope to the first creatable scope on init", async () => {
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["User", "Org"],
			manageable_roles: [],
		});
		const w = await mountCreate();
		expect(w.vm.form.scope).toBe("User");
	});

	it("falls back to Org when caps carries no creatable scopes", async () => {
		// init() itself only calls resetCreateForm() once caps.creatable_scopes is
		// known non-empty (it shows a permission error otherwise - WikiDetail.vue
		// ~659-664), so resetCreateForm's own `|| "Org"` fallback (~374) is exercised
		// directly here, the same way the deleted WikiTab suite drove createDialog
		// state straight from the component instance. Seeding with "User" first (not
		// "Org") makes this discriminating: form.scope must actually CHANGE to "Org"
		// once caps is emptied and resetCreateForm() reruns, not just happen to
		// already be "Org" from the initial mount.
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["User"],
			manageable_roles: [],
		});
		const w = await mountCreate();
		expect(w.vm.form.scope).toBe("User");
		w.vm.caps.creatable_scopes = [];
		w.vm.resetCreateForm();
		expect(w.vm.form.scope).toBe("Org");
	});

	it("never offers or defaults to Role when no roles are manageable", async () => {
		// The backend hides Role from creatable_scopes when manageable_roles is
		// empty, so the create form opens on a usable scope and never strands on an
		// empty role picker (same invariant the deleted WikiTab suite pinned).
		getWikiCaps.mockResolvedValueOnce({
			is_sm: false,
			creatable_scopes: ["User", "Org"],
			manageable_roles: [],
		});
		const w = await mountCreate();
		expect(w.vm.roleSelectOptions).toEqual([]);
		expect(w.vm.scopeSelectOptions.map((o) => o.value)).not.toContain("Role");
		expect(w.vm.form.scope).not.toBe("Role");
	});
});
