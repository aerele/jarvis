<!--
  A single chat/thread message, presentational only — all state and business
  logic (which message is "copied", failed-send retry, edit-and-resend, the
  markdown/HTML renderer) live in the parent. Two variants:
    - "bubble": right-aligned chat user message (implemented here, Task 2 of
      the PR1 extraction — see docs/superpowers/plans/2026-07-24-support-ui-
      pr1-extraction.md).
    - "row": left-aligned assistant/support-agent message with an identity
      line + markdown/HTML body (Task 3; stubbed here so the props/emits
      interface is final and Task 3 only has to fill in the template).
  Shared with the standalone Support page (PR2): a support customer message
  is also variant="bubble"; a support agent reply is variant="row" with a
  round human avatar via the #avatar slot (vs. chat's JarvisMark).
-->
<template>
	<template v-if="variant === 'bubble'">
		<div class="jv-umsg" style="display: flex; flex-direction: column; align-items: flex-end">
			<div
				v-if="text"
				class="jv-ububble"
				style="
					max-width: 78%;
					min-width: 0;
					background: var(--surface-2);
					border: 1px solid var(--border);
					border-radius: 14px 14px 4px 14px;
					padding: 10px 14px;
					font-size: 14px;
					line-height: 1.5;
					color: var(--text);
					white-space: pre-wrap;
					overflow-wrap: anywhere;
				"
			>
				{{ text }}
			</div>
			<div
				v-if="failed"
				style="
					display: flex;
					align-items: center;
					gap: 8px;
					margin-top: 4px;
					font-size: 11.5px;
					color: var(--red);
				"
			>
				<span>Not sent</span>
				<button
					@click="emit('retry')"
					style="
						background: none;
						border: none;
						color: var(--link);
						font: inherit;
						cursor: pointer;
						padding: 0;
						text-decoration: underline;
					"
				>
					Retry
				</button>
			</div>
			<!-- attached images → same clickable thumbnail + preview as generated ones -->
			<template v-for="cv in attachments || []" :key="cv.name">
				<button
					v-if="cv.type === 'image' && cv.file_url"
					class="jv-img-artifact"
					@click="emit('open-attachment', cv)"
					:title="'Open ' + cv.title"
					style="margin-top: 8px; cursor: zoom-in"
				>
					<img :src="cv.file_url" :alt="cv.title" loading="lazy" />
				</button>
			</template>
			<div class="jv-msgbar">
				<!-- sent-time: revealed with the bar on hover; its own hover
				     (native title) gives the full day-date-month-year-time.
				     Order: time → edit → copy (edit before copy). -->
				<span v-if="timestamp" class="jv-msgtime" :title="timestampFull">{{
					timestamp
				}}</span>
				<button
					v-if="editable"
					class="jv-msgbtn"
					@click="emit('edit')"
					title="Edit & resend"
				>
					<svg
						width="14"
						height="14"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
						<path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
					</svg>
				</button>
				<button
					v-if="copyable"
					class="jv-msgbtn"
					@click="emit('copy')"
					:title="copied ? 'Copied' : 'Copy'"
				>
					<svg
						v-if="copied"
						width="14"
						height="14"
						viewBox="0 0 24 24"
						fill="none"
						stroke="var(--green)"
						stroke-width="2.2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M20 6 9 17l-5-5" />
					</svg>
					<svg
						v-else
						width="14"
						height="14"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<rect x="9" y="9" width="13" height="13" rx="2" />
						<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
					</svg>
				</button>
			</div>
		</div>
	</template>
	<template v-else>
		<div class="jv-amsg" style="display: flex; gap: 12px">
			<slot name="avatar" />
			<div style="flex: 1; min-width: 0">
				<!-- support-agent identity line: name + role + time. Chat's
				     assistant passes no `sender`, so this never renders there. -->
				<div v-if="sender" class="jv-msg-who">
					<b class="jv-msg-name">{{ sender }}</b>
					<span v-if="role" class="jv-msg-role">{{ role }}</span>
					<span v-if="timestamp" class="jv-msg-when">{{ timestamp }}</span>
				</div>
				<slot name="above-body" />
				<!-- v-html body: markdown (chat, bodyClass="jv-md") or bare
				     Helpdesk HTML (support, bodyClass="jv-html"). Both style
				     blocks live in this component's scoped <style> and use
				     :deep() because v-html children carry no scope id. The base
				     font/line/color match chat's assistant body exactly. -->
				<div
					class="jv-md-body"
					:class="bodyClass"
					style="font-size: 14px; line-height: 1.6; color: var(--text)"
					v-html="html"
				></div>
				<slot name="below-body" />
				<!-- Built-in trailer (attachments + Copy bar) for consumers that
				     do NOT take over the post-body region via #below-body — i.e.
				     the standalone Support page (PR2). Chat provides #below-body
				     (its canvas loop + metabar live there, byte-identical), so
				     this stays hidden for chat and never double-renders. -->
				<template v-if="!hasBelowBody">
					<template v-for="cv in attachments || []" :key="cv.name">
						<button
							v-if="cv.type === 'image' && cv.file_url"
							class="jv-img-artifact"
							@click="emit('open-attachment', cv)"
							:title="'Open ' + cv.title"
						>
							<img :src="cv.file_url" :alt="cv.title" loading="lazy" />
						</button>
					</template>
					<div v-if="copyable" class="jv-msgbar">
						<span v-if="timestamp" class="jv-msgtime" :title="timestampFull">{{
							timestamp
						}}</span>
						<button
							class="jv-msgbtn"
							@click="emit('copy')"
							:title="copied ? 'Copied' : 'Copy'"
						>
							<svg
								v-if="copied"
								width="14"
								height="14"
								viewBox="0 0 24 24"
								fill="none"
								stroke="var(--green)"
								stroke-width="2.2"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M20 6 9 17l-5-5" />
							</svg>
							<svg
								v-else
								width="14"
								height="14"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<rect x="9" y="9" width="13" height="13" rx="2" />
								<path
									d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"
								/>
							</svg>
						</button>
					</div>
				</template>
			</div>
		</div>
	</template>
