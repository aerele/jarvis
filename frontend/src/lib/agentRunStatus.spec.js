import { describe, it, expect } from "vitest";
import { STATUS_THEME } from "./agentRunStatus";

describe("STATUS_THEME", () => {
	it("covers every Jarvis Agent Run status with the app's theme colours", () => {
		expect(STATUS_THEME).toEqual({
			running: "blue",
			completed: "green",
			partial: "orange",
			failed: "red",
			stopped: "gray",
		});
	});
});
