<template>
	<div class="jv-ask" :class="{ 'jv-ask--form': isForm }">
		<div v-for="(q, qi) in spec.questions" :key="qi" class="jv-ask-q">
			<div class="jv-ask-qt">
				<span class="jv-ask-num">{{ qi + 1 }}</span
				>{{ q.q }}
			</div>
			<!-- yes/no, with optional custom labels (e.g. Approve / Reject) -->
			<div v-if="q.type === 'yesno'" class="jv-ask-opts">
				<button
					v-for="(lbl, li) in q.options.length === 2 ? q.options : ['Yes', 'No']"
					:key="li"
					class="jv-ask-opt"
					:class="{ on: isPicked(qi, lbl) }"
					@click="toggleSingle(qi, lbl)"
				>
					<span v-if="isPicked(qi, lbl)" class="jv-ask-tick">✓</span>{{ lbl }}
				</button>
			</div>
			<!-- single / multi choice -->
			<div v-else-if="q.type === 'single' || q.type === 'multi'" class="jv-ask-opts">
				<button
					v-for="(opt, oi) in q.options"
					:key="oi"
					class="jv-ask-opt"
					:class="{ on: isPicked(qi, opt) }"
					@click="q.type === 'multi' ? toggleMulti(qi, opt) : toggleSingle(qi, opt)"
				>
					<span v-if="isPicked(qi, opt)" class="jv-ask-tick">✓</span>{{ opt }}
				</button>
			</div>
			<!-- date / datetime / free text fields -->
			<input
				v-else-if="q.type === 'date'"
				type="date"
				class="jv-ask-field"
				:value="sel[qi] || ''"
				@input="pickSingle(qi, $event.target.value)"
			/>
			<input
				v-else-if="q.type === 'datetime'"
				type="datetime-local"
				class="jv-ask-field"
				:value="sel[qi] || ''"
				@input="pickSingle(qi, $event.target.value)"
			/>
			<input
				v-else-if="q.type === 'text'"
				type="text"
				class="jv-ask-field"
				:value="sel[qi] || ''"
				@input="pickSingle(qi, $event.target.value)"
				placeholder="Type your answer…"
				@keydown.enter.prevent
			/>
			<!-- link: search a record of the given DocType -->
			<div v-else-if="q.type === 'link'" class="jv-ask-link">
				<input
					type="text"
					class="jv-ask-field"
					:value="link[qi] && link[qi].q != null ? link[qi].q : sel[qi] || ''"
					@input="onLinkSearch(qi, q.doctype, $event.target.value)"
					@focus="onLinkSearch(qi, q.doctype, (link[qi] && link[qi].q) || '')"
					:placeholder="'Search ' + (q.doctype || 'records') + '…'"
					@blur="closeLink(qi)"
					@keydown.enter.prevent
				/>
				<div
					v-if="link[qi] && link[qi].open && (link[qi].items || []).length"
					class="jv-ask-linkmenu"
				>
					<button
						v-for="(it, ii) in link[qi].items"
						:key="ii"
						@mousedown.prevent="pickLink(qi, it)"
					>
						<b>{{ it.value }}</b
						><span v-if="it.label"> · {{ it.label }}</span>
					</button>
				</div>
			</div>
			<!-- Other free-text only for choice questions -->
			<input
				v-if="q.type === 'single' || q.type === 'multi' || q.type === 'yesno'"
				class="jv-ask-other"
				v-model="other[qi]"
				placeholder="Other…"
				@input="onOther(qi, q.type)"
				@keydown.enter.prevent
			/>
		</div>
		<div class="jv-ask-foot">
			<button class="jv-ask-submit" :disabled="!ready" @click="submit">
				Submit answers
			</button>
			<span v-if="!ready" class="jv-ask-hint">Answer each question to continue</span>
		</div>
	</div>
</template>