</template>

<script setup>
import { computed, useSlots } from "vue";

defineProps({
	// 'bubble' (right-aligned chat/support-customer message) | 'row'
	// (left-aligned assistant/support-agent message with identity + body).
	variant: { type: String, default: "bubble" },
	// Pre-rendered, sanitized HTML body for variant="row". Chat passes
	// `render()` + linkify output; support passes DOMPurify'd Helpdesk
	// Communication HTML (with the /files/ → proxy rewrite already applied).
	html: { type: String, default: "" },
	// Which body-style block `html` gets: 'jv-md' (markdown, chat) or
	// 'jv-html' (bare element HTML, support tickets). variant="row" only.
	bodyClass: { type: String, default: "jv-md" },
	// Plain bubble text. variant="bubble" only.
	text: { type: String, default: "" },
	// Canvas/attachment records: { name, type, title, file_url, ... }.
	attachments: { type: Array, default: () => [] },
	// Identity line for variant="row" — sender name + role (support agent).
	sender: { type: String, default: "" },
	role: { type: String, default: "" },
	// Rendered short time (hover bar) and full date/time (native title).
	timestamp: { type: String, default: "" },
	timestampFull: { type: String, default: "" },
	// Show the Edit-and-resend button (chat user bubbles only).
	editable: { type: Boolean, default: false },
	// Show the Copy button.
	copyable: { type: Boolean, default: true },
	// The just-copied tick state (parent owns the "which message" bookkeeping).
	copied: { type: Boolean, default: false },
	// A failed-to-send user message: shows "Not sent" + Retry instead of the
	// hover bar.
	failed: { type: Boolean, default: false },
});

const emit = defineEmits(["edit", "copy", "retry", "open-attachment"]);

// When a consumer supplies #below-body it OWNS the whole post-body region
// (chat does — its activity/cards/metabar live there, byte-identical), so the
// built-in attachments + Copy trailer must yield to avoid double-rendering.
const slots = useSlots();
const hasBelowBody = computed(() => !!slots["below-body"]);
</script>

<style scoped>
/* per-message Copy/Edit bar — revealed on hover. Shared by BOTH variants:
   the assistant row moved in too, and ChatView's own copies of these rules
   were deleted with it, so this is now the single definition. */
