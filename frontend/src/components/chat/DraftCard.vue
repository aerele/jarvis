<script setup>
// The one card both apps draw for a draft the server has stored. It only draws
// draftCardView(card, ctx): no API calls, no router, no store. The host maps the four events.
// Plain elements and its own --dc-* tokens, so it needs nothing from either app's stylesheet
// (the desktop and the phone name their colours differently). frappe-ui is not imported on
// purpose: a file under frontend/ would resolve it from frontend/node_modules even in the phone
// build, which can load a second copy; ExportCard, the other shared card, does the same.
import { computed } from "vue";
import { draftCardView } from "../../lib/draftCardView.js";

const props = defineProps({
	card: { type: Object, required: true },
	ctx: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["create", "preview", "edit", "cancel"]);

const view = computed(() => draftCardView(props.card, props.ctx));
// "Save changes" is the primary of an update: the host confirms it exactly like Create.
const EVENT = {
	create: "create",
	save: "create",
	preview: "preview",
	edit: "edit",
	cancel: "cancel",
};

function press(button) {
	if (button.disabled || button.loading) return;
	emit(EVENT[button.id]);
}
</script>

<template>
	<section
		class="dc"
		:class="{ 'dc-sensitive': view.state === 'S11' }"
		:data-state="view.state"
		:aria-label="view.title || undefined"
	>
		<p v-if="view.message" class="dc-message" role="status">{{ view.message }}</p>
		<template v-else>
			<header class="dc-head">
				<h3 class="dc-title">{{ view.title }}</h3>
				<span v-if="view.tag" class="dc-tag">{{ view.tag }}</span>
			</header>
			<p v-if="view.summary" class="dc-summary">{{ view.summary }}</p>

			<dl v-if="view.rows.length" class="dc-fields">
				<div v-for="(r, i) in view.rows" :key="'r' + i" class="dc-field">
					<dt>{{ r.label }}</dt>
					<dd>{{ r.value }}</dd>
				</div>
			</dl>

			<ul v-if="view.diff.length" class="dc-diff" aria-label="Changes">
				<li v-for="(d, i) in view.diff" :key="'d' + i">
					<span class="dc-diff-label">{{ d.label }}</span>
					<span class="dc-old">{{ d.old }}</span>
					<span class="dc-arrow" aria-hidden="true">&rarr;</span>
					<span class="dc-new">{{ d.new }}</span>
				</li>
			</ul>

			<div v-for="(t, ti) in view.tables" :key="'t' + ti" class="dc-table">
				<div class="dc-scroll">
					<table>
						<caption>
							{{
								t.label
							}}
						</caption>
						<thead v-if="t.columns.length">
							<tr>
								<th v-for="(c, ci) in t.columns" :key="'c' + ci" scope="col">
									{{ c }}
								</th>
							</tr>
						</thead>
						<tbody>
							<tr v-for="(r, ri) in t.rows" :key="'tr' + ri">
								<td v-for="(c, ci) in r" :key="'td' + ci">{{ c }}</td>
							</tr>
						</tbody>
					</table>
				</div>
				<p v-if="t.more" class="dc-more">+{{ t.more }} more</p>
			</div>

			<p v-if="view.ignoredNote" class="dc-note">{{ view.ignoredNote }}</p>
		</template>

		<div class="dc-buttons">
			<button
				v-for="b in view.buttons"
				:key="b.id"
				type="button"
				class="dc-btn"
				:class="{ 'dc-primary': b.primary, 'dc-loading': b.loading }"
				:disabled="b.disabled"
				:aria-busy="b.loading ? 'true' : undefined"
				@click="press(b)"
			>
				<span v-if="b.loading" class="dc-spin" aria-hidden="true"></span>
				{{ b.label }}
			</button>
		</div>
		<!-- Always one line tall, so the card does not change height between states. -->
		<p class="dc-foot" aria-live="polite">{{ view.footer || view.hint || " " }}</p>
	</section>
</template>

<style scoped>
/* Own tokens. First fallback: the desktop's; second: the phone's; last: a neutral literal.
   A host that wants other colours sets --dc-* on the card or an ancestor. */
.dc {
	--dc-bg: var(--surface, var(--card, #ffffff));
	--dc-bg-soft: var(--surface-1, var(--card2, #f7f7f8));
	--dc-border: var(--border-2, var(--border2, #e2e2e6));
	--dc-text: var(--text, var(--ink9, #171717));
	--dc-muted: var(--text-2, var(--ink7, #4a4a4f));
	--dc-primary-bg: var(--cta, var(--inv-bg, #171717));
	--dc-primary-fg: var(--cta-fg, var(--inv-ink, #ffffff));
	--dc-focus: var(--link, var(--accent, #2563eb));
	--dc-danger: var(--red, #dc2626);
	--dc-ok: var(--green, #16a34a);
	--dc-warn: var(--amber, #d97706);

	margin: 12px 0;
	padding: 14px;
	border: 1px solid var(--dc-border);
	border-radius: 12px;
	background: var(--dc-bg);
	color: var(--dc-text);
	font-size: 14px;
	line-height: 1.45;
	overflow-wrap: anywhere;
	min-width: 0;
}
/* Sensitive keeps today's warning look; every other state is calm. */
.dc-sensitive {
	border-color: color-mix(in srgb, var(--dc-warn) 45%, var(--dc-border));
}
.dc-head {
	display: flex;
	align-items: center;
	gap: 8px;
}
.dc-title {
	margin: 0;
	font-size: 15px;
	font-weight: 600;
	min-width: 0;
}
.dc-tag {
	flex: 0 0 auto;
	font-size: 11.5px;
	padding: 1px 8px;
	border: 1px solid var(--dc-border);
	border-radius: 999px;
	background: var(--dc-bg-soft);
	color: var(--dc-muted);
}
.dc-summary,
.dc-message {
	margin: 2px 0 0;
	color: var(--dc-muted);
}
.dc-message {
	color: var(--dc-text);
}
.dc-fields {
	display: grid;
	gap: 6px;
	margin: 12px 0 0;
}
.dc-field {
	display: grid;
	grid-template-columns: minmax(80px, 32%) minmax(0, 1fr);
	gap: 10px;
}
.dc-field dt {
	color: var(--dc-muted);
}
.dc-field dd {
	margin: 0;
}
.dc-diff {
	list-style: none;
	margin: 12px 0 0;
	padding: 0;
	display: grid;
	gap: 6px;
}
.dc-diff li {
	display: flex;
	flex-wrap: wrap;
	gap: 4px 8px;
	align-items: baseline;
}
.dc-diff-label {
	color: var(--dc-muted);
}
.dc-old {
	color: var(--dc-muted);
	text-decoration: line-through;
}
.dc-table {
	margin-top: 12px;
}
.dc-scroll {
	overflow-x: auto;
}
.dc table {
	border-collapse: collapse;
	width: 100%;
	font-size: 12.5px;
}
.dc caption {
	text-align: left;
	font-size: 12px;
	color: var(--dc-muted);
	padding-bottom: 4px;
}
.dc th,
.dc td {
	text-align: left;
	padding: 4px 8px;
	border-bottom: 1px solid var(--dc-border);
	overflow-wrap: anywhere;
}
.dc th {
	color: var(--dc-muted);
	font-weight: 500;
}
.dc td {
	font-variant-numeric: tabular-nums;
}
.dc-more {
	margin: 4px 0 0;
	font-size: 12px;
	color: var(--dc-muted);
}
.dc-note {
	margin: 12px 0 0;
	font-size: 12.5px;
	color: var(--dc-muted);
}
.dc-buttons {
	display: flex;
	flex-wrap: wrap;
	gap: 8px;
	margin-top: 14px;
}
.dc-btn {
	font: inherit;
	font-size: 13px;
	font-weight: 500;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: 6px;
	min-height: 34px;
	padding: 6px 14px;
	border-radius: 8px;
	border: 1px solid var(--dc-border);
	background: var(--dc-bg-soft);
	color: var(--dc-text);
	cursor: pointer;
}
.dc-primary {
	background: var(--dc-primary-bg);
	color: var(--dc-primary-fg);
	border-color: var(--dc-primary-bg);
	font-weight: 600;
}
.dc-btn:disabled {
	opacity: 0.55;
	cursor: not-allowed;
}
.dc-btn.dc-loading:disabled {
	cursor: progress;
}
.dc-btn:focus-visible {
	outline: 2px solid var(--dc-focus);
	outline-offset: 2px;
}
.dc-spin {
	width: 12px;
	height: 12px;
	border: 2px solid currentColor;
	border-right-color: transparent;
	border-radius: 50%;
	animation: dc-spin 0.8s linear infinite;
}
@keyframes dc-spin {
	to {
		transform: rotate(360deg);
	}
}
@media (prefers-reduced-motion: reduce) {
	.dc-spin {
		animation-duration: 2.4s;
	}
}
.dc-foot {
	margin: 8px 0 0;
	min-height: 1.45em;
	font-size: 12.5px;
	color: var(--dc-muted);
}
@media (max-width: 420px) {
	.dc-field {
		grid-template-columns: minmax(0, 1fr);
		gap: 0;
	}
	.dc-btn {
		flex: 1 1 auto;
		min-height: 40px;
	}
}
</style>
