import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Dialog: {
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		// Exposes a `close` button standing in for Dialog's own Escape / outside
		// -click / X-button close paths, none of which are driven by the
		// consumer's "Maybe later" button - they all funnel through Dialog's
		// internal close(), which only ever emits update:modelValue (see real
		// Dialog.vue). A plain v-model here would bypass closePulseFeedback().
		template:
			"<div><slot name='body-content'/><slot name='actions'/><button data-test='dialog-escape' @click=\"$emit('update:modelValue', false)\">escape</button></div>",
	},
	Button: {
		props: ["label", "variant", "disabled"],
		emits: ["click"],
		template: "<button :disabled='disabled' @click=\"$emit('click')\">{{ label }}</button>",
	},
	FormControl: {
		props: ["modelValue", "type", "rows"],
		emits: ["update:modelValue"],
		template:
			"<textarea :value='modelValue' @input=\"$emit('update:modelValue', $event.target.value)\"/>",
	},
}));

vi.mock("@/branding", () => ({ brand: { agentName: "Jarvis" } }));

vi.mock("@/api", () => ({
	submitPulseFeedback: vi.fn().mockResolvedValue({ ok: true }),
}));

// Real gate behavior, but with closePulseFeedback spied so a test can tell
// "went through the one dismiss path" apart from "the ref just happened to
// end up false" -- both look identical if you only check pulseFeedbackOpen.
vi.mock("@/lib/pulseFeedbackGate", async (importOriginal) => {
	const actual = await importOriginal();
	return { ...actual, closePulseFeedback: vi.fn(actual.closePulseFeedback) };
});

import * as api from "@/api";
import PulseFeedbackDialog from "./PulseFeedbackDialog.vue";
import {
	pulseFeedbackOpen,
	pulseFeedbackContext,
	closePulseFeedback,
} from "@/lib/pulseFeedbackGate";

describe("PulseFeedbackDialog", () => {
	beforeEach(() => {
		pulseFeedbackOpen.value = false;
		pulseFeedbackContext.value = null;
		vi.clearAllMocks();
	});

	it("hides the chip question entirely when features_offered is empty", () => {
		pulseFeedbackContext.value = {
			period_label: "This month, Sep 2026",
			features_offered: [],
		};
		pulseFeedbackOpen.value = true;
		const w = mount(PulseFeedbackDialog);
		expect(w.text()).not.toContain("Which do you use most?");
	});

	it("renders chips with voice_chat mapped to Notes and dictation, not Voice chat", () => {
		pulseFeedbackContext.value = {
			period_label: "This month, Sep 2026",
			features_offered: ["file_box", "voice_chat", "skills"],
		};
		pulseFeedbackOpen.value = true;
		const w = mount(PulseFeedbackDialog);
		expect(w.text()).toContain("Which do you use most?");
		expect(w.text()).toContain("Notes and dictation");
		expect(w.text()).not.toContain("Voice chat");
		expect(w.text()).toContain("File box");
		expect(w.text()).toContain("Skills");
	});

	it("Send is disabled until a star is picked, then submits with offered+selected", async () => {
		pulseFeedbackContext.value = {
			period_label: "This month",
			features_offered: ["file_box"],
		};
		pulseFeedbackOpen.value = true;
		const w = mount(PulseFeedbackDialog);
		const sendBtn = () => w.findAll("button").find((b) => b.text() === "Send feedback");
		expect(sendBtn().attributes("disabled")).toBeDefined();

		const stars = w.findAll("button").filter((b) => b.text() === "★");
		await stars[2].trigger("click"); // 3rd star = 3 stars
		expect(sendBtn().attributes("disabled")).toBeUndefined();

		await w
			.findAll("button")
			.find((b) => b.text() === "File box")
			.trigger("click");
		await sendBtn().trigger("click");
		expect(api.submitPulseFeedback).toHaveBeenCalledWith(
			3,
			["file_box"],
			["file_box"],
			"",
			""
		);
	});

	it("asks about last month when the context says the previous month", () => {
		pulseFeedbackContext.value = {
			period_label: "Last month, Sep 2026",
			period_is_previous: true,
			features_offered: ["file_box"],
		};
		pulseFeedbackOpen.value = true;
		const w = mount(PulseFeedbackDialog);
		expect(w.text()).toContain("Last month, Sep 2026");
		expect(w.text()).toContain("how satisfied were you last month?");
		expect(w.text()).toContain("only what you used last month");
		expect(w.text()).not.toContain("this month");
	});

	it("keeps the this-month wording otherwise", () => {
		pulseFeedbackContext.value = {
			period_label: "This month, Oct 2026",
			period_is_previous: false,
			features_offered: ["file_box"],
		};
		pulseFeedbackOpen.value = true;
		const w = mount(PulseFeedbackDialog);
		expect(w.text()).toContain("how satisfied are you this month?");
		expect(w.text()).toContain("only what you've used this month");
	});

	it("Maybe later closes without submitting", async () => {
		pulseFeedbackContext.value = { period_label: "This month", features_offered: [] };
		pulseFeedbackOpen.value = true;
		const w = mount(PulseFeedbackDialog);
		await w
			.findAll("button")
			.find((b) => b.text() === "Maybe later")
			.trigger("click");
		expect(api.submitPulseFeedback).not.toHaveBeenCalled();
		expect(pulseFeedbackOpen.value).toBe(false);
	});

	it("Escape/outside-click/X (Dialog's own close, not the Maybe later button) still counts as a dismissal", async () => {
		// Dialog drives these three itself and only ever emits
		// update:modelValue(false) - never a click on "Maybe later". Before this
		// fix, a plain v-model wrote pulseFeedbackOpen.value = false directly on
		// that emit, skipping closePulseFeedback() entirely, so the "one offer
		// per page load" rule silently didn't apply to these three closes.
		pulseFeedbackContext.value = { period_label: "This month", features_offered: [] };
		pulseFeedbackOpen.value = true;
		const w = mount(PulseFeedbackDialog);
		await w.find("[data-test='dialog-escape']").trigger("click");
		expect(pulseFeedbackOpen.value).toBe(false);
		expect(closePulseFeedback).toHaveBeenCalledTimes(1);
	});
});