.jv-msgbar {
	display: flex;
	align-items: center;
	gap: 3px;
	margin-top: 0;
	opacity: 0;
	transition: opacity 0.12s ease;
}
.jv-umsg:hover .jv-msgbar {
	opacity: 1;
}
.jv-msgtime {
	font-size: 11.5px;
	color: var(--text-3);
	padding: 0 3px;
	cursor: default;
	user-select: none;
}
.jv-msgbtn {
	display: flex;
	align-items: center;
	justify-content: center;
	width: 26px;
	height: 26px;
	border: none;
	background: transparent;
	border-radius: 6px;
	cursor: pointer;
	color: var(--text-3);
}
.jv-msgbtn:hover {
	background: var(--surface-2);
	color: var(--text);
}
.jv-msgbtn:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* mobile layout (UX #12): inline styles win over class rules, so this
   overrides with !important, same as ChatView's other mobile overrides. */
@media (max-width: 640px) {
	.jv-ububble {
		max-width: 92% !important;
	}
}
/* touch devices can't hover, so always show per-message actions/timestamps */
@media (hover: none) {
	.jv-msgbar {
		opacity: 1 !important;
	}
}
/* artifact card (in the message) — generated/attached-image thumbnail.
   Shared with the not-yet-extracted assistant canvas loop in ChatView.vue
   (which also renders a caption span there), so only the rules this
   variant="bubble" markup uses (no caption) are duplicated here. */
.jv-img-artifact {
	display: block;
	position: relative;
	margin-top: 12px;
	padding: 0;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--surface-1);
	cursor: zoom-in;
	overflow: hidden;
	max-width: 380px;
	line-height: 0;
}
.jv-img-artifact:hover {
	border-color: var(--border-2);
}
.jv-img-artifact img {
	display: block;
	width: 100%;
	max-height: 320px;
	object-fit: cover;
}

/* ===== variant="row" identity line (support-agent name + role + time) ===== */
/* Chat's assistant renders no identity line (passes no `sender`). */
.jv-msg-who {
	display: flex;
	align-items: baseline;
	gap: 8px;
	margin-bottom: 4px;
}
.jv-msg-name {
	font-size: 13px;
	font-weight: 600;
	color: var(--text);
}
.jv-msg-role {
	font-size: 11.5px;
	color: var(--text-2);
}
.jv-msg-when {
	font-size: 11px;
	color: var(--text-3);
}

/* ===== variant="row" chrome: hover Copy bar + image artifacts ===== */
/* These style BOTH this component's own row markup AND chat's slotted
   #below-body content (its metabar Copy bar + canvas thumbnails), which carry
   the PARENT's scope id — so :deep() under the row wrapper is mandatory (a
   plain scoped rule would stamp data-v-<message> and miss the parent-scoped
   nodes). The originals were deleted from ChatView.vue; this is their only
   home now. The bubble variant keeps its own plain copies above. */
.jv-amsg :deep(.jv-msgbar) {
	display: flex;
	align-items: center;
	gap: 3px;
	margin-top: 0;
	opacity: 0;
	transition: opacity 0.12s ease;
}
.jv-amsg:hover :deep(.jv-msgbar) {
	opacity: 1;
}
.jv-amsg :deep(.jv-msgtime) {
	font-size: 11.5px;
	color: var(--text-3);
	padding: 0 3px;
	cursor: default;
	user-select: none;
}
.jv-amsg :deep(.jv-msgbtn) {
	display: flex;
	align-items: center;
	justify-content: center;
	width: 26px;
	height: 26px;
	border: none;
	background: transparent;
	border-radius: 6px;
	cursor: pointer;
	color: var(--text-3);
}
.jv-amsg :deep(.jv-msgbtn:hover) {
	background: var(--surface-2);
	color: var(--text);
}
.jv-amsg :deep(.jv-msgbtn:focus-visible) {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* touch devices can't hover, so always show the Copy bar */
@media (hover: none) {
	.jv-amsg :deep(.jv-msgbar) {
		opacity: 1 !important;
	}
}
.jv-amsg :deep(.jv-img-artifact) {
	display: block;
	position: relative;
	margin-top: 12px;
	padding: 0;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--surface-1);
	cursor: zoom-in;
	overflow: hidden;
	max-width: 380px;
	line-height: 0;
}
.jv-amsg :deep(.jv-img-artifact:hover) {
	border-color: var(--border-2);
}
.jv-amsg :deep(.jv-img-artifact img) {
	display: block;
	width: 100%;
	max-height: 320px;
	object-fit: cover;
}
.jv-amsg :deep(.jv-img-artifact-cap) {
	display: flex;
	align-items: center;
	gap: 6px;
	padding: 7px 10px;
	font-family: inherit;
	font-size: 11.5px;
	line-height: 1.3;
	color: var(--text-3);
	background: var(--surface);
	border-top: 1px solid var(--border);
}

/* ===== variant="row" body: markdown (bodyClass="jv-md") ===== */
/* Moved verbatim from ChatView.vue's scoped <style> (the assistant markdown
   body), rewriting the prefix `.jv-md` → `.jv-md-body.jv-md` so it applies
   only to a markdown body. The body is v-html, so its children carry no scope
   id → :deep() is mandatory (a plain scoped selector would match nothing).
   Narrow-window resilience: min-width:0 lets the flex child shrink; wide
   content (tables, code) scrolls INSIDE its own box. */
