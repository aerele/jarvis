<script setup>
import { computed, ref, watch } from "vue";
import { agentName } from "@/branding";
import { recoveryCopy } from "../lib/sendRecovery";
import Sheet from "./Sheet.vue";

const props = defineProps({
	request: { type: Object, required: true },
	disabled: Boolean,
	backup: Function,
});
const emit = defineEmits([
	"retry",
	"check",
	"discard",
	"edit",
	"edit-new",
	"reload",
	"retry-same",
	"edit-picks",
]);
const copy = computed(() => recoveryCopy(props.request, agentName));
const mode = ref("");
const text = ref("");
const backupNote = ref("");
const updateRequired = computed(() => props.request.result?.reason === "release_update_required");
const uncertain = computed(() => props.request.state === "uncertain");
const title = computed(
	() =>
		({
			edit: "Edit unsent message",
			discard: "Discard preserved request?",
			new: "Edit as new message?",
			reload: "Reload to update Jarvis?",
		}[mode.value] || "")
);
function open(kind) {
	if (!["rejected", "uncertain"].includes(props.request.state) || props.request.checking) return;
	mode.value = kind;
	text.value = props.request.text;
	backupNote.value = "";
}
function close() {
	mode.value = "";
}
watch(() => props.request.state, close);
function save() {
	if (props.request.state !== "rejected") return close();
	if (!text.value.trim() && !props.request.attachments.length) return;
	emit("edit", text.value);
	close();
}
function discard() {
	if (["rejected", "uncertain"].includes(props.request.state)) emit("discard");
	close();
}
function editNew() {
	if (uncertain.value) emit("edit-new");
	close();
}
function backup() {
	try {
		props.backup?.();
		backupNote.value =
			"Backup download requested. Check that the file was saved before reloading. It contains message text and file links, not the uploaded files themselves.";
	} catch {
		backupNote.value =
			"Could not download a backup. Copy your messages and file links before reloading.";
	}
}
function fileHref(file) {
	return file.file_url?.startsWith("/") && !file.file_url.startsWith("//")
		? file.file_url
		: undefined;
}
</script>

<template>
	<div class="jv-recover-request">
		<div v-if="request.text" class="jv-recover-bubble">{{ request.text }}</div>
		<ul
			v-if="request.attachments.length"
			class="jv-recover-files"
			aria-label="Preserved attachments"
		>
			<li v-for="(file, i) in request.attachments" :key="i">
				<svg
					viewBox="0 0 24 24"
					width="18"
					height="18"
					fill="none"
					stroke="currentColor"
					stroke-width="1.6"
					aria-hidden="true"
				>
					<path
						d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6"
					/>
				</svg>
				<a :href="fileHref(file)" target="_blank" rel="noopener noreferrer">{{
					file.name || "Attachment"
				}}</a>
			</li>
		</ul>
		<div class="jv-recovery" :class="{ 'is-accepted': request.state === 'accepted' }">
			<div role="status" aria-live="polite">
				<strong>{{ request.checking ? "Checking delivery…" : copy.title }}</strong>
				<p>{{ copy.detail }}</p>
				<p v-if="request.note">{{ request.note }}</p>
			</div>
			<div v-if="request.state === 'rejected'" class="jv-recovery-actions">
				<button
					class="is-primary"
					:disabled="(!updateRequired && disabled) || request.checking"
					@click="updateRequired ? open('reload') : emit('retry')"
				>
					{{ updateRequired ? "Reload" : "Retry" }}
				</button>
				<button :disabled="request.checking" @click="open('edit')">Edit</button>
				<button
					v-if="!request.conversation"
					:disabled="request.checking"
					@click="emit('edit-picks')"
				>
					Edit message and model
				</button>
				<button :disabled="request.checking" @click="open('discard')">Discard</button>
			</div>
			<div v-else-if="request.state === 'uncertain'" class="jv-recovery-actions">
				<button class="is-primary" :disabled="request.checking" @click="emit('check')">
					Check delivery
				</button>
				<button :disabled="request.checking || disabled" @click="emit('retry-same')">
					Retry same request
				</button>
				<button :disabled="request.checking" @click="open('new')">
					Edit as new message
				</button>
				<button :disabled="request.checking" @click="open('discard')">Discard</button>
			</div>
		</div>
		<Sheet :open="!!mode" :label="title" @close="close">
			<div class="jv-recovery-editor">
				<h2>{{ title }}</h2>
				<form v-if="mode === 'edit'" @submit.prevent="save">
					<p>
						Your newer draft stays in the composer. The files below remain attached to
						this request.
					</p>
					<label>Message<textarea v-model="text" autofocus rows="5" /></label>
					<ul>
						<li v-for="(file, i) in request.attachments" :key="i">
							{{ file.name || "Attachment" }}
						</li>
					</ul>
					<div class="jv-recovery-actions">
						<button type="button" @click="close">Cancel</button>
						<button
							class="is-primary"
							:disabled="!text.trim() && !request.attachments.length"
						>
							Save changes
						</button>
					</div>
				</form>
				<template v-else-if="mode === 'discard'">
					<p>
						This removes the preserved message from this tab. Your newer draft is kept.
						Uploaded files are not deleted.
					</p>
					<p v-if="uncertain">
						Delivery is unknown. Discarding does not cancel work that may already be
						running.
					</p>
					<div class="jv-recovery-actions">
						<button autofocus @click="close">Keep message</button
						><button @click="discard">Discard request</button>
					</div>
				</template>
				<template v-else-if="mode === 'new'">
					<p>
						The original request may already have been received or executed. Sending it
						again could duplicate work. Check the conversation before sending again.
					</p>
					<p>
						This moves the text and files into the composer without sending. Your
						current draft stays saved separately. Previous approval selections are not
						reused.
					</p>
					<div class="jv-recovery-actions">
						<button autofocus @click="close">Cancel</button
						><button @click="editNew">Move to composer</button>
					</div>
				</template>
				<template v-else-if="mode === 'reload'">
					<p>
						Reload is required before you can send again. Reloading clears all
						preserved requests and drafts in this tab, including other conversations.
						Save a backup first.
					</p>
					<p role="status">{{ backupNote }}</p>
					<div class="jv-recovery-actions">
						<button autofocus @click="close">Cancel</button
						><button @click="backup">Download backup</button
						><button @click="emit('reload')">Reload now</button>
					</div>
				</template>
			</div>
		</Sheet>
	</div>
