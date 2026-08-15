<template>
	<section class="rounded-lg border p-4">
		<div class="text-base font-semibold text-ink-gray-9">Learn from custom apps</div>
		<div class="mt-0.5 text-sm text-ink-gray-6">
			Teach Jarvis the workflows your custom apps implement. This now runs as the
			<span class="font-medium text-ink-gray-8">Custom App Learning</span> agent: it reads
			the apps <span class="font-medium text-ink-gray-8">you select</span> and writes
			business-first pages straight to the Org wiki, on demand, inside the Jarvis container —
			so your chat is never filled with source. Re-running updates the same pages in place.
			Restricted to admins.
		</div>
		<div
			class="mt-3 flex items-start gap-2 rounded-lg border border-outline-amber-2 bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
		>
			<FeatherIcon name="alert-triangle" class="size-4 shrink-0" />
			<span>
				A run reads only the apps you pick, and their source code is sent to the AI model
				provider configured for this site. Source text — including comments and docstrings
				— is treated as data, not instructions, but a hostile app could still attempt to
				steer the agent, so only select apps whose code you trust.
			</span>
		</div>
		<div class="mt-4 flex flex-wrap items-center gap-3">
			<Button
				v-if="installation"
				variant="solid"
				icon-left="play"
				label="Choose apps and run"
				:loading="running"
				@click="pickerOpen = true"
			/>
			<Button
				:variant="installation ? 'subtle' : 'solid'"
				icon-left="book-open"
				label="Open the Custom App Learning agent"
				@click="openAgent"
			/>
			<router-link
				:to="{ name: 'AgentsList' }"
				class="text-sm text-ink-blue-link hover:underline"
			>
				Browse all agents
			</router-link>
		</div>
		<div v-if="!loading && !installation" class="mt-2 text-sm text-ink-gray-5">
			Install the agent from its page to run it from here.
		</div>

		<AppSourceConsentDialog
			v-model="pickerOpen"
			:busy="running"
			confirm-label="Start learning"
			@confirm="run"
		/>
	</section>
</template>

<script setup>
// AppLearningCard - the "Learn from custom apps" surface inside AnalysisTab.
//
// The custom-app learning feature moved from the chat-batch pipeline to the
// Custom App Learning *scribe* delegate agent (marketplace slug
// `custom-app-learning`): a delegate that reads custom-app source via the
// self-gated source-read tools and writes the wiki via the audited-not-gated
// record_app_wiki, on demand, in our container, restricted to System Manager /
// Jarvis Admin.
//
// CX5-2: a run is authorised app-by-app. This card no longer just links to the
// agent page - it opens the shared consent dialog, and the admin's selection is
// what run_agent_now stamps on the run as its source-read authorization. Without
// a selection the server refuses to launch.
//
// The legacy BACKEND (`jarvis.learning.app_analysis`, its SPA API
// `app_learning_api` / `@/api/appLearning`, and the `Jarvis App Learning Run`
// doctype) is left physically present but DORMANT for rollback safety +
// historical run rows: the scheduler hook is disabled and schedule_app_learning
// now refuses, so no new chat-pipeline run can start. Its `list_custom_apps`
// endpoint is still the learnable-app roster the consent dialog reads.
import { onMounted, ref } from "vue";
import { Button, FeatherIcon, toast } from "frappe-ui";
import { useRouter } from "vue-router";
import * as api from "@/api";
import { errHtml } from "@/lib/errors";
import AppSourceConsentDialog from "@/components/learning/AppSourceConsentDialog.vue";

const router = useRouter();
const AGENT_SLUG = "custom-app-learning";

const installation = ref(null);
const loading = ref(true);
const running = ref(false);
const pickerOpen = ref(false);

function openAgent() {
	router.push({ name: "AgentDetail", params: { slug: AGENT_SLUG } });
}

async function load() {
	loading.value = true;
	try {
		const rows = (await api.getAgentInstallations()) || [];
		installation.value = rows.find((r) => r.agent === AGENT_SLUG && r.enabled) || null;
	} catch (e) {
		installation.value = null;
	} finally {
		loading.value = false;
	}
}

async function run(apps) {
	if (!installation.value || running.value) return;
	running.value = true;
	try {
		await api.runAgentNow(installation.value.name, { source_apps: apps });
		pickerOpen.value = false;
		toast.success(`Learning ${apps.length} app(s) — follow it on the agent's Runs tab`);
	} catch (e) {
		toast.error(errHtml(e));
	} finally {
		running.value = false;
	}
}

onMounted(load);

// AnalysisTab's header Refresh reaches in through this.
defineExpose({ reload: load });
</script>
