<template>
	<SettingsPane
		class="shared-chats-pane"
		:title="__('Shared chats')"
		:description="__('Manage read-only chat sharing across this site.')"
	>
		<div class="rounded-lg border border-outline-gray-2 p-4">
			<label class="flex cursor-pointer items-center justify-between gap-4">
				<span class="min-w-0">
					<span class="block text-base font-medium">{{
						__("Allow sharing chats")
					}}</span>
					<span class="mt-1 block text-sm text-ink-gray-6">{{
						__("Turning this off hides shares. Turning it back on restores access.")
					}}</span>
				</span>
				<span class="flex min-h-11 shrink-0 items-center">
					<input
						type="checkbox"
						role="switch"
						class="peer sr-only"
						:aria-label="__('Allow sharing chats')"
						:checked="enabled"
						:disabled="settingBusy || !settingLoaded || !!revoking"
						@change="toggle($event.target)"
					/>
					<span
						aria-hidden="true"
						class="inline-flex h-6 w-10 items-center rounded-full p-1 transition-colors peer-focus-visible:ring-2 peer-focus-visible:ring-outline-gray-4 peer-focus-visible:ring-offset-2 peer-disabled:opacity-50"
						:class="enabled ? 'bg-surface-gray-7' : 'bg-surface-gray-4'"
					>
						<span
							class="size-4 rounded-full bg-surface-white shadow transition-transform"
							:class="enabled ? 'translate-x-4' : ''"
						/>
					</span>
				</span>
			</label>
			<p class="mt-3 text-xs font-medium text-ink-gray-6" role="status">
				{{
					settingBusy
						? __("Saving sharing setting…")
						: !settingLoaded
						? settingLoading
							? __("Checking sharing setting…")
							: __("Sharing setting unavailable")
						: enabled
						? __("Sharing is on")
						: __("Sharing is off — existing shares are hidden")
				}}
			</p>
			<div v-if="settingError" role="alert" class="mt-3 text-sm">
				<p class="text-ink-red-5">{{ settingError }}</p>
				<button
					type="button"
					class="mt-2 min-h-9 rounded border border-outline-gray-3 px-3 font-medium"
					:disabled="settingBusy || settingLoading"
					@click="retrySetting"
				>
					{{ __("Retry setting") }}
				</button>
			</div>
		</div>
		<p v-if="actionError" role="alert" class="mt-3 text-sm text-ink-red-5">
			{{ actionError }}
		</p>
		<p v-if="notice" role="status" class="mt-3 text-sm text-ink-gray-6">{{ notice }}</p>

		<div class="mt-6 flex flex-wrap items-end gap-2">
			<label class="min-w-0 basis-full text-sm font-medium sm:basis-0 sm:flex-1">
				{{ __("Search people") }}
				<input
					v-model="search"
					type="search"
					:placeholder="__('Owner or recipient name or email')"
					class="mt-2 w-full min-w-0 rounded border border-outline-gray-2 bg-surface-white px-3 py-2 font-normal"
				/>
			</label>
			<button
				v-if="search"
				type="button"
				class="min-h-10 rounded border border-outline-gray-3 px-3 text-sm font-medium"
				@click="search = ''"
			>
				{{ __("Clear search") }}
			</button>
		</div>
		<div class="my-3 flex flex-wrap items-center justify-between gap-2 text-sm">
			<p role="status" class="text-ink-gray-6">
				{{
					loading
						? __("Loading shares…")
						: error
						? __("Share list unavailable")
						: total
						? __("{0}–{1} of {2} shares", [
								start + 1,
								Math.min(start + PAGE, total),
								total,
						  ])
						: __("0 shares")
				}}
			</p>
			<div
				v-if="total > PAGE"
				class="flex items-center gap-2"
				:aria-label="__('Share pages')"
			>
				<button
					type="button"
					class="min-h-9 rounded border border-outline-gray-3 px-3 disabled:opacity-40"
					:disabled="loading || !!revoking || start === 0"
					@click="page(-1)"
				>
					{{ __("Previous") }}
				</button>
				<button
					type="button"
					class="min-h-9 rounded border border-outline-gray-3 px-3 disabled:opacity-40"
					:disabled="loading || !!revoking || start + PAGE >= total"
					@click="page(1)"
				>
					{{ __("Next") }}
				</button>
			</div>
		</div>
		<div v-if="error" role="alert" class="my-3 text-sm">
			<p class="text-ink-red-5">{{ error }}</p>
			<button
				type="button"
				class="mt-2 min-h-9 rounded border border-outline-gray-3 px-3 font-medium"
				:disabled="loading"
				@click="load"
			>
				{{ __("Retry loading shares") }}
			</button>
		</div>
		<div
			v-if="!loading && !error && !rows.length"
			class="rounded-lg border border-outline-gray-2 p-5 text-sm text-ink-gray-6"
		>
			<p class="font-medium text-ink-gray-8">
				{{
					search
						? __("No shared chats match your search.")
						: __("No chats have been shared yet.")
				}}
			</p>
			<p class="mt-2">
				{{
					search
						? __(
								"Search by the owner or recipient’s name or email, or clear the search."
						  )
						: __(
								"When a chat owner shares with someone, you can review and revoke that access here."
						  )
				}}
			</p>
		</div>
		<div
			v-if="rows.length"
			role="table"
			:aria-label="__('Shared chats')"
			:aria-busy="loading"
			class="min-w-0"
		>
			<div
				role="row"
				class="share-grid hidden gap-3 border-b border-outline-gray-2 pb-2 text-xs text-ink-gray-5 lg:grid"
			>
				<span role="columnheader">{{ __("Chat / owner") }}</span
				><span role="columnheader">{{ __("Shared with") }}</span
				><span role="columnheader">{{ __("Shared on") }}</span
				><span role="columnheader" class="text-right">{{ __("Access") }}</span>
			</div>
			<div
				v-for="row in rows"
				:key="row.name"
				role="row"
				class="share-grid grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-2 border-b border-outline-gray-2 py-3 text-sm"
			>
				<div role="cell" class="min-w-0 break-words">
					<p class="font-medium">{{ row.conversation_title }}</p>
					<p class="mt-1 text-xs text-ink-gray-6">
						{{ __("Owner:") }} {{ row.owner_name }}
					</p>
					<span
						v-if="row.hidden"
						class="mt-1 inline-block rounded bg-surface-gray-2 px-2 py-0.5 text-xs text-ink-gray-6"
						>{{ __("Hidden") }}</span
					>
				</div>
				<div role="cell" class="col-start-1 min-w-0 break-words lg:col-auto">
					<span class="mr-1 text-xs text-ink-gray-5 lg:hidden">{{
						__("Shared with:")
					}}</span
					>{{ row.recipient_name }}
				</div>
				<div role="cell" class="col-start-1 text-xs text-ink-gray-5 lg:col-auto">
					<time
						:datetime="String(row.shared_on || '').replace(' ', 'T')"
						:title="row.shared_on"
						>{{ String(row.shared_on || "").slice(0, 10) }}</time
					>
				</div>
				<div
					role="cell"
					class="col-start-2 row-start-1 justify-self-end lg:col-auto lg:row-auto"
				>
					<button
						type="button"
						class="min-h-9 rounded border border-outline-gray-3 px-3 font-medium text-ink-red-5 hover:bg-surface-gray-2 disabled:opacity-40"
						:aria-label="
							__('Revoke access for {0} to {1}', [
								row.recipient_name,
								row.conversation_title,
							])
						"
						:disabled="loading || settingBusy || !!revoking || !!error"
						@click="confirming = row.name"
					>
						{{ __("Revoke") }}
					</button>
				</div>
				<div
					v-if="confirming === row.name"
					class="col-span-full rounded-lg bg-surface-gray-2 p-3"
					role="group"
					:aria-label="__('Revoke access?')"
				>
					<p class="font-medium">{{ __("Revoke access?") }}</p>
					<p class="mt-1 break-words text-sm text-ink-gray-6">
						{{
							__("{0} will lose access to “{1}”.", [
								row.recipient_name,
								row.conversation_title,
							])
						}}
					</p>
					<div class="mt-3 flex flex-wrap gap-2">
						<button
							type="button"
							class="min-h-10 rounded border border-outline-gray-3 bg-surface-white px-3 font-medium text-ink-red-5"
							:disabled="loading || settingBusy || !!revoking || !!error"
							@click="revoke(row.name)"
						>
							{{ revoking === row.name ? __("Revoking…") : __("Confirm revoke") }}
						</button>
						<button
							type="button"
							class="min-h-10 rounded border border-outline-gray-3 px-3"
							:disabled="!!revoking"
							@click="confirming = null"
						>
							{{ __("Cancel") }}
						</button>
					</div>
				</div>
			</div>
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
const settingLoaded = ref(false),
	settingLoading = ref(false),
	settingError = ref("");
