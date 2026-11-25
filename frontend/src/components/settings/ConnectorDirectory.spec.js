import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("@/components/settings/ConnectorLogo.vue", () => ({
	default: { name: "ConnectorLogo", template: `<span />` },
}));
vi.mock("frappe-ui", () => ({
	Badge: { name: "Badge", props: ["label"], template: `<span class="badge">{{ label }}</span>` },
	FeatherIcon: { name: "FeatherIcon", template: `<i />` },
	FormControl: {
		name: "FormControl",
		props: ["modelValue"],
		emits: ["update:modelValue"],
		template: `<input class="search" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Button: {
		name: "Button",
		props: ["label", "disabled"],
		emits: ["click"],
		template: `<button :data-label="label" :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
	},
}));

import ConnectorDirectory from "./ConnectorDirectory.vue";

const catalog = [
	{ name: "Linear", auth: "dcr", category: "work", description: "Issues", coming_soon: true },
	{ name: "GitHub", auth: "static", category: "dev", description: "Repos", coming_soon: false },
	{ name: "Stripe", auth: "token", category: "payments", description: "Payments" },
];

const mountIt = () => mount(ConnectorDirectory, { props: { catalog } });
const cards = (w) => w.findAll("div.rounded-lg");
const card = (w, name) => cards(w).find((c) => c.text().includes(name));
const addButtons = (w) => w.findAll('button[data-label="Add"]');

describe("ConnectorDirectory coming soon", () => {
	it("shows a Coming soon badge and no Add control on a coming-soon card", async () => {
		const w = mountIt();
		const soon = card(w, "Linear");
		expect(soon.find(".badge").text()).toBe("Coming soon");
		expect(soon.find("button").exists()).toBe(false);
		expect(soon.attributes("aria-label")).toBe("Linear, coming soon");
		expect(soon.classes()).toContain("opacity-50");
	});

	it("opens nothing when a coming-soon card is clicked", async () => {
		const w = mountIt();
		await card(w, "Linear").trigger("click");
		expect(w.emitted("add")).toBeUndefined();
	});

	it("leaves available cards unchanged", async () => {
		const w = mountIt();
		const github = card(w, "GitHub");
		expect(github.find(".badge").text()).toBe("Needs setup");
		expect(github.attributes("aria-label")).toBeUndefined();
		expect(github.classes()).not.toContain("opacity-50");
		expect(addButtons(w)).toHaveLength(2);
		await github.find('button[data-label="Add"]').trigger("click");
		expect(w.emitted("add")).toEqual([["GitHub"]]);
	});

	it("lists coming-soon cards after available ones", () => {
		const names = cards(mountIt()).map((c) => c.find("span.truncate").text());
		expect(names).toEqual(["GitHub", "Stripe", "Linear"]);
	});

	it("keeps a coming-soon card in its category and in search results", async () => {
		const w = mountIt();
		await w.find('button[data-label="Work"]').trigger("click");
		expect(cards(w).map((c) => c.find("span.truncate").text())).toEqual(["Linear"]);
		await w.find('button[data-label="All"]').trigger("click");
		await w.find(".search").setValue("lin");
		expect(cards(w).map((c) => c.find("span.truncate").text())).toEqual(["Linear"]);
	});

	it("never shows Added on a coming-soon card", () => {
		const w = mount(ConnectorDirectory, {
			props: { catalog, installedRows: [{ preset: "Linear", scope: "Personal" }] },
		});
		expect(card(w, "Linear").text()).not.toContain("Added");
	});
});
