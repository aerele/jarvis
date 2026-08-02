<template>
	<div class="flex h-full flex-col overflow-hidden">
		<!-- friendly no-access state: get_dashboards_caps rejected with a real 403
		     (TriggersPage probe precedent - transient failures retry, never block) -->
		<template v-if="accessDenied">
			<div class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
				<FeatherIcon name="bar-chart-2" class="size-7.5 text-ink-gray-5" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8"
						>No access to Dashboards</span
					>
					<span class="text-p-base text-ink-gray-6">
						Ask your {{ agentName }} admin for access to dashboards.
					</span>
				</div>
			</div>
		</template>

		<template v-else>
			<!-- THE LayoutHeader for /dashboards (both tabs; SavedDashboardsTab's
			     ListPage runs show-header=false) -->
			<LayoutHeader>
				<template #left-header>
					<Breadcrumbs
						:items="[{ label: 'Dashboards', route: { name: 'DashboardsPage' } }]"
					/>
				</template>
				<template #right-header>
					<Button
						v-if="activeTab === 'saved'"
						variant="solid"
						label="New dashboard"
						iconLeft="plus"
						@click="newDashboard"
					/>
				</template>
			</LayoutHeader>

			<TabBar
				class="shrink-0"
				:tabs="TABS"
				:model-value="activeTab"
				@update:model-value="setTab"
			/>

			<!-- ============ Builder tab: canvas over chat, drag-split ============ -->
			<!-- v-show, NOT v-if: switching to Saved must not unmount the chat
			     pane. Its socket listener is torn down on unmount and the agent's
			     kind:"canvas" frame is a ONE-SHOT publish, so a build that lands
			     while the user is looking at Saved would never reach the canvas. -->
			<div
				v-show="activeTab === 'builder'"
				ref="builderEl"
				class="flex min-h-0 flex-1 flex-col"
			>
				<!-- canvas pane (the surface's one solid action lives here) -->
				<div
					class="flex min-h-0 flex-1 flex-col"
					:class="resizing ? 'pointer-events-none select-none' : ''"
				>
					<div
						class="flex shrink-0 items-center justify-between gap-2 border-b px-4 py-2"
					>
						<div class="flex min-w-0 items-center gap-2">
							<span class="text-base font-semibold text-ink-gray-9">Canvas</span>
							<!-- informational (which dashboard is loaded), not a dirty
							     warning - so gray, not orange (§1.2 hue = meaning) -->
							<Badge
								v-if="editingDetail"
								theme="gray"
								variant="subtle"
								:label="`Editing ${
									editingDetail.dashboard_title || editingDetail.name
								}`"
							/>
						</div>
						<div class="flex shrink-0 items-center gap-3">
							<router-link
								v-if="savedName"
								:to="{ name: 'DashboardView', params: { id: savedName } }"
								class="text-sm text-ink-blue-link"
							>
								View dashboard
							</router-link>
							<!-- named render theme - the dashboard's look, not the app's -->
							<Dropdown :options="themeOptions">
								<Button
									variant="ghost"
									:label="themeLabel(builderTheme)"
									iconLeft="droplet"
									iconRight="chevron-down"
								/>
							</Dropdown>
							<Button
								variant="solid"
								label="Save dashboard"
								:disabled="!builderHtml"
								:loading="repairing"
								@click="openSave"
							/>
						</div>
					</div>
					<DashboardCanvas
						ref="canvasRef"
						class="min-h-0 flex-1"
						mode="builder"
						:html="builderHtml"
						:caps="caps"
						:theme="builderTheme"
						@sources="(s) => (detectedSources = s)"
					/>
				</div>

				<!-- drag divider (Sidebar's resize pattern, rotated). While dragging,
				     the canvas wrapper above goes pointer-events-none so the iframe
				     can't swallow the mousemoves. -->
				<div
					class="group relative z-10 flex h-2.5 shrink-0 cursor-row-resize items-center justify-center"
					role="separator"
					aria-orientation="horizontal"
					title="Drag to resize · double-click to reset"
					@mousedown.prevent="startResize"
					@dblclick="resetSplit"
				>
					<span
						class="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 transition-colors"
						:class="
							resizing
								? 'bg-surface-gray-4'
								: 'bg-transparent group-hover:bg-surface-gray-4'
						"
					/>
					<span
						class="relative h-1 w-7 rounded-full bg-surface-gray-4 transition-opacity"
						:class="resizing ? 'opacity-100' : 'opacity-30 group-hover:opacity-100'"
					/>
				</div>

				<DashboardChatPane
					ref="chatPane"
					class="shrink-0 border-t"
					:style="{ height: chatPct + '%' }"
					:caps="caps"
					:theme="builderTheme"
					:editing-name="agentEditingName"
					@canvas="onCanvas"
					@reset="resetBuilder"
				/>
			</div>

			<!-- ============ Saved tab ============ -->
			<SavedDashboardsTab v-if="activeTab === 'saved'" class="min-h-0 flex-1" />

			<SaveDashboardDialog
				v-model="saveOpen"
				:caps="caps"
				:html="builderHtml"
				:sources="detectedSources"
				:editing="editingDetail"
				:conversation="chatConv"
				:theme="builderTheme"
				@saved="onSaved"
				@fix-in-chat="fixInChat"
			/>

			<!-- Discard confirm. NOT frappe-ui's confirmDialog: that has room for
			     exactly one action, and telling someone who is about to lose a
			     canvas that "its chat stays in your conversations" is only useful
			     with a way to go there. -->
			<Dialog
				v-model="discardOpen"
				:options="{
					title: discardCopy.title,
					message: discardCopy.message,
					actions: discardActions,
				}"
				@close="settleDiscard(false)"
			/>
		</template>
	</div>
</template>

<script setup>
// DashboardsPage - the routed component for /dashboards: hash-synced tab shell
// (TriggersPage precedent; no hash or "#builder" = Builder, "#saved" = Saved)
// plus the single get_dashboards_caps probe that feeds both tabs. The Builder
// tab is the core UX: the sandboxed canvas on top, the assistant chat below,
// split by a draggable divider (persisted %). The chat's canvas frames pull
// the agent's html artifact onto the canvas; Save opens the scope/title
// dialog; ?edit=<name> seeds the canvas from a saved dashboard for editing, and
// ?chat=<conversation>&canvas=<message> promotes a build the user found in main
// chat back onto this builder, where it renders WITH its data.
// Probe failures follow the TriggersPage rule: a genuine 403 shows the
// no-access state; a transient 500/network blip retries once and otherwise
// proceeds with default caps rather than blocking an authorized user.
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useStorage } from "@vueuse/core";
import { Badge, Breadcrumbs, Button, Dialog, Dropdown, FeatherIcon, toast } from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import TabBar from "@/components/list/TabBar.vue";
import { session } from "@/data/session";
import { getCanvas } from "@/api";
import { agentName } from "@/branding";
import { getDashboardsCaps, getDashboard, getDashboardConversation } from "@/api/dashboards";
import { builderCanvasFrame } from "@/lib/dashboardRestore";
import {
	adoptionIdentity,
	agentRevisionTarget,
	resumesAdoption,
	wouldDiscardOnPromotion,
} from "@/lib/dashboardOpen";
import { DEFAULT_THEME, THEME_OPTIONS, themeKey, themeLabel } from "@/lib/dashboardThemes";
import DashboardCanvas from "./DashboardCanvas.vue";
import DashboardChatPane from "./DashboardChatPane.vue";
import SavedDashboardsTab from "./SavedDashboardsTab.vue";
import SaveDashboardDialog from "./SaveDashboardDialog.vue";

const route = useRoute();
const router = useRouter();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

const TABS = [
	{ label: "Builder", value: "builder" },
	{ label: "Saved", value: "saved" },
];

// caps flow down reactively - the save dialog's scope options appear once the
// probe lands; the default keeps a plain User-scoped save path.
const caps = ref({
	creatable_scopes: ["User"],
	manageable_roles: [],
	max_sources: 0,
	max_html_chars: 0,
	max_rows: 0,
});
const accessDenied = ref(false);

// ── hash-synced tabs (TriggersPage precedent) ────────────────────────────────
const activeTab = ref("builder");
function applyHash() {
	// tolerate suffixed forms like "#saved?x=1"
	const h = (route.hash || "").replace(/^#/, "").split("?")[0];
	activeTab.value = h === "saved" ? "saved" : "builder";
}
function setTab(v) {
	if (v === activeTab.value) return;
	activeTab.value = v;
	router.push({ hash: v === "builder" ? "" : `#${v}`, query: route.query });
}
applyHash();
// back/forward restores the tab (guard to this route so other pages' hashes
// are ignored - the SkillsPage rule)
watch(
	() => route.hash,
	() => {
		if (route.name === "DashboardsPage") applyHash();
	}
);

