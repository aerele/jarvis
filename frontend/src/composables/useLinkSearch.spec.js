import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useLinkSearch } from "./useLinkSearch";

// jarvis#1062: the debounced + monotonic-sequence + prime-on-focus remote
// search pattern, extracted from ConfigForm.vue, AgentAccessEditor.vue and
// FilterValueControl.vue into one composable. These specs cover the
// composable directly; each call site keeps its own specs unchanged to lock
// in "zero behaviour change" from the refactor.
describe("useLinkSearch", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it("prime() fetches immediately with the current (empty) query, once per prime cycle", async () => {
		const fetcher = vi.fn().mockResolvedValue([{ value: "a" }]);
		const s = useLinkSearch(fetcher);
		s.prime();
		s.prime(); // no-op, already primed
		await vi.runAllTimersAsync();
		expect(fetcher).toHaveBeenCalledTimes(1);
		expect(fetcher).toHaveBeenCalledWith("");
		expect(s.options.value).toEqual([{ value: "a" }]);
	});

	it("debounces onQuery - no fetch until the debounce interval elapses, then once", async () => {
		const fetcher = vi.fn().mockResolvedValue([]);
		const s = useLinkSearch(fetcher, { debounceMs: 300 });
		s.onQuery("a");
		s.onQuery("ab");
		s.onQuery("abc");
		expect(fetcher).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(300);
		expect(fetcher).toHaveBeenCalledTimes(1);
		expect(fetcher).toHaveBeenCalledWith("abc");
	});

	it("honours a custom debounceMs", async () => {
		const fetcher = vi.fn().mockResolvedValue([]);
		const s = useLinkSearch(fetcher, { debounceMs: 200 });
		s.onQuery("x");
		await vi.advanceTimersByTimeAsync(199);
		expect(fetcher).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(fetcher).toHaveBeenCalledTimes(1);
	});

	it("ignores a stale response that resolves after a newer request", async () => {
		let resolveFirst;
		const fetcher = vi
			.fn()
			.mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
			.mockResolvedValueOnce([{ value: "second" }]);
		const s = useLinkSearch(fetcher, { debounceMs: 0 });

		s.onQuery("first");
		await vi.advanceTimersByTimeAsync(0);
		s.onQuery("second");
		await vi.advanceTimersByTimeAsync(0);
		// "second" resolves before "first" does
		await Promise.resolve();
		expect(s.options.value).toEqual([{ value: "second" }]);

		resolveFirst([{ value: "first-stale" }]);
		await Promise.resolve();
		await Promise.resolve();
		// the late "first" response must not clobber the newer "second" result
		expect(s.options.value).toEqual([{ value: "second" }]);
	});

	it("applies mapper to raw rows and passes an empty array through unchanged when the fetcher returns nothing", async () => {
		const fetcher = vi.fn().mockResolvedValue(null);
		const s = useLinkSearch(fetcher, { mapper: (rows) => rows.map((r) => r.value) });
		s.prime();
		await vi.runAllTimersAsync();
		expect(s.options.value).toEqual([]);
	});

	it("clears options and swallows the error on a rejected fetch", async () => {
		const fetcher = vi.fn().mockRejectedValue(new Error("boom"));
		const s = useLinkSearch(fetcher);
		s.prime();
		await vi.runAllTimersAsync();
		expect(s.options.value).toEqual([]);
	});

	it("calls onSettled with the resolved rows and query, but not for a superseded response", async () => {
		let resolveFirst;
		const fetcher = vi
			.fn()
			.mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
			.mockResolvedValueOnce([{ value: "b" }]);
		const onSettled = vi.fn();
		const s = useLinkSearch(fetcher, { debounceMs: 0, onSettled });

		s.onQuery("a");
		await vi.advanceTimersByTimeAsync(0);
		s.onQuery("b");
		await vi.advanceTimersByTimeAsync(0);
		await Promise.resolve();
		expect(onSettled).toHaveBeenCalledTimes(1);
		expect(onSettled).toHaveBeenCalledWith({
			rows: [{ value: "b" }],
			query: "b",
			error: null,
		});

		resolveFirst([{ value: "a-stale" }]);
		await Promise.resolve();
		await Promise.resolve();
		expect(onSettled).toHaveBeenCalledTimes(1); // the stale "a" settle never fires
	});

	it("reprime() clears state and un-primes, so the next prime() fetches again", async () => {
		const fetcher = vi.fn().mockResolvedValue([{ value: "a" }]);
		const s = useLinkSearch(fetcher);
		s.prime();
		await vi.runAllTimersAsync();
		expect(fetcher).toHaveBeenCalledTimes(1);

		s.reprime();
		expect(s.options.value).toEqual([]);
		expect(s.query.value).toBe("");

		s.prime();
		await vi.runAllTimersAsync();
		expect(fetcher).toHaveBeenCalledTimes(2);
	});

	it("reprime() cancels a pending debounced search so it never fires", async () => {
		const fetcher = vi.fn().mockResolvedValue([]);
		const s = useLinkSearch(fetcher, { debounceMs: 300 });
		s.onQuery("partial");
		s.reprime();
		await vi.advanceTimersByTimeAsync(1000);
		expect(fetcher).not.toHaveBeenCalled();
	});

	it("cleanup() cancels a pending debounced search", async () => {
		const fetcher = vi.fn().mockResolvedValue([]);
		const s = useLinkSearch(fetcher, { debounceMs: 300 });
		s.onQuery("partial");
		s.cleanup();
		await vi.advanceTimersByTimeAsync(1000);
		expect(fetcher).not.toHaveBeenCalled();
	});
});
