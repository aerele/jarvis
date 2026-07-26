<!--
  The message composer — presentational generic core only. All state and
  business logic stay in the host (Task 4 of the PR1 extraction; see
  docs/superpowers/plans/2026-07-24-support-ui-pr1-extraction.md).

  Built in: the bordered box + auto-growing textarea, Enter/Shift+Enter,
  attachment chips, the drag-drop overlay, the hidden file input + 📎 button,
  the "Enter ↵"/"Stop" hint, Send/Stop, and the disclaimer line.

  NOT built in (host-owned, injected via named slots so this component never
  imports chat's dependency graph): the dictation mic, the @/ mention
  dropdown, the wiki-ground toggle, the wiki nudge card, the paste hint.

  **This component NEVER uploads.** File pick / drop / paste emit
  `files-added(File[])` and the host decides upload timing — chat uploads
  eagerly on attach (its `send()` posts the uploaded `{file_url}`), while the
  standalone Support page (PR2) holds local Files and uploads on submit
  (Helpdesk's `media.upload` needs an existing ticket and has no un-attach).
  Attachment chips render purely from the `attachments` display-objects.
-->
<template>
	<!-- Host content directly above the box, in flow (chat's wiki nudge card). -->
	<slot name="above" />
	<div
		class="jv-composer"
		@dragover.prevent
		@dragenter.prevent="onDragEnter"
		@dragleave.prevent="onDragLeave"
		@drop.prevent="onDrop"
		style="
			position: relative;
			border: 1.5px solid var(--text);
			border-radius: 13px;
			background: var(--surface);
			box-shadow: 0 2px 12px rgba(0, 0, 0, 0.07);
			padding: 5px 6px 6px 6px;
			transition: border-color 0.12s, box-shadow 0.12s;
		"
	>
		<div
			v-if="dragActive"
			style="
				position: absolute;
				inset: 0;
				z-index: 40;
				display: flex;
				align-items: center;
				justify-content: center;
				background: var(--cta-bg);
				border: 2px dashed var(--cta);
				border-radius: 13px;
				color: var(--cta);
				font-size: 13px;
				font-weight: 600;
				pointer-events: none;
			"
		>
			Drop image or file to attach
		</div>
		<!-- pending attachments: image thumbnails (Claude-style) + file chips.
		     Purely a projection of the `attachments` prop — an entry with a
		     `preview_url` is a thumbnail, one with `uploading` is the in-flight
		     placeholder pill, anything else is a 📎 chip. -->
		<div
			v-if="attachments.length"
			style="display: flex; flex-wrap: wrap; gap: 8px; padding: 6px 4px 2px"
		>
			<template v-for="(a, i) in attachments" :key="a.key ?? i">
				<span
					v-if="a.uploading"
					style="font-size: 11.5px; color: var(--text-3); padding: 3px 6px"
					>Uploading…</span
				>
				<span
					v-else-if="a.preview_url"
					:title="a.file_name"
					style="position: relative; display: inline-block; line-height: 0"
				>
					<img
						:src="a.preview_url"
						alt=""
						style="
							width: 52px;
							height: 52px;
							object-fit: cover;
							border-radius: 9px;
							border: 1px solid var(--border);
							display: block;
						"
					/>
					<button
						v-if="a.removable"
						@click="emit('remove-attachment', i)"
						title="Remove"
						style="
							position: absolute;
							top: -7px;
							right: -7px;
							width: 18px;
							height: 18px;
							border-radius: 50%;
							background: var(--text);
							color: var(--surface);
							border: none;
							cursor: pointer;
							font-size: 12px;
							line-height: 1;
							display: flex;
							align-items: center;
							justify-content: center;
							padding: 0;
						"
					>
						×
					</button>
				</span>
				<span
					v-else
					style="
						display: inline-flex;
						align-items: center;
						gap: 5px;
						font-size: 11.5px;
						padding: 3px 5px 3px 9px;
						border-radius: 999px;
						color: var(--text-2);
						background: var(--surface-1);
						border: 1px solid var(--border);
					"
					>📎 {{ a.file_name
					}}<button
						v-if="a.removable"
						@click="emit('remove-attachment', i)"
						style="
							border: none;
							background: transparent;
							cursor: pointer;
							font-size: 14px;
							line-height: 1;
							color: var(--text-3);
						"
					>
						×
					</button></span
				>
			</template>
		</div>
		<!-- Host popovers anchored to the box (chat's @/ mention dropdown, which
		     is absolutely positioned) plus any in-flow notice that belongs
		     directly above the textarea (chat's paste hint). Placed after the
		     chips so an in-flow notice lands exactly where chat renders it. -->
		<slot name="overlay" />
		<!-- v-model (not :value + a manual emit) so Vue's own vModelText directive
		     keeps handling IME composition: it suppresses the update mid-compose,
		     which a hand-rolled binding would not. Its listener is registered
		     before @input, so the host sees the raw event with the new value
		     already committed — exactly the order chat had inline. -->
		<textarea
			ref="inputEl"
			v-model="text"
			@input="onInputInternal"
			@keydown="onKeydownInternal"
			@paste="onPasteInternal"
			rows="1"
			:placeholder="placeholder"
			style="
				width: 100%;
				border: none;
				outline: none;
				resize: none;
				font-family: inherit;
				font-size: 14px;
				line-height: 1.5;
				color: var(--text);
				background: transparent;
				padding: 8px 8px 4px;
			"
			:style="{ maxHeight: maxHeight + 'px' }"
		></textarea>
		<input
			ref="fileInputEl"
			type="file"
			multiple
			style="display: none"
			@change="onFilesPicked"
		/>
		<!-- Host content directly below the input and above the toolbar row (chat's
		     voice-clip chips: recovery / failed / retained / unpersisted). Full-width
		     rows live here, not in #left-toolbar, so they don't disrupt the button row. -->
		<slot name="below-input" />
		<div style="display: flex; align-items: center; gap: 6px; padding: 2px 4px">
			<!-- Host toolbar buttons on the left (chat: mic + 📎 + wiki-ground).
			     `pickFiles` is handed down so a host that takes the slot over can
			     still open this component's own hidden file input. The fallback is
			     the plain attach button the generic composer ships with. -->
			<slot name="left-toolbar" :pick-files="pickFiles">
				<button
					class="jv-iconbtn"
					title="Attach file"
					@click="pickFiles"
					style="
						width: 30px;
						height: 30px;
						display: flex;
						align-items: center;
						justify-content: center;
						background: transparent;
						border: none;
						border-radius: 7px;
						cursor: pointer;
						color: var(--text-3);
					"
				>
					<svg
						width="17"
						height="17"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.7"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="M21.44 11.05l-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a1.5 1.5 0 0 1-2.12-2.12l8.49-8.49"
						/>
					</svg>
				</button>
			</slot>
			<span
				style="margin-left: auto; font-size: 11px; color: var(--text-3); margin-right: 4px"
				>{{ busy ? "Stop" : "Enter ↵" }}</span
			>
			<button
				v-if="busy"
				@click="emit('stop')"
				title="Stop generating"
				style="
					width: 32px;
					height: 32px;
					display: flex;
					align-items: center;
					justify-content: center;
					background: var(--cta);
					border: none;
					border-radius: 8px;
					cursor: pointer;
				"
			>
				<svg width="13" height="13" viewBox="0 0 24 24" fill="var(--cta-fg)">
					<rect x="6" y="6" width="12" height="12" rx="2.5" />
				</svg>
			</button>
			<button
				v-else
				class="jv-sendbtn"
				:class="{ ready: sendable }"
				@click="emit('submit')"
				:disabled="!sendable"
				:style="{
					width: '32px',
					height: '32px',
					display: 'flex',
					alignItems: 'center',
					justifyContent: 'center',
					background: sendable ? 'var(--cta)' : 'var(--surface-3)',
					border: 'none',
					borderRadius: '8px',
					cursor: sendable ? 'pointer' : 'default',
				}"
			>
				<svg
					width="16"
					height="16"
					viewBox="0 0 24 24"
					fill="none"
					:stroke="sendable ? 'var(--cta-fg)' : 'var(--text-3)'"
					stroke-width="2.1"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M12 19V5M5 12l7-7 7 7" />
				</svg>
			</button>
		</div>
	</div>
	<div
		v-if="disclaimer"
		style="text-align: center; font-size: 10.5px; color: var(--text-3); margin-top: 8px"
	>
		{{ disclaimer }}
	</div>
	<slot name="footer" />
</template>

<script setup>
import { computed, ref, watch } from "vue";

const props = defineProps({
	// The composed text. Use with v-model on the host.
	modelValue: { type: String, default: "" },
	// Display-objects ONLY — never File objects, never the host's upload
	// records: { key, file_name, preview_url, removable, uploading }.
	// `preview_url` set → image thumbnail; `uploading` → the in-flight pill.
	attachments: { type: Array, default: () => [] },
	// A turn is in flight: swaps Send for Stop and the hint for "Stop".
	busy: { type: Boolean, default: false },
	// Optional override for whether Send is armed. `null` (the default) derives
	// it from text/attachments; chat passes its own (it also blocks on
	// `sending` and a suspended subscription).
	canSend: { type: Boolean, default: null },
	placeholder: { type: String, default: "" },
	// Fine print under the box. Empty string hides the line entirely.
	disclaimer: { type: String, default: "" },
	// Auto-grow ceiling, in px — past this the textarea scrolls.
	maxHeight: { type: Number, default: 140 },
});

const emit = defineEmits([
	"update:modelValue",
	"submit",
	"stop",
	// (File[]) — picked, dropped or pasted. The HOST uploads; we never do.
	"files-added",
	// (index) — into the `attachments` array as passed in.
	"remove-attachment",
	// Raw DOM events, emitted BEFORE this component acts on them, so a host can
	// preventDefault to take the interaction over (chat's mention navigation,
	// prompt history and clipboard-image upload all do exactly that).
	"input",
	"keydown",
	"paste",
]);

const inputEl = ref(null);
const fileInputEl = ref(null);

// Writable proxy so the textarea can use a real v-model (see the template note).
const text = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const sendable = computed(() =>
	props.canSend === null
		? props.modelValue.trim().length > 0 || props.attachments.length > 0
		: props.canSend
);

// ---- auto-grow ----
function autoGrow() {
	const el = inputEl.value;
	if (!el) return;
	el.style.height = "auto";
	el.style.height = Math.min(el.scrollHeight, props.maxHeight) + "px";
}
// Programmatic changes (a restored draft, dictation, prompt-history recall, a
// send that clears the box) only reach us as a new modelValue — flush:"post"
// so the textarea's value is already in the DOM when we measure scrollHeight.
// Typed input is grown synchronously in the handler below instead, so the box
// never lags a frame behind the caret.
watch(() => props.modelValue, autoGrow, { flush: "post" });

// ---- textarea events: emit raw FIRST, then act (unless the host took over) ----
function onInputInternal(e) {
	// v-model has already pushed the new value up; grow, then hand the host the
	// raw event (chat parses @/ mentions off the caret here).
	//
	// DO NOT "optimise" this call away as redundant with the watcher above. Yes,
	// a normal keystroke measures twice per input (here, then post-flush) — but
	// during IME composition Vue's vModelText suppresses the modelValue update
	// entirely, so the watcher does NOT fire and only this call keeps the box
	// growing mid-compose (the pre-extraction inline `autoGrow()` did grow).
	// A dedup flag can't fix that without special-casing the IME path, which is
	// exactly the path that has no other grower.
	autoGrow();
	emit("input", e);
}
function onKeydownInternal(e) {
	emit("keydown", e);
	// The host claimed this key (chat: mention nav, prompt history, its own
	// Enter-to-send) — never double-handle it.
	if (e.defaultPrevented) return;
	if (e.key === "Enter" && !e.shiftKey) {
		e.preventDefault();
		emit("submit");
	}
}
function onPasteInternal(e) {
	emit("paste", e);
	if (e.defaultPrevented) return;
	const cd = e.clipboardData;
	if (!cd) return;
	// Screenshots / "Copy image" land in .items; a copied image FILE populates
	// .files. Check both, .files first. Images only: a pasted non-image file
	// is left to the browser's normal paste handling.
	const imgs = [];
	for (const f of cd.files || []) {
		if ((f.type || "").startsWith("image/")) imgs.push(f);
	}
	if (!imgs.length) {
		for (const it of cd.items || []) {
			if (it.kind === "file" && (it.type || "").startsWith("image/")) {
				const f = it.getAsFile();
				if (f) imgs.push(f);
			}
		}
	}
	if (!imgs.length) return;
	e.preventDefault();
	emit("files-added", imgs);
}

// ---- file input ----
function pickFiles() {
	fileInputEl.value?.click();
}
function onFilesPicked(e) {
	const picked = Array.from(e.target.files || []);
	// Reset so re-picking the same file still fires `change`.
	e.target.value = "";
	if (picked.length) emit("files-added", picked);
}

// ---- drag and drop ----
// dragDepth guards against the flicker from dragenter/leave firing on children.
const dragActive = ref(false);
let _dragDepth = 0;
function onDragEnter() {
	_dragDepth++;
	dragActive.value = true;
}
function onDragLeave() {
	_dragDepth = Math.max(0, _dragDepth - 1);
	if (!_dragDepth) dragActive.value = false;
}
function onDrop(e) {
	_dragDepth = 0;
	dragActive.value = false;
	const dropped = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
	if (dropped.length) emit("files-added", dropped);
}

function focusInput() {
	inputEl.value?.focus();
}
// `el` is the raw textarea: the host owns caret math (chat's mention insertion
// and edit-and-resend both setSelectionRange on it).
defineExpose({ el: inputEl, focusInput });
</script>

<style scoped>
/* black focus highlight on the composer */
.jv-composer:focus-within {
	border-color: var(--text);
	box-shadow: 0 0 0 3px rgba(23, 23, 23, 0.07);
}
/* The send button inverts to black/white on hover (depends on its base color,
   so the white icon flips to the surface color). !important beats the inline
   background. */
/* Send button: springy lift + arrow nudge on hover, press-in on click, and a
   one-shot pop when it becomes ready (text entered). */
.jv-sendbtn {
	transition: transform 0.16s cubic-bezier(0.34, 1.56, 0.64, 1), background 0.14s ease;
}
.jv-sendbtn svg {
	transition: transform 0.16s ease;
}
.jv-sendbtn:not(:disabled):hover {
	transform: translateY(-2px) scale(1.07);
}
.jv-sendbtn:not(:disabled):hover svg {
	transform: translateY(-2px);
}
.jv-sendbtn:not(:disabled):active {
	transform: scale(0.9);
}
.jv-sendbtn.ready {
	animation: jv-send-pop 0.3s ease;
}
/* Vue scopes @keyframes names, so the keyframe and the `animation:` that
   references it MUST share one scoped block — it travels with .jv-sendbtn. */
@keyframes jv-send-pop {
	0% {
		transform: scale(0.7);
	}
	55% {
		transform: scale(1.15);
	}
	100% {
		transform: scale(1);
	}
}
.jv-sendbtn:hover:not(:disabled) {
	background: var(--text) !important;
}
.jv-sendbtn:hover:not(:disabled) svg {
	stroke: var(--surface) !important;
}
/* buttons invert to black/white on hover (theme-adaptive: black on light,
   white on dark) — var(--text)/var(--surface) flip, with an svg-stroke
   override so the icon stays visible on the inverted background.
   COPIED, not moved: `.jv-iconbtn` is also worn by chat's header buttons and
   by the mic/wiki/nudge buttons it slots back in here, which carry ChatView's
   scope id — so ChatView keeps its own copy of these rules. This copy styles
   the default attach button above (the #left-toolbar fallback).

   MUST STAY IN SYNC — `.jv-iconbtn` lives in THREE places on purpose:
     1. here (scoped to Composer — its default attach button)
     2. `views/ChatView.vue` (scoped — header buttons + the mic/attach/wiki/nudge
        buttons chat slots BACK into this component, which carry ChatView's
        scope id, so these rules cannot reach them)
     3. `assets/settings.css` — GLOBAL, and DELIBERATELY DIFFERENT: the settings
        dialog wants a quiet ghost hover (--surface-2), not this invert. Do not
        "unify" it with these two.
   Hoisting 1+2 into a shared sheet was evaluated and rejected: without the
   `[data-v-*]` attribute these rules go global, and their `!important` would
   then beat settings.css's un-!important ghost hover on SettingsDialog's close
   button — i.e. it would silently restyle the settings dialog. Change the hover
   here and you MUST make the same edit in ChatView. */
.jv-iconbtn:hover {
	background: var(--text) !important;
	color: var(--surface) !important;
}
.jv-iconbtn:hover svg {
	stroke: var(--surface) !important;
}
/* visible keyboard focus (UX #15) */
.jv-sendbtn:focus-visible,
.jv-iconbtn:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* honor reduced-motion (UX #13) */
@media (prefers-reduced-motion: reduce) {
	.jv-sendbtn.ready {
		animation: none;
	}
}
/* Dark mode: a black hover is invisible on the dark surface and the invert
   would flash a stark white button, so neutral buttons get a subtle elevated
   grey hover and primaries keep their colour (just brighter). */
.jv-dark .jv-iconbtn:hover {
	background: var(--surface-3) !important;
	color: var(--text) !important;
	border-color: var(--border-2) !important;
}
.jv-dark .jv-iconbtn:hover svg {
	stroke: var(--text) !important;
}
.jv-dark .jv-sendbtn:hover:not(:disabled) {
	background: var(--cta) !important;
	color: var(--cta-fg) !important;
	border-color: var(--cta) !important;
	filter: brightness(1.18);
}
.jv-dark .jv-sendbtn:hover:not(:disabled) svg {
	stroke: var(--cta-fg) !important;
}
</style>
