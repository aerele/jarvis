<template>
	<section
		class="shared-chat flex min-h-0 min-w-0 flex-1 flex-col text-ink-gray-8"
		:class="{ 'jv-dark': dark }"
		:style="paletteVars"
	>
		<header
			class="flex min-w-0 flex-wrap items-center justify-between gap-3 border-b border-outline-gray-2 px-4 py-3"
		>
			<div class="min-w-0">
				<h1 class="break-words text-base font-semibold">
					{{ conversation?.title || __("Shared chat") }}
				</h1>
				<p v-if="conversation" class="mt-1 text-sm text-ink-gray-5">
					{{ __("Shared by {0}", [conversation.owner_name || conversation.owner]) }}
				</p>
			</div>
			<span class="shrink-0 rounded bg-surface-gray-2 px-2 py-1 text-xs">{{
				projectedMode
					? conversation?.can_chat
						? __("Can chat")
						: __("Can view")
					: __("Read only")
			}}</span>
			<button
				v-if="conversation?.is_owner"
				type="button"
				class="rounded border border-outline-gray-2 px-3 py-1.5 text-sm"
				@click="openChatShare(String(route.params.id), conversation.title)"
			>
				{{ __("Share") }}
			</button>
		</header>
		<p
			v-if="projectedMode && conversation?.notice && !refused"
			class="border-b border-outline-gray-2 bg-surface-gray-2 px-4 py-2 text-xs leading-relaxed text-ink-gray-6"
		>
			{{ conversation.notice }}
		</p>
		<div
			v-if="loading && !conversation"
			role="status"
			class="animate-pulse p-6 text-sm text-ink-gray-5"
		>
			{{ __("Loading shared chat…") }}
		</div>
		<p v-else-if="refused" role="status" class="p-6 text-sm">
			{{
				refused === "sharing_off"
					? __("Sharing is turned off on this site")
					: __("This chat is not shared with you")
			}}
		</p>
		<main v-else class="min-h-0 min-w-0 flex-1 overflow-y-auto px-3 py-5 sm:px-6">
			<div class="mx-auto w-full min-w-0 max-w-3xl">
				<div
					v-if="reconnecting"
					role="status"
					class="mb-4 flex flex-wrap items-center gap-3 text-sm text-ink-gray-5"
				>
					{{ __("Reconnecting…")
					}}<button
						type="button"
						class="underline"
						:disabled="loading"
						@click="reload()"
					>
						{{ __("Reconnect") }}
					</button>
				</div>
				<button
					v-if="paging?.hasMore"
					type="button"
					class="mb-5 rounded border border-outline-gray-2 px-3 py-1.5 text-sm"
					:disabled="loading"
					@click="reload(paging.before)"
				>
					{{ __("Load earlier") }}
				</button>
				<p v-if="conversation && !messages.length" class="py-6 text-sm text-ink-gray-5">
					{{ __("No messages yet") }}
				</p>
				<article v-for="row in messages" :key="row.name" class="mb-5 min-w-0">
					<p
						v-if="row.actor?.name"
						class="mb-1 break-words text-xs font-medium text-ink-gray-6"
					>
						{{
							row.role === "assistant"
								? __("{0} for {1}", [brand.agentName, row.actor.name])
								: row.actor.name
						}}
					</p>
					<div
						v-if="row.awaiting_actor"
						class="rounded border border-outline-gray-2 bg-surface-gray-2 p-3 text-sm"
					>
						<p>{{ __("Awaiting {0}'s confirmation", [row.awaiting_actor.name]) }}</p>
						<template v-if="conversation?.can_chat && row.control">
							<p class="mt-2 whitespace-pre-wrap break-words">
								{{ row.control.summary || row.control.card?.heading }}
							</p>
							<PendingCard
								v-if="commonCard(row.control)"
								:card="commonCard(row.control)"
								:details="JSON.stringify(row.control.card, null, 2)"
							/>
							<pre
								v-else-if="row.control.card"
								class="mt-2 whitespace-pre-wrap break-words text-xs"
								>{{ JSON.stringify(row.control.card, null, 2) }}</pre
							>
							<div class="mt-3 flex flex-wrap gap-2">
								<button
									type="button"
									class="rounded bg-surface-gray-7 px-3 py-1.5 text-ink-white"
									:disabled="!!actionBusy['action:' + row.control.action]"
									@click="
										perform('action:' + row.control.action, () =>
											confirmSharedAction(
												String(route.params.id),
												row.control.action
											)
										)
									"
								>
									{{ __("Confirm") }}
								</button>
								<button
									type="button"
									class="rounded border border-outline-gray-3 px-3 py-1.5"
									:disabled="!!actionBusy['action:' + row.control.action]"
									@click="
										perform('action:' + row.control.action, () =>
											discardSharedAction(
												String(route.params.id),
												row.control.action
											)
										)
									"
								>
									{{ __("Discard") }}
								</button>
							</div>
						</template>
					</div>
					<p
						v-else-if="row.awaiting_owner"
						class="rounded bg-surface-gray-2 px-3 py-2 text-sm text-ink-gray-6"
					>
						{{ __("Awaiting the owner's confirmation") }}
					</p>
					<template v-else-if="row.role === 'tool'">
						<ReceiptChip v-if="row.action_outcome" :message="row" :nextDone="true" />
						<SharedToolActivity
							v-else
							:message="row"
							:loadDetail="projectedMode ? getCollaborationTool : undefined"
						/>
					</template>
					<template v-else>
						<Message
							:variant="row.role === 'user' ? 'bubble' : 'row'"
							:text="row.role === 'user' ? row.content : ''"
							:html="''"
							:editable="false"
							:copyable="false"
							:sender="
								row.role === 'assistant' && !row.actor?.name ? brand.agentName : ''
							"
						>
							<template #above-body
								><StepsBox
									v-if="steps(row).length"
									:view="{
										mode: 'folded',
										head: { label: __('Activity'), expandable: true },
									}"
									:open="!!stepsOpen[row.name]"
									@toggle="stepsOpen[row.name] = !stepsOpen[row.name]"
									><template #details
										><p
											v-for="(step, i) in steps(row)"
											:key="i"
											class="mb-1 break-words text-sm"
										>
											{{ step.text || step }}
										</p></template
									></StepsBox
								></template
							>
							<template #below-body
								><template v-for="(part, i) in parts(row)" :key="i"
									><div
										v-if="part.type === 'text'"
										class="jv-md-body jv-md"
										style="font-size: 14px; line-height: 1.6"
										v-html="renderMarkdown(part.text)"
									/>
									<JvChart
										v-else-if="part.type === 'chart'"
										:spec="part.spec"
										:dark="dark"
									/><SharedDiagram
										v-else-if="part.type === 'diagram'"
										:source="part.text"
										:dark="dark"
									/>
									<pre
										v-else-if="part.type === 'summary'"
										class="my-3 whitespace-pre-wrap break-words rounded bg-surface-gray-2 p-3 text-xs"
										>{{ part.text }}</pre
									>
								</template></template
							>
						</Message>
						<p
							v-if="row.error_headline"
							class="my-2 break-words text-sm text-ink-red-5"
						>
							{{ row.error_headline }}
						</p>
						<p v-if="row.stopped" class="mt-1 text-sm text-ink-gray-5">
							{{ __("(stopped)") }}
						</p>
						<p
							v-else-if="row.recovering || row.streaming"
							role="status"
							class="mt-1 text-sm text-ink-gray-5"
						>
							{{ __("Still working…") }}
						</p>
					</template>
					<SharedCanvas
						v-for="(artifact, i) in canvas(row)"
						:key="artifact.name || i"
						:artifact="artifact"
						:message="row.name"
						:conversation="String(route.params.id)"
						:dark="dark"
						:loadCanvas="projectedMode ? getCollaborationCanvas : undefined"
					/>
				</article>
				<section
					v-for="question in questions.filter((q) => q.status === 'Pending')"
					:key="question.run_id"
					class="mb-4 rounded border border-outline-gray-2 p-3 text-sm"
				>
					<p class="mb-2 text-xs text-ink-gray-5">
						{{ __("Question for {0}", [question.actor?.name]) }}
					</p>
					<p class="whitespace-pre-wrap break-words">{{ question.question }}</p>
					<form
						v-if="conversation?.can_chat && question.control"
						class="mt-3"
						@submit.prevent="answer(question)"
					>
						<label class="block"
							>{{ __("Your answer")
							}}<input
								v-model="questionAnswers[question.control.question]"
								class="mt-1 w-full rounded border border-outline-gray-2 bg-surface-white px-3 py-2"
								:disabled="!!actionBusy['question:' + question.control.question]"
						/></label>
						<button
							type="submit"
							class="mt-2 rounded border border-outline-gray-3 px-3 py-1.5"
							:disabled="
								!questionAnswers[question.control.question]?.trim() ||
								!!actionBusy['question:' + question.control.question]
							"
						>
							{{ __("Send answer") }}
						</button>
					</form>
				</section>
				<div
					v-if="visibleTurns.length"
					class="mb-4 rounded border border-outline-gray-2 p-3 text-sm"
					aria-live="polite"
				>
					<div
						v-for="turn in visibleTurns"
						:key="turn.run_id"
						class="flex flex-wrap items-center gap-2 py-1"
					>
						<span class="min-w-0 flex-1 break-words"
							>{{ turn.actor?.name }} · {{ stateLabel(turn.state) }}</span
						>
						<button
							v-if="turn.can_stop"
							type="button"
							class="rounded border border-outline-gray-3 px-3 py-1"
							:aria-label="__('Stop request by {0}', [turn.actor?.name])"
							:disabled="!!actionBusy[turn.run_id]"
							@click="
								perform(turn.run_id, () =>
									stopSharedRun(String(route.params.id), turn.run_id)
								)
							"
						>
							{{ __("Stop") }}
						</button>
						<button
							v-if="turn.can_retry && conversation?.can_chat"
							type="button"
							class="rounded border border-outline-gray-3 px-3 py-1"
							:disabled="!!actionBusy[turn.run_id]"
							@click="retryTurn(turn)"
						>
							{{ __("Retry") }}
						</button>
					</div>
				</div>
			</div>
		</main>
		<footer
			v-if="conversation && !refused"
			class="border-t border-outline-gray-2 px-3 py-3 sm:px-6"
		>
			<div class="mx-auto w-full min-w-0 max-w-3xl">
				<p v-if="actionError" role="alert" class="mb-2 break-words text-sm text-ink-red-5">
					{{ actionError }}
				</p>
				<template v-if="conversation.can_chat">
					<p
						v-if="sendError"
						role="alert"
						class="mb-2 break-words text-sm text-ink-red-5"
					>
						{{ sendError }}
					</p>
					<p v-if="waiting" role="status" class="mb-2 text-xs text-ink-gray-5">
						{{ __("Request accepted. Your draft stays here until it starts.") }}
					</p>
					<button
						v-if="failed"
						type="button"
						class="mb-2 text-sm underline"
						:disabled="sending || waiting || reconnecting"
						@click="retryPrevious"
					>
						{{ __("Retry previous send") }}
					</button>
					<Composer
						v-model="draft"
						:attachments="files"
						:canSend="canSend"
						:placeholder="__('Message everyone in this chat…')"
						:disclaimer="__('Files you send are visible to everyone in this chat.')"
						@submit="send()"
						@files-added="addFiles"
						@remove-attachment="removeFile"
					/>
				</template>
			</div>
		</footer>
	</section>
