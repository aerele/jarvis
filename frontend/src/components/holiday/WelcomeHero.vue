<template>
	<div class="jv-hero">
		<div class="jv-hero-head">
			<HolidayMark :size="38" :radius="11" :mood="mood" welcome style="flex: none" />
			<h1 class="jv-hero-h1" data-testid="welcome-greeting">{{ text }}</h1>
		</div>
		<p v-if="leadup" class="jv-hero-leadup" data-testid="welcome-leadup">{{ leadup }}</p>
	</div>
</template>

<script setup>
// The empty-chat greeting: the brand mark beside the heading, plus on a lead-up day the
// advance wish above the orienting line ChatView renders next. On the day itself, or a
// birthday, the heading is the day heading from first paint, and the lead-up line is
// gone. The scene, the stage layout and the festive card are not part of this.
//
// The mark sits beside the h1, not inside it: on a holiday the mark is a button, and a
// button inside a heading would be read out as part of it. The h1 is only the greeting.
import { computed } from "vue";
import HolidayMark from "@/components/holiday/HolidayMark.vue";
import { holidayTouch } from "@/holidayTouch";
import { headingFor, leadupLineFor } from "@jsshared/holiday/copy.mjs";

const props = defineProps({
	/** The ordinary greeting ("Good morning"). */
	greeting: { type: String, required: true },
	firstName: { type: String, default: "" },
	/** "upgrading" during the maintenance hold, else "star". */
	mood: { type: String, default: "star" },
});

const answer = computed(() => holidayTouch.answer.value);
const name = computed(() => (props.firstName || "").trim());
const dayHeading = computed(() =>
	answer.value.kind === "day" || answer.value.kind === "birthday"
		? headingFor(answer.value.occasion, name.value)
		: null
);
// No name means no dangling comma ("Good morning", not "Good morning, ").
const text = computed(
	() => dayHeading.value || (name.value ? `${props.greeting}, ${name.value}` : props.greeting)
);
const leadup = computed(() =>
	answer.value.kind === "leadup" ? leadupLineFor(answer.value.occasion) : null
);
</script>

<style scoped>
.jv-hero-head {
	display: flex;
	align-items: center;
	justify-content: center;
	gap: 16px;
}
.jv-hero-h1 {
	min-width: 0;
	font-size: 40px;
	font-weight: 640;
	letter-spacing: -0.03em;
	margin: 0;
	overflow-wrap: anywhere;
}
/* The advance wish reads like the orienting line under it (ChatView's .jv-welcome-sub):
   same size, colour and spacing, so the two stack as one quiet block. */
.jv-hero-leadup {
	font-size: 15px;
	color: var(--text-2);
	line-height: 1.5;
	margin: 12px 0 0;
}
/* Same phone step-down the heading always had (ChatView's old .jv-welcome-h1 rule):
   a long name still fits a phone without wrapping oddly. */
@media (max-width: 640px) {
	.jv-hero-h1 {
		font-size: 28px;
	}
	.jv-hero-leadup {
		font-size: 14px;
		margin-top: 10px;
	}
}
</style>
