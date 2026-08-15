<template>
	<div>
		<div class="mb-4 flex items-center gap-2">
			<span class="text-base font-semibold text-ink-gray-9">Comments</span>
			<Badge
				v-if="comments.length"
				variant="subtle"
				theme="gray"
				:label="String(comments.length)"
			/>
		</div>

		<div v-if="comments.length" class="flex flex-col">
			<div v-for="c in comments" :key="c.name" class="flex gap-3 py-3">
				<Avatar
					size="md"
					:image="c.owner_image"
					:label="c.owner_name || c.owner"
					class="shrink-0"
				/>
				<div class="min-w-0 flex-1">
					<div class="flex items-center gap-2 text-sm">
						<span class="font-medium text-ink-gray-8">{{
							c.owner_name || c.owner
						}}</span>
						<Tooltip :text="exactDate(c.creation)">
							<span class="text-ink-gray-5">{{ timeAgo(c.creation) }}</span>
						</Tooltip>
						<div v-if="isOwn(c) && editingId !== c.name" class="ml-auto">
							<Dropdown :options="menuFor(c)" placement="right">
								<template #trigger>
									<Button
										variant="ghost"
										icon="more-horizontal"
										class="!h-5 !w-5"
									/>
								</template>
							</Dropdown>
						</div>
					</div>
					<CommentComposer
						v-if="editingId === c.name"
						class="mt-1"
						:content="c.content"
						submit-label="Save"
						:loading="savingId === c.name"
						autofocus
						@submit="(newHtml) => saveEdit(c, newHtml)"
						@discard="editingId = null"
					/>
					<div
						v-else
						class="prose prose-sm mt-1 max-w-none rounded bg-surface-gray-1 px-3 py-[7.5px] text-base leading-6"
						v-html="sanitize(c.content)"
					/>
				</div>
			</div>
		</div>
		<div v-else class="text-sm text-ink-gray-5">
			No comments yet - be the first to add one.
		</div>

		<template v-if="canComment">
			<button
				v-if="!composerOpen"
				type="button"
				class="mt-4 block w-full cursor-text rounded-lg border p-2 text-left"
				@click="composerOpen = true"
			>
				<span class="block min-h-[4rem] text-base text-ink-gray-4">
					Add a comment… @ to mention
				</span>
			</button>
			<CommentComposer
				v-else
				class="mt-4"
				:loading="posting"
				autofocus
				@submit="postComment"
				@discard="composerOpen = false"
			/>
		</template>
	</div>
</template>

<script setup>
// CommentsSection - the doc-page comment stream (DESIGN-V3 §6.1): asc by
// creation, own-comment edit (inline composer) / delete (confirm), comment
// HTML sanitized with DOMPurify (§14 O2 - TextEditor output rendered via
// v-html). The composer is a defineAsyncComponent behind a click-to-reveal
// placeholder, so the tiptap chunk (~280KB gzip) never loads - let alone
// leaks into list/detail chunks (D33) - unless the user actually comments
// or edits.
import { ref, computed, defineAsyncComponent } from "vue";
import { Avatar, Badge, Button, Dropdown, Tooltip, confirmDialog } from "frappe-ui";
import DOMPurify from "dompurify";
import { session } from "@/data/session";
import { timeAgo, exactDate } from "@/utils/datetime";

const CommentComposer = defineAsyncComponent(() => import("@/components/doc/CommentComposer.vue"));

const props = defineProps({
	docmeta: { type: Object, required: true }, // useDocmeta() object
	canComment: { type: Boolean, default: false },
});

const comments = computed(() => (props.docmeta.meta && props.docmeta.meta.comments) || []);

const composerOpen = ref(false); // false = lightweight placeholder, true = real editor
const posting = ref(false);
const editingId = ref(null);
const savingId = ref(null);

function sanitize(content) {
	return DOMPurify.sanitize(content || "");
}
function isOwn(c) {
	return c.owner === session.user;
}

function menuFor(c) {
	return [
		{ label: "Edit", icon: "edit-2", onClick: () => (editingId.value = c.name) },
		{ label: "Delete", icon: "trash-2", theme: "red", onClick: () => confirmDelete(c) },
	];
}

function confirmDelete(c) {
	confirmDialog({
		title: "Delete comment?",
		message: "This can't be undone.",
		onConfirm: async ({ hideDialog }) => {
			await props.docmeta.deleteComment(c.name);
			hideDialog();
		},
	});
}

async function postComment(html) {
	if (posting.value) return;
	posting.value = true;
	try {
		const row = await props.docmeta.addComment(html);
		if (row) composerOpen.value = false; // unmount = fresh empty editor next time
	} finally {
		posting.value = false;
	}
}

async function saveEdit(c, html) {
	if (savingId.value) return;
	savingId.value = c.name;
	try {
		const ok = await props.docmeta.updateComment(c.name, html);
		if (ok) editingId.value = null;
	} finally {
		savingId.value = null;
	}
}
</script>

<style>
/* Mention chips inside rendered comments. Mirrors frappe-ui's mention
   style.css, which only ships with the lazy composer chunk - comments with
   mentions must render styled even before the composer is ever opened. */
.prose .mention {
	font-weight: 600;
	box-decoration-break: clone;
	padding: 0 1px;
	border-radius: 4px;
	display: inline-block;
}
</style>
