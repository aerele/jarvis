<template>
	<SettingsPane title="Keyboard shortcuts" description="Keyboard shortcuts available in chat.">
		<h3 class="text-base font-semibold text-ink-gray-9">Composer</h3>
		<div class="mt-2">
			<KvRow label="Recall previous / next prompt">
				<span class="flex items-center gap-1.5">
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						<FeatherIcon name="arrow-up" class="size-3" />
					</kbd>
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						<FeatherIcon name="arrow-down" class="size-3" />
					</kbd>
				</span>
			</KvRow>
			<KvRow label="Send message">
				<kbd
					class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
				>
					Enter
				</kbd>
			</KvRow>
			<KvRow label="New line">
				<span class="flex items-center gap-1">
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						Shift
					</kbd>
					<span class="text-xs text-ink-gray-5">+</span>
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						Enter
					</kbd>
				</span>
			</KvRow>
			<KvRow label="Mention a doctype / record">
				<kbd class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>@</kbd
				>
			</KvRow>
		</div>

		<hr class="my-8" />

		<h3 class="text-base font-semibold text-ink-gray-9">Chat</h3>
		<div class="mt-2">
			<KvRow label="New chat">
				<span class="flex items-center gap-1">
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						Ctrl
					</kbd>
					<span class="text-xs text-ink-gray-5">+</span>
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						Shift
					</kbd>
					<span class="text-xs text-ink-gray-5">+</span>
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						O
					</kbd>
				</span>
			</KvRow>
			<KvRow label="Toggle sidebar">
				<span class="flex items-center gap-1">
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						Ctrl
					</kbd>
					<span class="text-xs text-ink-gray-5">+</span>
					<kbd
						class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
					>
						B
					</kbd>
				</span>
			</KvRow>
			<KvRow label="Close panel / cancel">
				<kbd
					class="rounded border bg-surface-gray-2 px-1.5 py-0.5 text-xs text-ink-gray-7"
				>
					Esc
				</kbd>
			</KvRow>
		</div>

		<!-- inline-flex + align-middle: a bare <kbd> is inline and the icon's svg
		     does not sit on the text baseline inside one, which breaks the
		     sentence onto a second line and strands the chip. The chips in the
		     rows above are already inside flex containers, so only this one in
		     running prose needs it. -->
		<p class="mt-4 text-p-sm text-ink-gray-5">
			Tip:
			<kbd
				class="inline-flex items-center rounded border bg-surface-gray-2 px-1.5 py-0.5 align-middle text-xs text-ink-gray-7"
			>
				<FeatherIcon name="arrow-up" class="size-3" />
			</kbd>
			at the start of an empty composer walks back through your earlier prompts in this chat.
		</p>
	</SettingsPane>
</template>

<script setup>
import { FeatherIcon } from "frappe-ui";
import SettingsPane from "@/components/settings/SettingsPane.vue";
import KvRow from "@/components/settings/KvRow.vue";
</script>
