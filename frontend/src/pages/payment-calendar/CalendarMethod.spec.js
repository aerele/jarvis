import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import CalendarMethod from "./CalendarMethod.vue";
import { signedMoney } from "@/lib/paymentCalendar/format";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";

const body = (selector) => document.body.querySelector(selector);

describe("CalendarMethod", () => {
	let wrapper, host;
	beforeEach(() => {
		host = document.createElement("div");
		document.body.appendChild(host);
	});
	afterEach(() => {
		wrapper?.unmount();
		host.remove();
	});
	function render(snapshot = modernStatus(), props = {}) {
		wrapper = mount(CalendarMethod, {
			props: {
				snapshot,
				selected: "2026-11-11",
				scope: "day",
				timezone: "Asia/Kolkata",
				...props,
			},
			attachTo: host,
		});
		return wrapper;
	}
	const rowsOf = () =>
		[...document.body.querySelectorAll(".pcal-modal .balance-row")].map((row) =>
			row.textContent.trim()
		);

	it("explains the net and the projection in two paragraphs, closed by default", () => {
		render();
		const details = wrapper.get("details.method");
		expect(details.attributes("open")).toBeUndefined();
		expect(details.get("summary").text()).toBe("How balances are calculated");
		expect(details.findAll("p").map((p) => p.text())).toEqual([
			"Net in a date cell means scheduled receipts minus scheduled payments. It is not the bank balance.",
			"Projected cash assumes collections arrive on time. It includes overdue and undated supplier dues once today. The forecast starts on the snapshot date.",
		]);
	});

	it("opens the calculation example from the exposed opener, with the server's figures", async () => {
		render();
		wrapper.vm.openCalculation();
		await flushPromises();
		expect(body(".pcal-modal h2").textContent).toBe("Forecast calculation · 11 Nov");
		expect(body(".pcal-modal").textContent).toMatch(/Snapshot 11 Nov, \d\d:\d\d \w+/);
		expect(rowsOf()).toEqual([
			"Book cash₹2,46,850.00",
			"+ Assumed collections through 11 Nov₹86,250.00",
			"− Scheduled supplier dues through 11 Nov₹42,048.00",
			"− Overdue supplier dues once₹22,500.00",
			"− Supplier dues with no date once₹5,000.00",
			"Projected closing cash₹2,63,552.00",
			"Minimum cash balance₹50,000.00",
			"Margin above minimum₹2,13,552.00",
		]);
	});

	it("uses the month-end date in month scope", async () => {
		render(modernStatus(), { scope: "month" });
		wrapper.vm.openCalculation();
		await flushPromises();
		expect(body(".pcal-modal h2").textContent).toBe("Forecast calculation · 30 Nov");
		expect(rowsOf()).toContain("Projected closing cash₹2,62,052.00");
		expect(rowsOf()).toContain("+ Assumed collections through 30 Nov₹6,21,100.00");
	});

	it("says no forecast is available, with the reason", async () => {
		render(modernStatus("accounts_none"));
		wrapper.vm.openCalculation();
		await flushPromises();
		expect(rowsOf()).toEqual([]);
		const paragraphs = [...document.body.querySelectorAll(".pcal-modal .method-body p")].map(
			(p) => p.textContent
		);
		expect(paragraphs).toEqual([
			"No forecast available.",
			"Select a bank or cash account to see the forecast.",
		]);
	});

	it("closes the calculation example with its close button", async () => {
		render();
		wrapper.vm.openCalculation();
		await flushPromises();
		expect(body(".pcal-modal")).not.toBeNull();
		body(".pcal-modal .pcal-icon-btn").click();
		await flushPromises();
		expect(body(".pcal-modal")).toBeNull();
	});

	it("opens balance details with the server's reconciliation rows", async () => {
		render();
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		expect(body(".pcal-modal h2").textContent).toBe("Outstanding balance details");
		const header = [...document.body.querySelectorAll(".balance-table th")].map((n) =>
			n.textContent.trim()
		);
		expect(header).toEqual(["Category", "Payable", "Receivable"]);
		const rows = [...document.body.querySelectorAll(".balance-table tbody tr")].map((tr) =>
			[...tr.children].map((n) => n.textContent.trim())
		);
		// Amounts come from the golden reconciliation; the four template rows, plus the server's
		// later-month and adjustment buckets while they hold something.
		const recon = modernStatus().projection.reconciliation;
		const row = (label, key) => [
			label,
			...["Payable", "Receivable"].map((side) => signedMoney(recon[side][key], "INR", 2)),
		];
		expect(rows).toEqual([
			row("Due today & later", "current_future"),
			row("Overdue", "overdue"),
			row("No due date", "undated"),
			row("After this month", "outside_horizon"),
			row("Adjustments", "adjustments"),
			row("Total outstanding", "canonical_total"),
		]);
	});

	it("leaves out a bucket that is zero on both sides", async () => {
		const status = modernStatus();
		for (const side of ["Payable", "Receivable"]) {
			status.projection.reconciliation[side].adjustments = "0.00";
			status.projection.reconciliation[side].outside_horizon = "0.00";
		}
		render(status);
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		const labels = [...document.body.querySelectorAll(".balance-table tbody tr")].map((tr) =>
			tr.children[0].textContent.trim()
		);
		expect(labels).toEqual([
			"Due today & later",
			"Overdue",
			"No due date",
			"Total outstanding",
		]);
	});

	it("explains the customer overdue and undated balances", async () => {
		render();
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		expect(body(".pcal-modal").textContent).toContain(
			"Overdue and undated customer balances are outside assumed on-time collections."
		);
	});

	it("shows Unavailable for a side with no reconciliation", async () => {
		const status = modernStatus();
		status.projection.reconciliation.Receivable = null;
		render(status);
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		const first = body(".balance-table tbody tr").children;
		expect(first[2].textContent.trim()).toBe("Unavailable");
	});

	it("shows loading in the calculation example while the snapshot is another month's", async () => {
		render(modernStatus(), { month: "2026-12", selected: "2026-12-01" });
		wrapper.vm.openCalculation();
		await flushPromises();
		// no date in the title: the stale snapshot's month end must not show
		expect(body(".pcal-modal h2").textContent).toBe("Forecast calculation");
		expect(rowsOf()).toEqual([]);
		const paragraphs = [...document.body.querySelectorAll(".pcal-modal .method-body p")].map(
			(p) => p.textContent
		);
		expect(paragraphs).toEqual(["No forecast available.", "Loading values…"]);
	});

	it("shows the title date again when the snapshot is the displayed month's", async () => {
		render(modernStatus("month_dec"), {
			month: "2026-12",
			selected: "2026-12-01",
			scope: "month",
		});
		wrapper.vm.openCalculation();
		await flushPromises();
		expect(body(".pcal-modal h2").textContent).toBe("Forecast calculation · 31 Dec");
	});

	it("names the month, not 'today', for the first bucket of another month's snapshot", async () => {
		render(modernStatus("month_dec"), { month: "2026-12", selected: "2026-12-01" });
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		const labels = [...document.body.querySelectorAll(".balance-table tbody tr")].map((tr) =>
			tr.children[0].textContent.trim()
		);
		expect(labels[0]).toBe("Due in December 2026");
		expect(labels).not.toContain("Due today & later");
	});

	it("keeps 'Due today & later' for the anchor month", async () => {
		const status = modernStatus();
		status.scope.month = status.scope.anchor.slice(0, 7);
		render(status, { month: status.scope.month });
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		expect(body(".balance-table tbody tr").children[0].textContent.trim()).toBe(
			"Due today & later"
		);
	});

	it("restores the excluded exchange differences row from the source notes", async () => {
		const status = modernStatus();
		status.projection.source_notes = {
			Payable: { currency_residue_excluded: { count: 2, amount: "0.04" } },
			Receivable: { currency_residue_excluded: { count: 0, amount: "0.00" } },
		};
		render(status);
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		const rows = [...document.body.querySelectorAll(".balance-table tbody tr")].map((tr) =>
			[...tr.children].map((n) => n.textContent.trim())
		);
		expect(rows.at(-1)).toEqual(["Exchange differences (excluded)", "₹0.04", "₹0.00"]);
	});

	it("leaves the exchange differences row out when no side excluded any", async () => {
		render();
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		expect(body(".balance-table").textContent).not.toContain("Exchange differences");
	});

	it("uses a neutral label for the later bucket while the snapshot is another month's", async () => {
		render(modernStatus(), { month: "2026-12", selected: "2026-12-01" });
		wrapper.vm.openBalanceDetails();
		await flushPromises();
		const labels = [...document.body.querySelectorAll(".balance-table tbody tr")].map((tr) =>
			tr.children[0].textContent.trim()
		);
		expect(labels).toContain("Outside forecast");
		expect(labels).not.toContain("After this month");
	});
});
