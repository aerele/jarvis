import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Skills page's tab strip and hash. What matters for the Admin tab: it is offered
 * only to the people the caps probe calls `analysis` (Jarvis Admin, System Manager);
 * `#admin` opens it for them; for anyone else `#admin` lands on Skills with the hash
 * cleared; the strip shows when Admin is the only extra tab.
 */

vi.mock("frappe-ui", () => ({
	Badge: { props: ["label"], template: `<span class="badge">{{ label }}</span>` },
}));

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
// The route is reactive, as the real one: the page watches its hash.
vi.mock("vue-router", async () => {
	const { reactive } = await import("vue");
	const route = reactive({ name: "SkillsList", hash: "", query: {} });
	return { useRoute: () => route, useRouter: () => router, route };
});

const caps = vi.hoisted(() => ({ getSkillsAreaCaps: vi.fn() }));
vi.mock("@/api/personalise", () => caps);
vi.mock("@/stores/shell", () => ({
	useShellStore: () => ({ reviewCount: 0, refreshReviewCount: vi.fn() }),
}));
vi.mock("wiki-graph-core", () => ({ renderer3dEnabled: () => false }));
vi.mock("@/pages/wiki/KnowledgeGraph.vue", () => ({ default: { template: "<i />" } }));

const tab = vi.hoisted(
	() => (name) => ({ default: { name, template: `<div class="tab-${name}" />` } })
);
vi.mock("./SkillsList.vue", () => tab("skills"));
vi.mock("./AnalysisTab.vue", () => tab("analysis"));
vi.mock("./ReviewTab.vue", () => tab("review"));
vi.mock("./PersonaliseTab.vue", () => tab("personalise"));
vi.mock("./WikiTab.vue", () => tab("wiki"));
vi.mock("./AdminTab.vue", () => tab("admin"));

import { route } from "vue-router";
import SkillsPage from "./SkillsPage.vue";

// Every page watches the one shared route, so each is unmounted after its test.
const mounted = [];
function mountPage() {
	const w = mount(SkillsPage);
	mounted.push(w);
	return w;
}
async function mountAs(flags, hash = "") {
	route.hash = hash;
	caps.getSkillsAreaCaps.mockResolvedValue(flags);
	const w = mountPage();
	await flushPromises();
	return w;
}
const labels = (w) => w.findAll('[role="tab"]').map((t) => t.text());
const body = (w) => ["skills", "analysis", "review", "personalise", "wiki", "admin"].filter((n) => w.find(`.tab-${n}`).exists());

beforeEach(() => {
	vi.clearAllMocks();
	route.hash = "";
});
afterEach(() => {
	for (const w of mounted.splice(0)) w.unmount();
});

describe("SkillsPage, the Admin tab", () => {
	it("is offered to an administrator, after Analysis and Review", async () => {
		const w = await mountAs({ analysis: true, review: true });
		expect(labels(w)).toEqual(["Skills", "Analysis", "Review", "Admin"]);
	});

	it("is not offered to a reviewer who is not an administrator", async () => {
		const w = await mountAs({ review: true });
		expect(labels(w)).toEqual(["Skills", "Review"]);
	});

	it("is not offered to anyone the probe gives nothing", async () => {
		const w = await mountAs({});
		expect(w.find('[role="tablist"]').exists()).toBe(false);
	});

	it("opens on #admin for an administrator", async () => {
		const w = await mountAs({ analysis: true }, "#admin");
		expect(body(w)).toEqual(["admin"]);
		expect(router.replace).not.toHaveBeenCalled();
	});

	it("sends a non-administrator on #admin to Skills and clears the hash", async () => {
		const w = await mountAs({ review: true }, "#admin");
		expect(body(w)).toEqual(["skills"]);
		expect(router.replace).toHaveBeenCalledWith({ hash: "", query: {} });
	});

	it("does the same for someone with no tabs at all", async () => {
		const w = await mountAs({}, "#admin");
		expect(body(w)).toEqual(["skills"]);
		expect(router.replace).toHaveBeenCalledWith({ hash: "", query: {} });
	});

	it("does not clear the hash before the probe has answered", async () => {
		let answer;
		caps.getSkillsAreaCaps.mockReturnValue(new Promise((r) => (answer = r)));
		route.hash = "#admin";
		const w = mountPage();
		await flushPromises();
		expect(router.replace).not.toHaveBeenCalled();
		answer({ analysis: true });
		await flushPromises();
		expect(body(w)).toEqual(["admin"]);
	});

	it("puts #admin in the address when the tab is clicked, and takes it out for Skills", async () => {
		const w = await mountAs({ analysis: true });
		await w.findAll('[role="tab"]').find((t) => t.text() === "Admin").trigger("click");
		expect(body(w)).toEqual(["admin"]);
		expect(router.push).toHaveBeenCalledWith({ hash: "#admin", query: {} });
		await w.findAll('[role="tab"]').find((t) => t.text() === "Skills").trigger("click");
		expect(router.push).toHaveBeenLastCalledWith({ hash: "", query: {} });
	});

	it("follows back and forward onto and off #admin", async () => {
		const w = await mountAs({ analysis: true });
		route.hash = "#admin";
		await flushPromises();
		expect(body(w)).toEqual(["admin"]);
		route.hash = "";
		await flushPromises();
		expect(body(w)).toEqual(["skills"]);
	});
});
