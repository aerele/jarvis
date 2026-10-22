<template>
	<section
		class="jv-root shared-chat flex min-h-0 min-w-0 flex-1 flex-col"
		:class="{ 'jv-dark': dark }"
		:style="paletteVars"
	>
		<header class="jv-chat-header shared-chat-header">
			<button
				v-if="store.mobile"
				type="button"
				class="shared-icon"
				:aria-label="__('Open navigation')"
				@click="store.mobileDrawerOpen = true"
			>
				<svg
					width="18"
					height="18"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					aria-hidden="true"
				>
					<path d="M3 6h18M3 12h18M3 18h18" />
				</svg>
			</button>
			<div class="shared-chat-title">
				<h1 :title="conversation?.title">
					{{ conversation?.title || __("Shared chat") }}
				</h1>
				<p v-if="conversation">
					{{ __("Shared by {0}", [conversation.owner_name || conversation.owner]) }}
				</p>
			</div>
			<span v-if="conversation && !refused" class="shared-access">{{
				projectedMode
					? conversation?.can_chat
						? __("Can chat")
						: __("Can view")
					: __("Read only")
			}}</span>
			<button
				v-if="conversation && !refused"
				type="button"
				disabled
				class="shared-action"
				:title="__('Additional chat actions are not available in shared chats yet')"
			>
				{{ __("Action") }}
			</button>
			<button
				v-if="conversation?.is_owner"
				type="button"
				class="shared-action"
				:aria-label="__('Share')"
				@click="openChatShare(String(route.params.id), conversation.title)"
			>
				{{ __("Share") }}
			</button>
			<button
				type="button"
				class="shared-icon"
				:aria-label="__('Toggle theme')"
				:title="__('Toggle theme')"
				@click="toggleTheme"
			>
				<svg
					width="16"
					height="16"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.7"
					aria-hidden="true"
				>
					<path d="M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z" />
				</svg>
			</button>
			<a
				href="/app"
				target="_blank"
				rel="noopener"
				class="shared-icon"
				:aria-label="__('Open ERPNext Desk')"
				:title="__('Open ERPNext Desk')"
			>
				<svg
					width="16"
					height="16"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.7"
					aria-hidden="true"
				>
					<path
						d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3"
					/>
				</svg>
			</a>
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
		<main
			v-else
			ref="thread"
			class="shared-chat-thread min-h-0 min-w-0 flex-1 overflow-y-auto"
			role="log"
			:aria-label="__('Shared conversation')"
			@scroll.passive="onThreadScroll"
		>
			<div class="jv-thread-inner shared-chat-content">
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
				<article
					v-for="row in messages"
					:key="row.name"
					class="shared-chat-message min-w-0"
				>
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
							<template #avatar>
								<JarvisMark
									v-if="row.role === 'assistant'"
									:size="28"
									:radius="7"
									:mood="row.streaming ? 'thinking' : 'star'"
									style="margin-top: 2px; flex: none"
								/>
							</template>
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
		<button
			v-if="showJump && conversation && !refused"
			type="button"
			class="mx-auto my-1 rounded-full border border-outline-gray-2 bg-surface-white px-3 py-1 text-xs text-ink-gray-7"
			:aria-label="__('Jump to latest')"
			@click="scrollToLatest"
		>
			{{ __("Jump to latest") }} ↓
		</button>
		<footer v-if="conversation && !refused" class="jv-composer-wrap shared-chat-footer">
			<div class="shared-chat-composer">
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
				</template>
				<Composer
					v-model="draft"
					:attachments="files"
					:canSend="canSend"
					:readonly="!conversation.can_chat"
					:placeholder="
						conversation.can_chat
							? __('Message everyone in this chat…')
							: __('You have view-only access to this chat')
					"
					:disclaimer="
						conversation.can_chat
							? __('Files you send are visible to everyone in this chat.')
							: __('Ask the owner for Can chat access to reply.')
					"
					@submit="sendMessage"
					@files-added="addFiles"
					@remove-attachment="removeFile"
				>
					<template #left-toolbar="{ pickFiles }">
						<button
							type="button"
							class="shared-attach"
							:disabled="!conversation.can_chat"
							:aria-label="__('Attach file')"
							:title="
								conversation.can_chat
									? __('Attach file')
									: __('Can chat access is required to attach files')
							"
							@click="conversation.can_chat && pickFiles()"
						>
							<svg
								width="17"
								height="17"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.7"
								stroke-linecap="round"
								stroke-linejoin="round"
								aria-hidden="true"
							>
								<path
									d="M21.44 11.05l-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a1.5 1.5 0 0 1-2.12-2.12l8.49-8.49"
								/>
							</svg>
						</button>
						<SharedComposerToolbar side="left" />
					</template>
					<template #right-toolbar><SharedComposerToolbar side="right" /></template>
				</Composer>
				<p v-if="conversation.can_chat" class="shared-control-note">
					{{
						__(
							"Model, voice, wiki and connector selection are not available in shared chats yet. Your own defaults and permissions apply; actions require confirmation."
						)
					}}
				</p>
			</div>
		</footer>
	</section>
</template>
<script setup>
import { ref, reactive, computed, watch, nextTick, onBeforeUnmount, inject } from "vue";
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
import JarvisMark from "@/components/JarvisMark.vue";
import SharedComposerToolbar from "@/components/chat/SharedComposerToolbar.vue";
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
import { needsJumpArrow, shouldFollowBottom } from "@/lib/chatScroll";
const route = useRoute(),
	router = useRouter(),
	store = useShellStore(),
	socket = inject("$socket", null);
const { effectiveDark: dark, paletteVars, toggleTheme } = useJarvisTheme();
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
const thread = ref(null),
	showJump = ref(false);
