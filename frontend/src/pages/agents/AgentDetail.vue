<template>
	<div class="flex h-full flex-col overflow-hidden">
		<LayoutHeader>
			<template #left-header>
				<!-- jarvis#1062 P1-6 (production-readiness audit): the raw slug
				     ("negative-stock-valuation-auditor") used to flash here before
				     the agent loaded, then get replaced by its real title - a
				     visible, jarring swap. An empty crumb + a skeleton bar
				     (item.loading, via Breadcrumbs' own #suffix slot) now render
				     until the title is actually known; nothing ever flashes.
				     Review fix: a failed load() left `agent` null forever, so a
				     bare load failure showed a blank crumb AND a permanently
				     spinning skeleton next to the error panel below. `error` set
				     falls back to the slug (the old, pre-P1-6 behaviour) and
				     stops the skeleton - there is nothing left to wait for. -->
				<Breadcrumbs
					:items="[
						{ label: 'Agents', route: { name: 'AgentsList' } },
						{
							label: agent ? agent.title || props.slug : error ? props.slug : '',
							loading: !agent && !error,
						},
					]"
				>
					<template #suffix="{ item }">
						<span
							v-if="item.loading"
							class="ml-1 inline-block h-4 w-32 animate-pulse rounded bg-surface-gray-3"
							aria-hidden="true"
						/>
					</template>
				</Breadcrumbs>
			</template>
			<template #right-header>
				<template v-if="agent && !installation">
					<Button
						variant="solid"
						label="Install"
						:loading="installing"
						:disabled="!canInstall"
						:tooltip="installTooltip"
						@click="install"
					/>
				</template>
				<template v-else-if="agent">
					<Button
						variant="solid"
						label="Run Now"
						iconLeft="play"
						:loading="running"
						:disabled="runDisabled"
						:tooltip="runTooltip"
						@click="runNow"
					/>
					<Dropdown
						:options="[
							{
								label: 'Uninstall',
								icon: 'trash-2',
								theme: 'red',
								onClick: confirmUninstall,
							},
						]"
					>
						<!-- label → aria-label on icon-only buttons (frappe-ui) -->
						<Button icon="more-horizontal" variant="ghost" label="Agent actions" />
					</Dropdown>
				</template>
			</template>
		</LayoutHeader>

		<!-- not found -->
		<div v-if="error" class="flex flex-1 flex-col items-center justify-center gap-3">
			<div class="flex flex-col items-center gap-1">
				<span class="text-lg font-medium text-ink-gray-8">Agent not found</span>
				<span class="text-center text-p-base text-ink-gray-6">{{ error }}</span>
			</div>
			<Button label="Back to Agents" @click="router.push({ name: 'AgentsList' })" />
		</div>
		<!-- loading (AgentsList/AgentActivityTab pattern - never a blank page) -->
		<div v-else-if="!agent" class="flex flex-1 flex-col items-center justify-center gap-2">
			<JvSpinner />
			<span class="text-sm text-ink-gray-5">Loading agent…</span>
		</div>

		<!-- runs tab pins hero+tabs and hands the remaining height to the two-pane
		     board (its rail/pane scroll independently); other tabs page-scroll -->
		<div
			v-else
			class="flex min-h-0 flex-1 flex-col"
			:class="tab === 'runs' && installation ? 'overflow-hidden' : 'overflow-y-auto'"
		>
			<!-- ── hero (marketplace template, D29; de-texted per §15.4) ── -->
			<div class="shrink-0 border-b bg-surface-gray-1 px-6 py-6">
				<div class="flex items-start justify-between gap-5">
					<div class="flex min-w-0 gap-5">
						<div
							class="grid h-16 w-16 shrink-0 place-items-center rounded-lg border bg-surface-gray-2 text-2xl font-semibold text-ink-gray-6"
						>
							{{ logoText }}
						</div>
						<div class="min-w-0">
							<h1 class="truncate text-xl font-semibold text-ink-gray-9">
								{{ agent.title }}
							</h1>
							<!-- ONE meta line: publisher · version · nature/status badges -->
							<div
								class="mt-1 flex flex-wrap items-center gap-1.5 text-sm text-ink-gray-5"
							>
								<span class="truncate">{{ heroMetaText }}</span>
								<span>·</span>
								<Badge variant="subtle" theme="gray" :label="agent.nature" />
								<Badge
									v-if="agent.status === 'Coming Soon'"
									variant="subtle"
									theme="blue"
									label="Coming Soon"
								/>
								<Badge
									v-else-if="agent.status === 'Deprecated'"
									variant="subtle"
									theme="red"
									label="Deprecated"
								/>
							</div>
							<!-- one-line tagline; the LONG description lives ONLY in Overview -->
							<p v-if="tagline" class="mt-1 line-clamp-1 text-base text-ink-gray-6">
								{{ tagline }}
							</p>
							<div class="mt-2 flex flex-wrap gap-2">
								<Badge
									variant="outline"
									theme="gray"
									:label="categoryTitle(agent.category)"
								/>
							</div>
						</div>
					</div>
					<div class="flex shrink-0 flex-col items-end gap-3 self-start">
						<div class="flex items-center gap-1 text-sm text-ink-gray-5">
							<FeatherIcon name="download" class="size-3.5" />
							{{ agent.install_count || 0 }} install{{
								agent.install_count === 1 ? "" : "s"
							}}
						</div>
						<!-- PP-4: the real activation state, honestly, wherever the agent
						     is discussed - not only in the passive Runs-tab "Preview" pill
						     (jarvis#456). The action itself lives in Configure. -->
						<Badge
							v-if="
								canReview && activation && activation.activation_state === 'live'
							"
							variant="subtle"
							theme="green"
							label="Live"
						/>
						<ShadowChip v-else-if="canReview && activation" label="Shadow (preview)" />
						<Switch
							v-if="installation"
							label="Enabled"
							:modelValue="!!installation.enabled"
							:disabled="togglingEnabled"
							@update:modelValue="setEnabled"
						/>
					</div>
				</div>
				<!-- #1062 review fix: an installed-but-not-allowed row (role revoked
				     after install) must say so visibly, not only via the disabled Run
				     button's tooltip - a tooltip is easy to miss entirely. -->
				<div v-if="installation && !agent.allowed" class="mt-3 text-sm text-ink-gray-5">
					You do not have access to run this agent. Ask your administrator.
				</div>
				<!-- jarvis#1062 polish: the same visible-hint treatment for the other
				     two reasons Run Now can be disabled - a tooltip alone is easy to
				     miss. -->
				<div
					v-else-if="installation && !installation.enabled"
					class="mt-3 text-sm text-ink-gray-5"
				>
					Enable this agent to run it.
				</div>
				<div v-else-if="shadowScribeBlocked" class="mt-3 text-sm text-ink-gray-5">
					Still in shadow preview - promote it to live under Configure first
				</div>
				<!-- jarvis#1062 polish: install_agent refuses an Administrator run-as
				     identity server-side ("agents cannot run as Administrator") - fail
				     this visibly instead of letting the click round-trip into a
				     toast-only refusal. -->
				<div
					v-if="!installation && isAdministratorSession"
					class="mt-3 text-sm text-ink-gray-5"
				>
					Log in as a named user to install this agent.
				</div>
				<!-- jarvis#1062 polish: the install-failure toast auto-dismisses in
				     ~2s - keep the reason visible under the Install button until the
				     next attempt. -->
				<div v-if="!installation && installError" class="mt-3 text-sm text-ink-red-4">
					{{ installError }}
				</div>
			</div>

			<TabBar class="shrink-0" :tabs="tabs" :modelValue="tab" @update:modelValue="setTab" />

			<!-- ── Overview ── -->
			<div v-if="tab === 'overview'" class="flex shrink-0">
				<div class="max-w-3xl flex-1 px-5 py-6">
					<!-- O1: renderMarkdown from @/markdown (escapes HTML first), NOT marked -->
					<div
						v-if="descriptionHtml"
						class="prose prose-sm max-w-none"
						v-html="descriptionHtml"
					/>
					<div v-else class="text-sm text-ink-gray-5">No description yet.</div>
					<div v-if="needs.length" class="mt-8">
						<div class="text-base font-medium text-ink-gray-9">What it needs</div>
						<div class="mt-2 flex flex-wrap gap-1.5">
							<code
								v-for="t in needs"
								:key="t"
								class="rounded bg-surface-gray-2 px-1.5 py-0.5 font-mono text-xs text-ink-gray-7"
							>
								{{ t }}
							</code>
						</div>
					</div>
					<!-- jarvis#1062 polish: doctypes_required (A12) rides in get_agent's
					     payload but was never shown - a customer had no way to see which
					     records the run-as user must be able to read. -->
					<div v-if="readsRecords.length" class="mt-8">
						<div class="text-base font-medium text-ink-gray-9">
							Reads these records
						</div>
						<div class="mt-2 flex flex-wrap gap-1.5">
							<code
								v-for="t in readsRecords"
								:key="t"
								class="rounded bg-surface-gray-2 px-1.5 py-0.5 font-mono text-xs text-ink-gray-7"
							>
								{{ t }}
							</code>
						</div>
					</div>
				</div>
				<!-- static facts panel (no Resizer, D29) -->
				<div class="w-[280px] shrink-0 space-y-6 border-l px-5 py-6">
					<div>
						<div class="text-sm font-medium text-ink-gray-5">Publisher</div>
						<div class="mt-1 text-base text-ink-gray-8">
							{{ agent.publisher || "Jarvis" }}
						</div>
					</div>
					<div>
						<div class="text-sm font-medium text-ink-gray-5">Nature</div>
						<div class="mt-1 text-base text-ink-gray-8">
							{{ agent.nature }} ·
							{{
								agent.nature === "Auditor"
									? "read-only"
									: agent.nature === "Scribe"
									? "writes wiki pages"
									: "writes drafts"
							}}
						</div>
					</div>
					<div>
						<div class="text-sm font-medium text-ink-gray-5">Category</div>
						<div class="mt-1 text-base text-ink-gray-8">
							{{ categoryTitle(agent.category) }}
						</div>
					</div>
					<div>
						<div class="text-sm font-medium text-ink-gray-5">Version</div>
						<div
							class="mt-1 flex flex-wrap items-center gap-2 text-base text-ink-gray-8"
						>
							<span>{{
								agent.version && agent.version !== "0.0.0"
									? "v" + agent.version
									: "-"
							}}</span>
							<Badge
								v-if="updateAvailable"
								variant="subtle"
								theme="orange"
								label="Update available"
							/>
						</div>
						<div v-if="updateAvailable" class="mt-1 text-sm text-ink-gray-5">
							installed v{{ installation.installed_version }}
						</div>
					</div>
					<div>
						<div class="text-sm font-medium text-ink-gray-5">Validated FY</div>
						<div class="mt-1 text-base text-ink-gray-8">
							{{ agent.validated_for_fy || "-" }}
						</div>
					</div>
					<div>
						<div class="text-sm font-medium text-ink-gray-5">Access</div>
						<!-- Admins see the roster; everyone else sees only whether THEY
						     are allowed - who else has access is admin information. -->
						<div v-if="isSM" class="mt-1 flex flex-wrap gap-1.5">
							<template v-if="accessGrants.length">
								<Badge
									v-for="g in accessGrants"
									:key="g"
									variant="subtle"
									theme="gray"
									:label="g"
								/>
							</template>
							<span v-else class="text-base text-ink-gray-8">Admins only</span>
						</div>
						<div v-else class="mt-1 text-base text-ink-gray-8">
							{{
								agent.allowed
									? "You have access"
									: "No access - ask your administrator"
							}}
						</div>
					</div>
					<div>
						<div class="text-sm font-medium text-ink-gray-5">Schedule default</div>
						<div class="mt-1 text-base text-ink-gray-8">{{ defaultScheduleText }}</div>
					</div>
				</div>
			</div>

			<!-- ── Configure (installed; §14 F3 + D28 comments) ── -->
			<div v-else-if="tab === 'configure' && installation" class="shrink-0 px-5 py-6">
				<section v-if="canReview" class="mb-10 max-w-2xl">
					<ActivationPanel
						:installation-name="installation.name"
						:agent-title="agent.title"
						:is-scribe="agent.nature === 'Scribe'"
						:state="activation"
						:loading="activationLoading"
						:fetch-error="activationError"
						:can-act="canActOnActivation"
						@promoted="onActivationChanged"
						@demoted="onActivationChanged"
						@retry="loadActivation"
					/>
				</section>

				<!-- Configuration on the left/primary column, Schedule on the right -
				     the same 2-col-on-lg+ pattern as the Admin tab (Access left,
				     Installs right), same section-heading style
				     (text-base font-medium text-ink-gray-9, matching AgentAccessEditor's
				     own "Access" heading) and the same lg:items-start so neither column
				     stretches to the taller one's height - both start at the identical
				     top edge. Comments moved OFF this tab onto the Run itself (owner
				     decision, jarvis#1062) - a Note now belongs to what it is actually
				     about; no divider/empty space is left behind for it. -->
				<div class="grid grid-cols-1 gap-10 lg:grid-cols-2 lg:items-start">
					<div class="min-w-0">
						<section>
							<div class="text-base font-medium text-ink-gray-9">Configuration</div>
							<!-- mt-2, matching AgentAccessEditor's own heading->content gap
							     (its "Access" heading is immediately followed by an
							     mt-2 description) - the Admin tab is the reference. -->
							<ConfigForm
								class="mt-2"
								:config="parsedConfig"
								:config-keys="configKeys"
								:saving="savingConfig"
								@save="saveConfig"
							/>
						</section>
					</div>

					<div class="min-w-0">
						<section>
							<div class="text-base font-medium text-ink-gray-9">Schedule</div>
							<!-- mt-2 for the same reason as Configuration above;
							     space-y-5 (not -4) to match ConfigForm's own
							     field-to-field rhythm - one consistent gap across
							     both columns, not two different densities. -->
							<div class="mt-2 space-y-5">
								<Switch
									label="Run automatically"
									:modelValue="sched.enabled"
									@update:modelValue="(v) => (sched.enabled = v)"
								/>
								<div
									v-if="sched.enabled"
									:class="[
										'grid gap-4',
										sched.frequency === 'daily'
											? 'grid-cols-2'
											: 'grid-cols-3',
									]"
								>
									<FormControl
										type="select"
										label="Frequency"
										:options="FREQUENCY_OPTIONS"
										:modelValue="sched.frequency"
										@update:modelValue="(v) => (sched.frequency = v)"
									/>
									<div>
										<FormLabel label="Time" class="mb-1.5" />
										<TimePicker
											:modelValue="sched.time"
											placeholder="09:00"
											@update:modelValue="(v) => (sched.time = v)"
										/>
									</div>
									<!-- weekly -> weekday name, monthly -> day of month; hidden for
									     daily. Copies the Frequency control above exactly (same
									     FormControl type=select shape) - jarvis#653. -->
									<FormControl
										v-if="sched.frequency !== 'daily'"
										type="select"
										label="Day"
										:options="dayOptions"
										:modelValue="sched.day"
										@update:modelValue="(v) => (sched.day = v)"
									/>
								</div>
								<!-- owner feedback: the committed schedule + its next run, ONE
								     line, shown only while the form matches what is actually
								     saved (scheduleDirty false) - editing any control hides
								     this and reveals Save instead (below). -->
								<div v-if="scheduleSummary" class="text-sm text-ink-gray-5">
									{{ scheduleSummary }}
								</div>
								<!-- owner feedback: Save is not always-on chrome - it appears
								     only once the form actually differs from the saved
								     installation (toggle/frequency/time), and disappears again
								     once a save lands (scheduleDirty recomputes off
								     `installation`, which `load()` refreshes post-save). -->
								<Button
									v-if="scheduleDirty"
									label="Save schedule"
									:loading="savingSchedule"
									@click="saveSchedule"
								/>
							</div>
						</section>
					</div>
				</div>
			</div>

			<!-- ── Runs (installed): two-pane master-detail board ── -->
			<AgentRunsBoard
				v-else-if="tab === 'runs' && installation"
				ref="runsBoard"
				:agent-name="agent.name"
				:can-review="canReview"
			/>

			<!-- ── Admin (SM only; server enforces every call). Listing status is
			     publisher/catalog state curated in registry.json (it reverts on the
			     next deploy) - deliberately NOT editable here. ── -->
			<!-- v-show, NOT v-else-if like the tabs above it: the Access editor holds
			     an unsaved draft, and the v-if chain unmounted it on every tab switch,
			     so a half-finished grant was silently thrown away by a trip to
			     Overview and back. v-if="isSM" still gates it, so a non-admin renders
			     no admin markup at all - only an admin pays the cost of keeping it
			     mounted, and only for the life of this page. -->
			<div v-if="isSM" v-show="tab === 'admin'" class="shrink-0 px-5 py-6">
				<!-- Access and Installs are read together - you grant, then look at who
				     actually has it - and the page had them stacked in a 2xl column that
				     left most of the width empty. Side by side on lg+, stacked below,
				     with the gap doing the separating that space-y-10 used to. -->
				<div class="grid grid-cols-1 gap-10 lg:grid-cols-2 lg:items-start">
					<AgentAccessEditor
						ref="accessEditor"
						:slug="props.slug"
						:roles="agent.allowed_roles || []"
						:users="agent.allowed_users || []"
						:all-roles="agent.all_roles || []"
						@saved="onAccessSaved"
					/>

					<section class="min-w-0">
						<div class="text-base font-medium text-ink-gray-9">
							Installs ({{ installRows.length }})
						</div>
						<div
							v-if="adminLoading && !adminData"
							class="mt-3 text-sm text-ink-gray-5"
						>
							Loading installs…
						</div>
						<div v-else-if="!installRows.length" class="mt-3 text-sm text-ink-gray-5">
							No installs yet.
						</div>
						<ListView
							v-else
							class="mt-3"
							:columns="INSTALL_COLUMNS"
							:rows="installRows"
							row-key="installation"
							:options="{
								selectable: false,
								rowHeight: 40,
								resizeColumn: false,
								showTooltip: true,
							}"
						>
							<template #default>
								<ListHeader>
									<ListHeaderItem
										v-for="column in INSTALL_COLUMNS"
										:key="column.key"
										:item="column"
									/>
								</ListHeader>
								<ListRows />
							</template>
							<template #cell="{ column, row, item, align }">
								<template v-if="column.key === 'state'">
									<!-- State precedence, most-actionable first: Blocked > Disabled >
									     Live > Shadow. `installable` is a last-reconciled STORED flag
									     (see get_agent_admin_overview), so Blocked reads "as last
									     reconciled" rather than claiming a live guarantee. -->
									<Tooltip v-if="!row.installable" :text="blockedReason(row)">
										<Badge variant="subtle" theme="orange" label="Blocked" />
									</Tooltip>
									<Badge
										v-else-if="!row.enabled"
										variant="subtle"
										theme="gray"
										label="Disabled"
									/>
									<Badge
										v-else-if="row.activation_state === 'live'"
										variant="subtle"
										theme="green"
										label="Live"
									/>
									<!-- jarvis#1062 P2-11 (production-readiness audit): the Blocked
									     badge just above already explains itself on hover - Shadow
									     is the only other State value that names a mode without
									     saying what it means. -->
									<Tooltip
										v-else
										text="Preview mode: runs are visible only to the reviewer until promoted to live."
									>
										<ShadowChip label="Shadow" />
									</Tooltip>
								</template>
								<div
									v-else-if="column.key === 'run_as_user'"
									class="truncate text-base"
								>
									<span v-if="row.run_as_user">{{ row.run_as_user }}</span>
									<Badge
										v-else
										variant="subtle"
										theme="orange"
										label="No run-as user"
									/>
								</div>
								<div
									v-else-if="column.key === 'last_run_at'"
									class="truncate text-base"
								>
									{{ row.last_run_at ? timeAgo(row.last_run_at) : "-" }}
								</div>
								<div
									v-else-if="column.key === 'sync_status'"
									class="truncate text-base"
								>
									{{ row.sync_status || "-" }}
								</div>
								<ListRowItem
									v-else
									:column="column"
									:row="row"
									:item="item"
									:align="align"
								/>
							</template>
						</ListView>
					</section>
				</div>
			</div>
		</div>

		<!-- CX5-2: the Custom App Learning agent cannot run without an explicit,
		     per-run app authorization + consent; Run Now opens this first. -->
		<AppSourceConsentDialog
			v-if="needsSourceApps"
			v-model="appPickerOpen"
			:busy="running"
			confirm-label="Start learning"
			@confirm="startRun"
		/>
	</div>
