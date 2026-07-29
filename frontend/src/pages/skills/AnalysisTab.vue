<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<Breadcrumbs
					:items="[
						{ label: 'Skills', route: { name: 'SkillsList' } },
						{ label: 'Analysis' },
					]"
				/>
			</template>
			<template #right-header>
				<Button
					icon="refresh-cw"
					variant="ghost"
					:tooltip="'Refresh'"
					@click="reloadAll"
				/>
				<Button
					variant="subtle"
					:label="status.latestRun ? 'Run analysis now' : 'Run first analysis now'"
					iconLeft="play"
					:loading="runningNow"
					@click="runNow"
				/>
			</template>
		</LayoutHeader>

		<div class="min-h-0 flex-1 overflow-y-auto">
			<div
				class="mx-auto grid w-full max-w-5xl grid-cols-1 items-start gap-5 px-5 py-5 lg:grid-cols-2"
			>
				<!-- ══════════════ Learning settings & controls ══════════════ -->
				<section class="rounded-lg border p-4">
					<div class="flex items-start justify-between gap-3">
						<div>
							<div class="text-base font-semibold text-ink-gray-9">
								Behavioural learning
							</div>
							<div class="mt-0.5 text-sm text-ink-gray-6">
								Analyse this site's history overnight and propose learned defaults
								for review.
							</div>
						</div>
						<Switch v-model="settings.pattern_learning_enabled" size="md" />
					</div>

					<div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
						<FormControl
							type="time"
							label="Window start"
							:modelValue="settings.pattern_window_start"
							@update:modelValue="(v) => (settings.pattern_window_start = v)"
						/>
						<FormControl
							type="time"
							label="Window end"
							:modelValue="settings.pattern_window_end"
							@update:modelValue="(v) => (settings.pattern_window_end = v)"
						/>
						<FormControl
							type="number"
							label="Max findings / run"
							:modelValue="settings.pattern_max_proposals_per_run"
							@update:modelValue="
								(v) => (settings.pattern_max_proposals_per_run = v)
							"
						/>
					</div>
					<p class="mt-2 text-sm text-ink-gray-5">
						Analysis runs inside this daily window (at least one hour, site time).
						Approved patterns still need an explicit Apply from the Review tab before
						they reach your assistant.
					</p>

					<div class="mt-4 flex flex-wrap items-center gap-2">
						<Button
							variant="solid"
							label="Save settings"
							:loading="savingSettings"
							@click="saveSettings"
						/>
					</div>

					<!-- run status line: Enabled/Disabled is the ONLY pill; the run-status
					     Long Text is plain text (not a Badge). -->
					<div class="mt-4 flex flex-col gap-1.5 border-t pt-3 text-sm">
						<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
							<span class="text-ink-gray-5">Status:</span>
							<Badge
								variant="subtle"
								:theme="status.enabled ? 'green' : 'gray'"
								:label="status.enabled ? 'Enabled' : 'Disabled'"
							/>
							<template v-if="status.lastRunAt">
								<span class="text-ink-gray-4">·</span>
								<span class="text-ink-gray-6">Last scheduled analysis</span>
								<Tooltip :text="exactDate(status.lastRunAt)">
									<span class="text-ink-gray-8">{{
										timeAgo(status.lastRunAt)
									}}</span>
								</Tooltip>
							</template>
							<template v-if="status.nextRunAt">
								<span class="text-ink-gray-4">·</span>
								<span class="text-ink-gray-6">Next run</span>
								<Tooltip :text="exactDate(status.nextRunAt)">
									<span class="text-ink-gray-8">{{
										timeAgo(status.nextRunAt)
									}}</span>
								</Tooltip>
							</template>
						</div>
						<div v-if="status.lastRunStatus" class="text-ink-gray-6">
							{{ status.lastRunStatus }}
						</div>
					</div>
				</section>

				<!-- ══════════════ Runs & findings ══════════════ -->
				<!-- Renders exactly what get_learning_status already returns (latest_run
				     telemetry + coverage); there is no run-history endpoint yet, so a
				     single latest-run card is the honest surface. -->
				<section class="rounded-lg border p-4">
					<div class="text-base font-semibold text-ink-gray-9">Runs &amp; findings</div>

					<div
						v-if="!status.latestRun"
						class="mt-3 flex flex-col items-center gap-1 rounded-lg border border-dashed py-10 text-center"
					>
						<FeatherIcon name="activity" class="size-7 text-ink-gray-5" />
						<span class="mt-1 text-base font-medium text-ink-gray-8"
							>No analysis runs yet</span
						>
						<span class="max-w-sm text-p-base text-ink-gray-6">
							Run your first analysis to mine this site's history for behavioural
							patterns. Findings become Personalise questions for the people
							involved; anything skill-shaped also lands pending in Review.
						</span>
					</div>

					<div v-else class="mt-3 flex flex-col gap-3">
						<div class="flex flex-wrap items-center gap-2">
							<Badge
								variant="subtle"
								:theme="runStatusTheme(status.latestRun.status)"
								:label="status.latestRun.status || 'Unknown'"
							/>
							<!-- two run kinds share this doctype; without the label two
							     different "last run" truths sit unlabeled side by side -->
							<Badge
								variant="outline"
								theme="gray"
								:label="
									status.latestRun.scan_mode === 'voice'
										? 'Voice-notes sweep'
										: 'Entry analysis'
								"
							/>
							<Badge
								v-if="status.latestRun.trigger"
								variant="outline"
								theme="gray"
								:label="
									status.latestRun.trigger === 'manual' ? 'Manual' : 'Scheduled'
								"
							/>
							<a
								:href="deskUrl('Jarvis Pattern Run', status.latestRun.name)"
								target="_blank"
								rel="noopener"
								class="inline-flex items-center gap-1 text-sm text-ink-gray-7 hover:underline"
							>
								<FeatherIcon name="activity" class="size-3.5" />
								Run {{ status.latestRun.name }}
							</a>
						</div>

						<div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
							<template v-if="status.latestRun.started_at">
								<span class="text-ink-gray-5">Started</span>
								<Tooltip :text="exactDate(status.latestRun.started_at)">
									<span class="text-ink-gray-8">{{
										timeAgo(status.latestRun.started_at)
									}}</span>
								</Tooltip>
							</template>
							<template v-if="status.latestRun.ended_at">
								<span class="text-ink-gray-4">·</span>
								<span class="text-ink-gray-5">Ended</span>
								<Tooltip :text="exactDate(status.latestRun.ended_at)">
									<span class="text-ink-gray-8">{{
										timeAgo(status.latestRun.ended_at)
									}}</span>
								</Tooltip>
							</template>
						</div>

						<!-- per-run detector findings: candidates seen vs proposals kept -->
						<div class="grid grid-cols-3 gap-3">
							<div class="rounded-lg border bg-surface-gray-1 px-3 py-2">
								<div class="text-lg font-semibold text-ink-gray-9">
									{{ intOr(status.latestRun.candidates_found, 0) }}
								</div>
								<div class="text-sm text-ink-gray-6">Candidates found</div>
							</div>
							<div class="rounded-lg border bg-surface-gray-1 px-3 py-2">
								<div class="text-lg font-semibold text-ink-gray-9">
									{{ intOr(status.latestRun.proposals_created, 0) }}
								</div>
								<div class="text-sm text-ink-gray-6">Findings surfaced</div>
							</div>
							<div class="rounded-lg border bg-surface-gray-1 px-3 py-2">
								<div class="text-lg font-semibold text-ink-gray-9">
									{{ intOr(status.latestRun.proposals_updated, 0) }}
								</div>
								<div class="text-sm text-ink-gray-6">Findings updated</div>
							</div>
						</div>

						<!-- detector coverage: printed ONCE (suppressed when the engine already
						     appended it to the last-run status shown in the settings pane) -->
						<div v-if="coverageNote" class="text-sm text-ink-gray-5">
							{{ coverageNote }}
						</div>

						<div class="text-sm text-ink-gray-6">
							Findings become Personalise questions for the people involved once the
							run finishes; anything skill-shaped also lands pending in
							<button class="text-ink-gray-8 underline" @click="goReview">
								Review</button
							>.
						</div>
					</div>
				</section>

				<!-- ══════════════ Learn from custom apps (M2) ══════════════ -->
				<!-- Below the Behavioural-learning settings card, same gate: this
				     whole tab only mounts for admin/SM viewers (SkillsPage's
				     caps.analysis probe - the gate the settings card above rides).
				     Full-width row: the card now points admins to the Custom App
				     Learning agent (the chat-batch runs list was retired). -->
				<AppLearningCard ref="appLearningCard" class="lg:col-span-2" />

				<!-- ══════════════ Personalisation questions pointer ══════════════ -->
				<!-- Discoverability for the gear-gated Settings dialog (DESIGN.md §6
				     ADOPTED: "gear on Personalise (admin-only) AND a link card on
				     Analysis tab"). This card only navigates the hash — the gear on
				     PersonaliseTab.vue is what actually opens the dialog (its
				     `settingsOpen` ref lives there, not here); see this file's report
				     note on why that's the deliberately simple wiring. -->
				<section class="rounded-lg border p-4 lg:col-span-2">
					<div class="flex flex-wrap items-center justify-between gap-3">
						<div class="min-w-0">
							<div class="text-base font-semibold text-ink-gray-9">
								Personalisation questions
							</div>
							<div class="mt-0.5 text-sm text-ink-gray-6">
								Configure what {{ agentName }} asks your team — org-wide, per role,
								or one person at a time — on the Personalise tab.
							</div>
						</div>
						<Button
							variant="subtle"
							label="Go to Personalise"
							iconLeft="settings"
							@click="goPersonalise"
						/>
					</div>
				</section>
			</div>
		</div>
	</div>
</template>

<script setup>
// AnalysisTab — the pattern-learning settings + run telemetry, the "Analysis"
// tab inside the Skills page (Skills IA v2). Split out of the old LearningTab:
// this file owns the Settings card (enable / window / max proposals / Save),
// the Run-now control (top-right, confirm popup, dynamic first-run label) and
// a "Runs & findings" pane that renders the latest-run telemetry + detector
// coverage from get_learning_status. The decision queue lives in ReviewTab.
import { ref, reactive, computed, onMounted } from "vue";
import {
	Badge,
	Breadcrumbs,
	Button,
	FeatherIcon,
	FormControl,
	Switch,
	Tooltip,
	toast,
	confirmDialog,
} from "frappe-ui";
import { useRouter } from "vue-router";
import LayoutHeader from "@/components/LayoutHeader.vue";
import AppLearningCard from "@/components/learning/AppLearningCard.vue";
import { timeAgo, exactDate } from "@/utils/datetime";
import {
	runPatternAnalysisNow,
	getLearningSettings,
	setLearningSettings,
	getLearningStatus,
} from "@/api/learning";
import { agentName } from "@/branding";

const emit = defineEmits(["changed"]);
const router = useRouter();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── state ────────────────────────────────────────────────────────────────────
const savingSettings = ref(false);
const runningNow = ref(false);

const settings = reactive({
	pattern_learning_enabled: false,
	pattern_window_start: "",
	pattern_window_end: "",
	pattern_max_proposals_per_run: 10,
	pattern_row_budget_per_night: 500000,
});
const status = reactive({
	enabled: false,
	lastRunAt: "",
	lastRunStatus: "",
	nextRunAt: "",
	scanMode: "",
	latestRun: null,
});

// ── display helpers ──────────────────────────────────────────────────────────
function toHHMM(t) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(t || ""));
	return m ? `${m[1].padStart(2, "0")}:${m[2]}` : "";
}
// Coerce an Int field to a number, falling back to a default only for a truly
// absent value (null / undefined / ""); a legitimate 0 is kept.
function intOr(v, d) {
	if (v === null || v === undefined || v === "") return d;
	const n = Number(v);
	return Number.isFinite(n) ? n : d;
}
// Never let a valid submitted window round-trip to blank. A midnight Time (00:00)
// can serialize back as "" through a falsy timedelta server-side; when the echo
// is blank but we sent a real value, keep what we sent.
function keepTime(returned, submitted) {
	return toHHMM(returned) || toHHMM(submitted) || String(submitted || "");
}
function deskUrl(doctype, name) {
	if (!doctype || !name) return "";
	const dt = String(doctype).toLowerCase().replace(/ /g, "-");
	return `/app/${dt}/${encodeURIComponent(name)}`;
}
// Jarvis Pattern Run status → badge theme (Queued/Running/Paused/Completed/
// Partial/Failed per the doctype Select).
function runStatusTheme(s) {
	if (s === "Completed") return "green";
	if (s === "Failed") return "red";
	if (s === "Partial") return "orange";
	if (s === "Running") return "blue";
	return "gray";
}