const enabled = ref(false),
	loading = ref(true),
	settingBusy = ref(false),
	rows = ref([]),
	total = ref(0),
	start = ref(0),
	search = ref(""),
	error = ref(""),
	actionError = ref(""),
	notice = ref(""),
	confirming = ref(null),
	revoking = ref(null);
let generation = 0,
	settingGeneration = 0,
	disposed = false,
	timer;
async function load() {
	clearTimeout(timer);
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
			if (!rows.value.some((row) => row.name === confirming.value)) confirming.value = null;
			total.value = data.total || 0;
		}
	} catch {
		if (gen === generation && !disposed)
			error.value = __("Couldn't load shared chats. Try again.");
	} finally {
		if (gen === generation && !disposed) loading.value = false;
	}
}
async function loadSetting() {
	const gen = ++settingGeneration;
	settingLoading.value = true;
	settingLoaded.value = false;
	try {
		const value = await getSharingSetting();
		if (disposed || gen !== settingGeneration) return false;
		enabled.value = !!value.enabled;
		settingLoaded.value = true;
		settingError.value = "";
		return true;
	} catch {
		if (!disposed && gen === settingGeneration)
			settingError.value = __(
				"Couldn't read the sharing setting. Retry to check its current value."
			);
		return false;
	} finally {
		if (!disposed && gen === settingGeneration) settingLoading.value = false;
	}
}
async function retrySetting() {
	if (await loadSetting()) {
		actionError.value = "";
		load();
	}
}
async function toggle(input) {
	const before = enabled.value;
	input.checked = before;
	if (settingBusy.value || !settingLoaded.value || revoking.value) return;
	settingBusy.value = true;
	actionError.value = notice.value = "";
	confirming.value = null;
	try {
		const result = await setSharingSetting(!before);
		if (!disposed) {
			enabled.value = !!result.enabled;
			notice.value = __("Sharing setting saved.");
			load();
		}
	} catch {
		if (!disposed) {
			const known = await loadSetting();
			if (!disposed) {
				actionError.value = known
					? __(
							"Couldn't update sharing reliably. The current setting has been reloaded."
					  )
					: __(
							"Couldn't update sharing reliably. Retry reading the setting before making another change."
					  );
				if (known) load();
			}
		}
	} finally {
		settingBusy.value = false;
		input.checked = enabled.value;
	}
}
async function revoke(name) {
	if (revoking.value || loading.value || settingBusy.value || error.value) return;
	revoking.value = name;
	actionError.value = notice.value = "";
	try {
		await revokeShare(name);
		if (!disposed) {
			confirming.value = null;
			notice.value = __("Access revoked.");
			await load();
		}
	} catch {
		if (!disposed) {
			actionError.value = __(
				"Couldn't confirm revocation. Check the share list before trying again."
			);
			await load();
		}
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
	loading.value = true;
	confirming.value = null;
	start.value = 0;
	timer = setTimeout(load, 200);
});
loadSetting();
load();
onBeforeUnmount(() => {
	disposed = true;
	++generation;
	++settingGeneration;
	clearTimeout(timer);
});
</script>
<style scoped>
@media (max-width: 639px) {
	.shared-chats-pane {
		padding: 24px 16px;
	}
}
@media (min-width: 1024px) {
	.share-grid {
		grid-template-columns: minmax(0, 2fr) minmax(0, 1.4fr) 90px 84px;
	}
}
</style>
