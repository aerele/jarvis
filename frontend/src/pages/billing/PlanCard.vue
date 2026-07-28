<template>
	<div
		class="relative flex flex-col rounded-lg border p-4.5 transition-colors"
		:class="
			current
				? 'border-outline-gray-5 ring-1 ring-outline-gray-5 bg-surface-gray-1'
				: 'border-outline-gray-1 hover:border-outline-gray-2'
		"
	>
		<!-- One badge slot, top right. "Current plan" is the marker the page is
		     built around, so it wins over any scheduled-state note below it. -->
		<Badge
			v-if="badge"
			class="absolute right-4 top-4"
			variant="subtle"
			:theme="current ? 'green' : 'gray'"
			:label="badge"
		/>

		<div class="pr-24 text-base font-medium text-ink-gray-9">
			{{ plan.plan_name || plan.name }}
		</div>

		<div class="mb-0.5 mt-2.5 text-[22px] font-medium text-ink-gray-9">
			{{ planAmount(plan.price_inr)
			}}<span class="text-p-sm font-normal text-ink-gray-5">
				{{ planSuffix(plan.price_inr, plan.billing_cycle) }}</span
			>
		</div>
		<div class="text-xs text-ink-gray-5">{{ cycleLabel }}</div>

		<ul v-if="features.length" class="mt-3.5 grid gap-2">
			<li
				v-for="(f, k) in features"
				:key="k"
				class="flex items-start gap-2 text-p-sm text-ink-gray-7"
			>
				<FeatherIcon name="check" class="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-green-3" />
				{{ f }}
			</li>
		</ul>

		<!-- note sits above the action so the reason a button is missing or
		     disabled reads before the button itself -->
		<p v-if="note" class="mt-3.5 text-p-sm text-ink-gray-6">{{ note }}</p>

		<!-- mt-auto pins every card's action to the same baseline however many
		     features each plan lists -->
		<div v-if="actionLabel" class="mt-5 pt-0">
			<Button
				class="w-full"
				:variant="current ? 'subtle' : 'solid'"
				:label="actionLabel"
				:disabled="disabled"
				:loading="loading"
				@click="emit('action', plan)"
			/>
		</div>
	</div>
</template>

<script setup>
// One plan, rendered for comparison against its siblings. Deliberately the same
// card the onboarding plan step draws (same border, price scale, check list) so
// a returning customer recognises the surface they first bought on.
import { computed } from "vue";
import { Badge, Button, FeatherIcon } from "frappe-ui";
import { planAmount, planSuffix, planFeatures, planCycleLabel } from "@/account/format.js";

const props = defineProps({
	plan: { type: Object, required: true },
	/** Marks this as the plan the customer is on now. */
	current: { type: Boolean, default: false },
	badge: { type: String, default: "" },
	actionLabel: { type: String, default: "" },
	note: { type: String, default: "" },
	disabled: { type: Boolean, default: false },
	loading: { type: Boolean, default: false },
});
const emit = defineEmits(["action"]);

const features = computed(() => planFeatures(props.plan));
const cycleLabel = computed(() => planCycleLabel(props.plan));
</script>
