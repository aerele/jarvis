import { createRouter, createWebHistory } from "vue-router";
import { isWorkspaceReady } from "@/onboarding/readiness.js";
// STATIC import: the main chunk = shell + store + ChatView (chat is the app
// home, D33); every other page is a route-level dynamic import.
import ChatView from "@/views/ChatView.vue";

// Both flags are boot-time globals (jarvis/www/jarvis.py:90-95), frozen for the
// page's life — so this is a plain function, not a reactive check.
function supportGuard(to, from, next) {
	next(window.support_available && window.has_support_access ? undefined : { name: "Chat" });
}

const routes = [
	{ path: "/", name: "Chat", component: ChatView, meta: { chat: true } },
	{ path: "/c/:id", name: "Conversation", component: ChatView, meta: { chat: true } },
	// /skills renders the two-tab shell (Skills | Learning). The name stays
	// "SkillsList" so every router.push({name:'SkillsList'}), the sidebar link,
	// the command palette and SkillsList's own breadcrumb keep resolving here;
	// the #skills legacy hash deep-link (HASH_ROUTES below) is unaffected.
	{
		path: "/skills",
		name: "SkillsList",
		component: () => import("@/pages/skills/SkillsPage.vue"),
	},
	{
		path: "/skills/new",
		name: "SkillNew",
		component: () => import("@/pages/skills/SkillDetail.vue"),
		props: { isNew: true },
	},
	{
		path: "/skills/:id",
		name: "SkillDetail",
		component: () => import("@/pages/skills/SkillDetail.vue"),
		props: true,
	},
	// First-run wizard (managed signup or self-hosted connect) — System-Manager
	// only; guard redirects others to Chat. Reached via the chat welcome card
	// or the desk banner, not a forced redirect (see beforeEach below).
	{
		path: "/onboarding",
		name: "Onboarding",
		component: () => import("@/views/OnboardingView.vue"),
		// Chrome-less: a first-run customer hasn't onboarded yet, so the full app
		// sidebar/header (Chat/Skills/Macros/…) is noise — AppShell hides them.
		meta: { chromeless: true },
		// PART 4 REVISED TASK 49(c): a Jarvis Admin (not necessarily SM) may onboard.
		beforeEnter: (to, from, next) => {
			next(
				window.is_system_manager || window.is_jarvis_admin ? undefined : { name: "Chat" }
			);
		},
	},
	// Support is a STANDALONE, shell-less space: chromeless drops both the
	// sidebar and the #app-header strip (AppShell.vue:18-33), and SupportShell
	// supplies its own top bar in their place. Static /support/new is registered
	// BEFORE /support/:ticket — house convention, and otherwise "new" matches as
	// a ticket id.
	//
	// Dual kill-switch (P2) on ALL THREE routes: support must be enabled
	// fleet-wide AND the user must have access. Deep-linking straight to
	// /support/TKT-1 must not bypass what /support enforces.
	{
		path: "/support",
		name: "Support",
		component: () => import("@/pages/support/SupportListPage.vue"),
		meta: { chromeless: true },
		beforeEnter: supportGuard,
	},
	{
		path: "/support/new",
		name: "SupportNew",
		component: () => import("@/pages/support/SupportNewPage.vue"),
		meta: { chromeless: true },
		beforeEnter: supportGuard,
	},
	{
		path: "/support/:ticket",
		name: "SupportTicket",
		component: () => import("@/pages/support/SupportThreadPage.vue"),
		meta: { chromeless: true },
		beforeEnter: supportGuard,
	},
	{
		path: "/macros",
		name: "MacrosList",
		component: () => import("@/pages/macros/MacrosList.vue"),
	},
	{
		path: "/macros/runs",
		name: "MacroRuns",
		component: () => import("@/pages/macros/MacrosList.vue"),
		props: { tab: "runs" },
	},
	{
		path: "/macros/new",
		name: "MacroNew",
		component: () => import("@/pages/macros/MacroDetail.vue"),
		props: { isNew: true },
	},
	{
		path: "/macros/:id",
		name: "MacroDetail",
		component: () => import("@/pages/macros/MacroDetail.vue"),
		props: true,
	},
	// Triggers: /triggers is the hash-tabbed shell (Triggers | #activity);
	// /triggers/new and /triggers/:id share the detail page (isNew prop, the
	// Macros shape). "new" is registered BEFORE :id so it can't be shadowed.
	{
		path: "/triggers",
		name: "TriggersPage",
		component: () => import("@/pages/triggers/TriggersPage.vue"),
	},
	{
		path: "/triggers/new",
		name: "TriggerNew",
		component: () => import("@/pages/triggers/TriggerDetail.vue"),
		props: { isNew: true },
	},
	{
		path: "/triggers/:id",
		name: "TriggerDetail",
		component: () => import("@/pages/triggers/TriggerDetail.vue"),
		props: true,
	},
	// Dashboards: /dashboards is the hash-tabbed builder/saved shell (no hash or
	// "#builder" = Builder, "#saved" = Saved); /dashboards/:id renders a saved
	// dashboard read-only.
	{
		path: "/dashboards",
		name: "DashboardsPage",
		component: () => import("@/pages/dashboards/DashboardsPage.vue"),
	},
	{
		path: "/dashboards/:id",
		name: "DashboardView",
		component: () => import("@/pages/dashboards/DashboardView.vue"),
		props: true,
	},
	{ path: "/files", name: "FilesList", component: () => import("@/pages/files/FilesList.vue") },
	// §15.2: both approval routes render the two-pane board (the :id row is
	// selected in place); names kept so nav highlighting + router.push targets
	// keep working. The board reads route.params itself — no props.
	{
		path: "/approvals",
		name: "ApprovalsList",
		component: () => import("@/pages/approvals/ApprovalsBoard.vue"),
	},
	{
		path: "/approvals/:id",
		name: "ApprovalDetail",
		component: () => import("@/pages/approvals/ApprovalsBoard.vue"),
	},
	{
		path: "/agents",
		name: "AgentsList",
		component: () => import("@/pages/agents/AgentsList.vue"),
	},
	// Legacy round-2 tab routes — static, registered BEFORE :slug (§9). Point at
	// the current hash-tabs (mine→Installed, activity→Activity); admin is now a
	// per-agent detail tab, so it falls back to the catalog.
	{ path: "/agents/mine", redirect: "/agents#installed" },
	{ path: "/agents/activity", redirect: "/agents#activity" },
	{ path: "/agents/admin", redirect: "/agents" },
	{
		path: "/agents/:slug",
		name: "AgentDetail",
		component: () => import("@/pages/agents/AgentDetail.vue"),
		props: true,
	},
	// Catch-all: an unknown path (e.g. the retired desk deep-link
	// "/jarvis/account" - its content now lives in the SPA settings dialog)
	// used to render a blank white screen. Vue Router 4 ranks a static path
	// above a wildcard regardless of registration order, so this cannot
	// shadow any route above it - it only ever matches when nothing else
	// did. This is a plain path redirect, unrelated to the beforeEach
	// onboarding guard below (which only ever redirects AWAY FROM
	// /onboarding, never forces a user INTO it - see the D11 note there).
	{ path: "/:pathMatch(.*)*", name: "NotFound", redirect: "/" },
];