</template>

<script setup>
// Agent detail - /agents/:slug (DESIGN-V3 §7.2, D29/D30 + §14 F3/O1 + §15.4).
// Marketplace template: de-texted hero (logo · name · one meta line ·
// one-line tagline · category chips; install count + Enabled switch right) →
// hash-synced tabs. Overview (markdown description + static facts panel) ·
// Configure (schedule / ConfigForm on the installation, D28) · Runs
// (AgentRunsBoard: two-pane runs rail → findings pane, which now also carries
// Notes-on-the-run, jarvis#1062 - Comments moved off this tab entirely) ·
// Admin (admin-only: the Access editor + installs overview; listing status is
// registry.json publisher state and intentionally has no tenant control here).
import { ref, computed, watch, nextTick } from "vue";
import { onBeforeRouteLeave, useRoute, useRouter } from "vue-router";
import {
	Badge,
	Breadcrumbs,
	Button,
	Dropdown,
	FeatherIcon,
	FormControl,
	FormLabel,
	ListView,
	ListHeader,
	ListHeaderItem,
	ListRows,
	ListRowItem,
	Switch,
	TimePicker,
	Tooltip,
	confirmDialog,
	toast,
} from "frappe-ui";
import LayoutHeader from "@/components/LayoutHeader.vue";
import TabBar from "@/components/list/TabBar.vue";
import AgentRunsBoard from "@/pages/agents/AgentRunsBoard.vue";
import ActivationPanel from "@/pages/agents/ActivationPanel.vue";
import AgentAccessEditor from "@/pages/agents/AgentAccessEditor.vue";
import ConfigForm from "@/pages/agents/ConfigForm.vue";
import AppSourceConsentDialog from "@/components/learning/AppSourceConsentDialog.vue";
import JvSpinner from "@/components/JvSpinner.vue";
import ShadowChip from "@/components/ShadowChip.vue";
import { timeAgo, exactDate as fmtDt } from "@/utils/datetime";
import * as api from "@/api";
import * as apiAgents from "@/api/agents";
import { renderMarkdown } from "@/markdown";
import { errMessage as errMsg, errHtml } from "@/lib/errors";
import { session } from "@/data/session";
import { parseListField } from "@/lib/parseListField";
import { categoryTitle } from "@/lib/agentCategory";
import {
	WEEKDAY_OPTIONS,
	DAY_OF_MONTH_OPTIONS,
	deriveScheduleDay,
	scheduleAnchorPhrase,
} from "@/lib/scheduleAnchor";

