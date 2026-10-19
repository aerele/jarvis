import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ref } from "vue";

vi.mock("@/api", () => ({ downloadConversation: vi.fn() }));
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn() } }));
vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));
import { downloadConversation } from "@/api";
import { toast } from "frappe-ui";
import { report } from "@/lib/errorReporter";
import { useConversationExport } from "./useConversationExport";

describe("chat export menu state", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		document.cookie = "user_id=export-owner@example.com; path=/";
	});
	afterEach(() => {
		document.cookie = "user_id=; max-age=0; path=/";
	});
	it.each([
		["pdf", "Preparing PDF…", 0],
		["md", "Preparing Markdown…", 1],
	])(
		"keeps %s pending until the download resolves and ignores repeat clicks from either menu",
		async (format, label, index) => {
			let finish;
			downloadConversation.mockImplementation(
				() => new Promise((resolve) => (finish = resolve))
			);
			const row = useConversationExport(() => "pending-" + format);
			const header = useConversationExport(() => "pending-" + format);
			const pending = row.downloadOptions.value[index].onClick();
			expect(downloadConversation).toHaveBeenCalledWith("pending-" + format, format);
			expect(row.downloadOptions.value[index].label).toBe(label);
			expect(header.downloadOptions.value[index].label).toBe(label);
			expect(row.downloadOptions.value.every((option) => option.disabled)).toBe(true);
			await header.downloadOptions.value[1 - index].onClick();
			await row.downloadOptions.value[index].onClick();
			expect(downloadConversation).toHaveBeenCalledTimes(1);
			finish();
			await pending;
			expect(row.downloadOptions.value.map((option) => option.label)).toEqual([
				"Download as PDF",
				"Download as Markdown",
			]);
			expect(row.downloadOptions.value.every((option) => !option.disabled)).toBe(true);
		}
	);
	it("escapes toast HTML, reports no transcript/server payload, and allows retry after failure", async () => {
		downloadConversation.mockRejectedValue(
			new Error('&lt;img src=x onerror="alert(1)"&gt; Try Markdown instead.')
		);
		const menu = useConversationExport(() => "failed-chat");
		await menu.downloadOptions.value[0].onClick();
		expect(toast.error).toHaveBeenCalledTimes(1);
		expect(toast.error.mock.calls[0][0]).toContain("&lt;img");
		expect(toast.error.mock.calls[0][0]).not.toContain("<img");
		expect(report).toHaveBeenCalledWith(
			expect.objectContaining({
				conversation: "failed-chat",
				error_code: "chat_export",
				message: "Chat PDF export failed",
			})
		);
		expect(JSON.stringify(report.mock.calls)).not.toContain("onerror");
		downloadConversation.mockResolvedValue(undefined);
		await menu.downloadOptions.value[1].onClick();
		expect(downloadConversation).toHaveBeenCalledTimes(2);
	});
	it.each([
		[429, "RateLimitExceededError", "Too many exports. Try again in a minute."],
		[429, "_ExportRateLimitError", "Too many exports are running. Try again in a moment."],
		[417, "_ExportRefusedError", "An export is already running. Try again in a moment."],
		[
			417,
			"_ExportRefusedError",
			"This chat is too long for a PDF. Download it as Markdown instead.",
		],
		[403, "PermissionError", "This chat isn't available."],
	])(
		"shows an actionable toast for expected HTTP%s / %s without error telemetry",
		async (status, excType, message) => {
			downloadConversation.mockRejectedValue(
				Object.assign(new Error(message), {
					status,
					exc_type: excType,
					expectedRefusal: true,
				})
			);
			const menu = useConversationExport(() => "expected-refusal");
			await menu.downloadOptions.value[0].onClick();
			expect(toast.error).toHaveBeenCalledTimes(1);
			expect(toast.error.mock.calls[0][0]).toContain(message.replaceAll("'", "&#39;"));
			expect(report).not.toHaveBeenCalled();
			expect(menu.downloadOptions.value.every((option) => !option.disabled)).toBe(true);
		}
	);
	it.each([
		[undefined, undefined, "Network failed with PRIVATE-CONTENT-SENTINEL"],
		[417, "_ExportRenderError", "We couldn't create the PDF. Try Markdown instead."],
		[500, "_ExportRefusedError", "Unexpected server failure PRIVATE-CONTENT-SENTINEL"],
	])(
		"reports unexpected HTTP%s / %s once without including server or network details",
		async (status, excType, message) => {
			downloadConversation.mockRejectedValue(
				Object.assign(new Error(message), {
					status,
					exc_type: excType,
					expectedRefusal: false,
				})
			);
			const menu = useConversationExport(() => "unexpected-export-failure");
			await menu.downloadOptions.value[0].onClick();
			expect(toast.error).toHaveBeenCalledTimes(1);
			expect(report).toHaveBeenCalledTimes(1);
			expect(report).toHaveBeenCalledWith(
				expect.objectContaining({
					error_code: "chat_export",
					error_class: "ChatExportError",
					message: "Chat PDF export failed",
				})
			);
			expect(JSON.stringify(report.mock.calls)).not.toContain("PRIVATE-CONTENT-SENTINEL");
		}
	);
	it("uses the selected chat, and disables export before a chat exists", async () => {
		downloadConversation.mockResolvedValue(undefined);
		const conversation = ref(null);
		const menu = useConversationExport(() => conversation.value);
		expect(menu.downloadOptions.value.every((option) => option.disabled)).toBe(true);
		await menu.downloadOptions.value[0].onClick();
		expect(downloadConversation).not.toHaveBeenCalled();
		conversation.value = "selected-chat";
		await menu.downloadOptions.value[1].onClick();
		expect(downloadConversation).toHaveBeenCalledWith("selected-chat", "md");
	});
});

