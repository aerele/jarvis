<template>
	<div class="border-t bg-surface-white px-5 py-3">
		<div class="mx-auto w-full max-w-7xl">
			<!-- "Answering: <question>" strip - dismiss returns to free capture -->
			<div
				v-if="question"
				class="mb-2 flex items-center gap-2 rounded-md bg-surface-gray-2 px-3 py-1.5"
			>
				<FeatherIcon name="corner-down-right" class="size-3.5 shrink-0 text-ink-gray-5" />
				<span class="min-w-0 flex-1 truncate text-sm text-ink-gray-7">
					Answering: {{ excerpt(question.question) }}
				</span>
				<button
					class="shrink-0 text-ink-gray-5 hover:text-ink-gray-8"
					aria-label="Stop answering this question"
					@click="$emit('clear-question')"
				>
					<FeatherIcon name="x" class="size-3.5" />
				</button>
			</div>

			<!-- attachment + link chips (present ones only) -->
			<div v-if="attachment || link" class="mb-2 flex flex-wrap items-center gap-2">
				<span
					v-if="attachment"
					class="inline-flex items-center gap-1.5 rounded-full bg-surface-gray-2 px-2.5 py-1 text-sm text-ink-gray-7"
				>
					<FeatherIcon name="paperclip" class="size-3.5 shrink-0 text-ink-gray-5" />
					<span class="max-w-[16rem] truncate">{{ attachment.file_name }}</span>
					<button
						class="shrink-0 text-ink-gray-5 hover:text-ink-gray-8"
						aria-label="Remove attachment"
						@click="removeAttachment"
					>
						<FeatherIcon name="x" class="size-3.5" />
					</button>
				</span>
				<span v-if="link" class="inline-flex items-center gap-1">
					<Badge theme="blue" variant="subtle" :label="linkLabel" />
					<button
						class="text-ink-gray-5 hover:text-ink-gray-8"
						aria-label="Remove link"
						@click="removeLink"
					>
						<FeatherIcon name="x" class="size-3.5" />
					</button>
				</span>
			</div>

			<!-- autosizing textarea (native keydown/input bubble up to this div) -->
			<div ref="box" @keydown="onKeydown" @input="autoGrow">
				<FormControl
					type="textarea"
					:rows="3"
					:placeholder="placeholder"
					:modelValue="draft"
					@update:modelValue="(v) => (draft = v)"
				/>
			</div>

			<!-- inline link field (toggled by the link button) -->
			<div v-if="linkOpen" class="mt-2 flex items-center gap-2">
				<FormControl
					ref="linkField"
					type="text"
					class="flex-1"
					placeholder="https://…"
					:modelValue="linkInput"
					@update:modelValue="(v) => (linkInput = v)"
					@keydown.enter.prevent="commitLink"
				>
					<template #prefix>
						<FeatherIcon name="link" class="size-4 text-ink-gray-5" />
					</template>
				</FormControl>
				<Button variant="subtle" label="Add" @click="commitLink" />
				<Button variant="ghost" label="Cancel" @click="cancelLink" />
			</div>

			<!-- toolbar: left = capture controls · right = discard/send -->
			<div class="mt-2 flex flex-wrap items-center justify-between gap-2">
				<div class="flex flex-wrap items-center gap-1.5">
					<!-- voice first + prominent (design-language §7) -->
					<VoiceRecorder v-if="sttEnabled" compact @transcript="onTranscript" />
					<FileUploader
						:upload-args="{ private: 1 }"
						@success="onUpload"
						@failure="onUploadError"
					>
						<template #default="{ openFileSelector, uploading }">
							<Button
								variant="ghost"
								icon="paperclip"
								:loading="uploading"
								:tooltip="'Attach a file'"
								@click="openFileSelector()"
							/>
						</template>
					</FileUploader>
					<Button
						variant="ghost"
						icon="link"
						:tooltip="'Add a link'"
						@click="toggleLink"
					/>
				</div>
				<div class="flex items-center gap-2">
					<Button v-if="dirty" variant="ghost" label="Discard" @click="discard" />
					<Button
						variant="solid"
						label="Send"
						:disabled="empty"
						:loading="saving"
						@click="submit"
					/>
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
// ChatComposer - the bottom-pinned capture box on the Personalise tab
// (design-language §7, modelled on CRM's CommentBox with voice promoted to a
// first-class left-cluster control). Handles all four note forms:
//   text        → the textarea
//   voice        → VoiceRecorder transcript appended to the textarea
//   attachment   → one private File via FileUploader (v1 keeps a single file)
//   link         → one validated http(s) URL chip
// It is stateless about persistence: on submit it emits {text, url, attachment,
// duration_s} and the parent (PersonaliseTab) routes it to answerQuestion (when
// a `question` is selected) or saveNote (free capture), then calls clear().
//
// Draft text is persisted per user+question via useStorage so a half-written
// answer survives navigation and sub-tab switches. The store holds a map keyed
// by question name (or "__free__"), namespaced by the logged-in user.
import { ref, computed, nextTick, watch } from "vue";
import { useStorage } from "@vueuse/core";
import {
	Badge,
	Button,
	FeatherIcon,
	FileUploader,
	FormControl,
	toast,
	confirmDialog,
} from "frappe-ui";
import VoiceRecorder from "@/components/VoiceRecorder.vue";
import { session } from "@/data/session";
import { agentName } from "@/branding";

const props = defineProps({
	// STT availability (pass caps.stt_enabled) - hides the recorder when off.
	sttEnabled: { type: Boolean, default: false },
	// the selected question ({name, question, …}) or null for free capture.
	question: { type: Object, default: null },
	// true while the parent's answer/save call is in flight (Send loading).
	saving: { type: Boolean, default: false },
});

