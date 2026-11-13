<template>
	<!-- One Document Intake review: the photo the values were read from, what was
	     read, and one control per value still needed. Approve sends the answers as
	     JSON; the server builds the draft from the stored reading, no AI call. -->
	<div :aria-busy="busy ? 'true' : 'false'" class="flex flex-col gap-4">
		<section v-if="file" aria-label="Source file">
			<a v-if="!file.is_image" :href="file.file_url" target="_blank" rel="noopener">
				<Button
					variant="subtle"
					iconLeft="paperclip"
					:label="file.file_name || 'Open file'"
				/>
			</a>
			<a v-else :href="file.file_url" target="_blank" rel="noopener">
				<img
					:src="file.file_url"
					:alt="file.file_name || 'Uploaded document'"
					class="max-h-80 max-w-full rounded border object-contain"
				/>
			</a>
		</section>

		<section v-if="read.length || table" aria-label="What was read">
			<h3 class="text-sm font-medium text-ink-gray-8">What was read</h3>
			<dl
				v-if="read.length"
				class="mt-1 grid grid-cols-[minmax(6rem,35%)_1fr] gap-x-3 gap-y-1"
			>
				<template v-for="r in read" :key="r.label">
					<dt class="text-sm text-ink-gray-5">{{ r.label }}</dt>
					<dd class="min-w-0 break-words text-base text-ink-gray-8">{{ r.value }}</dd>
				</template>
			</dl>
			<div v-if="table && table.rows.length" class="mt-2 overflow-x-auto">
				<table class="w-full text-left text-sm">
					<caption class="sr-only">
						{{
							table.label
						}}
					</caption>
					<thead>
						<tr class="text-ink-gray-5">
							<th class="pr-3 font-normal">#</th>
							<th v-for="c in table.columns" :key="c" class="pr-3 font-normal">
								{{ c }}
							</th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="(r, i) in table.rows" :key="i" class="text-ink-gray-8">
							<td class="pr-3 text-ink-gray-5">{{ i + 1 }}</td>
							<td v-for="(cell, j) in r" :key="j" class="pr-3">{{ cell }}</td>
						</tr>
					</tbody>
				</table>
			</div>
		</section>

		<section aria-label="Values needed" class="flex flex-col gap-3">
			<h3 class="text-sm font-medium text-ink-gray-8">Values needed</h3>
			<div v-for="m in missing" :key="m.key" :data-key="m.key" class="flex flex-col gap-1">
				<template v-if="m.answerable">
					<p class="text-sm text-ink-gray-6">{{ m.reason }}</p>
					<div v-if="m.fieldtype === 'Link'" class="flex flex-col gap-1">
						<div class="flex items-center gap-1">
							<Autocomplete
								:options="optionsFor(m)"
								:loading="loadingFor(m)"
								:modelValue="linkModel(m)"
								:placeholder="`Search ${m.options}…`"
								@update:query="(q) => linkSearch(m).onQuery(q)"
								@update:modelValue="(opt) => pickLink(m, opt)"
							/>
							<Button
								v-if="answers[m.key]"
								variant="ghost"
								icon="x"
								:label="`Clear ${m.label}`"
								@click="pickLink(m, null)"
							/>
						</div>
						<div
							v-if="m.candidates && m.candidates.length"
							class="flex flex-wrap items-center gap-1"
						>
							<span class="text-xs text-ink-gray-5">Closest</span>
							<Button
								v-for="c in m.candidates"
								:key="c"
								size="sm"
								variant="subtle"
								:label="c"
								@click="pickLink(m, { value: c, label: c })"
							/>
						</div>
					</div>
					<FormControl
						v-else
						:label="m.label"
						:type="controlType(m)"
						:options="selectOptions(m)"
						:modelValue="answers[m.key]"
						@update:modelValue="(v) => setAnswer(m, v)"
					/>
				</template>
				<p v-else class="text-sm text-ink-gray-6">
					{{ m.reason }}. Reject this review and add the document by hand.
				</p>
			</div>
		</section>

		<div v-if="error" role="alert" class="text-sm text-ink-red-5">{{ error }}</div>

		<div class="flex flex-wrap items-center gap-2">
			<Button
				variant="solid"
				theme="green"
				label="Approve"
				:loading="busy === 'approve'"
				:disabled="busy !== null || !ready"
				@click="send(1)"
			/>
			<Button
				variant="subtle"
				theme="red"
				label="Reject"
				:loading="busy === 'reject'"
				:disabled="busy !== null"
				@click="send(0)"
			/>
		</div>
	</div>
</template>

