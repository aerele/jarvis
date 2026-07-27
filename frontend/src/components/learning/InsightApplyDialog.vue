<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: 'Apply insight to a skill', size: 'lg' }"
		@update:modelValue="(v) => emit('update:modelValue', v)"
	>
		<template #body-content>
			<!-- the insight being folded into a skill -->
			<p v-if="pattern.pattern_statement" class="text-sm text-ink-gray-6">
				{{ pattern.pattern_statement }}
			</p>

			<!-- drafting -->
			<div
				v-if="phase === 'loading'"
				class="flex flex-col items-center gap-2 py-10 text-center"
			>
				<LoadingIndicator class="size-5 text-ink-gray-5" />
				<span class="max-w-sm text-sm text-ink-gray-6">
					Drafting - {{ agentName }} is matching this insight against your custom skills
					and writing the update. This takes a few seconds.
				</span>
			</div>

			<!-- verdict: not worth applying -->
			<div
				v-else-if="phase === 'none'"
				class="mt-3 flex items-start gap-2 rounded-lg border border-outline-gray-2 bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-7"
			>
				<FeatherIcon name="info" class="mt-0.5 size-4 shrink-0" />
				<span>{{
					draft.reason || "This insight is not worth folding into a skill."
				}}</span>
			</div>

			<!-- verdict: update an existing skill -->
			<template v-else-if="phase === 'update'">
				<div class="mt-3 flex flex-wrap items-center gap-1.5 text-sm">
					<span class="text-ink-gray-5">Target skill:</span>
					<Badge variant="subtle" theme="blue" :label="draft.skill_name" />
				</div>
				<p v-if="draft.reason" class="mt-2 text-sm text-ink-gray-6">{{ draft.reason }}</p>
				<div class="mt-3">
					<div class="mb-1 text-sm text-ink-gray-5">Current instructions</div>
					<div
						class="max-h-44 overflow-y-auto rounded border bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-8"
					>
						<div
							v-for="(l, i) in currentLines"
							:key="i"
							class="whitespace-pre-wrap"
							:class="l.changed ? 'rounded-sm bg-surface-red-1' : ''"
						>
							{{ l.line || " " }}
						</div>
					</div>
				</div>
				<div class="mt-3">
					<div class="mb-1 text-sm text-ink-gray-5">Proposed instructions</div>
					<div
						class="max-h-44 overflow-y-auto rounded border bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-8"
					>
						<div
							v-for="(l, i) in proposedLines"
							:key="i"
							class="whitespace-pre-wrap"
							:class="l.changed ? 'rounded-sm bg-surface-green-1' : ''"
						>
							{{ l.line || " " }}
						</div>
					</div>
				</div>
			</template>

			<!-- verdict: create a new skill -->
			<template v-else-if="phase === 'create'">
				<p v-if="draft.reason" class="mt-3 text-sm text-ink-gray-6">{{ draft.reason }}</p>
				<div class="mt-3 flex flex-wrap items-center gap-1.5 text-sm">
					<span class="text-ink-gray-5">New skill:</span>
					<Badge variant="subtle" theme="blue" :label="newSkill.skill_name" />
				</div>
				<p v-if="newSkill.description" class="mt-2 text-sm text-ink-gray-7">
					{{ newSkill.description }}
				</p>
				<div class="mt-3">
					<div class="mb-1 text-sm text-ink-gray-5">Instructions</div>
					<pre
						class="max-h-56 overflow-y-auto whitespace-pre-wrap rounded border bg-surface-gray-1 px-3 py-2 text-sm text-ink-gray-8"
						>{{ newSkill.instructions }}</pre
					>
				</div>
			</template>

			<p
				v-if="phase === 'update' || phase === 'create'"
				class="mt-3 text-sm text-ink-gray-5"
			>
				Confirming saves the skill only - it reaches your assistant with the next skills
				push.
			</p>
		</template>

		<template #actions>
			<div class="flex items-center gap-2">
				<Button
					v-if="phase === 'none'"
					variant="solid"
					label="Acknowledge instead"
					:loading="acknowledging"
					@click="acknowledgeInstead"
				/>
				<Button
					v-else-if="phase === 'update'"
					variant="solid"
					theme="green"
					label="Update skill"
					:loading="applying"
					@click="confirmApply"
				/>
				<Button
					v-else-if="phase === 'create'"
					variant="solid"
					theme="green"
					label="Create skill"
					:loading="applying"
					@click="confirmApply"
				/>
				<Button
					label="Cancel"
					:disabled="applying || acknowledging"
					@click="emit('update:modelValue', false)"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// InsightApplyDialog - the wiki-v2 D5 "Apply to skill…" flow for B/C