// Coverage note is printed ONCE. The engine appends it to pattern_last_run_status
// (rendered as plain text in the settings pane), so suppress the standalone line
// when the summary already contains it, otherwise it prints twice.
const coverageNote = computed(() => {
	const note = (status.latestRun && status.latestRun.coverage_note) || "";
	if (!note) return "";
	return (status.lastRunStatus || "").includes(note) ? "" : note;
});

// ── loaders ──────────────────────────────────────────────────────────────────
async function loadStatus() {
	try {
		const st = await getLearningStatus();
		status.enabled = !!st.enabled;
		status.lastRunAt = st.last_run_at || "";
		status.lastRunStatus = st.last_run_status || "";
		status.nextRunAt = st.next_run_at || "";
		status.scanMode = st.scan_mode || "";
		status.latestRun = st.latest_run || null;
	} catch (e) {
		// parent mounts this only for SMs; a failure here means no access
		toast.error(errMsg(e));
	}
}

async function loadSettings() {
	try {
		const res = await getLearningSettings();
		const s = res.settings || {};
		settings.pattern_learning_enabled = !!s.pattern_learning_enabled;
		settings.pattern_window_start = toHHMM(s.pattern_window_start);
		settings.pattern_window_end = toHHMM(s.pattern_window_end);
		settings.pattern_max_proposals_per_run = intOr(s.pattern_max_proposals_per_run, 10);
		settings.pattern_row_budget_per_night = intOr(s.pattern_row_budget_per_night, 500000);
	} catch (e) {
		toast.error(errMsg(e));
	}
}

