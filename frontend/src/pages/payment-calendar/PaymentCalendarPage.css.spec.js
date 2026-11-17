import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

const css = fs
	.readFileSync(path.resolve(__dirname, "PaymentCalendarPage.css"), "utf8")
	.replace(/\/\*[\s\S]*?\*\//g, "");

// The body of the first block that starts with `header`, matched by brace depth.
function block(header) {
	const start = css.indexOf(header);
	if (start < 0) return null;
	let depth = 0;
	for (let i = css.indexOf("{", start); i < css.length; i++) {
		if (css[i] === "{") depth++;
		if (css[i] === "}" && --depth === 0) return css.slice(css.indexOf("{", start) + 1, i);
	}
	return null;
}
const pad = (query) => block(`@container pcal-page (${query})`);

describe("page padding follows the content width", () => {
	it("makes .pcal-root a named inline-size container that .pcal-main queries", () => {
		const root = block(".pcal-root {");
		expect(root).toMatch(/container-type:\s*inline-size/);
		expect(root).toMatch(/container-name:\s*pcal-page/);
		expect(css).not.toMatch(/@media[^{]*\{[^@]*\.pcal-main/);
	});

	it("uses the template padding at each threshold", () => {
		expect(block(".pcal-main {")).toMatch(/padding:\s*23px 30px 30px/);
		expect(pad("min-width: 851px")).toMatch(/\.pcal-main\s*\{\s*padding-top:\s*19px/);
		expect(pad("max-width: 1100px")).toMatch(/\.pcal-main\s*\{\s*padding:\s*22px 20px/);
		expect(pad("max-width: 540px")).toMatch(/\.pcal-main\s*\{\s*padding:\s*19px 15px 26px/);
	});

	it("applies the thresholds in the template's cascade order", () => {
		const at = (query) => css.indexOf(`@container pcal-page (${query})`);
		expect(at("min-width: 851px")).toBeLessThan(at("max-width: 1100px"));
		expect(at("max-width: 1100px")).toBeLessThan(at("max-width: 540px"));
	});

	it("keeps the layout at the template page width: 1632 border-box = 1572 content", () => {
		expect(block(".pcal-main > .pcal-layout {")).toMatch(/max-width:\s*1572px/);
	});
});

describe("content grid: calendar column and aside (template .content, L56, L102, L106, L113)", () => {
	// The container query that styles the content grid (other queries share the thresholds).
	const query = (width) => {
		for (let from = 0; ; ) {
			const start = css.indexOf(`@container (max-width: ${width}px)`, from);
			if (start < 0) return null;
			const body = block(css.slice(start));
			if (body?.includes(".pcal-calendar-layout")) return body;
			from = start + 1;
		}
	};
	const layout = block(".pcal-calendar-layout {");
	const wide = query(1200);
	const narrow = query(850);

	it("is the calendar column and a 340 px aside, 310 px at 1200 and below", () => {
		expect(layout).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)\s+340px/);
		expect(wide).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)\s+310px/);
	});

	it("keeps Next payments within 16 px below the calendar", () => {
		const gap = block(".pcal-calendar-column {").match(/gap:\s*(\d+)px/);
		expect(Number(gap[1])).toBeLessThanOrEqual(16);
	});

	it("makes the aside sticky at 16 px", () => {
		const aside = block(".pcal-aside {");
		expect(aside).toMatch(/position:\s*sticky/);
		expect(aside).toMatch(/top:\s*16px/);
	});

	it("orders calendar, aside, Next payments in one column at 850 and below", () => {
		expect(narrow).toMatch(/\.pcal-calendar-layout\s*\{[^}]*flex-direction:\s*column/);
		expect(narrow).toMatch(/\.pcal-calendar-column\s*\{[^}]*display:\s*contents/);
		const order = (selector) =>
			Number(narrow.match(new RegExp(selector + "\\s*\\{[^}]*order:\\s*(\\d)"))[1]);
		expect(order("\\.pcal-calendar-column > \\.pcal-cal")).toBe(0);
		expect(order("\\.pcal-aside")).toBe(1);
		expect(order("\\.pcal-calendar-column > \\.pcal-next")).toBe(2);
		expect(narrow).toMatch(/\.pcal-aside\s*\{[^}]*position:\s*static/);
	});
});