</template>
<script setup>
import { ref, reactive, computed, watch, onBeforeUnmount, inject } from "vue";
import { useRoute, useRouter } from "vue-router";
import { getSharedConversation } from "@/api/chatSharing";
import { useShellStore } from "@/stores/shell";
import { useJarvisTheme } from "@/theme";
import { __ } from "@/lib/i18n";
import { renderMarkdown } from "@/markdown";
import { brand } from "@/branding";
import { sharedTranscript } from "@/lib/sharedTranscript";
import { mergeNewestPage, prependOlderPage, heldNewestSeq } from "@/lib/historyPaging";
import Message from "@/components/chat/Message.vue";
import StepsBox from "@/components/chat/StepsBox.vue";
import ReceiptChip from "@/components/ReceiptChip.vue";
import JvChart from "@/charts/JvChart.vue";
import SharedDiagram from "@/components/chat/SharedDiagram.vue";
import SharedCanvas from "@/components/chat/SharedCanvas.vue";
import SharedToolActivity from "@/components/chat/SharedToolActivity.vue";
import Composer from "@/components/chat/Composer.vue";
import PendingCard from "@/components/PendingCard.vue";
import { pendingCardOf } from "@/lib/actionSummary";
import {
	getCollaboration,
	stopSharedRun,
	retrySharedRun,
	confirmSharedAction,
	discardSharedAction,
	answerSharedQuestion,
	getCollaborationTool,
	getCollaborationCanvas,
} from "@/api/chatCollaboration";
import { useSharedComposer } from "@/composables/useSharedComposer";
import { sharedResultMessage } from "@/lib/sharedActionResult";
import { openChatShare } from "@/composables/useChatSharing";
import { errMessage } from "@/lib/errors";
const route = useRoute(),
	router = useRouter(),
	store = useShellStore(),
	socket = inject("$socket", null);
