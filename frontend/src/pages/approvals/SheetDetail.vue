<template>
	<!-- One document's approval sheet in the board's right pane: every record and
	     question a File Box run needs, decided together with one Apply. Record
	     values and titles are model-derived: text interpolation only. -->
	<div :aria-busy="loading || busy !== null ? 'true' : 'false'">
		<div v-if="loading" class="flex justify-start">
			<JvSpinner :label="__('Loading the approval sheet…')" />
		</div>

		<div v-else-if="loadError" role="alert" class="flex items-center gap-3 text-sm">
			<span class="text-ink-red-4">{{ loadError }}</span>
			<Button variant="subtle" size="sm" :label="__('Try again')" @click="load" />
		</div>

		<template v-else-if="rec">
			<div class="flex flex-wrap items-center gap-x-3 gap-y-2">
				<p class="min-w-0 flex-1 text-sm text-ink-gray-6">
					<span v-if="showFileName" class="font-medium text-ink-gray-8">{{
						fileName
					}}</span
					>{{ headline }}
				</p>
				<Button
					v-if="rec.conversation && !rec.for_user"
					variant="subtle"
					size="sm"
					:label="__('Open chat')"
					iconLeft="message-circle"
					@click="openChat"
				/>
			</div>

			<div
				v-if="rec.collecting"
				role="status"
				class="mt-4 rounded bg-surface-gray-2 px-3 py-3 text-sm text-ink-gray-7"
			>
				<p class="font-medium text-ink-gray-8">{{ __("Still listing…") }}</p>
				<p class="mt-1">
					{{
						__(
							"Jarvis is still reading this document and adding what it needs to this sheet. You can review it once the run pauses."
						)
					}}
				</p>
			</div>

			<div v-else-if="rec.status === 'Executing'" class="mt-4">
				<p
					ref="progressEl"
					role="status"
					tabindex="-1"
					class="text-base text-ink-gray-8 outline-none"
				>
					{{ progressLine }}
				</p>
				<div
					class="mt-2 h-1 w-full overflow-hidden rounded-full bg-surface-gray-3"
					role="progressbar"
					:aria-label="__('Apply progress')"
					aria-valuemin="0"
					:aria-valuemax="progress && progress.total ? progress.total : undefined"
					:aria-valuenow="progress && progress.total ? progress.done || 0 : undefined"
				>
					<div
						class="h-full rounded-full bg-surface-gray-7"
						:style="{ width: pct + '%' }"
					/>
				</div>
				<p class="mt-2 text-sm text-ink-gray-5">
					{{
						__(
							"Every record is saved together, or none is. You can leave this page: the run continues once it's done."
						)
					}}
				</p>
				<p v-if="notice" role="alert" class="mt-2 text-sm text-ink-red-4">{{ notice }}</p>
			</div>

			<template v-else-if="ready">
				<p class="mt-3 text-sm text-ink-gray-6">
					{{
						__(
							"Jarvis needs these records and answers before it drafts this document. Nothing is created until you apply."
						)
					}}
				</p>

				<div
					v-if="banner"
					ref="bannerEl"
					role="alert"
					tabindex="-1"
					class="mt-3 rounded bg-surface-red-1 px-3 py-2 text-sm text-ink-red-4 outline-none"
				>
					<p>{{ banner }}</p>
					<p v-for="(m, i) in errs.sheet" :key="i" class="mt-1">{{ m }}</p>
				</div>

				<section
					v-for="s in view"
					:key="s.key"
					class="mt-4 border-t"
					:aria-labelledby="s.id + '-title'"
				>
					<div class="flex flex-wrap items-center gap-2 pt-3">
						<h3 :id="s.id + '-title'" class="min-w-0 flex-1">
							<button
								type="button"
								class="flex items-center gap-1.5 text-left"
								:aria-expanded="open[s.key] === false ? 'false' : 'true'"
								:aria-controls="s.id + '-body'"
								@click="open[s.key] = open[s.key] === false"
							>
								<FeatherIcon
									:name="
										open[s.key] === false ? 'chevron-right' : 'chevron-down'
									"
									class="size-3.5 shrink-0 text-ink-gray-5"
									aria-hidden="true"
								/>
								<span class="text-base font-medium text-ink-gray-9">{{
									s.label
								}}</span>
								<span class="text-sm text-ink-gray-5">{{ s.records.length }}</span>
							</button>
						</h3>
						<span v-if="s.attention" class="text-sm text-ink-amber-3">{{
							s.attention
						}}</span>
						<Button
							variant="ghost"
							size="sm"
							:label="s.updatesOnly ? __('Apply all') : __('Create all')"
							@click="bulk(s, 'default')"
						/>
						<Button
							variant="ghost"
							size="sm"
							:label="__('Skip all')"
							@click="bulk(s, 'skip')"
						/>
					</div>
					<div :id="s.id + '-body'">
						<template v-if="open[s.key] !== false">
							<SetForAll
								:sheet="name"
								:section="s.key"
								:groups="s.groups"
								@set="setForAll"
							/>
							<ul class="flex flex-col divide-y">
								<SheetRecord
									v-for="r in s.shown"
									:key="rec.card_sha256 + ':' + r.index"
									:sheet="name"
									:record="r"
									:choice="choices[r.index]"
									:issue="gate.records[r.index] || ''"
									:error="errs.records[r.index] || ''"
									:cascaded-by="skipped.has(r.index) ? skipped.get(r.index) : -1"
									:cascade-reason="
										skipped.has(r.index)
											? cascadeReason(r, choices, skipped)
											: ''
									"
									:cascade-count="cascadeCount(r)"
									:form-meta="formMeta"
									:load-candidates="loadCandidates"
									@choose="choose"
								/>
							</ul>
							<Button
								v-if="s.hidden"
								class="mt-1"
								variant="ghost"
								size="sm"
								:label="__('Show all {0}', [s.records.length])"
								@click="showAllOf(s)"
							/>
						</template>
					</div>
				</section>

				<SheetQuestions
					v-if="rec.questions.length"
					:sheet="name"
					:questions="rec.questions"
					:answers="answers"
					:errors="errs.questions"
					:expanded="open.questions !== false"
					@toggle="open.questions = open.questions === false"
					@pick="pickOption"
					@note="(q, v) => (answers[q.name].note = v)"
				/>

				<div class="sticky bottom-0 z-10 mt-6 border-t bg-surface-white py-3">
					<p v-if="skipAll" class="mb-2 text-sm text-ink-gray-6">
						{{ __("You chose Skip this file: nothing on this sheet is created.") }}
					</p>
					<p v-else-if="skipped.size" class="mb-2 text-sm text-ink-gray-6">
						{{ cascadeNote(skipped.size) }}
					</p>
					<div class="flex flex-wrap items-center gap-2">
						<Button
							variant="solid"
							:label="__('Apply & continue')"
							:loading="busy === 'apply'"
							:disabled="busy !== null || !!gate.summary"
							:aria-describedby="gate.summary ? reasonId : undefined"
							@click="apply"
						/>
						<Button
							variant="ghost"
							theme="red"
							:label="__('Skip this file')"
							:loading="busy === 'discard'"
							:disabled="busy !== null"
							@click="confirmSkip"
						/>
					</div>
					<p v-if="gate.summary" :id="reasonId" class="mt-2 text-sm text-ink-amber-3">
						{{ gate.summary }}.
						<button type="button" class="ml-1 underline" @click="goTo(gate.first)">
							{{ __("Show the first") }}
						</button>
					</p>
					<p v-if="notice" role="alert" class="mt-2 text-sm text-ink-red-4">
						{{ notice }}
					</p>
				</div>
			</template>

			<!-- Pending but its seal can't be read back: nothing to review, only these -->
			<div v-else-if="rec.status === 'Pending'" class="mt-4">
				<p role="status" class="text-base text-ink-gray-8">{{ closedLine }}</p>
				<div class="mt-3 flex flex-wrap items-center gap-2">
					<Button
						variant="subtle"
						:label="__('Try again')"
						:disabled="busy !== null"
						@click="load"
					/>
					<Button
						variant="ghost"
						theme="red"
						:label="__('Skip this file')"
						:loading="busy === 'discard'"
						:disabled="busy !== null"
						@click="confirmSkip"
					/>
				</div>
				<p v-if="notice" role="alert" class="mt-2 text-sm text-ink-red-4">{{ notice }}</p>
			</div>

			<template v-else>
				<p
					ref="statusEl"
					role="status"
					tabindex="-1"
					class="mt-4 text-base text-ink-gray-8 outline-none"
				>
					{{ closedLine }}
				</p>
				<template v-if="outcome.rows.length || outcome.answers.length">
					<p v-if="outcome.line" class="mt-1 text-sm text-ink-gray-6">
						{{ outcome.line }}
					</p>
					<ul
						v-if="outcome.rows.length"
						class="mt-3 flex max-h-[50vh] flex-col divide-y overflow-auto rounded border"
						:aria-label="__('What happened to each record')"
					>
						<li
							v-for="row in outcome.rows"
							:key="row.key"
							class="px-3 py-2 text-sm text-ink-gray-8"
						>
							{{ row.text }}
						</li>
					</ul>
					<dl v-if="outcome.answers.length" class="mt-3 flex flex-col gap-2 text-sm">
						<div v-for="a in outcome.answers" :key="a.key">
							<dt class="text-ink-gray-5">{{ a.question }}</dt>
							<dd class="text-ink-gray-8">{{ a.answer }}</dd>
						</div>
					</dl>
				</template>
				<Button
					v-if="unreported"
					class="mt-4"
					variant="solid"
					:label="__('Done')"
					@click="settle(unreported)"
				/>
			</template>
		</template>
	</div>
