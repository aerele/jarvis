import { describe, expect, it } from "vitest";
import { conversationLoadFailure } from "./conversationLoadFailure";

const current = {
	requestedId: "chat-a",
	currentId: "chat-a",
	loadId: 2,
	latestLoadId: 2,
	viewActive: true,
};

describe("conversation load failure decision", () => {
	it.each(["PermissionError", "DoesNotExistError"])(
		"returns unavailable only for the current %s",
		(exc_type) => {
			expect(conversationLoadFailure({ exc_type }, current)).toBe("unavailable");
		}
	);
	it.each([
		{ exc_type: "ValidationError", status: 417 },
		{ exc_type: "CSRFTokenError", status: 400 },
		{ status: 403 },
		{ status: 404 },
		{ status: 500 },
		new Error("PermissionError"),
		new TypeError("Failed to fetch"),
		null,
	])("preserves handling for other failures: %s", (error) => {
		expect(conversationLoadFailure(error, current)).toBe("throw");
	});
	it.each(["PermissionError", "DoesNotExistError", "ValidationError"])(
		"ignores an old %s after selecting another chat or home",
		(exc_type) => {
			for (const currentId of ["chat-b", null]) {
				expect(conversationLoadFailure({ exc_type }, { ...current, currentId })).toBe(
					"ignore"
				);
			}
		}
	);
	it("ignores an older load even after returning to that same chat", () => {
		expect(
			conversationLoadFailure({ exc_type: "PermissionError" }, { ...current, loadId: 1 })
		).toBe("ignore");
	});
	it("ignores a refusal after leaving the view", () => {
		expect(
			conversationLoadFailure(
				{ exc_type: "PermissionError" },
				{ ...current, viewActive: false }
			)
		).toBe("ignore");
	});
});
