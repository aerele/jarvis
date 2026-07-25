<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: 'Choose the apps to learn from', size: 'lg' }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body-content>
			<p class="text-sm text-ink-gray-6">
				The Custom App Learning agent reads only the apps you select here, for this run.
			</p>

			<!-- what selecting an app actually means (the consent, spelled out) -->
			<div
				class="mt-3 flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
			>
				<FeatherIcon name="alert-triangle" class="mt-0.5 size-4 shrink-0" />
				<div class="space-y-1">
					<div>
						The <span class="font-medium">source code</span> of every app you select -
						including comments, docstrings and configuration files - is sent to the AI
						model provider configured for this site.
					</div>
					<div>
						What the agent writes lands in the <span class="font-medium">Org wiki</span>,
						visible to everyone in your organisation.
					</div>
					<div>Select only apps whose code you trust and are willing to share.</div>
				</div>
			</div>

			<!-- roster -->
			<div v-if="loading" class="flex items-center gap-2 py-8 text-sm text-ink-gray-5">
				<LoadingIndicator class="size-4" />
				Loading your custom apps…
			</div>
			<div
				v-else-if="!apps.length"
				class="mt-4 rounded-lg border border-outline-gray-2 bg-surface-gray-1 px-3 py-6 text-center text-sm text-ink-gray-6"
			>
				No custom apps are installed on this site. Core apps (Frappe, ERPNext, HRMS,
				India Compliance, Jarvis) are never learnable.
			</div>
			<div v-else class="mt-4 max-h-64 space-y-1 overflow-y-auto">
				<label
					v-for="a in apps"
					:key="a.app"
					class="flex cursor-pointer items-center gap-3 rounded-lg border border-outline-gray-1 px-3 py-2 hover:bg-surface-gray-1"
					:class="!a.path_ok ? 'cursor-not-allowed opacity-50' : ''"
				>
					<Checkbox
						:modelValue="selected.includes(a.app)"
						:disabled="!a.path_ok"
						:label="''"
						@update:modelValue="() => toggle(a.app)"
					/>
					<span class="min-w-0 flex-1">
						<span class="block truncate text-sm font-medium text-ink-gray-8">
							{{ a.title || a.app }}
						</span>
						<span class="block truncate text-xs text-ink-gray-5">
							{{ a.app
							}}<template v-if="a.installed_version"> · v{{ a.installed_version }}</template>
							<template v-if="a.path_ok">
								· ~{{ a.approx_files }} files ({{ a.approx_kb }} KB)
							</template>
							<template v-else> · source directory not readable</template>
						</span>
					</span>
				</label>
			</div>

			<!-- explicit consent, never pre-checked -->
			<div class="mt-4 border-t pt-3">
				<Checkbox
					v-model="consented"
					label="I understand the selected apps' source will be sent to the AI model provider, and I authorise it."
				/>
			</div>
		</template>
		<template #actions>
			<div class="flex justify-end gap-2">
				<Button label="Cancel" @click="emit('update:modelValue', false)" />
				<Button
					variant="solid"
					:label="confirmLabel"
					:loading="busy"
					:disabled="!selected.length || !consented || busy"
					@click="confirm"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// AppSourceConsentDialog - the explicit, per-run app authorization + consent for
// the Custom App Learning agent.
//
// Selecting an app here is what AUTHORISES the run to read it: the server
// validates the selection, stamps it on the run (scope_json.source_apps) and
// refuses every source read / wiki page for an app outside it. "Installed" is
// never consent, so this dialog is the only way a run gets any apps at all - both
// the Analysis-tab card and the agent page open it before calling run_agent_now.
import { ref, watch } from "vue";
import { Button, Checkbox, Dialog, FeatherIcon, LoadingIndicator, toast } from "frappe-ui";
import { listCustomApps } from "@/api/appLearning";
import { errMessage as errMsg } from "@/lib/errors";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	confirmLabel: { type: String, default: "Start learning" },
	busy: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "confirm"]);

const apps = ref([]);
const loading = ref(false);
const selected = ref([]);
const consented = ref(false);

function toggle(app) {
	const i = selected.value.indexOf(app);
	if (i === -1) selected.value.push(app);
	else selected.value.splice(i, 1);
}

async function load() {
	loading.value = true;
	try {
		apps.value = (await listCustomApps()) || [];
	} catch (e) {
		apps.value = [];
		toast.error(errMsg(e));
	} finally {
		loading.value = false;
	}
}

// Re-fetch and RESET the consent on every open: consent is per launch, never
// carried over from a previous one.
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		selected.value = [];
		consented.value = false;
		load();
	}
);

function confirm() {
	if (!selected.value.length || !consented.value) return;
	emit("confirm", [...selected.value]);
}
</script>