// ── builder state ────────────────────────────────────────────────────────────
const builderHtml = ref("");
const editingDetail = ref(null); // full get_dashboard detail while editing
const savedName = ref(""); // last save's name → the "View dashboard" link
const detectedSources = ref([]); // parsed #jarvis-sources (DashboardCanvas emit)
const saveOpen = ref(false);
const chatPane = ref(null);
const canvasRef = ref(null);
// Named render theme; "Jarvis" (the app's design language) unless picked or
// seeded from the edited dashboard.
const builderTheme = ref(DEFAULT_THEME);
const themeOptions = THEME_OPTIONS.map((t) => ({
	label: t.label,
	onClick: () => (builderTheme.value = t.key),
}));

// Same per-user keys DashboardChatPane persists under (vueuse syncs same-
// document instances) — the page seeds/clears them around edit/new so the chat
// pane resumes the right thread and data mode instead of a stale sticky one.
const chatConv = useStorage(`jarvis-dash-conv-${session.user || "anon"}`, "");
const dashDataMode = useStorage(`jarvis-dash-datamode-${session.user || "anon"}`, "auto");
// The dashboard this builder is EDITING, and the message whose canvas it last
// rendered. Both are page state that a navigation would otherwise drop - and
// dropping the first one is not cosmetic: without it a remount restores the
// canvas but not the binding, so "Save dashboard" writes a SECOND row instead of
// updating the one on screen.
const editingSticky = useStorage(`jarvis-dash-editing-${session.user || "anon"}`, "");
const canvasMsg = useStorage(`jarvis-dash-canvasmsg-${session.user || "anon"}`, "");
// The row an ADOPTED promotion is bound to, while the canvas still holds the
// promoted build. Sticky like the two above, and for the same reason: a remount
// has to be able to tell "editing that row" from "holding a build that row is
// behind". Empty again the moment the two are one document - after a Save, or
// after ?edit= puts the row's own html on the canvas.
const adoptedRow = useStorage(`jarvis-dash-adopted-${session.user || "anon"}`, "");

