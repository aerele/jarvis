import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "Asia/Kolkata" }));
import fs from "node:fs";
import path from "node:path";
import { defineComponent, nextTick } from "vue";
import { mount } from "@vue/test-utils";
import InvoiceDetailsDialog from "./InvoiceDetailsDialog.vue";
import { provideOverlayStack } from "./overlayStack";
import { reply, scenario } from "../../../tests/payment-calendar/templateFixtures";

const row = (over = {}) => ({
	id: "PINV-1-1",
	party_name: "Cedar Trading Co.",
	party: "SUP-1",
	voucher_no: "PINV-2026-0142",
	voucher_type: "Purchase Invoice",
	term: "Net 30",
	due_date: "2026-11-11",
	original: "20000.00",
	remaining: "18500.00",
	settled: "1500.00",
	settlement: "Partially paid",
	...over,
});
const modalState = (over = {}) => ({
	open: true,
	scope: "date",
	side: "Payable",
	date: "2026-11-11",
	page: 1,
	rows: [row()],
	total: 8,
	total_remaining: "42048.00",
	has_more: true,
	loading: false,
	error: null,
	unavailable: false,
	...over,
});
const $ = (selector) => document.body.querySelector(selector);
const $$ = (selector) => [...document.body.querySelectorAll(selector)];

