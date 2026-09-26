import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { BADGE_SLACK_MS, createBadgeTimer } from "./badgeTimer";

describe("createBadgeTimer", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it("refreshes once the next card crosses the delay, not before", () => {
		const refresh = vi.fn();
		createBadgeTimer(refresh).schedule(5);
		vi.advanceTimersByTime(5000 + BADGE_SLACK_MS - 1);
		expect(refresh).not.toHaveBeenCalled();
		vi.advanceTimersByTime(1);
		expect(refresh).toHaveBeenCalledTimes(1);
	});

	it("keeps only the latest schedule", () => {
		const refresh = vi.fn();
		const timer = createBadgeTimer(refresh);
		timer.schedule(500);
		timer.schedule(2);
		vi.advanceTimersByTime(600 * 1000);
		expect(refresh).toHaveBeenCalledTimes(1);
	});

	it("does nothing when no card is on the way, and clear cancels", () => {
		const refresh = vi.fn();
		const timer = createBadgeTimer(refresh);
		for (const none of [null, undefined, "x", -1]) timer.schedule(none);
		timer.schedule(3);
		timer.clear();
		vi.advanceTimersByTime(600 * 1000);
		expect(refresh).not.toHaveBeenCalled();
	});
});
