import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

const css = fs
	.readFileSync(path.resolve(__dirname, "CalendarFilterBar.css"), "utf8")
	.replace(/\/\*[\s\S]*?\*\//g, "");

// The declarations of the rule whose selector is exactly `selector`.
function rule(selector) {
	const found = css.split("}").find((block) => block.split("{")[0].trim() === selector);
	return found ? found.split("{")[1] : null;
}
const SEARCH = '.pcal-root .menu input[type="search"]';
const CHECKBOX = '.pcal-root .account-option input[type="checkbox"]';

describe("account picker controls in dark mode (template dark inputs)", () => {
	it("paints the search field with the input surface, not the panel surface", () => {
		expect(rule(`[data-theme="dark"] ${SEARCH}`)).toMatch(
			/background:\s*var\(--pc-surface-2\)/
		);
	});

	it("keeps the light search field on the card colour", () => {
		expect(rule(SEARCH)).toMatch(/background:\s*var\(--pc-card\)/);
	});

	it("gives checkboxes the native look back from the forms plugin reset", () => {
		const body = rule(CHECKBOX);
		expect(body).toMatch(/appearance:\s*auto/);
		expect(body).toMatch(/background-color:\s*revert/);
		expect(body).toMatch(/background-image:\s*none/);
	});

	it("draws native dark checkboxes with the link colour in dark mode", () => {
		const body = rule(`[data-theme="dark"] ${CHECKBOX}`);
		expect(body).toMatch(/color-scheme:\s*dark/);
		expect(body).toMatch(/accent-color:\s*var\(--pc-link\)/);
	});
});