const { effectiveDark: dark, paletteVars } = useJarvisTheme();
const conversation = ref(null),
	messages = ref([]),
	paging = ref(null),
	loading = ref(false),
	refused = ref(""),
	reconnecting = ref(false),
	stepsOpen = ref({});
const projectedMode = ref(false),
	turns = ref([]),
	questions = ref([]),
	actionError = ref("");
const actionBusy = reactive({}),
	questionAnswers = reactive({});
const chatId = computed(() => String(route.params.id));
const canChat = computed(
	() => !!conversation.value?.can_chat && !refused.value && !reconnecting.value
);
const {
	draft,
	files,
	sending,
	error: sendError,
	canSend,
	send,
	reconcile,
	adopt,
	acceptedRun,
	addFiles,
	removeFile,
	waiting,
	failed,
	retryPrevious,
} = useSharedComposer({ id: chatId, canChat, refresh: () => reload() });
const commonCard = (control) => pendingCardOf({ preview: { card: control?.card } });
const activeStates = new Set([
	"queued",
	"preparing",
	"ready",
	"dispatching",
	"streaming",
	"recovering",
]);
const visibleTurns = computed(() =>
	turns.value.filter((t) => activeStates.has(t.state) || t.can_retry)
);
const stateLabel = (state) =>
	({
		queued: __("Queued"),
		preparing: __("Preparing"),
		ready: __("Preparing"),
		dispatching: __("Starting"),
		streaming: __("Working"),
		recovering: __("Checking outcome"),
		errored: __("Not started"),
		cancelled: __("Stopped"),
	}[state] || __("Finished"));
