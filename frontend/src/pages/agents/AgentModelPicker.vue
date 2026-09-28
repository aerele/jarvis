<template>
	<div>
		<div v-if="loading" class="flex items-center gap-2 py-6 text-sm text-ink-gray-5">
			<JvSpinner /> Loading models…
		</div>
		<div v-else-if="catalogUnknown" class="py-6 text-sm text-ink-gray-5">
			Model list unavailable — try again shortly.
		</div>
		<div v-else-if="!models.length" class="py-6 text-sm text-ink-gray-5">
			No eligible models right now.
		</div>
		<!-- A plain list of independent toggle buttons, not an ARIA radio GROUP:
		     each option is its own real, focusable <button> (Tab between them,
		     Enter/Space to pick), which is exactly what aria-pressed describes -
		     no roving-tabindex to implement, and nothing here claims the
		     single-tab-stop contract role=radiogroup would promise but not keep. -->
		<div
			aria-label="Choose a model"
			:aria-busy="saving"
			class="max-h-80 space-y-3 overflow-y-auto"
		>
			<div v-for="g in groups" :key="g.provider">
				<div
					class="px-1 pb-1 text-xs font-semibold uppercase tracking-wide text-ink-gray-4"
				>
					{{ g.provider }}
				</div>
				<div class="space-y-1">
					<button
						v-for="m in g.models"
						:key="m.provider + '/' + m.model"
						type="button"
						:aria-pressed="isCurrent(m)"
						:disabled="saving"
						class="flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left hover:bg-surface-gray-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3 disabled:cursor-not-allowed disabled:opacity-60"
						:class="
							isCurrent(m)
								? 'border-outline-gray-3 bg-surface-gray-1'
								: 'border-transparent'
						"
						@click="onClick(m)"
					>
						<span class="min-w-0 flex-1">
							<span class="block truncate text-sm text-ink-gray-8">{{
								m.label || m.model
							}}</span>
							<span
								v-if="m.cost_note"
								class="block truncate text-xs text-ink-gray-5"
								>{{ m.cost_note }}</span
							>
						</span>
						<Badge
							v-if="m.capability_tier"
							variant="subtle"
							:theme="tierTheme(m.capability_tier)"
							:label="m.capability_tier"
						/>
						<FeatherIcon
							v-if="isCurrent(m)"
							name="check"
							class="size-4 shrink-0 text-ink-gray-8"
						/>
					</button>
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
// AgentModelPicker - the grouped-by-provider model list behind the "Change"
// action on AgentModelCard AND the install-time confirm dialog (T6): one
// shell, reused rather than forked, so both surfaces list the same options
// the same way. Deliberately NOT ModelEffortPicker (that owns its own
// trigger pill + open state + Effort/Persona flyouts for the composer) -
// this is a plain, presentational grouped list a HOST renders inside its own
// Dialog or inline disclosure; it owns no open/close state of its own.
import { computed } from "vue";
import { Badge, FeatherIcon } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import { groupByProvider, tierTheme, isCatalogUnknown } from "@/lib/agentModelTier";

const props = defineProps({
	// get_eligible_models() response, or null while unfetched.
	eligible: { type: Object, default: null },
	loading: { type: Boolean, default: false },
	// {provider, model} of the option to show checked, or null/undefined.
	current: { type: Object, default: null },
	// A pick is in flight (host is awaiting set_agent_model): disables every
	// option so a second rapid click can't fire a second request, and marks
	// the list aria-busy for assistive tech.
	saving: { type: Boolean, default: false },
});
const emit = defineEmits(["select"]);

const models = computed(() => (props.eligible && props.eligible.models) || []);
const catalogUnknown = computed(() => isCatalogUnknown(props.eligible));
const groups = computed(() => groupByProvider(models.value));

function isCurrent(m) {
	return !!(
		props.current &&
		props.current.provider === m.provider &&
		props.current.model === m.model
	);
}
function onClick(m) {
	if (props.saving) return; // belt-and-braces: :disabled already blocks the click
	emit("select", { provider: m.provider, model: m.model });
}
</script>