<script setup>
// The review form for a Document Intake request on the Approval Board. `approval` is
// the get_approval payload (`intake` holds the missing fields, what was read and the
// source file). `onDecided` is a callback, not an emit: switching rows mid-decision
// unmounts this form, and Vue drops an unmounted instance's emits.
import { computed, onMounted, reactive, ref } from "vue";
import { Autocomplete, Button, FormControl, toast } from "frappe-ui";
import { decideApproval, searchLink as searchLinkApi } from "@/api";
import { useLinkSearch } from "@/composables/useLinkSearch";
import { errHtml, errMessage } from "@/lib/errors";

const props = defineProps({
	approval: { type: Object, required: true },
	onDecided: { type: Function, default: null },
});

const intake = computed(() => props.approval.intake || {});
const missing = computed(() => intake.value.missing || []);
const read = computed(() => intake.value.read || []);
const table = computed(() => intake.value.table || null);
const file = computed(() => intake.value.file || null);

const NUMBER_TYPES = ["Int", "Float", "Currency", "Percent"];
const answers = reactive({});
const busy = ref(null); // "approve" | "reject" | null
const error = ref("");

const answerable = computed(() => missing.value.filter((m) => m.answerable));
const blank = (v) => v === undefined || v === null || String(v).trim() === "";
// A value the form cannot ask for ("no rows read") blocks Approve: only Reject is left.
const ready = computed(
	() =>
		missing.value.length > 0 &&
		missing.value.every((m) => m.answerable && !blank(answers[m.key]))
);

function controlType(m) {
	if (m.fieldtype === "Select") return "select";
	if (NUMBER_TYPES.includes(m.fieldtype)) return "number";
	if (m.fieldtype === "Date") return "date";
	if (m.fieldtype === "Datetime") return "datetime-local";
	if (["Small Text", "Text", "Long Text"].includes(m.fieldtype)) return "textarea";
	return "text";
}
function selectOptions(m) {
	if (m.fieldtype !== "Select") return undefined;
	return (m.options || "")
		.split("\n")
		.filter(Boolean)
		.map((o) => ({ label: o, value: o }));
}
// Numbers go as numbers: the server parses text by the site's number format, a
// number needs no parsing.
function setAnswer(m, v) {
	answers[m.key] =
		NUMBER_TYPES.includes(m.fieldtype) && !blank(v) && !isNaN(Number(v)) ? Number(v) : v;
}

// Link pickers: one search per field, as the dashboard filter bar does.
const searches = new Map();
const labels = reactive({});
function linkSearch(m) {
	if (!searches.has(m.key)) {
		searches.set(
			m.key,
			useLinkSearch((q) => searchLinkApi(m.options, q, 10), {
				mapper: (rows) =>
					(rows || []).map((r) => ({
						value: String(r.value),
						label: r.label || r.value,
						description:
							r.label && r.label !== r.value ? String(r.value) : r.description || "",
					})),
			})
		);
	}
	return searches.get(m.key);
}
// Each picker lists its first page on open, as the dashboard filter bar does.
onMounted(() =>
	missing.value
		.filter((m) => m.answerable && m.fieldtype === "Link")
		.forEach((m) => linkSearch(m).prime())
);
const optionsFor = (m) => linkSearch(m).options.value;
const loadingFor = (m) => linkSearch(m).loading.value;
const linkModel = (m) =>
	answers[m.key] ? { value: answers[m.key], label: labels[m.key] || answers[m.key] } : null;
function pickLink(m, opt) {
	answers[m.key] = opt && opt.value != null ? String(opt.value) : "";
	labels[m.key] = (opt && opt.label) || "";
}

async function send(approve) {
	if (busy.value) return;
	// Captured now: the board may select another row while the call is out.
	const id = props.approval.name;
	busy.value = approve ? "approve" : "reject";
	error.value = "";
	try {
		const body = approve
			? Object.fromEntries(
					answerable.value
						.filter((m) => !blank(answers[m.key]))
						.map((m) => [m.key, answers[m.key]])
			  )
			: {};
		const res = (await decideApproval(id, JSON.stringify(body), approve)) || {};
		if (res.status === "Answered")
			toast.warning("The draft could not be created. The upload shows why");
		else
			toast.success(
				approve ? "Approved. The draft was created" : "Rejected. No draft was made"
			);
		if (props.onDecided) props.onDecided(res, id);
	} catch (e) {
		// The server names what is still needed and the request stays Pending.
		error.value = errMessage(e, "The review could not be saved.");
		toast.error(errHtml(e));
	} finally {
		busy.value = null;
	}
}
</script>
