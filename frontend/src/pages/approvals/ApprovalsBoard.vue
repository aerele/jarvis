<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs
					:items="[{ label: 'Approval Board', route: { name: 'ApprovalsList' } }]"
				/>
			</template>
		</LayoutHeader>

		<!-- toolbar (§15.2): search · status · type facets · Refresh - no
		     Filter/Sort/Columns; the split view IS the workflow -->
		<div class="flex items-center justify-between gap-2 border-b px-5 py-3">
			<div class="flex flex-1 items-center gap-2 overflow-x-auto py-0.5">
				<div class="w-60 shrink-0">
					<FormControl
						type="text"
						placeholder="Search approvals"
						:modelValue="search"
						@update:modelValue="onSearch"
					/>
				</div>
				<div class="w-36 shrink-0">
					<FormControl
						type="select"
						:options="STATUS_OPTIONS"
						:modelValue="filters.status || 'Pending'"
						@update:modelValue="(v) => setQuick('status', v)"
					/>
				</div>
				<div class="w-44 shrink-0">
					<FormControl
						type="select"
						:options="typeOptions"
						:modelValue="filters.document_type || ''"
						@update:modelValue="(v) => setQuick('document_type', v)"
					/>
				</div>
			</div>
			<Button
				:tooltip="'Refresh'"
				icon="refresh-cw"
				:loading="loading"
				@click="resetLoad()"
			/>
		</div>

		<div class="flex min-h-0 flex-1">
			<!-- LEFT rail: inbox-style rows on a standing gray-1 surface so the
			     selected row's white chip + shadow reads in light mode (CRM
			     sidebar pattern; dark gray-1 ≠ surface-selected per §2.5) -->
			<div class="w-[360px] shrink-0 overflow-y-auto border-r bg-surface-gray-1">
				<!-- "Waiting for your reply" strip (notify-approvals Part 3): prose
				     questions the agent asked in chat — derived server-side
				     (envelope.awaiting_reply, page 1 only), no approval row behind
				     them, so the answer happens in the conversation. Hidden when
				     empty; refreshes with every normal list refresh. -->
				<div v-if="awaitingReply.length" class="border-b">
					<div
						class="px-4 pb-1 pt-3 text-2xs font-medium uppercase tracking-wide text-ink-gray-4"
					>
						Waiting for your reply
					</div>
					<div class="flex flex-col divide-y">
						<button
							v-for="w in awaitingReply"
							:key="w.conversation"
							class="flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-surface-gray-2"
							@click="openConversation(w.conversation)"
						>
							<FeatherIcon
								name="message-circle"
								class="mt-1 size-3.5 shrink-0 text-ink-gray-5"
							/>
							<div class="min-w-0 flex-1">
								<div class="truncate text-base text-ink-gray-9">
									{{ w.title || w.conversation }}
								</div>
								<div class="mt-0.5 truncate text-sm text-ink-gray-6">
									{{ w.question_excerpt }}
								</div>
								<div class="mt-1 flex items-center gap-2">
									<Tooltip :text="exactDate(w.last_at)">
										<span class="whitespace-nowrap text-sm text-ink-gray-5">{{
											timeAgo(w.last_at)
										}}</span>
									</Tooltip>
								</div>
							</div>
						</button>
					</div>
				</div>
				<template v-if="railRows.length">
					<div class="flex flex-col divide-y">
						<button
							v-for="row in railRows"
							:key="row.name"
							class="flex w-full items-start gap-3 px-4 py-3 text-left"
							:class="
								row.name === selectedId
									? 'bg-surface-selected shadow-sm'
									: 'hover:bg-surface-gray-2'
							"
							@click="onRowClick(row)"
						>
							<div class="min-w-0 flex-1">
								<div class="truncate text-base text-ink-gray-9">
									{{ row.title || row.name }}
								</div>
								<div class="mt-1 flex items-center gap-2">
									<!-- source: Chat blue / File Box gray (NULL = File Box) -->
									<Badge
										variant="subtle"
										:theme="sourceOf(row) === 'Chat' ? 'blue' : 'gray'"
										:label="sourceOf(row)"
									/>
									<Badge variant="subtle" theme="gray" :label="docType(row)" />
									<Tooltip v-if="row.shared" text="Shared with you for review">
										<Badge variant="subtle" theme="blue" label="Shared" />
									</Tooltip>
									<Tooltip :text="exactDate(row.creation)">
										<span class="whitespace-nowrap text-sm text-ink-gray-5">{{
											timeAgo(row.creation)
										}}</span>
									</Tooltip>
								</div>
							</div>
							<Badge
								class="mt-0.5 shrink-0"
								variant="subtle"
								:theme="STATUS_THEME[row.status] || 'gray'"
								:label="row.status"
							/>
						</button>
					</div>
					<div class="flex items-center justify-between gap-2 border-t px-4 py-2">
						<Button
							v-if="hasMore"
							variant="ghost"
							label="Load More"
							:loading="loading"
							@click="loadMore()"
						/>
						<div v-else />
						<div class="text-sm text-ink-gray-5">
							{{ railRows.length }} of {{ railTotal }}
						</div>
					</div>
				</template>
				<!-- h-full centering only when the strip isn't occupying the column —
				     otherwise the 100%-height block would force the rail to scroll -->
				<div
					v-else-if="!loading"
					class="flex flex-col items-center justify-center gap-3 px-6 text-center"
					:class="awaitingReply.length ? 'py-16' : 'h-full'"
				>
					<FeatherIcon :name="emptyState.icon" class="size-7.5 text-ink-gray-5" />
					<div class="flex flex-col items-center gap-1">
						<span class="text-lg font-medium text-ink-gray-8">{{
							emptyState.title
						}}</span>
						<span class="text-p-base text-ink-gray-6">{{
							emptyState.description
						}}</span>
					</div>
				</div>
				<div
					v-else
					class="flex items-center justify-center"
					:class="awaitingReply.length ? 'py-16' : 'h-full'"
				>
					<JvSpinner />
				</div>
			</div>

			<!-- RIGHT pane: review + act on the selected approval -->
			<div class="flex-1 overflow-y-auto">
				<div
					v-if="!selectedId"
					class="flex h-full flex-col items-center justify-center gap-3 px-8 text-center"
				>
					<FeatherIcon name="inbox" class="size-7.5 text-ink-gray-5" />
					<div class="flex flex-col items-center gap-1">
						<span class="text-lg font-medium text-ink-gray-8">Select an approval</span>
						<span class="text-p-base text-ink-gray-6">
							Pick a request from the list to review it, decide, or tag someone in
							the comments.
						</span>
					</div>
				</div>
				<div
					v-else-if="paneError"
					class="flex h-full flex-col items-center justify-center gap-1 px-8 text-center"
				>
					<div class="text-lg font-medium text-ink-gray-8">Approval not found</div>
					<div class="text-p-base text-ink-gray-6">{{ paneError }}</div>
				</div>
				<div v-else-if="!selected" class="flex h-full items-center justify-center">
					<JvSpinner />
				</div>
				<div v-else class="mx-auto w-full max-w-3xl px-8 py-6">
					<!-- 1. title row -->
					<div class="flex items-center gap-3">
						<h1 class="min-w-0 flex-1 truncate text-xl font-semibold text-ink-gray-9">
							{{ selected.title || selected.name }}
						</h1>
						<Badge
							variant="subtle"
							:theme="STATUS_THEME[selected.status] || 'gray'"
							:label="selected.status"
						/>
						<Tooltip v-if="selected.shared" text="Shared with you for review">
							<Badge variant="subtle" theme="blue" label="Shared" />
						</Tooltip>
						<Button
							v-if="selected.conversation"
							variant="subtle"
							label="Open Chat"
							iconLeft="message-circle"
							@click="openChat"
						/>
					</div>

					<!-- 2. question + meta rows -->
					<div class="mt-4 text-base text-ink-gray-8">
						{{ selected.question || selected.title }}
					</div>
					<div class="mt-4 flex flex-col gap-3">
						<div class="flex items-center gap-2 leading-5">
							<div class="w-[35%] min-w-20 shrink-0 text-sm text-ink-gray-5">
								Type
							</div>
							<div class="flex w-[65%] items-center text-base">
								<Badge variant="subtle" theme="gray" :label="selectedDocType" />
							</div>
						</div>
						<div
							v-if="selected.ref_doctype && selected.ref_name"
							class="flex items-center gap-2 leading-5"
						>
							<div class="w-[35%] min-w-20 shrink-0 text-sm text-ink-gray-5">
								Reference
							</div>
							<div class="flex w-[65%] items-center text-base">
								<a
									:href="refUrl"
									target="_blank"
									rel="noopener"
									class="flex min-w-0 items-center gap-1 text-ink-gray-8 hover:underline"
								>
									<span class="truncate"
										>{{ selected.ref_doctype }} {{ selected.ref_name }}</span
									>
									<FeatherIcon
										name="external-link"
										class="size-3.5 shrink-0 text-ink-gray-5"
									/>
								</a>
							</div>
						</div>
						<div class="flex items-center gap-2 leading-5">
							<div class="w-[35%] min-w-20 shrink-0 text-sm text-ink-gray-5">
								Created
							</div>
							<div class="flex w-[65%] items-center text-base text-ink-gray-8">
								<Tooltip :text="exactDate(selected.creation)">
									<span>{{ timeAgo(selected.creation) }}</span>
								</Tooltip>
							</div>
						</div>
						<div
							v-if="selected.conversation"
							class="flex items-center gap-2 leading-5"
						>
							<div class="w-[35%] min-w-20 shrink-0 text-sm text-ink-gray-5">
								Conversation
							</div>
							<div class="flex w-[65%] items-center text-base">
								<router-link
									:to="'/c/' + selected.conversation"
									class="inline-flex h-6 min-w-0 items-center gap-1.5 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8 hover:bg-surface-gray-3"
								>
									<FeatherIcon
										name="message-circle"
										class="size-3.5 shrink-0 text-ink-gray-5"
									/>
									<span class="truncate">{{ selected.conversation }}</span>
								</router-link>
							</div>
						</div>
					</div>

					<div class="mt-4">
						<!-- 3. context -->
						<DocSection v-if="selected.context_md" label="Context">
							<!-- O1: renderMarkdown from @/markdown (escapes HTML first - safe) -->
							<div class="prose prose-sm max-w-none" v-html="contextHtml" />
						</DocSection>

						<!-- 4. decision -->
						<DocSection label="Decision" :collapsible="false">
							<template v-if="selected.status === 'Pending'">
								<div v-if="selected.can_act">
									<!-- the LLM's options are varied per chat - selectable chips;
									     hidden when they merely restate Approve/Reject -->
									<div v-if="showOptionChips" class="flex flex-wrap gap-2">
										<Button
											v-for="opt in options"
											:key="opt"
											:label="opt"
											:variant="selectedOption === opt ? 'solid' : 'subtle'"
											@click="toggleOption(opt)"
										/>
									</div>
									<FormControl
										v-if="!chatAnswerOnly"
										type="textarea"
										:class="showOptionChips ? 'mt-3' : ''"
										placeholder="Add a note - optional"
										:modelValue="note"
										@update:modelValue="(v) => (note = v)"
									/>
									<div
										class="flex items-center gap-2"
										:class="chatAnswerOnly ? '' : 'mt-3'"
									>
										<!-- chat asks without options are free-form questions: a
										     verdict decide() would be meaningless — answer in the
										     conversation instead (decide resumes chat only for
										     options-bearing rows) -->
										<Button
											v-if="chatAnswerOnly"
											variant="solid"
											label="Answer in chat"
											iconLeft="message-circle"
											@click="openChat"
										/>
										<template v-else>
											<Button
												variant="solid"
												theme="green"
												label="Approve"
												:loading="deciding === 1"
												:disabled="deciding !== null"
												@click="submitDecide(1)"
											/>
											<Button
												variant="subtle"
												theme="red"
												label="Reject"
												:loading="deciding === 0"
												:disabled="deciding !== null"
												@click="submitDecide(0)"
											/>
										</template>
										<!-- Ignore: clear it off the board without acting — no
										     verdict, no chat resume; reversible via Restore -->
										<Button
											variant="ghost"
											label="Ignore"
											iconLeft="bell-off"
											:tooltip="'Dismiss without acting — the assistant is not told anything'"
											:loading="dismissing"
											:disabled="deciding !== null"
											@click="submitDismiss"
										/>
										<div class="flex-1" />
										<!-- tag for review - the DocShare path DocMetaPanel's
										     "Shared with" block uses (same docmeta object, so the
										     Record details block stays in sync) -->
										<Popover placement="bottom-end">
											<template #target="{ togglePopover }">
												<Button
													variant="subtle"
													size="sm"
													label="Tag for review"
													iconLeft="user-plus"
													:tooltip="'Share this approval so a colleague can review it'"
													@click="openTagPicker(togglePopover)"
												/>
											</template>
											<template #body>
												<div
													class="my-2 w-[320px] rounded-lg bg-surface-modal p-3 shadow-2xl ring-1 ring-black ring-opacity-5"
												>
													<div
														v-if="shares.length"
														class="mb-2 flex flex-wrap gap-1.5"
													>
														<div
															v-for="s in shares"
															:key="s.user"
															class="flex h-6 items-center gap-1 rounded bg-surface-gray-2 px-2 text-sm text-ink-gray-8"
														>
															<span class="truncate">{{
																s.full_name || s.user
															}}</span>
															<Button
																variant="ghost"
																icon="x"
																class="!h-4 !w-4"
																@click="
																	docmeta.toggleShare(
																		s.user,
																		'remove'
																	)
																"
															/>
														</div>
													</div>
													<Autocomplete
														:options="tagOptions"
														:modelValue="null"
														placeholder="Tag a colleague…"
														@update:modelValue="
															(opt) => opt && addTag(opt)
														"
													/>
												</div>
											</template>
										</Popover>
									</div>
									<div class="mt-2 text-sm text-ink-gray-5">
										{{
											chatAnswerOnly
												? "This question needs a longer answer — continue in chat."
												: "Tag a colleague to view and comment — only you (or an admin) can approve."
										}}
									</div>
								</div>
								<div v-else class="text-sm text-ink-gray-5">
									Waiting for a decision - comment below and @mention someone who
									can approve.
								</div>
							</template>
							<template v-else>
								<div class="text-base text-ink-gray-8">
									{{ selected.decision }}
								</div>
								<div v-if="decidedLine" class="mt-2 text-sm text-ink-gray-5">
									{{ decidedLine }}
								</div>
								<!-- undo an accidental Ignore: back to Pending, nothing was
								     ever told to the assistant -->
								<Button
									v-if="selected.status === 'Dismissed' && selected.can_act"
									class="mt-3"
									variant="subtle"
									label="Restore to board"
									iconLeft="rotate-ccw"
									:loading="restoring"
									@click="submitRestore"
								/>
							</template>
						</DocSection>

						<!-- 5. comments - the "tag someone to approve" surface -->
						<div class="border-t py-4">
							<CommentsSection :docmeta="docmeta" :can-comment="true" />
						</div>

						<!-- 6. doc-parity block, collapsed by default -->
						<DocSection label="Record details" :opened="false">
							<div class="overflow-hidden rounded-lg border">
								<DocMetaPanel :docmeta="docmeta" :can-write="!!selected.can_act" />
							</div>
						</DocSection>
					</div>
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
// Approval Board - two-pane master-detail (DESIGN-V3 §15.2, supersedes
// §5.8/§6.4). LEFT: envelope-fed inbox rail (search/status/type facets,
// Load More + "N of M", auto-select first row). RIGHT: the action pane for
// the selected approval - question + meta, Context markdown, varied option
// chips + note + Approve/Reject, CommentsSection (mentions), collapsed
// DocMetaPanel. Row click → router.replace('/approvals/'+id); both approval
// routes render this board.
// notify-approvals Part 3: source badge on rail rows (Chat blue / File Box
// gray; NULL = File Box), a "Waiting for your reply" strip above the rail
// (envelope.awaiting_reply — prose questions with no row behind them),
// chat-sourced option-less rows hand off to the conversation ("Answer in
// chat") instead of Approve/Reject, and the status filter gains "Answered".
import { ref, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
	Autocomplete,
	Badge,
	Breadcrumbs,
	Button,
	FeatherIcon,
	FormControl,
	Popover,
	Tooltip,
	toast,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import DocSection from "@/components/doc/DocSection.vue";
import DocMetaPanel from "@/components/doc/DocMetaPanel.vue";
import CommentsSection from "@/components/doc/CommentsSection.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import { useDocmeta } from "@/composables/useDocmeta";
import { useListPage } from "@/composables/useListPage";
import { useShellStore } from "@/stores/shell";
import { session } from "@/data/session";
import { timeAgo, exactDate } from "@/utils/datetime";
import { getApproval } from "@/api/approvals";
import * as api from "@/api";
import { renderMarkdown } from "@/markdown";
import { errMessage as errMsg } from "@/lib/errors";

const route = useRoute();
const router = useRouter();
const store = useShellStore();

// ── rail config ──────────────────────────────────────────────────────────────
// "Answered" = a chat ask the user resolved IN CHAT (chat_asks.resolve_on_
// user_message) — filterable for audit; the sidebar badge stays Pending-only.
const STATUS_THEME = {
	Pending: "orange",
	Approved: "green",
	Rejected: "red",
	Answered: "blue",
	Dismissed: "gray",
};
const STATUS_OPTIONS = [
	{ label: "Pending", value: "Pending" },
	{ label: "Decided", value: "Decided" },
	{ label: "Answered", value: "Answered" },
	{ label: "Dismissed", value: "Dismissed" },
	{ label: "All", value: "All" },
];
const STATUS_VALUES = ["Pending", "Decided", "Answered", "Dismissed", "All"];
const DEFAULT_SORT = { field: "creation", dir: "desc" };

// deep-link seeds (?status=, ?type= - parity with the old list page)
const initialStatus = STATUS_VALUES.includes(route.query.status) ? route.query.status : "Pending";
const initialType = typeof route.query.type === "string" ? route.query.type : "";

// ── awaiting-reply strip (notify-approvals Part 3) ───────────────────────────
// Prose questions never create approval rows — the server derives them into
// the envelope (`awaiting_reply`, first page only). Captured via a fetchFn tap
// so the strip refreshes with every normal list refresh (reset/keep both hit
// start=0, and replacing with an empty array hides the strip); Load More
// responses don't carry the key and leave it alone.
const awaitingReply = ref([]);
let awaitReq = 0; // monotonic — stale responses dropped (paneReq idiom)
async function fetchApprovals(p) {
	const id = ++awaitReq;
	const res = (await api.listApprovalsPage(p)) || {};
	if (id === awaitReq && Array.isArray(res.awaiting_reply))
		awaitingReply.value = res.awaiting_reply;
	return res;
}

const {
	rows,
	total,
	hasMore,
	loading,
	facets,
	search,
	filters,
	setFilters,
	resetLoad,
	loadMore,
	refreshKeep,
} = useListPage({
	fetchFn: fetchApprovals,
	defaultSort: DEFAULT_SORT,
	storageKey: "approvals",
	initialFilters: {
		status: initialStatus,
		...(initialType ? { document_type: initialType } : {}),
	},
});

// composable debounces search → resetLoad; the input just writes the ref
function onSearch(v) {
	search.value = v;
}

// document_type quick filter options from the page-1 facets ("Type (N)";
// the server pre-labels blank types as "Unclassified")
const typeOptions = computed(() => {
	const opts = [{ label: "All types", value: "" }];
	const facetRows = (facets.value && facets.value.document_type) || [];
	if (facetRows.length) {
		for (const f of facetRows) opts.push({ label: `${f.value} (${f.count})`, value: f.value });
	} else if (initialType) {
		// keep the deep-linked value selectable before facets arrive
		opts.push({ label: initialType, value: initialType });
	}
	return opts;
});

const emptyState = computed(() => {
	if ((filters.status || "Pending") === "Pending") {
		return {
			icon: "check-square",
			title: "No pending approvals",
			description: "Approval requests from File Box and chat will appear here.",
		};
	}
	return {
		icon: "check-square",
		title: "No approvals found",
		description: "Try a different status or type filter.",
	};
});

function setQuick(key, value) {
	const next = { ...filters };
	if (value === "" || value == null) delete next[key];
	else next[key] = value;
	// status always travels explicitly (server defaults to Pending otherwise)
	if (!next.status) next.status = "Pending";
	setFilters(next);
	syncQuery();
}
// keep ?status=/?type= in the URL so deep links preserve the view (D32);
// Pending is the default - keep the URL clean for it.
function syncQuery() {
	const q = { ...route.query };
	const status = filters.status || "Pending";
	if (status !== "Pending") q.status = status;
	else delete q.status;
	if (filters.document_type) q.type = filters.document_type;
	else delete q.type;
	router.replace({ query: q });
}

function docType(row) {
	return (row.document_type || "").trim() || "Unclassified";
}

// NULL/absent source predates the field — reads as File Box (backend contract)
function sourceOf(row) {
	return row && row.source === "Chat" ? "Chat" : "File Box";
}

// ── selection (right pane always reads the full record via get_approval,
//    incl. can_act - the rail row is never enough) ─────────────────────────────
const selectedId = ref("");
const selected = ref(null);
const paneError = ref("");
let paneReq = 0; // monotonic - stale responses dropped

const docmeta = useDocmeta("Jarvis Approval Request", selectedId);

// deep-link seed: a routed :id beyond the loaded page still gets a rail row,
// built from the get_approval payload and pinned on top. The computed drops
// the seed the moment the real row shows up (page 1 or Load More) - dedupe
// for free - and never mutates `rows`, so Load More's start offset stays true.
const seedTargetId = typeof route.params.id === "string" ? route.params.id : "";
let seedWanted = !!seedTargetId;
const seedRow = ref(null);
const railRows = computed(() => {
	const seed = seedRow.value;
	if (!seed || rows.value.some((r) => r.name === seed.name)) return rows.value;
	return [seed, ...rows.value];
});
// the seed sits outside the server's filtered total - count it explicitly
const railTotal = computed(() => total.value + (railRows.value.length - rows.value.length));

function select(id) {
	if (!id || (id === selectedId.value && !paneError.value)) return;
	// the seed row only earns its place while it is the selection
	if (seedRow.value && seedRow.value.name !== id) seedRow.value = null;
	selectedId.value = id;
	selected.value = null;
	paneError.value = "";
	selectedOption.value = "";
	note.value = "";
	loadRecord(id);
}

async function loadRecord(id, { keep = false } = {}) {
	const rid = ++paneReq;
	try {
		const rec = (await getApproval(id)) || null;
		if (rid !== paneReq) return;
		selected.value = rec;
		paneError.value = "";
		if (seedWanted && rec && rec.name === seedTargetId) {
			seedWanted = false;
			seedRow.value = {
				name: rec.name,
				title: rec.title,
				status: rec.status,
				source: rec.source || "File Box",
				document_type: rec.document_type || "",
				creation: rec.creation,
			};
		}
	} catch (e) {
		if (rid !== paneReq) return;
		// a keep-reload (post-decide) failing must not blank the pane
		if (!keep) {
			selected.value = null;
			paneError.value = errMsg(e);
		}
	}
}

function onRowClick(row) {
	select(row.name);
	// replace, not push - selection must not spam browser history
	router.replace({ name: "ApprovalDetail", params: { id: row.name }, query: route.query });
}

function openChat() {
	if (selected.value && selected.value.conversation) {
		router.push("/c/" + selected.value.conversation);
	}
}

// awaiting-reply strip rows answer in the conversation itself
function openConversation(conversation) {
	if (conversation) router.push("/c/" + conversation);
}

// ── right-pane helpers ────────────────────────────────────────────────────────
const selectedDocType = computed(() => {
	const dt = selected.value && selected.value.document_type;
	return (dt || "").trim() || "Unclassified";
});
const refUrl = computed(() => {
	if (!selected.value || !selected.value.ref_doctype || !selected.value.ref_name) return "";
	const dt = selected.value.ref_doctype.toLowerCase().replace(/ /g, "-");
	return `/app/${dt}/${encodeURIComponent(selected.value.ref_name)}`;
});
const contextHtml = computed(() =>
	selected.value && selected.value.context_md ? renderMarkdown(selected.value.context_md) : ""
);
// options may arrive parsed (list) or as the raw JSON string - be defensive
const options = computed(() => {
	const raw = selected.value && selected.value.options;
	if (Array.isArray(raw)) return raw.map(String);
	if (typeof raw === "string" && raw.trim()) {
		try {
			const parsed = JSON.parse(raw);
			return Array.isArray(parsed) ? parsed.map(String) : [];
		} catch (e) {
			return [];
		}
	}
	return [];
});
// chips that only restate the Approve/Reject buttons are noise: a lone
// option, or exactly one approve-verb + one reject-verb (case-insensitive,
// "Approve as drafted"-style prefixes included). Genuinely varied sets
// (3+, or non-verb pairs like Yes/No) keep the chip row.
const showOptionChips = computed(() => {
	const opts = options.value;
	if (!opts.length) return false;
	if (opts.length === 1) return false;
	if (opts.length === 2) {
		const low = opts.map((o) => o.trim().toLowerCase());
		const approveish = low.filter((o) => /^approve(d)?\b/.test(o)).length;
		const rejectish = low.filter((o) => /^reject(ed)?\b/.test(o)).length;
		if (approveish === 1 && rejectish === 1) return false;
	}
	return true;
});
// Chat-sourced asks with NO options can't be verdict-decided — the answer is
// free-form prose, so the board hands off to the conversation ("Answer in
// chat"). Gate on options PRESENCE, not chip visibility: a chat row whose two
// options merely restate Approve/Reject keeps the plain Approve/Reject
// buttons (decide() already resumes the conversation with the decision).
const chatAnswerOnly = computed(
	() => !!selected.value && sourceOf(selected.value) === "Chat" && !options.value.length
);
const decidedLine = computed(() => {
	const s = selected.value;
	if (!s || !s.decided_by) return "";
	const who = s.decided_by_name || s.decided_by;
	return "Decided by " + who + (s.decided_at ? " · " + timeAgo(s.decided_at) : "");
});

// ── tag for review (§15.2 addendum) ──────────────────────────────────────────
// Sharing IS tagging: same DocShare path as DocMetaPanel's "Shared with" block
// (docmeta.toggleShare), surfaced next to the Decision buttons so it's
// discoverable. The backend makes shared approvals visible on the tagged
// user's board; can_act stays owner/SM-only - tagged users view and comment.
const shares = computed(() => (docmeta.meta && docmeta.meta.shares) || []);
const shareUsers = ref([]);
let shareUsersLoaded = false;
async function loadShareUsers() {
	if (shareUsersLoaded) return;
	shareUsersLoaded = true;
	try {
		shareUsers.value = (await api.listShareableUsers()) || [];
	} catch (e) {
		shareUsersLoaded = false; // retry on next open
	}
}
function openTagPicker(togglePopover) {
	loadShareUsers();
	togglePopover();
}
// toggleShare returns true on success (errors toast inside the composable)
async function addTag(opt) {
	if (await docmeta.toggleShare(opt.value, "add")) {
		toast.success("Shared with " + (opt.label || opt.value));
	}
}
const tagOptions = computed(() => {
	const taken = new Set(shares.value.map((s) => s.user));
	if (docmeta.meta && docmeta.meta.created) taken.add(docmeta.meta.created.owner);
	return shareUsers.value
		.filter((u) => !taken.has(u.name))
		.map((u) => ({ label: u.full_name || u.name, value: u.name }));
});

// ── decide (§15.2 point 4) ────────────────────────────────────────────────────
const selectedOption = ref("");
const note = ref("");
const deciding = ref(null); // 1 | 0 | null
const dismissing = ref(false);
const restoring = ref(false);

function toggleOption(opt) {
	selectedOption.value = selectedOption.value === opt ? "" : opt;
}

async function submitDecide(approve) {
	if (deciding.value !== null || !selected.value) return;
	deciding.value = approve;
	const noteText = note.value.trim();
	// decide() requires non-empty decision text - Approve sends the selected
	// option chip, else the note, else the verdict word; Reject sends the note
	// or the verdict word (the picked option was what got refused)
	const text = approve ? selectedOption.value || noteText || "Approved" : noteText || "Rejected";
	const id = selected.value.name;
	try {
		const res = (await api.decideApproval(id, text, approve)) || {};
		const status = approve ? "Approved" : "Rejected";
		// optimistic: rail row swaps status, pane flips to the decided state
		const r = railRows.value.find((x) => x.name === id);
		if (r) {
			r.status = status;
			r.decision = text;
		}
		if (selected.value && selected.value.name === id) {
			selected.value = {
				...selected.value,
				status,
				decision: text,
				decided_by: session.user,
				decided_by_name: "",
				decided_at: "",
			};
		}
		selectedOption.value = "";
		note.value = "";
		toast.success(
			(approve ? "Approved" : "Rejected") + (res.resumed ? " - conversation resumed" : "")
		);
		store.refreshApprovalsCount();
		if ((filters.status || "Pending") === "Pending") {
			// the decided row no longer matches the Pending quick-filter -
			// drop it and move the selection along so triage keeps flowing
			advanceAfterDecide(id);
		} else {
			// authoritative decided_by/decided_at + the decide trace comment
			loadRecord(id, { keep: true });
			docmeta.reload();
		}
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		deciding.value = null;
	}
}

// Ignore = dismiss off the board with no verdict and no chat resume. Same
// optimistic swap as decide, but the row lands in "Dismissed".
async function submitDismiss() {
	if (dismissing.value || deciding.value !== null || !selected.value) return;
	dismissing.value = true;
	const id = selected.value.name;
	try {
		await api.dismissApproval(id);
		const r = railRows.value.find((x) => x.name === id);
		if (r) {
			r.status = "Dismissed";
			r.decision = "(dismissed - no action taken)";
		}
		if (selected.value && selected.value.name === id) {
			selected.value = {
				...selected.value,
				status: "Dismissed",
				decision: "(dismissed - no action taken)",
				decided_by: session.user,
				decided_by_name: "",
				decided_at: "",
			};
		}
		note.value = "";
		selectedOption.value = "";
		toast.success("Ignored — the assistant was not notified");
		store.refreshApprovalsCount();
		if ((filters.status || "Pending") === "Pending") advanceAfterDecide(id);
		else loadRecord(id, { keep: true });
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		dismissing.value = false;
	}
}

// Restore a dismissed request back to Pending (the undo).
async function submitRestore() {
	if (restoring.value || !selected.value) return;
	restoring.value = true;
	const id = selected.value.name;
	try {
		await api.restoreApproval(id);
		const r = railRows.value.find((x) => x.name === id);
		if (r) r.status = "Pending";
		toast.success("Restored to the board");
		store.refreshApprovalsCount();
		if ((filters.status || "Pending") === "Dismissed") advanceAfterDecide(id);
		else loadRecord(id, { keep: true });
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		restoring.value = false;
	}
}

// Remove a just-decided row from the Pending rail and select the next row
// (same index after removal), or clear the pane when the rail runs empty.
// Splicing `rows` keeps Load More's start offset aligned with the server,
// which no longer counts the row under the Pending filter either.
function advanceAfterDecide(id) {
	const at = railRows.value.findIndex((x) => x.name === id);
	const idx = rows.value.findIndex((x) => x.name === id);
	if (idx !== -1) {
		rows.value.splice(idx, 1);
		total.value = Math.max(0, total.value - 1);
	}
	if (seedRow.value && seedRow.value.name === id) seedRow.value = null;
	if (selectedId.value !== id) return;
	const rail = railRows.value;
	const next = at === -1 ? rail[0] : rail[Math.min(at, rail.length - 1)];
	if (next) {
		select(next.name);
		router.replace({ name: "ApprovalDetail", params: { id: next.name }, query: route.query });
	} else {
		paneReq++; // drop any in-flight load for the removed row
		selectedId.value = "";
		selected.value = null;
		paneError.value = "";
		router.replace({ name: "ApprovalsList", query: route.query });
	}
}

// ── selection wiring (after every ref it touches exists - the immediate
//    watcher fires during setup) ───────────────────────────────────────────────
// route :id → selection (deep links, back/forward); absent id keeps the
// current selection (breadcrumb click doesn't blank the pane)
watch(
	() => route.params.id,
	(id) => {
		if (typeof id === "string" && id) select(id);
	},
	{ immediate: true }
);
// auto-select the first row only when the route carries no :id
watch(rows, (r) => {
	if (!route.params.id && !selectedId.value && r.length) select(r[0].name);
});

// ── freshness: refetch on tab-visible (no realtime approval event today) ─────
function onVisibility() {
	if (document.visibilityState === "visible") refreshKeep();
}
onMounted(() => document.addEventListener("visibilitychange", onVisibility));
onBeforeUnmount(() => document.removeEventListener("visibilitychange", onVisibility));
</script>
