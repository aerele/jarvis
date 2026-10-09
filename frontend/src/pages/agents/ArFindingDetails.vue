<template>
	<div class="min-w-0 space-y-5 text-sm text-ink-gray-7" data-testid="ar-finding-details">
		<section aria-label="Review result" class="space-y-2">
			<h3 class="text-base font-semibold text-ink-gray-9">{{ presentation.outcome }}</h3>
			<p class="whitespace-pre-line break-words leading-relaxed">
				{{ presentation.summary }}
			</p>
			<p v-if="presentation.opening" class="text-xs text-ink-gray-6">
				Includes an opening / prior-period invoice.
			</p>
		</section>

		<dl class="grid gap-4 rounded-lg border bg-surface-white p-4 sm:grid-cols-2">
			<div v-if="presentation.balance" class="min-w-0">
				<dt class="text-xs text-ink-gray-6">Recorded balance at cutoff</dt>
				<dd
					class="mt-1 break-words text-xl font-semibold text-ink-gray-9"
					data-testid="ar-balance"
				>
					{{ presentation.balance }}
				</dd>
			</div>
			<div class="min-w-0">
				<dt class="text-xs text-ink-gray-6">Evidence cutoff</dt>
				<dd class="mt-1 text-base font-medium text-ink-gray-9">
					{{ presentation.cutoff }}
				</dd>
			</div>
			<div v-for="fact in presentation.facts" :key="fact.label" class="min-w-0">
				<dt class="text-xs text-ink-gray-6">{{ fact.label }}</dt>
				<dd class="mt-1 break-words text-ink-gray-9">{{ fact.value }}</dd>
			</div>
		</dl>

		<section
			v-if="presentation.reminderDraft"
			aria-label="Unsent reminder draft"
			class="rounded-lg border bg-surface-white p-4"
		>
			<h3 class="font-semibold text-ink-gray-9">Unsent reminder draft</h3>
			<p class="mt-1 text-xs text-ink-gray-6">
				For human review only. This review does not send messages.
			</p>
			<p
				class="mt-4 whitespace-pre-wrap break-words leading-relaxed"
				data-testid="ar-reminder-draft"
			>
				{{ presentation.reminderDraft }}
			</p>
		</section>

		<section aria-label="Review boundaries">
			<h3 class="font-medium text-ink-gray-9">Review boundaries</h3>
			<ul class="mt-2 list-disc space-y-2 pl-5 leading-relaxed">
				<li
					v-for="(limitation, index) in presentation.limitations"
					:key="index"
					class="break-words"
				>
					{{ limitation }}
				</li>
			</ul>
		</section>

		<DocSection v-if="presentation.india" label="India Compliance context" :opened="false">
			<p class="break-words leading-relaxed">{{ presentation.india.guidance }}</p>
			<dl class="mt-4 grid gap-3 sm:grid-cols-2">
				<div v-for="field in presentation.india.fields" :key="field.label" class="min-w-0">
					<dt class="text-xs text-ink-gray-6">{{ field.label }}</dt>
					<dd class="mt-1 break-words">{{ field.value }}</dd>
				</div>
			</dl>
		</DocSection>

		<DocSection label="Recorded evidence" :opened="false">
			<div v-if="finding.match_basis" class="mb-3">
				<h4 class="font-medium text-ink-gray-9">Evidence used</h4>
				<p class="mt-1 whitespace-pre-line break-words leading-relaxed">
					{{ finding.match_basis }}
				</p>
			</div>
			<div v-if="finding.false_positive_path" class="mb-3">
				<h4 class="font-medium text-ink-gray-9">Limitations to verify</h4>
				<p class="mt-1 whitespace-pre-line break-words leading-relaxed">
					{{ finding.false_positive_path }}
				</p>
			</div>
			<h4 class="font-medium text-ink-gray-9">Original recorded explanation</h4>
			<p
				class="mt-2 whitespace-pre-wrap break-words leading-relaxed"
				data-testid="ar-recorded-explanation"
			>
				{{ finding.detail_md }}
			</p>
		</DocSection>
	</div>
</template>

<script setup>
import DocSection from "@/components/doc/DocSection.vue";

defineProps({
	presentation: { type: Object, required: true },
	finding: { type: Object, required: true },
});
</script>
