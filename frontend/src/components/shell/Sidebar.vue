<template>
	<div
		class="relative flex h-full flex-col ease-in-out"
		:class="[resizing ? '' : 'transition-all duration-300', collapsed ? 'w-12' : '']"
		:style="widthStyle"
	>
		<!-- 1. brand + user menu -->
		<div class="p-2">
			<UserMenu :is-collapsed="collapsed" />
		</div>

		<!-- 2. action links -->
		<nav class="flex flex-col">
			<SidebarLink
				label="New Chat"
				icon="plus"
				class="mx-2 my-[1.5px]"
				:is-collapsed="collapsed"
				:on-click="() => store.requestNewChat(router)"
			>
				<template v-if="!collapsed" #right>
					<KeyboardShortcut combo="Ctrl+Shift+O" />
				</template>
			</SidebarLink>
			<SidebarLink
				label="Search Chat"
				icon="search"
				class="mx-2 my-[1.5px]"
				:is-collapsed="collapsed"
				:on-click="() => (store.paletteOpen = true)"
			>
				<template v-if="!collapsed" #right>
					<KeyboardShortcut combo="Mod+K" />
				</template>
			</SidebarLink>
		</nav>

		<!-- 3. nav links -->
		<nav
			class="flex flex-col rounded-lg transition-colors"
			:class="editing ? 'bg-surface-gray-1 ring-1 ring-outline-gray-2' : ''"
		>
			<div
				v-for="(link, index) in navLinks"
				:key="link.label"
				class="relative flex flex-col"
				:draggable="editing"
				@dragstart="onDragStart('top', index, $event)"
				@dragover.prevent
				@drop.prevent="onDrop('top', index)"
				@dragend="onDragEnd"
				:class="[
					editing ? 'cursor-grab' : '',
					dragging && dragging.group === 'top' && dragging.index === index
						? 'opacity-40'
						: '',
				]"
			>
				<span
					v-if="editing"
					class="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 text-ink-gray-4"
					><FeatherIcon name="more-vertical" class="size-4"
				/></span>
				<SidebarLink
					:label="link.label"
					:icon="link.icon"
					:to="link.to"
					:is-active="link.isActive()"
					class="mx-2 my-[1.5px]"
					:is-collapsed="collapsed"
				>
					<template v-if="link.badge && !collapsed && store.approvalsCount" #right>
						<Badge
							:label="store.approvalsCount > 9 ? '9+' : String(store.approvalsCount)"
							theme="red"
							variant="subtle"
						/>
					</template>
				</SidebarLink>
				<!-- collapsed badge → floating dot (HD pattern; red = pending action;
				     semantic token so the dot tracks data-theme: #CC2929 light / #E43838 dark) -->
				<div
					v-if="link.badge && collapsed && store.approvalsCount"
					class="absolute size-1.5 translate-x-6 translate-y-1 rounded-full bg-surface-red-5"
				/>
			</div>
			<!-- trailing drop zone: the only way to drop an item into the LAST slot
			     (items insert BEFORE themselves) or into an emptied group. Only shown
			     mid-drag so edit mode has no empty gaps otherwise. -->
			<div
				v-if="editing && dragging"
				class="mx-2 my-[1.5px] h-7 rounded-md border border-dashed border-outline-gray-3 bg-surface-gray-1"
				@dragover.prevent
				@drop.prevent="onDrop('top', navLinks.length)"
			/>
		</nav>

		<!-- 3b. "More" group: a collapsible section of overflow destinations
		     (Macros, Triggers). The "More" row toggles the group inline (moreOpen),
		     it does NOT open a palette. It lights up when the user is on one of its
		     destinations, so a page reached via More still reads as a section. -->
		<nav
			class="flex flex-col rounded-lg transition-colors"
			:class="editing ? 'bg-surface-gray-1 ring-1 ring-outline-gray-2' : ''"
		>
			<SidebarLink
				label="More"
				icon="more-horizontal"
				class="mx-2 my-[1.5px]"
				:is-collapsed="collapsed"
				:is-active="onMoreDestination"
				:on-click="() => (moreOpen = !moreOpen)"
			>
				<template v-if="!collapsed" #right>
					<FeatherIcon
						:name="moreOpen ? 'chevron-down' : 'chevron-right'"
						class="size-3.5 text-ink-gray-4"
					/>
				</template>
			</SidebarLink>
			<template v-if="moreOpen">
				<div
					v-for="(link, index) in moreLinks"
					:key="link.label"
					class="relative flex flex-col"
					:draggable="editing"
					@dragstart="onDragStart('more', index, $event)"
					@dragover.prevent
					@drop.prevent="onDrop('more', index)"
					@dragend="onDragEnd"
					:class="[
						editing ? 'cursor-grab' : '',
						dragging && dragging.group === 'more' && dragging.index === index
							? 'opacity-40'
							: '',
					]"
				>
					<span
						v-if="editing"
						class="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 text-ink-gray-4"
						><FeatherIcon name="more-vertical" class="size-4"
					/></span>
					<SidebarLink
						:label="link.label"
						:icon="link.icon"
						:to="link.to"
						:is-active="link.isActive()"
						class="mx-2 my-[1.5px]"
						:is-collapsed="collapsed"
					/>
				</div>
				<!-- trailing drop zone (see the top group). onDragStart forces
				     moreOpen open, so this is reachable even when More is empty. -->
				<div
					v-if="editing && dragging"
					class="mx-2 my-[1.5px] h-7 rounded-md border border-dashed border-outline-gray-3 bg-surface-gray-1"
					@dragover.prevent
					@drop.prevent="onDrop('more', moreLinks.length)"
				/>
			</template>
		</nav>

		<!-- 4. recent chats (hidden entirely when collapsed, D6) -->
		<template v-if="!collapsed">
			<div class="px-4 pb-2.5 pt-[11px] text-sm text-ink-gray-5">Recent chats</div>
			<div class="min-h-0 flex-1 overflow-y-auto pb-2">
				<template v-if="starred.length">
					<div
						class="px-4 pb-1 text-2xs font-medium uppercase tracking-wide text-ink-gray-4"
					>
						Starred
					</div>
					<ConversationRow v-for="c in starred" :key="c.name" :conv="c" />
				</template>
				<template v-if="recent.length">
					<div
						v-if="starred.length"
						class="px-4 pb-1 pt-2 text-2xs font-medium uppercase tracking-wide text-ink-gray-4"
					>
						Recent
					</div>
					<ConversationRow v-for="c in recent" :key="c.name" :conv="c" />
				</template>
				<div
					v-if="!store.conversations.length && !store.conversationsLoading"
					class="px-4 py-3 text-sm text-ink-gray-4"
				>
					No chats yet
				</div>
				<!-- tail row: beyond the 50-row cap, retrieval moves to the palette -->
				<nav v-if="store.conversations.length > 50" class="flex flex-col">
					<SidebarLink
						label="Search chats…"
						icon="search"
						class="mx-2 my-[1.5px]"
						:is-collapsed="false"
						:on-click="() => (store.paletteOpen = true)"
					/>
				</nav>
			</div>
		</template>
		<div v-else class="flex-1" />

		<!-- 5. footer: collapse toggle (desktop only — a drawer has nothing to
		     collapse to, and it closes via the scrim / nav tap) -->
		<div v-if="!store.mobile" class="m-2 flex items-center gap-1">
			<SidebarLink
				label="Collapse"
				class="min-w-0 flex-1"
				:is-collapsed="collapsed"
				:on-click="toggleCollapse"
			>
				<template #icon>
					<FeatherIcon
						name="chevrons-left"
						class="size-4 text-ink-gray-8 duration-300 ease-in-out"
						:class="{ '[transform:rotateY(180deg)]': collapsed }"
					/>
				</template>
			</SidebarLink>
			<button
				v-if="!collapsed && editing"
				class="flex size-8 shrink-0 items-center justify-center rounded-md text-ink-gray-5 hover:bg-surface-gray-2 hover:text-ink-gray-8"
				title="Reset to default order"
				@click="resetOrder"
			>
				<FeatherIcon name="rotate-ccw" class="size-4" />
			</button>
			<button
				v-if="!collapsed"
				class="flex size-8 shrink-0 items-center justify-center rounded-md hover:bg-surface-gray-2"
				:class="editing ? 'text-ink-blue-link' : 'text-ink-gray-5 hover:text-ink-gray-8'"
				:title="editing ? 'Done' : 'Edit sidebar order'"
				@click="editing = !editing"
			>
				<FeatherIcon :name="editing ? 'check' : 'edit-2'" class="size-4" />
			</button>
		</div>

		<!-- 6. drag-to-resize handle (expanded only): grab the right edge to set
		     the width, double-click to reset. The collapsed rail is a fixed 48px,
		     so the handle is hidden there. -->
		<div
			v-if="!collapsed && !store.mobile"
			class="group absolute inset-y-0 right-0 z-20 flex w-2.5 translate-x-1/2 cursor-col-resize items-center justify-center"
			role="separator"
			aria-orientation="vertical"
			title="Drag to resize · double-click to reset"
			@mousedown.prevent="startResize"
			@dblclick="resetWidth"
		>
			<!-- full-height hairline: appears on hover / while dragging -->
			<span
				class="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors"
				:class="
					resizing ? 'bg-surface-gray-4' : 'bg-transparent group-hover:bg-surface-gray-4'
				"
			/>
			<!-- grip pill: always faintly visible so the edge reads as adjustable,
			     solid on hover / while dragging -->
			<span
				class="relative h-7 w-1 rounded-full bg-surface-gray-4 transition-opacity"
				:class="resizing ? 'opacity-100' : 'opacity-30 group-hover:opacity-100'"
			/>
		</div>
	</div>
