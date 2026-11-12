import { afterEach, describe, expect, it, vi } from "vitest";
import { defineComponent, h, nextTick } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import CalendarFilterBar from "./CalendarFilterBar.vue";
import { provideOverlayStack } from "./overlayStack";

const COMPANIES = [
	{ name: "Northstar Manufacturing Pvt Ltd" },
	{ name: "Northstar Services Pvt Ltd" },
];
const ACCOUNTS = [
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
	{
		name: "ICICI Savings",
		account_name: "ICICI Savings",
		bank: "ICICI Bank",
		reference: "•••• 9920",
		account_currency: "INR",
	},
	{
		name: "Axis Payroll",
		account_name: "Axis Payroll",
		bank: "Axis Bank",
		reference: "•••• 0417",
		account_currency: "INR",
	},
	{
		name: "SBI Escrow",
		account_name: "SBI Escrow",
		bank: "SBI",
		reference: "•••• 7755",
		account_currency: "INR",
	},
];
const CAPS = {
	companies: COMPANIES,
	accounts: ACCOUNTS,
	can_configure: true,
	settings: {
		configured: true,
		currency: "INR",
		safety_floor: "50000.00",
		revision: 1,
		configuration_id: "f",
	},
};

let wrapper;
afterEach(() => {
	wrapper?.unmount();
	document.body.innerHTML = "";
});
function render(props = {}) {
	const saveFloor = vi.fn(async () => ({ ok: true }));
	const Host = defineComponent({
		setup() {
			provideOverlayStack();
			return () =>
				h(CalendarFilterBar, {
					companies: COMPANIES,
					company: COMPANIES[0].name,
					caps: CAPS,
					accounts: ["HDFC Current", "Petty Cash"],
					companyDisabled: false,
					floorSaving: false,
					floorError: null,
					saveFloor,
					...props,
				});
		},
	});
	wrapper = mount(Host, { attachTo: document.body });
	return { saveFloor, bar: () => wrapper.findComponent(CalendarFilterBar) };
}
const chips = () => wrapper.findAll("#account-chips .account-chip");
const emitted = (bar, name) => bar().emitted(name);