const props = defineProps({
	slug: { type: String, required: true },
});

const route = useRoute();
const router = useRouter();

const FREQUENCY_OPTIONS = [
	{ label: "Daily", value: "daily" },
	{ label: "Weekly", value: "weekly" },
	{ label: "Monthly", value: "monthly" },
];
const INSTALL_COLUMNS = [
	{ label: "Owner", key: "owner", width: 2 },
	// The EXECUTING identity, distinct from the owner. A blank one is a legacy /
	// misconfigured row that every dispatch path refuses to run, so it is surfaced
	// here (as a badge) to give an admin an in-product path to the offending row.
	{ label: "Runs as", key: "run_as_user", width: 2 },
	{ label: "State", key: "state", width: "8rem" },
	{ label: "Last run", key: "last_run_at", width: "8rem" },
	{ label: "Sync", key: "sync_status", width: "7rem" },
];

// not_installable_reason is a machine enum; humanise it for the admin's Blocked
// tooltip. Unknown values fall back to a de-underscored form so a newly-added
// reason still reads rather than showing a raw token.
const REASON_LABELS = {
	app_absent_or_ineligible: "Required app is missing or ineligible",
	permission_slice: "Missing a required permission",
	configuration_missing: "Configuration is incomplete",
	record_coverage_insufficient: "Not enough records to run on",
	source_stale: "Source data is stale",
	rule_expired: "An activation rule has expired",
	external_evidence_absent: "Required external evidence is absent",
	run_truncated_watermark: "A prior run was truncated",
	unsupported_customisation: "An unsupported customisation blocks it",
};
function blockedReason(row) {
	const r = row.not_installable_reason;
	const human = r ? REASON_LABELS[r] || r.replace(/_/g, " ") : "";
	// "as last reconciled": installable is a stored flag, not a live re-check.
	return human ? `Can't run (as last reconciled): ${human}.` : "Can't run, as last reconciled.";
}