// What the builder chat tells the AGENT it is revising - not the same as what
// Save updates while an adoption is active. See agentRevisionTarget().
const agentEditingName = computed(() =>
	agentRevisionTarget({
		editingName: (editingDetail.value || {}).name || "",
		adoptionActive: !!adoptedRow.value,
	})
);

// A pending ?edit=<name> deep-link, or - on a plain remount - the dashboard the
// builder was editing when it went away. Read synchronously at setup, i.e.
// BEFORE the chat pane mounts and replays its transcript's canvas: an explicit
// edit target owns the canvas, so its restore must not flash a build first.
const routeEdit = typeof route.query.edit === "string" ? route.query.edit : "";
// ...unless this mount is resuming an ADOPTED promotion, where the canvas the
// user left was the transcript's build and not the row's html. Then the seed is
// deliberately empty, so the pane's own transcript restore is the thing that
// refills the canvas (onCanvas holds restores off while a seed is pending) and
// resumeAdoption() brings back the identity WITHOUT the stored document.
// Decided synchronously here, from the sticky slots, for the same reason the
// seed is: the pane mounts first.
const adoptionResume = resumesAdoption({
	routeEdit,
	adoptedRow: adoptedRow.value,
	editingSticky: editingSticky.value,
	canvasMsg: canvasMsg.value,
	chatConv: chatConv.value,
});
const editSeed = ref(routeEdit || (adoptionResume ? "" : editingSticky.value));

// ?chat=<conversation>&canvas=<message>: main chat handing a builder
// conversation's artifact back here ("Open in Dashboards"). Read at setup for
// the same reason as ?edit=, and watched below so an already-mounted builder
// honours the deep-link too.
const routeChat = typeof route.query.chat === "string" ? route.query.chat : "";
const routeCanvas = typeof route.query.canvas === "string" ? route.query.canvas : "";
// ...and `dash=<name>`: the saved dashboard this conversation already has, sent
// only when main chat promotes a build made AFTER it. The promotion ADOPTS that
// row - badge and "Save changes" keep pointing at it - instead of dropping the
// identity and writing a second row for the same dashboard. User-influencable
// (it is a URL), and deliberately not trusted: the only thing done with it is a
// permission-gated get_dashboard, so an unknown or foreign name simply fails and
// degrades to an identity-less promotion.
const routeDash = typeof route.query.dash === "string" ? route.query.dash : "";
// A promotion the page owes the user but has not finished, held exactly like
// `editSeed`: an explicit deep-link owns the canvas, so the chat pane's own
// transcript restore must not land on it first. The pane's onMounted fires
// BEFORE this page's, and the promotion waits on the caps probe, so without
// this the restore reliably wins the race - and its canvas then reads as
// "unsaved work" to the discard guard, popping a confirm over the very document
// the user asked to open. Taken here for the mount path (the promotion itself
// only starts after the caps probe) and by promoteFromChat for a live
// deep-link; cleared when the promotion settles, either way.
const promotionPending = ref(!!(routeChat && routeCanvas));

// Message ids whose artifact could not be replayed (the File was purged, the
// conversation was reassigned). The pane emits a restore frame on EVERY
// transcript load - including the 300ms-debounced refetch behind each realtime
// frame - so without this latch an unreadable artifact means an unbounded run of
// silent, failing get_canvas calls (a full File load + a disk read each time).
const failedRestores = new Set();

// A restore frame that could not be replayed. The latch is the loop guard above;
// what follows it is the self-heal an ADOPTION needs.
//
// An adopted promotion keeps the identity of a row whose html it never loaded,
// so an artifact that has since been purged leaves the builder with nothing to
// show and nothing to fall back to: an empty canvas under an "Editing X" badge,
// Save disabled, and the same outcome on every later mount. The promoted build
// is genuinely unrecoverable — but the ROW still holds a document, and opening
// it is what the mount would have done before the adoption existed. loadEdit
// ends the adoption on its own (the canvas becomes the row's own html, so the
// two are one document again and `canvasMsg`/`adoptedRow` are cleared), and the
// latch means this can run at most once per message.
function restoreFailed(message_id) {
	failedRestores.add(message_id);
	if (message_id !== canvasMsg.value || !adoptedRow.value) return;
	const name = adoptedRow.value;
	// the seed discipline the other loadEdit callers follow: it holds further
	// restores off the canvas until this one settles, and loadEdit clears it
	editSeed.value = name;
	loadEdit(name, { deepLink: false });
}