describe("CalendarFilterBar", () => {
	it("lays out company, accounts and minimum as the template's filter bar", () => {
		render();
		const bar = wrapper.get("section.filters");
		expect(bar.attributes("aria-label")).toBe("Calendar filters");
		const cells = [...bar.element.children].map((cell) => cell.className);
		expect(cells).toEqual([
			"filter company-filter",
			"filter accounts-filter",
			"filter floor-filter",
		]);
		expect(wrapper.get(".accounts-filter .field-label").text()).toBe("Bank & cash accounts");
	});

	describe("account chips", () => {
		it("shows the selected accounts as removable chips", () => {
			render();
			expect(chips().map((chip) => chip.text())).toEqual(["HDFC Current×", "Petty Cash×"]);
			expect(chips()[0].attributes("aria-label")).toBe("Remove HDFC Current");
			expect(wrapper.get("#account-chips").attributes("aria-label")).toBe(
				"Selected bank and cash accounts"
			);
			expect(wrapper.find("#accounts-overflow").exists()).toBe(false);
		});

		it("shows the first two in catalog order and a +n selected chip", () => {
			render({ accounts: ["SBI Escrow", "ICICI Savings", "HDFC Current", "Axis Payroll"] });
			expect(chips().map((chip) => chip.text())).toEqual([
				"HDFC Current×",
				"ICICI Savings×",
				"+2 selected",
			]);
		});

		it("says so when no account is selected", () => {
			render({ accounts: [] });
			expect(chips()).toHaveLength(0);
			expect(wrapper.get("#account-chips").text()).toBe("No accounts selected");
		});

		it("removes an account at once from its chip, with no picker", async () => {
			const { bar } = render();
			await chips()[0].trigger("click");
			expect(emitted(bar, "set-accounts")).toEqual([[["Petty Cash"]]]);
			expect(wrapper.find("#accounts-menu").exists()).toBe(false);
		});

		it("moves focus to the Choose accounts button after a chip is removed", async () => {
			render();
			chips()[0].element.focus();
			await chips()[0].trigger("click");
			await flushPromises();
			expect(document.activeElement).toBe(wrapper.get("#accounts-trigger").element);
		});

		it("keeps every other selected account when a chip is removed", async () => {
			const { bar } = render({ accounts: ["HDFC Current", "Petty Cash", "ICICI Savings"] });
			await chips()[1].trigger("click");
			expect(emitted(bar, "set-accounts")).toEqual([[["HDFC Current", "ICICI Savings"]]]);
		});

		it("opens the picker from the overflow chip", async () => {
			render({ accounts: ["HDFC Current", "Petty Cash", "ICICI Savings"] });
			await wrapper.get("#accounts-overflow").trigger("click");
			expect(wrapper.find("#accounts-menu").exists()).toBe(true);
		});
	});

	describe("account picker", () => {
		it("opens from the trigger with the catalog and the active selection", async () => {
			render();
			const trigger = wrapper.get("#accounts-trigger");
			expect(trigger.text()).toBe("Choose accounts");
			expect(trigger.attributes("aria-haspopup")).toBe("dialog");
			expect(trigger.attributes("aria-expanded")).toBe("false");
			await trigger.trigger("click");
			expect(trigger.attributes("aria-expanded")).toBe("true");
			expect(wrapper.get("#accounts-count").text()).toBe(
				"2 of 50 selected · 5 of 5 accounts"
			);
		});

		it("is not available until the company's catalog has loaded", () => {
			render({ caps: null, accounts: [] });
			expect(wrapper.get("#accounts-trigger").attributes("disabled")).toBeDefined();
		});

		it("applies the draft as one change and returns focus to the trigger", async () => {
			const { bar } = render();
			await wrapper.get("#accounts-trigger").trigger("click");
			await wrapper.findAll("#accounts-options input")[2].setValue(true);
			await wrapper.get("#accounts-apply").trigger("click");
			await flushPromises();
			expect(emitted(bar, "set-accounts")).toEqual([
				[["HDFC Current", "Petty Cash", "ICICI Savings"]],
			]);
			expect(wrapper.find("#accounts-menu").exists()).toBe(false);
			expect(document.activeElement).toBe(wrapper.get("#accounts-trigger").element);
		});

		it("discards the draft on Cancel and on Escape", async () => {
			const { bar } = render();
			await wrapper.get("#accounts-trigger").trigger("click");
			await wrapper.findAll("#accounts-options input")[2].setValue(true);
			await wrapper.get("#accounts-cancel").trigger("click");
			await flushPromises();
			expect(document.activeElement).toBe(wrapper.get("#accounts-trigger").element);
			await wrapper.get("#accounts-trigger").trigger("click");
			await flushPromises();
			document.activeElement.dispatchEvent(
				new KeyboardEvent("keydown", { key: "Escape", bubbles: true })
			);
			await flushPromises();
			expect(wrapper.find("#accounts-menu").exists()).toBe(false);
			expect(document.activeElement).toBe(wrapper.get("#accounts-trigger").element);
			expect(emitted(bar, "set-accounts")).toBeUndefined();
		});

		it("closes on a pointerdown outside the accounts filter, not inside it", async () => {
			render();
			await wrapper.get("#accounts-trigger").trigger("click");
			wrapper
				.get("#accounts-search")
				.element.dispatchEvent(new Event("pointerdown", { bubbles: true }));
			await nextTick();
			expect(wrapper.find("#accounts-menu").exists()).toBe(true);
			wrapper
				.get("#company-trigger")
				.element.dispatchEvent(new Event("pointerdown", { bubbles: true }));
			await nextTick();
			expect(wrapper.find("#accounts-menu").exists()).toBe(false);
		});
	});

	describe("company menu", () => {
		it("lets only one menu be open at a time", async () => {
			render();
			await wrapper.get("#accounts-trigger").trigger("click");
			await wrapper.get("#company-trigger").trigger("click");
			expect(wrapper.find("#accounts-menu").exists()).toBe(false);
			expect(wrapper.find("#company-menu").exists()).toBe(true);
			await wrapper.get("#accounts-trigger").trigger("click");
			expect(wrapper.find("#company-menu").exists()).toBe(false);
			expect(wrapper.find("#accounts-menu").exists()).toBe(true);
		});

		it("emits the chosen company, and nothing when the same one is chosen again", async () => {
			const { bar } = render();
			await wrapper.get("#company-trigger").trigger("click");
			await wrapper.findAll('#company-options [role="option"]')[0].trigger("click");
			expect(emitted(bar, "choose-company")).toBeUndefined();
			await wrapper.get("#company-trigger").trigger("click");
			await wrapper.findAll('#company-options [role="option"]')[1].trigger("click");
			expect(emitted(bar, "choose-company")).toEqual([["Northstar Services Pvt Ltd"]]);
		});

		it("is disabled while companies load", () => {
			render({ companyDisabled: true });
			expect(wrapper.get("#company-trigger").attributes("disabled")).toBeDefined();
		});
	});

	describe("minimum cash balance", () => {
		it("passes Save to the core's saveFloor", async () => {
			const { saveFloor } = render();
			await wrapper.get("#floor-input").setValue("75000");
			await wrapper.get("#floor-save").trigger("click");
			expect(saveFloor).toHaveBeenCalledWith("75000");
		});

		it("is read-only when the person cannot configure the company", () => {
			render({ caps: { ...CAPS, can_configure: false } });
			expect(wrapper.get("#floor-input").attributes("readonly")).toBeDefined();
			expect(wrapper.find("#floor-save").exists()).toBe(false);
		});

		it("shows a refusal under the field", () => {
			render({ floorError: { code: "settings_changed" } });
			expect(wrapper.get("#floor-error").text()).toContain("changed by another user");
		});
	});
});
