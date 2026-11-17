import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectScope } from "vue";
import { useAccountBalances } from "./useAccountBalances";

// The composable takes the API as an argument; keep frappe-ui out of this spec.
vi.mock("@/api/paymentCalendar", () => ({}));

const T0 = Date.parse("2026-11-11T05:12:00Z");
const ok = (data) => ({ ok: true, data });
const refusal = (code, state = "unavailable") => ({ ok: false, code, state, retryable: true });
const ACCOUNTS = [
	{
		account: "HDFC Current",
		account_name: "HDFC Current",
		account_type: "Bank",
		bank: "HDFC Bank",
		reference: "•••• 3281",
		balance: "221850.00",
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
	generated_at: new Date(T0 + 3000).toISOString(),
	expires_at: new Date(T0 + 303000).toISOString(),
	currency: "INR",
	precision: 2,
	accounts: ACCOUNTS,
	total: "221860.00",
	...extra,
});
const running = () => ok({ state: "running" });

function setup(overrides = {}) {
	const api = {
		requestAccountBalances: vi.fn(async () => ok({ job_id: "job-1", state: "queued" })),
		getAccountBalancesStatus: vi.fn(async () => ok(complete())),
		...overrides,
	};
	const scope = effectScope();
	const balances = scope.run(() => useAccountBalances({ api }));
	return { api, balances, scope };
}

