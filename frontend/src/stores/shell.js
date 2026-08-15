// Shared shell state (DESIGN-V3 §4) — a module-scope singleton, house style
// (no pinia). The shell (Sidebar/UserMenu/palette) and ChatView both consume
// it; write ownership is split by contract:
//   - only ChatView writes currentConvId / streamingConvId
//   - only shell components write paletteOpen / sidebar preference
//   - both sides may call loadConversations (idempotent, in-flight de-duped)
//   - `conversations` is externally written only via applyRemoteRename /
//     applyRemoteNew (DA-04) — called from ChatView's socket handlers and the
//     AppShell-level global notifier (notify/globalNotifier.js).
//   - unreadConvs: the global notifier marks, ChatView clears on open.
import { reactive, ref, computed } from "vue";
import { useStorage } from "@vueuse/core";
import { toast } from "frappe-ui";
import * as api from "@/api";
import { errHtml } from "@/lib/errors";
import { needsOnboarding } from "@/onboarding/readiness.js";

// ---- state ------------------------------------------------------------------
const conversations = ref([]); // [{name,title,last_active_at,starred,message_count}]
const conversationsLoading = ref(false);
const currentConvId = ref(null); // written by ChatView only
const streamingConvId = ref(null); // written by ChatView only
// Conversations with a finished (or errored) reply the user hasn't opened yet —
// the sidebar's unread dot (distinct from the streaming dot). Replaced
// wholesale on every write so Set mutations stay reactive.
const unreadConvs = ref(new Set());
const approvalsCount = ref(0);
const settingsOpen = ref(false); // the shell SettingsDialog binds to this
const settingsSection = ref("general"); // active pane key in the settings dialog
// True while the active settings pane is applying a change it cannot safely be
// unmounted mid-flight (currently only AiModelsPane during a model apply).
// Published by the pane itself (AiModelsPane watches LlmPoolEditor's busy.active
// and mirrors it here, clearing it both when the apply settles AND from an
// onUnmounted hook, so a pane torn down mid-apply can never leave this stuck
// true). Read by every writer of settingsSection - SettingsDialog's go() and
// openSettings() below - so a section switch can never abandon an in-flight
// apply regardless of which caller triggered it (jarvis#821 review).
const settingsApplying = ref(false);
// Bumped whenever the tenant's LLM config changes (a pool save, a subscription
// connect/disconnect) so views that snapshot chat-ui settings at mount - the
// chat model picker in particular - can re-fetch instead of showing a stale
// model list until a full page reload (jarvis: pin-not-refreshed-after-add).
const llmConfigVersion = ref(0);
function bumpLlmConfig() {
	llmConfigVersion.value += 1;
}
const pendingNewChat = ref(false); // consumed + cleared by ChatView
const paletteOpen = ref(false);

// Chat-scoped context published by ChatView WHILE MOUNTED (null otherwise), so
// the shell-level settings panes can read the current conversation's live stats
// and degrade gracefully (—/empty) on non-chat routes. Shape when set:
//   { conversationId, sessionStats:{ msgCount,userMsgCount,assistantMsgCount,
//       avgTokensPerMsg,convCount,starredCount,toolCount },
//     convAutoApply, autoApplyNote, modelLabel, ui }
// Tool-call counts are deliberately NOT here: they are read from the persisted
// rows (jarvis.chat.api.get_tool_activity), not from this live snapshot (#551).
const chatContext = ref(null);
function setChatContext(v) {
	chatContext.value = v;
}

// Actions with chat side-effects, registered by ChatView while mounted so panes
// can invoke them when present (and disable/hint when absent). ChatView
// registers { toggleAutoApply, clearAllHistory }.
const settingsActions = reactive({});
function registerSettingsActions(obj) {
	Object.assign(settingsActions, obj || {});
}
function clearSettingsActions() {
	for (const k of Object.keys(settingsActions)) delete settingsActions[k];
}

