import { request } from "./chatSharing";
const call = (method, args, options = {}) =>
	request(method, args, { ...options, namespace: "collaboration_api" });
export const getCollaboration = (conversation, { before, limit = 80, watchRun } = {}, signal) =>
	call(
		"get_conversation",
		{
			conversation,
			limit,
			...(before != null ? { before } : {}),
			...(watchRun ? { watch_run: watchRun } : {}),
		},
		{ signal }
	);
export const sendSharedMessage = (conversation, message, request_id, attachments = []) =>
	call("send_message", { conversation, message, request_id, attachments });
export const stopSharedRun = (conversation, run_id) => call("stop_run", { conversation, run_id });
export const retrySharedRun = (conversation, run_id) =>
	call("retry_run", { conversation, run_id });
export const answerSharedQuestion = (conversation, question, decision) =>
	call("answer_question", { conversation, question, decision });
export const confirmSharedAction = (conversation, action) =>
	request("confirm_action", { conversation, action }, { namespace: "collaboration_actions" });
export const discardSharedAction = (conversation, action) =>
	request("discard_action", { conversation, action }, { namespace: "collaboration_actions" });
export const getChatAccess = (conversation) =>
	request("get_access", { conversation }, { namespace: "collaboration_access" });
export const setChatRecipients = (conversation, users, modes) =>
	request(
		"set_recipients",
		{
			conversation,
			recipients: users.map((user) => ({
				user,
				access_mode: modes[user] === "Chat" ? "Chat" : "View",
			})),
		},
		{ namespace: "collaboration_access" }
	);
export const getCollaborationTool = (message) => call("get_tool_detail", { message });
export const getCollaborationCanvas = (message, name, dark = false) =>
	call("get_canvas", { message, name, dark: dark ? 1 : 0 });
export const uploadSharedFile = (conversation, file, signal) => {
	const formData = new FormData();
	formData.append("conversation", conversation);
	formData.append("file", file);
	return call("upload_file", {}, { signal, formData });
};
