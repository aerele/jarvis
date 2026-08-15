<template>
	<Dialog v-model="open" :options="{ size: 'xl', position: 'top' }" @after-leave="reset">
		<template #body>
			<div>
				<div class="relative">
					<div class="absolute inset-y-0 left-0 flex items-center pl-4.5">
						<FeatherIcon name="search" class="h-4 w-4 text-ink-gray-5" />
					</div>
					<input
						ref="inputEl"
						v-model="searchQuery"
						type="text"
						placeholder="Search"
						autocomplete="off"
						spellcheck="false"
						class="w-full border-none bg-transparent py-3 pl-11.5 pr-4.5 text-base text-ink-gray-8 placeholder-ink-gray-4 focus:ring-0"
						@keydown="onKeydown"
					/>
				</div>
				<div ref="listEl" class="max-h-96 overflow-auto border-t">
					<div v-for="group in groups" :key="group.title" class="mb-2 mt-4.5 first:mt-3">
						<div class="mb-2.5 px-4.5 text-base text-ink-gray-5">
							{{ group.title }}
						</div>
						<div
							v-for="item in group.items"
							:key="item.name"
							class="px-2.5"
							:data-cp-active="item === activeItem || undefined"
							@mousedown.prevent
							@mousemove="!item.disabled && setActive(item)"
							@click="select(item)"
						>
							<PaletteItem :item="item" :active="item === activeItem" />
						</div>
					</div>
					<div v-if="empty" class="px-4.5 pb-4 pt-3 text-base text-ink-gray-4">
						No results
					</div>
				</div>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// ⌘K palette (DESIGN-V3 §3.5), rebuilt on plain frappe-ui Dialog. The stock
// CommandPalette in 0.1.278 nests a bare <template> element at its root, which
// Vue renders as an inert native <template> - its whole Dialog subtree never
// mounts (no [role=dialog], no errors), so ⌘K and the sidebar Search rows did
// nothing. Same anatomy as the stock component: search input + grouped list,
// ↑/↓/Enter keyboard navigation, Esc closes (Dialog's own handling). Opens via
// store.paletteOpen (sidebar "Search Chat" + tail row) and the Ctrl/⌘+K
// binding in AppShell's useShortcuts (replaces the stock component's
// self-owned key, §14 DA-06). Empty query = nav items + 10 recent chats;
// typing = filtered nav + server title search (D40, 300ms debounce).
import { ref, computed, watch, nextTick } from "vue";
import { useRouter } from "vue-router";
import { Dialog, FeatherIcon } from "frappe-ui";
import { useShellStore } from "@/stores/shell";
import * as apiShell from "@/api/shell";
import PaletteItem from "./PaletteItem.vue";

const store = useShellStore();
const router = useRouter();

const open = computed({
	get: () => store.paletteOpen,
	set: (v) => (store.paletteOpen = v),
});

const inputEl = ref(null);
const listEl = ref(null);
const searchQuery = ref("");
const results = ref([]);
const frappeGroups = ref([]);
const searching = ref(false);
const activeIndex = ref(0);

// Focus the input once the Dialog's portal content is in the DOM (reka-ui's
// autofocus usually lands on it anyway - this covers re-opens reliably).
watch(open, (v) => {
	if (v) nextTick(() => inputEl.value?.focus());
});

let _debounce = null;
let _seq = 0;
watch(searchQuery, (q) => {
	clearTimeout(_debounce);
	const query = (q || "").trim();
	if (!query) {
		searching.value = false;
		results.value = [];
		frappeGroups.value = [];
		return;
	}
	searching.value = true;
	_debounce = setTimeout(async () => {
		const seq = ++_seq;
		try {
			// Conversation titles + the Frappe desk (records/reports) in parallel.
			const [conv, ws] = await Promise.all([
				apiShell.searchConversations({ search: query, page_length: 20 }),
				apiShell.searchWorkspace({ search: query, limit: 6 }),
			]);
			if (seq !== _seq) return; // stale response
			results.value = (conv && conv.rows) || [];
			frappeGroups.value = (ws && ws.groups) || [];
		} catch (e) {
			if (seq === _seq) {
				results.value = [];
				frappeGroups.value = [];
			}
		} finally {
			if (seq === _seq) searching.value = false;
		}
	}, 300);
});