// Device-local behaviour prefs. These were owned by ChatView, but the
// settings dialog is now hoisted to the shell — a single source here keeps
// the GeneralPane toggle and ChatView's live gating in sync same-tab (a
// pane-local ref could not notify ChatView). localStorage is the boot-time
// cache (renders instantly, no network wait); `Jarvis User Settings` on the
// server is the roaming source of truth once GeneralPane's onMounted fetches
// get_my_settings and calls syncSettingsFromServer below. The "1"/"0"
// encoding is kept for backward compat with existing stored values.
// Default ON for new/unset devices — the live tool trace is the product's best
// trust signal, so hide it only when the user has explicitly turned it off
// ("0"). The server-side default matches (Jarvis User Settings.activity_detail
// defaults to 1) so the first get_my_settings sync can't flip a fresh device.
function _lsGet(key) {
	try {
		return localStorage.getItem(key);
	} catch (e) {
		return null;
	}
}
const _storedActivityDetail = _lsGet("jarvis-activity-detail");
const activityDetail = ref(_storedActivityDetail === null ? true : _storedActivityDetail === "1");
// `persist:false` is used only by syncSettingsFromServer, to apply a value
// that already came FROM the server without immediately POSTing it back.
function setActivityDetail(v, { persist = true } = {}) {
	activityDetail.value = !!v;
	try {
		localStorage.setItem("jarvis-activity-detail", v ? "1" : "0");
	} catch (e) {
		// localStorage throws in private mode and when the quota is full.
		// The preference is a nicety; losing it must never break the toggle.
	}
	if (persist) {
		// Fire-and-forget: never blocks the toggle UI; failure surfaces as a
		// toast (same as renameConversation/toggleStar below) but the local
		// (localStorage-cached) value already stuck, so the UI stays correct.
		api.updateMySettings({ activity_detail: activityDetail.value ? 1 : 0 }).catch((e) =>
			toast.error(errHtml(e))
		);
	}
}
// Per-user persona voice (Jarvis default / Jara). Same localStorage-cache +
// roaming-server pattern as activityDetail; a string, default "Jarvis". Voice
// only — the agent's tools, permissions, and behaviour are identical either way.
const _storedPersona = _lsGet("jarvis-preferred-persona");
const preferredPersona = ref(_storedPersona === "Jara" ? "Jara" : "Jarvis");
// In-flight counter for persona server writes (N3): incremented right before
// each api.updateMySettings persona POST, decremented when it settles. While
// non-zero, the persist:false adopt-from-server path (mount reconcile /
// GeneralPane's syncSettingsFromServer) must not overwrite the ref +
// localStorage, or it can clobber a write the user just made that hasn't
// round-tripped yet.
let _personaWritesInflight = 0;
// Request-sequence guard (N4): only the most recently issued POST may roll
// back on failure, so a stale rejected request can't stomp a newer accepted
// value.
let _personaSeq = 0;
// The last value the SERVER confirmed - a successful write, or a value the
// server pushed via syncSettingsFromServer. Rollback restores THIS, not the ref
// value at call time: during rapid toggles that ref may itself be an unconfirmed
// optimistic value, so rolling back to it could strand the pill on a value the
// server never accepted. Initialised to the booted local value as the baseline.
let _personaConfirmed = preferredPersona.value;
// Upper bound on how long a persona POST may hold the reconcile guard. Without
// it, one hung (never-settling) request would pin _personaWritesInflight above 0
// for the rest of the session, silently and permanently disabling every
// persist:false reconcile (cross-tab / cross-device sync). The seq guard still
// prevents a late real settlement from rolling back a newer value.
const PERSONA_WRITE_TIMEOUT_MS = 15000;
function setPreferredPersona(v, { persist = true } = {}) {
	const persona = v === "Jara" ? "Jara" : "Jarvis";
	if (!persist && _personaWritesInflight > 0) return;
	preferredPersona.value = persona;
	try {
		localStorage.setItem("jarvis-preferred-persona", persona);
	} catch (e) {
		// localStorage throws in private mode and when the quota is full.
		// The preference is a nicety; losing it must never break the picker.
	}
	if (!persist) {
		// This IS the server's value (mount reconcile / GeneralPane sync), so it
		// becomes the confirmed baseline a later failed write rolls back to.
		_personaConfirmed = persona;
		return;
	}
	const seq = ++_personaSeq;
	// Fire-and-forget, same as renameConversation/toggleStar: on failure roll the
	// ref + cache back to the last server-confirmed value so a device that never
	// actually wrote the server value doesn't keep showing it as current.
	_personaWritesInflight++;
	let settled = false;
	let timer = null;
	const release = () => {
		if (settled) return;
		settled = true;
		if (timer) clearTimeout(timer);
		_personaWritesInflight = Math.max(0, _personaWritesInflight - 1);
	};
	api.updateMySettings({ preferred_persona: persona })
		.then(() => {
			if (seq !== _personaSeq) return;
			// Server accepted this value: make it authoritative for BOTH the ref and
			// the confirmed baseline. Re-asserting the ref matters because the
			// timeout below can force-release the in-flight guard before a slow POST
			// settles, letting a persist:false reconcile set the ref back to the
			// pre-write value; without this the pill would stay stale after the POST
			// finally succeeds. On the normal path the ref is already `persona`, so
			// this is a no-op. (Ordering vs a reconcile is best-effort: _personaSeq
			// tracks only local writes, so in the rare window of a >timeout hang plus
			// a genuine cross-device change landing via reconcile, a still-current
			// local write re-asserts its own value; it self-heals on the next
			// reconcile - far rarer and milder than the stale pill this closes.)
			_personaConfirmed = persona;
			preferredPersona.value = persona;
			try {
				localStorage.setItem("jarvis-preferred-persona", persona);
			} catch (_e) {
				// see above: localStorage write failures are non-fatal
			}
		})
		.catch(() => {
			if (seq !== _personaSeq) return;
			preferredPersona.value = _personaConfirmed;
			try {
				localStorage.setItem("jarvis-preferred-persona", _personaConfirmed);
			} catch (_e) {
				// see above: localStorage write failures are non-fatal
			}
			toast.error(
				"Couldn't switch to " + persona + " - still using " + _personaConfirmed + "."
			);
		})
		.finally(release);
	// Safety net: force-release the guard after the bound even if the POST never
	// settles, so the reconcile path can always recover. Cleared on settle.
	timer = setTimeout(release, PERSONA_WRITE_TIMEOUT_MS);
}
const notifyEnabled = ref(
	typeof Notification !== "undefined" &&
		_lsGet("jarvis-notify") === "1" &&
		Notification.permission === "granted"
);
async function toggleNotify() {
	if (typeof Notification === "undefined") return;
	if (notifyEnabled.value) {
		notifyEnabled.value = false;
		try {
			localStorage.setItem("jarvis-notify", "0");
		} catch (e) {
			// localStorage throws in private mode and when the quota is full.
			// The preference is a nicety; losing it must never break the toggle.
		}
		api.updateMySettings({ notify_enabled: 0 }).catch((e) => toast.error(errHtml(e)));
		return;
	}
	let perm = Notification.permission;
	if (perm !== "granted") {
		try {
			perm = await Notification.requestPermission();
		} catch (e) {
			perm = "denied";
		}
	}
	if (perm === "granted") {
		notifyEnabled.value = true;
		try {
			localStorage.setItem("jarvis-notify", "1");
		} catch (e) {
			// localStorage throws in private mode and when the quota is full.
			// The preference is a nicety; losing it must never break the toggle.
		}
		api.updateMySettings({ notify_enabled: 1 }).catch((e) => toast.error(errHtml(e)));
	}
}
// Called by GeneralPane's onMounted (get_my_settings) once the server row is
// known. Never persists back (persist:false) — this IS the server's value.
// notify_enabled is ANDed with the live browser permission: the server field
// is account-level intent, but a device that never granted Notification
// permission still can't fire one, so local state must reflect that.
function syncSettingsFromServer(data) {
	if (!data) return;
	if (data.activity_detail !== undefined)
		setActivityDetail(!!data.activity_detail, { persist: false });
	if (data.notify_enabled !== undefined) {
		const allowed =
			typeof Notification !== "undefined" && Notification.permission === "granted";
		notifyEnabled.value = !!data.notify_enabled && allowed;
		try {
			localStorage.setItem("jarvis-notify", notifyEnabled.value ? "1" : "0");
		} catch (e) {
			// localStorage throws in private mode and when the quota is full.
			// The preference is a nicety; losing it must never break the toggle.
		}
	}
	if (data.preferred_persona !== undefined)
		setPreferredPersona(data.preferred_persona, { persist: false });
}

