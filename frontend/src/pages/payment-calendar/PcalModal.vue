<template>
	<Teleport to="body">
		<div
			v-if="modelValue"
			ref="overlay"
			class="pcal-overlay pcal-backdrop"
			@mousedown.self="pressOnBackdrop"
			@mouseup="releaseOnBackdrop"
			@click="keepFocusInside"
			@keydown.tab="trapTab"
		>
			<section
				ref="dialog"
				tabindex="-1"
				class="pcal-modal"
				:class="`pcal-modal-${size}`"
				role="dialog"
				aria-modal="true"
				:aria-labelledby="headingId"
			>
				<div class="pcal-modal-heading">
					<h2 :id="headingId">{{ title }}</h2>
					<button
						:id="closeId"
						ref="closeButton"
						type="button"
						class="pcal-icon-btn"
						:aria-label="closeLabel"
						@click="close"
					>
						<svg viewBox="0 0 24 24" aria-hidden="true">
							<path d="m6 6 12 12M18 6 6 18" />
						</svg>
					</button>
				</div>
				<slot />
			</section>
		</div>
	</Teleport>
</template>
<script setup>
import { nextTick, onBeforeUnmount, ref, useId, watch } from "vue";
import { OVERLAY_PRIORITY, useOverlay } from "./overlayStack";
import "./tokens.css";
import "./PcalModal.css";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	title: { type: String, required: true },
	closeLabel: { type: String, required: true },
	// Optional id of the Close button (the template's #close-aux / #close-details)
	closeId: { type: String, default: undefined },
	// sm 650 (balances, aux), md 850, lg 1080 (full details)
	size: { type: String, default: "md" },
	// "aux" sits above "details" in the Escape order
	layer: { type: String, default: "details" },
	// Where focus goes when the element that opened the modal is gone
	fallbackFocus: { type: String, default: "#selected-invoices" },
});
const emit = defineEmits(["update:modelValue"]);
const headingId = `pcal-modal-${useId()}`;
const overlay = ref(null),
	dialog = ref(null),
	closeButton = ref(null);
let opener = null,
	pressedOnBackdrop = false;

const FOCUSABLE =
	'button:not(:disabled), input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])';

function close() {
	emit("update:modelValue", false);
}
// Keeps focus in the dialog: a press on the backdrop must not move it to <body>, and a click
// that still ends outside the dialog puts it back on the dialog itself (tabindex -1).
function pressOnBackdrop(event) {
	pressedOnBackdrop = true;
	event.preventDefault();
}
function keepFocusInside() {
	if (!dialog.value?.contains(document.activeElement)) dialog.value?.focus();
}
// A drag that starts or ends inside the dialog (text selection) must not dismiss it.
function releaseOnBackdrop(event) {
	const both = pressedOnBackdrop && event.target === event.currentTarget;
	pressedOnBackdrop = false;
	if (both) close();
}
useOverlay(() => props.modelValue, OVERLAY_PRIORITY[props.layer], close);

// Runs before the render, so document.activeElement is still the opener. Focus is restored
// only on a true -> false change: a modal that mounts closed must not move focus.
watch(
	() => props.modelValue,
	(open, wasOpen) => {
		if (!open) return void (wasOpen && nextTick(restoreFocus));
		opener = document.activeElement;
		void nextTick(() => closeButton.value?.focus());
	},
	{ immediate: true }
);
onBeforeUnmount(() => {
	if (props.modelValue) restoreFocus();
});

function restoreFocus() {
	const target = opener?.isConnected ? opener : document.querySelector(props.fallbackFocus);
	opener = null;
	target?.focus();
}

// Tab and Shift+Tab wrap inside the dialog. The keydown only fires for focus inside it.
function trapTab(event) {
	const nodes = [...overlay.value.querySelectorAll(FOCUSABLE)].filter(
		(node) => !node.closest("[hidden]")
	);
	if (!nodes.length) return void event.preventDefault();
	// The dialog itself (focused by a click on plain text) is not in the list: Shift+Tab from it
	// goes to the last control, Tab to the first.
	const index = nodes.indexOf(document.activeElement);
	const wrapTo = event.shiftKey
		? index <= 0
			? nodes[nodes.length - 1]
			: null
		: index === -1 || index === nodes.length - 1
		? nodes[0]
		: null;
	if (!wrapTo) return;
	event.preventDefault();
	wrapTo.focus();
}
</script>
