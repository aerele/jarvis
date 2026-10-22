import { firstServerMessage, messageText } from "@/lib/errors";
import { __ } from "@/lib/i18n";

// Unlike the generic call helper, preserve the shared-read endpoint's typed
// 403 reason. Every other refusal remains an error; HTML/proxy bodies are inert.
export async function request(
	method,
	args = {},
	{ signal, refusals = false, namespace = "sharing", formData = null } = {}
) {
	const controller = new AbortController();
	const abort = () => controller.abort();
	signal?.addEventListener("abort", abort, { once: true });
	if (signal?.aborted) abort();
	const timer = setTimeout(abort, 20000);
	try {
		const response = await fetch(`/api/method/jarvis.chat.${namespace}.${method}`, {
			method: "POST",
			credentials: "same-origin",
			signal: controller.signal,
			headers: {
				Accept: "application/json",
				...(!formData ? { "Content-Type": "application/json" } : {}),
				"X-Frappe-CSRF-Token": window.csrf_token || "",
			},
			body: formData || JSON.stringify(args),
		});
		let body;
		try {
			body = await response.json();
		} catch {
			/* A proxy need not return JSON. */
		}
		if (
			refusals &&
			response.status === 403 &&
			["owner", "not_shared", "sharing_off"].includes(body?.message?.refused)
		)
			return { refused: body.message.refused };
		if (!response.ok)
			throw Object.assign(
				new Error(
					messageText(firstServerMessage(body?._server_messages)) ||
						__("Couldn't complete this request. Try again.")
				),
				{ status: response.status, exc_type: body?.exc_type }
			);
		if (!body || !("message" in body))
			throw new Error(__("Couldn't read the server response."));
		return body.message;
	} finally {
		clearTimeout(timer);
		signal?.removeEventListener("abort", abort);
	}
}
export const shareCandidates = (query = "") => request("share_candidates", { query });
export const getChatShares = (conversation) => request("get_shares", { conversation });
export const setChatShares = (conversation, users) =>
	request("set_shares", { conversation, users });
export const listSharedWithMe = () => request("list_shared_with_me");
export const isSharedWithMe = (conversation) => request("is_shared_with_me", { conversation });
export const getSharedConversation = (
	conversation,
	{ before, after, refresh, limit = 80 } = {},
	signal
) =>
	request(
		"get_shared_conversation",
		{
			conversation,
			limit,
			...(before != null ? { before } : {}),
			...(after != null ? { after } : {}),
			...(refresh?.length ? { refresh: JSON.stringify(refresh) } : {}),
		},
		{ signal, refusals: true }
	);
export const getSharedToolDetail = (message) => request("get_shared_tool_detail", { message });
export const getSharedCanvas = (message, name, dark = false) =>
	request("get_shared_canvas", { message, name, dark: dark ? 1 : 0 });
export const getSharingSetting = () => request("get_sharing_setting");
export const setSharingSetting = (enabled) =>
	request("set_sharing_setting", { enabled: enabled ? 1 : 0 });
export const listAllShares = ({ start = 0, page_length = 25, search = "" } = {}) =>
	request("list_all_shares", { start, page_length, search, include_modes: 1 });
export const revokeShare = (name) => request("revoke_share", { name });
