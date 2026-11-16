import { describe, it, expect } from "vitest";
import {
	MIC_BLOCKED_MESSAGE,
	RECORDING_FAILED_MESSAGE,
	actionErrorCopy,
	causeOf,
	micErrorCopy,
	thrownActionError,
	uploadErrorCopy,
} from "./chatErrorCopy";
import { OFFLINE_MESSAGE } from "./errors";

const net = new TypeError("Failed to fetch");
const server = (message, extra = {}) =>
	Object.assign(new Error("raw"), { messages: [message], ...extra });

describe("actionErrorCopy", () => {
	it("names the action and says to retry when nothing readable came back", () => {
		expect(actionErrorCopy("Couldn't compact this chat", new Error("boom"))).toBe(
			"Couldn't compact this chat. Try again in a moment."
		);
	});
	it("never quotes a raw exception message", () => {
		expect(
			actionErrorCopy("Couldn't stop that run", new Error("TimeoutError: socket hang up"))
		).not.toContain("socket");
	});
	it("a network failure gets the offline sentence, not Failed to fetch", () => {
		const out = actionErrorCopy("Couldn't stop that run", net);
		expect(out).toBe(`Couldn't stop that run. ${OFFLINE_MESSAGE}`);
	});
	it("quotes what the server said and retries", () => {
		expect(actionErrorCopy("Couldn't run that again", server("The macro was removed."))).toBe(
			"Couldn't run that again: The macro was removed. Try again."
		);
	});
	it("a permission error points at the admin, not retry", () => {
		document.cookie = "user_id=a%40b.com";
		const out = actionErrorCopy(
			"Couldn't run that again",
			server("You can't run macros.", { exc_type: "PermissionError", status: 403 })
		);
		expect(out).toContain("Ask your workspace admin if you need access.");
		expect(out).not.toContain("Try again");
		document.cookie = "user_id=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
	});
	it("a caller-supplied cause is used with the retry step", () => {
		expect(
			actionErrorCopy(
				"Couldn't change the model for this chat",
				new Error("rejected"),
				"That model is not available."
			)
		).toBe("Couldn't change the model for this chat: That model is not available. Try again.");
	});
});

describe("causeOf and thrownActionError", () => {
	it("is empty for an unreadable error", () => {
		expect(causeOf(new Error("x"))).toBe("");
	});
	it("flags an unreadable failure as internal so the card offers Report error", () => {
		expect(thrownActionError(new Error("x"), "Could not confirm this action.")).toEqual({
			code: "InternalError",
			message: "Could not confirm this action.",
			hint: "Try again in a moment.",
		});
	});
	it("keeps a server sentence as a plain, non-internal message", () => {
		expect(thrownActionError(server("Due Date is required."), "fallback")).toEqual({
			message: "Due Date is required.",
		});
	});
	it("a 5xx is internal even with a server sentence", () => {
		expect(
			thrownActionError(server("Oops", { status: 500 }), "Could not discard this action.")
				.code
		).toBe("InternalError");
	});
});

describe("uploadErrorCopy", () => {
	it("too large from the pre-check names the limit", () => {
		expect(uploadErrorCopy(new Error("larger than the 25 MB limit"))).toBe(
			"That file is larger than 25 MB. Choose a smaller file."
		);
	});
	it("maps 413 to a smaller-file step", () => {
		expect(uploadErrorCopy(new Error("upload failed (413)"))).toBe(
			"That file is too large. Choose a smaller file and try again."
		);
	});
	it("maps a blocked type to the admin step", () => {
		expect(uploadErrorCopy(new Error("File type of x.exe is not allowed"))).toContain(
			"ask your workspace admin which types are allowed"
		);
	});
	it("maps offline and 401", () => {
		expect(uploadErrorCopy(net)).toBe(OFFLINE_MESSAGE);
		expect(uploadErrorCopy(new Error("upload failed (401)"))).toContain("sign in again");
	});
	it("falls back to a plain sentence for anything else", () => {
		expect(uploadErrorCopy(new Error("upload failed (500)"))).toBe(
			"Couldn't attach that file. Try again."
		);
	});
});

describe("micErrorCopy", () => {
	it("a denied microphone says where to allow it", () => {
		expect(
			micErrorCopy(
				"Microphone access is blocked. Allow the microphone for this site and try again."
			)
		).toBe(MIC_BLOCKED_MESSAGE);
		expect(MIC_BLOCKED_MESSAGE).toBe(
			"Allow microphone access in your browser settings, then try again."
		);
	});
	it("keeps another plain recorder sentence and falls back when empty", () => {
		expect(micErrorCopy("No microphone was found on this device.")).toBe(
			"No microphone was found on this device."
		);
		expect(micErrorCopy("")).toBe(RECORDING_FAILED_MESSAGE);
	});
});
