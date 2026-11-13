import { afterEach, describe, expect, it } from "vitest";
import { defineComponent, nextTick, ref } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import AccountPicker from "./AccountPicker.vue";
import { provideOverlayStack } from "./overlayStack";

// 60 sample accounts like the template: HDFC Current and Petty Cash first, then Bank 03..Bank 60.
function catalog() {
	const rows = [
		{
			name: "HDFC Current",
			account_name: "HDFC Current",
			bank: "HDFC Bank",
			reference: "•••• 3281",
			account_currency: "INR",
		},
		{
			name: "Petty Cash",
			account_name: "Petty Cash",
			bank: null,
			reference: "Main office",
			account_currency: "INR",
		},
	];
	for (let n = 3; n <= 60; n++) {
		const digits = String(1000 + n * 7).padStart(4, "0");
		rows.push({
			name: `Branch ${String(n).padStart(2, "0")}`,
			account_name: `Branch ${String(n).padStart(2, "0")}`,
			bank: n % 2 ? "ICICI Bank" : "Axis Bank",
			reference: `•••• ${digits}`,
			account_currency: "INR",
		});
	}
	return rows;
}
const DEFAULT = ["HDFC Current", "Petty Cash"];

let wrapper;
afterEach(() => {
	wrapper?.unmount();
	document.body.innerHTML = "";
});
function render(props = {}) {
	wrapper = mount(AccountPicker, {
		props: { open: true, catalog: catalog(), selected: DEFAULT, ...props },
		attachTo: document.body,
	});
	return wrapper;
}
const search = () => wrapper.get("#accounts-search");
const count = () => wrapper.get("#accounts-count").text();
const boxes = () => wrapper.findAll('#accounts-options input[type="checkbox"]');
const checked = () => boxes().filter((box) => box.element.checked);
const apply = async () => {
	await wrapper.get("#accounts-apply").trigger("click");
	return wrapper.emitted("apply")?.at(-1)?.[0];
};