describe("useAccountBalances", () => {
	let scope;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(T0);
	});
	afterEach(() => {
		scope?.stop();
		vi.useRealTimers();
	});

	it("starts idle", () => {
		const ctx = setup();
		scope = ctx.scope;
		expect(ctx.balances.state.value).toBe("idle");
		expect(ctx.balances.result.value).toBeNull();
	});

	it("requests the company, reads the job at once and stores the server result", async () => {
		const ctx = setup();
		scope = ctx.scope;
		const done = ctx.balances.load("Northstar");
		expect(ctx.balances.state.value).toBe("loading");
		await done;
		expect(ctx.api.requestAccountBalances).toHaveBeenCalledWith("Northstar");
		expect(ctx.api.getAccountBalancesStatus).toHaveBeenCalledWith("job-1");
		expect(ctx.balances.state.value).toBe("ready");
		expect(ctx.balances.result.value).toMatchObject({
			total: "221860.00",
			currency: "INR",
			precision: 2,
		});
		expect(ctx.balances.result.value.accounts).toHaveLength(2);
		expect(ctx.balances.result.value.generated_at).toBe(new Date(T0 + 3000).toISOString());
	});

	it("polls with a 1, 2, 3, then 5 second backoff until the job completes", async () => {
		const statuses = [running(), running(), running(), running(), running(), ok(complete())];
		const ctx = setup({ getAccountBalancesStatus: vi.fn(async () => statuses.shift()) });
		scope = ctx.scope;
		const stamps = [];
		ctx.api.getAccountBalancesStatus.mockImplementation(async () => {
			stamps.push(Date.now() - T0);
			return statuses.shift();
		});
		void ctx.balances.load("Northstar");
		await vi.advanceTimersByTimeAsync(20000);
		expect(stamps).toEqual([0, 1000, 3000, 6000, 11000, 16000]);
		expect(ctx.balances.state.value).toBe("ready");
	});

	describe("while the tab is hidden", () => {
		const setHidden = (hidden) =>
			Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
		afterEach(() => setHidden(false));

		it("polls every 15 seconds, then returns to the normal backoff when visible", async () => {
			const stamps = [];
			const ctx = setup({
				getAccountBalancesStatus: vi.fn(async () => {
					stamps.push(Date.now() - T0);
					return running();
				}),
			});
			scope = ctx.scope;
			setHidden(true);
			void ctx.balances.load("Northstar");
			await vi.advanceTimersByTimeAsync(31000);
			expect(stamps).toEqual([0, 15000, 30000]);
			setHidden(false);
			await vi.advanceTimersByTimeAsync(15000);
			// the pending hidden wait ends, then visible polls follow the attempt backoff (5 s by now)
			expect(stamps.slice(3)).toEqual([45000]);
			await vi.advanceTimersByTimeAsync(5000);
			expect(stamps.slice(3)).toEqual([45000, 50000]);
		});
	});

	it("stops polling once complete", async () => {
		const ctx = setup();
		scope = ctx.scope;
		await ctx.balances.load("Northstar");
		await vi.advanceTimersByTimeAsync(60000);
		expect(ctx.api.getAccountBalancesStatus).toHaveBeenCalledTimes(1);
	});

	describe("reusing a completed job", () => {
		it("re-reads the cached job's status once and shows that reply, without a new job", async () => {
			const ctx = setup();
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			ctx.balances.reset();
			const reads = ctx.api.getAccountBalancesStatus.mock.calls.length;
			ctx.api.getAccountBalancesStatus.mockResolvedValueOnce(
				ok(complete({ total: "5.00" }))
			);
			await ctx.balances.load("Northstar");
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(1);
			expect(ctx.api.getAccountBalancesStatus).toHaveBeenCalledTimes(reads + 1);
			expect(ctx.api.getAccountBalancesStatus).toHaveBeenLastCalledWith("job-1");
			expect(ctx.balances.state.value).toBe("ready");
			expect(ctx.balances.result.value.total).toBe("5.00");
		});

		it("shows nothing from the cache while the status re-read is pending", async () => {
			const ctx = setup();
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			ctx.balances.reset();
			let release;
			ctx.api.getAccountBalancesStatus.mockReturnValueOnce(
				new Promise((resolve) => (release = resolve))
			);
			const loading = ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("loading");
			expect(ctx.balances.result.value).toBeNull();
			release(ok(complete()));
			await loading;
			expect(ctx.balances.state.value).toBe("ready");
		});

		it("drops the cache and asks for a new job when the status re-read says scope_changed", async () => {
			const ctx = setup();
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			ctx.balances.reset();
			ctx.api.getAccountBalancesStatus.mockResolvedValueOnce(
				ok({ state: "failed", code: "scope_changed" })
			);
			await ctx.balances.load("Northstar");
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(2);
			expect(ctx.balances.state.value).toBe("ready");
		});

		it("fails with forbidden, and forgets the cache, when the status re-read is refused", async () => {
			const ctx = setup();
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			ctx.balances.reset();
			ctx.api.getAccountBalancesStatus.mockResolvedValueOnce(
				refusal("forbidden", "forbidden")
			);
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("failed");
			expect(ctx.balances.errorCode.value).toBe("forbidden");
			expect(ctx.balances.result.value).toBeNull();
			await ctx.balances.load("Northstar");
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(2);
		});

		it("requests again after the job expired", async () => {
			const ctx = setup();
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			await vi.advanceTimersByTimeAsync(304000);
			await ctx.balances.load("Northstar");
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(2);
		});

		it("requests again for another company", async () => {
			const ctx = setup();
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			await ctx.balances.load("Other");
			expect(ctx.api.requestAccountBalances).toHaveBeenNthCalledWith(2, "Other");
		});

		it("never reuses a result that carries no expiry", async () => {
			const ctx = setup({
				getAccountBalancesStatus: vi.fn(async () =>
					ok(complete({ expires_at: undefined }))
				),
			});
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			await ctx.balances.load("Northstar");
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(2);
		});
	});

	describe("failures", () => {
		it.each(["worker_busy", "forbidden", "too_large", "network_error"])(
			"stops on a %s refusal at the request",
			async (code) => {
				const ctx = setup({ requestAccountBalances: vi.fn(async () => refusal(code)) });
				scope = ctx.scope;
				await ctx.balances.load("Northstar");
				expect(ctx.balances.state.value).toBe("failed");
				expect(ctx.balances.errorCode.value).toBe(code);
				expect(ctx.api.getAccountBalancesStatus).not.toHaveBeenCalled();
				await vi.advanceTimersByTimeAsync(30000);
				expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(1);
			}
		);

		it("stops on a failed job and keeps its code", async () => {
			const ctx = setup({
				getAccountBalancesStatus: vi.fn(async () =>
					ok({ state: "failed", code: "source_changed" })
				),
			});
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("failed");
			expect(ctx.balances.errorCode.value).toBe("source_changed");
			await vi.advanceTimersByTimeAsync(30000);
			expect(ctx.api.getAccountBalancesStatus).toHaveBeenCalledTimes(1);
		});

		it("discards the job on scope_changed and asks again once, on its own", async () => {
			const status = vi
				.fn()
				.mockResolvedValueOnce(refusal("scope_changed", "scope_changed"))
				.mockResolvedValue(ok(complete()));
			const ctx = setup({ getAccountBalancesStatus: status });
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(2);
			expect(status).toHaveBeenCalledTimes(2);
			expect(ctx.balances.state.value).toBe("ready");
			expect(ctx.balances.errorCode.value).toBe("");
		});

		it("also asks again when the job itself failed with scope_changed", async () => {
			const status = vi
				.fn()
				.mockResolvedValueOnce(ok({ state: "failed", code: "scope_changed" }))
				.mockResolvedValue(ok(complete()));
			const ctx = setup({ getAccountBalancesStatus: status });
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(2);
			expect(ctx.balances.state.value).toBe("ready");
		});

		it("shows the failure after a second scope_changed, and stops", async () => {
			const status = vi.fn(async () => refusal("scope_changed", "scope_changed"));
			const ctx = setup({ getAccountBalancesStatus: status });
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("failed");
			expect(ctx.balances.errorCode.value).toBe("scope_changed");
			await vi.advanceTimersByTimeAsync(30000);
			expect(ctx.api.requestAccountBalances).toHaveBeenCalledTimes(2);
			expect(status).toHaveBeenCalledTimes(2);
		});

		it("lets a person try again after that failure with a fresh single retry", async () => {
			const status = vi
				.fn()
				.mockResolvedValueOnce(refusal("scope_changed", "scope_changed"))
				.mockResolvedValueOnce(refusal("scope_changed", "scope_changed"))
				.mockResolvedValue(ok(complete()));
			const ctx = setup({ getAccountBalancesStatus: status });
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("failed");
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("ready");
		});

		it("fails with internal_error when the request names no job", async () => {
			const ctx = setup({
				requestAccountBalances: vi.fn(async () => ok({ state: "queued" })),
			});
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.balances.errorCode.value).toBe("internal_error");
		});

		it("fails with internal_error on a complete job without accounts or total", async () => {
			const ctx = setup({
				getAccountBalancesStatus: vi.fn(async () => ok(complete({ total: null }))),
			});
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("failed");
			expect(ctx.balances.errorCode.value).toBe("internal_error");
			expect(ctx.balances.result.value).toBeNull();
		});

		it("gives up with a timeout after 15 minutes", async () => {
			const ctx = setup({ getAccountBalancesStatus: vi.fn(async () => running()) });
			scope = ctx.scope;
			void ctx.balances.load("Northstar");
			await vi.advanceTimersByTimeAsync(905000);
			expect(ctx.balances.state.value).toBe("failed");
			expect(ctx.balances.errorCode.value).toBe("timeout");
		});

		it("can retry after a failure", async () => {
			const ctx = setup({
				requestAccountBalances: vi
					.fn()
					.mockResolvedValueOnce(refusal("worker_busy"))
					.mockResolvedValue(ok({ job_id: "job-2", state: "queued" })),
			});
			scope = ctx.scope;
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("failed");
			await ctx.balances.load("Northstar");
			expect(ctx.balances.state.value).toBe("ready");
			expect(ctx.balances.errorCode.value).toBe("");
		});
	});

	describe("stale replies", () => {
		it("reset stops the polling and drops a late reply", async () => {
			let release;
			const late = new Promise((resolve) => (release = resolve));
			const ctx = setup({ getAccountBalancesStatus: vi.fn(() => late) });
			scope = ctx.scope;
			void ctx.balances.load("Northstar");
			await vi.advanceTimersByTimeAsync(0);
			ctx.balances.reset();
			release(ok(complete()));
			await vi.advanceTimersByTimeAsync(10000);
			expect(ctx.balances.state.value).toBe("idle");
			expect(ctx.balances.result.value).toBeNull();
			expect(ctx.api.getAccountBalancesStatus).toHaveBeenCalledTimes(1);
		});

		it("a newer load replaces the older one", async () => {
			const first = new Promise(() => {});
			const requests = [first, Promise.resolve(ok({ job_id: "job-2", state: "queued" }))];
			const ctx = setup({ requestAccountBalances: vi.fn(() => requests.shift()) });
			scope = ctx.scope;
			void ctx.balances.load("Northstar");
			await ctx.balances.load("Other");
			expect(ctx.api.getAccountBalancesStatus).toHaveBeenCalledWith("job-2");
			expect(ctx.balances.result.value.total).toBe("221860.00");
		});

		it("stops polling when its scope is disposed", async () => {
			const ctx = setup({ getAccountBalancesStatus: vi.fn(async () => running()) });
			scope = ctx.scope;
			void ctx.balances.load("Northstar");
			await vi.advanceTimersByTimeAsync(1500);
			const calls = ctx.api.getAccountBalancesStatus.mock.calls.length;
			scope.stop();
			await vi.advanceTimersByTimeAsync(30000);
			expect(ctx.api.getAccountBalancesStatus.mock.calls.length).toBe(calls);
		});
	});
});
