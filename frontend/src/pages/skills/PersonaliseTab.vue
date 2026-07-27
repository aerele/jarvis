<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs
					:items="[
						{ label: 'Skills', route: { name: 'SkillsList' } },
						{ label: 'Personalise' },
					]"
				/>
			</template>
			<template #right-header>
				<Button
					icon="refresh-cw"
					variant="ghost"
					:tooltip="'Refresh'"
					:loading="refreshing"
					@click="refreshAll"
				/>
				<!-- gear is admin-only: caps.analysis is the org-admin gate -->
				<Button
					v-if="caps.analysis"
					icon="settings"
					variant="ghost"
					:tooltip="'Personalisation settings'"
					@click="settingsOpen = true"
				/>
			</template>
		</LayoutHeader>

		<!-- nested sub-tabs: Questions | Notes (last sub-tab persisted) -->
		<TabBar :tabs="subTabs" :model-value="subTab" @update:model-value="setSubTab" />

		<!-- ══════════════════════════ QUESTIONS ══════════════════════════ -->
		<!-- flex-col so the panes scroll and the composer stays pinned below -->
		<div v-if="subTab === 'questions'" class="flex min-h-0 flex-1 flex-col">
			<div class="min-h-0 flex-1 overflow-y-auto">
				<div
					class="mx-auto grid w-full max-w-7xl grid-cols-1 gap-6 px-5 py-5 xl:grid-cols-5"
				>
					<!-- ─────────── LEFT · question list ─────────── -->
					<section class="flex min-w-0 flex-col gap-4 xl:col-span-3">
						<!-- status filter pills (facet-chip idiom) -->
						<div class="flex flex-wrap items-center gap-2">
							<Button
								v-for="p in STATUS_PILLS"
								:key="p.value"
								:label="p.label"
								:variant="status === p.value ? 'solid' : 'subtle'"
								@click="setStatus(p.value)"
							/>
						</div>

						<!-- search + sort -->
						<div class="flex flex-wrap items-center gap-2">
							<FormControl
								type="text"
								class="w-56"
								placeholder="Search questions"
								:modelValue="search"
								@update:modelValue="(v) => (search = v)"
							>
								<template #prefix>
									<FeatherIcon name="search" class="size-4 text-ink-gray-5" />
								</template>
							</FormControl>
							<Button
								variant="subtle"
								:label="sort === 'newest' ? 'Newest' : 'Oldest'"
								:iconLeft="sort === 'newest' ? 'arrow-down' : 'arrow-up'"
								@click="toggleSort"
							/>
						</div>

						<!-- cards -->
						<div
							v-if="board.loading && !board.rows.length"
							class="grid place-items-center py-12"
						>
							<LoadingIndicator class="size-5 text-ink-gray-5" />
						</div>

						<div
							v-else-if="!board.rows.length"
							class="grid place-items-center py-12 text-center"
						>
							<div class="max-w-md space-y-2">
								<FeatherIcon
									:name="emptyState.icon"
									class="mx-auto size-9 text-ink-gray-4"
								/>
								<div class="text-base font-medium text-ink-gray-8">
									{{ emptyState.title }}
								</div>
								<div class="text-p-base text-ink-gray-6">
									{{ emptyState.body }}
								</div>
							</div>
						</div>

						<div v-else class="flex flex-col gap-3">
							<div
								v-for="row in board.rows"
								:key="row.name"
								class="rounded-lg border p-4"
								:class="
									selected && selected.name === row.name
										? 'border-outline-gray-3 bg-surface-gray-2'
										: 'border-outline-gray-2'
								"
							>
								<div class="flex flex-col gap-1">
									<OriginBadge :origin="row.origin" />
									<div class="mt-1 text-base text-ink-gray-9">
										{{ row.question }}
									</div>
									<div class="text-sm text-ink-gray-5">
										<Tooltip :text="exactDate(row.created)">
											<span>{{ timeAgo(row.created) }}</span>
										</Tooltip>
										<template
											v-if="row.status === 'Answered' && row.answered_at"
										>
											· answered {{ timeAgo(row.answered_at) }}
										</template>
										<template v-else-if="row.status === 'Ignored'">
											· set aside</template
										>
									</div>
								</div>
								<div class="mt-3 flex flex-wrap items-center gap-2">
									<Button
										variant="solid"
										:label="
											row.status === 'Answered' ? 'Answer again' : 'Answer'
										"
										@click="selectQuestion(row)"
									/>
									<Button
										v-if="row.status === 'Unanswered'"
										variant="subtle"
										label="Ignore"
										:loading="acting === row.name + ':ignore'"
										@click="ignore(row)"
									/>
									<Button
										variant="ghost"
										theme="red"
										label="Delete"
										@click="confirmDelete(row)"
									/>
								</div>
							</div>

							<!-- footer: N of M + Load more -->
							<div v-if="board.total" class="flex items-center justify-between pt-1">
								<span class="text-sm text-ink-gray-5">
									{{ board.rows.length }} of {{ board.total }}
								</span>
								<Button
									v-if="board.hasMore"
									variant="subtle"
									label="Load more"
									:loading="board.loading"
									@click="fetchQuestions('more')"
								/>
							</div>
						</div>
					</section>

					<!-- ─────────── RIGHT · context / guidance ─────────── -->
					<!-- On stacked (below xl) layouts this pane would otherwise render
					     AFTER the whole question list; when a question is selected,
					     hoist it above the list so its context is visible while the
					     user answers (order resets to natural on xl's two columns). -->
					<section
						ref="contextPane"
						class="min-w-0 xl:col-span-2"
						:class="selected ? 'order-first xl:order-none' : ''"
					>
						<div class="rounded-lg border border-outline-gray-2 p-4">
							<!-- selected question: what Jarvis noticed + prior answer -->
							<template v-if="selected">
								<OriginBadge :origin="selected.origin" />
								<div class="mt-3 text-sm font-semibold text-ink-gray-9">
									What {{ agentName }} noticed
								</div>
								<div
									v-if="selected.context_md"
									class="prose prose-sm mt-2 max-w-none text-ink-gray-7"
									v-html="contextHtml"
								/>
								<div v-else class="mt-2 text-p-base text-ink-gray-6">
									{{ agentName }} didn't attach any extra detail to this one —
									just answer in your own words below.
								</div>

								<div
									v-if="selected.status === 'Answered'"
									class="mt-4 rounded-md bg-surface-gray-1 p-3"
								>
									<div class="text-sm text-ink-gray-7">
										You answered<template v-if="selected.answered_at">
											· {{ timeAgo(selected.answered_at) }}</template
										>
									</div>
									<div class="mt-0.5 text-xs text-ink-gray-5">
										Answering again adds a new note — {{ agentName }} uses your
										latest answer. Your earlier note stays in Notes.
									</div>
								</div>
							</template>

							<!-- nothing selected: warm guidance + the mental model -->
							<template v-else>
								<div class="text-base font-semibold text-ink-gray-9">
									Teach {{ agentName }} how you work
								</div>
								<p class="mt-1 text-p-base text-ink-gray-6">
									Answer a question on the left, or just say anything in the box
									below.
								</p>

								<!-- Questions → Notes → Wiki → Skills (DESIGN §6 mental model) -->
								<div
									class="mt-4 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-ink-gray-6"
								>
									<span class="font-medium text-ink-gray-9">Questions</span>
									<FeatherIcon
										name="arrow-right"
										class="size-3.5 text-ink-gray-4"
									/>
									<span class="font-medium text-ink-gray-9">Notes</span>
									<FeatherIcon
										name="arrow-right"
										class="size-3.5 text-ink-gray-4"
									/>
									<span class="font-medium text-ink-gray-9">Wiki</span>
									<FeatherIcon
										name="arrow-right"
										class="size-3.5 text-ink-gray-4"
									/>
									<span class="font-medium text-ink-gray-9">Skills</span>
								</div>
								<div class="mt-1 text-xs text-ink-gray-5">
									{{ agentName }} asks · you answer · it remembers · it turns the
									how-to into reusable skills.
								</div>

								<div
									class="mt-4 text-xs font-medium uppercase tracking-wide text-ink-gray-5"
								>
									Things worth telling {{ agentName }}
								</div>
								<ul class="mt-2 space-y-1 text-p-base text-ink-gray-6">
									<li v-for="s in SUGGESTIONS" :key="s">{{ s }}</li>
								</ul>
							</template>
						</div>
					</section>
				</div>
			</div>

			<!-- composer: outside the scroll region, bottom-pinned -->
			<ChatComposer
				ref="composer"
				:stt-enabled="!!caps.stt_enabled"
				:question="selected"
				:saving="submitting"
				@submit="onSubmit"
				@clear-question="selected = null"
			/>
		</div>

		<!-- ══════════════════════════ NOTES ══════════════════════════ -->
		<div v-else class="min-h-0 flex-1 overflow-y-auto">
			<div class="mx-auto w-full max-w-7xl px-5 py-5">
				<NotesView ref="notesView" @reanswer="onReanswer" />
			</div>
		</div>

		<!-- admin-only settings dialog (component owned by F4) -->
		<PersonalisationSettings v-if="caps.analysis" v-model:open="settingsOpen" />
	</div>
</template>

<script setup>
// PersonaliseTab - the "Personalise" tab inside the Skills page (replaces the
// old BusinessTab; the "Go to Wiki" card is intentionally gone). Two nested
// sub-tabs:
//   Questions - a two-pane surface (left: the user's own question bank with
//     status pills / search / sort / Load more; right: the selected question's
//     "what Jarvis noticed" context, or a warm guidance card when nothing is
//     selected) with a bottom-pinned ChatComposer. Answering a question routes
//     to answer_question; free capture routes to save_note.
//   Notes - NotesView (F3), the user's captured notes.
//
// Capabilities (stt/admin-gate/unanswered count) come from get_skills_area_caps.
// SkillsPage (F1) may seed them via the `caps` prop to avoid a flash; the tab
// also refetches on mount so it is self-sufficient. The async "Added to your
// wiki" receipt rides the shared jarvis:event socket (personalise:processed),
// the same channel globalNotifier/ChatView subscribe to.
import { ref, reactive, computed, watch, nextTick, inject, onMounted, onBeforeUnmount } from "vue";
import { useStorage } from "@vueuse/core";
import {
	Breadcrumbs,
	Button,
	FeatherIcon,
	FormControl,
	LoadingIndicator,
	Tooltip,
	toast,
	confirmDialog,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import TabBar from "@/components/list/TabBar.vue";
import OriginBadge from "@/components/personalise/OriginBadge.vue";
import ChatComposer from "@/components/personalise/ChatComposer.vue";
import NotesView from "@/components/personalise/NotesView.vue";
import PersonalisationSettings from "@/components/personalise/PersonalisationSettings.vue";
import {
	getSkillsAreaCaps,
	listQuestionsPage,
	getQuestion,
	answerQuestion,
	ignoreQuestion,
	deleteQuestion,
	saveNote,
} from "@/api/personalise";
import { renderMarkdown } from "@/markdown";
import { timeAgo, exactDate } from "@/utils/datetime";
import { agentName } from "@/branding";

const PAGE = 20;

// Suggestion prompts (plain-text, from the old Business tab). Shown in the
// guidance card - click-to-answer lives on the generated questions instead.
const SUGGESTIONS = [
	"What does your business do, and who are your customers?",
	"How does your team use ERPNext day to day?",
	`Any customisations or quirks ${agentName} should know about`,
	"Your month-end rituals and recurring tasks",
	"Vendors or customers that need special handling",
	`What you'd love ${agentName} to handle for you`,
];

const STATUS_PILLS = [
	{ label: "Unanswered", value: "Unanswered" },
	{ label: "Answered", value: "Answered" },
	{ label: "Ignored", value: "Ignored" },
];

const props = defineProps({
	// optional seed from SkillsPage (F1); the tab refetches caps on mount too.
	caps: { type: Object, default: () => ({}) },
});

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── caps ─────────────────────────────────────────────────────────────────────
const caps = reactive({
	personalise: false,
	wiki: false,
	analysis: false,
	review: false,
	stt_enabled: false,
	unanswered_count: 0,
	// total non-deleted questions across all statuses - lets the empty state
	// tell a brand-new user (never asked anything) apart from someone who
	// cleared their backlog (§16).
	questions_total: 0,
	personalise_enabled: true,
});
watch(
	() => props.caps,
	(v) => Object.assign(caps, v || {}),
	{ immediate: true }
);

async function loadCaps() {
	try {
		const c = await getSkillsAreaCaps();
		Object.assign(caps, c || {});
	} catch (e) {
		// non-fatal: the parent only mounts this after a desk-user probe passed;
		// the page stays usable (list + composer still work)
	}
}

// ── sub-tabs ─────────────────────────────────────────────────────────────────
const subTab = useStorage("jarvis-personalise-tab", "questions");
const settingsOpen = ref(false);
const subTabs = computed(() => [
	{
		label: "Questions",
		value: "questions",
		count: caps.unanswered_count > 0 ? caps.unanswered_count : null,
	},
	{ label: "Notes", value: "notes" },
]);
function setSubTab(v) {
	subTab.value = v;
	if (v === "questions" && !board.rows.length && !board.loading) fetchQuestions("reset");
}

// ── questions list (hand-rolled reqId guard, flat kwargs - NotesPane idiom) ───
const status = ref("Unanswered");
const search = ref("");
const sort = ref("newest");
const board = reactive({ rows: [], total: 0, hasMore: false, loading: false });
const selected = ref(null);
const acting = ref("");
const contextPane = ref(null);
let reqId = 0;

async function fetchQuestions(mode = "reset") {
	const my = ++reqId;
	board.loading = true;
	const start = mode === "more" ? board.rows.length : 0;
	try {
		const r = await listQuestionsPage({
			status: status.value,
			search: search.value.trim(),
			sort: sort.value,
			start,
			page_length: PAGE,
		});
		if (my !== reqId) return;
		const rows = r.rows || [];
		board.rows = mode === "more" ? [...board.rows, ...rows] : rows;
		board.total = r.total || 0;
		board.hasMore = !!r.has_more;
	} catch (e) {
		if (my === reqId) toast.error(errMsg(e));
	} finally {
		if (my === reqId) board.loading = false;
	}
}

function setStatus(v) {
	if (status.value === v) return;
	status.value = v;
	fetchQuestions("reset");
}
function toggleSort() {
	sort.value = sort.value === "newest" ? "oldest" : "newest";
	fetchQuestions("reset");
}

let searchTimer = null;
watch(search, () => {
	clearTimeout(searchTimer);
	searchTimer = setTimeout(() => fetchQuestions("reset"), 300);
});

// ── row actions ──────────────────────────────────────────────────────────────
function selectQuestion(row) {
	selected.value = row;
	nextTick(() => {
		composer.value?.focus?.();
		// On stacked (below xl) layouts the context pane is order-first'd to the
		// top of the scroll region; bring it into view so "what Jarvis noticed"
		// is visible while the (always-visible, bottom-pinned) composer is used.
		if (window.innerWidth < 1280)
			contextPane.value?.scrollIntoView?.({ behavior: "smooth", block: "start" });
	});
}

async function ignore(row) {
	acting.value = row.name + ":ignore";
	try {
		await ignoreQuestion(row.name);
		toast.success("Set aside — you can still answer it later");
		if (selected.value?.name === row.name) selected.value = null;
		fetchQuestions("reset");
		loadCaps();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acting.value = "";
	}
}

function confirmDelete(row) {
	confirmDialog({
		title: "Stop asking this?",
		message: `${agentName} will stop asking this question. This can't be undone.`,
		onConfirm: async ({ hideDialog }) => {
			try {
				await deleteQuestion(row.name);
				if (selected.value?.name === row.name) selected.value = null;
				toast.success("Removed");
				hideDialog();
				fetchQuestions("reset");
				loadCaps();
			} catch (e) {
				toast.error(errMsg(e));
			}
		},
	});
}

// ── composer submit ──────────────────────────────────────────────────────────
const composer = ref(null);
const submitting = ref(false);

async function onSubmit(payload) {
	submitting.value = true;
	const wasQuestion = !!selected.value;
	try {
		if (wasQuestion) await answerQuestion({ name: selected.value.name, ...payload });
		else await saveNote({ ...payload, source: "Personalise" });
		composer.value?.clear?.();
		toast.success(`Saved — ${agentName} will use this`);
		// answered questions flip to the Answered filter; drop back to free capture
		selected.value = null;
		if (wasQuestion) fetchQuestions("reset");
		loadCaps();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		submitting.value = false;
	}
}

// ── notes → re-answer hand-off (NotesView emits @reanswer) ───────────────────
// F3's NotesView "Re-answer" emits just the originating question's docname. We
// fetch the FULL row directly via get_question (not a paginated list lookup, so
// it works no matter which page/sort the question would land on), then switch to
// the Questions view and select it. A DoesNotExist-shaped failure means the
// question was deleted/gone - toast and STAY on Notes rather than stranding the
// user on an unanswerable composer.
function isDoesNotExist(e) {
	return !!(e && (e.status === 404 || e.exc_type === "DoesNotExistError"));
}
async function onReanswer(payload) {
	const name = typeof payload === "string" ? payload : payload && payload.name;
	if (!name) return;
	let full;
	try {
		full = await getQuestion(name);
	} catch (e) {
		if (isDoesNotExist(e)) toast.error("This question is no longer available");
		else toast.error(errMsg(e));
		return; // stay on the Notes view
	}
	subTab.value = "questions";
	selected.value = full;
	// only switch the status filter (and refetch the list) if the fetched row's
	// status differs from what's showing, so the row appears/highlights in the list
	if (full.status && full.status !== status.value) {
		status.value = full.status;
		await fetchQuestions("reset");
		const row = board.rows.find((r) => r.name === full.name);
		if (row) selected.value = row;
	}
	nextTick(() => composer.value?.focus?.());
}

// ── refresh (whichever sub-view is active) ───────────────────────────────────
const notesView = ref(null);
const refreshing = ref(false);
async function refreshAll() {
	refreshing.value = true;
	try {
		await loadCaps();
		if (subTab.value === "questions") await fetchQuestions("reset");
		else await notesView.value?.reload?.();
	} finally {
		refreshing.value = false;
	}
}

// ── async "Added to your wiki" receipt (personalise:processed) ────────────────
const socket = inject("$socket", null);
function onEvent(p) {
	if (!p || p.kind !== "personalise:processed") return;
	const pages = Array.isArray(p.pages) ? p.pages : [];
	const titles = pages.map((x) => x && x.title).filter(Boolean);
	toast.info(
		titles.length
			? `Added to your wiki: ${titles.join(", ")}`
			: `${agentName} finished processing your note`
	);
}

// ── derived ──────────────────────────────────────────────────────────────────
const contextHtml = computed(() =>
	selected.value?.context_md ? renderMarkdown(selected.value.context_md) : ""
);

const emptyState = computed(() => {
	if (search.value.trim())
		return {
			icon: "search",
			title: "No questions match your search",
			body: "Try a different word, or clear the search to see everything.",
		};
	if (status.value === "Unanswered") {
		// a brand-new user who has never had ANY question (total across all
		// statuses is 0) gets a genuine first-run welcome, not a "cleared the
		// backlog" pat on the back (§16)
		if (caps.questions_total === 0)
			return {
				icon: "message-circle",
				title: `${agentName} hasn't asked anything yet`,
				body: `Questions appear here as ${agentName} learns how you work. You don't have to wait — tell it anything below.`,
			};
		return {
			icon: "check-circle",
			title: "You're all caught up",
			body: `${agentName} will ask when it learns something new. You can still tell it anything below.`,
		};
	}
	if (status.value === "Answered")
		return {
			icon: "message-square",
			title: "No answered questions yet",
			body: "When you answer a question, it moves here.",
		};
	return {
		icon: "moon",
		title: "Nothing set aside",
		body: "Questions you ignore wait here — you can still answer them anytime.",
	};
});

// ── init ─────────────────────────────────────────────────────────────────────
onMounted(() => {
	loadCaps();
	fetchQuestions("reset");
	socket?.on("jarvis:event", onEvent);
});
onBeforeUnmount(() => {
	socket?.off("jarvis:event", onEvent);
	clearTimeout(searchTimer);
});
</script>