</template>

<script setup>
// Detail + decision for one File Box approval sheet (T3, S3). Apply sends every
// decision with the sheet's card_sha256 + record_count; the server checks it all,
// then applies in the background (sheet:progress realtime, a poll as fallback).
// onDecided fires once the sheet ends while open here: an applied one once the
// applier has read its outcome (Done, or leaving it).
import { ref, reactive, computed, nextTick, inject, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { Button, FeatherIcon, confirmDialog, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import SetForAll from "@/pages/approvals/SetForAll.vue";
import SheetQuestions from "@/pages/approvals/SheetQuestions.vue";
import SheetRecord from "@/pages/approvals/SheetRecord.vue";
import {
	getPendingAction,
	getSheetStatus,
	getSheetCandidates,
	applySheet,
	discardSheet,
} from "@/api/approvals";
import { getDoctypeFormMeta } from "@/api";
import { errMessage, escapeHtml } from "@/lib/errors";
import { __ } from "@/lib/i18n";
import {
	PREVIEW,
	actionsFor,
	blockers,
	buildDecisions,
	cascade,
	cascadeNote,
	cascadeReason,
	fixOf,
	initialAnswers,
	initialChoices,
	isSheetSettled,
	isTerminal,
	outcomeSummary,
	progressText,
	reachFrom,
	sections,
	setForAllGroups,
	sheetRefusal,
	sheetStatusLine,
	sheetTitle,
	skipsFile,
	splitErrors,
} from "@/lib/sheet";

const props = defineProps({
	name: { type: String, required: true },
	// Callbacks, not emits: Vue drops an unmounted instance's emits, and switching
	// rows mid-apply unmounts this detail. `onChanged` asks for a fresh lane.
	onDecided: { type: Function, default: null },
	onChanged: { type: Function, default: null },
	// false when the pane's heading already names the file
	showFileName: { type: Boolean, default: true },
});
const router = useRouter();
const socket = inject("$socket", null);

const POLL_MS = 5000;
const RELOADED = __(
	"This sheet changed since you opened it, so it was reloaded. Review it and apply again."
);

const rec = ref(null);
const loading = ref(true);
const loadError = ref("");
const busy = ref(null); // "apply" | "discard" | null
const notice = ref("");
const localErrors = ref(null); // an apply refused as invalid, until the next load
const choices = reactive({});
const answers = reactive({});
const progress = ref(null);
const open = reactive({});
const showAll = reactive({});
const metas = reactive({});
const bannerEl = ref(null);
const progressEl = ref(null);
const statusEl = ref(null);
const unreported = ref(null); // applied here: onDecided waits for Done

let alive = true;
let watching = false; // applied from here: its end is ours to report (else shown in place)
let settled = false;
let req = 0;
let pollTimer = null;

function settle(res) {
	if (settled) return;
	settled = true;
	watching = false;
	unreported.value = null;
	if (props.onDecided) props.onDecided(res);
}

async function focusOn(el) {
	await nextTick();
	if (alive && el.value) el.value.focus();
}

const fileName = computed(() => sheetTitle(rec.value));
const headline = computed(() => {
	const r = rec.value;
	const parts = [r.counts_line, r.for_user && "for " + r.for_user].filter(Boolean).join(" · ");
	return props.showFileName && parts ? " · " + parts : parts;
});
const ready = computed(
	() =>
		!!rec.value &&
		rec.value.status === "Pending" &&
		!!rec.value.can_act &&
		!rec.value.collecting
);
const records = computed(() => (rec.value && rec.value.records) || []);
const skipAll = computed(() => skipsFile(rec.value && rec.value.questions, answers));
const skipped = computed(() => (skipAll.value ? new Map() : cascade(records.value, choices)));
const gate = computed(() =>
	blockers({
		records: records.value,
		choices,
		questions: (rec.value && rec.value.questions) || [],
		answers,
	})
);
const errs = computed(
	() =>
		localErrors.value ||
		splitErrors(rec.value && rec.value.errors, rec.value && rec.value.questions)
);
const banner = computed(() => {
	const r = Object.keys(errs.value.records).length;
	const q = Object.keys(errs.value.questions).length;
	if (!r && !q && !errs.value.sheet.length) return "";
	const parts = [
		r && `${r} record${r === 1 ? "" : "s"}`,
		q && `${q} answer${q === 1 ? "" : "s"}`,
	];
	const what = parts.filter(Boolean).join(" and ");
	return what
		? `Nothing was applied: ${what} need${r + q === 1 ? "s" : ""} attention.`
		: "Nothing was applied.";
});
const view = computed(() =>
	sections(records.value).map((s) => {
		const all = showAll[s.key] || s.records.length <= PREVIEW;
		const n = s.records.filter(
			(r) => gate.value.records[r.index] || errs.value.records[r.index]
		).length;
		return {
			...s,
			id: `sheet-${props.name}-${s.key}`,
			shown: all ? s.records : s.records.slice(0, PREVIEW),
			hidden: all ? 0 : s.records.length - PREVIEW,
			attention: n ? `${n} need${n === 1 ? "s" : ""} attention` : "",
			groups: skipAll.value ? [] : setForAllGroups(s.records, choices, skipped.value, metas),
		};
	})
);
const reasonId = computed(() => `sheet-${props.name}-reason`);
const questionId = (q) => `sheet-${props.name}-q-${q.name}`;
const progressLine = computed(() => progressText(progress.value));
const pct = computed(() => {
	const p = progress.value;
	return p && p.total ? Math.min(100, Math.round(((p.done || 0) / p.total) * 100)) : 0;
});
const closedLine = computed(() => sheetStatusLine(rec.value));
const outcome = computed(() => outcomeSummary(rec.value && rec.value.outcome));

function cascadeCount(r) {
	const c = choices[r.index];
	return c && c.action === "skip" ? reachFrom(records.value, choices, r.index).size : 0;
}

function replace(target, source) {
	for (const k of Object.keys(target)) delete target[k];
	Object.assign(target, source);
}
const signature = (r) =>
	((r && r.records) || []).map((x) => `${x.index}:${x.doctype}:${x.title}`).join("|");

// A new sheet, a new version of it, or one back from an apply: fill the choices
// (a bounced apply's decisions first) and move to its first error.
function receive(res) {
	const before = rec.value;
	rec.value = res;
	if (!res) return;
	if (isTerminal(res.status)) {
		if (watching) finish(res);
		return;
	}
	progress.value = res.status === "Executing" ? res.progress || progress.value : null;
	const newSha = !before || before.card_sha256 !== res.card_sha256;
	if (newSha) candidateCache = {};
	const fresh = newSha || before.status !== res.status;
	if (res.status !== "Pending" || !fresh) return;
	watching = false; // back from an apply: the next one is watched again
	const same = before && signature(before) === signature(res);
	replace(choices, initialChoices(res.records, res.decisions, same ? { ...choices } : null));
	replace(answers, initialAnswers(res.questions, res.decisions, same ? { ...answers } : null));
	localErrors.value = null;
	loadFixMetas(res.records);
	if (Object.keys(res.errors || {}).length) focusFirstError();
}

function finish(res) {
	if (res.status === "Executed") {
		const line = outcomeSummary(res.outcome).line;
		toast.success(
			escapeHtml(
				__("Applied{0}. The run continues.", [line ? ": " + line.toLowerCase() : ""])
			)
		);
		// the outcome stays in view; the rail lets the row go now
		watching = false;
		unreported.value = res;
		if (props.onChanged) props.onChanged(res);
		focusOn(statusEl);
		return;
	}
	if (res.status === "Discarded") toast.success(escapeHtml(sheetStatusLine(res)));
	else toast.error(escapeHtml(sheetStatusLine(res)));
	settle(res);
}

async function load() {
	const id = ++req;
	loading.value = !rec.value;
	loadError.value = "";
	try {
		const res = await getPendingAction(props.name);
		if (id !== req || !alive) return;
		receive(res || null);
	} catch (e) {
		if (id !== req || !alive) return;
		// a failed poll keeps what is on screen
		if (!rec.value)
			loadError.value = errMessage(e, __("This approval sheet could not be loaded."));
	} finally {
		if (id === req) {
			loading.value = false;
			schedule();
		}
	}
}

// Listing or applying: poll the slim status until it moves (realtime may never
// come). A full reload only when the status or card_sha256 changed - a slim
// tick otherwise just patches collecting/progress/counts in place.
function schedule() {
	clearTimeout(pollTimer);
	const r = rec.value;
	if (alive && r && (r.collecting || r.status === "Executing"))
		pollTimer = setTimeout(pollTick, POLL_MS);
}

async function pollTick() {
	if (!alive || busy.value !== null) return;
	const id = ++req;
	try {
		const s = await getSheetStatus(props.name);
		if (id !== req || !alive) return;
		const r = rec.value;
		if (!s || !r || s.status !== r.status || s.card_sha256 !== r.card_sha256) {
			await load();
			return;
		}
		rec.value = {
			...r,
			collecting: s.collecting,
			record_count: s.record_count,
			counts_line: s.counts_line,
		};
		if (r.status === "Executing") progress.value = s.progress || progress.value;
	} catch (e) {
		// a failed poll keeps what is on screen; retried below
	} finally {
		if (id === req) schedule();
	}
}

function onEvent(p) {
	if (!p || p.kind !== "sheet:progress" || p.name !== props.name || !alive) return;
	if (p.state === "applying" && rec.value && rec.value.status === "Executing")
		progress.value = { done: p.done, total: p.total };
	else if (busy.value === null) load();
}

// The board calls this when the row changes or leaves its rail: never over an
// apply in flight (the choices survive a reload of the same sheet).
function refresh() {
	if (busy.value === null) load();
}

// ── focus ────────────────────────────────────────────────────────────────────
function sectionOf(index) {
	return view.value.find((s) => s.records.some((r) => r.index === index));
}
async function goTo(target) {
	if (!target || !alive) return;
	let id;
	if (target.type === "question") {
		open.questions = true;
		id = questionId({ name: target.name });
	} else {
		const s = sectionOf(target.index);
		if (!s) return;
		open[s.key] = true;
		if (s.records.findIndex((r) => r.index === target.index) >= PREVIEW) showAll[s.key] = true;
		id = `sheet-${props.name}-r${target.index}`;
	}
	await nextTick();
	const el = document.getElementById(id);
	if (!el) return;
	if (el.scrollIntoView) el.scrollIntoView({ block: "center" });
	el.focus();
}
async function focusFirstError() {
	const e = errs.value;
	for (const s of sections(records.value))
		for (const r of s.records)
			if (e.records[r.index]) return goTo({ type: "record", index: r.index });
	const q = ((rec.value && rec.value.questions) || []).find((x) => e.questions[x.name]);
	if (q) return goTo({ type: "question", name: q.name });
	await nextTick();
	if (bannerEl.value && alive) bannerEl.value.focus();
}

// ── choices ──────────────────────────────────────────────────────────────────
function choose(index, patch) {
	if (choices[index]) Object.assign(choices[index], patch);
}
function bulk(s, mode) {
	for (const r of s.records) {
		const c = choices[r.index];
		if (mode === "skip") c.action = "skip";
		else if (c.action === "skip" || c.action === "use_existing") c.action = actionsFor(r)[0];
	}
}
async function showAllOf(s) {
	showAll[s.key] = true;
	await nextTick();
	const next = s.records[PREVIEW];
	const el = next && document.getElementById(`sheet-${props.name}-r${next.index}`);
	if (el) el.focus();
}
function pickOption(q, o) {
	const a = answers[q.name];
	a.option = a.option === o ? "" : o;
}

// SP-D1: one value into each record's edit
function setForAll(g, value) {
	for (const i of g.indexes) {
		const r = records.value.find((x) => x.index === i);
		const c = choices[i];
		if (!r || !c) continue;
		const base = c.values || JSON.parse(JSON.stringify(r.values || {}));
		Object.assign(c, { action: "edit", values: { ...base, [g.fieldname]: value } });
	}
}

// ── lazy loads ───────────────────────────────────────────────────────────────
const metaCache = {};
function formMeta(doctype) {
	if (!metaCache[doctype]) {
		metaCache[doctype] = getDoctypeFormMeta(doctype)
			.then((res) => {
				if (!res || !res.ok) throw new Error("no form meta");
				return res;
			})
			.catch((e) => {
				delete metaCache[doctype];
				throw e;
			});
	}
	return metaCache[doctype];
}
// A doctype with a fix among two or more flagged records loads its fields once,
// so "Set <field> for all" can offer the field the fix is about.
function loadFixMetas(list) {
	const count = {};
	const fixes = new Set();
	for (const r of list || []) {
		const fix = fixOf(r);
		if (fix) fixes.add(r.doctype);
		if (fix || (r.needs_input || []).length) count[r.doctype] = (count[r.doctype] || 0) + 1;
	}
	for (const doctype of fixes) {
		if (count[doctype] < 2 || metas[doctype]) continue;
		formMeta(doctype)
			.then((m) => alive && (metas[doctype] = m))
			.catch(() => {});
	}
}
let candidateCache = {}; // per sheet version
function loadCandidates(index) {
	const cache = candidateCache;
	if (!cache[index]) {
		cache[index] = getSheetCandidates(props.name, [index])
			.then((res) => (res && res[String(index)]) || [])
			.catch((e) => {
				delete cache[index];
				throw e;
			});
	}
	return cache[index];
}

// ── apply / skip ─────────────────────────────────────────────────────────────
async function apply() {
	if (busy.value !== null || !ready.value || gate.value.summary) return;
	req++; // a load in flight never lands over the apply
	busy.value = "apply";
	notice.value = "";
	localErrors.value = null;
	try {
		const res =
			(await applySheet(props.name, buildDecisions(rec.value, choices, answers))) || {};
		if (res.ok) {
			if (props.onChanged) props.onChanged(res);
			if (!alive) return;
			rec.value = { ...rec.value, status: "Executing", errors: {}, progress: null };
			progress.value = { done: 0, total: rec.value.record_count };
			watching = true;
			schedule();
			focusOn(progressEl);
			return;
		}
		const message = sheetRefusal(res);
		if (isSheetSettled(res)) {
			toast.error(escapeHtml(message));
			settle(res);
			return;
		}
		if (!alive) return;
		if (res.reason_code === "invalid") {
			localErrors.value = splitErrors(
				{ ...(res.errors || {}), ...(res.answer_errors || {}) },
				rec.value.questions
			);
			focusFirstError();
			return;
		}
		notice.value = message;
		if (res.reason_code === "changed" || res.reason_code === "executing") {
			busy.value = null;
			await load();
			if (res.reason_code === "changed") notice.value = RELOADED;
		}
	} catch (e) {
		if (alive) notice.value = errMessage(e);
	} finally {
		busy.value = null;
	}
}

function confirmSkip() {
	if (busy.value !== null) return;
	confirmDialog({
		title: __("Skip this file?"),
		message: __(
			"Nothing on this sheet is created and its questions are closed. Jarvis is told you skipped the file."
		),
		onConfirm: ({ hideDialog }) => {
			hideDialog();
			discard();
		},
	});
}
async function discard() {
	if (busy.value !== null) return;
	req++;
	busy.value = "discard";
	notice.value = "";
	try {
		const res = (await discardSheet(props.name)) || {};
		if (res.ok) {
			toast.success(__("Skipped this file. Nothing was created."));
			settle(res);
			return;
		}
		const message = sheetRefusal(res);
		if (isSheetSettled(res)) {
			toast.error(escapeHtml(message));
			settle(res);
			return;
		}
		if (alive) notice.value = message;
	} catch (e) {
		if (alive) notice.value = errMessage(e);
	} finally {
		busy.value = null;
	}
}

function openChat() {
	if (rec.value && rec.value.conversation) router.push("/c/" + rec.value.conversation);
}

onMounted(() => {
	load();
	if (socket && socket.on) socket.on("jarvis:event", onEvent);
});
onBeforeUnmount(() => {
	if (unreported.value) settle(unreported.value);
	alive = false;
	clearTimeout(pollTimer);
	if (socket && socket.off) socket.off("jarvis:event", onEvent);
});
defineExpose({ refresh });
</script>
