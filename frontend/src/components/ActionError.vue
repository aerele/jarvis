<!--
  Rich, actionable failure for a chat ACTION card (confirm a gated tool, apply a
  model-drafted create/update). Replaces the old flat red `.jv-draft-error` box:
  a clear headline, the message, a visible "what you can do" hint, and the real
  reason tucked behind a "Show details" expander - mirroring the turn-level error
  card. Fed by the enriched `{ok:false, error:{code, message, detail, hint}}`
  envelope (see jarvis/api.py _translate_write_error); tolerates a bare string or
  a partial object so the thrown-error (network/500) fallback still renders.
-->
<template>
	<div role="alert">
		<Banner type="error" :title="headline" :message="err.message">
			<div v-if="err.hint" class="jv-ae-hint">{{ err.hint }}</div>
			<details v-if="err.detail" class="jv-ae-details">
				<summary>Show details</summary>
				<div class="jv-ae-detail">{{ err.detail }}</div>
			</details>
		</Banner>
	</div>
</template>

<script setup>
import { computed } from "vue";
import Banner from "./Banner.vue";

const props = defineProps({
	// The envelope's `error` object, or a plain message string (fallback path).
	error: { type: [Object, String], default: () => ({}) },
});

// Plain-language headline keyed by the wire `code`. This is the ACTION-failure
// axis - distinct from ChatView's run/transport taxonomy (lib/errors.js
// turnErrorInfo, formerly ChatView's own ERROR_HEADLINES before #702), kept
// separate on purpose.
const HEADLINES = {
	PermissionDeniedError: "You don't have permission to do this",
	InvalidArgumentError: "Some values need attention",
	ValidationError: "Some values need attention",
	NoDataError: "Nothing to do here",
	ResultTooLargeError: "That result is too large to return",
	ToolNotFoundError: "That action isn't available",
};

const err = computed(() => {
	const e = props.error;
	if (typeof e === "string") return { message: e };
	return e || {};
});

const headline = computed(() => HEADLINES[err.value.code] || "Something went wrong");
</script>

<style scoped>
.jv-ae-hint {
	font-size: 12.5px;
	color: var(--text-2);
	line-height: 1.5;
	margin-top: 6px;
}
.jv-ae-details {
	margin-top: 6px;
}
.jv-ae-details summary {
	font-size: 11.5px;
	color: var(--text-3);
	cursor: pointer;
}
.jv-ae-detail {
	font-size: 12px;
	color: var(--text-2);
	line-height: 1.5;
	margin-top: 4px;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
}
</style>
