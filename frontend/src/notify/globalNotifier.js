// Global attention signals (NOTIFY-APPROVALS design, Part 1) — the app-level
// answer to "Jarvis finished / errored / needs you while you're elsewhere".
// One `jarvis:event` listener attached by AppShell (mounted on every authed
// route), so signals survive route changes; ChatView keeps handling the
// on-screen conversation's own events (streaming, cards) and no longer owns
// any notification.
//
// Decision per event, evaluated AT DISPATCH TIME (never cached at mount):
//   - tab hidden            → browser Notification (opt-in: localStorage
//     `jarvis-notify` === "1" AND Notification.permission === "granted"),
//     tag `jarvis-<conversation>` + renotify so bursts alert again.
//   - visible but elsewhere → in-app toast (bottom-right, see NotifyToaster),
//     never a browser notification.
//   - visible on the event's conversation → nothing (ChatView renders it).
// "On screen" = the /c/:id param or, on the chat home, the store's
// currentConvId (ChatView mirrors its selection there); any non-chat route
// means no conversation is on screen.
import { ref } from "vue";
import { useShellStore } from "@/stores/shell";
import { session } from "@/data/session";
import { agentName } from "@/branding";

// ---- toast state (rendered by NotifyToaster.vue) -----------------------------
const MAX_TOASTS = 3;
const TOAST_MS = 6000;
const toasts = ref([]); // [{ id, title, body, onClick }]
let _seq = 0;
const _timers = new Map();

export function useToasts() {
	return toasts;
}

export function pushToast({ title, body, onClick }) {
	const id = ++_seq;
	const next = [...toasts.value, { id, title: title || agentName, body: body || "", onClick }];
	// max 3 stacked — drop the oldest (and its timer) instead of growing a pile
	while (next.length > MAX_TOASTS) {
		const drop = next.shift();
		const tm = _timers.get(drop.id);
		if (tm) clearTimeout(tm);
		_timers.delete(drop.id);
	}
	toasts.value = next;
	_timers.set(
		id,
		setTimeout(() => dismissToast(id), TOAST_MS)
	);
	return id;
}

export function dismissToast(id) {
	const tm = _timers.get(id);
	if (tm) {
		clearTimeout(tm);
		_timers.delete(id);
	}
	toasts.value = toasts.value.filter((t) => t.id !== id);
}

// ---- helpers -----------------------------------------------------------------
// The browser-notification opt-in, read fresh per dispatch: a grant/revoke in
// another tab (or the site settings) takes effect immediately (D13).
function _notifyAllowed() {
	try {
		return (
			typeof Notification !== "undefined" &&
			localStorage.getItem("jarvis-notify") === "1" &&
			Notification.permission === "granted"
		);
	} catch (e) {
		return false;
	}
}

function _excerpt(s, n = 120) {
	const t = String(s || "")
		.trim()
		.replace(/\s+/g, " ");
	return t.length > n ? t.slice(0, n - 1) + "…" : t;
}