async function perform(key, action) {
	if (actionBusy[key] || refused.value) return;
	const gen = generation;
	actionBusy[key] = true;
	actionError.value = "";
	try {
		const result = await action();
		if (result?.ok === false) throw new Error(sharedResultMessage(result));
	} catch (exception) {
		if (gen === generation)
			actionError.value = errMessage(
				exception,
				__("The action was not confirmed. Reload the chat to check its outcome.")
			);
	} finally {
		delete actionBusy[key];
		if (gen === generation) await reload();
	}
}
function retryTurn(turn) {
	if (actionBusy[turn.run_id] || refused.value) return;
	adopt(turn.run_id);
	return perform(turn.run_id, () => retrySharedRun(chatId.value, turn.run_id));
}
function answer(question) {
	const name = question.control?.question,
		decision = questionAnswers[name]?.trim();
	if (name && decision && conversation.value?.can_chat)
		perform("question:" + name, () => answerSharedQuestion(chatId.value, name, decision));
}
function commonPage(data) {
	return {
		...data,
		paging: { has_more: data.has_more, before: data.oldest_seq },
		older_pending: [],
		refreshed: [],
	};
}
let generation = 0,
	flight = null,
	controller = null,
	again = false,
	disposed = false;
function list(value) {
	if (Array.isArray(value)) return value;
	if (typeof value === "string") {
		try {
			const parsed = JSON.parse(value);
			return Array.isArray(parsed) ? parsed : [];
		} catch {
			return [];
		}
	}
	return [];
}
const canvas = (row) => list(row.canvas),
	steps = (row) => list(row.steps).concat(list(row.live_steps));