// (insight-only) learned patterns. Opening drafts server-side (ONE LLM call
// matching the insight against org custom skills) and returns a verdict:
// update an existing skill (before/after comparison with a cheap line-diff
// highlight), create a new one (preview), or not worth applying (reason +
// the ordinary Acknowledge as a shortcut). Confirm calls
// apply_insight_skill_update, which writes the skill and marks the pattern
// acknowledged with an applied-to-skill note; the change then rides the
// normal Skills-tab apply (no auto-push). Self-contained like ShareDialog:
// owns its busy refs; the parent refreshes on @applied.
import { reactive, ref, computed, watch } from "vue";
import { Badge, Button, Dialog, FeatherIcon, LoadingIndicator, toast } from "frappe-ui";
import {
	draftInsightSkillUpdate,
	applyInsightSkillUpdate,
	acknowledgeLearnedPattern,
} from "@/api/learning";
import { agentName } from "@/branding";

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	pattern: { type: Object, default: () => ({}) }, // board row: {name, pattern_statement, ...}
});

const emit = defineEmits(["update:modelValue", "applied"]);

const phase = ref("loading"); // loading | none | update | create
const applying = ref(false);
const acknowledging = ref(false);
const draft = reactive({
	reason: "",
	action: "",
	skill_name: "",
	before_instructions: "",
	updated_instructions: "",
	new_skill: null,
});
// monotonic open counter: a draft resolving after close (or a re-open) is dropped
let loadSeq = 0;

watch(
	() => props.modelValue,
	(open) => {
		if (open) load();
	}
);

async function load() {
	const seq = ++loadSeq;
	phase.value = "loading";
	applying.value = false;
	acknowledging.value = false;
	Object.assign(draft, {
		reason: "",
		action: "",
		skill_name: "",
		before_instructions: "",
		updated_instructions: "",
		new_skill: null,
	});
	try {
		const r = await draftInsightSkillUpdate(props.pattern.name);
		if (seq !== loadSeq || !props.modelValue) return;
		if (!r || r.ok === false) {
			toast.error((r && r.reason) || "Could not draft a skill update.");
			emit("update:modelValue", false);
			return;
		}
		Object.assign(draft, {
			reason: r.reason || "",
			action: r.action || "none",
			skill_name: r.skill_name || "",
			before_instructions: r.before_instructions || "",
			updated_instructions: r.updated_instructions || "",
			new_skill: r.new_skill || null,
		});
		phase.value =
			!r.worth_applying || r.action === "none"
				? "none"
				: r.action === "create"
				? "create"
				: "update";
	} catch (e) {
		if (seq !== loadSeq) return;
		toast.error(errMsg(e));
		emit("update:modelValue", false);
	}
}

const newSkill = computed(() => draft.new_skill || {});

// Cheap line-level diff: a line is highlighted when its trimmed text does not
// appear anywhere on the other side. Not a real diff (moved/duplicated lines
// read as changes) but enough to draw the eye to what the LLM touched.
// Single-paragraph instructions (the common case) degrade to "everything
// changed" under a line diff, so fall back to sentence segments there.
function segments(text, splitSentences) {
	const s = String(text || "");
	if (!splitSentences) return s.split("\n");
	return s.split(/(?<=[.!?])\s+/);
}
function diffLines(text, otherText) {
	const bothMultiline =
		String(text || "")
			.trim()
			.includes("\n") &&
		String(otherText || "")
			.trim()
			.includes("\n");
	const bySentence = !bothMultiline;
	const other = new Set(
		segments(otherText, bySentence)
			.map((l) => l.trim())
			.filter(Boolean)
	);
	return segments(text, bySentence).map((line) => ({
		line,
		changed: !!line.trim() && !other.has(line.trim()),
	}));
}
const currentLines = computed(() =>
	diffLines(draft.before_instructions, draft.updated_instructions)
);
const proposedLines = computed(() =>
	diffLines(draft.updated_instructions, draft.before_instructions)
);

async function confirmApply() {
	if (applying.value) return;
	applying.value = true;
	try {
		const payload =
			phase.value === "create"
				? { action: "create", new_skill: draft.new_skill }
				: {
						action: "update",
						skill_name: draft.skill_name,
						updated_instructions: draft.updated_instructions,
				  };
		const r = await applyInsightSkillUpdate(props.pattern.name, payload);
		if (r && r.ok === false) {
			toast.error(r.reason || "Could not apply the insight.");
			return; // dialog stays open
		}
		const skill =
			(r && r.skill_name) || draft.skill_name || newSkill.value.skill_name || "skill";
		toast.success(
			phase.value === "create"
				? `Skill “${skill}” created. It reaches your assistant with the next skills push.`
				: `Skill “${skill}” updated. The change reaches your assistant with the next skills push.`
		);
		emit("applied", { skill_name: skill });
		emit("update:modelValue", false);
	} catch (e) {
		toast.error(errMsg(e)); // dialog stays open for retry / cancel
	} finally {
		applying.value = false;
	}
}

// The verdict said "not worth applying": offer the ordinary B/C disposition
// without a round-trip back to the board.
async function acknowledgeInstead() {
	if (acknowledging.value) return;
	acknowledging.value = true;
	try {
		await acknowledgeLearnedPattern(props.pattern.name);
		toast.success("Acknowledged");
		emit("applied", { acknowledged: true });
		emit("update:modelValue", false);
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		acknowledging.value = false;
	}
}
</script>