// Sidebar collapse: persisted preference (same localStorage key/values as
// today — existing prefs survive, D5) + a non-persisted narrow-screen
// override (auto-collapse at ≤820px; manual toggles there are temporary, D8).
const sidebarPref = useStorage("jarvis-sidebar", "open"); // 'open' | 'collapsed'
const _narrow = ref(false);
const _narrowOverride = ref(null); // null | 'open' | 'collapsed' (narrow-only, not persisted)
if (typeof window !== "undefined") {
	const mq = window.matchMedia("(max-width: 820px)");
	_narrow.value = mq.matches;
	mq.addEventListener("change", (e) => {
		_narrow.value = e.matches;
		_narrowOverride.value = null; // crossing the breakpoint resets to width-driven default
	});
}
const sidebarCollapsed = computed({
	get() {
		if (_narrow.value) {
			return _narrowOverride.value ? _narrowOverride.value === "collapsed" : true;
		}
		return sidebarPref.value === "collapsed";
	},
	set(v) {
		if (_narrow.value) _narrowOverride.value = v ? "collapsed" : "open";
		else sidebarPref.value = v ? "collapsed" : "open";
	},
});

// Phone layout (< 768px): the sidebar leaves the flow and becomes an off-canvas
// drawer (AppShell renders the scrim + slide-in; Sidebar drops its resize
// handle). `mobile` is the single source of truth for that switch — distinct
// from the 820px rail auto-collapse above, which still governs the 768–820px
// band. `mobileDrawerOpen` is transient (never persisted) and is forced shut
// when we leave phone width so it can't leak across the breakpoint.
const mobile = ref(false);
const mobileDrawerOpen = ref(false);
if (typeof window !== "undefined") {
	const mmq = window.matchMedia("(max-width: 767px)");
	mobile.value = mmq.matches;
	mmq.addEventListener("change", (e) => {
		mobile.value = e.matches;
		if (!e.matches) mobileDrawerOpen.value = false;
	});
}

