import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

vi.mock("@/api/paymentCalendar", () => ({
	requestAccountBalances: vi.fn(),
	getAccountBalancesStatus: vi.fn(),
}));
import * as api from "@/api/paymentCalendar";
import AccountBalancesDialog from "./AccountBalancesDialog.vue";
import { modernStatus } from "../../../tests/payment-calendar/headlineFixtures";

const ok = (data) => ({ ok: true, data });
const refusal = (code, state = "unavailable") => ({ ok: false, code, state, retryable: true });
const COMPANY = "Northstar Manufacturing Pvt Ltd";
const JOB_ACCOUNTS = [
	{
		account: "HDFC Current",
		account_name: "HDFC Current",
		account_type: "Bank",
		bank: "HDFC Bank",
		reference: "•••• 3281",
		balance: "221850.00",
	},
	{
		account: "Petty Cash",
		account_name: "Petty Cash",
		account_type: "Cash",
		bank: null,
		reference: "Main office",
		balance: "25000.00",
	},
	{
		account: "Branch 03",
		account_name: "Branch 03",
		account_type: "Bank",
		bank: "ICICI Bank",
		reference: "•••• 1021",
		balance: "10.00",
	},
];
const complete = (extra = {}) => ({
	state: "complete",
	generated_at: "2026-11-11T05:15:00+00:00",
	expires_at: "2026-11-11T05:20:00+00:00",
	currency: "INR",
	precision: 2,
	accounts: JOB_ACCOUNTS,
	// deliberately not the visible sum: the total is the server's field
	total: "777777.00",
	...extra,
});

const $ = (selector) => document.body.querySelector(selector);
const text = (selector) => $(selector)?.textContent.trim();
let wrapper;

async function render(props = {}) {
	wrapper = mount(AccountBalancesDialog, {
		props: {
			modelValue: true,
			snapshot: modernStatus(),
			company: COMPANY,
			timezone: "Asia/Kolkata",
			...props,
		},
		attachTo: document.body,
	});
	await flushPromises();
}
// "name | detail | balance" per row
const rows = () =>
	[...document.body.querySelectorAll(".account-balance-row")].map((row) =>
		[...row.querySelectorAll("strong, small, b")]
			.map((node) => node.textContent.trim())
			.join(" | ")
	);
const showAll = async () => {
	$("#account-balance-toggle").click();
	await flushPromises();
};