describe("AccountPicker", () => {
	it("renders the template's dialog", async () => {
		render();
		await nextTick();
		const menu = wrapper.get("#accounts-menu");
		expect(menu.attributes("role")).toBe("dialog");
		expect(menu.attributes("aria-label")).toBe("Choose bank and cash accounts");
		expect(menu.get(".account-picker-head").text()).toBe(
			"Bank & cash accountsChoose up to 50 accounts"
		);
		expect(search().attributes("placeholder")).toBe("Search bank, account or last digits…");
		expect(search().attributes("aria-label")).toBe("Search bank and cash accounts");
		expect(count()).toBe("2 of 50 selected · 60 of 60 accounts");
		expect(boxes()).toHaveLength(60);
		expect(wrapper.get("#accounts-cancel").text()).toBe("Cancel");
		expect(wrapper.get("#accounts-apply").text()).toBe("Apply accounts");
		expect(wrapper.get("#accounts-select-matching").text()).toBe("Select matching");
		expect(wrapper.get("#accounts-clear").text()).toBe("Clear");
	});

	it("renders nothing while closed", () => {
		render({ open: false });
		expect(wrapper.find("#accounts-menu").exists()).toBe(false);
	});

	it("lists the name in bold and the bank and reference beneath it", () => {
		render();
		const row = wrapper.get("#accounts-options label");
		expect(row.get("strong").text()).toBe("HDFC Current");
		expect(row.get("small").text()).toBe("HDFC Bank · •••• 3281");
		expect(row.get("input").attributes("aria-label")).toBe("HDFC Current");
		expect(wrapper.findAll("#accounts-options label")[1].get("small").text()).toBe(
			"Main office"
		);
	});

	it("starts with the active accounts ticked", () => {
		render();
		expect(checked().map((box) => box.attributes("aria-label"))).toEqual(DEFAULT);
	});

	it("focuses the search box on open", async () => {
		render({ open: false });
		await wrapper.setProps({ open: true });
		await flushPromises();
		expect(document.activeElement).toBe(search().element);
	});

	it("searches by name, bank and reference digits", async () => {
		render();
		await search().setValue("petty");
		expect(boxes().map((box) => box.attributes("aria-label"))).toEqual(["Petty Cash"]);
		await search().setValue("3281");
		expect(boxes().map((box) => box.attributes("aria-label"))).toEqual(["HDFC Current"]);
		await search().setValue("axis bank");
		expect(boxes().length).toBeGreaterThan(20);
		expect(count()).toBe(`2 of 50 selected · ${boxes().length} of 60 accounts`);
	});

	it("says so when nothing matches", async () => {
		render();
		await search().setValue("zzz");
		expect(wrapper.get("#accounts-options").text()).toBe("No matching accounts.");
		expect(wrapper.get("#accounts-select-matching").attributes("disabled")).toBeDefined();
	});

	it("keeps focus on a checkbox after toggling it", async () => {
		render();
		await flushPromises();
		const box = boxes()[2];
		box.element.focus();
		await box.setValue(true);
		expect(document.activeElement).toBe(boxes()[2].element);
		expect(count()).toBe("3 of 50 selected · 60 of 60 accounts");
	});

	it("applies exactly the ticked accounts in one event", async () => {
		render();
		await boxes()[2].setValue(true);
		await boxes()[0].setValue(false);
		expect(await apply()).toEqual(["Petty Cash", "Branch 03"]);
		expect(wrapper.emitted("apply")).toHaveLength(1);
	});

	it("does not emit apply when the applied set equals the current set", async () => {
		render();
		await wrapper.get("#accounts-apply").trigger("click");
		expect(wrapper.emitted("apply")).toBeUndefined();
		expect(wrapper.emitted("close")).toHaveLength(1);
	});

	it("does not emit apply after a tick that is undone", async () => {
		render();
		await boxes()[2].setValue(true);
		await boxes()[2].setValue(false);
		await wrapper.get("#accounts-apply").trigger("click");
		expect(wrapper.emitted("apply")).toBeUndefined();
		expect(wrapper.emitted("close")).toHaveLength(1);
	});

	it("keeps names that are not in the catalog out of the draft and the count", async () => {
		render({ selected: ["HDFC Current", "Gone Account"] });
		expect(count()).toBe("1 of 50 selected · 60 of 60 accounts");
		await boxes()[1].setValue(true);
		expect(await apply()).toEqual(["HDFC Current", "Petty Cash"]);
	});

	it("does not emit apply when only a stale name differs", async () => {
		render({ selected: ["HDFC Current", "Gone Account"] });
		await wrapper.get("#accounts-apply").trigger("click");
		expect(wrapper.emitted("apply")).toBeUndefined();
	});

	it("discards the draft on Cancel", async () => {
		render();
		await boxes()[2].setValue(true);
		await wrapper.get("#accounts-cancel").trigger("click");
		expect(wrapper.emitted("close")).toHaveLength(1);
		expect(wrapper.emitted("apply")).toBeUndefined();
		await wrapper.setProps({ open: false });
		await wrapper.setProps({ open: true });
		expect(checked().map((box) => box.attributes("aria-label"))).toEqual(DEFAULT);
	});

	it("resets the search when it opens again", async () => {
		render();
		await search().setValue("petty");
		await wrapper.setProps({ open: false });
		await wrapper.setProps({ open: true });
		expect(search().element.value).toBe("");
		expect(boxes()).toHaveLength(60);
	});

	it("selects the matching accounts only", async () => {
		render();
		await search().setValue("icici");
		const matching = boxes().length;
		await wrapper.get("#accounts-select-matching").trigger("click");
		await search().setValue("");
		expect(count()).toBe(`${2 + matching} of 50 selected · 60 of 60 accounts`);
	});

	it("clears the draft and disables Clear when it is empty", async () => {
		render();
		await wrapper.get("#accounts-clear").trigger("click");
		expect(count()).toBe("0 of 50 selected · 60 of 60 accounts");
		expect(wrapper.get("#accounts-clear").attributes("disabled")).toBeDefined();
		expect(await apply()).toEqual([]);
	});

	it("says the company has no accounts, apart from a search with no match", async () => {
		render({ catalog: [], selected: [] });
		expect(wrapper.get("#accounts-options").text()).toBe(
			"This company has no bank or cash accounts you can choose."
		);
		await wrapper.setProps({ catalog: catalog() });
		await search().setValue("zzz");
		expect(wrapper.get("#accounts-options").text()).toBe("No matching accounts.");
	});

	describe("the 50-account limit", () => {
		it("refuses Select matching when the matches would pass 50, and explains why", async () => {
			render({ selected: [] });
			// 60 match but only 50 fit: the button is disabled and its title explains why
			expect(wrapper.get("#accounts-select-matching").attributes("disabled")).toBeDefined();
			expect(wrapper.get("#accounts-select-matching").attributes("title")).toBe(
				"Narrow the search to stay within the 50-account selection limit."
			);
			expect(count()).toBe("0 of 50 selected · 60 of 60 accounts");
			// the reason is also visible text, not only a tooltip
			expect(wrapper.get("#accounts-limit-note").text()).toBe(
				"Narrow the search to stay within the 50-account selection limit."
			);
			await search().setValue("Branch 0");
			expect(wrapper.find("#accounts-limit-note").exists()).toBe(false);
			await wrapper.get("#accounts-select-matching").trigger("click");
			expect(checked()).toHaveLength(7);
		});

		it("disables unticked boxes once 50 are selected, and lifts it on untick", async () => {
			const names = catalog()
				.slice(0, 50)
				.map((account) => account.name);
			render({ selected: names });
			expect(count()).toBe("50 of 50 selected · 60 of 60 accounts");
			const unticked = boxes().filter((box) => !box.element.checked);
			expect(unticked).toHaveLength(10);
			expect(unticked.every((box) => box.attributes("disabled") !== undefined)).toBe(true);
			expect(
				boxes()
					.filter((box) => box.element.checked)
					.every((box) => box.attributes("disabled") === undefined)
			).toBe(true);
			await boxes()[0].setValue(false);
			expect(
				boxes()
					.filter((box) => !box.element.checked)
					.every((box) => box.attributes("disabled") === undefined)
			).toBe(true);
		});

		it("never applies more than 50", async () => {
			const names = catalog()
				.slice(0, 50)
				.map((account) => account.name);
			render({ selected: names });
			// swap one account for another: the applied set is still 50
			await boxes()[0].setValue(false);
			await boxes()[50].setValue(true);
			expect((await apply()).length).toBe(50);
		});

		it("accepts a search that fits the remaining room", async () => {
			render({ selected: DEFAULT });
			await search().setValue("Branch 1");
			expect(
				wrapper.get("#accounts-select-matching").attributes("disabled")
			).toBeUndefined();
			expect(wrapper.get("#accounts-select-matching").attributes("title") || "").toBe("");
		});
	});

	describe("Escape through the page's overlay stack", () => {
		const Host = defineComponent({
			components: { AccountPicker },
			setup() {
				provideOverlayStack();
				return { open: ref(true), catalog: catalog(), closed: ref(0) };
			},
			template: `<AccountPicker :open="open" :catalog="catalog" :selected="[]" @close="closed++; open = false" />`,
		});
		it("closes the picker like Cancel", async () => {
			wrapper = mount(Host, { attachTo: document.body });
			await nextTick();
			document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
			await nextTick();
			expect(wrapper.vm.closed).toBe(1);
			expect(wrapper.find("#accounts-menu").exists()).toBe(false);
		});
	});
});
