<script setup>
import { computed, ref } from "vue";
import * as api from "../api";
import { agentName } from "@/branding";

// The agent proposes a document; a human applies it.
//
// create/update never go through a tool — the agent emits a ```jarvis-action```
// card and stops. So this card IS the write path, and without it the phone could
// read everything and change nothing.
//
// Read-only on purpose: the desktop opens a full editable draft panel, which is
// a form builder's job, not a phone's. If a value is wrong the right move is to
// tell Jarvis, not to hand-edit a form with a thumb — so the card offers Apply,
// Cancel, and the truth about what it is going to do.
const props = defineProps({
	action: { type: Object, required: true },
	conversation: { type: String, required: true },
});
const emit = defineEmits(["applied", "dismissed"]);

const state = ref("review"); // review | busy | done
const error = ref("");
const applied = ref(null);

const isEmail = computed(() => props.action.kind === "email");
const verb = computed(() => String(props.action.verb || "").toLowerCase());
const isWrite = computed(() => verb.value === "create" || verb.value === "update");
const heading = computed(
	() =>
		props.action.title ||
		`${verb.value === "create" ? "Create" : "Update"} ${props.action.doctype || "record"}`
);

// No `tables` escape here, unlike the desktop: this card neither renders nor
// applies child tables, so a block with no `fields` has nothing to show.
const invalid = computed(() => {
	if (!isWrite.value) return "";
	if (Array.isArray(props.action.docs))
		return `This draft carries a \`docs\` batch, which is a create_doc payload rather than a card. Ask ${agentName} to apply them as a batch.`;
	if (verb.value === "create" && !(props.action.fields || []).length)
		return "This draft has no fields to show.";
	return "";
});

async function apply() {
	if (state.value === "busy") return;
	state.value = "busy";
	error.value = "";
	try {
		// The card names fields by LABEL; the write wants fieldnames. Ask the
		// server for the form meta and map — guessing (lower-casing and swapping
		// spaces for underscores) is right until it isn't, and then it silently
		// writes the wrong field.
		const meta = await api.getDoctypeFormMeta(props.action.doctype);
		if (meta?.ok === false) {
			error.value = meta.reason || "That document type isn't available to you.";
			state.value = "review";
			return;
		}
		const byLabel = new Map();
		for (const f of meta.fields || []) {
			if (f.label) byLabel.set(String(f.label).toLowerCase(), f.fieldname);
			byLabel.set(String(f.fieldname).toLowerCase(), f.fieldname);
		}

		const values = {};
		const unmapped = [];
		for (const f of props.action.fields || []) {
			const fieldname = byLabel.get(f.label.toLowerCase());
			if (fieldname) values[fieldname] = f.value;
			else unmapped.push(f.label);
		}
		if (!Object.keys(values).length) {
			error.value = "Couldn't match any of these fields on that document type.";
			state.value = "review";
			return;
		}
		// Never apply half a record silently: if a field the agent proposed has no
		// home on the doctype, say so instead of writing the rest and calling it done.
		if (unmapped.length) {
			error.value = `Not applying: ${unmapped.join(", ")} ${
				unmapped.length === 1 ? "is not a field" : "are not fields"
			} on ${props.action.doctype}. Ask ${agentName} to correct it.`;
			state.value = "review";
			return;
		}

		const r = await api.applyAction({
			verb: verb.value,
			doctype: props.action.doctype,
			name: props.action.name || "",
			values,
			submit: props.action.submit ? 1 : 0,
			conversation: props.conversation,
			continue: props.action.continue ? 1 : 0,
		});
		if (r?.ok === false) {
			error.value = r.error?.message || r.reason || "Couldn't save that.";
			state.value = "review";
			return;
		}
		applied.value = r?.data?.name || r?.name || "";
		state.value = "done";
		emit("applied", applied.value);
	} catch (e) {
		error.value = e?.message || "Couldn't save that.";
		state.value = "review";
	}
}

async function copyBody() {
	try {
		await navigator.clipboard.writeText(props.action.body || "");
	} catch {
		/* clipboard blocked — nothing useful to say about it */
	}
}
</script>