// ---- the listener --------------------------------------------------------------
// Returns a detach function; AppShell calls attach in onMounted and the
// detacher in onBeforeUnmount (same socket instance ChatView injects).
export function attachGlobalNotifier({ socket, router }) {
	if (!socket) return () => {};
	const store = useShellStore();

	const onScreenConv = () => (router.currentRoute.value.meta.chat ? store.currentConvId : null);
	// The Triggers pane renders its own conversation inline, but /triggers has
	// meta.chat falsy so onScreenConv() can't see it. The pane persists its
	// conversation id per user (useStorage writes the raw string); read that
	// same key so its turns don't dot/toast while the pane is on screen. Only
	// the Triggers tab mounts the pane — not #activity, not the detail pages.
	const triggersPaneConv = () => {
		const r = router.currentRoute.value;
		if (r.name !== "TriggersPage" || String(r.hash || "").startsWith("#activity")) return null;
		try {
			return localStorage.getItem(`jarvis-triggers-conv-${session.user || "anon"}`) || null;
		} catch (e) {
			return null;
		}
	};
	const convTitle = (id) => (id && store.conversations.find((c) => c.name === id)?.title) || "";
	const go = (path) => {
		try {
			window.focus();
		} catch (e) {
			// Some browsers refuse programmatic focus. Routing still works.
		}
		router.push(path);
	};

	function browserNotify({ title, body, tag, onclick }) {
		if (!_notifyAllowed()) return;
		try {
			const n = new Notification(title, { body, tag, renotify: true });
			n.onclick = () => {
				if (onclick) onclick();
				n.close();
			};
		} catch (e) {
			// page-context Notification unsupported (e.g. Android Chrome) — no-op
		}
	}

	// trigger:activity bursts (a bulk save fires one frame per doc) coalesce to
	// one signal per trigger+status within this window.
	const trigSignalAt = new Map();
	const TRIG_SIGNAL_WINDOW_MS = 5000;

	// hidden → browser notification; visible-but-elsewhere → toast;
	// visible on the conversation → nothing.
	function signal({ conv, title, body, tag, open, toastAnywhere = false }) {
		if (document.hidden) {
			browserNotify({ title, body, tag, onclick: open });
		} else if (toastAnywhere || conv !== onScreenConv()) {
			pushToast({ title, body, onClick: open });
		}
	}

	function onEvent(p) {
		if (!p || !p.kind) return;
		switch (p.kind) {
			case "run:end":
			case "run:error": {
				const conv = p.conversation_id;
				if (!conv) return;
				// The Triggers pane renders this conversation's turns inline — treat
				// it like the on-screen chat: no unread dot, no "Reply ready" toast.
				// A hidden tab still browser-notifies below, same as a hidden chat.
				const onTriggersPane = conv === triggersPaneConv();
				if (onTriggersPane && !document.hidden) return;
				if (!onTriggersPane && conv !== onScreenConv()) {
					// the sidebar unread dot — the in-app "multiple chats" signal
					store.markUnread(conv);
					// keep the row's title/order honest (debounced reload)
					store.applyRemoteNew();
				}
				const title = convTitle(conv) || agentName;
				// A stop is the user's own click, seconds ago - the dot is useful, a
				// notification saying "Reply ready" for the reply they just killed is not.
				if (p.stopped) return;
				const body =
					p.kind === "run:error"
						? `${agentName} hit an error in ${convTitle(conv) || "your chat"}`
						: _excerpt(p.preview) || "Reply ready";
				signal({ conv, title, body, tag: "jarvis-" + conv, open: () => go("/c/" + conv) });
				return;
			}
			case "action:pending": {
				// keyed `conversation` (may be "" when the server can't resolve one —
				// it still reached THIS user's socket, so fall back to the active chat)
				const conv = p.conversation || store.currentConvId || null;
				const tool = p.tool ? String(p.tool).replace(/^jarvis__/, "") : "";
				signal({
					conv,
					title: convTitle(conv) || agentName,
					body: `${agentName} needs your confirmation` + (tool ? " — " + tool : ""),
					tag: "jarvis-" + (conv || "confirm"),
					open: () => go(conv ? "/c/" + conv : "/"),
				});
				return;
			}
			case "approval:new": {
				// bump the badge NOW — the 60s poll reconciles later
				store.approvalsCount = (store.approvalsCount || 0) + 1;
				signal({
					conv: null,
					toastAnywhere: true, // waiting-on-you is worth a toast even on-conversation
					title: `${agentName} is waiting on you`,
					body:
						_excerpt(p.question) ||
						"A question needs your answer on the Approval Board.",
					tag: "jarvis-" + (p.conversation_id || "approvals"),
					open: () => go("/approvals"),
				});
				return;
			}
			case "trigger:activity": {
				// A trigger run the user would want to hear about: Failed / Blocked
				// always; Success only for LLM actions (the "warn me when…" case —
				// Script successes and Skipped rows are routine noise). action_type
				// may be absent on older frames: then only Failed/Blocked signal.
				const status = p.status;
				const wanted =
					status === "Failed" ||
					status === "Blocked" ||
					(status === "Success" && p.action_type === "LLM");
				if (!wanted) return;
				// coalesce a burst into one signal per trigger+status
				const key = (p.trigger || "") + "|" + status;
				const now = Date.now();
				if (now - (trigSignalAt.get(key) || 0) < TRIG_SIGNAL_WINDOW_MS) return;
				trigSignalAt.set(key, now);
				const label = p.trigger_label || p.trigger || "";
				const what = label ? `Trigger "${label}"` : "A trigger";
				const title =
					status === "Failed"
						? "Trigger failed"
						: status === "Blocked"
						? "Trigger blocked a save"
						: "Trigger fired";
				const body =
					status === "Failed"
						? `${what} hit an error — open the activity log for details.`
						: status === "Blocked"
						? `${what} blocked a document from saving.`
						: `${what} ran — see the activity log for what it found.`;
				const open = () => go("/triggers#activity");
				// like conversation:new, hand-rolled: signal() would swallow a
				// conv-less toast on non-chat routes (null === onScreenConv()).
				// The Activity tab itself live-refreshes — no toast on top of it.
				const r = router.currentRoute.value;
				const onActivityTab =
					r.name === "TriggersPage" && String(r.hash || "").startsWith("#activity");
				if (document.hidden) {
					browserNotify({ title, body, tag: "jarvis-trigger-activity", onclick: open });
				} else if (!onActivityTab) {
					pushToast({ title, body, onClick: open });
				}
				return;
			}
			case "conversation:new": {
				const conv = p.conversation_id;
				if (!conv) return;
				// off the chat routes ChatView isn't mounted to refresh the sidebar
				// list — do it here (debounced; harmless double when both run)
				if (!router.currentRoute.value.meta.chat) store.applyRemoteNew();
				const title = p.title || `Message from ${agentName}`;
				const body =
					_excerpt(p.preview) || `${agentName} started a new conversation with you.`;
				const open = () => go("/c/" + conv);
				if (document.hidden) {
					browserNotify({ title, body, tag: "jarvis-" + conv, onclick: open });
				} else if (!router.currentRoute.value.meta.chat) {
					// ChatView's own proactive toast covers visible chat routes
					pushToast({ title, body, onClick: open });
				}
				return;
			}
		}
	}

	socket.on("jarvis:event", onEvent);
	return () => socket.off("jarvis:event", onEvent);
}
