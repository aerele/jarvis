<template>
	<StepsBox
		:view="{
			mode: 'folded',
			head: { label: message.tool_name || __('Tool activity'), expandable: true },
		}"
		:open="open"
		@toggle="toggle"
	>
		<template #details
			><p v-if="error" role="status" class="text-sm">{{ error }}</p>
			<p v-else-if="loading" role="status">{{ __("Loading tool details…") }}</p>
			<template v-else-if="detail"
				><h4 class="mt-2 text-xs font-medium">{{ __("Input") }}</h4>
				<pre class="whitespace-pre-wrap break-all text-xs">{{
					display(detail.tool_args)
				}}</pre>
				<h4 class="mt-3 text-xs font-medium">{{ __("Result") }}</h4>
				<pre class="whitespace-pre-wrap break-all text-xs">{{
					display(detail.tool_result)
				}}</pre>
			</template></template
		>
	</StepsBox>
</template>
<script setup>
import { ref, watch, onBeforeUnmount } from "vue";
import StepsBox from "./StepsBox.vue";
import { getSharedToolDetail } from "@/api/chatSharing";
import { __ } from "@/lib/i18n";
const props = defineProps({ message: { type: Object, required: true } });
const open = ref(false),
	loading = ref(false),
	detail = ref(null),
	error = ref("");
let generation = 0;
const display = (v) => (typeof v === "string" ? v : JSON.stringify(v, null, 2));
watch(
	() => [props.message.name, props.message.modified],
	() => {
		++generation;
		detail.value = null;
		open.value = false;
		loading.value = false;
		error.value = "";
	}
);
async function toggle() {
	open.value = !open.value;
	if (!open.value || detail.value || loading.value) return;
	const gen = generation;
	loading.value = true;
	error.value = "";
	try {
		const value = await getSharedToolDetail(props.message.name);
		if (gen === generation) detail.value = value;
	} catch {
		if (gen === generation)
			error.value = __(
				"Couldn't load these details. Close and expand this row to try again."
			);
	} finally {
		if (gen === generation) loading.value = false;
	}
}
onBeforeUnmount(() => ++generation);
</script>
