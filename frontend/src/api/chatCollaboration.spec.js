import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
	sendSharedMessage,
	confirmSharedAction,
	setChatRecipients,
	uploadSharedFile,
	getCollaboration,
} from "./chatCollaboration";
beforeEach(() => {
	window.csrf_token = "test-session-csrf";
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => ({ ok: true, json: async () => ({ message: { ok: true } }) }))
	);
});
afterEach(() => {
	vi.unstubAllGlobals();
	delete window.csrf_token;
});
describe("explicit shared APIs", () => {
	it("posts the request and nonce without any client-selected execution identity", async () => {
		await sendSharedMessage("C1", "My request", "nonce", [
			{ file_url: "/private/files/mine.txt" },
		]);
		const [url, options] = fetch.mock.calls[0];
		expect(url).toBe("/api/method/jarvis.chat.collaboration_api.send_message");
		expect(JSON.parse(options.body)).toEqual({
			conversation: "C1",
			message: "My request",
			request_id: "nonce",
			attachments: [{ file_url: "/private/files/mine.txt" }],
		});
		expect(options.credentials).toBe("same-origin");
		expect(options.headers["X-Frappe-CSRF-Token"]).toBe("test-session-csrf");
	});
	it("saves membership and modes together using the dedicated endpoint", async () => {
		await setChatRecipients("C1", ["a@example.com", "b@example.com"], {
			"a@example.com": "Chat",
		});
		expect(fetch.mock.calls[0][0]).toBe(
			"/api/method/jarvis.chat.collaboration_access.set_recipients"
		);
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
			conversation: "C1",
			recipients: [
				{ user: "a@example.com", access_mode: "Chat" },
				{ user: "b@example.com", access_mode: "View" },
			],
		});
	});
	it("confirms a specific action through its share-gated API", async () => {
		await confirmSharedAction("C1", "PA1");
		expect(fetch.mock.calls[0][0]).toBe(
			"/api/method/jarvis.chat.collaboration_actions.confirm_action"
		);
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
			conversation: "C1",
			action: "PA1",
		});
	});
	it("uploads multipart files without overriding the server's privacy policy", async () => {
		await uploadSharedFile("C1", new File(["mine"], "mine.txt"));
		const [url, options] = fetch.mock.calls[0];
		expect(url).toBe("/api/method/jarvis.chat.collaboration_api.upload_file");
		expect([...options.body.keys()]).toEqual(["conversation", "file"]);
		expect(options.headers["Content-Type"]).toBeUndefined();
	});
	it("retains a typed denial for the view to clear old content", async () => {
		fetch.mockResolvedValue({
			ok: false,
			status: 403,
			json: async () => ({ exc_type: "PermissionError" }),
		});
		await expect(getCollaboration("C1")).rejects.toMatchObject({
			status: 403,
			exc_type: "PermissionError",
		});
	});
});