// ── data ──────────────────────────────────────────────────────────────────────
const agent = ref(null); // get_agent payload (§8.3)
const error = ref("");
// jarvis#1062 fix: Configure/Runs only exist once `installation` resolves
// (tabs computed, below) - true only after this first settles (success or
// error). applyHash() reads this to tell "the hash names a tab that does
// not exist YET" (still loading - wait) apart from "...that never will"
// (settled - give up to Overview for real).
const initialLoadSettled = ref(false);

async function load() {
	try {
		agent.value = (await apiAgents.getAgent(props.slug)) || null;
		error.value = "";
	} catch (e) {
		error.value = errMsg(e);
	}
}
load().then(() => {
	initialLoadSettled.value = true;
	applyHash();
});
watch(
	() => props.slug,
	() => {
		agent.value = null;
		error.value = "";
		adminData.value = null;
		activation.value = null;
		initialLoadSettled.value = false;
		load().then(() => {
			initialLoadSettled.value = true;
			applyHash();
		});
	}
);

const installation = computed(() => (agent.value && agent.value.installation) || null);
// §8.3: all_roles is present only in the SM payload - the Admin-tab signal
const isSM = computed(() => Array.isArray(agent.value && agent.value.all_roles));
const updateAvailable = computed(
	() =>
		!!(
			installation.value &&
			installation.value.installed_version &&
			agent.value.version &&
			installation.value.installed_version !== agent.value.version
		)
);

