import { call } from "frappe-ui";

export const getFileBoxSourceState = (conversation, attachment = {}) =>
	call("jarvis.chat.filebox_source.get_source_state", { conversation, ...attachment });

export const setFileBoxSource = (selection) =>
	call("jarvis.chat.filebox_source.set_source", selection);
