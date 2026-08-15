<template>
	<!-- self-start: hosts render this inside flex-col card stacks whose default
	     align-items:stretch would smear the pill across the full card width -->
	<Badge class="self-start" :label="meta.label" :theme="meta.theme" variant="subtle" size="sm" />
</template>

<script setup>
// OriginBadge - provenance tag on a Personalise question (design-language §8).
// `origin` is the `Jarvis Personalise Question` doctype's Select value, which
// stores the human-readable label ("Behavioural Learning", "From your
// organisation", …); the short aliases the design-language report used are
// mapped too, so the badge is robust to whichever the backend sends.
//
// Theme convention (design-language §6/§8): blue=behavioural, orange=org,
// gray=chat-pattern. Reviewer follow-ups deliberately carry NO attribution in
// the UI (DESIGN §6 ADOPTED: origin chip reads "From your organisation",
// asked_by is audit-only) - so "From your reviewer" renders exactly like org.
import { computed } from "vue";
import { Badge } from "frappe-ui";

const MAP = {
	// doctype Select values
	"Behavioural Learning": { label: "Behavioural learning", theme: "blue" },
	"From your organisation": { label: "From your organisation", theme: "orange" },
	"From your chat patterns": { label: "From your chat patterns", theme: "gray" },
	"From your reviewer": { label: "From your organisation", theme: "orange" },
	// short aliases (design-language report)
	behavioural: { label: "Behavioural learning", theme: "blue" },
	org: { label: "From your organisation", theme: "orange" },
	chat: { label: "From your chat patterns", theme: "gray" },
	reviewer: { label: "From your organisation", theme: "orange" },
};

const props = defineProps({
	origin: { type: String, default: "" },
});

const meta = computed(
	() => MAP[props.origin] || { label: props.origin || "Personalise", theme: "gray" }
);
</script>
