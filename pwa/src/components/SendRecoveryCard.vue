<script setup>
import { computed, nextTick, onBeforeUnmount, ref } from "vue";
import { agentName } from "@/branding";
import { recoveryCopy } from "../lib/sendRecovery";

const props = defineProps({ request: { type: Object, required: true }, disabled: Boolean });
const emit = defineEmits(["retry", "check", "discard", "edit"]);
const copy = computed(() => recoveryCopy(props.request, agentName));
const dialog = ref(null);
const editor = ref(null);
const mode = ref("");
const text = ref("");
let opener;
async function open(kind) {
	if (props.request.state !== "rejected" || props.request.checking) return;
	opener = document.activeElement;
	mode.value = kind;
	text.value = props.request.text;
	await nextTick();
	dialog.value?.showModal();
	if (kind === "edit") editor.value?.focus();
}
function close() {
	dialog.value?.close();
	mode.value = "";
	if (opener?.isConnected) opener.focus();
}
function save() {
	if (props.request.state !== "rejected") return close();
	if (!text.value.trim() && !props.request.attachments.length) return;
	emit("edit", text.value);
	close();
}
function discard() {
	close();
	if (props.request.state === "rejected") emit("discard");
}
function keepFocus(event) {
	if (event.key !== "Tab") return;
	const controls = [
		...dialog.value.querySelectorAll("button:not(:disabled), textarea:not(:disabled)"),
	];
	const first = controls[0];
	const last = controls[controls.length - 1];
	if (event.shiftKey && document.activeElement === first) {
		event.preventDefault();
		last?.focus();
	} else if (!event.shiftKey && document.activeElement === last) {
		event.preventDefault();
		first?.focus();
	}
}

function fileHref(file) {
	return file.file_url?.startsWith("/") && !file.file_url.startsWith("//")
		? file.file_url
		: undefined;
}
onBeforeUnmount(close);
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
					:disabled="disabled || request.checking"
					@click="emit('retry')"
				>
					Retry
				</button>
				<button :disabled="request.checking" @click="open('edit')">Edit</button>
				<button :disabled="request.checking" @click="open('discard')">Discard</button>
			</div>
			<div v-else-if="request.state === 'uncertain'" class="jv-recovery-actions">
				<button class="is-primary" :disabled="request.checking" @click="emit('check')">
					Check delivery
				</button>
			</div>
		</div>
		<dialog
			ref="dialog"
			class="jv-recovery-dialog"
			:aria-label="mode === 'edit' ? 'Edit unsent message' : 'Discard unsent request'"
			@cancel.prevent="close"
			@keydown="keepFocus"
		>
			<form v-if="mode === 'edit'" @submit.prevent="save">
				<h2>Edit unsent message</h2>
				<p>
					Your newer draft stays in the composer. The files below remain attached to this
					request.
				</p>
				<label>Message<textarea ref="editor" v-model="text" rows="5" /></label>
				<ul>
					<li v-for="(file, i) in request.attachments" :key="i">
						{{ file.name || "Attachment" }}
					</li>
				</ul>
				<div class="jv-recovery-actions">
					<button type="button" @click="close">Cancel</button
					><button
						class="is-primary"
						:disabled="!text.trim() && !request.attachments.length"
					>
						Save changes
					</button>
				</div>
			</form>
			<div v-else-if="mode === 'discard'">
				<h2>Discard this unsent request?</h2>
				<p>
					This removes the preserved message from this tab. Your newer draft is kept.
					Uploaded files are not deleted.
				</p>
				<div class="jv-recovery-actions">
					<button autofocus @click="close">Keep message</button
					><button @click="discard">Discard request</button>
				</div>
			</div>
		</dialog>
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
.jv-recovery-dialog {
	inset: auto 0 0;
	width: min(100%, 520px);
	max-height: 85dvh;
	margin: auto auto 0;
	padding: 24px 20px max(24px, env(safe-area-inset-bottom));
	border: 1px solid var(--border);
	border-radius: 22px 22px 0 0;
	color: var(--ink9);
	background: var(--card);
	overflow: auto;
}
.jv-recovery-dialog::backdrop {
	background: var(--scrim);
}
.jv-recovery-dialog h2 {
	font-size: 18px;
	margin: 0 0 12px;
}
.jv-recovery-dialog p,
.jv-recovery-dialog li {
	font-size: 13px;
	line-height: 1.6;
	color: var(--ink6);
}
.jv-recovery-dialog label {
	display: block;
	margin-top: 16px;
	font-size: 12px;
}
.jv-recovery-dialog textarea {
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