<template>
	<!-- email drafts are a read-only preview: sending is a gated write and its
	     confirmation arrives separately as an action:pending card. -->
	<div v-if="isEmail" class="jv-action">
		<div class="jv-action-head">
			<svg
				viewBox="0 0 24 24"
				width="15"
				height="15"
				fill="none"
				stroke="currentColor"
				stroke-width="1.8"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M4 4h16v16H4z" />
				<path d="M22 6l-10 7L2 6" />
			</svg>
			Email draft
		</div>
		<div class="jv-action-body">
			<div class="jv-field">
				<span>To</span><strong>{{ props.action.to }}</strong>
			</div>
			<div class="jv-field">
				<span>Subject</span><strong>{{ props.action.subject }}</strong>
			</div>
			<pre class="jv-email-body">{{ props.action.body }}</pre>
		</div>
		<div class="jv-action-foot">
			<button class="jv-btn is-ghost" @click="copyBody">Copy</button>
		</div>
	</div>

	<div v-else-if="isWrite" class="jv-action" :class="{ 'is-done': state === 'done' }">
		<div class="jv-action-head">
			<svg
				viewBox="0 0 24 24"
				width="15"
				height="15"
				fill="none"
				stroke="currentColor"
				stroke-width="1.8"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
				<path d="M14 2v6h6" />
			</svg>
			<span class="jv-action-title">{{ heading }}</span>
			<span class="jv-action-tag">{{ props.action.doctype }}</span>
		</div>

		<div class="jv-action-body">
			<div v-for="(f, i) in props.action.fields" :key="i" class="jv-field">
				<span>{{ f.label }}</span>
				<strong>{{ f.value || "—" }}</strong>
			</div>
		</div>

		<div v-if="invalid || error" class="jv-action-err">{{ invalid || error }}</div>

		<div v-if="state === 'done'" class="jv-action-done">
			<svg
				viewBox="0 0 24 24"
				width="15"
				height="15"
				fill="none"
				stroke="currentColor"
				stroke-width="2.4"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M20 6 9 17l-5-5" />
			</svg>
			{{ verb === "create" ? "Created" : "Updated" }}{{ applied ? ` ${applied}` : "" }}
		</div>

		<div v-else class="jv-action-foot">
			<button
				class="jv-btn is-ghost"
				:disabled="state === 'busy'"
				@click="emit('dismissed')"
			>
				Cancel
			</button>
			<button
				class="jv-btn is-primary"
				:disabled="state === 'busy' || !!invalid"
				@click="apply"
			>
				<span v-if="state === 'busy'" class="jv-spinner" />
				<span v-else>{{ verb === "create" ? "Create" : "Save" }}</span>
			</button>
		</div>
	</div>
</template>

<style scoped>
.jv-action {
	margin-top: 8px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	overflow: hidden;
}
.jv-action.is-done {
	border-color: var(--green);
}
.jv-action-head {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 11px 12px;
	border-bottom: 1px solid var(--border);
	color: var(--accent);
	font-size: 13.5px;
	font-weight: 600;
}
.jv-action-title {
	flex: 1;
	min-width: 0;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-action-tag {
	flex: none;
	padding: 2px 7px;
	border-radius: 999px;
	background: var(--card2);
	color: var(--ink6);
	font-size: 11px;
	font-weight: 500;
}
.jv-action-body {
	padding: 4px 12px 10px;
}
.jv-field {
	display: flex;
	justify-content: space-between;
	gap: 12px;
	padding: 7px 0;
	border-bottom: 1px solid var(--border);
	font-size: 12.5px;
	color: var(--ink5);
}
.jv-field:last-child {
	border-bottom: 0;
}
.jv-field strong {
	font-weight: 500;
	color: var(--ink8);
	text-align: right;
	min-width: 0;
	overflow-wrap: anywhere;
}
.jv-email-body {
	margin: 8px 0 0;
	padding: 10px;
	border-radius: 8px;
	background: var(--card2);
	color: var(--ink7);
	font-size: 12.5px;
	line-height: 1.5;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
}
.jv-action-err {
	margin: 0 12px 10px;
	padding: 9px 10px;
	border-radius: 8px;
	background: var(--red-bg);
	color: var(--red);
	font-size: 12px;
	line-height: 1.4;
}
.jv-action-done {
	display: flex;
	align-items: center;
	gap: 7px;
	padding: 11px 12px;
	border-top: 1px solid var(--border);
	color: var(--green);
	font-size: 13px;
	font-weight: 600;
}
.jv-action-foot {
	display: flex;
	gap: 8px;
	padding: 10px 12px;
	border-top: 1px solid var(--border);
}
.jv-btn {
	flex: 1;
	display: grid;
	place-items: center;
	height: 40px;
	border: 0;
	border-radius: 10px;
	font: inherit;
	font-size: 14px;
	font-weight: 600;
	cursor: pointer;
}
.jv-btn.is-primary {
	background: var(--accent-solid);
	color: #fff;
}
.jv-btn.is-ghost {
	flex: none;
	width: 92px;
	border: 1px solid var(--border2);
	background: var(--card);
	color: var(--ink8);
}
.jv-btn:disabled {
	opacity: 0.6;
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
