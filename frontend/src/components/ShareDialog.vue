<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title, size: 'md' }"
		:disable-outside-click-to-close="saving"
		@update:modelValue="close"
	>
		<template #body-content>
			<p class="text-p-sm text-ink-gray-6 break-words">{{ hint }}</p>
			<p v-if="!sharingEnabled" class="mt-3 text-sm text-ink-gray-6">
				{{ __("Sharing is turned off on this site") }}
			</p>
			<p v-if="error" role="alert" class="mt-3 text-sm text-ink-red-5">{{ error }}</p>
			<div v-if="loading" role="status" class="py-8 text-center text-sm text-ink-gray-5">
				{{ __("Loading people…") }}
			</div>
			<template v-else>
				<ul v-if="selected.length" class="mt-3 flex flex-wrap gap-2">
					<li
						v-for="id in selected"
						:key="id"
						class="flex min-w-0 max-w-full items-center gap-2 rounded bg-surface-gray-2 px-2 py-1 text-sm text-ink-gray-8"
					>
						<span class="min-w-0 break-words"
							>{{ label(id)
							}}<span
								v-if="current.get(id)?.hidden"
								class="block text-xs text-ink-gray-5"
								>{{ hiddenShareReason(current.get(id).hidden_reason) }}</span
							></span
						>
						<button
							type="button"
							class="shrink-0 rounded p-1 focus-visible:ring-2"
							:disabled="saving"
							:aria-label="__('Remove {0}', [label(id)])"
							@click="toggle(id)"
						>
							<FeatherIcon name="x" class="size-4" />
						</button>
					</li>
				</ul>
				<label class="mt-4 block text-sm text-ink-gray-6"
					>{{ __("Search people")
					}}<input
						v-model="search"
						type="search"
						class="mt-1 w-full min-w-0 rounded border border-outline-gray-2 bg-surface-white px-3 py-2 text-ink-gray-8"
						:disabled="saving"
						:placeholder="__('Search people…')"
				/></label>
				<p v-if="searching" role="status" class="mt-2 text-sm text-ink-gray-5">
					{{ __("Searching…") }}
				</p>
				<div class="mt-2 max-h-64 overflow-y-auto">
					<button
						v-for="person in candidates"
						:key="person.name"
						type="button"
						class="flex w-full min-w-0 items-center gap-3 rounded px-2 py-2 text-left text-ink-gray-8 hover:bg-surface-gray-2 disabled:opacity-50"
						:aria-pressed="selected.includes(person.name)"
						:disabled="saving || (!sharingEnabled && !selected.includes(person.name))"
						@click="toggle(person.name)"
					>
						<Avatar size="md" :label="person.full_name || person.name" /><span
							class="min-w-0 flex-1"
							><span class="block break-words">{{
								person.full_name || person.name
							}}</span
							><span class="block break-all text-xs text-ink-gray-5">{{
								person.name
							}}</span></span
						><FeatherIcon
							v-if="selected.includes(person.name)"
							name="check"
							class="size-4 shrink-0"
						/>
					</button>
					<p
						v-if="!candidates.length && !searching"
						class="py-5 text-center text-sm text-ink-gray-5"
					>
						{{ __("No people match your search.") }}
					</p>
				</div>
			</template>
		</template>
		<template #actions
			><div class="flex flex-wrap items-center gap-2">
				<Button
					variant="solid"
					:label="__('Save')"
					:loading="saving"
					:disabled="loading || loadFailed"
					@click="save"
				/><Button :label="__('Cancel')" :disabled="saving" @click="close(false)" /><span
					class="ml-auto text-sm text-ink-gray-5"
					>{{ __("{0} selected", [selected.length]) }}</span
				>
			</div></template
		>
	</Dialog>
</template>
<script setup>
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { Dialog, Button, FeatherIcon, Avatar, toast } from "frappe-ui";
import { __ } from "@/lib/i18n";
import { hiddenShareReason } from "@/lib/sharedTranscript";
import { errMessage } from "@/lib/errors";
const props = defineProps({
	modelValue: Boolean,
	title: String,
	hint: String,
	loadCandidates: { type: Function, required: true },
	loadShares: { type: Function, required: true },
	saveShares: { type: Function, required: true },
	enabled: { type: Boolean, default: true },
});
const emit = defineEmits(["update:modelValue", "saved"]);
const loading = ref(false),
	saving = ref(false),
	loadFailed = ref(false),
	search = ref(""),
	searching = ref(false),
	error = ref(""),
	selected = ref([]),
	candidates = ref([]),
	shares = ref([]),
	serverEnabled = ref(true);
const current = computed(() => new Map(shares.value.map((u) => [u.name, u]))),
	sharingEnabled = computed(() => props.enabled && serverEnabled.value);
let generation = 0,
	searchEpoch = 0,
	timer;
const label = (id) =>
	current.value.get(id)?.full_name ||
	candidates.value.find((u) => u.name === id)?.full_name ||
	id;
function close(value) {
	if (!saving.value) emit("update:modelValue", value);
}
function toggle(id) {
	if (saving.value) return;
	if (selected.value.includes(id)) selected.value = selected.value.filter((u) => u !== id);
	else if (sharingEnabled.value) selected.value = [...selected.value, id];
}
async function load() {
	const gen = ++generation;
	++searchEpoch;
	clearTimeout(timer);
	search.value = "";
	searching.value = false;
	serverEnabled.value = true;
	error.value = "";
	selected.value = [];
	shares.value = [];
	candidates.value = [];
	loading.value = true;
	loadFailed.value = false;
	try {
		const [people, result] = await Promise.all([props.loadCandidates(""), props.loadShares()]);
		if (gen !== generation) return;
		candidates.value = people || [];
		shares.value = result?.users || [];
		selected.value = shares.value.map((u) => u.name);
		serverEnabled.value = result?.enabled !== false;
	} catch {
		if (gen === generation) {
			error.value = __("Couldn't load sharing. Close this dialog and try again.");
			loadFailed.value = true;
		}
	} finally {
		if (gen === generation) loading.value = false;
	}
}
watch(
	() => props.modelValue,
	(open) => {
		if (open) load();
		else {
			++generation;
			++searchEpoch;
			clearTimeout(timer);
		}
	},
	{ immediate: true }
);
watch(search, (query) => {
	clearTimeout(timer);
	const epoch = ++searchEpoch,
		gen = generation;
	if (!props.modelValue || loading.value) return;
	searching.value = true;
	timer = setTimeout(async () => {
		try {
			const people = await props.loadCandidates(query);
			if (epoch === searchEpoch && gen === generation) candidates.value = people || [];
		} catch {
			if (epoch === searchEpoch && gen === generation)
				error.value = __("Couldn't search people. Try again.");
		} finally {
			if (epoch === searchEpoch && gen === generation) searching.value = false;
		}
	}, 200);
});
async function save() {
	if (saving.value || loading.value || loadFailed.value) return;
	saving.value = true;
	error.value = "";
	const gen = generation;
	try {
		const result = await props.saveShares([...selected.value]);
		if (gen !== generation) return;
		toast.success(__("Sharing updated"));
		emit("saved", result);
		emit("update:modelValue", false);
	} catch (exception) {
		if (gen === generation) {
			const detail = errMessage(exception, "");
			error.value =
				__("Couldn't update sharing. Your selection is kept; try saving again.") +
				(detail ? " " + detail : "");
		}
	} finally {
		if (gen === generation) saving.value = false;
	}
}
onBeforeUnmount(() => {
	++generation;
	++searchEpoch;
	clearTimeout(timer);
});
</script>
