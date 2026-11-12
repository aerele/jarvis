import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { DARK_VARS } from "@/theme";

const read = (file) => fs.readFileSync(path.resolve(__dirname, file), "utf8");
const tokens = read("tokens.css");
const indexCss = fs.readFileSync(path.resolve(__dirname, "../../index.css"), "utf8");

// The declarations of the first rule whose selector list contains `selector`.
function declarations(selector) {
	const rule = tokens.split("}").find((block) => block.split("{")[0].includes(selector));
	const body = rule.split("{")[1];
	return Object.fromEntries(
		[...body.matchAll(/(--pc-[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()])
	);
}
const LIGHT = ".pcal-root,\n.pcal-overlay";
const DARK = '[data-theme="dark"] .pcal-root,\n[data-theme="dark"] .pcal-overlay';

describe("payment calendar tokens", () => {
	it("declares the light tokens on both roots, so a teleported overlay reads them", () => {
		expect(tokens).toMatch(/\.pcal-root,\s*\.pcal-overlay\s*\{/);
		expect(tokens).toMatch(
			/\[data-theme="dark"\] \.pcal-root,\s*\[data-theme="dark"\] \.pcal-overlay\s*\{/
		);
	});

	it("uses literal values only, so no token depends on a variable the overlay may not inherit", () => {
		const all = [...tokens.matchAll(/(--pc-[\w-]+):\s*([^;]+);/g)];
		expect(all.length).toBeGreaterThan(40);
		for (const [, name, value] of all) {
			if (name === "--pc-font") continue;
			expect(value, name).not.toMatch(/var\(/);
		}
	});

	it("keeps the template light palette", () => {
		const light = declarations(LIGHT);
		expect(light).toMatchObject({
			"--pc-bg": "#f7f8fa",
			"--pc-text": "#202a31",
			"--pc-card": "#fff",
			"--pc-primary": "#285d4b",
			"--pc-link": "#28644f",
			"--pc-positive": "#326e52",
			"--pc-balance-total": "#4b7357",
			"--pc-caution": "#936b25",
			"--pc-negative": "#ad503f",
			"--pc-muted": "#75818a",
			"--pc-focus": "#276c57",
			"--pc-backdrop": "#12271e62",
		});
	});

	it("maps dark tokens to the Jarvis DARK_VARS values as the template does", () => {
		const dark = declarations(DARK);
		expect(dark).toMatchObject({
			"--pc-bg": DARK_VARS["--surface"],
			"--pc-card": DARK_VARS["--surface-1"],
			"--pc-surface-2": DARK_VARS["--surface-2"],
			"--pc-surface-3": DARK_VARS["--surface-3"],
			"--pc-border": DARK_VARS["--border"],
			"--pc-border-strong": DARK_VARS["--border-2"],
			"--pc-text": DARK_VARS["--text"],
			"--pc-text-2": DARK_VARS["--text-2"],
			"--pc-primary": DARK_VARS["--cta"],
			"--pc-primary-fg": DARK_VARS["--cta-fg"],
			"--pc-link": DARK_VARS["--link"],
			"--pc-focus": DARK_VARS["--link"],
			"--pc-positive": DARK_VARS["--green"],
			"--pc-positive-bg": DARK_VARS["--green-bg"],
			"--pc-positive-bd": DARK_VARS["--green-bd"],
			"--pc-balance-total": DARK_VARS["--text-2"],
			"--pc-caution": DARK_VARS["--amber"],
			"--pc-negative": DARK_VARS["--red"],
			"--pc-negative-bg": DARK_VARS["--red-bg"],
			"--pc-muted": "#9a9aa5",
			"--pc-backdrop": "#0009",
		});
	});

	it("has the template's details surface: #fbfcfb with a #e1e7e3 border, link-blue summaries in dark", () => {
		expect(declarations(LIGHT)).toMatchObject({
			"--pc-details-bg": "#fbfcfb",
			"--pc-details-border": "#e1e7e3",
			"--pc-details-rule": "#e7ede8",
			"--pc-details-summary": "#75897b",
			"--pc-details-method-summary": "#65776b",
			"--pc-details-date": "#9aa79e",
		});
		expect(declarations(DARK)).toMatchObject({
			"--pc-details-bg": DARK_VARS["--surface-1"],
			"--pc-details-border": DARK_VARS["--border"],
			"--pc-details-rule": DARK_VARS["--border"],
			"--pc-details-summary": DARK_VARS["--link"],
			"--pc-details-method-summary": DARK_VARS["--link"],
			"--pc-details-date": "#9a9aa5",
		});
	});

	it("uses the details tokens on the two disclosures, not the tinted surface", () => {
		const details = read("CashDetails.css").replace(/\/\*[\s\S]*?\*\//g, "");
		const rule = (selector) =>
			details.split("}").find((b) => b.split("{")[0].trim() === selector);
		expect(rule(".financial-details")).toMatch(/background:\s*var\(--pc-details-bg\)/);
		expect(rule(".financial-details")).toMatch(
			/border:\s*1px solid var\(--pc-details-border\)/
		);
		expect(rule(".financial-details > summary")).toMatch(
			/color:\s*var\(--pc-details-summary\)/
		);
		expect(read("CalendarMethod.css")).toMatch(
			/\.method summary\s*\{[^}]*var\(--pc-details-method-summary\)/
		);
	});

	it("keeps the page's two link and negative colours in the tokens, not in a second palette", () => {
		for (const file of ["PaymentCalendarPage.css", "CashDetails.css", "CalendarMethod.css"])
			expect(read(file), file).not.toMatch(/--pcal-/);
		expect(fs.existsSync(path.resolve(__dirname, "payment-calendar.css"))).toBe(false);
	});

	it("defines the template type scale, radii and shadows", () => {
		const light = declarations(LIGHT);
		expect(light).toMatchObject({
			"--pc-fs-h1": "26px",
			"--pc-fs-kpi": "34px",
			"--pc-fs-modal-title": "19px",
			"--pc-radius-card": "9px",
			"--pc-radius-modal": "12px",
			"--pc-radius-preview": "11px",
			"--pc-radius-button": "7px",
			"--pc-radius-chip": "6px",
			"--pc-radius-tag": "4px",
			"--pc-shadow-menu": "0 10px 34px #23312c23",
			"--pc-shadow-modal": "0 24px 80px #10231840",
		});
		expect(declarations(DARK)["--pc-shadow-modal"]).toBe("0 24px 80px #0008");
	});
});

describe("index.css focus-ring opt-in", () => {
	const css = indexCss.replace(/\/\*[\s\S]*?\*\//g, "");
	const ruleWith = (needle) => css.split("}").find((block) => block.includes(needle));

	it("drops the forms-plugin focus ring on the page and its overlays, like the onboarding root", () => {
		const rule = ruleWith(".jv-ob-root :where(");
		expect(rule).toContain(".pcal-root :where(");
		expect(rule).toContain(".pcal-overlay :where(");
	});

	it("keeps the app's input ring and border off the page and its overlays", () => {
		const rule = ruleWith(".jv-ob-root *");
		expect(rule).toContain(".pcal-root *");
		expect(rule).toContain(".pcal-overlay *");
	});

	it("does not give the page the chat's own accent ring: only the template ring applies", () => {
		const ring = ruleWith("outline: 2px solid var(--focus-ring)");
		expect(ring).not.toContain("pcal");
	});
});
