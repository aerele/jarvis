<template>
	<!-- Asks why before an admin puts another user's skill on hold: the owner reads
	     the reason on their skill. A dialog over the Skills page's Admin tab. -->
	<Dialog v-model="show" :options="{ title }">
		<!-- The title wraps anywhere: a long unbroken name would otherwise push the
		     close button out of the dialog on a phone. -->
		<template #body-title>
			<h3 class="jv-skill-dialog-title text-2xl font-semibold leading-7 text-ink-gray-9">
				{{ title }}
			</h3>
		</template>
		<template #body-content>
			<div ref="bodyEl" class="flex flex-col gap-3">
				<p
					v-if="ownerLabel"
					class="jv-skill-hold-owner break-words text-p-sm font-medium text-ink-gray-8"
				>
					Owned by {{ ownerLabel }}
				</p>
				<p class="text-p-sm text-ink-gray-7">
					The assistant stops using this skill until an admin releases it: it is not
					offered in chat, not found by search, not used by File Box and not followed by
					a macro. It is also switched off and stops writing without asking. Its owner
					sees the reason on the skill.
				</p>
				<p v-if="company" class="jv-skill-hold-company text-p-sm text-ink-gray-7">
					This skill is shared with everyone on this site, so it stops for everyone. The
					shared assistant's catalog changes only after you press Apply on the tab.
				</p>
				<p class="jv-skill-hold-note text-p-sm text-ink-gray-7">
					Releasing the hold later does not undo these: the skill stays off, and does not
					write without asking again, until it is switched on and set up again.
				</p>
				<FormControl
					type="textarea"
					label="Reason"
					:rows="3"
					placeholder="e.g. It tells the assistant to email customers; paused while we check it."
					:modelValue="reason"
					:maxlength="REASON_MAX"
					:disabled="saving"
					:aria-invalid="tooLong ? 'true' : undefined"
					@update:modelValue="(v) => (reason = v == null ? '' : String(v))"
				/>
				<p
					class="jv-skill-hold-count text-xs"
					:class="tooLong ? 'text-ink-red-4' : 'text-ink-gray-5'"
					aria-live="polite"
				>
					{{ reason.trim().length }} / {{ REASON_MAX }}
				</p>
				<ErrorMessage v-if="error" :message="error" />
			</div>
		</template>
		<template #actions>
			<div class="flex items-center justify-end gap-2">
				<Button label="Cancel" :disabled="saving" @click="show = false" />
				<Button
					class="jv-skill-hold-confirm"
					variant="solid"
					theme="red"
					:label="company ? 'Hold for everyone' : 'Put on hold'"
					:loading="saving"
					:disabled="!canSave"
					@click="save"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// One question, the reason, then admin_hold. The tab re-reads its list when told
// (`held`, and `failed`: a refusal may mean the row changed). A refusal stays in
// the dialog, next to the reason it may be about. While the hold is in flight the
// dialog stays open: its answer belongs here. A reason typed and then closed on
// (Escape, a click outside) is still there when the same skill's dialog is opened
// again.
import { computed, nextTick, ref, watch } from "vue";
import { Button, Dialog, ErrorMessage, FormControl, toast } from "frappe-ui";
import { adminHold } from "@/api/skillsAdmin";
import { errMessage, escapeHtml } from "@/lib/errors";

// The server's limit.
const REASON_MAX = 500;

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// The skill's row name, its own name (the title), and whose it is (said under it).
	name: { type: String, required: true },
	skillName: { type: String, default: "" },
	ownerLabel: { type: String, default: "" },
	// A skill pushed to everyone: the hold reaches everyone.
	company: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "held", "failed"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => {
		if (!v && saving.value) return; // Escape or a click outside, mid hold
		emit("update:modelValue", v);
	},
});
const reason = ref("");
const saving = ref(false);
const error = ref("");
const bodyEl = ref(null);
// The skill the typed reason is for: another skill's dialog starts empty.
let draftFor = "";
watch(
	() => props.modelValue,
	async (open) => {
		if (!open) return;
		if (draftFor !== props.name) reason.value = "";
		draftFor = props.name;
		error.value = "";
		// The dialog puts focus on its close button: typing would press it. The one
		// thing asked here is the reason, so focus starts there.
		await nextTick();
		requestAnimationFrame(() => {
			const field = bodyEl.value && bodyEl.value.querySelector("textarea");
			if (field) field.focus();
		});
	},
	{ immediate: true }
);

const label = computed(() => props.skillName || props.name);
const title = computed(
	() => `${props.company ? "Hold for everyone" : "Put on hold"}: ${label.value}`
);
const tooLong = computed(() => reason.value.trim().length > REASON_MAX);
const canSave = computed(() => !saving.value && !!reason.value.trim() && !tooLong.value);

async function save() {
	if (!canSave.value) return;
	saving.value = true;
	error.value = "";
	try {
		const res = (await adminHold(props.name, reason.value.trim())) || {};
		const used = Number((res.used_by_macros || {}).total) || 0;
		// A toast renders HTML: the skill's name goes in escaped.
		toast.success(
			`“${escapeHtml(label.value)}” is on hold` +
				(used ? `; ${used} macro${used === 1 ? " uses" : "s use"} it` : "")
		);
		emit("held", { name: props.name, needs_apply: !!res.needs_apply });
		reason.value = "";
		saving.value = false;
		show.value = false;
	} catch (e) {
		error.value = errMessage(e, "Could not put the skill on hold.");
		emit("failed", { name: props.name });
	} finally {
		saving.value = false;
	}
}
</script>

<style>
/* Not scoped: the dialog is drawn outside this component, at the end of the page. */
.jv-skill-dialog-title {
	overflow-wrap: anywhere;
}
</style>