// Served under /jarvis (website_route_rules catch-all → www/jarvis page).
const router = createRouter({
	history: createWebHistory("/jarvis"),
	routes,
});

// First-run guard: bounce a fully-onboarded user away from a stale
// /onboarding link back to Chat. Readiness is resolved once per page load via
// the shared, memoized helper (see @/onboarding/readiness.js) — the AppShell
// onboarding gate reads the SAME promise, so the two stay consistent and only
// one backend round-trip fires. Fail-open lives in the helper.
//
// The reverse direction (forcing a NOT-ready user into the wizard) is
// deliberately NOT a redirect here: the old D11 forced redirect looped between
// the desk and SPA onboarding pages and bricked fresh sites. Instead, AppShell
// blocks the app with a rendered, no-sidebar poster when not ready — a render,
// not a navigation, so it cannot loop.
// The most recent navigation target, recorded here (where `to` is always
// populated) so router.onError can reload straight to the intended path even
// when its own `to` argument is absent (see the stale-chunk recovery below).
let _pendingTarget = null;
router.beforeEach(async (to) => {
	_pendingTarget = to && to.fullPath;
	const ready = await isWorkspaceReady();
	if (ready && to.name === "Onboarding") {
		return { name: "Chat" };
	}
	return true;
});

// Legacy hash deep-links (moved out of ChatView, §9): /jarvis/#skills etc.
// map to real routes. Applies only on chat routes so doc-page tab hashes
// (#overview on /agents/:slug) are unaffected.
const HASH_ROUTES = {
	"#skills": "/skills",
	"#macros": "/macros",
	"#filebox": "/files",
	"#approvals": "/approvals",
	"#agents": "/agents",
};
function applyLegacyHash() {
	const target = HASH_ROUTES[window.location.hash];
	if (!target) return;
	const current = router.currentRoute.value;
	if (current.name && current.meta.chat !== true) return;
	router.replace(target);
}
router.isReady().then(applyLegacyHash);
window.addEventListener("hashchange", applyLegacyHash);

