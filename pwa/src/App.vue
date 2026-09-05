<script setup>
import { onMounted, onUnmounted, inject } from "vue";
import { useRouter } from "vue-router";
import AppDrawer from "./components/AppDrawer.vue";
import InstallBanner from "./components/InstallBanner.vue";
import UpdateBanner from "./components/UpdateBanner.vue";
import UpdateNoticeGate from "./components/UpdateNoticeGate.vue";
import WhatsNewSheet from "./components/WhatsNewSheet.vue";
import { store } from "./store";
import { sessionUser } from "./router";
import { showBanner, showNotice } from "./noticeGate";
import { installBannerVisible } from "./lib/installBanner";
import { prefs } from "./lib/prefs";
import { agentName } from "@/branding";
import { flushBuffered } from "@shared/lib/errorReporter";
import { recordEvent } from "./lib/notifications";

const socket = inject("$socket");
const router = useRouter();

// Only when the user is looking somewhere else. A notification for the thing
// already on screen is noise, and it is the fastest way to get a site's
// notification permission revoked for good.
function notify(title, body, conversationId) {
	if (!("Notification" in window) || Notification.permission !== "granted") return;
	if (!document.hidden) return;
	try {
		const n = new Notification(title, {
			body,
			icon: "/assets/jarvis/manifest/icon-192.png",
			tag: conversationId,
		});
		n.onclick = () => {
			window.focus();
			if (conversationId) router.push(`/c/${conversationId}`);
			n.close();
		};
	} catch {
		/* some browsers reject construction outside a service worker; not fatal */
	}
}

// The conversation on screen right now, or "" everywhere else — /c/:id is the
// only route that shows one.
function openConversationId() {
	const r = router.currentRoute.value;
	return r.name === "Chat" ? String(r.params.id || "") : "";
}

// Chat-list-level realtime. The per-message stream is handled inside ChatView;
// these kinds have to land even when the user is NOT in that chat, so they live
// at the shell: a chat titles itself after its first turn, Jarvis can open a
// conversation on its own, and a finished run or a parked write is exactly what
// the user walked away from the phone waiting for.
function onEvent(p) {
	const conv = p.conversation_id || p.conversation;
	// The bell's feed: every event is recorded whether or not it also buzzes.
	recordEvent(p);

	if (p.kind === "conversation:renamed" && p.conversation_id) {
		store.applyRename(p.conversation_id, p.title);
	} else if (p.kind === "conversation:new") {
		store.loadConversations();
	} else if (p.kind === "run:end" && !p.stopped) {
		// A reply the user hasn't seen. Not gated on notifyDone: the dot is the
		// quiet in-app signal, and someone who turned notifications off still
		// wants to know which chat moved. Skip the chat that's already open —
		// they're watching it arrive.
		if (conv && conv !== openConversationId()) store.markUnread(conv);
		if (prefs.notifyDone) {
			const title = store.conversations.find((c) => c.name === conv)?.title || agentName;
			notify(`${agentName} finished`, title, conv);
		}
	} else if (p.kind === "import:finished") {
		// Slice B: a background CSV import finished; the completion message is
		// already durable in the conversation. ChatView live-renders it when
		// that chat is open — this shell-level handler owns only the
		// off-screen unread dot. Deliberately no browser push for this event,
		// this wave (unlike run:end above).
		if (conv && conv !== openConversationId()) store.markUnread(conv);
	} else if (p.kind === "action:pending" && prefs.notifyDecision) {
		notify(
			`${agentName} needs your approval`,
			p.summary || p.tool || "A change is waiting for you",
			conv
		);
	}
}

// socket.io has no replay: frames published while the phone was asleep or the
// socket was down are simply gone. Refetch on reconnect and on tab-wake rather
// than trusting the stream — the same contract the desktop SPA follows.
function onResync() {
	// Behind the login screen there is nothing to resync, and asking would just
	// log a 403 on every tab-wake.
	if (!sessionUser()) return;
	// Flush any errors buffered while offline (no-op when the buffer is empty).
	void flushBuffered();
	store.loadConversations();
	if (router.currentRoute.value.name === "Chat") window.dispatchEvent(new Event("jv:resync"));
}
function onVisibility() {
	if (document.visibilityState === "visible") onResync();
}

onMounted(() => {
	socket?.on("jarvis:event", onEvent);
	socket?.on("connect", onResync);
	document.addEventListener("visibilitychange", onVisibility);
});
onUnmounted(() => {
	socket?.off("jarvis:event", onEvent);
	socket?.off("connect", onResync);
	document.removeEventListener("visibilitychange", onVisibility);
});
</script>

<template>
	<div class="jv-app">
		<!-- First child, in the flow: the install strip pushes the app down rather
		     than covering any part of it. -->
		<InstallBanner />
		<!-- Release-nudge soft banner (Slice 3b): top-of-app, next to the install
		     strip. Yields to InstallBanner (installBannerVisible - only one top
		     slot shows at a time) and to a signed-out visitor (nothing to nudge on
		     the login screen). Dismiss minimises it into whichever VersionPill is
		     currently mounted (ChatView's header; see noticeGate.js's pillHandle). -->
		<UpdateBanner v-if="showBanner && !installBannerVisible && sessionUser()" />
		<router-view v-slot="{ Component }">
			<component :is="Component" />
		</router-view>
		<AppDrawer />
		<!-- Release-notice overlay: a hard block above the app for a signed-in
		     user, until the control plane stops serving the notice. -->
		<UpdateNoticeGate v-if="showNotice && sessionUser()" />
		<!-- What's-new sheet (Slice 3b): ONE global instance, opened from the
		     pill, the soft banner above, and the hard gate's "See what's new"
		     link, all via the shared whatsNewOpen ref. -->
		<WhatsNewSheet />
	</div>
</template>
