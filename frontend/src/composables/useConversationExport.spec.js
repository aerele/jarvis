import { beforeEach, describe, expect, it, vi } from "vitest";
import { ref } from "vue";

vi.mock("@/api", () => ({ downloadConversation: vi.fn() }));
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn() } }));
vi.mock("@/lib/errorReporter", () => ({ report: vi.fn() }));
import { downloadConversation } from "@/api";
import { toast } from "frappe-ui";
import { report } from "@/lib/errorReporter";
import { useConversationExport } from "./useConversationExport";

describe("chat export menu state", () => {
	beforeEach(() => vi.clearAllMocks());
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