.jv-md-body.jv-md {
	min-width: 0;
	max-width: 100%;
	overflow-wrap: anywhere;
}
.jv-md-body.jv-md :deep(table) {
	display: block;
	max-width: 100%;
	overflow-x: auto;
	border-collapse: collapse;
}
.jv-md-body.jv-md :deep(pre) {
	max-width: 100%;
	overflow-x: auto;
}
.jv-md-body.jv-md :deep(img) {
	max-width: 100%;
	height: auto;
}
.jv-md-body.jv-md :deep(.jv-mermaid) {
	position: relative;
	margin: 8px 0 12px;
	text-align: center;
	overflow-x: auto;
}
.jv-md-body.jv-md :deep(.jv-mermaid svg) {
	max-width: 100%;
	height: auto;
}
/* skeleton shimmer while a chart hasn't rendered to SVG yet (no data-rendered) —
   hides the raw mermaid source so the user never sees the markup flash. */
.jv-md-body.jv-md :deep(.jv-mermaid:not([data-rendered])) {
	min-height: 196px;
	color: transparent !important;
	user-select: none;
	overflow: hidden;
	border-radius: 10px;
	border: 1px solid var(--border);
	background: var(--surface-1);
}
.jv-md-body.jv-md :deep(.jv-mermaid:not([data-rendered])) * {
	color: transparent !important;
}
.jv-md-body.jv-md :deep(.jv-mermaid:not([data-rendered]))::after {
	content: "";
	position: absolute;
	inset: 0;
	background: linear-gradient(100deg, transparent 20%, var(--surface-2) 50%, transparent 80%);
	background-size: 220% 100%;
	animation: jv-shimmer 1.25s ease-in-out infinite;
}
/* jv-shimmer keyframe moved here with its only consumer (the mermaid skeleton
   ::after above) — Vue scopes @keyframes names, so the keyframe and the
   `animation:` that references it MUST share one scoped block. */
@keyframes jv-shimmer {
	0% {
		background-position: 180% 0;
	}
	100% {
		background-position: -180% 0;
	}
}
.jv-md-body.jv-md :deep(.jv-chart-dl) {
	position: absolute;
	top: 6px;
	right: 6px;
	width: 26px;
	height: 26px;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	padding: 0;
	background: var(--surface);
	color: var(--text-3);
	border: 1px solid var(--border);
	border-radius: 6px;
	cursor: pointer;
	opacity: 0;
	transition: opacity 0.12s, color 0.12s, background 0.12s;
}
.jv-md-body.jv-md :deep(.jv-mermaid:hover .jv-chart-dl) {
	opacity: 1;
}
.jv-md-body.jv-md :deep(.jv-chart-dl:hover) {
	color: var(--text);
	background: var(--surface-1);
	border-color: var(--border-2);
}
.jv-md-body.jv-md :deep(.jv-md-pre) {
	margin: 6px 0 12px;
	padding: 12px 14px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 8px;
	overflow-x: auto;
}
.jv-md-body.jv-md :deep(.jv-md-pre code) {
	font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	font-size: 12px;
	color: var(--text);
	white-space: pre;
}
/* markdown content → the imported design's table look */
.jv-md-body.jv-md :deep(.jv-md-p) {
	margin: 0 0 10px;
}
.jv-md-body.jv-md :deep(.jv-md-p:last-child) {
	margin-bottom: 0;
}
.jv-md-body.jv-md :deep(.jv-md-h) {
	margin: 14px 0 6px;
	font-weight: 600;
	color: var(--text);
}
.jv-md-body.jv-md :deep(h3.jv-md-h) {
	font-size: 15px;
}
.jv-md-body.jv-md :deep(h4.jv-md-h) {
	font-size: 14px;
}
.jv-md-body.jv-md :deep(h5.jv-md-h),
.jv-md-body.jv-md :deep(h6.jv-md-h) {
	font-size: 13px;
}
.jv-md-body.jv-md :deep(.jv-md-h:first-child) {
	margin-top: 0;
}
.jv-md-body.jv-md :deep(.jv-md-list) {
	margin: 0 0 10px;
	padding-left: 20px;
}
.jv-md-body.jv-md :deep(.jv-md-list li) {
	margin: 2px 0;
}
.jv-md-body.jv-md :deep(.jv-md-code) {
	background: var(--surface-2);
	padding: 1px 5px;
	border-radius: 4px;
	font-size: 12px;
	font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	overflow-wrap: anywhere;
}
.jv-md-body.jv-md :deep(.jv-md-list .jv-md-list) {
	margin: 2px 0;
}
.jv-md-body.jv-md :deep(.jv-md-quote) {
	margin: 0 0 10px;
	padding: 2px 0 2px 12px;
	border-left: 3px solid var(--border-2);
	color: var(--text-2);
}
.jv-md-body.jv-md :deep(del) {
	opacity: 0.65;
}
.jv-md-body.jv-md :deep(.jv-md-link) {
	color: var(--cta);
	text-decoration: none;
	font-weight: 500;
}
/* Auto-linked document IDs → open the record in ERPNext Desk. Dashed underline
   marks them as record links, distinct from plain markdown links. */
