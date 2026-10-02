import { describe, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// A step the save refused is marked where it is: under that step's prompt, not
// in a toast that has gone by the time the owner finds the step.

vi.mock("@/api", () => ({ listCustomSkills: vi.fn(async () => []) }));
vi.mock("frappe-ui", () => ({
	confirmDialog: vi.fn(),
	Button: { props: ["icon", "disabled", "tooltip"], template: "<button />" },
	Autocomplete: { template: "<div />" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "disabled"],
		emits: ["update:modelValue"],
		template: "<div />",
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<div v-if="message" class="error-message">{{ message }}</div>`,
	},
}));

import StepsBuilder from "./StepsBuilder.vue";

const STEPS = [
	{ label: "", prompt: "pull the trial balance", skills: [] },
	{ label: "Post the entries", prompt: "", skills: [] },
	{ label: "", prompt: "mail the summary", skills: [] },
];
const NEEDS_PROMPT = "Add a prompt for this step, or remove the step.";

async function mountBuilder(errors) {
	const w = mount(StepsBuilder, { props: { modelValue: STEPS, ...(errors ? { errors } : {}) } });
	await flushPromises();
	return w;
}
const cards = (w) => w.findAll('[data-testid="macro-step"]');
const promptOf = (card) =>
	card.findAllComponents({ name: "FormControl" }).find((c) => c.props("type") === "textarea");

describe("StepsBuilder: a step the save refused", () => {
	it("shows the message on that step and no other", async () => {
		const w = await mountBuilder({ 1: NEEDS_PROMPT });
		expect(cards(w)).toHaveLength(3);
		expect(cards(w)[1].find(".error-message").text()).toBe(NEEDS_PROMPT);
		expect(cards(w)[0].find(".error-message").exists()).toBe(false);
		expect(cards(w)[2].find(".error-message").exists()).toBe(false);
	});

	it("flags that step's prompt as invalid for assistive tech", async () => {
		const w = await mountBuilder({ 1: NEEDS_PROMPT });
		expect(promptOf(cards(w)[1]).attributes("aria-invalid")).toBe("true");
		expect(promptOf(cards(w)[0]).attributes("aria-invalid")).toBeUndefined();
	});

	it("shows nothing when no step was refused", async () => {
		const w = await mountBuilder();
		expect(w.find(".error-message").exists()).toBe(false);
	});
});