// Sidebar width when expanded: persisted per device, drag-resizable via the
// handle in Sidebar.vue. Clamped to [MIN, MAX]; the collapsed rail (48px) is
// fixed and unaffected. Reads clamp too, so a stale/hand-edited value can't
// wedge the rail off-screen.
const SIDEBAR_MIN_W = 180;
const SIDEBAR_MAX_W = 460;
const _sidebarWidth = useStorage("jarvis-sidebar-width", 220);
const clampSidebarWidth = (n) =>
	Math.min(SIDEBAR_MAX_W, Math.max(SIDEBAR_MIN_W, Math.round(Number(n) || 220)));
const sidebarWidth = computed({
	get() {
		return clampSidebarWidth(_sidebarWidth.value);
	},
	set(v) {
		_sidebarWidth.value = clampSidebarWidth(v);
	},
});

// ---- actions (all errors → toast; never throw to callers) -------------------
let _convsInflight = null;
async function loadConversations() {
	if (_convsInflight) return _convsInflight;
	conversationsLoading.value = true;
	_convsInflight = (async () => {
		try {
			conversations.value = (await api.listConversations()) || [];
			refreshApprovalsCount(); // poll-on-activity parity (D12)
		} catch (e) {
			toast.error(errHtml(e));
		} finally {
			conversationsLoading.value = false;
			_convsInflight = null;
		}
	})();
	return _convsInflight;
}

// AppShell fires this from four triggers (mount · route change · visibility ·
// 60s interval) that often co-fire; the in-flight de-dupe coalesces them into
// one request, and a hidden tab skips entirely (the visibility trigger
// refreshes on return).
let _approvalsInflight = null;
function refreshApprovalsCount() {
	if (typeof document !== "undefined" && document.hidden) return Promise.resolve();
	if (_approvalsInflight) return _approvalsInflight;
	_approvalsInflight = (async () => {
		try {
			approvalsCount.value = (await api.approvalsPendingCount()) || 0;
		} catch (e) {
			/* badge is best-effort */
		} finally {
			_approvalsInflight = null;
		}
	})();
	return _approvalsInflight;
}

async function renameConversation(name, title) {
	const conv = conversations.value.find((c) => c.name === name);
	const prev = conv ? conv.title : "";
	if (conv) conv.title = title; // optimistic
	try {
		await api.renameConversation(name, title);
	} catch (e) {
		if (conv) conv.title = prev;
		toast.error(errHtml(e));
	}
}

async function toggleStar(name) {
	const conv = conversations.value.find((c) => c.name === name);
	if (!conv) return;
	const next = conv.starred ? 0 : 1;
	conv.starred = next; // optimistic — regroups instantly
	try {
		await api.setStar(name, next);
	} catch (e) {
		conv.starred = next ? 0 : 1;
		toast.error(errHtml(e));
	}
}

