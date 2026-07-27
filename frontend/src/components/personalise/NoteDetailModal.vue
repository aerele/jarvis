<template>
	<Dialog v-model="show" :options="{ size: 'xl' }">
		<template #body-title>
			<div v-if="note" class="flex flex-wrap items-center gap-2">
				<Badge :theme="kindBadge.theme" :variant="kindBadge.variant" :label="note.kind" />
				<span class="text-base font-medium text-ink-gray-8">{{ originText }}</span>
				<span class="text-sm text-ink-gray-5">· {{ exactDate(note.creation) }}</span>
			</div>
			<span v-else class="text-lg font-semibold text-ink-gray-9">Note</span>
		</template>

		<template #body-content>
			<div v-if="loading" class="py-8 text-center">
				<LoadingIndicator class="size-5 text-ink-gray-5" />
			</div>
			<template v-else-if="note">
				<!-- full transcript / caption, the user's own words -->
				<div class="whitespace-pre-wrap text-sm text-ink-gray-8">
					{{ note.transcript || "(No caption)" }}
				</div>

				<!-- extracted link chip: safe external link, opens in a new tab -->
				<div v-if="note.kind === 'Link' && note.url" class="mt-3">
					<a
						:href="note.url"
						target="_blank"
						rel="noopener"
						class="inline-flex max-w-full items-center gap-1.5 truncate rounded-lg border px-3 py-1.5 text-sm text-ink-blue-3 hover:underline"
					>
						<FeatherIcon name="link" class="size-3.5 shrink-0" />
						<span class="truncate">{{ note.url }}</span>
					</a>
				</div>

				<!-- attachment chip: file name linking to the private file -->
				<div v-if="note.kind === 'Attachment' && note.attachment" class="mt-3">
					<a
						:href="note.attachment"
						target="_blank"
						rel="noopener"
						class="inline-flex max-w-full items-center gap-1.5 truncate rounded-lg border px-3 py-1.5 text-sm text-ink-gray-8 hover:underline"
					>
						<FeatherIcon name="paperclip" class="size-3.5 shrink-0" />
						<span class="truncate">{{ attachmentName }}</span>
					</a>
				</div>

				<!-- two-stage receipt, second stage: what Jarvis did with it -->
				<div v-if="note.status === 'Processed'" class="mt-4 border-t pt-3">
					<div class="text-xs-medium text-ink-gray-5">Saved to your wiki</div>
					<template v-if="note.wiki_pages && note.wiki_pages.length">
						<!-- plain titles: there's no per-page deep link, so these are NOT
						     links (a link here would mislead by dumping the user on the
						     Wiki tab root, not the named page) - see §17 -->
						<ul class="mt-1.5 flex flex-col gap-1">
							<li
								v-for="p in note.wiki_pages"
								:key="p.slug"
								class="text-sm text-ink-gray-8"
							>
								{{ p.title || p.slug }}
							</li>
						</ul>
						<Button
							class="mt-2"
							variant="subtle"
							size="sm"
							label="Open the Wiki tab"
							iconLeft="arrow-up-right"
							@click="openWiki"
						/>
					</template>
					<p v-else class="mt-1.5 text-sm text-ink-gray-6">
						Processed - nothing new was added to your wiki.
					</p>
				</div>
			</template>
		</template>

		<template #actions>
			<div v-if="note" class="flex items-center gap-2">
				<Button
					variant="subtle"
					theme="red"
					label="Delete"
					iconLeft="trash-2"
					:loading="deleting"
					@click="confirmDelete"
				/>
				<Button
					v-if="note.question"
					variant="solid"
					label="Re-answer"
					iconLeft="corner-up-right"
					@click="onReanswer"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