// ── PP-4 activation (jarvis#456) ─────────────────────────────────────────────
// activation_state/reviewer/run_as_user/promoted_by/promoted_at aren't in
// get_agent's frozen §8.3 `installation` shape - fetched separately (see
// api/agents.getInstallationActivation) so both the hero badge and the
// Configure-tab ActivationPanel read one shared, always-in-sync value.
const activation = ref(null);
const activationLoading = ref(false);
const activationError = ref("");

async function loadActivation() {
	if (!installation.value) {
		activation.value = null;
		return;
	}
	activationLoading.value = true;
	activationError.value = "";
	try {
		activation.value =
			(await apiAgents.getInstallationActivation(installation.value.name)) || null;
	} catch (e) {
		activation.value = null;
		activationError.value = errMsg(e);
	} finally {
		activationLoading.value = false;
	}
}
watch(
	() => installation.value && installation.value.name,
	(name) => {
		if (name) loadActivation();
		else activation.value = null;
	},
	{ immediate: true }
);

// ── reviewer capability (jarvis#1062) ────────────────────────────────────────
// The reviewer set (Jarvis Skill Reviewer / Jarvis Admin / System Manager), read
// from get_agents_caps().review - the SAME capability apply_agents and
// promote_installation gate on, so the button appears exactly when the call
// would succeed. It replaced `session.user === activation.reviewer`: install_agent
// stamps reviewer = the installer, so that test made every installer their own
// approver and put the whole shadow/attestation vocabulary in front of people who
// have no say over it. A plain user now sees nothing about shadow at all.
const canReview = ref(false);
(async () => {
	try {
		canReview.value = !!((await apiAgents.getAgentsCaps()) || {}).review;
	} catch {
		canReview.value = false; // fail closed: no control, no shadow copy
	}
})();
const canActOnActivation = computed(() => !!activation.value && canReview.value);
function onActivationChanged(next) {
	activation.value = next;
}

// ── hash-synced tabs (useActiveTabManager pattern) ───────────────────────────
const tab = ref("overview");
const runsBoard = ref(null);

const tabs = computed(() => {
	const out = [{ label: "Overview", value: "overview" }];
	if (installation.value) {
		out.push({ label: "Configure", value: "configure" });
		out.push({ label: "Runs", value: "runs" });
	}
	if (isSM.value) out.push({ label: "Admin", value: "admin" });
	return out;
});

