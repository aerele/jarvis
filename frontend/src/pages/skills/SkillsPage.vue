<template>
	<div class="flex h-full flex-col overflow-hidden">
		<!-- Tab strip renders once the single get_skills_area_caps probe (below)
		     has resolved with at least one true flag. Skills always shows;
		     Personalise + Wiki (+ Knowledge Graph flag) gate on caps.personalise,
		     Analysis on caps.analysis, Review on caps.review - two INDEPENDENT
		     flags now (a Jarvis Skill Reviewer without the admin role sees Review
		     but not Analysis; a Jarvis Admin sees both, since the admin role set
		     is a subset of the reviewer set server-side). Portal/guest sessions
		     that fail the probe entirely see exactly today's Skills list with no
		     tab chrome (zero regression). The active tab stays "skills" until the
		     probe lands, so SkillsList mounts once and is NOT remounted when the
		     strip later appears - only clicking another tab swaps the body (v-if
		     so exactly one LayoutHeader teleports to #app-header at a time, the
		     Macros-page precedent). -->
		<TabBar
			v-if="personaliseAllowed || analysisAllowed || reviewAllowed"
			class="shrink-0"
			:tabs="tabs"
			:model-value="activeTab"
			@update:model-value="setTab"
		/>

		<SkillsList v-if="activeTab === 'skills'" class="min-h-0 flex-1" />
		<AnalysisTab
			v-else-if="activeTab === 'analysis'"
			class="min-h-0 flex-1"
			@changed="refreshBadge"
		/>
		<ReviewTab
			v-else-if="activeTab === 'review'"
			class="min-h-0 flex-1"
			@changed="refreshBadge"
		/>
		<WikiTab v-else-if="activeTab === 'wiki'" class="min-h-0 flex-1" />
		<KnowledgeGraph v-else-if="activeTab === 'graph'" class="min-h-0 flex-1" />
		<PersonaliseTab v-else class="min-h-0 flex-1" :caps="capsSeed" />
	</div>
</template>

<script setup>
// SkillsPage - the routed component for /skills (Skills-area rework,
// DESIGN.md §5/§6/§6b): the hash-synced tab shell, extended (not replaced)
// from the plan §6.4 shape. Tab "Skills" renders SkillsList.vue unchanged;
// tab "Personalise" (#personalise, the renamed/rebuilt former "Business" tab)
// renders the Questions/Notes capture surface; tab "Wiki" (#wiki) renders the
// scope-aware org wiki; tab "Analysis" (#analysis) renders the pattern-
// learning settings + run telemetry; tab "Review" (#review) renders the
// pattern/promotion decision queue + decided log.
//
// Gating used to be two independent booleans from two probes
// (`get_learning_status` = System-Manager-only, `get_business_status` = any
// desk user). DESIGN.md §6 collapses that into ONE probe,
// `get_skills_area_caps`, returning four independent flags
// {personalise, wiki, analysis, review} plus the Personalise unanswered-
// question count - so a single round-trip now drives every gate below
// instead of N. Analysis and Review are DECOUPLED role sets server-side
// (Jarvis Admin | System Manager unlocks Analysis; Jarvis Skill Reviewer |
// Jarvis Admin | System Manager unlocks Review) - a reviewer with no admin
// role sees Review without Analysis.
//
// "#business" (the pre-rework hash) redirects (replace, so back doesn't
// loop) to "#personalise" - the exact same muscle-memory precedent
// "#learning" already gets for "#review".
import { ref, computed, onMounted, onBeforeUnmount, watch, inject } from "vue";
import { useRoute, useRouter } from "vue-router";
import TabBar from "@/components/list/TabBar.vue";
import SkillsList from "./SkillsList.vue";
import AnalysisTab from "./AnalysisTab.vue";
import ReviewTab from "./ReviewTab.vue";
import PersonaliseTab from "./PersonaliseTab.vue";
import WikiTab from "./WikiTab.vue";
import KnowledgeGraph from "@/pages/wiki/KnowledgeGraph.vue";
import { renderer3dEnabled } from "wiki-graph-core";
import { getReviewAccess } from "@/api/learning";
import { getSkillsAreaCaps } from "@/api/personalise";

