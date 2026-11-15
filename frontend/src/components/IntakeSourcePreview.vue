<template>
	<!-- The source file of a Document Intake row: a photo, an embedded PDF, or a file link.
	     Shared by the Approval Board review and the File Box detail page. Wide: pinned in
	     its own column so it stays on screen while the page scrolls. Narrow: a normal
	     block (never pinned: it would cover the list rail and crowd the field when the
	     keyboard is open). -->
	<section
		v-if="file"
		aria-label="Source file"
		class="min-w-0 lg:sticky lg:top-0 lg:self-start"
		data-testid="intake-photo"
	>
		<!-- A Button inside the link would nest two controls: style the link itself -->
		<a
			v-if="!file.is_image && !file.is_pdf"
			:href="file.file_url"
			target="_blank"
			rel="noopener"
			:title="file.file_name || undefined"
			class="inline-flex min-h-7 max-w-full min-w-0 items-center gap-1.5 rounded bg-surface-gray-2 px-2 text-base text-ink-gray-8 hover:bg-surface-gray-3"
		>
			<FeatherIcon name="paperclip" class="size-4 shrink-0" aria-hidden="true" />
			<span class="min-w-0 truncate">{{ file.file_name || "Open file" }}</span>
		</a>
		<iframe
			v-else-if="file.is_pdf"
			:src="file.file_url"
			:title="file.file_name || 'Uploaded document'"
			class="h-[40vh] w-full rounded border lg:h-[70vh]"
		/>
		<a v-else :href="file.file_url" target="_blank" rel="noopener">
			<img
				:src="file.file_url"
				:alt="file.file_name || 'Uploaded document'"
				:class="tall ? 'max-h-[40vh]' : 'max-h-[30vh]'"
				class="max-w-full rounded border object-contain lg:max-h-[70vh]"
			/>
		</a>
	</section>
</template>

<script setup>
import { FeatherIcon } from "frappe-ui";

// file: {file_url, file_name, is_image, is_pdf}. `tall` gives a phone a taller photo.
defineProps({
	file: { type: Object, default: null },
	tall: { type: Boolean, default: false },
});
</script>