const emit = defineEmits(["submit", "clear-question"]);

// ── draft persistence (per user + per question) ──────────────────────────────
const drafts = useStorage(`jarvis-personalise-drafts-${session.user || "anon"}`, {});
const draftKey = computed(() => props.question?.name || "__free__");
const draft = computed({
	get: () => drafts.value[draftKey.value] || "",
	set: (v) => {
		const next = { ...drafts.value };
		const t = v || "";
		if (t) next[draftKey.value] = t;
		else delete next[draftKey.value];
		drafts.value = next;
	},
});

// ── local capture state ──────────────────────────────────────────────────────
const attachment = ref(null); // frappe File payload {file_url, file_name, …} | null
const link = ref(""); // committed http(s) URL | ""
const linkOpen = ref(false);
const linkInput = ref("");
const linkField = ref(null);
const box = ref(null);
// The recording length VoiceRecorder reported with its transcript (second emit
// arg); non-zero means the draft contains dictation, so the server tags the
// saved note kind "Voice". Reset on clear().
const voiceDurationS = ref(0);

const placeholder = computed(() =>
	props.question
		? "Answer in your own words — type, record, attach, or paste a link…"
		: `Tell ${agentName} anything about how you work — type, record, attach, or paste a link…`
);

const linkLabel = computed(() => {
	const u = String(link.value || "").replace(/^https?:\/\//i, "");
	return u.length > 40 ? u.slice(0, 39) + "…" : u;
});

// question text for the "Answering: …" strip; falls back gracefully when the
// re-answer hand-off only carried the question's name (no text - §onReanswer).
function excerpt(s, n = 80) {
	const t = String(s || "")
		.trim()
		.replace(/\s+/g, " ");
	if (!t) return "your question";
	return t.length > n ? t.slice(0, n - 1) + "…" : t;
}

const empty = computed(() => !draft.value.trim() && !attachment.value && !link.value);
const dirty = computed(() => !empty.value);

// ── autosize + submit shortcut ───────────────────────────────────────────────
function autoGrow() {
	const ta = box.value?.querySelector("textarea");
	if (!ta) return;
	ta.style.height = "auto";
	ta.style.height = Math.min(ta.scrollHeight, 220) + "px";
}

function onKeydown(e) {
	// Ctrl/Cmd+Enter submits (plain Enter keeps a newline in the textarea).
	if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
		e.preventDefault();
		submit();
	}
}

// When the target question changes the TEXT draft swaps correctly via the keyed
// store, but the staged attachment/link are component-global refs - they must
// NOT ride along to a different question (or into free capture when the chip is
// dismissed), else the wrong file/URL gets submitted with the wrong answer.
// Reset the capture staging on every switch, then re-fit the textarea.
watch(draftKey, () => {
	attachment.value = null;
	link.value = "";
	linkOpen.value = false;
	linkInput.value = "";
	nextTick(autoGrow);
});

// ── voice ────────────────────────────────────────────────────────────────────
function onTranscript(text, durationS) {
	// dictation appends to any typed draft (composer-mic precedent, BusinessTab)
	const cur = draft.value;
	draft.value = cur.trim() ? cur.replace(/\s+$/, "") + " " + text : text;
	// VoiceRecorder reports the recording length as the second emit arg; keep
	// the longest take so the saved note is tagged kind Voice server-side.
	voiceDurationS.value = Math.max(voiceDurationS.value, Number(durationS) || 0);
	nextTick(autoGrow);
}

// ── attachment ───────────────────────────────────────────────────────────────
function onUpload(f) {
	// v1 keeps a single attachment - a new pick replaces the old one
	attachment.value = f || null;
}
function onUploadError(e) {
	toast.error((e && e.message) || "Couldn't attach that file.");
}
function removeAttachment() {
	attachment.value = null;
}

// ── link ─────────────────────────────────────────────────────────────────────
function toggleLink() {
	linkOpen.value = !linkOpen.value;
	if (linkOpen.value) nextTick(() => linkField.value?.$el?.querySelector("input")?.focus());
}
function commitLink() {
	const u = linkInput.value.trim();
	if (!u) {
		linkOpen.value = false;
		return;
	}
	if (!/^https?:\/\//i.test(u)) {
		toast.error("Enter a full link starting with http:// or https://");
		return;
	}
	link.value = u;
	linkInput.value = "";
	linkOpen.value = false;
}
function cancelLink() {
	linkInput.value = "";
	linkOpen.value = false;
}
function removeLink() {
	link.value = "";
}

// ── submit / discard ─────────────────────────────────────────────────────────
function submit() {
	if (empty.value || props.saving) return;
	emit("submit", {
		text: draft.value.trim(),
		url: link.value || "",
		attachment: attachment.value?.file_url || "",
		duration_s: voiceDurationS.value || 0,
	});
}

function discard() {
	// A tiny draft clears instantly; anything substantial (possibly minutes of
	// dictation) gets a confirm - the old Business-tab capture card's rule.
	if (draft.value.trim().length <= 15 && !attachment.value && !link.value) {
		clear();
		return;
	}
	confirmDialog({
		title: "Discard this note?",
		message: "Your unsaved note will be cleared. This can't be undone.",
		onConfirm: ({ hideDialog }) => {
			clear();
			hideDialog();
		},
	});
}

// Reset everything - called by the parent after a successful save, and by
// Discard. Clearing the draft removes it from the per-question store.
function clear() {
	draft.value = "";
	attachment.value = null;
	link.value = "";
	linkOpen.value = false;
	linkInput.value = "";
	voiceDurationS.value = 0;
	nextTick(autoGrow);
}

function focus() {
	box.value?.querySelector("textarea")?.focus();
}

defineExpose({ clear, focus });
</script>
