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
				__("Read only")
			}}</span>
		</header>
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
						v-if="row.awaiting_owner"
						class="rounded bg-surface-gray-2 px-3 py-2 text-sm text-ink-gray-6"
					>
						{{ __("Awaiting the owner's confirmation") }}
					</p>
					<template v-else-if="row.role === 'tool'">
						<ReceiptChip v-if="row.action_outcome" :message="row" :nextDone="true" />
						<SharedToolActivity v-else :message="row" />
					</template>
					<template v-else>
						<Message
							:variant="row.role === 'user' ? 'bubble' : 'row'"
							:text="row.role === 'user' ? row.content : ''"
							:html="''"
							:editable="false"
							:copyable="false"
							:sender="row.role === 'assistant' ? brand.agentName : ''"
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
					/>
				</article>
			</div>
		</main>
	</section>
</template>
<script setup>
import { ref, computed, watch, onBeforeUnmount, inject } from "vue";
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
			const data = await getSharedConversation(
				id,
				{
					...(before != null
						? { before }
						: { after: heldNewestSeq(held, paging.value), refresh: asked }),
				},
				signal
			);
			if (gen !== generation || disposed) return;
			if (data?.refused) {
				conversation.value = null;
				messages.value = [];
				paging.value = null;
				refused.value = data.refused;
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
	if (event?.kind === "share:changed" && event.conversation_id === String(route.params.id))
		reload();
};
window.addEventListener("focus", onFocus);
socket?.on("jarvis:event", onEvent);
const timer = setInterval(() => {
	if (messages.value.some((m) => m.streaming)) reload();
}, 15000);
onBeforeUnmount(() => {
	disposed = true;
	++generation;
	controller?.abort();
	clearInterval(timer);
	window.removeEventListener("focus", onFocus);
	socket?.off("jarvis:event", onEvent);
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
.shared-chat :deep(svg) {
	max-width: 100%;
}
</style>