describe("AccountBalancesDialog", () => {
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-11-11T05:16:00Z"));
		vi.clearAllMocks();
		api.requestAccountBalances.mockResolvedValue(ok({ job_id: "job-1", state: "queued" }));
		api.getAccountBalancesStatus.mockResolvedValue(ok(complete()));
	});
	afterEach(() => {
		wrapper?.unmount();
		document.body.innerHTML = "";
		vi.useRealTimers();
	});

	it("renders nothing while closed", async () => {
		await render({ modelValue: false });
		expect($(".pcal-modal")).toBeNull();
	});

	describe("selected accounts (from the snapshot)", () => {
		it("shows the template's modal for the default accounts", async () => {
			await render();
			expect(text(".pcal-modal-heading h2")).toBe("Bank & cash account balances");
			expect($(".pcal-icon-btn").getAttribute("aria-label")).toBe("Close account balances");
			expect($("#close-aux").getAttribute("aria-label")).toBe("Close account balances");
			expect($(".pcal-modal").className).toContain("pcal-modal-sm");
			expect(text(".account-balance-context")).toBe("Book balances as of 11 Nov, 10:42 IST");
			expect(text(".account-balance-scope span")).toBe("Selected accounts · 2");
			expect(rows()).toEqual([
				"HDFC Current | Bank · HDFC Bank · •••• 3281 | ₹2,21,850.00",
				"Petty Cash | Cash | ₹25,000.00",
			]);
			expect(text(".account-balance-total span")).toBe("Book cash now · selected accounts");
			expect(text(".account-balance-total strong")).toBe("₹2,46,850.00");
			expect(text(".demo-note")).toBe(
				"The forecast uses selected accounts only. These are book balances in INR."
			);
			expect(text("#account-balance-toggle")).toBe("Show all accounts");
			expect(api.requestAccountBalances).not.toHaveBeenCalled();
		});

		it("takes the total from the server, not from the rows", async () => {
			const snapshot = modernStatus();
			snapshot.bank.total = "1.00";
			await render({ snapshot });
			expect(text(".account-balance-total strong")).toBe("₹1.00");
		});

		it("explains an empty selection and shows Unavailable", async () => {
			const snapshot = modernStatus();
			snapshot.scope.accounts = [];
			snapshot.bank = null;
			await render({ snapshot });
			expect(text(".account-balance-scope span")).toBe("Selected accounts · 0");
			expect(text(".account-balance-list")).toBe(
				"No accounts selected. Select accounts to include book cash in the forecast."
			);
			expect(text(".account-balance-total strong")).toBe("Unavailable");
		});

		it("says the balances are missing for an old-shape snapshot, never ₹0", async () => {
			const snapshot = modernStatus();
			delete snapshot.bank;
			await render({ snapshot });
			expect(text(".account-balance-list")).toBe(
				"Account balances are not available for this snapshot."
			);
			expect(text(".account-balance-total strong")).toBe("Unavailable");
			expect(document.body.textContent).not.toContain("₹0.00");
		});

		it("shows a loading line, not 'not available', while the snapshot is refreshing", async () => {
			const snapshot = modernStatus();
			snapshot.snapshot_id = null;
			snapshot.bank = null;
			await render({ snapshot });
			expect(text(".account-balance-list")).toBe("Loading account balances…");
			expect(document.body.textContent).not.toContain("not available for this snapshot");
		});

		it("leaves out a missing bank or reference", async () => {
			const snapshot = modernStatus();
			snapshot.bank.accounts[0].bank = null;
			snapshot.bank.accounts[0].reference = null;
			await render({ snapshot });
			expect(rows()[0]).toBe("HDFC Current | Bank | ₹2,21,850.00");
		});
	});

	describe("Show all accounts (server job)", () => {
		it("shows a loading line while the job runs, then the job's list and total", async () => {
			api.getAccountBalancesStatus
				.mockResolvedValueOnce(ok({ state: "running" }))
				.mockResolvedValue(ok(complete()));
			await render();
			await showAll();
			expect(api.requestAccountBalances).toHaveBeenCalledWith(COMPANY);
			expect(text(".account-balance-list")).toBe("Loading account balances…");
			expect(text("#account-balance-toggle")).toBe("Show selected accounts");
			expect(rows()).toEqual([]);
			await vi.advanceTimersByTimeAsync(1000);
			expect(text(".account-balance-context")).toBe("Book balances as of 11 Nov, 10:45 IST");
			expect(text(".account-balance-scope span")).toBe("All accounts · 3");
			expect(rows()).toEqual([
				"HDFC Current | Bank · HDFC Bank · •••• 3281 · Selected | ₹2,21,850.00",
				"Petty Cash | Cash · Main office · Selected | ₹25,000.00",
				"Branch 03 | Bank · ICICI Bank · •••• 1021 | ₹10.00",
			]);
			expect(text(".account-balance-total span")).toBe("Total shown · all accounts");
			expect(text(".account-balance-total strong")).toBe("₹7,77,777.00");
		});

		it("says there are no accounts to view, and shows no total, when the job lists none", async () => {
			api.getAccountBalancesStatus.mockResolvedValue(
				ok(complete({ accounts: [], total: "0.00" }))
			);
			await render();
			await showAll();
			expect(text(".account-balance-list")).toBe("No bank or cash accounts you can view.");
			expect($(".account-balance-total")).toBeNull();
			expect(text(".account-balance-scope span")).toBe("All accounts · 0");
		});

		it("keeps focus on the toggle", async () => {
			await render();
			$("#account-balance-toggle").focus();
			await showAll();
			expect(document.activeElement).toBe($("#account-balance-toggle"));
		});

		it("goes back to the selected view without a request, and reuses the finished job", async () => {
			await render();
			await showAll();
			await showAll();
			expect(text(".account-balance-scope span")).toBe("Selected accounts · 2");
			expect(text(".account-balance-context")).toBe("Book balances as of 11 Nov, 10:42 IST");
			await showAll();
			expect(api.requestAccountBalances).toHaveBeenCalledTimes(1);
			expect(text(".account-balance-total strong")).toBe("₹7,77,777.00");
		});

		it("asks again once the finished job expired", async () => {
			await render();
			await showAll();
			await showAll();
			await vi.advanceTimersByTimeAsync(5 * 60000);
			await showAll();
			expect(api.requestAccountBalances).toHaveBeenCalledTimes(2);
		});

		it("leaves the forecast selection alone", async () => {
			await render();
			await showAll();
			expect(wrapper.emitted("update:modelValue")).toBeUndefined();
			expect(api.requestAccountBalances).toHaveBeenCalledTimes(1);
		});

		it.each([
			["worker_busy", "Your calendar jobs are still running. Please wait, then try again."],
			[
				"scope_changed",
				"Access or company settings changed. Try again to read the balances.",
			],
			[
				"network_error",
				"The server could not be reached. Check your connection and try again.",
			],
		])(
			"shows try-again copy for a %s refusal, with a Try again button, and stops",
			async (code, copy) => {
				api.requestAccountBalances.mockResolvedValue(refusal(code));
				await render();
				await showAll();
				expect(text(".account-balance-list")).toContain(copy);
				expect(text("#account-balances-retry")).toBe("Try again");
				expect(text(".account-balance-total")).toBeUndefined();
				await vi.advanceTimersByTimeAsync(30000);
				expect(api.requestAccountBalances).toHaveBeenCalledTimes(1);
				expect(api.getAccountBalancesStatus).not.toHaveBeenCalled();
			}
		);

		it.each([
			[
				"too_large",
				"This company has more than 500 bank and cash accounts. Choose accounts to see their balances.",
			],
			[
				"forbidden",
				"You no longer have access to this financial view. Ask your accounts administrator for access.",
			],
		])("shows %s copy without a Try again button", async (code, copy) => {
			api.requestAccountBalances.mockResolvedValue(refusal(code));
			await render();
			await showAll();
			expect(text(".account-balance-list")).toContain(copy);
			expect($("#account-balances-retry")).toBeNull();
		});

		it("shows a failed job's code copy and lets the person try again", async () => {
			api.getAccountBalancesStatus
				.mockResolvedValueOnce(ok({ state: "failed", code: "job_lost" }))
				.mockResolvedValue(ok(complete()));
			await render();
			await showAll();
			expect(text(".account-balance-list")).toContain(
				"The balances job stopped before it finished. Try again."
			);
			$("#account-balances-retry").click();
			await flushPromises();
			expect(api.requestAccountBalances).toHaveBeenCalledTimes(2);
			expect(text(".account-balance-total strong")).toBe("₹7,77,777.00");
		});
	});

	describe("closing", () => {
		it("stops polling when the dialog closes", async () => {
			api.getAccountBalancesStatus.mockResolvedValue(ok({ state: "running" }));
			await render();
			await showAll();
			await vi.advanceTimersByTimeAsync(1500);
			const calls = api.getAccountBalancesStatus.mock.calls.length;
			await wrapper.setProps({ modelValue: false });
			await vi.advanceTimersByTimeAsync(30000);
			expect(api.getAccountBalancesStatus.mock.calls.length).toBe(calls);
		});

		it("opens on the selected view again", async () => {
			await render();
			await showAll();
			await wrapper.setProps({ modelValue: false });
			await wrapper.setProps({ modelValue: true });
			await flushPromises();
			expect(text(".account-balance-scope span")).toBe("Selected accounts · 2");
			expect(text("#account-balance-toggle")).toBe("Show all accounts");
		});

		it("emits update:modelValue false from the close button", async () => {
			await render();
			$(".pcal-icon-btn").click();
			expect(wrapper.emitted("update:modelValue")).toEqual([[false]]);
		});
	});
});
