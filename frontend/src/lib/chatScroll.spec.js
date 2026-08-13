import { describe, it, expect } from "vitest";
import { shouldFollowBottom } from "./chatScroll";

/**
 * Regression guard for the "text keeps moving up" chat-scroll bug.
 *
 * While a reply streams, the view must NOT chase the bottom on every chunk — doing
 * so dragged the line being read up and off the top of a long answer. Following is
 * allowed ONLY when the reader is parked at the bottom AND the turn is fully settled
 * (not streaming AND the paced reveal has drained). The load-bearing case is #4: an
 * earlier fix keyed only on the reveal queue, which EMPTIES between deltas, so each
 * gap re-enabled following mid-stream and it still dragged. `streaming` stays true
 * across the whole turn and is what closes that hole.
 */
describe("shouldFollowBottom", () => {
	it("follows when pinned and the turn is fully settled", () => {
		expect(shouldFollowBottom({ pinned: true, streaming: false, revealPending: 0 })).toBe(
			true
		);
	});

	it("does NOT follow when the reader has scrolled up (not pinned)", () => {
		expect(shouldFollowBottom({ pinned: false, streaming: false, revealPending: 0 })).toBe(
			false
		);
	});

	it("does NOT follow while a reply is streaming, even if pinned", () => {
		expect(shouldFollowBottom({ pinned: true, streaming: true, revealPending: 5 })).toBe(
			false
		);
	});

	// The specific regression: mid-stream, the paced reveal has momentarily drained
	// (revealPending === 0) in the gap between two deltas. Following here is exactly
	// what dragged the reader; `streaming` must still block it.
	it("does NOT follow between deltas mid-stream (reveal drained but still streaming)", () => {
		expect(shouldFollowBottom({ pinned: true, streaming: true, revealPending: 0 })).toBe(
			false
		);
	});

	// The trailing reveal animation can outlive run:end (streaming already false), so
	// a non-empty reveal queue must still hold following off until it drains.
	it("does NOT follow during the post-run:end reveal tail", () => {
		expect(shouldFollowBottom({ pinned: true, streaming: false, revealPending: 3 })).toBe(
			false
		);
	});

	it("resumes following once settled, so late images keep a pinned reader at the bottom", () => {
		// Same inputs as a late image/chart load after the turn ended.
		expect(shouldFollowBottom({ pinned: true, streaming: false, revealPending: 0 })).toBe(
			true
		);
	});
});
