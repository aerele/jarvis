<script setup>
import { computed, ref } from "vue";
import { agentName } from "@/branding";

// Attachments + dictation + send/stop. Split out of ChatView so the thread
// screen stays about the thread.
const props = defineProps({
	modelValue: { type: String, default: "" },
	sending: { type: Boolean, default: false },
	attachments: { type: Array, default: () => [] },
	micEnabled: { type: Boolean, default: false },
	placeholder: { type: String, default: () => `Message ${agentName}…` },
});
const emit = defineEmits(["update:modelValue", "send", "stop", "attach", "remove", "mic"]);

const inputEl = ref(null);
const fileEl = ref(null);

const uploading = computed(() => props.attachments.some((a) => a.uploading));
// An attachment still uploading is not sendable: the worker would get a
// file_url that doesn't exist yet.
const canSend = computed(
	() =>
		!uploading.value &&
		(props.modelValue.trim().length > 0 || props.attachments.some((a) => a.file_url))
);

function onInput(e) {
	emit("update:modelValue", e.target.value);
	autoGrow();
}

function autoGrow() {
	const el = inputEl.value;
	if (!el) return;
	el.style.height = "auto";
	el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
}

// Enter sends on a physical keyboard; on a phone the on-screen Return key should
// insert a newline, so only intercept when there is no soft keyboard.
function onKeydown(e) {
	if (e.key === "Enter" && !e.shiftKey && !/Mobi|Android/i.test(navigator.userAgent)) {
		e.preventDefault();
		if (canSend.value) emit("send");
	}
}

function pick(e) {
	const files = [...(e.target.files || [])];
	if (files.length) emit("attach", files);
	// Reset so picking the same file twice in a row still fires a change event.
	e.target.value = "";
}

function reset() {
	autoGrow();
}
defineExpose({ reset });
</script>

<template>
	<div class="jv-composer jv-safe-bottom">
		<div v-if="props.attachments.length" class="jv-atts">
			<div v-for="a in props.attachments" :key="a.key" class="jv-att">
				<img v-if="a.preview" class="jv-att-img" :src="a.preview" :alt="a.name" />
				<div v-else class="jv-att-file">
					<svg
						viewBox="0 0 24 24"
						width="16"
						height="16"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
						<path d="M14 2v6h6" />
					</svg>
					<span class="jv-att-name">{{ a.name }}</span>
				</div>
				<div v-if="a.uploading" class="jv-att-busy"><span class="jv-spinner" /></div>
				<button
					class="jv-att-x"
					aria-label="Remove attachment"
					@click="emit('remove', a.key)"
				>
					<svg
						viewBox="0 0 24 24"
						width="11"
						height="11"
						fill="none"
						stroke="currentColor"
						stroke-width="2.6"
						stroke-linecap="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</button>
			</div>
		</div>

		<div class="jv-composer-row">
			<input ref="fileEl" type="file" multiple hidden @change="pick" />
			<button
				class="jv-icon-btn"
				aria-label="Attach a file"
				:disabled="props.sending"
				@click="fileEl.click()"
			>
				<svg
					viewBox="0 0 24 24"
					width="21"
					height="21"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path
						d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"
					/>
				</svg>
			</button>

			<div class="jv-pill">
				<textarea
					ref="inputEl"
					rows="1"
					:value="props.modelValue"
					:placeholder="props.placeholder"
					@input="onInput"
					@keydown="onKeydown"
				/>
				<button
					v-if="props.micEnabled"
					class="jv-mic"
					aria-label="Dictate"
					@click="emit('mic')"
				>
					<svg
						viewBox="0 0 24 24"
						width="18"
						height="18"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
						<path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3" />
					</svg>
				</button>
			</div>

			<button
				v-if="props.sending"
				class="jv-send is-stop"
				aria-label="Stop"
				@click="emit('stop')"
			>
				<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor">
					<rect x="6" y="6" width="12" height="12" rx="2" />
				</svg>
			</button>
			<button
				v-else
				class="jv-send"
				aria-label="Send"
				:disabled="!canSend"
				@click="emit('send')"
			>
				<svg
					viewBox="0 0 24 24"
					width="19"
					height="19"
					fill="none"
					stroke="currentColor"
					stroke-width="2.1"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M12 19V5M5 12l7-7 7 7" />
				</svg>
			</button>
		</div>
	</div>
</template>

<style scoped>
.jv-composer {
	flex: none;
	background: var(--menu-bar);
	border-top: 1px solid var(--border);
}
.jv-composer-row {
	display: flex;
	align-items: flex-end;
	gap: 8px;
	padding: 8px 12px 6px;
}
.jv-pill {
	flex: 1;
	min-width: 0;
	display: flex;
	align-items: flex-end;
	border: 1px solid var(--border2);
	border-radius: 19px;
	background: var(--card);
	padding-left: 14px;
	padding-right: 4px;
}
.jv-pill:focus-within {
	border-color: var(--accent);
}
.jv-pill textarea {
	flex: 1;
	min-width: 0;
	resize: none;
	border: 0;
	outline: none;
	background: transparent;
	color: var(--ink9);
	font: inherit;
	font-size: 15px;
	line-height: 1.35;
	padding: 9px 0;
	max-height: 120px;
}
.jv-mic {
	display: grid;
	place-items: center;
	width: 30px;
	height: 30px;
	margin-bottom: 4px;
	flex: none;
	border: 0;
	border-radius: 50%;
	background: transparent;
	color: var(--ink5);
	cursor: pointer;
}
.jv-send {
	flex: none;
	width: 38px;
	height: 38px;
	display: grid;
	place-items: center;
	border: 0;
	border-radius: 50%;
	background: var(--inv-bg);
	color: var(--inv-ink);
	cursor: pointer;
}
.jv-send:disabled {
	opacity: 0.4;
	cursor: default;
}
.jv-send.is-stop {
	background: var(--inv-bg);
}

.jv-atts {
	display: flex;
	flex-wrap: wrap;
	gap: 12px;
	padding: 12px 14px 2px;
}
.jv-att {
	position: relative;
}
.jv-att-img {
	display: block;
	width: 52px;
	height: 52px;
	object-fit: cover;
	border: 1px solid var(--border);
	border-radius: 9px;
	background: var(--card2);
}
.jv-att-file {
	display: flex;
	align-items: center;
	gap: 7px;
	max-width: 180px;
	height: 52px;
	padding: 0 10px;
	border: 1px solid var(--border);
	border-radius: 9px;
	background: var(--card2);
	color: var(--ink6);
}
.jv-att-name {
	font-size: 11.5px;
	font-weight: 500;
	color: var(--ink8);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-att-busy {
	position: absolute;
	inset: 0;
	display: grid;
	place-items: center;
	border-radius: 9px;
	background: rgba(0, 0, 0, 0.35);
}
.jv-att-x {
	position: absolute;
	top: -6px;
	right: -6px;
	display: grid;
	place-items: center;
	width: 20px;
	height: 20px;
	border: 2px solid var(--menu-bar);
	border-radius: 999px;
	background: var(--inv-bg);
	color: var(--inv-ink);
	cursor: pointer;
	padding: 0;
}
.jv-spinner {
	width: 16px;
	height: 16px;
	border-radius: 50%;
	border: 2px solid rgba(255, 255, 255, 0.35);
	border-top-color: #fff;
	animation: jv-spin 0.7s linear infinite;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
</style>