function applyHash() {
	const h = (route.hash || "").replace(/^#/, "");
	if (!h) {
		tab.value = "overview";
		return;
	}
	if (tabs.value.some((t) => t.value === h)) {
		tab.value = h;
		return;
	}
	// h names a tab that is not in the set RIGHT NOW - Configure/Runs need
	// `installation`, which is only known once the async get_agent fetch
	// resolves. Only a settled load makes "not in tabs.value" a real verdict;
	// until then, leave tab.value alone (the page shows "Loading agent…"
	// regardless) rather than lock in Overview and drop the requested tab -
	// load()/the slug watcher re-run applyHash() once settled.
	if (initialLoadSettled.value) tab.value = "overview";
}
function setTab(v) {
	if (tab.value === v && route.hash === "#" + v) return;
	tab.value = v;
	router.push({ hash: "#" + v });
}
// back/forward restores the tab
watch(
	() => route.hash,
	() => {
		if (route.name === "AgentDetail") applyHash();
	}
);
// tab set no longer valid (e.g. after uninstall) → fall back to overview
watch(tabs, (list) => {
	if (!list.some((t) => t.value === tab.value)) tab.value = "overview";
});

// ── header actions ────────────────────────────────────────────────────────────
const installing = ref(false);
// jarvis#1062 polish: the toast auto-dismisses after ~2s, easy to miss - keep
// a persistent inline echo under the Install button until the next attempt.
const installError = ref("");
// jarvis#1062 polish: install_agent refuses a run-as identity of
// Administrator ("agents cannot run as Administrator") - read the session
// user the same way @/data/session already exposes it elsewhere, and
// disable Install client-side instead of letting the click round-trip into
// a toast-only refusal.
const isAdministratorSession = computed(() => session.user === "Administrator");
const canInstall = computed(
	() =>
		!!(
			agent.value &&
			agent.value.allowed &&
			agent.value.status === "Published" &&
			!isAdministratorSession.value
		)
);
const installTooltip = computed(() => {
	if (!agent.value || canInstall.value) return "";
	if (isAdministratorSession.value) return "Log in as a named user to install this agent.";
	// Never the roster: get_agent strips allowed_roles/allowed_users for non-admins,
	// and naming who DOES have access is admin information anyway.
	if (!agent.value.allowed)
		return "You do not have access to this agent. Ask your administrator.";
	return agent.value.status === "Coming Soon" ? "Coming soon" : "Not available to install";
});

async function install() {
	if (installing.value || !canInstall.value) return;
	installing.value = true;
	installError.value = "";
	const p = api.installAgent(props.slug);
	toast.promise(p, {
		loading: "Installing…",
		success: () => `${agent.value.title} installed`,
		error: (e) => errMsg(e),
	});
	try {
		await p;
	} catch (e) {
		installError.value = errMsg(e);
		installing.value = false;
		return;
	}
	await load();
	installing.value = false;
	setTab("configure");
}

const running = ref(false);
// On-demand run is offered for read-only auditors AND scribes (mirrors the
// backend run_agent_now gate: nature in Auditor/Scribe); operators draft through
// the Approval Board and never run on demand.
// A scribe writes the live Org wiki directly (no shadow holding pen - see
// agents_api.run_agent_now), so the backend refuses it outright while shadow
// (jarvis#456). Block it here too rather than let the click round-trip into
// that refusal - an auditor has no such restriction, its findings just land
// in the reviewer-only preview set instead.
const shadowScribeBlocked = computed(
	() =>
		!!(
			agent.value &&
			agent.value.nature === "Scribe" &&
			activation.value &&
			activation.value.activation_state !== "live"
		)
);
const runDisabled = computed(
	() =>
		!installation.value ||
		!installation.value.enabled ||
		(agent.value && !["Auditor", "Scribe"].includes(agent.value.nature)) ||
		!(agent.value && agent.value.allowed) ||
		shadowScribeBlocked.value
);
const runTooltip = computed(() => {
	if (!agent.value || !installation.value) return "";
	const nature = agent.value.nature;
	if (nature !== "Auditor" && nature !== "Scribe")
		return "Operators draft through the Approval Board - no on-demand runs";
	if (!installation.value.enabled) return "Enable the agent first";
	if (!agent.value.allowed) return "You do not have access to this agent";
	// The shadow vocabulary is reviewer language (jarvis#1062): a plain user has
	// no promote control and no say in the sign-off, so telling them to go and
	// promote it names an action they cannot take.
	if (shadowScribeBlocked.value)
		return canReview.value
			? "Still in shadow preview - promote it to live under Configure first"
			: "Not yet enabled for live runs - ask your administrator";
	return nature === "Scribe" ? "Run this agent now" : "Run this audit now";
});

// CX5-2: the Custom App Learning agent reads customer SOURCE, so a run must name
// the apps it is authorised to read. Run Now opens the consent dialog for it; the
// server refuses a launch without a validated selection either way.
const APP_LEARNING_SLUG = "custom-app-learning";
const appPickerOpen = ref(false);
const needsSourceApps = computed(() => props.slug === APP_LEARNING_SLUG);

function runNow() {
	if (running.value || runDisabled.value) return;
	if (needsSourceApps.value) {
		appPickerOpen.value = true;
		return;
	}
	return startRun();
}

async function startRun(sourceApps) {
	if (running.value || runDisabled.value) return;
	running.value = true;
	try {
		await api.runAgentNow(
			installation.value.name,
			sourceApps && sourceApps.length ? { source_apps: sourceApps } : undefined
		);
		appPickerOpen.value = false;
		toast.success(
			agent.value && agent.value.nature === "Scribe" ? "Run started" : "Audit started"
		);
		setTab("runs");
		await nextTick();
		// jump the board to the freshly queued run (clears hiding facets)
		if (runsBoard.value) runsBoard.value.reload({ selectNewest: true });
		load(); // refresh last_run_at etc. in the background
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		running.value = false;
	}
}

function confirmUninstall() {
	if (!installation.value) return;
	const name = installation.value.name;
	confirmDialog({
		title: `Uninstall ${agent.value.title}?`,
		// the backend cascade-deletes findings → runs → installation, but never
		// touches a run's linked Jarvis Dashboard (#1062 polish - the warning was
		// overclaiming what actually gets deleted).
		message:
			"This removes the agent and its run history and findings; saved dashboards are kept. This can't be undone.",
		onConfirm: async ({ hideDialog }) => {
			try {
				await api.uninstallAgent(name);
				hideDialog();
				toast.success(`${agent.value.title} uninstalled`);
				router.push({ name: "AgentsList" });
			} catch (e) {
				toast.error(errHtml(e));
			}
		},
	});
}

const togglingEnabled = ref(false);
async function setEnabled(v) {
	if (!installation.value || togglingEnabled.value) return;
	togglingEnabled.value = true;
	try {
		await api.setAgentEnabled(installation.value.name, v ? 1 : 0);
		await load();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		togglingEnabled.value = false;
	}
}

// ── hero + overview helpers ───────────────────────────────────────────────────
const logoText = computed(() =>
	String((agent.value && agent.value.title) || props.slug || "?")
		.slice(0, 2)
		.toUpperCase()
);
// §15.4 - ONE meta line: "by {publisher} · v{version}" (badges follow inline)
const heroMetaText = computed(() => {
	const parts = ["by " + ((agent.value && agent.value.publisher) || "Jarvis")];
	if (agent.value && agent.value.version && agent.value.version !== "0.0.0") {
		parts.push("v" + agent.value.version);
	}
	return parts.join(" · ");
});
// §15.4 - one-line tagline: first non-empty description line, heading markers
// stripped; the full markdown renders only in the Overview tab
const tagline = computed(() => {
	const d = (agent.value && agent.value.description) || "";
	const line = d.split("\n").find((l) => l.trim()) || "";
	return line.replace(/^#{1,6}\s+/, "").trim();
});
// O1 - renderMarkdown (jv-md-* classes are global via the main chunk)
const descriptionHtml = computed(() =>
	agent.value && agent.value.description ? renderMarkdown(agent.value.description) : ""
);
const needs = computed(() => {
	if (!agent.value) return [];
	return ["tools_required", "min_apps"].flatMap((key) => parseListField(agent.value[key]));
});
// jarvis#1062 polish: shares parseListField with `needs`, its own field/
// heading - doctypes_required (A12) is a distinct concept (records read,
// not tools/apps).
const readsRecords = computed(() =>
	agent.value ? parseListField(agent.value.doctypes_required) : []
);
// jarvis#1063 (jarvis-only half): which agent-specific config keys this
// agent's bundle actually reads (get_agent) - gates ConfigForm's
// agent-specific fields; [] shows its "no additional settings" note.
const configKeys = computed(() => (agent.value ? parseListField(agent.value.config_keys) : []));
const defaultScheduleText = computed(() => {
	let s = {};
	try {
		s = JSON.parse((agent.value && agent.value.default_schedule) || "{}") || {};
	} catch (e) {
		s = {};
	}
	const freq = String(s.schedule_frequency || "").toLowerCase();
	if (!freq) return "None - runs on demand.";
	return s.schedule_enabled ? `On by default · ${freq}` : `Off by default · suggested ${freq}`;
});
// ── Configure: schedule ───────────────────────────────────────────────────────
const sched = ref({ enabled: false, frequency: "daily", time: "09:00", day: "" });
const savingSchedule = ref(false);
// jarvis#653: weekly -> weekday names, monthly -> 1..31 (ordinal labels) - the
// Day control's own option list, keyed off the DRAFT frequency so it flips the
// instant Frequency changes, before any save.
const dayOptions = computed(() =>
	sched.value.frequency === "weekly" ? WEEKDAY_OPTIONS : DAY_OF_MONTH_OPTIONS
);
// jarvis#653: guards the interactive-frequency-change watcher below from firing
// off the SEED watch's own frequency+day assignment (which legitimately sets
// day to "" for a legacy row with no anchor saved - the defaulting watcher must
// not treat that as "needs a default"). True for exactly the synchronous
// pre-flush window the seed watch's own effect runs in; nextTick clears it once
// the DOM update (and any watcher chained off THIS assignment) has settled.
let seedingSchedule = false;
// seed once per installation (a background reload must not clobber edits)
watch(
	() => installation.value && installation.value.name,
	(name) => {
		if (!name) return;
		seedingSchedule = true;
		const inst = installation.value;
		const freq = inst.schedule_frequency || "daily";
		sched.value = {
			enabled: !!inst.schedule_enabled,
			frequency: freq,
			time: timeHHMM(inst.schedule_time) || "09:00",
			day: deriveScheduleDay(freq, inst.schedule_weekday, inst.schedule_day_of_month),
		};
		nextTick(() => {
			seedingSchedule = false;
		});
	},
	{ immediate: true }
);
// jarvis#653: an interactive Frequency change (NOT the seed above, guarded off
// by `seedingSchedule`) needs its OWN default day - "Monday" for weekly, the
// 1st for monthly - so the Day select never shows a visible option ("Monday")
// while the model is empty (a phantom selection that would silently save
// nothing). Only fills in when the current day does not already belong to the
// new frequency's own option set, so switching away and back keeps whatever
// the owner picked.
watch(
	() => sched.value.frequency,
	(freq) => {
		if (seedingSchedule) return;
		if (freq === "weekly" && !WEEKDAY_OPTIONS.some((o) => o.value === sched.value.day)) {
			sched.value.day = "Monday";
		} else if (
			freq === "monthly" &&
			!DAY_OF_MONTH_OPTIONS.some((o) => o.value === sched.value.day)
		) {
			sched.value.day = "1";
		}
	}
);
// owner feedback: the ACTUAL saved shape, in `sched`'s own shape, derived
// live off `installation` (not a second seeded ref) - so it tracks a
// successful save's post-`load()` refresh for free, with no manual
// dirty-reset bookkeeping to get wrong.
const savedSched = computed(() => {
	const inst = installation.value;
	const freq = (inst && inst.schedule_frequency) || "daily";
	return {
		enabled: !!(inst && inst.schedule_enabled),
		frequency: freq,
		time: timeHHMM(inst && inst.schedule_time) || "09:00",
		day: deriveScheduleDay(
			freq,
			inst && inst.schedule_weekday,
			inst && inst.schedule_day_of_month
		),
	};
});
const scheduleDirty = computed(
	() =>
		sched.value.enabled !== savedSched.value.enabled ||
		sched.value.frequency !== savedSched.value.frequency ||
		// timeHHMM()-normalize both sides - TimePicker's own modelValue may
		// carry seconds ("10:30:00"), while savedSched.time is already
		// normalized to "HH:MM"; comparing raw would leave Save stuck visible
		// after a successful time-only save (the two would never look equal).
		timeHHMM(sched.value.time) !== savedSched.value.time ||
		// both sides are always the SAME string shape (deriveScheduleDay / a
		// native select's modelValue), so a plain compare is enough - no
		// int-vs-string mismatch to normalize the way schedule_day_of_month
		// arrives from the server.
		sched.value.day !== savedSched.value.day
);
// "Scheduled monthly on the 15th at 9:00 am. Next run: Sat, Oct 3, 2026 9:00 AM"
// (or, with no day anchor saved, the plain legacy wording: "Scheduled monthly
// at 9:00 am. ...") - the SAVED frequency/time/day (never the unsaved draft)
// plus the existing next_run_at rendering (fmtDt); empty while dirty (would
// describe a state that no longer matches the form), when nothing is actually
// scheduled, or when the SAVED schedule is off (a legacy/stale next_run_at
// must not be narrated as a live schedule - "saved AND enabled").
const scheduleSummary = computed(() => {
	if (scheduleDirty.value || !savedSched.value.enabled) return "";
	const nextRunAt = installation.value && installation.value.next_run_at;
	if (!nextRunAt) return "";
	const anchor = scheduleAnchorPhrase(savedSched.value.frequency, savedSched.value.day);
	return (
		`Scheduled ${savedSched.value.frequency}${anchor ? ` ${anchor}` : ""} ` +
		`at ${formatTime12h(savedSched.value.time)}. Next run: ${fmtDt(nextRunAt)}`
	);
});

async function saveSchedule() {
	if (!installation.value || savingSchedule.value) return;
	savingSchedule.value = true;
	try {
		await api.setAgentSchedule(installation.value.name, {
			schedule_enabled: sched.value.enabled ? 1 : 0,
			schedule_frequency: sched.value.frequency,
			schedule_time: sched.value.time || "",
			schedule_weekday: sched.value.frequency === "weekly" ? sched.value.day || "" : "",
			schedule_day_of_month:
				sched.value.frequency === "monthly" ? Number(sched.value.day) || 0 : 0,
		});
		toast.success("Schedule saved");
		await load();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		savingSchedule.value = false;
	}
}

// ── Configure: config JSON → ConfigForm (§14 F3) ─────────────────────────────
// keyed off the JSON *string* so unrelated reloads don't reseed the form
const parsedConfig = ref({});
watch(
	() => (installation.value && installation.value.config) || "{}",
	(raw) => {
		try {
			parsedConfig.value = JSON.parse(raw) || {};
		} catch (e) {
			parsedConfig.value = {};
		}
	},
	{ immediate: true }
);

const savingConfig = ref(false);
async function saveConfig(merged) {
	if (!installation.value || savingConfig.value) return;
	savingConfig.value = true;
	try {
		await api.setAgentConfig(installation.value.name, merged);
		toast.success("Configuration saved");
		await load();
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		savingConfig.value = false;
	}
}

// jarvis#1062: Comments moved off Configure onto the Run itself
// (FindingsPanel.vue's new Notes section) - existing installation comments
// are deliberately left in the DB, unmigrated, just no longer surfaced here.

// ── Admin (SM) ────────────────────────────────────────────────────────────────
const adminData = ref(null); // {roles, listings} from get_agent_admin_overview
const adminLoading = ref(false);

async function loadAdmin() {
	if (adminLoading.value) return;
	adminLoading.value = true;
	try {
		adminData.value = (await api.getAgentAdminOverview()) || null;
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		adminLoading.value = false;
	}
}
watch(
	tab,
	(v) => {
		if (v === "admin" && isSM.value && !adminData.value) loadAdmin();
	},
	{ immediate: true }
);

const adminListing = computed(() => {
	const listings = (adminData.value && adminData.value.listings) || [];
	return listings.find((l) => l.agent_slug === props.slug) || null;
});
const installRows = computed(() => (adminListing.value && adminListing.value.installs) || []);

// ── access (jarvis#1062) ──────────────────────────────────────────────────────
// The editor itself is AgentAccessEditor.vue; the detail page only owns the
// saved value, so the Overview summary and the Admin tab cannot disagree.
const accessGrants = computed(() => [
	...((agent.value && agent.value.allowed_roles) || []),
	...((agent.value && agent.value.allowed_users) || []),
]);
function onAccessSaved(next) {
	if (!agent.value) return;
	agent.value.allowed_roles = next.allowed_roles || [];
	agent.value.allowed_users = next.allowed_users || [];
	load(); // re-read `allowed` - an admin can lock themselves out of the user surface
}

// ── leaving the page with an unsaved access draft ────────────────────────────
// Keeping the editor mounted saves the draft from a TAB switch; this saves it
// from a ROUTE change. frappe-ui's confirmDialog, not useConfirm(): its own docs
// say the frappe-ui-styled list/detail pages (agents among them) use frappe-ui's
// dialog and the jv- surfaces use the other one - "don't cross the streams".
const accessEditor = ref(null);
// Set only for the navigation we have already asked about, so confirming does
// not re-open the dialog on the re-push below.
let leaveConfirmed = false;

onBeforeRouteLeave((to) => {
	if (leaveConfirmed || !accessEditor.value?.dirty) return true;
	confirmDialog({
		title: "Discard unsaved access changes?",
		message:
			"Your changes to who can use this agent have not been saved. Leaving now discards them.",
		onConfirm: ({ hideDialog }) => {
			hideDialog();
			leaveConfirmed = true;
			router.push(to.fullPath);
		},
	});
	// Refuse the navigation rather than holding a `next` callback open: a
	// dismissed dialog (Escape, backdrop) never calls onConfirm, and a guard that
	// was waiting on that callback would wedge routing for the rest of the
	// session. Cancelling costs nothing - the user is already where they were.
	return false;
});

// ── formatting helpers ────────────────────────────────────────────────────────
// "9:00:00" (python str(timedelta)) → "09:00" for the TimePicker
function timeHHMM(s) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(s || ""));
	return m ? `${m[1].padStart(2, "0")}:${m[2]}` : "";
}
// "09:00" → "9:00 am" - mirrors TimePicker's OWN formatDisplay (frappe-ui,
// use12Hour default) exactly, so the schedule summary line reads the same
// way the picker itself would show that time.
function formatTime12h(hhmm) {
	const m = /^(\d{1,2}):(\d{2})/.exec(String(hhmm || ""));
	if (!m) return "";
	const h = parseInt(m[1], 10);
	const am = h < 12;
	const h12 = h % 12 === 0 ? 12 : h % 12;
	return `${h12}:${m[2]} ${am ? "am" : "pm"}`;
}
</script>
