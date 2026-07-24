<template>
	<!-- The frame every settings pane shares (design.md §4.1). The dialog itself
	     has no title bar and no global footer, so the pane owns its single
	     header — which is also what keeps panes from rendering a second heading
	     under the dialog's (§5 anti-pattern 7).

	     `actions` is for the pane's own header button (a Save, a Refresh). Long
	     panes that want a pinned Save should render it in the body inside a
	     `sticky bottom-0` bar instead. -->
	<div class="flex h-full flex-col gap-6 px-10 py-8 text-ink-gray-8">
		<div class="flex items-start justify-between gap-4">
			<div class="flex flex-col gap-1">
				<h2 class="flex items-center gap-2 text-lg font-semibold text-ink-gray-8">
					{{ title }}
					<slot name="title-suffix" />
				</h2>
				<p v-if="description" class="max-w-md text-p-sm text-ink-gray-6">
					{{ description }}
				</p>
			</div>
			<div class="flex shrink-0 items-center gap-2">
				<slot name="actions" />
			</div>
		</div>

		<div class="min-h-0 flex-1 overflow-y-auto">
			<slot />
		</div>

		<!-- One error surface per pane. Panes must not invent their own red block
		     or a green success banner: failures land here, successes go through
		     toast.success (§5 anti-pattern 16). -->
		<ErrorMessage v-if="error" :message="error" />
	</div>
</template>

<script setup>
import { ErrorMessage } from "frappe-ui";

defineProps({
	title: { type: String, required: true },
	description: { type: String, default: "" },
	error: { type: String, default: "" },
});
</script>