<script setup>
// AskCard - the ONE renderer for the agent's ```jarvis-ask blocks: option cards
// (or a compact mini-form when every question is a field type) plus a single
// "Submit answers" button that emits the answers as the next user message.
//
// Extracted from ChatView so the Dashboards builder pane renders identical
// cards instead of stripping the block into an empty bubble. The parsing /
// readiness / answer-formatting rules live in @/lib/chatAsk, shared with any
// caller; this component owns only the draft state and the interaction.
//
// Styling is the jv-* design tokens (var(--surface-2), var(--cta), …). Those
// are NOT global: the host must sit inside a subtree that binds
// useJarvisTheme().paletteVars (ChatView's root does; the dashboards pane binds
// them on its wrapper, the SkillDetail precedent).
//
// HOSTS MUST KEY THIS BY MESSAGE (`:key="m.name"`). The draft lives in this
// component, and `spec` is typically a computed that re-parses (new object
// identity) on every stream tick — resetting on a `spec` watcher would wipe
// half-made picks. Remounting on a new message is what clears the draft.
import { ref, computed } from "vue";
import { searchLink } from "@/api";
import { ASK_FIELD_TYPES, isAskReady, askAnswerText } from "@/lib/chatAsk";

const props = defineProps({
	// A parsed ask: { questions: [{q, type, options, doctype}] } (see parseAsk).
	spec: { type: Object, required: true },
});
// The formatted answer text; the host sends it as an ordinary user message.
const emit = defineEmits(["submit"]);

const sel = ref({}); // qIdx -> string (single/yesno/date/datetime/text/link) | string[] (multi)
const other = ref({}); // qIdx -> free-text (option types only)
const link = ref({}); // qIdx -> { q, items, open } for link-type record search

// An ask whose questions are ALL field-type reads better as a compact mini-form
// (no numbered badges, no dividers) than as a numbered question list.
const isForm = computed(
	() =>
		props.spec.questions.length > 0 &&
		props.spec.questions.every((q) => ASK_FIELD_TYPES.includes(q.type))
);
const ready = computed(() => isAskReady(props.spec, sel.value, other.value));

async function onLinkSearch(i, doctype, val) {
	link.value = { ...link.value, [i]: { ...(link.value[i] || {}), q: val, open: true } };
	if (!doctype) return;
	try {
		const r = await searchLink(doctype, val);
		const items = (r || [])
			.map((x) => ({ value: x.value, label: x.description || "" }))
			.slice(0, 8);
		link.value = { ...link.value, [i]: { q: val, items, open: true } };
	} catch (e) {
		link.value = { ...link.value, [i]: { q: val, items: [], open: true } };
	}
}
function pickLink(i, item) {
	sel.value = { ...sel.value, [i]: item.value };
	link.value = { ...link.value, [i]: { q: item.value, items: [], open: false } };
}
// Hide the record dropdown when the field loses focus (clicking elsewhere on
// the screen / tabbing away). @mousedown.prevent on the result buttons lets a
// pick land before the blur fires.
function closeLink(i) {
	const cur = link.value[i];
	if (cur && cur.open) link.value = { ...link.value, [i]: { ...cur, open: false } };
}
function pickSingle(i, opt) {
	sel.value = { ...sel.value, [i]: opt };
}
// Option BUTTONS (single/yesno) toggle: clicking the picked option again
// unselects it, and picking one clears the "Other…" text (they're exclusive —
// both being sent as the answer was a reported bug).
function toggleSingle(i, opt) {
	const cur = sel.value[i];
	sel.value = { ...sel.value, [i]: cur === opt ? "" : opt };
	if (cur !== opt && (other.value[i] || "").trim()) {
		other.value = { ...other.value, [i]: "" };
	}
}
// Typing in "Other…" clears a picked option for single/yesno (mirror of the above).
function onOther(i, qtype) {
	if (qtype !== "multi" && (other.value[i] || "").trim() && sel.value[i]) {
		sel.value = { ...sel.value, [i]: "" };
	}
}
function toggleMulti(i, opt) {
	const cur = Array.isArray(sel.value[i]) ? sel.value[i].slice() : [];
	const ix = cur.indexOf(opt);
	if (ix >= 0) cur.splice(ix, 1);
	else cur.push(opt);
	sel.value = { ...sel.value, [i]: cur };
}
function isPicked(i, opt) {
	const v = sel.value[i];
	return Array.isArray(v) ? v.includes(opt) : v === opt;
}
function submit() {
	if (!ready.value) return;
	// Emit and keep the picks. The card is keyed by message, so it unmounts the
	// moment the next assistant message arrives — there is nothing to reset. And
	// a host is allowed to REFUSE the text (a send already in flight, a dictation
	// still transcribing): clearing first would silently swallow the answers and
	// leave the user staring at an empty, disabled card.
	emit("submit", askAnswerText(props.spec, sel.value, other.value));
}
</script>