const navItems = computed(() => [
	{
		name: "nav-new-chat",
		label: "New Chat",
		icon: "plus",
		action: () => store.requestNewChat(router),
	},
	{
		name: "nav-chat",
		label: "Chat",
		icon: "message-circle",
		action: () => router.push({ name: "Chat" }),
	},
	{
		name: "nav-skills",
		label: "Skills",
		icon: "zap",
		action: () => router.push({ name: "SkillsList" }),
	},
	{
		name: "nav-macros",
		label: "Macros",
		icon: "layers",
		action: () => router.push({ name: "MacrosList" }),
	},
	{
		name: "nav-files",
		label: "File Box",
		icon: "inbox",
		action: () => router.push({ name: "FilesList" }),
	},
	{
		name: "nav-approvals",
		label: "Approval Board",
		icon: "check-square",
		action: () => router.push({ name: "ApprovalsList" }),
	},
	{
		name: "nav-agents",
		label: "Agents",
		icon: "cpu",
		action: () => router.push({ name: "AgentsList" }),
	},
]);

function chatItem(c) {
	return {
		name: c.name,
		label: c.title || "New chat",
		icon: "message-circle",
		last_active_at: c.last_active_at,
	};
}

const groups = computed(() => {
	const q = (searchQuery.value || "").trim().toLowerCase();
	if (!q) {
		const out = [{ title: "Jump to", items: navItems.value }];
		const recents = store.conversations.slice(0, 10).map(chatItem);
		if (recents.length) out.push({ title: "Recent chats", items: recents });
		return out;
	}
	const out = [];
	const nav = navItems.value.filter((it) => it.label.toLowerCase().includes(q));
	if (nav.length) out.push({ title: "Jump to", items: nav });
	const chats = searching.value
		? [{ name: "-loading", label: "Searching…", icon: "loader", disabled: true }]
		: results.value.map(chatItem);
	if (chats.length) out.push({ title: "Chats", items: chats });
	// Frappe desk results (Lists / Records / Reports) after the chats.
	for (const g of frappeGroups.value) {
		if (g.items && g.items.length) out.push({ title: g.title, items: g.items });
	}
	return out;
});

// Keyboard cursor runs over the selectable (non-disabled) rows in list order.
const flatItems = computed(() =>
	groups.value.flatMap((g) => g.items).filter((it) => !it.disabled)
);
const activeItem = computed(() => flatItems.value[activeIndex.value] || null);
const empty = computed(
	() => !!searchQuery.value.trim() && !searching.value && !flatItems.value.length
);

// Any result change (typing, search landing, conversations refresh) resets
// the cursor to the top row.
watch(groups, () => {
	activeIndex.value = 0;
});

function setActive(item) {
	const i = flatItems.value.indexOf(item);
	if (i >= 0) activeIndex.value = i;
}

function move(delta) {
	const n = flatItems.value.length;
	if (!n) return;
	activeIndex.value = (activeIndex.value + delta + n) % n;
	nextTick(() => {
		listEl.value?.querySelector("[data-cp-active]")?.scrollIntoView({ block: "nearest" });
	});
}

function onKeydown(e) {
	if (e.key === "ArrowDown") {
		e.preventDefault();
		move(1);
	} else if (e.key === "ArrowUp") {
		e.preventDefault();
		move(-1);
	} else if (e.key === "Enter") {
		e.preventDefault();
		select(activeItem.value);
	}
}

function select(item) {
	if (!item || item.disabled) return;
	open.value = false;
	if (item.action) item.action();
	// SPA-native results (search_workspace "dashboards" group) navigate in-app.
	else if (item.spa_route) router.push(item.spa_route);
	// Frappe desk records/reports open in a new tab so the chat isn't lost.
	else if (item.route) window.open(item.route, "_blank", "noopener");
	else router.push("/c/" + item.name);
}

// after-leave (Dialog's overlay finished its exit) - wipe for the next open.
function reset() {
	clearTimeout(_debounce);
	_seq++; // invalidate any in-flight search
	searchQuery.value = "";
	results.value = [];
	frappeGroups.value = [];
	searching.value = false;
	activeIndex.value = 0;
}
</script>