// NoteDetailModal - Notes-view detail popup (Wave F3, DESIGN.md sections 5/
// 6b). Fetches jarvis.chat.personalise_api.get_note on open, keyed on `name`
// (WikiPageDialog.vue's own slug-fetch idiom, same shape). Header = kind
// badge + origin ("Answers: <question text>" when the note answered a
// question, else "Free note") + created exact date. Body = full transcript,
// an extracted link/attachment chip, and - once the note has been processed
// - the "Saved to your wiki" receipt (DESIGN.md §5c/§6's two-stage receipt:
// instant "Saved" toast on capture, this block is the async second stage).
//
// EMIT CONTRACT (binding for the F2 integrator wiring PersonaliseTab.vue):
//   @reanswer(questionName: string) - fired ONLY when `note.question` is set
//   (the Re-answer button is hidden otherwise). The parent (NotesView, a
//   pure pass-through) re-emits this unchanged up to PersonaliseTab, which
//   should switch its nested TabBar to the Questions sub-tab and select the
//   question named `questionName` (DESIGN.md §5's two-pane Questions view -
//   RIGHT pane shows that question's context panel, composer's "Answering:"
//   chip reflects it). This modal never navigates itself - it only closes
//   and hands back the question name; wiring the actual tab-switch/selection
//   is PersonaliseTab's job, by design (F3 does not own that file).
//
//   @changed - fired after a successful delete, so the owning NotesView (or
//   any other host) knows to refetch its list. No payload.
import { ref, computed, watch } from "vue";
import { useRouter } from "vue-router";
import {
	Badge,
	Button,
	Dialog,
	FeatherIcon,
	LoadingIndicator,
	toast,
	confirmDialog,
} from "frappe-ui";
import { exactDate } from "@/utils/datetime";
import { getNote, deleteNote } from "@/api/personalise";
import { agentName } from "@/branding";

const router = useRouter();

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	name: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "changed", "reanswer"]);

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// Same theme map as NotesView's row badges - kept as a local copy (not a
// shared import) because NotesView's is inline too; both are small enough
// that a shared constants module would be more indirection than it's worth
// for two four-entry maps.
const KIND_BADGE = {
	Text: { theme: "gray", variant: "subtle" },
	Voice: { theme: "blue", variant: "subtle" },
	Attachment: { theme: "orange", variant: "subtle" },
	Link: { theme: "blue", variant: "outline" },
};

const show = computed({
	get: () => props.modelValue,
	set: (v) => emit("update:modelValue", v),
});

const note = ref(null);
const loading = ref(false);
const deleting = ref(false);

const kindBadge = computed(
	() => KIND_BADGE[note.value && note.value.kind] || { theme: "gray", variant: "subtle" }
);
const originText = computed(() => {
	if (!note.value) return "";
	return note.value.question_text ? `Answers: "${note.value.question_text}"` : "Free note";
});
// The `attachment` field is a raw Attach file_url (e.g. "/private/files/
// report.pdf?abc123") - there's no separate file_name in get_note's payload,
// so derive a readable name from the URL the same way DocMetaPanel falls
// back to `f.file_url` when `f.file_name` is absent.
const attachmentName = computed(() => {
	const url = note.value && note.value.attachment;
	if (!url) return "";
	try {
		return decodeURIComponent(url.split("/").pop().split("?")[0]) || url;
	} catch (e) {
		return url;
	}
});

watch(
	() => props.modelValue,
	(open) => {
		if (open) load();
	}
);

async function load() {
	note.value = null;
	loading.value = true;
	try {
		note.value = await getNote(props.name);
	} catch (e) {
		show.value = false;
		toast.error(errMsg(e));
	} finally {
		loading.value = false;
	}
}

function confirmDelete() {
	confirmDialog({
		title: "Delete this note?",
		message:
			note.value && note.value.status === "Processed"
				? `This removes the note from your list. Knowledge ${agentName} already extracted from it is kept.`
				: `This note hasn't been processed yet - ${agentName} won't learn from it if you delete it now.`,
		onConfirm: async ({ hideDialog }) => {
			deleting.value = true;
			try {
				await deleteNote(props.name);
				hideDialog();
				show.value = false;
				toast.success("Note deleted");
				emit("changed");
			} catch (e) {
				toast.error(errMsg(e));
			} finally {
				deleting.value = false;
			}
		},
	});
}

function onReanswer() {
	if (!note.value || !note.value.question) return;
	emit("reanswer", note.value.question);
	show.value = false;
}

function openWiki() {
	show.value = false;
	router.push({ name: "SkillsList", hash: "#wiki" });
}
</script>
