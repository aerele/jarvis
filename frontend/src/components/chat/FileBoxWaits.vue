<template>
	<section class="jv-fbw" aria-label="Waiting on you">
		<div class="jv-fbw-head">
			<span class="jv-fbw-title">Waiting on you</span>
			<span class="jv-fbw-sub">Also on the Approval Board</span>
		</div>
		<div v-for="it in items" :key="it.name" class="jv-fbw-item">
			<template v-if="it.src === 'ar'">
				<div class="jv-fbw-q">{{ it.question || it.title }}</div>
				<div
					v-if="it.options && it.options.length"
					class="jv-fbw-opts"
					role="group"
					:aria-label="it.question || it.title"
				>
					<button
						v-for="opt in it.options"
						:key="opt"
						type="button"
						class="jv-fbw-opt"
						:class="{ on: picked[it.name] === opt }"
						:aria-pressed="picked[it.name] === opt"
						:disabled="busy === it.name"
						@click="pick(it.name, opt)"
					>
						{{ opt }}
					</button>
				</div>
				<textarea
					v-model="notes[it.name]"
					class="jv-fbw-field"
					rows="2"
					:aria-label="it.options && it.options.length ? 'Note' : 'Your answer'"
					:placeholder="
						it.options && it.options.length
							? 'Add a note (optional)'
							: 'Type your answer'
					"
					:disabled="busy === it.name"
				/>
				<div class="jv-fbw-foot">
					<button
						type="button"
						class="jv-fbw-send"
						:disabled="!answerOf(it) || busy === it.name"
						@click="emit('decide', it, answerOf(it), 1)"
					>
						Send answer
					</button>
					<button
						type="button"
						class="jv-fbw-reject"
						:disabled="busy === it.name"
						@click="emit('decide', it, rejectionOf(it), 0)"
					>
						Reject
					</button>
					<router-link class="jv-fbw-link" :to="it.link"
						>Open on Approval Board</router-link
					>
				</div>
			</template>
			<div v-else class="jv-fbw-held">
				<span class="jv-fbw-heldtext">{{ heldLabel(it) }}</span>
				<router-link class="jv-fbw-link" :to="it.link"
					>Review on Approval Board</router-link
				>
			</div>
		</div>
	</section>
</template>

<script setup>
import { reactive } from "vue";

// A File Box file's open items, shown in that file's chat (the Approval Board lists
// the same ones): a question is answered here exactly as on the board; a held record
// or an approval sheet needs the board's fuller review, so it links there.
defineProps({
	items: { type: Array, required: true },
	busy: { type: String, default: "" }, // the item being decided
});
const emit = defineEmits(["decide"]);

const picked = reactive({});
const notes = reactive({});

function pick(name, opt) {
	picked[name] = picked[name] === opt ? "" : opt;
}
// The board's rules: an answer is the picked option, else the note; a rejection
// sends the note, else "Rejected".
function answerOf(it) {
	return picked[it.name] || (notes[it.name] || "").trim();
}
function rejectionOf(it) {
	return (notes[it.name] || "").trim() || "Rejected";
}
function heldLabel(it) {
	const what = it.src === "sheet" ? "Approval sheet" : "Waiting for approval";
	return it.title ? `${what}: ${it.title}` : what;
}
</script>

<style scoped>
.jv-fbw {
	margin: 12px 0;
	padding: 14px;
	border: 1px solid var(--border);
	background: var(--surface-1);
	border-radius: 12px;
}
.jv-fbw-head {
	display: flex;
	flex-wrap: wrap;
	align-items: baseline;
	gap: 8px;
	margin-bottom: 10px;
}
.jv-fbw-title {
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
}
.jv-fbw-sub {
	font-size: 11.5px;
	color: var(--text-3);
}
.jv-fbw-item {
	padding-bottom: 13px;
	margin-bottom: 13px;
	border-bottom: 1px solid var(--border);
}
.jv-fbw-item:last-of-type {
	border-bottom: 0;
	padding-bottom: 0;
	margin-bottom: 0;
}
.jv-fbw-q {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--text);
	margin-bottom: 9px;
	line-height: 1.4;
	overflow-wrap: anywhere;
}
.jv-fbw-opts {
	display: flex;
	flex-wrap: wrap;
	gap: 7px;
	margin-bottom: 9px;
}
.jv-fbw-opt {
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
.jv-fbw-opt:hover {
	border-color: var(--border-2);
	color: var(--text);
}
.jv-fbw-opt.on {
	border-color: var(--cta);
	background: var(--cta-bg);
	color: var(--text);
	font-weight: 600;
}
.jv-fbw-field {
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
	resize: vertical;
}
.jv-fbw-field:focus {
	border-color: var(--cta);
}
.jv-fbw-foot {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 10px;
	margin-top: 10px;
}
.jv-fbw-send {
	padding: 8px 16px;
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 8px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 600;
	color: var(--cta-fg);
	cursor: pointer;
}
.jv-fbw-reject {
	padding: 8px 14px;
	background: transparent;
	border: 1px solid var(--border);
	border-radius: 8px;
	font-family: inherit;
	font-size: 13px;
	color: var(--text-2);
	cursor: pointer;
}
.jv-fbw-send:disabled,
.jv-fbw-reject:disabled,
.jv-fbw-opt:disabled,
.jv-fbw-field:disabled {
	opacity: 0.5;
	cursor: default;
}
.jv-fbw-send:focus-visible,
.jv-fbw-reject:focus-visible,
.jv-fbw-opt:focus-visible,
.jv-fbw-link:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
.jv-fbw-link {
	font-size: 12.5px;
	color: var(--cta);
	text-decoration: none;
}
.jv-fbw-link:hover {
	text-decoration: underline;
}
.jv-fbw-held {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 10px;
	font-size: 13px;
	color: var(--text);
}
.jv-fbw-heldtext {
	overflow-wrap: anywhere;
}
</style>
