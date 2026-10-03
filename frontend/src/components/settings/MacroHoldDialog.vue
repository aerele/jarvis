<template>
	<!-- Asks why before an admin puts another user's macro on hold: the owner reads
	     the reason on their macro form. A second dialog over Settings, as
	     TokenLimitDialog is, so Escape closes this one alone. -->
	<Dialog v-model="show" :options="{ title }">
		<template #body-content>
			<div class="flex flex-col gap-3">
				<p class="text-p-sm text-ink-gray-7">
					The macro stops running until an admin releases it: not by hand, not on its
					schedule. It is also switched off, taken off its schedule and disarmed, and a
					run that is going now is stopped. Its owner sees the reason on the macro.
				</p>
				<FormControl
					type="textarea"
					label="Reason"
					:rows="3"
					placeholder="e.g. It sends emails to customers; paused while we check them."
					:modelValue="reason"
					:disabled="saving"
					:aria-invalid="tooLong ? 'true' : undefined"
					@update:modelValue="(v) => (reason = v == null ? '' : String(v))"
				/>
				<p class="text-xs" :class="tooLong ? 'text-ink-red-4' : 'text-ink-gray-5'">
					{{ reason.trim().length }} / {{ REASON_MAX }}
				</p>
				<ErrorMessage v-if="error" :message="error" />
			</div>
		</template>
		<template #actions>
			<div class="flex items-center justify-end gap-2">
				<Button label="Cancel" :disabled="saving" @click="show = false" />
				<Button
					class="jv-macro-hold-confirm"
					variant="solid"
					theme="red"
					label="Put on hold"
					:loading="saving"
					:disabled="!canSave"
					@click="save"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// One question, the reason, then admin_hold. The pane re-reads its list when told
// (`held`). A refusal stays in the dialog, next to the reason it may be about.
import { computed, ref, watch } from "vue";
import { Button, Dialog, ErrorMessage, FormControl, toast } from "frappe-ui";
import { adminHold } from "@/api/macrosAdmin";
import { errMessage, escapeHtml } from "@/lib/errors";

// The server's limit (macros_admin_api.HOLD_REASON_MAX).
const REASON_MAX = 500;

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// The macro's row name, its own name, and whose it is, for the title.
	name: { type: String, required: true },
	macroName: { type: String, default: "" },
	ownerLabel: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "held"]);

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});
const reason = ref("");
const saving = ref(false);
const error = ref("");
watch(
	() => props.modelValue,
	(open) => {
		if (!open) return;
		reason.value = "";
		error.value = "";
	}
);

const title = computed(
	() =>
		`Put “${props.macroName || props.name}” on hold` +
		(props.ownerLabel ? ` (${props.ownerLabel})` : "")
);
const tooLong = computed(() => reason.value.trim().length > REASON_MAX);
const canSave = computed(() => !saving.value && !!reason.value.trim() && !tooLong.value);

async function save() {
	if (!canSave.value) return;
	saving.value = true;
	error.value = "";
	try {
		const res = (await adminHold(props.name, reason.value.trim())) || {};
		const stopped = Number(res.stopped_runs) || 0;
		// A toast renders HTML: the macro's name goes in escaped.
		toast.success(
			`“${escapeHtml(props.macroName || props.name)}” is on hold` +
				(stopped ? `; ${stopped} run${stopped === 1 ? "" : "s"} stopped` : "")
		);
		emit("held", { name: props.name, stopped_runs: stopped });
		show.value = false;
	} catch (e) {
		error.value = errMessage(e, "Could not put the macro on hold.");
	} finally {
		saving.value = false;
	}
}
</script>