const appLearningCard = ref(null);

function reloadAll() {
	loadStatus();
	loadSettings();
	// the header Refresh also refreshes the app-learning overview + run list
	if (appLearningCard.value) appLearningCard.value.reload();
}

function goReview() {
	router.push({ hash: "#review" });
}

// Pointer card affordance only (DESIGN.md §6 ADOPTED: "Settings discoverability
// - gear on Personalise (admin-only) AND a link card on Analysis tab"). This
// deliberately just switches the Skills-page hash tab; it does NOT also open
// the Settings dialog itself - PersonaliseTab.vue owns that gear + its
// `settingsOpen` ref, and reaching into a sibling tab's local state from here
// would mean inventing a NEW cross-component channel for one convenience
// click (the brief explicitly says not to over-engineer this). The user sees
// the same admin-only gear on arrival and opens Settings from there.
function goPersonalise() {
	router.push({ hash: "#personalise" });
}

// ── settings actions ─────────────────────────────────────────────────────────
async function saveSettings() {
	savingSettings.value = true;
	try {
		const res = await setLearningSettings({
			pattern_learning_enabled: settings.pattern_learning_enabled ? 1 : 0,
			pattern_window_start: settings.pattern_window_start,
			pattern_window_end: settings.pattern_window_end,
			pattern_max_proposals_per_run: Number(settings.pattern_max_proposals_per_run) || 0,
			pattern_row_budget_per_night: Number(settings.pattern_row_budget_per_night) || 0,
		});
		const s = (res && res.settings) || {};
		settings.pattern_window_start = keepTime(
			s.pattern_window_start,
			settings.pattern_window_start
		);
		settings.pattern_window_end = keepTime(s.pattern_window_end, settings.pattern_window_end);
		toast.success("Settings saved");
		loadStatus();
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		savingSettings.value = false;
	}
}

function runNow() {
	confirmDialog({
		title: "Run analysis now?",
		message:
			"Runs a full pattern analysis immediately, bypassing the nightly window. It scans this site's history and can add database load during business hours. New findings become Personalise questions once the run finishes; skill-shaped ones land pending on the Review tab.",
		onConfirm: async ({ hideDialog }) => {
			runningNow.value = true;
			try {
				const r = await runPatternAnalysisNow();
				hideDialog();
				if (r && r.ok === false) {
					toast.error(r.reason || "Could not start the run.");
				} else {
					toast.success("Analysis started" + (r && r.run ? ` (${r.run})` : ""));
				}
				loadStatus();
				emit("changed");
			} catch (e) {
				toast.error(errMsg(e));
			} finally {
				runningNow.value = false;
			}
		},
	});
}

// ── init ─────────────────────────────────────────────────────────────────────
onMounted(async () => {
	await loadStatus();
	loadSettings();
});
</script>
