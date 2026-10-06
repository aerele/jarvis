import { sendRejectionCopy } from "../../../frontend/src/lib/sendRejectionCopy.js";

// Request state is independent of the view and transcript. The caller makes
// `state` reactive; this module stays testable without a Vue runtime.
export function createSendRecovery(state, id = () => crypto.randomUUID().replaceAll("-", "")) {
	return {
		stage(conversation, text, attachments, approvalTokens = []) {
			const request = {
				id: id(),
				conversation,
				text,
				attachments: attachments.map((a) => ({ file_url: a.file_url, name: a.name })),
				approvalTokens: [...approvalTokens],
				state: "sending",
				checking: false,
				result: null,
				note: "",
			};
			state.requests.push(request);
			// Return the proxy if state is reactive, not the raw object.
			return state.requests[state.requests.length - 1];
		},
		settle(request, envelope) {
			const result = envelope?.delivery === "settled" ? envelope.result : null;
			// Positive server evidence wins over a later lost/failed HTTP response.
			if (["accepted", "confirmed", "rejected"].includes(request.state))
				return request.state;
			if (
				result?.confirmed === true &&
				typeof result.ok === "boolean" &&
				result.conversation_id
			) {
				request.state = "confirmed";
			} else if (
				result?.ok === true &&
				result.conversation_id &&
				result.message_id &&
				result.run_id
			) {
				request.state = "accepted";
			} else if (result?.ok === false) {
				request.state = "rejected";
			} else {
				request.state = "uncertain";
			}
			request.result = result;
			return request.state;
		},
		retry(request) {
			if (request.state !== "rejected" || request.checking) return false;
			// A definitively rejected attempt is finished. The next attempt gets
			// its own receipt; an unknown attempt never reaches this path.
			request.id = id();
			request.state = "sending";
			request.result = null;
			request.note = "";
			return true;
		},
		remove(request) {
			const i = state.requests.indexOf(request);
			if (i !== -1) state.requests.splice(i, 1);
		},
		reconcile(conversation, messages) {
			const names = new Set(messages.map((m) => m.name));
			state.requests = state.requests.filter(
				(r) =>
					!(
						r.conversation === conversation &&
						r.state === "accepted" &&
						names.has(r.result?.message_id)
					)
			);
		},
	};
}

export function recoveryCopy(request, agentName = "Jarvis") {
	if (request.state === "sending")
		return {
			title: "Sending…",
			detail: "Your message and files are preserved until delivery is confirmed.",
		};
	if (request.state === "accepted")
		return {
			title: request.result?.queued ? "Message queued" : "Message delivered",
			detail: "Loading the saved message. Your request has been accepted.",
		};
	if (request.state === "confirmed")
		return {
			title: "Confirmation received",
			detail: "Check the action receipts in this conversation. This message was not sent as a new turn.",
		};
	if (request.state === "uncertain")
		return {
			title: "Delivery not confirmed",
			detail: "Jarvis may already be working. Check delivery before sending again. Your message and files are preserved in this tab.",
		};
	if (request.result?.reason === "maintenance")
		return {
			title: "Not sent · Maintenance",
			detail: "Your message and files are preserved. Retry when Jarvis is available.",
		};
	if (request.result?.reason === "release_update_required")
		return {
			title: "Not sent · Update required",
			detail: "Keep a copy of your message and file references before reloading. This tab’s preserved requests do not survive a reload.",
		};
	return {
		title: "Not sent",
		detail: `${
			request.result?.message ||
			sendRejectionCopy(request.result?.reason, agentName, request.result).message
		} Your message and files are preserved in this tab.`,
	};
}
