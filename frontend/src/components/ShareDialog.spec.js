import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, afterEach } from "vitest";
vi.mock("frappe-ui", () => ({
	Dialog: {
		props: ["modelValue", "options"],
		template:
			'<div v-if="modelValue"><h1>{{options.title}}</h1><slot name="body-content"/><slot name="actions"/></div>',
	},
	Button: {
		props: ["label", "disabled", "loading"],
		template: '<button :disabled="disabled || loading"><slot/>{{label}}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	Avatar: { template: "<i/>" },
	toast: { success: vi.fn(), error: vi.fn() },
}));
import ShareDialog from "./ShareDialog.vue";
const wrappers = [];
afterEach(() => {
	wrappers.splice(0).forEach((w) => w.unmount());
	vi.useRealTimers();
});
async function dialog(extra = {}) {
	const props = {
		modelValue: true,
		title: "Share chat",
		hint: "Whole chat warning",
		loadCandidates: vi.fn(async () => [{ name: "new@example.com", full_name: "New person" }]),
		loadShares: vi.fn(async () => ({
			enabled: true,
			users: [
				{
					name: "old@example.com",
					full_name: "Old person",
					hidden: true,
					hidden_reason: "recipient_disabled",
				},
			],
		})),
		saveShares: vi.fn(async () => ({ enabled: true, users: [] })),
		...extra,
	};
	const w = mount(ShareDialog, { props });
	wrappers.push(w);
	await flushPromises();
	return { w, props };
}
describe("generic ShareDialog", () => {
	it("shows a refused recipient limit while preserving the selected people", async () => {
		const { w } = await dialog({
			saveShares: vi.fn(async () => {
				throw Object.assign(new Error("A chat can have at most 25 visible recipients."), {
					status: 417,
					exc_type: "ValidationError",
				});
			}),
		});
		await w
			.findAll("button")
			.find((b) => b.text() === "Save")
			.trigger("click");
		await flushPromises();
		expect(w.text()).toContain("25 visible recipients");
		expect(w.text()).toContain("Old person");
		expect(w.emitted("saved")).toBeUndefined();
	});

	it("loads on initial open and retains hidden recipients outside search results", async () => {
		const { w, props } = await dialog();
		expect(props.loadShares).toHaveBeenCalledOnce();
		expect(w.text()).toContain("Whole chat warning");
		expect(w.text()).toContain("Old person");
		expect(w.text()).toContain("disabled");
		await w.get('[aria-label="Remove Old person"]').trigger("click");
		await w
			.findAll("button")
			.find((b) => b.text() === "Save")
			.trigger("click");
		await flushPromises();
		expect(props.saveShares).toHaveBeenCalledWith([]);
		expect(w.emitted("saved")).toHaveLength(1);
	});
	it("allows only removal when sharing is disabled", async () => {
		const { w, props } = await dialog({
			loadShares: vi.fn(async () => ({
				enabled: false,
				users: [{ name: "old@example.com", full_name: "Old person" }],
			})),
		});
		expect(w.text()).toContain("Sharing is turned off on this site");
		expect(
			w
				.findAll("button")
				.find((b) => b.text().includes("New person"))
				?.attributes("disabled")
		).toBeDefined();
		await w.get('[aria-label="Remove Old person"]').trigger("click");
		await w
			.findAll("button")
			.find((b) => b.text() === "Save")
			.trigger("click");
		expect(props.saveShares).toHaveBeenCalledWith([]);
	});
	it("keeps selection and dialog open when save fails", async () => {
		const { w } = await dialog({
			saveShares: vi.fn(async () => {
				throw new Error("Network unavailable");
			}),
		});
		await w
			.findAll("button")
			.find((b) => b.text() === "Save")
			.trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Old person");
		expect(w.emitted("update:modelValue")).toBeUndefined();
		expect(w.text()).toContain("Couldn't update sharing");
	});
	it("ignores a stale search result", async () => {
		let finish;
		const loadCandidates = vi.fn(async (q) =>
			q === "old"
				? new Promise((r) => (finish = r))
				: [{ name: q || "new@example.com", full_name: q || "New person" }]
		);
		const { w } = await dialog({ loadCandidates });
		vi.useFakeTimers();
		await w.get('input[type="search"]').setValue("old");
		await vi.advanceTimersByTimeAsync(250);
		await w.get('input[type="search"]').setValue("latest");
		await vi.advanceTimersByTimeAsync(250);
		await flushPromises();
		finish([{ name: "stale", full_name: "Stale result" }]);
		await flushPromises();
		expect(w.text()).toContain("latest");
		expect(w.text()).not.toContain("Stale result");
	});
});
