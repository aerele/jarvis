import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

// The declarations of the first rule whose selector is exactly `selector`.
function rule(file, selector) {
	const css = fs
		.readFileSync(path.resolve(__dirname, file), "utf8")
		.replace(/\/\*[\s\S]*?\*\//g, "");
	const block = css.split("}").find((part) => part.split("{")[0].trim() === selector);
	return block ? block.split("{")[1] : "";
}

describe("template surfaces that the review found off", () => {
	it("draws the sort select natively, like the template: no forms-plugin chevron", () => {
		const select = rule("InvoiceDetailsDialog.css", ".sort-bar select");
		expect(select).toMatch(/appearance:\s*auto/);
		expect(select).toMatch(/background-image:\s*none/);
		expect(select).toMatch(/padding:\s*4px 6px/);
		expect(select).toMatch(/line-height:\s*normal/);
		expect(select).toMatch(/color:\s*var\(--pc-select-text\)/);
	});

	it("sets the 'Book balances as of' line at the template's 13 px", () => {
		const context = rule(
			"AccountBalancesDialog.css",
			".pcal-overlay .account-balance-context"
		);
		expect(context).toMatch(/font-size:\s*13px/);
	});

	it("wraps a long unbroken account name inside its row", () => {
		const names = rule(
			"AccountBalancesDialog.css",
			".pcal-overlay .account-balance-row strong,\n.pcal-overlay .account-balance-row small"
		);
		expect(names).toMatch(/overflow-wrap:\s*anywhere/);
	});

	it("gives the week strip's chevrons a 44 px touch target", () => {
		const chevron = rule("CalendarWeekStrip.css", ".pcal-cal .week-strip > .icon-btn");
		expect(chevron).toMatch(/width:\s*44px/);
		expect(chevron).toMatch(/min-height:\s*44px/);
	});

	it("shows the picker's limit reason as text", () => {
		expect(rule("CalendarFilterBar.css", ".pcal-root .account-limit-note")).toMatch(
			/font-size/
		);
	});

	it("draws the ledger tab count pills as the template does in dark: surface-3 and text-2", () => {
		const pill = rule("LedgerTabs.css", '[data-theme="dark"] .ledger-tabs .count');
		expect(pill).toMatch(/background:\s*var\(--pc-surface-3\)/);
		expect(pill).toMatch(/color:\s*var\(--pc-text-2\)/);
	});

	it("keeps the details' button defaults off the primary button, so its own colour and cursor win", () => {
		// Bare `.financial-details button` (0,1,1) would beat `.pcal-primary-btn` (0,1,0).
		expect(rule("CashDetails.css", ".financial-details button")).toBe("");
		expect(rule("CashDetails.css", ".financial-details button:disabled")).toBe("");
		const base = rule("CashDetails.css", ".financial-details button:not(.pcal-primary-btn)");
		expect(base).toMatch(/color:\s*inherit/);
		const disabled = rule(
			"CashDetails.css",
			".financial-details button:not(.pcal-primary-btn):disabled"
		);
		expect(disabled).toMatch(/cursor:\s*default/);
		expect(rule("PaymentCalendarPage.css", ".pcal-primary-btn")).toMatch(
			/color:\s*var\(--pc-primary-fg\);/
		);
	});
});