const route = useRoute();
const router = useRouter();
// The same shared socket AppShell's global notifier listens on (main.js
// provides it app-wide; null under ?nosocket/headless screenshots) - reused
// here for the Personalise unanswered-count badge's realtime refresh instead
// of re-polling the probe.
const socket = inject("$socket", null);

const personaliseAllowed = ref(false);
const analysisAllowed = ref(false);
const reviewAllowed = ref(false);
const learningPending = ref(0);
// Raw caps probe result, seeded into <PersonaliseTab :caps> so its own
// stt/admin-gate/unanswered-count render immediately instead of flashing their
// all-false defaults until the tab's own loadCaps() resolves (DESIGN §6b).
const capsSeed = ref({});
// Personalise tab badge (DESIGN.md §5c/§6: "unanswered-count badge on the
// Personalise tab, realtime-refreshed... NO per-question toasts"). Seeded
// from the caps probe, then kept live by the `personalise:question` event
// below - no polling.
const unansweredCount = ref(0);
const activeTab = ref("skills");
// Knowledge Graph tab is behind the per-surface renderer flag (R10, default off);
// flip via localStorage.wg3d = "on" until the 3D renderer soaks in prod.
const graph3dOn = renderer3dEnabled();

const tabs = computed(() => {
	const t = [{ label: "Skills", value: "skills" }];
	if (personaliseAllowed.value) {
		t.push({
			label: "Personalise",
			value: "personalise",
			count: unansweredCount.value || null,
		});
		t.push({ label: "Wiki", value: "wiki" });
		if (graph3dOn) t.push({ label: "Knowledge Graph", value: "graph" });
	}
	if (analysisAllowed.value) t.push({ label: "Analysis", value: "analysis" });
	if (reviewAllowed.value)
		t.push({ label: "Review", value: "review", count: learningPending.value || null });
	return t;
});

