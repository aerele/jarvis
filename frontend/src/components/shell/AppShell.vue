<template>
	<FrappeUIProvider>
		<div class="flex h-dvh-safe w-full">
			<!-- Hold a neutral surface until the onboarding verdict resolves AND a
			     route commits — prevents both a sidebar flash on fresh sites and a
			     poster flicker on a not-ready /onboarding deep-link. -->
			<div v-if="!shellReady" class="flex-1 bg-surface-white" />
			<!-- Onboarding gate (D11-safe): when the workspace has never finished
			     onboarding, block the WHOLE app with a full-screen poster — no
			     sidebar, no header — inviting setup. A RENDERED gate, not a
			     redirect, so it can't reintroduce the old desk↔SPA loop that the
			     forced D11 redirect caused. The /onboarding wizard itself is
			     exempt so the poster's "Complete setup" button can reach it. -->
			<OnboardingGate v-else-if="showGate" />
			<!-- Release-notice gate: a hard block, shown only once the app would
			     otherwise render. Exempts the wizard so a mid-onboarding workspace
			     can still finish setup. -->
			<UpdateNoticeGate v-else-if="showNotice && route.name !== 'Onboarding'" />
			<template v-else>
				<!-- Chrome-less routes (onboarding) drop the sidebar entirely — a
				     not-yet-onboarded customer has no app to navigate. Desktop keeps
				     the sidebar in-flow; phone width moves it off-canvas (drawer
				     block below). -->
				<div
					v-if="!route.meta.chromeless && !store.mobile"
					class="h-full shrink-0 border-r bg-surface-gray-1"
				>
					<Sidebar />
				</div>
				<!-- Phone: sidebar as an off-canvas drawer + tap-scrim. Translated
				     off-screen until opened; slides over the content. -->
				<template v-if="!route.meta.chromeless && store.mobile">
					<div
						v-if="store.mobileDrawerOpen"
						data-testid="drawer-scrim"
						class="fixed inset-0 z-40 bg-black/40"
						@click="store.mobileDrawerOpen = false"
					/>
					<div
						data-testid="mobile-drawer"
						class="fixed inset-y-0 left-0 z-50 border-r bg-surface-gray-1 shadow-xl transition-transform duration-300 ease-in-out"
						:class="store.mobileDrawerOpen ? 'translate-x-0' : '-translate-x-full'"
					>
						<Sidebar />
					</div>
				</template>
				<div class="flex flex-1 flex-col h-full min-w-0 overflow-auto bg-surface-white">
					<!-- Phone top bar for chrome'd non-chat routes: the sidebar is a
					     drawer now, so each such page needs a hamburger to reach nav.
					     Chat renders its own hamburger inside its header. -->
					<div
						v-if="store.mobile && !route.meta.chat && !route.meta.chromeless"
						class="flex h-12 shrink-0 items-center gap-2 border-b px-3"
					>
						<button
							class="jv-focus-ring grid size-9 place-items-center rounded-md text-ink-gray-7"
							aria-label="Open navigation"
							@click="store.mobileDrawerOpen = true"
						>
							<FeatherIcon name="menu" class="size-5" />
						</button>
						<div id="app-header" class="min-w-0 flex-1" />
					</div>
					<!-- LayoutHeader teleport target — non-chat routes only (D41).
					     The "Go to Desk" button is rendered INSIDE LayoutHeader's right
					     cluster (leftmost, before each page's own actions) so it is
					     uniform across pages and never displaces the page's primary
					     action from the rightmost corner; chat has its own header button
					     (ChatView openErpDesk), styled to match. -->
					<div
						v-else-if="!route.meta.chat && !route.meta.chromeless"
						class="flex border-b"
					>
						<div id="app-header" class="flex-1" />
					</div>
					<router-view v-if="booted" />
				</div>
			</template>
			<Dialogs />
			<SettingsDialog />
			<JarvisCommandPalette />
			<NotifyToaster />
			<ConfirmDialog />
			<SupportCopyPromptDialog />
		</div>
	</FrappeUIProvider>
</template>

<script setup>
// App shell (DESIGN-V3 §3.1): persistent sidebar around every route, the
// #app-header strip, the confirmDialog host, the ⌘K palette, the onboarding
// gate (D11), the approvals-badge poll (D12), the global notifier (attention
// signals for background conversations/routes, NOTIFY-APPROVALS Part 1) and
// the global shortcuts.
import { computed, onMounted, onBeforeUnmount, ref, watch, inject } from "vue";
import { useRoute, useRouter } from "vue-router";
import { FrappeUIProvider, Dialogs, setConfig, FeatherIcon } from "frappe-ui";
import * as api from "@/api";
import { useShellStore } from "@/stores/shell";
import { useShortcuts } from "@/composables/useShortcuts";
import { attachGlobalNotifier } from "@/notify/globalNotifier";
import NotifyToaster from "@/notify/NotifyToaster.vue";
import { needsOnboarding, regateOnRouteChange } from "@/onboarding/readiness.js";
import Sidebar from "./Sidebar.vue";
import JarvisCommandPalette from "./JarvisCommandPalette.vue";
import SettingsDialog from "./SettingsDialog.vue";
import ConfirmDialog from "./ConfirmDialog.vue";
// Mounted here (not in ChatView) so a route change away from Chat can never
// unmount it mid-prompt - same reasoning as ConfirmDialog living here.
import SupportCopyPromptDialog from "@/components/support/SupportCopyPromptDialog.vue";
import OnboardingGate from "./OnboardingGate.vue";
import UpdateNoticeGate from "./UpdateNoticeGate.vue";
import { showNotice } from "@/noticeGate";
// Unscoped global stylesheet. ChatView and OnboardingView render .jv-btn /
// .jv-iconbtn from it, so it is imported here at the shell rather than left as
// a side effect of whichever component happens to mount first (it used to ride
// along with SettingsDialog, which only worked because both are always-mounted
// siblings of this component).
import "@/assets/settings.css";

const route = useRoute();
const router = useRouter();
const store = useShellStore();
// The same shared socket ChatView injects (main.js provides it app-wide;
// null under ?nosocket) — the global notifier listens on it for the whole
// session, independent of which route is mounted.
const socket = inject("$socket");

// Onboarding gate state. `gatedOnboarding` starts null (unresolved). We render
// NOTHING but a neutral surface until BOTH the verdict has resolved AND a route
// has committed (`shellReady`), so we never flash the sidebar/app chrome before
// knowing whether to show the poster (else a fresh site briefly shows an empty
// sidebar), and never flash the poster on a not-ready /onboarding deep-link
// before the wizard mounts (route.name is momentarily undefined). The router's
// beforeEach awaits the same shared readiness promise, so onboarded users see
// the sidebar + their page appear together — the hold costs them nothing.
//
// Only a workspace that has NEVER onboarded is gated (needsOnboarding) — a
// merely-degraded but already-onboarded workspace (e.g. expired LLM creds) is
// NOT ejected from its chat/data and keeps /account reachable to recover.
const gatedOnboarding = ref(null);
needsOnboarding().then((v) => {
	gatedOnboarding.value = v;
});
// True while a route-triggered re-check (below) is in flight. Folded into
// `shellReady` so that window holds the SAME neutral surface the initial boot
// read already uses, rather than ever rendering `showGate`'s stale answer -
// see the watcher below for why this exists (round-4 review F3).
const regatingOnboarding = ref(false);
const shellReady = computed(
	() => gatedOnboarding.value !== null && !!route.name && !regatingOnboarding.value
);
// The wizard route is exempt so the poster's button can navigate into it.
const showGate = computed(() => gatedOnboarding.value === true && route.name !== "Onboarding");

// #691: the boot-time read above runs exactly ONCE for the whole SPA session -
// AppShell never remounts on a client-side navigation (App.vue is just
// <AppShell/>, owning the single <router-view/>). A connect that succeeds
// while still on /onboarding changes the backend verdict without remounting
// this component; the only thing that follows is a route change into Chat, and
// without this watcher `gatedOnboarding` never hears about it - the customer
// lands on a ready workspace still showing the "Finish setting up" poster,
// clearable only by a hard reload that nothing on screen suggests (three
// production reproductions, jarvis#691).
//
// The actual race/flash-safe logic (round-4 review F3/F4: never write the
// stale value mid-flight, never let a superseded call win) lives in
// readiness.js's regateOnRouteChange(), pulled out of this file so it can be
// pinned by a plain unit test without mounting this (heavy) component. The
// two refs below ARE its state; passing them by reference is what lets it
// write straight into AppShell's reactivity.
watch(
	() => route.name,
	() => regateOnRouteChange(gatedOnboarding, regatingOnboarding)
);

// Deep-link contract: desk surfaces outside the SPA (the onboarding banner
// bundle, the floating chat widget's degraded-connection CTA) can't call
// store.openSettings() directly - they navigate here instead, to
// "/jarvis/?settings=aimodels" and the like, and this reads the param once on
// mount. Kept as a plain Set rather than importing SettingsDialog.vue's PANES
// (a stack of defineAsyncComponent imports this file has no other reason to
// pull in) - keep it in sync with that object BY HAND, same duplication
// tradeoff panel_readiness.mjs makes against readiness.js.
const SETTINGS_DEEP_LINK_KEYS = new Set([
	"general",
	"usage",
	"activity",
	"shortcuts",
	"plan",
	"aimodels",
	"billing", // legacy alias, not a PANES key - SettingsDialog maps it to "usage"
	"branding",
	"usageadmin",
]);
// Only ever OPENS a pane - an absent or unrecognised key is silently ignored
// so a plain "/jarvis/" load is untouched. Strips the param either way it
// matched or not is irrelevant once read; leaving it in the URL would reopen
// the dialog on every refresh, which is the one thing a deep link must not do.
function openSettingsFromQuery() {
	const params = new URLSearchParams(window.location.search);
	const key = params.get("settings");
	if (key && SETTINGS_DEEP_LINK_KEYS.has(key)) store.openSettings(key);
	if (!params.has("settings")) return;
	params.delete("settings");
	const query = params.toString();
	const url = window.location.pathname + (query ? `?${query}` : "") + window.location.hash;
	history.replaceState(history.state, "", url);
}

// Boot gate: hold the routed page (NOT the shell chrome) until systemTimezone
// is configured — timeAgo strings render once, so a late setConfig would leave
// stale future-tense timestamps on the first paint. Production boot injects
// window.time_zone via jinjaBootData (www/jarvis.py), so this is synchronous;
// the awaited getChatUiSettings read in onMounted is only the dev-server /
// missing-key fallback.
const booted = ref(false);
if (typeof window !== "undefined" && window.time_zone) {
	setConfig("systemTimezone", window.time_zone);
	booted.value = true;
}

// Global shortcuts (§3.1). ⌘K moved here from the stock CommandPalette —
// JarvisCommandPalette is now built on plain Dialog and owns no keys itself
// (the stock component never mounted its Dialog subtree; DA-06 died with it).
useShortcuts([
	{ key: "o", ctrl: true, shift: true, handler: () => store.requestNewChat(router) },
	{ key: "b", ctrl: true, handler: () => (store.sidebarCollapsed = !store.sidebarCollapsed) },
	{ key: "k", meta: true, handler: () => (store.paletteOpen = !store.paletteOpen) },
]);

// Badge upkeep (D12): mount · route change · visibility (2s debounce) · 60s
// interval while the tab is visible.
let _interval = null;
function startInterval() {
	if (_interval) return;
	_interval = setInterval(() => store.refreshApprovalsCount(), 60000);
}
function stopInterval() {
	if (_interval) {
		clearInterval(_interval);
		_interval = null;
	}
}
let _visTimer = null;
function onVisibility() {
	if (document.visibilityState === "visible") {
		startInterval();
		clearTimeout(_visTimer);
		_visTimer = setTimeout(() => store.refreshApprovalsCount(), 2000);
	} else {
		stopInterval();
	}
}
const removeAfterEach = router.afterEach(() => {
	store.refreshApprovalsCount();
	// A tap that navigates from inside the phone drawer should leave it closed.
	store.mobileDrawerOpen = false;
});

// Global notifier: one app-scoped jarvis:event listener (attached below,
// detached on unmount) that turns background run:end / run:error /
// action:pending / approval:new / conversation:new into browser notifications
// (hidden tab), toasts (visible but elsewhere) and sidebar unread dots.
let _detachNotifier = null;

onMounted(async () => {
	openSettingsFromQuery();
	document.addEventListener("visibilitychange", onVisibility);
	if (document.visibilityState === "visible") startInterval();
	_detachNotifier = attachGlobalNotifier({ socket, router });

	// Sidebar fills without ChatView needing to be mounted (§3.1).
	store.loadConversations();
	store.refreshApprovalsCount();

	// Fallback only (vite dev server serves index.html without the jinja boot
	// injection, and older cached shells may miss the key): fetch the timezone
	// the old way before revealing the routed page. In production this branch
	// never runs — booted flipped synchronously in setup above.
	if (!booted.value) {
		try {
			const res = await api.getChatUiSettings();
			if (res?.time_zone) setConfig("systemTimezone", res.time_zone);
		} catch {
			// fall through — dayjsLocal degrades to browser-local parsing
		}
		booted.value = true;
	}

	// NOTE: no onboarding force-REDIRECT here — that path (the old D11 gate)
	// redirected not-ready users to /app/jarvis-onboarding (desk), which
	// redirects back to /jarvis/onboarding (SPA) → AppShell re-mounts → redirect
	// again: an infinite loop that bricked the whole SPA on any fresh
	// (not-onboarded) site. Instead, a not-ready workspace is gated by RENDERING
	// the <OnboardingGate> poster in place of the sidebar + routed page (see
	// `showGate` in setup + the template). A render can't navigate, so it can't
	// loop. The router beforeEach still bounces a fully-onboarded user off a
	// stale /onboarding link.
});

onBeforeUnmount(() => {
	document.removeEventListener("visibilitychange", onVisibility);
	clearTimeout(_visTimer);
	stopInterval();
	removeAfterEach();
	if (_detachNotifier) _detachNotifier();
});
</script>
