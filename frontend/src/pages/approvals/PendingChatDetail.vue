<template>
	<div :aria-busy="loading || busy !== null ? 'true' : 'false'">
		<!-- One chat action card, in the board's right pane: the card the chat shows
		     (PendingCard, text interpolation only - the values are model-derived),
		     decided with the chat's own Confirm / Discard. Only its owner sees it (D1). -->
		<div v-if="loading" class="flex justify-start">
			<JvSpinner label="Loading the proposed action…" />
		</div>

		<div v-else-if="loadError" role="alert" class="flex items-center gap-3 text-sm">
			<span class="text-ink-red-5">{{ loadError }}</span>
			<Button variant="subtle" size="sm" label="Try again" @click="load" />
		</div>

		<template v-else-if="rec">
			<p class="text-sm text-ink-gray-6">
				Jarvis proposed this in
				<span class="font-medium text-ink-gray-8">{{
					rec.conversation_title || "a chat"
				}}</span>
				and runs it only once you confirm.
			</p>

			<div class="mt-3">
				<PendingCard v-if="card" :card="card" details="" />
				<div v-else class="text-base text-ink-gray-8">{{ rec.summary || rec.tool }}</div>
			</div>

			<div v-if="!rec.can_act" class="mt-3 text-sm text-ink-gray-6" role="status">
				{{ closedText }}
			</div>

			<div class="mt-4 flex flex-wrap items-center gap-2">
				<template v-if="rec.can_act">
					<Button
						variant="solid"
						label="Confirm"
						:loading="busy === 'confirm'"
						:disabled="busy !== null"
						@click="decide('confirm')"
					/>
					<Button
						variant="ghost"
						theme="red"
						label="Discard"
						:loading="busy === 'discard'"
						:disabled="busy !== null"
						@click="decide('discard')"
					/>
				</template>
				<Button
					v-if="rec.conversation"
					variant="subtle"
					label="Open chat"
					:aria-label="'Open chat: ' + (rec.conversation_title || 'the conversation')"
					:disabled="busy !== null"
					@click="openChat"
				/>
			</div>
			<div v-if="notice" role="alert" class="mt-2 text-sm text-ink-red-5">{{ notice }}</div>
		</template>
	</div>
</template>

<script setup>
// Detail + decision for one chat action card on the Approval Board (PR-3c). The
// board adds no decide path of its own: Confirm / Discard call confirm_tool /
// dismiss_tool exactly as the chat does, so the server re-checks everything and
// the chat continues after a Confirm. The words live in lib/chatCardActions.
import { ref, computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { Button, toast } from "frappe-ui";
import PendingCard from "@/components/PendingCard.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { getPendingAction } from "@/api/approvals";
import { confirmTool, dismissTool } from "@/api";
import { errMessage, escapeHtml } from "@/lib/errors";
import {
	chatOutcomeMessage,
	chatRefusalMessage,
	chatStatusLine,
	isChatSettled,
} from "@/lib/chatCardActions";

const props = defineProps({
	name: { type: String, required: true },
	// Called with each settled answer. A callback, not an emit: Vue drops an unmounted
	// instance's emits, and switching rows mid-decision unmounts this detail.
	onDecided: { type: Function, default: null },
});
const decided = (res) => props.onDecided && props.onDecided(res);
const router = useRouter();

const rec = ref(null);
const loading = ref(true);
const loadError = ref("");
const busy = ref(null); // "confirm" | "discard" | null
const notice = ref("");

// Only a plain object with a kind renders as a card; anything else is the summary line.
const card = computed(() => {
	const c = rec.value && rec.value.card;
	return c && typeof c === "object" && c.kind ? c : null;
});
const closedText = computed(() => chatStatusLine(rec.value));

let req = 0; // monotonic: a stale load never overwrites a newer one
async function load() {
	const id = ++req;
	loading.value = !rec.value;
	loadError.value = "";
	try {
		const res = await getPendingAction(props.name);
		if (id !== req) return;
		rec.value = res || null;
	} catch (e) {
		if (id !== req) return;
		loadError.value = errMessage(e, "This action could not be loaded.");
	} finally {
		if (id === req) loading.value = false;
	}
}

async function decide(action) {
	if (busy.value !== null || !rec.value) return;
	busy.value = action;
	notice.value = "";
	try {
		const send = action === "confirm" ? confirmTool : dismissTool;
		const res = (await send(props.name, rec.value.conversation)) || {};
		if (res.ok) {
			toast.success(escapeHtml(chatOutcomeMessage(res, action)));
			decided(res);
			return;
		}
		const message = chatRefusalMessage(res);
		if (isChatSettled(res)) {
			toast.error(escapeHtml(message));
			decided(res);
			return;
		}
		notice.value = message;
	} catch (e) {
		notice.value = errMessage(e);
	} finally {
		busy.value = null;
	}
}

function openChat() {
	const conversation = rec.value && rec.value.conversation;
	if (!conversation) return;
	router.push(
		rec.value.origin_page === "dashboards"
			? { name: "DashboardsPage", query: { conversation } }
			: "/c/" + conversation
	);
}

// The board calls this when the card leaves its rail: show the settled status, but
// never over a decision in flight.
function refresh() {
	if (busy.value === null) load();
}

onMounted(load);
defineExpose({ refresh });
</script>
