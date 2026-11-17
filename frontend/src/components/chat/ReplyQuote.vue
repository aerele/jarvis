<!--
  aerele/jarvis#2191: the part of an earlier reply the next message replies to, shown
  inside the composer above the text (as WhatsApp and Claude do): an accent bar, who
  is quoted, the text (clamped) and a close button. The quote is not in the textarea:
  the chat view prepends it to the message as a Markdown blockquote when it is sent.
  Shared by the desktop chat and the PWA (through @shared).
-->
<template>
	<div class="jv-rq" role="group" :aria-label="`Replying to ${who}`">
		<div class="jv-rq-body">
			<div class="jv-rq-who">
				<svg
					width="12"
					height="12"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="M9 14 4 9l5-5" />
					<path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
				</svg>
				Replying to {{ who }}
			</div>
			<div class="jv-rq-text">{{ text }}</div>
		</div>
		<button
			type="button"
			class="jv-rq-x"
			title="Remove quote"
			aria-label="Remove quote"
			@click="emit('close')"
		>
			<svg
				width="14"
				height="14"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				aria-hidden="true"
			>
				<path d="M18 6 6 18M6 6l12 12" />
			</svg>
		</button>
	</div>
</template>

<script setup>
defineProps({
	text: { type: String, required: true },
	who: { type: String, default: "Jarvis" },
});
const emit = defineEmits(["close"]);
</script>

<style scoped>
/* The Jarvis brand (--brand-1 / --brand-2 / --brand-grad, main.css and the PWA's
   index.css): the gradient of the Jarvis mark, theme-invariant on purpose. The bar is
   the gradient; the label is --brand-2; the card is the surface with a faint brand
   tint. Text and the close button keep each app's own palette. */
.jv-rq {
	position: relative;
	display: flex;
	align-items: flex-start;
	gap: 8px;
	margin: 3px 2px 6px;
	padding: 7px 6px 7px 13px;
	border: 1px solid var(--border);
	border-radius: 9px;
	background: var(--surface, var(--card));
	background: color-mix(in srgb, var(--brand-2, #8b5cf6) 7%, var(--surface, var(--card)));
	overflow: hidden;
}
.jv-rq::before {
	content: "";
	position: absolute;
	left: 0;
	top: 0;
	bottom: 0;
	width: 3px;
	background: var(--brand-grad, linear-gradient(135deg, #6e8bff, #8b5cf6));
}
.jv-rq-body {
	flex: 1;
	min-width: 0;
}
.jv-rq-who {
	display: flex;
	align-items: center;
	gap: 5px;
	font-size: 12px;
	font-weight: 600;
	color: var(--brand-2, #8b5cf6);
	margin-bottom: 2px;
}
.jv-rq-text {
	font-size: 13px;
	line-height: 1.4;
	color: var(--text-2, var(--ink6));
	white-space: pre-line;
	overflow-wrap: anywhere;
	display: -webkit-box;
	-webkit-line-clamp: 2;
	-webkit-box-orient: vertical;
	overflow: hidden;
}
.jv-rq-x {
	flex: none;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	width: 24px;
	height: 24px;
	border: 0;
	border-radius: 6px;
	background: transparent;
	color: var(--text-3, var(--ink5));
	cursor: pointer;
}
.jv-rq-x:hover {
	background: var(--surface-3, var(--card3));
	color: var(--text, var(--ink9));
}
</style>