<style scoped>
/* interactive clarifying-question cards */
.jv-ask {
	margin-top: 12px;
	padding: 14px;
	border: 1px solid var(--border);
	background: var(--surface-1);
	border-radius: 12px;
}
.jv-ask-q {
	padding-bottom: 13px;
	margin-bottom: 13px;
	border-bottom: 1px solid var(--border);
}
.jv-ask-q:last-of-type {
	border-bottom: 0;
	padding-bottom: 4px;
	margin-bottom: 4px;
}
.jv-ask-qt {
	display: flex;
	align-items: flex-start;
	gap: 8px;
	font-size: 13.5px;
	font-weight: 600;
	color: var(--text);
	margin-bottom: 9px;
	line-height: 1.4;
}
.jv-ask-num {
	flex: none;
	width: 19px;
	height: 19px;
	border-radius: 99px;
	background: var(--cta-bg);
	color: var(--cta);
	font-size: 11px;
	font-weight: 700;
	display: flex;
	align-items: center;
	justify-content: center;
	margin-top: 1px;
}
.jv-ask--form .jv-ask-num {
	display: none;
}
.jv-ask--form .jv-ask-q {
	border-bottom: 0;
	padding-bottom: 11px;
	margin-bottom: 11px;
}
.jv-ask--form .jv-ask-q:last-of-type {
	padding-bottom: 0;
	margin-bottom: 0;
}
.jv-ask--form .jv-ask-qt {
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
	margin-bottom: 6px;
}
.jv-ask-opts {
	display: flex;
	flex-wrap: wrap;
	gap: 7px;
}
.jv-ask-opt {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	padding: 7px 12px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 9px;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 500;
	color: var(--text-2);
	cursor: pointer;
	transition: border-color 0.12s, background 0.12s, color 0.12s;
}
.jv-ask-opt:hover {
	border-color: var(--border-2);
	color: var(--text);
}
.jv-ask-opt.on {
	border-color: var(--cta);
	background: var(--cta-bg);
	color: var(--text);
	font-weight: 600;
}
.jv-ask-tick {
	color: var(--cta);
	font-weight: 700;
	font-size: 11px;
}
.jv-ask-field {
	width: 100%;
	box-sizing: border-box;
	padding: 8px 10px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 8px;
	font-family: inherit;
	font-size: 13px;
	color: var(--text);
	outline: none;
}
.jv-ask-field:focus {
	border-color: var(--cta);
}
.jv-ask-link {
	position: relative;
}
.jv-ask-linkmenu {
	position: absolute;
	left: 0;
	right: 0;
	top: calc(100% + 4px);
	z-index: 20;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 9px;
	box-shadow: 0 8px 24px rgba(20, 20, 30, 0.14);
	padding: 4px;
	max-height: 220px;
	overflow-y: auto;
}
.jv-ask-linkmenu button {
	display: block;
	width: 100%;
	text-align: left;
	padding: 7px 9px;
	background: transparent;
	border: none;
	border-radius: 6px;
	font-family: inherit;
	font-size: 12.5px;
	color: var(--text-2);
	cursor: pointer;
	white-space: normal;
	overflow-wrap: anywhere;
}
.jv-ask-linkmenu button:hover {
	background: var(--surface-2);
	color: var(--text);
}
.jv-ask-other {
	width: 100%;
	box-sizing: border-box;
	margin-top: 8px;
	padding: 7px 10px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 8px;
	font-family: inherit;
	font-size: 12.5px;
	color: var(--text);
	outline: none;
}
.jv-ask-other:focus {
	border-color: var(--cta);
}
.jv-ask-foot {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 10px;
	margin-top: 14px;
}
.jv-ask-submit {
	padding: 8px 16px;
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 8px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 600;
	color: var(--cta-fg);
	cursor: pointer;
	transition: opacity 0.12s;
}
.jv-ask-submit:hover {
	opacity: 0.9;
}
.jv-ask-submit:disabled {
	opacity: 0.45;
	cursor: default;
}
.jv-ask-hint {
	font-size: 11.5px;
	color: var(--text-3);
}
</style>