// Confirm is handled by the caller (ConversationRow uses frappe-ui
// confirmDialog). ChatView reacts to the removal via its store watcher.
async function archiveConversation(name) {
	try {
		await api.archiveConversation(name);
		conversations.value = conversations.value.filter((c) => c.name !== name);
		toast.success("Chat deleted");
	} catch (e) {
		toast.error(errHtml(e));
	}
}

// D10 — New Chat from any route: one mechanism for on-chat and cross-route.
function requestNewChat(router) {
	pendingNewChat.value = true;
	const name = router.currentRoute.value.name;
	if (name !== "Chat" && name !== "Conversation") router.push({ name: "Chat" });
}

// D9 — the settings dialog lives at the shell now (SettingsDialog.vue), so it
// opens over ANY route without a chat redirect. Optional `section` targets a
// pane; a non-string arg (e.g. a legacy `router` caller not yet updated) falls
// back to "general" so old call-sites keep opening the dialog harmlessly.
//
// Guarded here (jarvis#810), not at each call site, so EVERY caller is
// covered without auditing them one by one. GeneralPane/UserMenu/ChatView
// only ever call this post-onboarding anyway, but AppShell's openSettingsFromQuery()
// reads a `?settings=` deep link on mount with no onboarding check of its own,
// which let a never-onboarded workspace open the dialog straight over the
// gate. needsOnboarding() shares AppShell's own memoized readiness promise
// (readiness.js's checkReady()), so this costs no extra round-trip: it's
// already in flight or resolved by the time any real caller fires.
//
// Also guarded on settingsApplying (jarvis#821 review): this is a second writer
// of settingsSection alongside SettingsDialog's rail go() - GeneralPane's "AI
// models" buttons, UserMenu, ChatView and AppShell's deep-link handler all reach
// settingsSection through here, not through go(), so a lock enforced only in
// go() left every one of those callers free to switch away from AiModelsPane
// mid-apply. settingsApplying can only be true while the dialog is already open
// on the applying pane (see its own doc above), so refusing here never blocks a
// legitimate first open.
async function openSettings(section) {
	if (await needsOnboarding()) return;
	if (settingsApplying.value) return;
	settingsOpen.value = true;
	settingsSection.value = typeof section === "string" && section ? section : "general";
}

// ---- socket contract (§14 DA-04) — called by ChatView's handlers only ------
function applyRemoteRename(name, title) {
	const conv = conversations.value.find((c) => c.name === name);
	if (conv && title) conv.title = title;
}

let _remoteNewTimer = null;
function applyRemoteNew() {
	if (_remoteNewTimer) return;
	_remoteNewTimer = setTimeout(() => {
		_remoteNewTimer = null;
		loadConversations();
	}, 500);
}

// ---- unread signal (NOTIFY-APPROVALS Part 1) --------------------------------
// The global notifier marks a conversation unread when its run ends off-screen;
// ChatView clears it the moment that conversation is opened.
function markUnread(id) {
	if (!id || unreadConvs.value.has(id)) return;
	unreadConvs.value = new Set(unreadConvs.value).add(id);
}
function clearUnread(id) {
	if (!id || !unreadConvs.value.has(id)) return;
	const next = new Set(unreadConvs.value);
	next.delete(id);
	unreadConvs.value = next;
}

// reactive() unwraps the refs/computeds, so consumers read and write plain
// properties: `store.pendingNewChat = false`, `watch(() => store.paletteOpen)`.
const store = reactive({
	// state
	conversations,
	conversationsLoading,
	currentConvId,
	streamingConvId,
	unreadConvs,
	approvalsCount,
	settingsOpen,
	settingsSection,
	settingsApplying,
	llmConfigVersion,
	chatContext,
	settingsActions,
	activityDetail,
	notifyEnabled,
	preferredPersona,
	pendingNewChat,
	paletteOpen,
	sidebarPref,
	sidebarCollapsed,
	sidebarWidth,
	SIDEBAR_MIN_W,
	SIDEBAR_MAX_W,
	mobile,
	mobileDrawerOpen,
	// actions
	loadConversations,
	refreshApprovalsCount,
	renameConversation,
	toggleStar,
	archiveConversation,
	requestNewChat,
	openSettings,
	setChatContext,
	registerSettingsActions,
	clearSettingsActions,
	setActivityDetail,
	setPreferredPersona,
	toggleNotify,
	syncSettingsFromServer,
	applyRemoteRename,
	applyRemoteNew,
	markUnread,
	clearUnread,
	bumpLlmConfig,
});

export function useShellStore() {
	return store;
}
