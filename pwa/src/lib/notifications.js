import { reactive } from "vue";

import { agentName } from "@/branding";

// The notification feed, assembled client-side from realtime events — the bench
// has no persistent notification store (the desktop uses live toasts, the native
// app builds the same feed in memory from the same events).
//
// It IS persisted here, and that is a deliberate divergence from the native app:
// a native process survives being backgrounded, so an in-memory feed is fine. A
// web page does not — every reload, every tab-restore, every cold launch of an
// installed PWA starts a fresh JS heap. An in-memory feed would therefore be
// empty exactly when the user goes looking for it ("Jarvis buzzed while I was in
// another app — what did it want?"), which is the only moment the bell matters.
const KEY = "jarvis.notifications";
const CAP = 100;

function read() {
	try {
		const raw = JSON.parse(localStorage.getItem(KEY) || "[]");
		return Array.isArray(raw) ? raw.slice(0, CAP) : [];
	} catch {
		return [];
	}
}

export const feed = reactive({
	items: read(),
	get unread() {
		return this.items.filter((i) => !i.read).length;
	},
});

function persist() {
	try {
		localStorage.setItem(KEY, JSON.stringify(feed.items));
	} catch {
		/* private mode / quota: the feed just won't survive the reload */
	}
}

/** Ids are derived from the run/token, so a replayed event can't double-post. */
function push(item) {
	if (feed.items.some((i) => i.id === item.id)) return;
	feed.items.unshift(item);
	if (feed.items.length > CAP) feed.items.length = CAP;
	persist();
}

export function markRead(id) {
	const row = feed.items.find((i) => i.id === id);
	if (row && !row.read) {
		row.read = true;
		persist();
	}
}

export function markAllRead() {
	feed.items.forEach((i) => (i.read = true));
	persist();
}

// The feed is one person's task history. Signing out must drop it, or the next
// user of this browser inherits it — and its unread badge.
export function resetFeed() {
	feed.items.length = 0;
	try {
		localStorage.removeItem(KEY);
	} catch {
		/* ignore */
	}
}

/** Fold one realtime event into the feed. Same four kinds the native app tracks. */
export function recordEvent(e) {
	const conv = e.conversation_id || e.conversation;
	const at = Date.now();

	// A stopped run is not a finished task. This feed is durable (localStorage +
	// unread badge), so without the guard the user finds an unread "Task
	// finished" hours after they killed the reply themselves.
	if (e.kind === "run:end" && !e.stopped && conv) {
		push({
			id: `end:${e.run_id || e.message_id}`,
			kind: "task-finished",
			title: "Task finished",
			body: `${agentName} finished working on your request.`,
			conversation: conv,
			at,
			read: false,
		});
	} else if (e.kind === "run:error" && conv) {
		push({
			id: `err:${e.run_id || e.message_id}`,
			kind: "task-failed",
			title: "Task failed",
			body: e.error || "Something went wrong during the run.",
			conversation: conv,
			at,
			read: false,
		});
	} else if (e.kind === "action:pending") {
		push({
			id: `dec:${e.token || e.run_id}`,
			kind: "needs-decision",
			title: "Needs your decision",
			body: e.summary || `${agentName} is waiting for your approval.`,
			conversation: conv,
			at,
			read: false,
		});
	} else if (e.kind === "conversation:new" && conv) {
		push({
			id: `new:${conv}`,
			kind: "new-conversation",
			title: e.title || "New conversation",
			body: e.preview || `${agentName} started a conversation for you.`,
			conversation: conv,
			at,
			read: false,
		});
	}
}