// Chat drew/updated an artifact: pick the LAST html item and pull its
// render-ready content onto the canvas.
//
// `restore` marks a replay of the canvas this builder last rendered, found
// again in the loaded transcript (the chat pane emits one on every transcript
// load), rather than a live socket frame. It rehydrates the canvas after a
// navigation dropped it, and must never
// overwrite what is already on screen - a live frame for the turn in flight
// always wins over a replay of an older turn.
// Answers whether the artifact actually reached the canvas, so a caller that
// repointed the builder at it (the ?chat= promotion) can tell a render from a
// no-op instead of leaving the previous document on screen under a new thread.
async function onCanvas({ message_id, items, restore }) {
	if (restore && (builderHtml.value || editSeed.value || promotionPending.value)) return false;
	if (restore && failedRestores.has(message_id)) return false;
	const htmlItem = [...(items || [])].reverse().find((it) => it && it.type === "html");
	if (!htmlItem || !message_id) return false;
	try {
		const r = await getCanvas(message_id, htmlItem.name, 0);
		const content = r && (r.content || r.data_url);
		if (!content) {
			if (restore) restoreFailed(message_id);
			return false;
		}
		// The await above is a real round trip: re-check that a live frame (or a
		// resolved ?edit= seed, or a promotion that started meanwhile) did not
		// land while this replay was in flight.
		if (restore && (builderHtml.value || editSeed.value || promotionPending.value))
			return false;
		builderHtml.value = content;
		// Remember WHICH message is on the canvas: a later rehydration replays
		// exactly this artifact instead of the newest html in a conversation main
		// chat may also have drawn in.
		canvasMsg.value = message_id;
		return true;
	} catch (e) {
		// A restore is best-effort background work the user did not ask for -
		// a deleted/expired artifact must not raise a toast on page load.
		if (restore) restoreFailed(message_id);
		else toast.error(errMsg(e));
		return false;
	}
}

// A kept identity is a NAME without a detail (resumeAdoption's blip branch), and
// the Save dialog updates in place off the DETAIL — so on its own that name
// keeps nothing: the dialog would title itself "Save dashboard" and insert the
// duplicate the adoption exists to prevent. The moment the user asks to save is
// the one place a repair is worth a round trip, so take it, once: the row comes
// back and Save updates it in place; the row is gone (or no longer this user's)
// and the identity goes with it, because a new row beats a "Save changes" that
// throws on submit; another blip changes nothing and the save proceeds as it
// would have.
const repairing = ref(false);
async function openSave() {
	if (!builderHtml.value || repairing.value) return;
	const name = adoptedRow.value;
	if (name && !editingDetail.value) {
		repairing.value = true;
		try {
			const d = await getDashboard(name);
			// a real round trip: the identity may have been discarded (New chat) or
			// replaced (?edit=) while this was in flight
			if (adoptedRow.value === name) {
				if (d && d.name && d.can_edit) {
					editingDetail.value = d;
					savedName.value = d.name;
					// The repair only ever runs on a fresh mount, where the picker still
					// holds the default — but a user who re-themed this session owns the
					// picker, so seed the row's theme only over the untouched default.
					if (builderTheme.value === DEFAULT_THEME)
						builderTheme.value = themeKey(d.theme);
				} else {
					editingSticky.value = "";
					adoptedRow.value = "";
				}
			}
		} catch (e) {
			if (adoptedRow.value === name && isGoneError(e)) {
				editingSticky.value = "";
				adoptedRow.value = "";
			}
		} finally {
			repairing.value = false;
		}
		// ...and the canvas can be gone with it, in which case there is nothing to
		// save any more — and a discard confirm raised mid-repair owns the screen,
		// so the dialog must not stack on top of it
		if (!builderHtml.value || discardOpen.value) return;
	}
	saveOpen.value = true;
}

// A theme-rejected save hands its violations back to the model: close the dialog
// and post the message into the builder chat, so the agent regenerates on-theme
// without the user relaying CSS jargon (P0-2).
function fixInChat({ text }) {
	saveOpen.value = false;
	if (chatPane.value && chatPane.value.sendText) chatPane.value.sendText(text);
}

function onSaved(detail) {
	savedName.value = (detail && detail.name) || "";
	// keep editing the row we just saved - the next Save is "Save changes", and
	// it stays that way across a navigation (the sticky target below)
	if (detail && detail.name) {
		editingDetail.value = detail;
		editingSticky.value = detail.name;
		// The row now HOLDS the canvas: the two documents an adoption was keeping
		// apart are one again, so the agent is told what it is revising from here on.
		adoptedRow.value = "";
	}
	toast.success("Dashboard saved");
}

// A canvas the user would lose: html on the builder that isn't (still) the
// document of the dashboard we last saved or opened for editing.
const unsavedCanvas = computed(
	() => !!builderHtml.value && builderHtml.value !== ((editingDetail.value || {}).html || "")
);

// The canvas is never thrown away silently. Every path that would drop an
// unsaved dashboard (New dashboard, the chat pane's New chat, opening another
// dashboard for editing) asks first. The chat itself is NOT lost either way -
// it stays in the user's conversations - so say so, and offer to go there.
const discardOpen = ref(false);
let discardYes = null;
let discardNo = null;

