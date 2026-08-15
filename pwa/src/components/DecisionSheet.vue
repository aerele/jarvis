<script setup>
import { computed, ref, watch } from "vue";
import { renderMarkdown } from "@shared/markdown.js";
import { pendingCardOf, pendingExpiry } from "@shared/lib/actionSummary.js";
import Sheet from "./Sheet.vue";
import PendingCard from "./PendingCard.vue";
import * as api from "../api";
import { agentName } from "@/branding";

// Approve / deny a parked write.
//
// Approve calls confirm_tool with the one-time token — the ONLY path that runs
// the parked call. Deny has no endpoint by design: dropping the card leaves the
// token to expire server-side, which is exactly what "no" means. The native app
// puts a biometric prompt in front of approve; the browser has no equivalent it
// can trust, and this surface is already behind the Frappe session, so the
// confirmation itself is the gate.
const props = defineProps({ action: { type: Object, default: null } });
const emit = defineEmits(["close", "resolved"]);

const state = ref("review"); // review | busy | approved | denied
const error = ref("");

// Fresh card → fresh sheet.
watch(
	() => props.action?.token,
	() => {
		state.value = "review";
		error.value = "";
	}
);

const summary = computed(
	() => props.action?.summary || props.action?.tool || "Approve this change"
);

// F9: the server-built "what will change" card (null -> fall back to previewHtml).
const card = computed(() => pendingCardOf({ preview: props.action?.preview }));
// Raw dry-run JSON for the card's Details expander; empty for described-intent
// previews or when there is no dry-run doc.
const rawDetails = computed(() => {
	const pv = props.action?.preview;
	if (!pv || typeof pv === "string" || pv.described || pv.would == null) return "";
	return typeof pv.would === "string" ? pv.would : JSON.stringify(pv.would, null, 2);
});
// F15: the server's wall-clock expiry (epoch seconds) for this parked token.
const expired = computed(() => pendingExpiry(props.action?.expires_at, Date.now()).expired);

// The preview is either a ready-made string or {note, would} — `described` means
// the note already says it all and the payload dump would just be noise.
const previewHtml = computed(() => {
	const pv = props.action?.preview;
	if (!pv) return "";
	if (typeof pv === "string") return renderMarkdown(pv);
	const parts = [];
	if (pv.note) parts.push(pv.note);
	if (!pv.described && pv.would != null) {
		const body = typeof pv.would === "string" ? pv.would : JSON.stringify(pv.would, null, 2);
		parts.push("```\n" + body + "\n```");
	}
	return parts.length ? renderMarkdown(parts.join("\n\n")) : "";
});

function deny() {
	if (state.value === "busy") return;
	state.value = "denied";
	emit("resolved", props.action.token, "denied");
}

async function approve() {
	if (state.value === "busy") return;
	error.value = "";
	state.value = "busy";
	try {
		const r = await api.confirmTool(props.action.token, props.action.conversation);
		if (r && r.ok === false) {
			// The token is single-use and short-lived: a stale card must say so
			// rather than look like a failure the user can retry.
			if (r.error?.type === "InvalidConfirmation") {
				// InvalidConfirmation is deliberately opaque; use the card's own
				// wall-clock expiry to say the right thing (F15).
				error.value = pendingExpiry(props.action?.expires_at, Date.now()).expired
					? `This confirmation expired. Tell ${agentName} the action again to retry it.`
					: `Couldn't confirm. It may have been handled elsewhere. Refresh, or ask ${agentName} to try again.`;
				state.value = "review";
				emit("resolved", props.action.token, "expired");
				return;
			}
			error.value = r.error?.message || r.reason || "Couldn't run this action.";
			state.value = "review";
			return;
		}
		state.value = "approved";
		emit("resolved", props.action.token, "approved");
	} catch (e) {
		error.value = e?.message || "Couldn't run this action.";
		state.value = "review";
	}
}
</script>

