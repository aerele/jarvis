import { describe, expect, it } from "vitest";
import {
	fromPickerDatetime,
	inputAttrsFor,
	inputClassesFor,
	toDate,
	toPickerDatetime,
	toTime,
} from "./dateControl";

// DateControl (#833) swaps the native inputs for frappe-ui pickers but must speak the
// strings those inputs did, or the card's dirty check and save path would change.
describe("DateControl value adapters", () => {
	it("date stays YYYY-MM-DD", () => {
		expect(toDate("2026-11-10")).toBe("2026-11-10");
		expect(toDate("2026-11-10 00:00:00")).toBe("2026-11-10");
		expect(toDate("")).toBe("");
		expect(toDate(null)).toBe("");
	});
	it("time stays HH:mm, seconds dropped", () => {
		expect(toTime("09:30")).toBe("09:30");
		expect(toTime("09:30:15")).toBe("09:30");
		expect(toTime("")).toBe("");
	});
	it("datetime goes to the picker as YYYY-MM-DD HH:mm:ss", () => {
		expect(toPickerDatetime("2026-11-10T14:30")).toBe("2026-11-10 14:30:00");
		expect(toPickerDatetime("2026-11-10 14:30:45")).toBe("2026-11-10 14:30:45");
		expect(toPickerDatetime("2026-11-10")).toBe("2026-11-10 00:00:00");
		expect(toPickerDatetime("garbage")).toBe("");
	});
	it("datetime comes back as the card's YYYY-MM-DDTHH:mm", () => {
		expect(fromPickerDatetime("2026-11-10 14:30:00")).toBe("2026-11-10T14:30");
		expect(fromPickerDatetime("")).toBe("");
	});
});

describe("inputAttrsFor", () => {
	it("takes id and aria-* from the caller, the aria-label prop first; blank is removed", () => {
		expect(
			inputAttrsFor(
				{ id: "f", "aria-label": "attr", "aria-describedby": "", "aria-required": "true" },
				"Prop label"
			)
		).toEqual({
			id: "f",
			"aria-label": "Prop label",
			"aria-describedby": null,
			"aria-required": "true",
		});
		expect(inputAttrsFor({}, "")).toEqual({
			id: null,
			"aria-label": null,
			"aria-describedby": null,
			"aria-required": null,
		});
	});
});

describe("inputClassesFor", () => {
	it("is the look's input class plus the extra ones, de-duplicated", () => {
		expect(inputClassesFor("card", "")).toEqual(["jv-action-input"]);
		expect(inputClassesFor("ask", "a  b jv-ask-field")).toEqual(["jv-ask-field", "a", "b"]);
	});
});
