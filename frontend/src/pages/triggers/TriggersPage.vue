<template>
	<div class="flex h-full flex-col overflow-hidden">
		<!-- friendly no-access state: get_triggers_caps rejected with a real 403
		     (SkillsPage probe precedent - transient failures retry, never block) -->
		<template v-if="accessDenied">
			<div class="flex flex-1 flex-col items-center justify-center gap-3 px-8 text-center">
				<FeatherIcon name="git-branch" class="size-7.5 text-ink-gray-5" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-lg font-medium text-ink-gray-8">No access to Triggers</span>
					<span class="text-p-base text-ink-gray-6">
						Ask your Jarvis admin for access to automations.
					</span>
				</div>
			</div>
		</template>

		<template v-else>
			<TabBar
				class="shrink-0"
				:tabs="TABS"
				:model-value="activeTab"
				@update:model-value="setTab"
			/>

			<!-- ============ Triggers tab: triggers list (full width) ============
			     The "Create with chat" pane was removed - triggers are created and
			     managed from the list itself (New trigger -> TriggerDetail form). -->
			<TriggersListPane
				v-if="activeTab === 'triggers'"
				class="min-h-0 flex-1"
				:caps="caps"
			/>

			<!-- ============ Activity tab ============ -->
			<ActivityTab
				v-else
				class="min-h-0 flex-1"
				:caps="caps"
				:initial-trigger="initialActivityTrigger"
			/>
		</template>
	</div>
</template>

<script setup>
// TriggersPage - the routed component for /triggers: hash-synced tab shell
// (SkillsPage/Agents precedent; no hash = Triggers, "#activity" = Activity)
// plus the single get_triggers_caps probe that feeds both tabs. Tab 1 is the
// full-width triggers list; triggers are created and managed there (New trigger
// -> TriggerDetail form), not via a chat pane. Probe failures follow the
// SkillsPage rule: a genuine 403 shows the no-access state; a transient
// 500/network blip retries once and otherwise proceeds with default caps
// (read-only rendering) rather than blocking an authorized user.
import { ref, computed, watch, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { FeatherIcon } from "frappe-ui";
import TabBar from "@/components/list/TabBar.vue";
import TriggersListPane from "./TriggersListPane.vue";
import ActivityTab from "./ActivityTab.vue";
import { getTriggersCaps } from "@/api/triggers";

const route = useRoute();
const router = useRouter();

const TABS = [
	{ label: "Triggers", value: "triggers" },
	{ label: "Activity", value: "activity" },
];

// caps flow down reactively - admin affordances (New/bulk/enable toggles)
// appear once the probe lands; the default keeps everything read-only.
const caps = ref({
	can_manage: false,
	scripts_enabled: false,
	stt_enabled: false,
	events: [],
	llm_events: [],
});
const accessDenied = ref(false);

// "View all" from a trigger's Recent activity deep-links with ?trigger=<id>
// so the Activity tab opens pre-filtered to that trigger.
const initialActivityTrigger = computed(() =>
	typeof route.query.trigger === "string" ? route.query.trigger : ""
);

// ── hash-synced tabs (SkillsPage precedent; no gating - both tabs are public) ─
const activeTab = ref("triggers");
function applyHash() {
	// tolerate suffixed forms like "#activity?x=1"
	const h = (route.hash || "").replace(/^#/, "").split("?")[0];
	activeTab.value = h === "activity" ? "activity" : "triggers";
}
// Activity's `?trigger=` deep-link is the only key that belongs to ONE tab.
// Everything else in the query belongs to the page — above all the Triggers
// list's `fv2` filter set — and survives a tab switch in both directions.
//
// This used to replace the whole query with `{}` on the way back, which was
// harmless while Triggers owned nothing in the URL and became a silent wipe of
// the user's filters the moment this surface migrated. Same shape as
// DashboardsPage's `_withoutEditSeed`.
function _withoutActivitySeed() {
	const q = { ...route.query };
	delete q.trigger;
	return q;
}
function setTab(v) {
	if (v === activeTab.value) return;
	activeTab.value = v;
	const query = v === "activity" ? { ...route.query } : _withoutActivitySeed();
	router.push({ hash: v === "triggers" ? "" : `#${v}`, query });
}
applyHash();
// back/forward restores the tab (guard to this route so other pages' hashes
// are ignored - the SkillsPage rule)
watch(
	() => route.hash,
	() => {
		if (route.name === "TriggersPage") applyHash();
	}
);

// ── caps probe (403 vs transient, SkillsPage pattern) ────────────────────────
function isPermissionError(e) {
	return !!(e && (e.status === 403 || e.exc_type === "PermissionError"));
}

onMounted(async () => {
	let fresh = null;
	try {
		fresh = await getTriggersCaps();
	} catch (e) {
		if (isPermissionError(e)) {
			accessDenied.value = true;
			return;
		}
		// transient (500/network) - retry once before giving up
		await new Promise((r) => setTimeout(r, 1000));
		try {
			fresh = await getTriggersCaps();
		} catch (e2) {
			if (isPermissionError(e2)) {
				accessDenied.value = true;
				return;
			}
			// still transient - keep the read-only defaults instead of blocking
			console.warn("get_triggers_caps failed twice; keeping read-only caps", e2);
		}
	}
	if (fresh) caps.value = { ...caps.value, ...fresh };
});
</script>
