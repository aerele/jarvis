<template>
	<section class="rounded-lg border p-4">
		<div class="text-base font-semibold text-ink-gray-9">Learn from custom apps</div>
		<div class="mt-0.5 text-sm text-ink-gray-6">
			Teach Jarvis the workflows your custom apps implement. This now runs as the
			<span class="font-medium text-ink-gray-8">Custom App Learning</span> agent: it reads your
			custom apps' source and writes business-first pages straight to the Org wiki, on demand, inside
			the Jarvis container — so your chat is never filled with source. Re-running updates the same
			pages in place. Restricted to admins.
		</div>
		<div class="mt-4 flex flex-wrap items-center gap-3">
			<Button
				variant="solid"
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
// Jarvis Admin. This card now points admins to that agent instead of running the
// old chat pipeline.
//
// The legacy engine (`jarvis.learning.app_analysis`), its SPA API
// (`app_learning_api` / `@/api/appLearning`), the run-history + consent
// sub-components, and the `Jarvis App Learning Run` doctype are all left
// physically present but DORMANT (rollback safety + historical run rows); the
// old scheduler hook is disabled so no new chat-pipeline runs start. Full
// removal is a tracked fast-follow.
import { Button } from "frappe-ui";
import { useRouter } from "vue-router";

const router = useRouter();
const AGENT_SLUG = "custom-app-learning";

function openAgent() {
	router.push({ name: "AgentDetail", params: { slug: AGENT_SLUG } });
}

// AnalysisTab's header Refresh reaches in through this; nothing to refetch now.
defineExpose({ reload: () => {} });
</script>
