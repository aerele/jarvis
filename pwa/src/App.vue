<script setup>
import { onMounted, onUnmounted, inject } from "vue";
import { useRouter } from "vue-router";
import AppDrawer from "./components/AppDrawer.vue";
import InstallBanner from "./components/InstallBanner.vue";
import UpdateBanner from "./components/UpdateBanner.vue";
import AnnouncementBanner from "./components/AnnouncementBanner.vue";
import UpdateNoticeGate from "./components/UpdateNoticeGate.vue";
import WhatsNewSheet from "./components/WhatsNewSheet.vue";
import { store } from "./store";
import { sessionUser } from "./router";
import { showBanner, showNotice } from "./noticeGate";
import { showAnnouncement } from "./announcementGate";
import { holdActive, holdText, raiseHold, recheck } from "./maintenanceGate";
import { makeOnLlmSwitch } from "./llmSwitch";
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

// Built once, outside onMounted, so on/off pair against the same function identity.
const onLlmSwitch = makeOnLlmSwitch({ raiseHold, recheck });

onMounted(() => {
	socket?.on("jarvis:event", onEvent);
	// Registered here (the app shell, mounted once for the whole session) rather
	// than in ChatView, which mounts/unmounts per conversation route: the hold
	// strip this drives is rendered in the shell's own template below and must
	// show on every route, not only while a conversation happens to be open.
	socket?.on("jarvis:llm_switch", onLlmSwitch);
	socket?.on("connect", onResync);
	document.addEventListener("visibilitychange", onVisibility);
});
onUnmounted(() => {
	socket?.off("jarvis:event", onEvent);
	socket?.off("jarvis:llm_switch", onLlmSwitch);
	socket?.off("connect", onResync);
	document.removeEventListener("visibilitychange", onVisibility);
});
</script>

<template>
	<div class="jv-app">
		<!-- Upgrade maintenance hold (Stream E): a persistent "back shortly" strip
		     while this tenant's agent is being upgraded. Top-of-app on every route
		     (before the install strip), like the desktop banner; the composer stays
		     enabled and the send-gate self-heals when the roll clears. Shown only to
		     a signed-in user - nothing to hold on the login screen. Amber tokens flip
		     with the theme. role/aria-live announce it to screen readers. -->
		<div
			v-if="holdActive && sessionUser()"
			class="jv-maint-strip"
			role="status"
			aria-live="polite"
		>
			{{ holdText }}
		</div>
		<!-- First child, in the flow: the install strip pushes the app down rather
		     than covering any part of it. -->
		<InstallBanner />
		<!-- Customer announcement banner (Slice B): the fleet-wide operator notice.
		     Yields to InstallBanner (only one top slot at a time) and to a signed-out
		     visitor, and WINS the slot over the update banner below (which yields via
		     !showAnnouncement). Dismiss snoozes it per-device. -->
		<AnnouncementBanner v-if="showAnnouncement && sessionUser() && !installBannerVisible" />
		<!-- Release-nudge soft banner (Slice 3b): top-of-app, next to the install
		     strip. Yields to InstallBanner (installBannerVisible - only one top
		     slot shows at a time), to a signed-out visitor (nothing to nudge on
		     the login screen), and to the announcement banner above. Dismiss
		     minimises it into whichever VersionPill is currently mounted (ChatView's
		     header; see noticeGate.js's pillHandle). -->
		<UpdateBanner
			v-if="
				showBanner &&
				!installBannerVisible &&
				sessionUser() &&
				!showAnnouncement &&
				!holdActive
			"
		/>
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

<style scoped>
/* Upgrade maintenance hold strip - an amber card matching the InstallBanner /
   UpdateBanner idiom (rounded + margin), not a full-bleed bar. Amber tokens flip
   with the theme (see index.css). */
.jv-maint-strip {
	margin: 10px 12px 0;
	padding: 10px 14px;
	background: var(--amber-bg);
	color: var(--amber);
	border: 1px solid color-mix(in srgb, var(--amber) 35%, transparent);
	border-radius: 12px;
	font-size: 13px;
	font-weight: 600;
	line-height: 1.35;
	text-align: center;
}
</style>
