<template>
	<!-- Asks why before an admin puts another user's macro on hold: the owner reads
	     the reason on their macro form. A dialog over the Macros page's Admin tab. -->
	<Dialog v-model="show" :options="{ title }">
		<!-- The title wraps anywhere: a long unbroken name would otherwise push the
		     close button out of the dialog on a phone. -->
		<template #body-title>
			<h3 class="jv-macro-dialog-title text-2xl font-semibold leading-7 text-ink-gray-9">
				{{ title }}
			</h3>
		</template>
		<template #body-content>
			<div ref="bodyEl" class="flex flex-col gap-3">
				<!-- Whose it is, under the title and not in it: with a name and an
				     address the title ran to nine lines on a phone. -->
				<p
					v-if="ownerLabel"
					class="jv-macro-hold-owner break-words text-p-sm font-medium text-ink-gray-8"
				>
					Owned by {{ ownerLabel }}
				</p>
				<p class="text-p-sm text-ink-gray-7">
					The macro stops running until an admin releases it: not by hand, not on its
					schedule. It is also switched off, taken off its schedule and disarmed, and a
					run that is going now is stopped. Its owner sees the reason on the macro.
				</p>
				<p class="jv-macro-hold-note text-p-sm text-ink-gray-7">
					Releasing the hold later does not undo these: its owner switches the macro on,
					schedules and arms it again.
				</p>
				<FormControl
					type="textarea"
					label="Reason"
					:rows="3"
					placeholder="e.g. It sends emails to customers; paused while we check them."
					:modelValue="reason"
					:maxlength="REASON_MAX"
					:disabled="saving"
					:aria-invalid="tooLong ? 'true' : undefined"
					@update:modelValue="(v) => (reason = v == null ? '' : String(v))"
				/>
				<p
					class="jv-macro-hold-count text-xs"
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
// (`held`, and `failed`: a hold can be committed and still answer an error, when one
// of the macro's runs could not be stopped). A refusal stays in the dialog, next to
// the reason it may be about. While the hold is in flight the dialog stays open:
// its answer belongs here. A reason typed and then closed on (Escape, a click
// outside) is still there when the same macro's dialog is opened again.
import { computed, nextTick, ref, watch } from "vue";
import { Button, Dialog, ErrorMessage, FormControl, toast } from "frappe-ui";
import { adminHold } from "@/api/macrosAdmin";
import { errMessage, escapeHtml } from "@/lib/errors";

// The server's limit (macros_admin_api.HOLD_REASON_MAX).
const REASON_MAX = 500;

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	// The macro's row name, its own name (the title), and whose it is (said under it).
	name: { type: String, required: true },
	macroName: { type: String, default: "" },
	ownerLabel: { type: String, default: "" },
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
// The macro the typed reason is for: another macro's dialog starts empty.
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

const title = computed(() => `Put “${props.macroName || props.name}” on hold`);
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
		reason.value = "";
		saving.value = false;
		show.value = false;
	} catch (e) {
		error.value = errMessage(e, "Could not put the macro on hold.");
		emit("failed", { name: props.name });
	} finally {
		saving.value = false;
	}
}
</script>

<style>
/* Not scoped: the dialog is drawn outside this component, at the end of the page. */
.jv-macro-dialog-title {
	overflow-wrap: anywhere;
}
</style>
