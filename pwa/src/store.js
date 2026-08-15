import { reactive } from "vue";
import * as api from "./api";

// One small shared store: the conversation list is read by the chats screen and
// the drawer, and written by the chat screen (a reply retitles the chat). The
// drawer is opened from whichever screen is on top.
export const store = reactive({
	drawerOpen: false,
	conversations: [],
	loaded: false,
	// Conversations whose reply landed while the user was somewhere else. The
	// shell marks on run:end, the chat screen clears on open. In memory only,
	// like the desktop SPA: it answers "what changed since I looked away", not
	// "what have I ever read", so surviving a reload would be wrong.
	unread: new Set(),

	async loadConversations() {
		try {
			const rows = await api.listConversations();
			this.conversations = Array.isArray(rows) ? rows : [];
		} catch (e) {
			// Offline or a dropped bench: keep whatever is on screen rather than
			// blanking the list under the user.
			console.error("Jarvis PWA: failed to load conversations", e);
		} finally {
			this.loaded = true;
		}
	},

	// The worker titles a chat asynchronously, after the first real turn.
	applyRename(id, title) {
		const row = this.conversations.find((c) => c.name === id);
		if (row) row.title = title;
	},

	markUnread(id) {
		if (id) this.unread.add(id);
	},

	clearUnread(id) {
		if (id) this.unread.delete(id);
	},
});