const DISCARD_COPY = {
	title: "Discard this unsaved dashboard?",
	message: "Its chat stays in your conversations.",
};
// The ?chat= promotion replaces the whole builder (canvas AND thread), which is
// worth confirming even when the canvas on screen is a saved document.
const PROMOTE_COPY = {
	title: "Discard what's in the builder?",
	message: "Opening this chat's dashboard replaces it. Its chat stays in your conversations.",
};
const discardCopy = ref(DISCARD_COPY);
// "Open its chat" is an escape hatch to the thread the builder is holding — it
// is only an escape when that is somewhere ELSE. A caller that is already
// acting on that same conversation turns it into a loop, so it can drop it.
const discardOfferChat = ref(true);

// `force` is for callers whose own state (an editing target, another chat's
// restored canvas) is worth confirming even when `unsavedCanvas` is false.
function confirmDiscard(
	onYes,
	onNo,
	{ force = false, copy = DISCARD_COPY, offerChat = true } = {}
) {
	if (!force && !unsavedCanvas.value) {
		onYes();
		return;
	}
	discardCopy.value = copy;
	discardOfferChat.value = offerChat;
	discardYes = onYes;
	discardNo = onNo || null;
	discardOpen.value = true;
}

// Both outcomes run exactly once: the callbacks are taken before the dialog
// closes, so the component's own @close can never fire a second settle.
function settleDiscard(yes) {
	const y = discardYes;
	const n = discardNo;
	discardYes = null;
	discardNo = null;
	discardOpen.value = false;
	discardCopy.value = DISCARD_COPY;
	discardOfferChat.value = true;
	if (yes) {
		if (y) y();
	} else if (n) n();
}

const discardActions = computed(() => {
	const actions = [{ label: "Discard", variant: "solid", onClick: () => settleDiscard(true) }];
	if (chatConv.value && discardOfferChat.value) {
		actions.push({
			label: "Open its chat",
			variant: "subtle",
			onClick: () => {
				const id = chatConv.value;
				settleDiscard(false);
				router.push("/c/" + id);
			},
		});
	}
	return actions;
});

function clearBuilder() {
	// The pane stays mounted across tabs now, so clear its thread through the
	// exposed handle rather than relying on a remount to do it.
	if (chatPane.value && chatPane.value.resetChat) chatPane.value.resetChat();
	chatConv.value = "";
	dashDataMode.value = "auto";
	editingSticky.value = "";
	adoptedRow.value = "";
	canvasMsg.value = "";
	editSeed.value = "";
	builderHtml.value = "";
	editingDetail.value = null;
	savedName.value = "";
	detectedSources.value = [];
	builderTheme.value = DEFAULT_THEME;
}

// "New dashboard" (Saved tab header) - fresh chat + empty canvas on Builder.
// One navigation clears the tab hash AND any ?edit seed together (resetBuilder
// + setTab separately would race on route.query).
function newDashboard() {
	confirmDiscard(() => {
		clearBuilder();
		activeTab.value = "builder";
		// Drop the ?edit seed, NOT the whole query: `fv2` is the Saved tab's
		// filter set, and only Clear All unfilters a list.
		router.push({ hash: "", query: _withoutEditSeed() });
	});
}

// Also fired by the chat pane's own "New chat" (emit("reset")).
function resetBuilder() {
	confirmDiscard(() => {
		clearBuilder();
		if (route.query.edit) router.replace({ query: _withoutEditSeed(), hash: route.hash });
	});
}

// ?edit=<name> deep-link: seed the canvas + save dialog from a saved dashboard.
// Also resume the conversation that built it (so the agent has memory of the
// document) and seed the data-mode from its derived type, so an edit session
// never silently drifts onto/converts the wrong dashboard.
//
// It also repoints the single sticky conversation slot, so if an unsaved canvas
// is on the builder it gets the same discard confirm as New dashboard.
//
// `deepLink: false` is the remount path (restoring the sticky editing target):
// silent about a target that has since been deleted, and it never blanks the
// thread in progress just because the stored document names no conversation.
async function loadEdit(name, { deepLink = true } = {}) {
	// The seed is settled only when the confirm is - clearing it up front would
	// let a transcript restore land on the canvas while the dialog is still up.
	const settle = () => {
		if (editSeed.value === name) editSeed.value = "";
	};
	try {
		const d = await getDashboard(name);
		if (!d || !d.name) {
			settle();
			return;
		}
		confirmDiscard(() => {
			builderHtml.value = d.html || "";
			editingDetail.value = d;
			editingSticky.value = d.name;
			savedName.value = d.name;
			builderTheme.value = themeKey(d.theme);
			// the canvas is this document now, not an artifact from the transcript
			canvasMsg.value = "";
			// ...so whatever the builder was holding is no longer ahead of its row:
			// the agent revises this document by name again.
			adoptedRow.value = "";
			// resume the build thread, or a fresh one — never the stale sticky
			// conversation left over from editing a different dashboard.
			if (deepLink || d.source_conversation) {
				chatConv.value = d.source_conversation || "";
				dashDataMode.value = d.dashboard_type === "Connected" ? "live" : "static";
			}
			settle();
		}, settle);
	} catch (e) {
		// A restored target that has since been deleted is not the user's doing -
		// forget it silently instead of toasting on every page load.
		if (deepLink) toast.error(errMsg(e));
		else editingSticky.value = "";
		settle();
	}
}

