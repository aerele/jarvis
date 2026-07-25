<template>
	<SettingsPane
		title="Billing and metering"
		description="Live usage and cost across the model pool."
		:error="errorMessage"
	>
		<div class="flex flex-col gap-4">
			<div class="rounded-md border p-4">
				<h3 class="text-base font-semibold text-ink-gray-9">Status</h3>
				<div class="mt-2">
					<KvRow label="Mode" :value="config.proxy_active ? 'Proxy' : 'Direct'" />
					<KvRow label="Sync" :value="sync.last_sync_status || '—'" />
					<KvRow v-if="sync.last_sync_at" label="Last sync" :value="sync.last_sync_at" />
				</div>
			</div>

			<div class="rounded-md border p-4">
				<h3 class="text-base font-semibold text-ink-gray-9">Active pool</h3>
				<div class="mt-2">
					<KvRow label="Preset" :value="config.preset || 'Custom'" />
					<KvRow label="Routing" :value="config.routing_mode || 'failover'" />
				</div>
				<div class="mt-2 flex flex-col gap-2">
					<div
						v-for="(m, i) in config.models || []"
						:key="i"
						class="flex items-center justify-between gap-4 text-sm text-ink-gray-8"
					>
						<span>{{ m.provider }} · {{ m.model }}</span>
						<Badge
							:label="i === 0 ? 'runs every turn' : 'backup'"
							:theme="i === 0 ? 'blue' : 'gray'"
							variant="subtle"
						/>
					</div>
				</div>
			</div>

			<div class="rounded-md border p-4">
				<h3 class="flex items-center gap-2 text-base font-semibold text-ink-gray-9">
					Usage
					<span class="text-p-sm font-normal text-ink-gray-5">
						· {{ usage.period || "current period" }}
					</span>
				</h3>
				<Button
					v-if="usageError"
					class="mt-2"
					variant="subtle"
					label="Retry"
					iconLeft="refresh-cw"
					:loading="loading"
					@click="loadAll"
				/>
				<p v-else-if="!usage.applicable" class="mt-2 text-p-sm text-ink-gray-5">
					Usage is available on multi-model (proxy) setups. This tenant runs a single
					model (direct), so there is no proxy to meter.
				</p>
				<template v-else>
					<div class="mt-2 grid grid-cols-3 gap-4">
						<div class="rounded-md border p-4">
							<div class="text-2xl font-medium text-ink-gray-8">
								{{ usage.tokens_in }}
							</div>
							<div class="mt-1 text-sm text-ink-gray-6">Tokens in</div>
						</div>
						<div class="rounded-md border p-4">
							<div class="text-2xl font-medium text-ink-gray-8">
								{{ usage.tokens_out }}
							</div>
							<div class="mt-1 text-sm text-ink-gray-6">Tokens out</div>
						</div>
						<div class="rounded-md border p-4">
							<div class="text-2xl font-medium text-ink-gray-8">
								${{ usage.cost_usd }}
							</div>
							<div class="mt-1 text-sm text-ink-gray-6">Cost</div>
						</div>
					</div>
					<div class="mt-4 flex flex-col gap-4">
						<JvChart v-if="perModelSpec" :spec="perModelSpec" :dark="dark" />
						<EChart v-if="gaugeOption" :option="gaugeOption" />
					</div>
				</template>
			</div>

			<!-- "Request log & failover history" placeholder removed — no "coming
			     soon" cards in the language (design.md §5 #18); the section returns
			     when the feature ships. -->
		</div>
	</SettingsPane>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { Badge, Button } from "frappe-ui";
import JvChart from "@/charts/JvChart.vue";
import EChart from "@/charts/EChart.vue";
import { budgetGaugeOption, perModelBarSpec } from "@/charts/usageCharts.js";
import { getLlmConfig, getLlmUsage, getLlmSyncStatus } from "@/api";
import { useJarvisTheme } from "@/theme";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";

const { effectiveDark: dark } = useJarvisTheme();

const config = ref({ models: [], proxy_active: 0 });
const usage = ref({ applicable: false, per_model: [], used_vs_limit: {} });
const sync = ref({});
const usageError = ref(false);
const loading = ref(false);

// SettingsPane renders the one error surface for the pane (design.md §4.1); the
// usage card keeps only the Retry button, this supplies the message it retries.
const errorMessage = computed(() => (usageError.value ? "Usage is unavailable right now." : ""));

const perModelSpec = computed(() =>
	(usage.value.per_model || []).length ? perModelBarSpec(usage.value.per_model, "tokens") : null
);
const gaugeOption = computed(() => {
	const uv = usage.value.used_vs_limit || {};
	return budgetGaugeOption(uv.used_usd, uv.limit_usd, dark.value);
});

async function load(fetchFn, target) {
	try {
		target.value = (await fetchFn()) || target.value;
		return true;
	} catch (e) {
		return false;
	}
}
async function loadAll() {
	loading.value = true;
	usageError.value = false;
	// Order matters: results map 1:1 to the calls below. A failed usage fetch must
	// set usageError so the card shows the retry button instead of the false
	// "single model (direct)" note a transient error would otherwise render.
	const [, usageOk] = await Promise.all([
		load(getLlmConfig, config),
		load(getLlmUsage, usage),
		load(getLlmSyncStatus, sync),
	]);
	if (!usageOk) usageError.value = true;
	loading.value = false;
}
onMounted(loadAll);
</script>
