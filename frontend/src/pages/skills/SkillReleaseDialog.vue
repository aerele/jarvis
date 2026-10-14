<template>
	<!-- Lifts an admin's hold. For a skill shared with a role or everyone the admin may
	     switch it back on in the same step; a private skill is switched on by its
	     owner. Nothing is ever allowed to write without asking again. -->
	<Dialog v-model="show" :options="{ title }">
		<template #body-title>
			<h3 class="jv-skill-dialog-title text-2xl font-semibold leading-7 text-ink-gray-9">
				{{ title }}
			</h3>
		</template>
		<template #body-content>
			<div class="flex flex-col gap-3">
				<p
					v-if="ownerLabel"
					class="jv-skill-release-owner break-words text-p-sm font-medium text-ink-gray-8"
				>
					Owned by {{ ownerLabel }}
				</p>
				<p class="text-p-sm text-ink-gray-7">
					The assistant may use the skill again once it is switched on. Releasing does not
					let it write without asking: that stays off until it is set up again.
				</p>
				<Checkbox
					v-if="shared"
					class="jv-skill-release-restore"
					label="and switch it back on"
					:modelValue="restore"
					:disabled="saving"
					@update:modelValue="(v) => (restore = !!v)"
				/>
				<p v-else class="jv-skill-release-owner-note text-p-sm text-ink-gray-7">
					It stays off until its owner switches it on.
				</p>
				<ErrorMessage v-if="error" :message="error" />
			</div>
		</template>
		<template #actions>
			<div class="flex items-center justify-end gap-2">
				<Button label="Cancel" :disabled="saving" @click="show = false" />
				<Button
					class="jv-skill-release-confirm"
					variant="solid"
					label="Release"
					:loading="saving"
					@click="save"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// admin_release for one skill. The tab re-reads its list when told (`released`, and
// `failed`: a refusal may mean the row changed). A refusal stays in the dialog.
import { computed, ref, watch } from "vue";
import { Button, Checkbox, Dialog, ErrorMessage, toast } from "frappe-ui";
import { adminRelease } from "@/api/skillsAdmin";
import { errMessage, escapeHtml } from "@/lib/errors";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	name: { type: String, required: true },
	skillName: { type: String, default: "" },
	ownerLabel: { type: String, default: "" },
	// Shared with a role or everyone: the admin may switch it back on.
	shared: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "released", "failed"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => {
		if (!v && saving.value) return;
		emit("update:modelValue", v);
	},
});
const restore = ref(false);
const saving = ref(false);
const error = ref("");

// Every opening starts unticked: switching it on is a choice made each time.
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		restore.value = false;
		error.value = "";
	},
	{ immediate: true }
);

const label = computed(() => props.skillName || props.name);
const title = computed(() => `Release the hold: ${label.value}`);

async function save() {
	if (saving.value) return;
	saving.value = true;
	error.value = "";
	try {
		const res =
			(await adminRelease(props.name, props.shared && restore.value)) || {};
		toast.success(
			`Released the hold on “${escapeHtml(label.value)}”` +
				(res.restored ? " and switched it back on" : "")
		);
		emit("released", { name: props.name, needs_apply: !!res.needs_apply });
		saving.value = false;
		show.value = false;
	} catch (e) {
		error.value = errMessage(e, "Could not release the hold.");
		emit("failed", { name: props.name });
	} finally {
		saving.value = false;
	}
}
</script>
