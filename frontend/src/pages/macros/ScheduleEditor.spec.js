import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

/**
 * jarvis-admin-v2#801: a macro can run on several cron schedules (one per line).
 * An example chip ADDS its line to the cron box; it used to replace what was there,
 * so a second schedule could not be built from the examples.
 */
vi.mock("frappe-ui", async () => ({
	Button: { name: "Button", props: ["label"], template: "<button>{{ label }}</button>" },
	DatePicker: { name: "DatePicker", template: "<div />" },
	ErrorMessage: { name: "ErrorMessage", props: ["message"], template: "<div />" },
	TabButtons: { name: "TabButtons", props: ["buttons", "modelValue"], template: "<div />" },
	TextInput: { name: "TextInput", template: "<input />" },
	// the real dayjs with the tz plugin: the editor previews the next runs
	dayjs: (await import("./testDayjs.js")).default,
}));

import ScheduleEditor from "./ScheduleEditor.vue";

const draft = (cron) => ({
	frequency: "custom",
	times: [{ h: "9", m: "00", ap: "AM" }],
	days: [1],
	dom: [1],
	everyN: "1",
	hourlyMin: "00",
	cron,
});

function mountWith(cron) {
	return mount(ScheduleEditor, { props: { modelValue: draft(cron) } });
}
const chip = (w, label) => w.findAll("button.jv-sched-example").find((b) => b.text() === label);
const lastCron = (w) => w.emitted("update:modelValue").at(-1)[0].cron;

describe("ScheduleEditor cron examples (#801)", () => {
	it("an example adds its line under the ones already written", async () => {
		const w = mountWith("0 9 * * 1-5");
		await chip(w, "Fridays at 5:30 pm").trigger("click");
		expect(lastCron(w)).toBe("0 9 * * 1-5\n30 17 * * 5");
	});

	it("an example already in the box shows as picked and adds nothing", async () => {
		const w = mountWith("0 9 * * 1-5");
		const picked = chip(w, "Weekdays at 9 am");
		expect(picked.attributes("aria-pressed")).toBe("true");
		expect(chip(w, "Fridays at 5:30 pm").attributes("aria-pressed")).toBe("false");
		await picked.trigger("click");
		expect(lastCron(w)).toBe("0 9 * * 1-5");
	});

	it("a full box disables the examples it does not hold", () => {
		const full = Array.from({ length: 6 }, (_, i) => `${i} 8 * * *`).join("\n");
		const w = mountWith(full);
		expect(chip(w, "Fridays at 5:30 pm").attributes("disabled")).toBeDefined();
	});

	it("says a schedule can have several lines", () => {
		expect(mountWith("").text()).toContain("One schedule per line, up to 6");
	});
});
