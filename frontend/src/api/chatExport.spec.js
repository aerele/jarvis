import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("frappe-ui", () => ({ call: vi.fn() }));
import { downloadConversation } from "../api";

describe("downloadConversation", () => {
	let anchorClick;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.stubGlobal("fetch", vi.fn());
		URL.createObjectURL = vi.fn(() => "blob:chat-download");
		URL.revokeObjectURL = vi.fn();
		anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
	});
	afterEach(() => {
		vi.runOnlyPendingTimers();
		vi.useRealTimers();
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});
	it.each([
		["pdf", "application/pdf"],
		["md", "text/markdown; charset=utf-8"],
	])("fetches an authenticated GET and downloads the returned %s blob", async (format, type) => {
		const blob = new Blob(["export bytes"], { type });
		fetch.mockResolvedValue({
			ok: true,
			headers: new Headers({
				"Content-Disposition": `attachment; filename="chat-2026-10-16.${format}"`,
				"Content-Type": type,
			}),
			blob: vi.fn().mockResolvedValue(blob),
		});
		await downloadConversation("chat /?&secret", format);
		const [url, options] = fetch.mock.calls[0];
		expect(url.split("?")[0]).toBe("/api/method/jarvis.chat.export.download_conversation");
		expect(new URL(url, "https://site.example").searchParams.get("conversation")).toBe(
			"chat /?&secret"
		);
		expect(new URL(url, "https://site.example").searchParams.get("format")).toBe(format);
		expect(options).toMatchObject({ method: "GET", credentials: "same-origin" });
		expect(URL.createObjectURL).toHaveBeenCalledWith(blob);
		expect(anchorClick).toHaveBeenCalledTimes(1);
		const anchor = anchorClick.mock.instances[0];
		expect(anchor.download).toBe(`chat-2026-10-16.${format}`);
		expect(document.body.contains(anchor)).toBe(false);
		vi.runOnlyPendingTimers();
		expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:chat-download");
	});
	it("refuses an HTTP error without downloading its JSON as a file", async () => {
		fetch.mockResolvedValue({
			ok: false,
			status: 429,
			json: vi.fn().mockResolvedValue({
				_server_messages: JSON.stringify([
					JSON.stringify({ message: "Try Markdown instead." }),
				]),
			}),
		});
		await expect(downloadConversation("chat", "pdf")).rejects.toMatchObject({
			status: 429,
			message: "Try Markdown instead.",
		});
		expect(anchorClick).not.toHaveBeenCalled();
		expect(URL.createObjectURL).not.toHaveBeenCalled();
	});
	it.each([
		[401, "AuthenticationError", true],
		[403, "PermissionError", true],
		[404, "DoesNotExistError", true],
		[429, "RateLimitExceededError", true],
		[429, "_ExportRateLimitError", true],
		[417, "_ExportRefusedError", true],
		[409, "_ExportRefusedError", true],
		[417, "ValidationError", false],
		[417, "_ExportRenderError", false],
		[500, "_ExportRefusedError", false],
		[502, "RateLimitExceededError", false],
	])(
		"classifies HTTP%s / %s without retaining the server payload",
		async (status, excType, expectedRefusal) => {
			fetch.mockResolvedValue({
				ok: false,
				status,
				json: vi.fn().mockResolvedValue({
					exc_type: excType,
					exception: "RAW-SERVER-EXCEPTION-SENTINEL",
					exc: ["RAW-STACK-SENTINEL"],
					_server_messages: JSON.stringify([
						JSON.stringify({ message: "Actionable export message" }),
					]),
				}),
			});
			const error = await downloadConversation("chat", "pdf").catch((failure) => failure);
			expect(error).toMatchObject({
				status,
				exc_type: excType,
				expectedRefusal,
				message: "Actionable export message",
			});
			expect(JSON.stringify(error)).not.toContain("SENTINEL");
			expect(error).not.toHaveProperty("exception");
			expect(error).not.toHaveProperty("exc");
			expect(anchorClick).not.toHaveBeenCalled();
		}
	);
	it.each(["<img onerror=alert(1)>", "x".repeat(200), { name: "_ExportRefusedError" }])(
		"does not propagate malformed exception class %s",
		async (excType) => {
			fetch.mockResolvedValue({
				ok: false,
				status: 417,
				json: vi.fn().mockResolvedValue({ exc_type: excType }),
			});
			const error = await downloadConversation("chat", "pdf").catch((failure) => failure);
			expect(error.exc_type).toBeUndefined();
			expect(error.expectedRefusal).toBe(false);
			expect(anchorClick).not.toHaveBeenCalled();
		}
	);
	it("does not expose an HTML proxy error or raw server exception", async () => {
		fetch.mockResolvedValue({
			ok: false,
			status: 502,
			json: vi.fn().mockRejectedValue(new Error("raw upstream secret")),
		});
		await expect(downloadConversation("chat", "pdf")).rejects.toMatchObject({
			status: 502,
			message: "Couldn't download this chat. Try again.",
		});
		expect(anchorClick).not.toHaveBeenCalled();
	});
	it("uses a local fallback filename rather than a path from the response header", async () => {
		fetch.mockResolvedValue({
			ok: true,
			headers: new Headers({
				"Content-Disposition": 'attachment; filename="../../private/export.pdf"',
				"Content-Type": "application/pdf",
			}),
			blob: vi.fn().mockResolvedValue(new Blob(["pdf"])),
		});
		await downloadConversation("chat", "pdf");
		expect(anchorClick.mock.instances[0].download).toBe("jarvis-chat.pdf");
	});
	it("refuses a successful login or error page instead of saving it as a PDF", async () => {
		fetch.mockResolvedValue({
			ok: true,
			headers: new Headers({ "Content-Type": "text/html" }),
			blob: vi.fn().mockResolvedValue(new Blob(["<html>Sign in</html>"])),
		});
		await expect(downloadConversation("chat", "pdf")).rejects.toThrow(
			"Couldn't download this chat. Try again."
		);
		expect(anchorClick).not.toHaveBeenCalled();
	});
});