describe("grouped chat download menu", () => {
	beforeEach(() => vi.clearAllMocks());
	it("keeps library menu identities stable when progress changes the visible labels", async () => {
		let finish;
		downloadConversation.mockImplementation(
			() => new Promise((resolve) => (finish = resolve))
		);
		const menu = useConversationExport(() => "stable-menu");
		const identities = () => [
			menu.downloadMenuOption.value.value,
			...menu.downloadMenuOption.value.submenu.map((option) => option.value),
		];
		expect(identities()).toEqual(["download", "pdf", "md"]);
		const request = menu.downloadMenuOption.value.submenu[0].onClick();
		try {
			expect(menu.downloadMenuOption.value.label).toBe("Preparing PDF…");
			expect(identities()).toEqual(["download", "pdf", "md"]);
		} finally {
			finish();
			await request;
		}
		expect(menu.downloadMenuOption.value.label).toBe("Download");
		expect(identities()).toEqual(["download", "pdf", "md"]);
	});
	it("offers one Download action with PDF and Markdown choices without starting a request", () => {
		const menu = useConversationExport(() => "grouped-chat");
		expect(menu.downloadMenuOption.value.label).toBe("Download");
		expect(menu.downloadMenuOption.value.submenu.map((option) => option.label)).toEqual([
			"PDF",
			"Markdown",
		]);
		expect(downloadConversation).not.toHaveBeenCalled();
	});
	it.each([
		["pdf", 0, "Preparing PDF…"],
		["md", 1, "Preparing Markdown…"],
	])(
		"shares %s progress across grouped menus without disabling their focusable parent",
		async (format, index, label) => {
			let finish;
			downloadConversation.mockImplementation(
				() => new Promise((resolve) => (finish = resolve))
			);
			const row = useConversationExport(() => "grouped-" + format);
			const header = useConversationExport(() => "grouped-" + format);
			const request = row.downloadMenuOption.value.submenu[index].onClick();
			try {
				expect(downloadConversation).toHaveBeenCalledWith("grouped-" + format, format);
				for (const menu of [row, header]) {
					expect(menu.downloadMenuOption.value.label).toBe(label);
					expect(menu.downloadMenuOption.value.disabled).toBe(false);
					expect(
						menu.downloadMenuOption.value.submenu.every((option) => option.disabled)
					).toBe(true);
				}
				await header.downloadMenuOption.value.submenu[1 - index].onClick();
				expect(downloadConversation).toHaveBeenCalledTimes(1);
			} finally {
				finish();
				await request;
			}
			expect(row.downloadMenuOption.value.label).toBe("Download");
			expect(header.downloadMenuOption.value.submenu.map((option) => option.label)).toEqual([
				"PDF",
				"Markdown",
			]);
		}
	);
	it("disables the grouped action when there is no conversation", () => {
		const menu = useConversationExport(() => null);
		expect(menu.downloadMenuOption.value.disabled).toBe(true);
		expect(menu.downloadMenuOption.value.submenu.every((option) => option.disabled)).toBe(
			true
		);
	});
});
