import { describe, it, expect, afterEach } from "vitest";
import { cardLimitPx } from "./useAdminRowMenu";

// The Admin tabs' styles fold a row into a card at `max-width: 48rem`.
describe("cardLimitPx", () => {
	afterEach(() => {
		document.documentElement.style.fontSize = "";
	});

	it("is 48rem: 768px at a 16px root font size", () => {
		document.documentElement.style.fontSize = "16px";
		expect(cardLimitPx()).toBe(768);
	});

	it("follows the root font size, as rem does", () => {
		document.documentElement.style.fontSize = "20px";
		expect(cardLimitPx()).toBe(960);
		document.documentElement.style.fontSize = "12px";
		expect(cardLimitPx()).toBe(576);
	});
});