// ?edit= also changes WITHOUT a remount — "Edit in builder" on a second
// dashboard while this page is already open. A one-shot read at setup made that
// silently repoint the builder AND made loadEdit's discard confirm unreachable
// (a fresh mount always has an empty canvas). Watch it, and the confirm is real.
watch(
	() => route.query.edit,
	(v) => {
		if (route.name !== "DashboardsPage") return;
		const name = typeof v === "string" ? v : "";
		if (!name || name === (editingDetail.value || {}).name) return;
		editSeed.value = name;
		loadEdit(name);
	}
);

// Permanent enough to forget a stored target over: the row was deleted, or this
// user may no longer touch it. Anything else is a blip the next mount retries.
function isGoneError(e) {
	return (
		!!(e && (e.status === 404 || e.exc_type === "DoesNotExistError")) || isPermissionError(e)
	);
}

// The third mount path: coming back to an ADOPTED promotion (resumesAdoption
// above). What was on the canvas is the transcript's build, NEWER than the row
// it adopted, so loadEdit is the wrong restore - it would answer with the row's
// stored html and drop the build the user was looking at. Restore the identity
// alone (badge, theme, Save-in-place) and let the pane's transcript restore
// replay the canvas, which it can because `editSeed` was left empty at setup.
async function resumeAdoption(name) {
	let d = null;
	try {
		d = await getDashboard(name);
	} catch (e) {
		// This mount's own await is a real window - the caps probe alone can sleep a
		// second before this even starts - and "New chat" is one click away in the
		// pane throughout it. A builder cleared meanwhile DISCARDED this identity,
		// and a discarded identity must stay discarded: re-attaching it here arms
		// the next Save over a row that has nothing to do with what is now on the
		// canvas.
		if (adoptedRow.value !== name) return;
		// A blip keeps the NAME for the next mount to retry. The name alone is not
		// yet a Save target - the dialog updates in place off `editingDetail`, which
		// this mount has none of - so what actually keeps the promise is the one
		// repair attempt openSave() makes when it finds a name without a detail.
		if (isGoneError(e)) {
			editingSticky.value = "";
			adoptedRow.value = "";
		}
		return;
	}
	if (adoptedRow.value !== name) return;
	if (d && d.name && d.can_edit) {
		editingDetail.value = d;
		editingSticky.value = d.name;
		savedName.value = d.name;
		builderTheme.value = themeKey(d.theme);
		adoptedRow.value = d.name;
		return;
	}
	// Gone, or readable but no longer editable: the canvas is still the user's
	// build, so keep it and let Save write a row of its own rather than offer a
	// "Save changes" that throws on submit.
	editingSticky.value = "";
	adoptedRow.value = "";
}

// ── ?chat=&canvas= — promoting a chat artifact onto the builder ──────────────
// Main chat can open a builder conversation like any other, but its canvas only
// PREVIEWS the document; nothing there runs the query tools. "Open in
// Dashboards" sends the pair here, and the ordinary restore path takes over:
// the same artifact, rendered by DashboardCanvas, which does run it.
//
// The DOCUMENT that arrives is never a saved one (main chat routes those to
// ?edit=): it is a build made after the last save, or before any. Its IDENTITY
// can be, though - `&dash=` names the row the conversation already has, and the
// promotion adopts it so a live tweak saves back onto that dashboard instead of
// forking a second one.

// Drop the promotion keys once it has settled, so a reload/back does not replay
// it — the editSeed discipline, one route write.
// The ?edit seed alone. Everything else in the query belongs to somebody —
// `fv2` to the Saved list, the promotion keys to stripPromotionQuery.
function _withoutEditSeed() {
	const q = { ...route.query };
	delete q.edit;
	return q;
}

function stripPromotionQuery(hash = route.hash) {
	if (route.name !== "DashboardsPage") return;
	if (route.query.chat === undefined && route.query.canvas === undefined) return;
	const q = { ...route.query };
	delete q.chat;
	delete q.canvas;
	delete q.dash;
	router.replace({ query: q, hash });
}

// The dashboard this builder is editing, by NAME - the three slots in the order
// they settle (the sticky target, the loaded detail, a seed whose loadEdit is
// still in flight). Only the adoption matrix needs the name; `editing` below
// stays the wider "is there any editing state at all" flag.
const editingName = () =>
	editingSticky.value || (editingDetail.value || {}).name || editSeed.value || "";

// What the promotion would overwrite. Wider than `unsavedCanvas` on purpose: it
// also takes the thread and the editing identity, so a saved dashboard open for
// editing, or another conversation's restored canvas, is worth asking about -
// but re-opening the builder's OWN thread costs nothing and must not ask. An
// adoption (`dash`) keeps the identity it names, so there is nothing to own
// there either. The rules live in lib/dashboardOpen.js so they are testable
// outside the SFC.
function promotionWouldDiscard(conv, dash = "") {
	return wouldDiscardOnPromotion({
		conv,
		chatConv: chatConv.value,
		canvasMsg: canvasMsg.value,
		unsavedCanvas: unsavedCanvas.value,
		editing: !!(editingSticky.value || editingDetail.value || editSeed.value),
		editingName: editingName(),
		dash,
	});
}

// The (conversation, message) promotion IN FLIGHT: the query watcher re-fires
// on unrelated route writes (a tab hash push carries the query along), and a
// confirm dialog is open across several of them. Cleared on every settled path
// - accepted, declined or failed - so the same link can be asked for again.
let promoting = "";