</template>

<script setup>
// App-shell sidebar (DESIGN-V3 §3.2): 220px expanded / 48px rail (D5),
// user menu · New Chat/Search · nav links (Approvals badge, D12) · recent
// chats (starred pinned, capped 50, D6/D7) · collapse toggle (persisted via
// the store; ≤820px auto-collapse, D8).
import { computed, ref, onBeforeUnmount } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Badge, FeatherIcon, KeyboardShortcut } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import { getMySettings, setSidebarOrder } from "@/api";
import { reconcileOrder, moveOrderItem } from "@/lib/sidebarOrder";
import UserMenu from "./UserMenu.vue";
import SidebarLink from "./SidebarLink.vue";
import ConversationRow from "./ConversationRow.vue";

const store = useShellStore();
const route = useRoute();
const router = useRouter();

// Inside the phone drawer the rail makes no sense (there is no chat beside it
// to reclaim width for), so force the expanded layout regardless of the
// persisted/auto-collapse preference.
const collapsed = computed(() => (store.mobile ? false : store.sidebarCollapsed));

// Desktop honours the drag-resized width; the drawer takes most of the screen
// but is capped so it never exceeds a small phone's viewport.
const widthStyle = computed(() => {
	if (store.mobile) return { width: "min(84vw, 320px)" };
	return collapsed.value ? undefined : { width: store.sidebarWidth + "px" };
});

