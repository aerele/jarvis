<template>
	<!-- The sheet's questions: each with its context (renderMarkdown escapes HTML
	     first and links only http(s)), option chips, and a note unless it routes
	     the file (options only). Question text is model-derived: text only. -->
	<section class="mt-4 border-t" :aria-labelledby="baseId + '-title'">
		<h3 :id="baseId + '-title'" class="pt-3">
			<button
				type="button"
				class="flex items-center gap-1.5 text-left"
				:aria-expanded="expanded ? 'true' : 'false'"
				:aria-controls="baseId + '-body'"
				@click="emit('toggle')"
			>
				<FeatherIcon
					:name="expanded ? 'chevron-down' : 'chevron-right'"
					class="size-3.5 shrink-0 text-ink-gray-5"
					aria-hidden="true"
				/>
				<span class="text-base font-medium text-ink-gray-9">{{ __("Questions") }}</span>
				<span class="text-sm text-ink-gray-5">{{ questions.length }}</span>
			</button>
		</h3>
		<div :id="baseId + '-body'">
			<div v-if="expanded" class="flex flex-col divide-y">
				<div
					v-for="q in questions"
					:id="questionId(q)"
					:key="q.name"
					tabindex="-1"
					class="py-3 outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3"
					:aria-labelledby="questionId(q) + '-text'"
					:aria-describedby="errors[q.name] ? questionId(q) + '-error' : undefined"
				>
					<p :id="questionId(q) + '-text'" class="text-base text-ink-gray-9">
						{{ q.question || q.title }}
					</p>
					<div
						v-if="q.context_md"
						class="prose prose-sm mt-1 max-w-none text-ink-gray-7"
						v-html="renderMarkdown(q.context_md)"
					/>
					<template v-if="isOpenQuestion(q)">
						<div
							v-if="optionsOf(q).length"
							role="group"
							:aria-labelledby="questionId(q) + '-text'"
							class="mt-2 flex flex-wrap gap-2"
						>
							<Button
								v-for="o in optionsOf(q)"
								:key="o"
								:label="o"
								:variant="answers[q.name].option === o ? 'solid' : 'subtle'"
								:aria-pressed="answers[q.name].option === o ? 'true' : 'false'"
								@click="emit('pick', q, o)"
							/>
						</div>
						<div v-if="!q.routing" class="mt-2">
							<FormControl
								type="textarea"
								:label="
									optionsOf(q).length ? __('Note (optional)') : __('Your answer')
								"
								:placeholder="
									optionsOf(q).length ? __('Add a note') : __('Type your answer')
								"
								:modelValue="answers[q.name].note"
								@update:modelValue="(v) => emit('note', q, v)"
							/>
						</div>
						<p
							v-if="errors[q.name]"
							:id="questionId(q) + '-error'"
							class="mt-1 text-sm text-ink-red-4"
						>
							{{ errors[q.name] }}
						</p>
					</template>
					<p v-else class="mt-1 text-sm text-ink-gray-6">
						{{ __("Answered: {0}", [q.decision || q.status]) }}
					</p>
				</div>
			</div>
		</div>
	</section>
</template>

<script setup>
import { computed } from "vue";
import { Button, FeatherIcon, FormControl } from "frappe-ui";
import { renderMarkdown } from "@/markdown";
import { __ } from "@/lib/i18n";
import { isOpenQuestion } from "@/lib/sheet";

const props = defineProps({
	sheet: { type: String, required: true },
	questions: { type: Array, required: true },
	answers: { type: Object, required: true },
	errors: { type: Object, default: () => ({}) },
	expanded: { type: Boolean, default: true },
});
const emit = defineEmits(["toggle", "pick", "note"]);

const baseId = computed(() => `sheet-${props.sheet}-questions`);
const questionId = (q) => `sheet-${props.sheet}-q-${q.name}`;
const optionsOf = (q) => (Array.isArray(q.options) ? q.options.map(String) : []);
</script>
