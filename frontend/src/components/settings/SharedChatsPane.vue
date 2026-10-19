<template>
	<SettingsPane
		:title="__('Shared chats')"
		:description="__('Manage read-only chat sharing across this site.')"
	>
		<label class="flex items-start gap-3 text-sm"
			><input
				type="checkbox"
				:checked="enabled"
				:disabled="loading || settingBusy || !settingLoaded"
				@change="toggle($event.target)"
			/><span
				>{{ __("Allow sharing chats")
				}}<span class="mt-1 block text-xs text-ink-gray-5">{{
					__("Turning this off hides shares. Turning it back on restores access.")
				}}</span></span
			></label
		>
		<p v-if="settingError" role="alert" class="my-3 text-sm text-ink-red-5">
			{{ settingError }}
		</p>
		<p v-if="error" role="alert" class="my-3 text-sm text-ink-red-5">{{ error }}</p>
		<label class="mt-5 block text-sm"
			>{{ __("Search people")
			}}<input
				v-model="search"
				type="search"
				:placeholder="__('Owner or recipient name')"
				class="mt-1 w-full min-w-0 rounded border border-outline-gray-2 bg-surface-white px-3 py-2"
		/></label>
		<p v-if="loading" role="status" class="py-6 text-sm">{{ __("Loading shares…") }}</p>
		<p v-else-if="!rows.length" class="py-6 text-sm text-ink-gray-5">
			{{ __("No shared chats match your search.") }}
		</p>
		<div v-else class="mt-4 min-w-0" role="table" :aria-label="__('Shared chats')">
			<div
				role="row"
				class="hidden grid-cols-[2fr_1fr_1fr_1fr_auto] gap-3 border-b pb-2 text-xs text-ink-gray-5 lg:grid"
			>
				<span role="columnheader">{{ __("Chat") }}</span
				><span role="columnheader">{{ __("Owner") }}</span
				><span role="columnheader">{{ __("Recipient") }}</span
				><span role="columnheader">{{ __("Shared on") }}</span
				><span role="columnheader">{{ __("Access") }}</span>
			</div>
			<div
				v-for="row in rows"
				:key="row.name"
				role="row"
				class="grid min-w-0 gap-2 border-b py-3 text-sm lg:grid-cols-[2fr_1fr_1fr_1fr_auto] lg:gap-3"
			>
				<span role="cell" class="min-w-0 break-words font-medium"
					>{{ row.conversation_title
					}}<span v-if="row.hidden" class="ml-2 text-xs font-normal text-ink-gray-5">{{
						__("Hidden")
					}}</span></span
				>
				<span role="cell" class="min-w-0 break-words"
					><span class="mr-1 text-xs text-ink-gray-5 lg:hidden">{{ __("Owner:") }}</span
					>{{ row.owner_name }}</span
				>
				<span role="cell" class="min-w-0 break-words"
					><span class="mr-1 text-xs text-ink-gray-5 lg:hidden">{{
						__("Recipient:")
					}}</span
					>{{ row.recipient_name }}</span
				>
				<span role="cell" class="text-xs text-ink-gray-5">{{ row.shared_on }}</span>
				<div role="cell">
					<button
						type="button"
						class="rounded px-2 py-1 text-ink-red-5 hover:bg-surface-gray-2"
						:disabled="!!revoking"
						@click="confirming = row.name"
					>
						{{ __("Revoke") }}
					</button>
				</div>
				<div
					v-if="confirming === row.name"
					class="flex flex-wrap items-center gap-3 rounded bg-surface-gray-2 p-3 lg:col-span-5"
					role="group"
					:aria-label="__('Revoke access?')"
				>
					<span>{{ __("Revoke access?") }}</span
					><button
						type="button"
						:disabled="!!revoking"
						class="rounded border border-outline-gray-3 px-2 py-1 text-ink-red-5"
						@click="revoke(row.name)"
					>
						{{ __("Confirm revoke") }}</button
					><button
						type="button"
						:disabled="!!revoking"
						class="underline"
						@click="confirming = null"
					>
						{{ __("Cancel") }}
					</button>
				</div>
			</div>
		</div>
		<div v-if="total > PAGE" class="mt-4 flex items-center justify-between gap-3 text-sm">
			<button
				type="button"
				class="underline"
				:disabled="loading || start === 0"
				@click="page(-1)"
			>
				{{ __("Previous") }}</button
			><span>{{
				__("{0}–{1} of {2}", [start + 1, Math.min(start + PAGE, total), total])
			}}</span
			><button
				type="button"
				class="underline"
				:disabled="loading || start + PAGE >= total"
				@click="page(1)"
			>
				{{ __("Next") }}
			</button>
		</div>
	</SettingsPane>