.jv-md-body.jv-md :deep(.jv-doclink) {
	color: var(--cta);
	text-decoration: none;
	font-weight: 550;
	border-bottom: 1px dashed var(--cta);
	cursor: pointer;
	transition: background 0.12s;
}
.jv-md-body.jv-md :deep(.jv-doclink:hover) {
	border-bottom-style: solid;
	background: var(--cta-bg);
	border-radius: 3px;
}
.jv-md-body.jv-md :deep(.jv-md-tablewrap) {
	border: 1px solid var(--border);
	border-radius: 10px;
	overflow: hidden;
	margin: 4px 0 10px;
}
.jv-md-body.jv-md :deep(.jv-md-table) {
	width: 100%;
	border-collapse: collapse;
	font-size: 12.5px;
}
.jv-md-body.jv-md :deep(.jv-md-table th) {
	padding: 8px 13px;
	font-weight: 550;
	color: var(--text-3);
	background: var(--surface-1);
	border-bottom: 1px solid var(--border);
}
.jv-md-body.jv-md :deep(.jv-md-table td) {
	padding: 9px 13px;
	border-bottom: 1px solid var(--border);
	color: var(--text);
	font-variant-numeric: tabular-nums;
}
.jv-md-body.jv-md :deep(.jv-md-table tr:last-child td) {
	border-bottom: 0;
}
/* honor reduced-motion on the markdown body (moved from ChatView's shared
   reduced-motion block). */
@media (prefers-reduced-motion: reduce) {
	.jv-md-body.jv-md :deep(.jv-mermaid:not([data-rendered]))::after {
		animation: none;
	}
}

/* ===== variant="row" body: bare Helpdesk HTML (bodyClass="jv-html") ===== */
/* Support ticket bodies are raw email HTML, NOT run through the markdown
   pipeline, so they have none of the jv-md-* classes. Tailwind's preflight
   strips element defaults (list bullets, link underline, table borders), so
   restore them at the element level here. */
/* Mirrors the .jv-md base rule deliberately: email HTML carries long unbroken
   strings (tracking URLs, order ids) that would otherwise push the row wide. */
.jv-md-body.jv-html {
	min-width: 0;
	max-width: 100%;
	overflow-wrap: anywhere;
}
.jv-md-body.jv-html :deep(p) {
	margin: 0 0 10px;
}
.jv-md-body.jv-html :deep(ul),
.jv-md-body.jv-html :deep(ol) {
	margin: 0 0 10px;
	padding-left: 22px;
	list-style: revert;
}
.jv-md-body.jv-html :deep(li) {
	margin: 2px 0;
}
.jv-md-body.jv-html :deep(a) {
	color: var(--link);
	text-decoration: underline;
}
/* display:block is load-bearing, not cosmetic: overflow does not scroll a
   display:table box, so without it a wide email table blows out the row
   instead of scrolling inside it. Same reason the .jv-md twin carries it. */
.jv-md-body.jv-html :deep(table) {
	display: block;
	max-width: 100%;
	overflow-x: auto;
	border-collapse: collapse;
}
.jv-md-body.jv-html :deep(th),
.jv-md-body.jv-html :deep(td) {
	padding: 8px 12px;
	border: 1px solid var(--border);
}
.jv-md-body.jv-html :deep(blockquote) {
	margin: 0 0 10px;
	padding: 2px 0 2px 12px;
	border-left: 3px solid var(--border-2);
	color: var(--text-2);
}
.jv-md-body.jv-html :deep(img) {
	max-width: 100%;
	height: auto;
}
.jv-md-body.jv-html :deep(pre) {
	overflow: auto;
}
</style>
