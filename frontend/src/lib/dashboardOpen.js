// "Open in Dashboards" — the way back from a builder conversation in MAIN CHAT.
//
// The Dashboards builder's thread is an ordinary Jarvis Conversation, so it
// also shows up in the main chat list. Opened there, the agent's html artifact
// renders as a static preview: main chat's canvas has no query-tool bridge, so
// nothing in it fetches data. That is accepted — what was missing is the door
// back to the page where the same document DOES run with data.
//
// Nothing in a message says "this is a dashboard": an html canvas in main chat
// is just an html canvas (dashboardRestore.js documents the same trap on the
// builder side). The marker lives on the CONVERSATION — `origin_page`, stamped
// server-side from the builder's send context — so the affordance is a
// property of the conversation plus the item type, never of the html itself.

/** Does this canvas item, in this conversation, get the affordance? */
export function canOpenInDashboards(originPage, cv) {
	return originPage === "dashboards" && !!cv && cv.type === "html";
}

/**
 * Where the affordance goes.
 *
 * A saved dashboard bound to this conversation opens IN PLACE (`?edit=`): the
 * builder loads the stored document, runs its sources live, and saves back
 * over the same row. With no saved row there is nothing to edit, so the
 * builder is asked to promote THIS artifact instead (`?chat=&canvas=`) and
 * replays it from the transcript.
 *
 * @param {{dashboard: {name?: string}|null, conversation: string, messageId: string}} arg
 * @returns {{path: string, query: object}} a vue-router location
 */
export function dashboardOpenRoute({ dashboard, conversation, messageId }) {
	const saved = (dashboard && dashboard.name) || "";
	if (saved) return { path: "/dashboards", query: { edit: saved } };
	return {
		path: "/dashboards",
		query: { chat: conversation || "", canvas: messageId || "" },
	};
}