async function promoteFromChat(conversation, messageId, { fallback = null, dash = "" } = {}) {
	if (!conversation || !messageId) return;
	const key = conversation + "::" + messageId;
	if (key === promoting) return;
	promoting = key;
	// The hold goes up here, BELOW the dedupe, not in the watcher: that watcher
	// wakes on any route write and re-arms with the same pair, and an arm whose
	// call then dedupes away has nothing left to clear it - the pane's transcript
	// restore would stay blocked for the life of the page. Still synchronous,
	// nothing awaits above it, so the pane's restore cannot slip in first.
	promotionPending.value = true;
	const giveUp = (msg) => {
		if (msg) toast.error(msg);
		promotionPending.value = false;
		// The hold above dropped the restore frame the pane emitted while this
		// promotion was in flight. Declining (or failing) must leave the builder
		// as it was, not empty, so ask the pane for that frame again.
		if (chatPane.value && chatPane.value.restoreCanvas) chatPane.value.restoreCanvas();
		stripPromotionQuery();
		promoting = "";
		if (fallback) fallback();
	};
	// Validate against the transcript, not the link: the message must still
	// exist and must still carry an html artifact.
	let frame = null;
	try {
		const d = (await getDashboardConversation(conversation)) || {};
		frame = builderCanvasFrame(d.messages || [], messageId);
	} catch (e) {
		giveUp(errMsg(e));
		return;
	}
	if (!frame) {
		giveUp("That dashboard is no longer in this chat.");
		return;
	}
	const accept = async () => {
		// ADOPT the saved row this build belongs to, when main chat named one. Its
		// detail is fetched exactly as loadEdit does - but its html never reaches
		// the canvas: the promoted frame is the document the user asked for, and
		// the row only supplies the identity, so the badge names it and the next
		// Save updates it in place instead of creating a duplicate. The fetch is
		// permission-gated server-side, so a foreign or deleted name simply fails
		// here; adoptionIdentity() decides what that failure costs.
		const priorName = editingName();
		let detail = null;
		let gone = false;
		if (dash) {
			try {
				detail = await getDashboard(dash);
			} catch (e) {
				// Not every failure is a blip. get_dashboard is read-gated, so a
				// deleted row - or one this user may no longer touch - THROWS rather
				// than answering `can_edit: false`, and that is an answer: the
				// identity naming it is forgotten, exactly as the remount path does
				// with a sticky it can no longer resolve. Only a fetch that never
				// answered at all can reach `keepPrior`.
				gone = isGoneError(e);
				detail = null;
			}
		}
		const identity = adoptionIdentity({ dash, detail, priorName, gone });
		// The builder is this conversation's now - anything else it was editing is
		// over, or Save would write back onto the wrong dashboard.
		editingSticky.value = identity.name;
		// SAVE identity armed, revision identity NOT: the canvas holds a build the
		// row is behind, so the agent keeps working from the transcript until a
		// Save reconciles the two (agentEditingName).
		adoptedRow.value = identity.name;
		// `keepPrior` is a fetch that blipped on the row the builder ALREADY had:
		// the skipped confirm promised to keep that identity, so the sticky name
		// above stands and the detail already in hand is left alone rather than
		// nulled. Every other outcome takes what the fetch answered.
		if (!identity.keepPrior) {
			editingDetail.value = identity.adopted;
			savedName.value = identity.name;
		}
		// Design for, and re-lint against, the theme the ROW actually has - a Save
		// with the picker left on the default rewrites the row's look, and the
		// validator rejects the html outright when the two disagree.
		if (identity.theme) builderTheme.value = identity.theme;
		editSeed.value = "";
		detectedSources.value = [];
		chatConv.value = conversation;
		canvasMsg.value = messageId;
		// The sticky data-mode belongs to the build the builder was on. Carried
		// into a promoted one it declares an intent the user never expressed - a
		// "static" left over from a baked report tells the agent to freeze the
		// numbers of the live dashboard just promoted. Neutral, as clearBuilder.
		dashDataMode.value = "auto";
		activeTab.value = "builder";
		// The pane's own transcript restore is held off by promotionPending, so
		// only this frame can reach the canvas. onCanvas fetches the artifact and
		// renders it through DashboardCanvas - which is what runs the queries as
		// the viewer.
		const rendered = await onCanvas({ message_id: frame.message_id, items: frame.items });
		// The artifact was in the transcript a moment ago, so a failure here means
		// its File is gone. Leaving the previous document on the canvas would arm
		// Save over html that has nothing to do with the thread now underneath it -
		// and an empty canvas alone reads as "nothing built yet", so say what
		// happened instead of failing silently.
		if (!rendered) {
			builderHtml.value = "";
			toast.error("Couldn't load that dashboard's content — it may have expired.");
		}
		promotionPending.value = false;
		// Settled: release the re-entrancy key, exactly as giveUp does. It guards
		// a promotion IN FLIGHT, not one that finished - left set, asking for the
		// same pair again (the same card, a second time in this page session)
		// would dedupe into silence.
		promoting = "";
		stripPromotionQuery("");
	};
	if (!promotionWouldDiscard(conversation, dash)) {
		await accept();
		return;
	}
	confirmDiscard(accept, () => giveUp(""), {
		force: true,
		copy: PROMOTE_COPY,
		// The builder is already holding the very conversation being promoted
		// (the same-thread-with-an-editing-identity case), so "Open its chat"
		// would land the user back in the chat they clicked the button in.
		offerChat: chatConv.value !== conversation,
	});
}

