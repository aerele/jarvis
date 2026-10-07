import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createReplySettle } from "./replySettle";

describe("createReplySettle", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it("settles with the last delta's time once the text goes quiet, before any run:end", () => {
		const onSettle = vi.fn();
		const s = createReplySettle({ onSettle, quietMs: 600 });
		s.touch("m1", 1000);
		vi.advanceTimersByTime(300);
		s.touch("m1", 1300);
		vi.advanceTimersByTime(599);
		expect(onSettle).not.toHaveBeenCalled();
		vi.advanceTimersByTime(1);
		expect(onSettle).toHaveBeenCalledOnce();
		expect(onSettle).toHaveBeenCalledWith("m1", 1300);
	});

	it("reports a resumed answer so the bar can step back", () => {
		const onSettle = vi.fn();
		const onResume = vi.fn();
		const s = createReplySettle({ onSettle, onResume, quietMs: 600 });
		s.touch("m1");
		vi.advanceTimersByTime(600);
		s.touch("m1");
		expect(onResume).toHaveBeenCalledWith("m1");
	});

	it("clear() cancels a pending settle", () => {
		const onSettle = vi.fn();
		const s = createReplySettle({ onSettle, quietMs: 600 });
		s.touch("m1");
		s.clear("m1");
		vi.advanceTimersByTime(2000);
		expect(onSettle).not.toHaveBeenCalled();
	});
});