<template>
	<Sheet :open="!!props.action" @close="emit('close')">
		<div v-if="props.action" class="jv-dsheet">
			<div v-if="state === 'approved' || state === 'denied'" class="jv-dresult">
				<div class="jv-dresult-icon" :class="state === 'approved' ? 'is-ok' : 'is-no'">
					<svg
						v-if="state === 'approved'"
						viewBox="0 0 24 24"
						width="34"
						height="34"
						fill="none"
						stroke="currentColor"
						stroke-width="2.4"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M20 6 9 17l-5-5" />
					</svg>
					<svg
						v-else
						viewBox="0 0 24 24"
						width="32"
						height="32"
						fill="none"
						stroke="currentColor"
						stroke-width="2.4"
						stroke-linecap="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</div>
				<div class="jv-dresult-title">
					{{ state === "approved" ? "Approved" : "Denied" }}
				</div>
				<div class="jv-dresult-sub">
					{{
						state === "approved"
							? `${agentName} is running the action now. You'll see the result in the thread.`
							: `The change was rejected. ${agentName} won't run this action.`
					}}
				</div>
				<button class="jv-btn is-primary" @click="emit('close')">Done</button>
			</div>

			<template v-else>
				<div class="jv-dbody">
					<div class="jv-dtags">
						<span class="jv-dtag">NEEDS APPROVAL</span>
						<span class="jv-dtool">{{ props.action.tool }}</span>
					</div>
					<div class="jv-dsummary">{{ summary }}</div>
					<PendingCard v-if="card" :card="card" :details="rawDetails" class="jv-dcard" />
					<div v-else-if="previewHtml" class="jv-dpreview" v-html="previewHtml" />
					<div v-if="expired" class="jv-dexpired">
						This confirmation expired. Tell {{ agentName }} the action again to retry
						it.
					</div>
					<div v-if="error" class="jv-derror">{{ error }}</div>
				</div>

				<div class="jv-dactions">
					<button class="jv-btn is-ghost" :disabled="state === 'busy'" @click="deny">
						Deny
					</button>
					<button
						class="jv-btn is-primary jv-dapprove"
						:disabled="state === 'busy' || expired"
						@click="approve"
					>
						<span v-if="state === 'busy'" class="jv-spinner" />
						<span v-else>Approve</span>
					</button>
				</div>
			</template>
		</div>
	</Sheet>
</template>

<style scoped>
.jv-dsheet {
	display: flex;
	flex-direction: column;
	min-height: 0;
}
.jv-dbody {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	padding: 6px 20px 12px;
}
.jv-dtags {
	display: flex;
	align-items: center;
	gap: 8px;
	margin-bottom: 8px;
}
.jv-dtag {
	padding: 2px 7px;
	border-radius: 5px;
	background: var(--amber);
	color: #fff;
	font-size: 10px;
	font-weight: 600;
}
.jv-dtool {
	font-size: 10.5px;
	font-weight: 600;
	letter-spacing: 0.5px;
	text-transform: uppercase;
	color: var(--ink5);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-dsummary {
	font-size: 19px;
	font-weight: 600;
	line-height: 1.3;
	letter-spacing: -0.2px;
	color: var(--ink9);
}
.jv-dpreview {
	margin-top: 12px;
	font-size: 13px;
	line-height: 1.5;
	color: var(--ink7);
}
.jv-dpreview :deep(pre) {
	display: block;
	max-width: 100%;
	overflow-x: auto;
	padding: 10px;
	border-radius: 8px;
	background: var(--card2);
	font-size: 12px;
}
.jv-dcard {
	margin-top: 12px;
}
.jv-dexpired {
	margin-top: 12px;
	padding: 11px;
	border-radius: 10px;
	background: var(--card2);
	color: var(--ink5);
	font-size: 12.5px;
	line-height: 1.45;
}
.jv-derror {
	margin-top: 12px;
	padding: 11px;
	border-radius: 10px;
	background: var(--red-bg);
	color: var(--red);
	font-size: 12.5px;
	line-height: 1.4;
}
.jv-dactions {
	display: flex;
	gap: 10px;
	flex: none;
	padding: 12px 20px 16px;
	border-top: 1px solid var(--border);
}
.jv-btn {
	height: 48px;
	padding: 0 18px;
	border: 0;
	border-radius: 12px;
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
.jv-btn.is-primary {
	background: var(--accent-solid);
	color: #fff;
}
.jv-btn.is-ghost {
	width: 96px;
	flex: none;
	border: 1px solid var(--border2);
	background: var(--card);
	color: var(--ink8);
}
.jv-dapprove {
	flex: 1;
	display: grid;
	place-items: center;
}
.jv-btn:disabled {
	opacity: 0.6;
}
.jv-dresult {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 14px;
	padding: 34px 26px 26px;
	text-align: center;
}
.jv-dresult-icon {
	display: grid;
	place-items: center;
	width: 70px;
	height: 70px;
	border-radius: 999px;
}
.jv-dresult-icon.is-ok {
	background: var(--green-bg);
	color: var(--green);
}
.jv-dresult-icon.is-no {
	background: var(--red-bg);
	color: var(--red);
}
.jv-dresult-title {
	font-size: 18px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-dresult-sub {
	font-size: 13.5px;
	line-height: 1.5;
	color: var(--ink5);
}
.jv-dresult .jv-btn {
	align-self: stretch;
	margin-top: 6px;
}
.jv-spinner {
	width: 18px;
	height: 18px;
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