// Live, for the same reason ?edit= is: the builder may already be open.
watch(
	() => [route.query.chat, route.query.canvas],
	([c, m]) => {
		if (route.name !== "DashboardsPage") return;
		const conv = typeof c === "string" ? c : "";
		const msg = typeof m === "string" ? m : "";
		if (!conv || !msg) return;
		if (route.query.edit) {
			console.warn("dashboards: ?edit= wins over ?chat=&canvas=; the promotion is ignored");
			return;
		}
		// The hold is armed inside promoteFromChat, below its dedupe check - see
		// there for why it cannot be armed from this watcher. `dash` is read here
		// rather than watched: it only ever arrives written together with the pair
		// above, in the single route push main chat makes.
		const dash = typeof route.query.dash === "string" ? route.query.dash : "";
		promoteFromChat(conv, msg, { dash });
	}
);

// v-show keeps the builder mounted while Saved is up, so its iframe loads and
// runs at 0x0 behind `display:none` — charts initialised against a zero-size
// container draw nothing and do not self-heal. Re-drive the document when the
// canvas is visible again (flush:"post", i.e. after `display` is back).
watch(
	activeTab,
	(v) => {
		if (v !== "builder" || !builderHtml.value) return;
		if (canvasRef.value && canvasRef.value.rebuild) canvasRef.value.rebuild();
	},
	{ flush: "post" }
);

// ── the drag-split (Sidebar's resize machinery, vertical) ────────────────────
const builderEl = ref(null);
const _split = useStorage("jarvis-dash-split", 40);
const clampPct = (n) => Math.min(70, Math.max(20, Math.round(Number(n) || 40)));
const chatPct = computed({
	get: () => clampPct(_split.value),
	set: (v) => (_split.value = clampPct(v)),
});
const resizing = ref(false);
let startY = 0;
let startPct = 40;
let containerH = 1;

function startResize(e) {
	if (e.button !== 0) return;
	resizing.value = true;
	startY = e.clientY;
	startPct = chatPct.value;
	containerH = (builderEl.value && builderEl.value.getBoundingClientRect().height) || 1;
	window.addEventListener("mousemove", onResize);
	window.addEventListener("mouseup", stopResize);
	document.body.style.userSelect = "none";
	document.body.style.cursor = "row-resize";
}
function onResize(e) {
	chatPct.value = startPct + ((startY - e.clientY) / containerH) * 100;
}
function stopResize() {
	if (!resizing.value) return;
	resizing.value = false;
	window.removeEventListener("mousemove", onResize);
	window.removeEventListener("mouseup", stopResize);
	document.body.style.userSelect = "";
	document.body.style.cursor = "";
}
function resetSplit() {
	chatPct.value = 40;
}
onBeforeUnmount(stopResize);

// ── caps probe (403 vs transient, TriggersPage pattern) ──────────────────────
function isPermissionError(e) {
	return !!(e && (e.status === 403 || e.exc_type === "PermissionError"));
}

onMounted(async () => {
	let fresh = null;
	try {
		fresh = await getDashboardsCaps();
	} catch (e) {
		if (isPermissionError(e)) {
			accessDenied.value = true;
			return;
		}
		// transient (500/network) - retry once before giving up
		await new Promise((r) => setTimeout(r, 1000));
		try {
			fresh = await getDashboardsCaps();
		} catch (e2) {
			if (isPermissionError(e2)) {
				accessDenied.value = true;
				return;
			}
			// still transient - keep the defaults instead of blocking
			console.warn("get_dashboards_caps failed twice; keeping default caps", e2);
		}
	}
	if (fresh) caps.value = { ...caps.value, ...fresh };

	// An explicit ?edit= is the user asking; the sticky target is this page
	// remembering what it was editing, so a failure there stays quiet. A builder
	// that went away mid-ADOPTION remembers something else - a row its canvas is
	// ahead of - and resumes that instead of re-opening the row's document.
	const normalMount = () => {
		if (adoptionResume) {
			resumeAdoption(adoptedRow.value);
			return;
		}
		if (editSeed.value) loadEdit(editSeed.value, { deepLink: !!routeEdit });
	};
	// ?edit= wins over ?chat=: it names a saved document, the promotion only a
	// draft. Declining or failing the promotion falls back to what this mount
	// would otherwise have done.
	if (routeEdit && routeChat) {
		console.warn("dashboards: ?edit= wins over ?chat=&canvas=; the promotion is ignored");
	}
	if (!routeEdit && routeChat && routeCanvas) {
		promoteFromChat(routeChat, routeCanvas, { fallback: normalMount, dash: routeDash });
	} else {
		// No promotion will run on this mount (?edit= won, or the pair is half
		// present): release the hold taken at setup, or the pane's restore stays
		// blocked for the life of the page.
		promotionPending.value = false;
		normalMount();
	}
});
</script>