// The "More" overflow row lights up on any of its destinations (currently the
// Dashboards page + detail). Extend the prefix list as destinations are added.
const onMoreDestination = computed(
	() => route.path.startsWith("/macros") || route.path.startsWith("/triggers")
);
function toggleCollapse() {
	store.sidebarCollapsed = !store.sidebarCollapsed;
}

// ── drag-to-resize (expanded width, persisted in the store, D5-adjacent) ──────
// The store getter/setter clamps to [SIDEBAR_MIN_W, SIDEBAR_MAX_W], so we can
// feed it raw deltas. `resizing` suppresses the width transition mid-drag so the
// edge tracks the cursor 1:1 instead of lagging behind the 300ms ease.
const resizing = ref(false);
let startX = 0;
let startW = 0;

function startResize(e) {
	if (e.button !== 0 || collapsed.value) return;
	resizing.value = true;
	startX = e.clientX;
	startW = store.sidebarWidth;
	window.addEventListener("mousemove", onResize);
	window.addEventListener("mouseup", stopResize);
	document.body.style.userSelect = "none";
	document.body.style.cursor = "col-resize";
}
function onResize(e) {
	store.sidebarWidth = startW + (e.clientX - startX);
}
function stopResize() {
	if (!resizing.value) return;
	resizing.value = false;
	window.removeEventListener("mousemove", onResize);
	window.removeEventListener("mouseup", stopResize);
	document.body.style.userSelect = "";
	document.body.style.cursor = "";
}
function resetWidth() {
	store.sidebarWidth = 220;
}
onBeforeUnmount(stopResize);