// hash-synced tabs (Agents precedent; no hash = Skills). "#analysis"/"#review"
// only resolve once the caps probe has confirmed that gate, and "#personalise"/
// "#wiki"/"#graph" once the personalise gate is confirmed - an unauthorized
// deep link falls back to the Skills tab. "#learning" is the pre-split name
// for the combined board; "#business" is the pre-rework Personalise name;
// both redirect (replace, so back doesn't loop) to their new homes.
// "#skills" is intentionally never used (the router keeps that legacy chat
// deep-link mapping /jarvis/#skills → /skills untouched).
function applyHash() {
	// tolerate suffixed forms like "#wiki?page=x" - land on the right tab
	// instead of silently falling through to Skills
	const h = (route.hash || "").replace(/^#/, "").split("?")[0];
	if (h === "review" && reviewAllowed.value) activeTab.value = "review";
	else if (h === "analysis" && analysisAllowed.value) activeTab.value = "analysis";
	else if (h === "learning" && reviewAllowed.value) {
		activeTab.value = "review";
		router.replace({ hash: "#review" });
	} else if (h === "business" && personaliseAllowed.value) {
		activeTab.value = "personalise";
		router.replace({ hash: "#personalise" });
	} else if (h === "personalise" && personaliseAllowed.value) activeTab.value = "personalise";
	else if (h === "wiki" && personaliseAllowed.value) activeTab.value = "wiki";
	else if (h === "graph" && graph3dOn && personaliseAllowed.value) activeTab.value = "graph";
	else {
		activeTab.value = "skills";
		// A disallowed/unknown hash falls back to Skills - also clear it from the
		// URL (replace, so back doesn't loop) or the address bar keeps claiming
		// e.g. "#review" while the Skills tab renders (QA finding B17). Only after
		// the caps probe settles: the setup-time applyHash() runs with all gates
		// still false, and clearing there would destroy a legitimate deep link
		// (#personalise) before the probe can honor it.
		if (h && probeSettled) router.replace({ hash: "" });
	}
}
let probeSettled = false;
function setTab(v) {
	if (v === activeTab.value) return;
	if (v === "analysis" && !analysisAllowed.value) return;
	if (v === "review" && !reviewAllowed.value) return;
	if ((v === "personalise" || v === "wiki") && !personaliseAllowed.value) return;
	if (v === "graph" && !(graph3dOn && personaliseAllowed.value)) return;
	activeTab.value = v;
	router.push({ hash: v === "skills" ? "" : `#${v}` });
}
applyHash();
// back/forward restores the tab (guard to this route so other pages' hashes,
// e.g. doc-page tabs, are ignored)
watch(
	() => route.hash,
	() => {
		if (route.name === "SkillsList") applyHash();
	}
);

async function refreshBadge() {
	// The Review badge is reviewer-gated server-side (`get_review_access` is the
	// reviewer-set probe) - skip the call entirely for viewers who can't reach
	// Review at all, rather than relying on the catch to swallow the 403 silently.
	if (!reviewAllowed.value) return;
	try {
		// The Review tab holds THREE actionable queues - learned patterns, wiki
		// promotions and skill promotions. Count all pending review work in one
		// probe so a pending promotion (the wiki requester side is finally wired,
		// and skills' reviewer queue is new) is never invisible on the tab.
		// pending_patterns == the old pending_learned_count exactly.
		const a = (await getReviewAccess()) || {};
		learningPending.value =
			(a.pending_patterns || 0) +
			(a.pending_promotions || 0) +
			(a.pending_skill_promotions || 0);
	} catch (e) {
		// best-effort badge; a transient failure must not disturb the page
	}
}

// Realtime unanswered-count refresh (DESIGN.md §3/§6b: `personalise:question`
// on the existing `jarvis:event` channel, `{kind, count}`). Reuses the exact
// listener idiom `notify/globalNotifier.js` already established (attach on
// mount, detach on unmount, same shared socket) rather than inventing a new
// mechanism - the payload already carries the fresh count, so no re-fetch
// round-trip is needed, just a straight assignment.
function attachRealtime() {
	if (!socket) return null;
	function onEvent(p) {
		if (p && p.kind === "personalise:question" && typeof p.count === "number") {
			unansweredCount.value = p.count;
			// keep the seed in lockstep so PersonaliseTab's own Questions badge
			// (driven by the seeded caps.unanswered_count) can't diverge from the
			// outer tab badge when a question arrives in real time.
			capsSeed.value = { ...capsSeed.value, unanswered_count: p.count };
		}
	}
	socket.on("jarvis:event", onEvent);
	return () => socket.off("jarvis:event", onEvent);
}
let detachRealtime = null;

// A genuine permission rejection (Guest/portal) is the ONLY reason to hide the
// whole tab strip; a transient 500/network blip must not. Collapsing the two
// old probes into one means a single failed call now gates Personalise + Wiki +
// Analysis + Review together, so distinguish the two: 403/PermissionError →
// legitimately no tabs; anything else → retry once, and if that also fails keep
// the previously-known flags rather than wiping every tab for an authorized user.
function isPermissionError(e) {
	return !!(e && (e.status === 403 || e.exc_type === "PermissionError"));
}

onMounted(async () => {
	// One probe now instead of two - the flip is atomic by construction (a
	// single await), matching the old Promise.allSettled intent ("never let
	// the strip visibly grow mid-load"): every gate ref is set together, right
	// before applyHash resolves whatever hash the viewer landed on.
	let caps = null;
	let permissionDenied = false;
	try {
		caps = await getSkillsAreaCaps(); // any desk user; throws for Guest/portal
	} catch (e) {
		if (isPermissionError(e)) {
			permissionDenied = true; // no access - the zero-tab Skills-only view is correct
		} else {
			// transient (500/network) - retry once before giving up
			await new Promise((r) => setTimeout(r, 1000));
			try {
				caps = await getSkillsAreaCaps();
			} catch (e2) {
				if (isPermissionError(e2)) permissionDenied = true;
				else {
					// still transient - keep whatever gates were already known
					// (all-false on first mount) instead of wiping the strip, and
					// leave a breadcrumb for the console.
					console.warn("get_skills_area_caps failed twice; keeping prior tab state", e2);
				}
			}
		}
	}
	if (caps || permissionDenied) {
		// apply fresh caps, or hard-reset to no-access on a real 403
		personaliseAllowed.value = !!(caps && caps.personalise);
		analysisAllowed.value = !!(caps && caps.analysis);
		reviewAllowed.value = !!(caps && caps.review);
		unansweredCount.value = (caps && caps.unanswered_count) || 0;
		capsSeed.value = caps || {};
	}
	probeSettled = true;
	applyHash();
	if (reviewAllowed.value) refreshBadge();
	detachRealtime = attachRealtime();
});

onBeforeUnmount(() => {
	if (detachRealtime) detachRealtime();
});
</script>
