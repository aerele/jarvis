<template>
	<div
		class="group mx-2 my-[1.5px] flex h-7.5 cursor-pointer items-center rounded px-2 text-sm text-ink-gray-8"
		:class="active ? 'bg-surface-selected shadow-sm' : 'hover:bg-surface-gray-2'"
		@click="open"
	>
		<!-- inline rename: the row body swaps to an input (parity with today) -->
		<input
			v-if="renaming"
			ref="renameEl"
			v-model="renameText"
			class="h-6 w-full rounded border border-outline-gray-2 bg-surface-white px-1 text-sm text-ink-gray-8 focus:border-outline-gray-3 focus:outline-none focus:ring-0"
			@click.stop
			@keydown.enter.stop.prevent="commitRename"
			@keydown.esc.stop.prevent="cancelRename"
			@blur="commitRename"
		/>
		<template v-else>
			<FeatherIcon
				v-if="conv.starred"
				name="star"
				class="mr-1.5 size-3 shrink-0 text-ink-gray-5"
			/>
			<span class="flex-1 truncate">{{ conv.title || "New chat" }}</span>
			<span
				v-if="store.streamingConvId === conv.name"
				class="ml-1.5 size-1.5 shrink-0 animate-pulse rounded-full bg-blue-500"
			/>
			<!-- unread: a finished reply the user hasn't opened yet (global
			     notifier marks, opening clears) — solid + slightly larger, so it
			     reads distinctly from the pulsing streaming dot above -->
			<span
				v-else-if="store.unreadConvs.has(conv.name)"
				class="ml-1.5 size-2 shrink-0 rounded-full bg-blue-500"
				title="New reply"
			/>
			<Dropdown :options="menuOptions" placement="right">
				<template #trigger>
					<Button
						variant="ghost"
						icon="more-horizontal"
						class="!h-5 !w-5 opacity-0 group-hover:opacity-100 focus-visible:!opacity-100"
						:aria-label="'Options for ' + (conv.title || 'chat')"
						@click.stop
					/>
				</template>
			</Dropdown>
		</template>
	</div>
</template>

<script setup>
// Recent-chats row (DESIGN-V3 §3.4): hover ⋯ menu (Star/Rename/Delete),
// inline rename, streaming + unread dots; click navigates to /c/:id.
import { ref, computed, nextTick } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Button, Dropdown, FeatherIcon, confirmDialog } from "frappe-ui";
import { useShellStore } from "@/stores/shell";

const props = defineProps({
	conv: { type: Object, required: true }, // {name, title, starred, last_active_at}
});

const store = useShellStore();
const route = useRoute();
const router = useRouter();

const active = computed(() => store.currentConvId === props.conv.name && !!route.meta.chat);

function open() {
	if (renaming.value) return;
	router.push("/c/" + props.conv.name);
}

// ---- inline rename ----------------------------------------------------------
const renaming = ref(false);
const renameText = ref("");
const renameEl = ref(null);

function startRename() {
	renaming.value = true;
	renameText.value = props.conv.title || "";
	nextTick(() => {
		renameEl.value?.focus();
		renameEl.value?.select();
	});
}
function cancelRename() {
	renaming.value = false;
}
function commitRename() {
	if (!renaming.value) return;
	renaming.value = false; // clear first - Enter also fires blur
	const t = renameText.value.trim();
	if (!t || t === (props.conv.title || "")) return;
	store.renameConversation(props.conv.name, t);
}

// ---- ⋯ menu -----------------------------------------------------------------
const menuOptions = computed(() => [
	{
		label: props.conv.starred ? "Unstar" : "Star",
		icon: "star",
		onClick: () => store.toggleStar(props.conv.name),
	},
	{ label: "Rename", icon: "edit-3", onClick: startRename },
	{ label: "Delete", icon: "trash-2", theme: "red", onClick: confirmDelete },
]);

// ConfirmDialog renders `message` with v-html - escape the user-authored title.
function esc(s) {
	return String(s).replace(
		/[&<>"']/g,
		(c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
	);
}

function confirmDelete() {
	confirmDialog({
		title: "Delete chat?",
		message: `Delete "${esc(props.conv.title || "this chat")}"? This can't be undone.`,
		onConfirm: ({ hideDialog }) => {
			store.archiveConversation(props.conv.name);
			hideDialog();
		},
	});
}
</script>