const parsed = computed(
	() =>
		new Map(
			messages.value.map((row) => [row.name, sharedTranscript(row.content, !!row.streaming)])
		)
);
const parts = (row) => (row.role === "assistant" ? parsed.value.get(row.name) || [] : []);
async function reload(before = null) {
	if (disposed) return;
	if (flight) {
		if (before == null) again = true;
		return flight;
	}
	const gen = generation,
		id = String(route.params.id),
		held = messages.value,
		asked = held
			.filter((m) => m.awaiting_owner || m.streaming)
			.map((m) => m.name)
			.slice(0, 100);
	controller = new AbortController();
	loading.value = true;
	const signal = controller.signal;
	flight = (async () => {
		try {
			let data = projectedMode.value
				? commonPage(
						await getCollaboration(id, { before, watchRun: acceptedRun.value }, signal)
				  )
				: await getSharedConversation(
						id,
						{
							...(before != null
								? { before }
								: { after: heldNewestSeq(held, paging.value), refresh: asked }),
						},
						signal
				  );
			if (
				!projectedMode.value &&
				(data?.conversation?.collaboration_available ||
					data?.conversation?.collaboration_active ||
					data?.refused === "owner")
			) {
				try {
					const common = await getCollaboration(
						id,
						{ watchRun: acceptedRun.value },
						signal
					);
					if (data?.refused !== "owner" || common.conversation?.activated) {
						if (gen !== generation || disposed) return;
						projectedMode.value = true;
						messages.value = [];
						paging.value = null;
						before = null;
						data = commonPage(common);
					}
				} catch (error) {
					if (data?.refused !== "owner") throw error;
				}
			}
			if (gen !== generation || disposed) return;
			if (data?.refused) {
				conversation.value = null;
				messages.value = [];
				paging.value = null;
				refused.value = data.refused;
				turns.value = [];
				questions.value = [];
				reconnecting.value = false;
				store.loadSharedConversations?.();
				if (data.refused === "owner") router.replace("/c/" + encodeURIComponent(id));
				return;
			}
			const page =
				before != null
					? prependOlderPage(messages.value, paging.value, data)
					: mergeNewestPage(messages.value, paging.value, data, asked);
			const unique = new Map(
				[...page.messages, ...(data.older_pending || [])].map((m) => [m.name, m])
			);
			messages.value = [...unique.values()].sort((a, b) => a.seq - b.seq);
			paging.value = page.paging;
			conversation.value = data.conversation;
			if (projectedMode.value) {
				turns.value = data.turns || [];
				questions.value = data.questions || [];
				reconcile(turns.value);
			}
			refused.value = "";
			reconnecting.value = false;
			store.markSharedSeen(id);
			store.loadSharedConversations?.();
		} catch (error) {
			if (gen === generation && !disposed) {
				if ([401, 403, 404].includes(error?.status)) {
					conversation.value = null;
					messages.value = [];
					paging.value = null;
					refused.value = "not_shared";
					turns.value = [];
					questions.value = [];
					reconnecting.value = false;
				} else reconnecting.value = true;
			}
		} finally {
			if (gen === generation) {
				flight = null;
				loading.value = false;
				controller = null;
				if (again) {
					again = false;
					reload();
				}
			}
		}
	})();
	return flight;
}
watch(
	() => route.params.id,
	() => {
		++generation;
		controller?.abort();
		flight = null;
		again = false;
		messages.value = [];
		conversation.value = null;
		paging.value = null;
		refused.value = "";
		stepsOpen.value = {};
		projectedMode.value = false;
		turns.value = [];
		questions.value = [];
		actionError.value = "";
		reconnecting.value = false;
		store.markSharedSeen(String(route.params.id));
		reload();
	},
	{ immediate: true }
);
const onFocus = () => {
	reload();
	store.loadSharedConversations?.();
};
const onEvent = (event) => {
	if (
		["share:changed", "collaboration:changed"].includes(event?.kind) &&
		event.conversation_id === String(route.params.id)
	)
		reload();
};
window.addEventListener("focus", onFocus);
socket?.on("jarvis:event", onEvent);
socket?.on("connect", onFocus);
const timer = setInterval(() => {
	if (!refused.value && (projectedMode.value || messages.value.some((m) => m.streaming)))
		reload();
}, 15000);
const activeTimer = setInterval(() => {
	if (
		!refused.value &&
		projectedMode.value &&
		(visibleTurns.value.some((t) => activeStates.has(t.state)) || waiting.value)
	)
		reload();
}, 3000);
onBeforeUnmount(() => {
	disposed = true;
	++generation;
	controller?.abort();
	clearInterval(timer);
	clearInterval(activeTimer);
	window.removeEventListener("focus", onFocus);
	socket?.off("jarvis:event", onEvent);
	socket?.off("connect", onFocus);
});
</script>
<style scoped>
.shared-chat :deep(pre),
.shared-chat :deep(table) {
	max-width: 100%;
	overflow-x: auto;
}
.shared-chat :deep(.jv-md-body) {
	overflow-wrap: anywhere;
}
.shared-chat :deep(.jv-ububble) {
	max-width: 100% !important;
}
.shared-chat :deep(table) {
	width: 100%;
	border-collapse: collapse;
	margin: 12px 0;
}
.shared-chat :deep(th),
.shared-chat :deep(td) {
	padding: 6px 8px;
	border-bottom: 1px solid var(--border, #e5e7eb);
	text-align: left;
}
.shared-chat :deep(th) {
	overflow-wrap: normal;
}
.shared-chat :deep(svg) {
	max-width: 100%;
}
</style>