</template>
<script setup>
import { ref, watch, onBeforeUnmount } from "vue";
import SettingsPane from "./SettingsPane.vue";
import {
	getSharingSetting,
	setSharingSetting,
	listAllShares,
	revokeShare,
} from "@/api/chatSharing";
import { __ } from "@/lib/i18n";
const PAGE = 25;
const settingLoaded = ref(false);
const settingError = ref("");
const enabled = ref(false),
	loading = ref(true),
	settingBusy = ref(false),
	rows = ref([]),
	total = ref(0),
	start = ref(0),
	search = ref(""),
	error = ref(""),
	confirming = ref(null),
	revoking = ref(null);
let generation = 0,
	disposed = false,
	timer;
async function load() {
	const gen = ++generation;
	error.value = "";
	loading.value = true;
	try {
		const data = await listAllShares({
			start: start.value,
			page_length: PAGE,
			search: search.value,
		});
		if (gen === generation && !disposed) {
			if (!(data.rows || []).length && start.value > 0) {
				start.value = Math.max(
					0,
					Math.min(
						start.value - PAGE,
						Math.floor((Math.max(1, data.total || 0) - 1) / PAGE) * PAGE
					)
				);
				await load();
				return;
			}
			rows.value = data.rows || [];
			total.value = data.total || 0;
		}
	} catch {
		if (gen === generation && !disposed)
			error.value = __("Couldn't load shared chats. Search again to retry.");
	} finally {
		if (gen === generation && !disposed) loading.value = false;
	}
}
async function toggle(input) {
	const before = enabled.value;
	input.checked = before;
	if (settingBusy.value) return;
	settingBusy.value = true;
	error.value = "";
	try {
		const result = await setSharingSetting(!before);
		if (!disposed) {
			enabled.value = !!result.enabled;
			await load();
		}
	} catch {
		if (!disposed) error.value = __("Couldn't update sharing. The previous setting is kept.");
	} finally {
		settingBusy.value = false;
		input.checked = enabled.value;
	}
}
async function revoke(name) {
	if (revoking.value) return;
	revoking.value = name;
	error.value = "";
	try {
		await revokeShare(name);
		if (!disposed) {
			confirming.value = null;
			await load();
		}
	} catch {
		if (!disposed) error.value = __("Couldn't revoke access. Try again.");
	} finally {
		revoking.value = null;
	}
}
function page(direction) {
	start.value = Math.max(0, start.value + direction * PAGE);
	confirming.value = null;
	load();
}
watch(search, () => {
	clearTimeout(timer);
	++generation;
	timer = setTimeout(() => {
		start.value = 0;
		confirming.value = null;
		load();
	}, 200);
});
getSharingSetting()
	.then((value) => {
		if (!disposed) {
			enabled.value = !!value.enabled;
			settingLoaded.value = true;
		}
	})
	.catch(() => {
		if (!disposed)
			settingError.value = __(
				"Couldn't read the sharing setting. Reopen Settings to try again."
			);
	});
load();
onBeforeUnmount(() => {
	disposed = true;
	++generation;
	clearTimeout(timer);
});
</script>
