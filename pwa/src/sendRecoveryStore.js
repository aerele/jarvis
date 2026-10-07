import { reactive } from "vue";
import { createSendRecovery } from "./lib/sendRecovery";

// Tab memory only. No business data in persistent browser storage. A full page
// logout/reload clears it. Key each draft/request to its originating chat.
export const recoveryState = reactive({
	requests: [],
	heroDraft: null,
	editingNewRequest: null,
	newChatPicks: {},
	drafts: {},
	parkedDrafts: [],
	queued: {},
	starting: {},
	observedRuns: {},
});
export const sendRecovery = createSendRecovery(recoveryState);