let pinned = true;
function onThreadScroll() {
	showJump.value = !!thread.value && needsJumpArrow(thread.value);
	pinned = !showJump.value;
}
function scrollToLatest() {
	if (thread.value)
		thread.value.scrollTop = Math.max(
			0,
			thread.value.scrollHeight - thread.value.clientHeight
		);
	pinned = true;
	showJump.value = false;
}
function onThreadResize() {
	const follow = pinned;
	requestAnimationFrame(() => {
		if (!disposed && follow) scrollToLatest();
	});
}
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
async function sendMessage() {
	const id = chatId.value;
	scrollToLatest();
	await send();
	if (id === chatId.value && pinned) scrollToLatest();
}
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
			const initial = !conversation.value,
				previousNewest = messages.value.at(-1)?.seq || 0,
				anchor = {
					top: thread.value?.scrollTop || 0,
					height: thread.value?.scrollHeight || 0,
				};
			const page =
				before != null
					? prependOlderPage(messages.value, paging.value, data)
					: mergeNewestPage(messages.value, paging.value, data, asked);
			const unique = new Map(
				[...page.messages, ...(data.older_pending || [])].map((m) => [m.name, m])
			);
			messages.value = [...unique.values()].sort((a, b) => a.seq - b.seq);
			const addedLatest = messages.value.some((m) => m.seq > previousNewest);
			paging.value = page.paging;
			conversation.value = data.conversation;
			if (projectedMode.value) {
				turns.value = data.turns || [];
				questions.value = data.questions || [];
				reconcile(turns.value);
			}
			refused.value = "";
			reconnecting.value = false;
			await nextTick();
			if (gen !== generation || disposed) return;
			if (thread.value) {
				if (before != null) {
					thread.value.scrollTop =
						anchor.top + thread.value.scrollHeight - anchor.height;
					onThreadScroll();
				} else if (
					initial ||
					(pinned && addedLatest) ||
					shouldFollowBottom({
						pinned,
						streaming:
							messages.value.some((m) => m.streaming) ||
							visibleTurns.value.some((t) => activeStates.has(t.state)),
						revealPending: false,
					})
				)
					scrollToLatest();
				else showJump.value = needsJumpArrow(thread.value);
			}
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
		pinned = true;
		showJump.value = false;
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
window.addEventListener("resize", onThreadResize);
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
	window.removeEventListener("resize", onThreadResize);
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

<style scoped>
/* Match ChatView's header, thread and composer measurements; reuse its actual
   Message/Composer/mark components without mounting its private-chat lifecycle. */
.shared-chat {
	height: 100%;
	overflow: hidden;
	position: relative;
	color: var(--text);
	background: var(--surface);
	font-family: "Inter", system-ui, sans-serif;
}
.shared-chat-header {
	display: flex;
	align-items: center;
	flex: none;
	height: 52px;
	min-width: 0;
	gap: 8px;
	padding: 0 18px;
	border-bottom: 1px solid var(--border);
}
.shared-chat-title {
	flex: 1;
	min-width: 0;
	line-height: 1.15;
}
.shared-chat-title h1 {
	font-size: 14px;
	font-weight: 600;
	letter-spacing: -0.01em;
}
.shared-chat-title p {
	margin-top: 2px;
	font-size: 11.5px;
	color: var(--text-3);
}
.shared-chat-title h1,
.shared-chat-title p {
	overflow: hidden;
	white-space: nowrap;
	text-overflow: ellipsis;
}
.shared-access {
	flex: none;
	padding: 3px 7px;
	border-radius: 6px;
	font-size: 11px;
	color: var(--text-2);
	background: var(--surface-1);
}
.shared-icon {
	display: flex;
	align-items: center;
	justify-content: center;
	flex: none;
	width: 32px;
	height: 32px;
	border: 1px solid var(--border);
	border-radius: 7px;
	color: var(--text-2);
	background: transparent;
}
.shared-action {
	flex: none;
	border: 1px solid var(--border);
	border-radius: 20px;
	padding: 5px 10px;
	background: var(--surface-1);
	color: var(--text-2);
	font-size: 12px;
}
.shared-action:disabled {
	opacity: 0.45;
	cursor: not-allowed;
}
.shared-chat-content {
	max-width: 1280px;
	margin: 0 auto;
	padding: 26px 40px 36px;
	min-width: 0;
}
.shared-chat-message {
	margin-bottom: 26px;
}
.shared-chat-footer {
	flex: none;
	min-width: 0;
	padding: 12px 40px 16px;
	background: var(--surface);
}
.shared-chat-composer {
	max-width: 1280px;
	min-width: 0;
	margin: 0 auto;
}
.shared-attach {
	display: flex;
	align-items: center;
	justify-content: center;
	flex: none;
	width: 30px;
	height: 30px;
	border: none;
	border-radius: 7px;
	background: transparent;
	color: var(--text-3);
}
.shared-control-note {
	margin-top: 6px;
	text-align: center;
	font-size: 10.5px;
	line-height: 1.5;
	color: var(--text-3);
}
.shared-chat :is(button, a):focus-visible {
	outline: 2px solid var(--link);
	outline-offset: 3px;
}
@media (max-width: 767px) {
	.shared-chat-header {
		gap: 6px;
		padding: 0 12px;
	}
	.shared-action:disabled {
		display: none;
	}
	.shared-chat-footer {
		padding-bottom: calc(14px + env(safe-area-inset-bottom, 0px));
	}
}
@media (max-width: 640px) {
	.shared-chat-content {
		padding: 22px 16px 28px;
	}
	.shared-chat-footer {
		padding: 10px 14px calc(14px + env(safe-area-inset-bottom, 0px));
	}
}
</style>
