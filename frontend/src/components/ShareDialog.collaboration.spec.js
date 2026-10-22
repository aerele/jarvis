import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, afterEach } from "vitest";
vi.mock("frappe-ui", () => ({
	Dialog: { template: '<div><slot name="body-content"/><slot name="actions"/></div>' },
	Button: {
		props: ["label", "disabled", "loading"],
		template: '<button :disabled="disabled || loading">{{label}}</button>',
	},
	FeatherIcon: { template: "<i/>" },
	Avatar: { template: "<i/>" },
	toast: { success: vi.fn() },
}));
import ShareDialog from "./ShareDialog.vue";
const wrappers = [];
afterEach(() => wrappers.splice(0).forEach((w) => w.unmount()));
async function open(canConfigure = true) {
	const save = vi.fn(async () => ({}));
	const w = mount(ShareDialog, {
		props: {
			modelValue: true,
			accessModes: true,
			title: "Share chat",
			hint: "Whole chat disclosure",
			loadCandidates: async () => [{ name: "new@example.com", full_name: "New person" }],
			loadShares: async () => ({
				enabled: true,
				can_configure_chat: canConfigure,
				users: [
					{
						name: "old@example.com",
						full_name: "Existing participant",
						access_mode: "Chat",
					},
				],
			}),
			saveShares: save,
		},
	});
	wrappers.push(w);
	await flushPromises();
	return { w, save };
}
const saveButton = (w) => w.findAll("button").find((b) => b.text() === "Save");
describe("shared chat recipient modes", () => {
	it("keeps changes local until Save and sends membership with explicit modes", async () => {
		const { w, save } = await open();
		await w.get('select[aria-label="Access for Existing participant"]').setValue("View");
		expect(save).not.toHaveBeenCalled();
		await saveButton(w).trigger("click");
		await flushPromises();
		expect(save).toHaveBeenCalledWith(["old@example.com"], { "old@example.com": "View" });
	});
	it("adds a new recipient as Can view and preserves the existing Can chat grant", async () => {
		const { w, save } = await open();
		await w
			.findAll("button")
			.find((b) => b.text().includes("New person"))
			.trigger("click");
		expect(w.get('select[aria-label="Access for New person"]').element.value).toBe("View");
		await saveButton(w).trigger("click");
		await flushPromises();
		expect(save).toHaveBeenCalledWith(["old@example.com", "new@example.com"], {
			"old@example.com": "Chat",
			"new@example.com": "View",
		});
	});
	it("allows a safe downgrade while writable sharing is unavailable", async () => {
		const { w, save } = await open(false);
		const select = w.get('select[aria-label="Access for Existing participant"]');
		expect(select.get('option[value="Chat"]').element.disabled).toBe(true);
		await select.setValue("View");
		await saveButton(w).trigger("click");
		await flushPromises();
		expect(save).toHaveBeenCalledWith(["old@example.com"], { "old@example.com": "View" });
	});
});
