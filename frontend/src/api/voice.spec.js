import { describe, expect, it, vi } from "vitest";

vi.mock("frappe-ui", () => ({ call: vi.fn(async () => ({})) }));

import { TRANSCRIPTION_TIMEOUT_MS, transcribeAudio } from "./voice.js";

describe("voice transcription request budget", () => {
	it("gives every recorder 150 seconds by default", () => {
		expect(TRANSCRIPTION_TIMEOUT_MS).toBe(150000);
	});

	it("does not abort an unresolved default request at the old 25-second limit", async () => {
		vi.useFakeTimers();
		const originalFetch = globalThis.fetch;
		const originalWindow = globalThis.window;
		const fetchMock = vi.fn((_url, options) => {
			return new Promise((_resolve, reject) => {
				options.signal.addEventListener("abort", () => reject(new Error("aborted")));
			});
		});
		globalThis.fetch = fetchMock;
		globalThis.window = { csrf_token: "test-csrf" };
		try {
			const pending = transcribeAudio(new Blob(["audio"], { type: "audio/webm" }), {
				durationS: 1,
			});
			let settled = false;
			void pending.then(
				() => {
					settled = true;
				},
				() => {
					settled = true;
				}
			);
			await vi.advanceTimersByTimeAsync(25000);
			expect(settled).toBe(false);
			await vi.advanceTimersByTimeAsync(125000);
			await expect(pending).rejects.toThrow("transcription timed out");
		} finally {
			globalThis.fetch = originalFetch;
			globalThis.window = originalWindow;
			vi.useRealTimers();
		}
	});
});
