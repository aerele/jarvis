import { computed, reactive } from "vue";
import { toast } from "frappe-ui";
import { downloadConversation } from "@/api";
import { errHtml } from "@/lib/errors";
import { report } from "@/lib/errorReporter";
import { __ } from "@/lib/i18n";

// Shared by the sidebar and header: switching menus cannot start a duplicate.
const pending = reactive(new Map());

export function useConversationExport(conversation) {
	async function download(format) {
		const id = conversation();
		if (!id || pending.has(id)) return;
		pending.set(id, format);
		try {
			await downloadConversation(id, format);
		} catch (error) {
			toast.error(errHtml(error, __("Couldn't download this chat. Try again.")));
			// Expected access, quota and size refusals are actionable user states.
			// Unexpected failures report operation metadata only, never server data.
			if (error?.expectedRefusal !== true) {
				report({
					conversation: id,
					error_code: "chat_export",
					error_class: "ChatExportError",
					message:
						format === "pdf"
							? "Chat PDF export failed"
							: "Chat Markdown export failed",
				});
			}
		} finally {
			pending.delete(id);
		}
	}
	const downloadOptions = computed(() => {
		const id = conversation();
		const running = pending.get(id);
		return ["pdf", "md"].map((format) => ({
			key: format,
			label:
				running === format
					? format === "pdf"
						? __("Preparing PDF…")
						: __("Preparing Markdown…")
					: format === "pdf"
					? __("Download as PDF")
					: __("Download as Markdown"),
			icon: "download",
			disabled: !id || !!running,
			onClick: () => download(format),
		}));
	});
	const downloadMenuOption = computed(() => {
		const id = conversation();
		const running = pending.get(id);
		return {
			key: "download",
			value: "download",
			label: running
				? running === "pdf"
					? __("Preparing PDF…")
					: __("Preparing Markdown…")
				: __("Download"),
			icon: "download",
			// Keep the disclosure focusable while busy; only its format choices
			// are disabled, so keyboard users can still open/leave the menu.
			disabled: !id,
			submenu: downloadOptions.value.map((option) => ({
				...option,
				value: option.key,
				label:
					running === option.key
						? option.label
						: option.key === "pdf"
						? __("PDF")
						: __("Markdown"),
			})),
		};
	});
	return { downloadOptions, downloadMenuOption };
}