</template>

<style scoped>
.jv-recover-request {
	display: flex;
	flex-direction: column;
	align-items: flex-end;
	gap: 8px;
}
.jv-recover-bubble {
	max-width: 82%;
	padding: 10px 13px;
	border-radius: 16px 16px 5px 16px;
	background: var(--accent-bg);
	color: var(--ink8);
	font-size: 13.5px;
	line-height: 1.45;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
}
.jv-recover-files {
	list-style: none;
	margin: 0;
	padding: 0;
	width: 82%;
	display: grid;
	gap: 6px;
}
.jv-recover-files li {
	display: flex;
	gap: 8px;
	align-items: center;
	border: 1px solid var(--border);
	border-radius: 9px;
	padding: 10px;
	background: var(--card);
	color: var(--ink6);
}
.jv-recover-files a {
	color: var(--ink8);
	font-size: 12px;
	overflow-wrap: anywhere;
}
.jv-recover-files svg {
	flex: none;
}
.jv-recovery {
	width: 82%;
	border: 1px solid var(--border2);
	border-radius: 10px;
	background: var(--card);
	padding: 12px;
	font-size: 12px;
}
.jv-recovery strong {
	color: var(--amber);
	font-weight: 600;
}
.jv-recovery.is-accepted strong {
	color: var(--green);
}
.jv-recovery p {
	margin: 8px 0 0;
	color: var(--ink6);
	line-height: 1.5;
	overflow-wrap: anywhere;
}
.jv-recovery-actions {
	display: flex;
	flex-wrap: wrap;
	gap: 7px;
	margin-top: 12px;
}
.jv-recovery-actions button {
	min-height: 44px;
	padding: 8px 12px;
	border: 1px solid var(--border2);
	border-radius: 8px;
	color: var(--ink7);
	background: var(--card);
	font: inherit;
	cursor: pointer;
}
.jv-recovery-actions .is-primary {
	background: var(--inv-bg);
	color: var(--inv-ink);
	border-color: var(--inv-bg);
}
.jv-recovery-actions button:disabled {
	opacity: 0.45;
	cursor: default;
}
.jv-recovery-editor {
	padding: 24px 20px max(24px, env(safe-area-inset-bottom));
	color: var(--ink9);
	overflow: auto;
}
.jv-recovery-editor h2 {
	font-size: 18px;
	margin: 0 0 12px;
}
.jv-recovery-editor p,
.jv-recovery-editor li {
	font-size: 13px;
	line-height: 1.6;
	color: var(--ink6);
}
.jv-recovery-editor label {
	display: block;
	margin-top: 16px;
	font-size: 12px;
}
.jv-recovery-editor textarea {
	display: block;
	width: 100%;
	padding: 12px;
	margin-top: 8px;
	border: 1px solid var(--border2);
	border-radius: 10px;
	background: var(--card2);
	color: var(--ink9);
	font: inherit;
	font-size: 16px;
	line-height: 1.5;
}
</style>
