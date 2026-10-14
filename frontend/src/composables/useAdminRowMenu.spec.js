import { describe, it, expect, afterEach } from "vitest";
import { isCardWidth } from "./useAdminRowMenu";

// The Admin tabs' styles fold a row into a card at `max-width: 48rem`.
describe("isCardWidth", () => {
	afterEach(() => {
		document.documentElement.style.fontSize = "";
	});

	it("counts 48rem as a card, with the limit included", () => {
		document.documentElement.style.fontSize = "16px";
		expect(isCardWidth(767)).toBe(true);
		expect(isCardWidth(768)).toBe(true);
		expect(isCardWidth(769)).toBe(false);
	});

	it("follows the root font size, as rem does", () => {
		document.documentElement.style.fontSize = "20px";
		expect(isCardWidth(960)).toBe(true);
		expect(isCardWidth(961)).toBe(false);
		document.documentElement.style.fontSize = "12px";
		expect(isCardWidth(576)).toBe(true);
		expect(isCardWidth(577)).toBe(false);
	});
});