describe("InvoiceDetailsDialog", () => {
	let wrapper;
	afterEach(() => {
		wrapper?.unmount();
		document.body.innerHTML = "";
	});
	async function render(props = {}) {
		wrapper = mount(InvoiceDetailsDialog, {
			attachTo: document.body,
			props: {
				snapshot: scenario("default"),
				modal: modalState(),
				sortKey: "remaining",
				sortDirection: "desc",
				...props,
			},
		});
		await nextTick();
		return wrapper;
	}
	const click = async (element) => {
		element.click();
		await nextTick();
	};

	describe("head and tabs", () => {
		it("is closed and absent when modal.open is false", async () => {
			await render({ modal: modalState({ open: false }) });
			expect($("[role=dialog]")).toBeNull();
		});
		it("is a labelled dialog with the scope title, context and a Close button", async () => {
			await render();
			const dialog = $("[role=dialog]");
			expect(dialog.getAttribute("aria-modal")).toBe("true");
			expect($("#" + dialog.getAttribute("aria-labelledby")).textContent).toBe(
				"Wednesday, 11 Nov"
			);
			expect(dialog.textContent).toContain("Remaining balances as of 11 Nov, 10:42 IST");
			const close = $('[aria-label="Close invoice details"]');
			expect(close.id).toBe("close-details");
			await click(close);
			expect(wrapper.emitted("close")).toHaveLength(1);
		});
		it("closes on Escape through the page overlay stack", async () => {
			const Host = defineComponent({
				components: { InvoiceDetailsDialog },
				setup: () => (provideOverlayStack(), {}),
				props: ["snapshot", "modal"],
				emits: ["close"],
				template:
					'<InvoiceDetailsDialog :snapshot="snapshot" :modal="modal" sort-key="remaining" sort-direction="desc" @close="$emit(\'close\')" />',
			});
			wrapper = mount(Host, {
				attachTo: document.body,
				props: { snapshot: scenario("default"), modal: modalState() },
			});
			await nextTick();
			document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
			expect(wrapper.emitted("close")).toHaveLength(1);
		});
		it("uses the snapshot context and no tabs for an obligation scope", async () => {
			await render({ modal: modalState({ scope: "overdue" }) });
			expect($("[role=dialog]").textContent).toContain("Snapshot 11 Nov, 10:42 IST");
			expect($("[role=dialog] h2").textContent).toBe("Overdue supplier invoices");
			expect($(".ledger-tabs")).toBeNull();
		});
		it("shows the server counts, marks the side and emits a switch", async () => {
			await render({ modal: modalState({ side: "Receivable" }) });
			const tabs = $$("[data-modal-side]");
			expect(tabs.map((tab) => tab.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
			expect(tabs[0].textContent).toContain("₹42,048.00");
			expect(tabs[1].textContent).toContain("₹86,250.00");
			await click(tabs[0]);
			expect(wrapper.emitted("side")).toEqual([["Payable"]]);
		});
	});

	describe("sort bar", () => {
		it("reads the record count and the server remaining total", async () => {
			await render();
			expect($(".sort-bar span").textContent).toBe("8 records · ₹42,048.00");
		});
		it("says Unavailable when the server sent no total", async () => {
			await render({ modal: modalState({ total_remaining: null }) });
			expect($(".sort-bar span").textContent).toBe("8 records · Unavailable");
		});
		it("offers the three sort keys and selects the active one", async () => {
			await render({ sortKey: "due_date", sortDirection: "asc" });
			const select = $('select[aria-label="Sort invoice records"]');
			expect(
				[...select.options].map((option) => [option.value, option.textContent])
			).toEqual([
				["remaining", "Remaining balance"],
				["date", "Due date"],
				["party", "Party name"],
			]);
			expect(select.value).toBe("date");
		});
		it("emits the chosen key from the select", async () => {
			await render();
			const select = $("#record-sort");
			select.value = "party";
			select.dispatchEvent(new Event("change"));
			expect(wrapper.emitted("sort")).toEqual([["party"]]);
			select.value = "date";
			select.dispatchEvent(new Event("change"));
			expect(wrapper.emitted("sort")[1]).toEqual(["due_date"]);
		});
	});

	describe("table", () => {
		it("lists the six columns in order", async () => {
			await render();
			expect($$("thead th").map((th) => th.textContent.trim())).toEqual([
				"Invoice / term",
				"Party",
				"Due date",
				"Invoice amount",
				"Settled",
				"Remaining ↓",
			]);
		});
		it("makes only Party, Due date and Remaining sortable", async () => {
			await render();
			expect($$("thead th button").map((b) => b.dataset.sort)).toEqual([
				"party",
				"date",
				"remaining",
			]);
		});
		it("sets aria-sort and the arrow from the sort state", async () => {
			await render({ sortKey: "party", sortDirection: "asc" });
			const sorts = () => $$("thead th").map((th) => th.getAttribute("aria-sort"));
			expect(sorts()).toEqual([null, "ascending", "none", null, null, "none"]);
			expect($('[data-sort="party"]').textContent.trim()).toBe("Party ↑");
			await wrapper.setProps({ sortDirection: "desc" });
			expect(sorts()).toEqual([null, "descending", "none", null, null, "none"]);
			expect($('[data-sort="party"]').textContent.trim()).toBe("Party ↓");
			expect($('[data-sort="remaining"]').textContent.trim()).toBe("Remaining");
		});
		it("emits the key of the header button that was clicked", async () => {
			await render();
			await click($('[data-sort="date"]'));
			await click($('[data-sort="remaining"]'));
			expect(wrapper.emitted("sort")).toEqual([["due_date"], ["remaining"]]);
		});
		it("keeps the same header button across a sort change so focus stays on it", async () => {
			await render();
			const button = $('[data-sort="party"]');
			button.focus();
			await wrapper.setProps({ sortKey: "party", sortDirection: "asc" });
			expect($('[data-sort="party"]')).toBe(button);
			expect(document.activeElement).toBe(button);
		});
		it("fills the cells from the server row", async () => {
			await render();
			const cells = $$("tbody tr:first-child td");
			expect(cells[0].querySelector(".invoice-reference").textContent).toBe(
				"PINV-2026-0142"
			);
			expect(cells[0].querySelector("small").textContent).toBe("Partially paid · Net 30");
			expect(cells[1].textContent).toBe("Cedar Trading Co.");
			expect(cells[2].textContent).toBe("11 Nov");
			expect(cells[3].textContent).toContain("₹20,000.00");
			expect(cells[4].textContent).toContain("₹1,500.00");
			expect(cells[5].querySelector("strong").textContent).toBe("₹18,500.00");
		});
		it("shows No due date and a dash for a null settled, never ₹0", async () => {
			await render({
				modal: modalState({
					rows: [row({ due_date: null, settled: null, settlement: "Unpaid", term: "" })],
				}),
			});
			const cells = $$("tbody tr:first-child td");
			expect(cells[2].textContent).toBe("No due date");
			expect(cells[4].textContent).toBe("Settled —");
			expect(cells[0].querySelector("small").textContent).toBe("Unpaid");
		});
		it("renders invoice numbers as plain text and escapes party names", async () => {
			const hostile = '<img src=x onerror="alert(1)">';
			await render({
				modal: modalState({ rows: [row({ party_name: hostile, source_link: "/app/x" })] }),
			});
			expect($("[role=dialog] a")).toBeNull();
			expect($("[role=dialog] img")).toBeNull();
			expect($("tbody tr td:nth-child(2)").textContent).toBe(hostile);
		});
		it("carries the phone-card labels and the remaining-column hook", async () => {
			await render();
			const row1 = $("tbody tr");
			expect(row1.querySelector("td.invoice-column")).not.toBeNull();
			expect(row1.querySelector("td.party-column")).not.toBeNull();
			expect(row1.querySelector("td.date-column")).not.toBeNull();
			expect(row1.querySelector("td.original-column .mobile-label").textContent).toBe(
				"Invoice "
			);
			expect(row1.querySelector("td.settled-column .mobile-label").textContent).toBe(
				"Settled "
			);
			expect(row1.querySelector("td.remaining-column .mobile-label").textContent).toBe(
				"Remaining"
			);
		});
	});

	describe("golden server reply (month scope, party ascending)", () => {
		it("lists the server rows in the server order with the server totals and settled", async () => {
			const data = reply("default_bucket_window_payable_party_asc");
			await render({
				modal: modalState({ scope: "month", ...data, rows: data.rows.slice(0, 25) }),
				sortKey: "party",
				sortDirection: "asc",
			});
			const parties = $$("tbody tr td:nth-child(2)").map((cell) => cell.textContent);
			expect(parties).toEqual(data.rows.slice(0, 25).map((row) => row.party_name));
			expect($(".sort-bar span").textContent).toBe("54 records · ₹6,00,898.00");
			expect($('[data-sort="party"]').closest("th").getAttribute("aria-sort")).toBe(
				"ascending"
			);
			expect($("tbody tr td:nth-child(5)").textContent).toBe("Settled ₹0.00");
		});
	});

	describe("states", () => {
		it("says there are no records", async () => {
			await render({ modal: modalState({ rows: [], total: 0 }) });
			expect($("td.empty-table").textContent).toBe("No records for this selection.");
		});
		it("says the data is unavailable instead of listing nothing", async () => {
			await render({ modal: modalState({ rows: [], total: null, unavailable: true }) });
			expect($("td.empty-table").textContent).toBe(
				"Invoice data is unavailable for this month."
			);
		});
		it("shows loading and no stale rows, keeping the header", async () => {
			await render({ modal: modalState({ rows: [], total: null, loading: true }) });
			expect($("td.empty-table").textContent).toBe("Loading records…");
			expect($$("thead th button")).toHaveLength(3);
		});
		it("shows the error with Retry", async () => {
			await render({
				modal: modalState({
					rows: [],
					total: null,
					error: { code: "network_error", state: "failed", retryable: true },
				}),
			});
			expect($("[role=alert]").textContent).toContain("The server could not be reached");
			await click($(".ledger-retry"));
			expect(wrapper.emitted("retry")).toHaveLength(1);
		});
	});

	describe("footer pagination", () => {
		const footer = () => $(".modal-foot");
		it("shows the record count, the settlement note and the page", async () => {
			await render();
			expect(footer().textContent).toContain("8 records");
			expect(footer().textContent).toContain(
				"Settlement can include payments, credits and discounts."
			);
			expect(footer().textContent).toContain("Page 1");
		});
		it("disables Previous on page 1 and pages forward through the composable", async () => {
			await render();
			expect($(".page-previous").disabled).toBe(true);
			await click($(".page-next"));
			expect(wrapper.emitted("page")).toEqual([[2]]);
		});
		it("pages back from page 2 and disables Next on the last page", async () => {
			await render({ modal: modalState({ page: 2, has_more: false }) });
			expect($(".page-next").disabled).toBe(true);
			await click($(".page-previous"));
			expect(wrapper.emitted("page")).toEqual([[1]]);
		});
		it("holds both buttons while a page is loading", async () => {
			await render({ modal: modalState({ loading: true, rows: [], total: null }) });
			expect($(".page-previous").disabled).toBe(true);
			expect($(".page-next").disabled).toBe(true);
		});
	});

	describe("phone cards (viewport @media, not a container: the dialog is teleported)", () => {
		const css = fs.readFileSync(path.resolve(__dirname, "InvoiceDetailsDialog.css"), "utf8");
		const phone = css.slice(css.indexOf("@media (max-width: 540px)"));
		it("hides the header and stacks each row as a two-column card at 540 px", () => {
			expect(css).not.toMatch(/@container/);
			expect(phone).toMatch(/\.detail-table thead\s*\{\s*display:\s*none/);
			expect(phone).toMatch(/\.detail-table tr\s*\{[^}]*display:\s*grid/);
		});
		it("puts the Remaining label above the amount and spans the right column", () => {
			expect(phone).toMatch(/td\.remaining-column\s*\{[^}]*grid-row:\s*1 \/ 4/);
			expect(phone).toMatch(/\.remaining-column \.mobile-label\s*\{[^}]*display:\s*block/);
		});
		it("hides the inline labels on desktop", () => {
			expect(css.slice(0, css.indexOf("@media (max-width"))).toMatch(
				/\.mobile-label\s*\{\s*display:\s*none/
			);
		});
	});
});