// ── Recover from a stale SPA tab after a deploy ──────────────────────────────
// Routes are lazy-loaded (`() => import(...)`), and every build content-hashes
// its chunks. After a redeploy, a tab that loaded the OLD index still asks for
// the OLD chunk filenames — which no longer exist — so navigating to a not-yet-
// loaded route (Macros, File Box, …) rejects with "Failed to fetch dynamically
// imported module" and the navigation silently aborts (the user is stranded on
// whatever page was already in memory). The fix: on a chunk-load error, do ONE
// full-page load of the intended path so the browser pulls the fresh index +
// chunks. A short-lived sessionStorage stamp prevents a reload loop when the
// chunk is genuinely missing (a real 404, not just a stale tab).
const _RELOAD_STAMP = "jarvis:stale-chunk-reload-at";
const _RELOAD_WINDOW_MS = 15000;

function _isChunkLoadError(err) {
	const msg = String((err && (err.message || err)) || "");
	return (
		/dynamically imported module/i.test(msg) ||
		/Importing a module script failed/i.test(msg) ||
		/error loading dynamically imported module/i.test(msg) ||
		/ChunkLoadError/i.test(msg) ||
		/Loading (CSS )?chunk .* failed/i.test(msg) ||
		/Failed to fetch dynamically imported module/i.test(msg)
	);
}

function _recoverFromStaleChunk(targetFullPath) {
	let last = 0;
	try {
		last = Number(sessionStorage.getItem(_RELOAD_STAMP)) || 0;
	} catch {
		last = 0;
	}
	// Already reloaded very recently for this — the chunk is genuinely gone, not
	// just stale. Stop, and let the error surface instead of looping.
	if (last && Date.now() - last < _RELOAD_WINDOW_MS) return false;
	try {
		sessionStorage.setItem(_RELOAD_STAMP, String(Date.now()));
	} catch {
		// sessionStorage unavailable (private mode) — reload once anyway.
	}
	const path =
		targetFullPath || window.location.pathname + window.location.search + window.location.hash;
	// router base is "/jarvis"; to.fullPath is base-relative (e.g. "/macros").
	const url = path.startsWith("/jarvis")
		? path
		: "/jarvis" + (path.startsWith("/") ? path : "/" + path);
	window.location.assign(url);
	return true;
}

router.onError((error, to) => {
	if (_isChunkLoadError(error)) {
		// Prefer the router's own `to`, but fall back to the target recorded in
		// beforeEach (onError's `to` is empty in some failure paths).
		_recoverFromStaleChunk((to && to.fullPath) || _pendingTarget);
	}
});

// Vite's own signal when a preloaded module fails to fetch — fires before the
// import promise rejects, so it's the earliest chance to recover. Both handlers
// share the sessionStorage guard, so at most one reload happens.
window.addEventListener("vite:preloadError", (event) => {
	if (_isChunkLoadError(event && event.payload)) {
		event.preventDefault();
		// Reload straight to the route being navigated to (recorded in
		// beforeEach), not the page currently in memory.
		_recoverFromStaleChunk(_pendingTarget);
	}
});

export default router;
