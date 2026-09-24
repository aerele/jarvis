import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { BOOT_TIMEOUT_MS, withDeadline } from "./bootDeadline.js";

describe("withDeadline (ChatView boot requests)", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it("resolves undefined and reports the timeout when the request never settles", async () => {
		const onTimeout = vi.fn();
		const result = withDeadline(new Promise(() => {}), BOOT_TIMEOUT_MS, onTimeout);
		await vi.advanceTimersByTimeAsync(BOOT_TIMEOUT_MS);
		await expect(result).resolves.toBeUndefined();
		expect(onTimeout).toHaveBeenCalledTimes(1);
	});

	it("passes a value through and never reports a timeout once it settles in time", async () => {
		const onTimeout = vi.fn();
		const result = withDeadline(Promise.resolve({ ok: 1 }), BOOT_TIMEOUT_MS, onTimeout);
		await expect(result).resolves.toEqual({ ok: 1 });
		await vi.advanceTimersByTimeAsync(BOOT_TIMEOUT_MS * 2);
		expect(onTimeout).not.toHaveBeenCalled();
	});

	it("propagates a rejection so the caller's own error handling still runs", async () => {
		const onTimeout = vi.fn();
		const result = withDeadline(Promise.reject(new Error("gone")), BOOT_TIMEOUT_MS, onTimeout);
		await expect(result).rejects.toThrow("gone");
		await vi.advanceTimersByTimeAsync(BOOT_TIMEOUT_MS * 2);
		expect(onTimeout).not.toHaveBeenCalled();
	});
});