const TOP_DEFS = [
	{
		label: "File Box",
		icon: "inbox",
		to: { name: "FilesList" },
		isActive: () => route.path.startsWith("/files"),
	},
	{
		label: "Approval Board",
		icon: "check-square",
		to: { name: "ApprovalsList" },
		isActive: () => route.path.startsWith("/approvals"),
		badge: true,
	},
	{
		label: "Dashboard",
		icon: "bar-chart-2",
		to: { name: "DashboardsPage" },
		isActive: () => route.path.startsWith("/dashboards"),
	},
	{
		label: "Skills",
		icon: "zap",
		to: { name: "SkillsList" },
		isActive: () => route.path.startsWith("/skills"),
	},
	{
		label: "Agents",
		icon: "cpu",
		to: { name: "AgentsList" },
		isActive: () => route.path.startsWith("/agents"),
	},
];
// "More" group: macros + triggers are created from the main chat.
const MORE_DEFS = [
	{
		label: "Macros",
		icon: "layers",
		to: { name: "MacrosList" },
		isActive: () => route.path.startsWith("/macros"),
	},
	{
		label: "Triggers",
		icon: "git-branch",
		to: { name: "TriggersPage" },
		isActive: () => route.path.startsWith("/triggers"),
	},
];
// Reactive, drag-reorderable order (persisted per user in Jarvis User Settings).
const navLinks = ref([...TOP_DEFS]);
const moreLinks = ref([...MORE_DEFS]);
const moreOpen = ref(false);

// Reconcile the saved {top, more} order against the current defs
// (lib/sidebarOrder keeps the "a stale label can never hide a nav item" rule).
function applySaved(saved) {
	const { top, more } = reconcileOrder(saved, TOP_DEFS, MORE_DEFS);
	navLinks.value = top;
	moreLinks.value = more;
}

// Load the saved order once (best-effort; defaults stand on any failure).
getMySettings()
	.then((r) => {
		const raw = (r && r.data && r.data.sidebar_order) || "";
		if (!raw) return;
		applySaved(JSON.parse(raw));
	})
	.catch(() => {});

let _saveTimer = null;
function persistOrder() {
	clearTimeout(_saveTimer);
	_saveTimer = setTimeout(() => {
		setSidebarOrder({
			top: navLinks.value.map((l) => l.label),
			more: moreLinks.value.map((l) => l.label),
		}).catch(() => {});
	}, 400);
}

// Native drag-to-reorder. The move happens on DROP (not dragover) so an item can
// cross between the top and More groups without the drag source node being
// destroyed mid-drag. Items can move within a group OR between groups.
const editing = ref(false);
const dragging = ref(null);
function onDragStart(group, index, e) {
	dragging.value = { group, index };
	moreOpen.value = true; // expose the More group as a drop target during a drag
	if (e && e.dataTransfer) {
		e.dataTransfer.effectAllowed = "move";
		try {
			e.dataTransfer.setData("text/plain", String(index));
		} catch (_) {
			/* some browsers require setData; ignore if it throws */
		}
	}
}
function onDrop(group, index) {
	const d = dragging.value;
	dragging.value = null;
	if (!d) return;
	const { top, more } = moveOrderItem(
		navLinks.value,
		moreLinks.value,
		d.group,
		d.index,
		group,
		index
	);
	navLinks.value = top;
	moreLinks.value = more;
	persistOrder();
}
function onDragEnd() {
	dragging.value = null;
}
function resetOrder() {
	navLinks.value = [...TOP_DEFS];
	moreLinks.value = [...MORE_DEFS];
	persistOrder();
}

// Starred pinned on top; starred + recent capped at 50 rows total (D6).
const starred = computed(() => store.conversations.filter((c) => c.starred).slice(0, 50));
const recent = computed(() =>
	store.conversations.filter((c) => !c.starred).slice(0, Math.max(0, 50 - starred.value.length))
);
</script>
