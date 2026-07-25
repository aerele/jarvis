<template>
	<!-- No `chat-surface`: this is a frappe-ui form (like Helpdesk's customer
	     portal new-ticket), NOT a chat surface. Rendering outside the jv-root
	     forms reset is what lets the Subject FormControl + the TextEditor keep
	     their stock frappe-ui look instead of the bare UA input the chat reset
	     produced. -->
	<SupportShell
		:crumbs="[{ label: 'Support', route: { name: 'Support' } }, { label: 'New ticket' }]"
	>
		<div class="flex h-full flex-col overflow-y-auto">
			<div class="mx-auto flex w-full max-w-3xl flex-col gap-5 px-5 py-8">
				<div>
					<h1 class="text-xl font-semibold text-ink-gray-9">How can we help?</h1>
					<p class="mt-1 text-p-base text-ink-gray-6">
						Start a ticket and the Aerele support team will pick it up - usually within
						a few hours.
					</p>
				</div>

				<div class="flex flex-col gap-2">
					<span class="text-sm text-ink-gray-6">
						Subject <span class="text-ink-red-5">*</span>
					</span>
					<FormControl
						v-model="subject"
						type="text"
						placeholder="A short summary - e.g. Invoice total is wrong"
						:maxlength="140"
					/>
				</div>

				<div class="flex flex-col gap-2">
					<span class="text-sm text-ink-gray-6">Description</span>
					<!-- Same recipe as CommentComposer: a bordered TextEditor + a footer
					     row. Ctrl/Cmd+Enter submits. -->
					<div
						class="rounded-lg border border-outline-gray-2"
						@keydown="onEditorKeydown"
					>
						<TextEditor
							:content="initialBody"
							editor-class="prose-sm max-w-none min-h-[9rem] px-3 py-2.5"
							:bubble-menu="true"
							placeholder="Describe the issue - what you expected, what happened, and where."
							@change="(v) => (bodyHtml = v)"
						/>
						<div
							class="flex flex-wrap items-center gap-2 border-t border-outline-gray-2 px-2 py-2"
						>
							<label
								class="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-outline-gray-2 px-2 py-1 text-sm text-ink-gray-7 hover:bg-surface-gray-2"
							>
								<input type="file" multiple class="hidden" @change="onFileInput" />
								<FeatherIcon name="paperclip" class="size-4" />
								<span>Attach</span>
							</label>
							<span
								v-for="p in pending"
								:key="p.key"
								class="jv-supn-chip inline-flex max-w-[14rem] items-center gap-1.5 rounded-md bg-surface-gray-2 px-2 py-1 text-sm text-ink-gray-7"
							>
								<span class="truncate">{{ p.file_name }}</span>
								<button
									type="button"
									aria-label="Remove attachment"
									class="shrink-0 text-ink-gray-5 hover:text-ink-gray-8"
									@click="removeFile(p.key)"
								>
									<FeatherIcon name="x" class="size-3.5" />
								</button>
							</span>
							<div class="ml-auto">
								<Button
									variant="solid"
									label="Submit"
									:loading="creating"
									:disabled="!canSubmit"
									@click="create"
								/>
							</div>
						</div>
					</div>
				</div>
			</div>
		</div>
	</SupportShell>
</template>

<script setup>
import "frappe-ui/editor-style.css";
import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { FormControl, TextEditor, Button, FeatherIcon, toast } from "frappe-ui";
import DOMPurify from "dompurify";
import SupportShell from "@/components/support/SupportShell.vue";
import { useSupportStore } from "@/stores/support";
import { useStagedFiles } from "@/composables/useStagedFiles";

const route = useRoute();
const router = useRouter();
const store = useSupportStore();

// A repeated query param (?subject=a&subject=b) makes vue-router hand back an
// ARRAY, not a string — String([...]) would silently join it with commas
// instead of taking the first value, so unwrap before stringifying.
function firstOf(v) {
	return Array.isArray(v) ? v[0] : v;
}
// The chat "Get help from a human" hook carries context as READABLE plain text.
// TextEditor's content is HTML, so escape it and turn line breaks into markup so
// the reference keeps its shape in the editor (and, once sent, in the thread).
function plainToHtml(s) {
	const esc = String(s || "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
	return esc ? "<p>" + esc.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>") + "</p>" : "";
}

const subject = ref(String(firstOf(route.query.subject) || ""));
const initialBody = plainToHtml(firstOf(route.query.body));
const bodyHtml = ref(initialBody);

const creating = ref(false);

// Staged-file / object-URL lifecycle shared with SupportThreadPage.
const { pending, onFiles, removeFile, snapshotStaged, settleUpload } = useStagedFiles();

function onFileInput(e) {
	onFiles(e.target.files);
	e.target.value = ""; // let the same file be picked again after removal
}

// TipTap emits "<p></p>" for an empty doc — strip tags to detect real emptiness
// (an image-only body still counts as content). Same test as CommentComposer.
const bodyEmpty = computed(
	() => !bodyHtml.value.replace(/<[^>]*>/g, "").trim() && !/<img\b/i.test(bodyHtml.value)
);
// Subject AND description are both required, matching Helpdesk's new-ticket form
// (its Submit is disabled while the editor is empty or the subject is blank).
const canSubmit = computed(() => !creating.value && !!subject.value.trim() && !bodyEmpty.value);

async function create() {
	if (!canSubmit.value) return;
	creating.value = true;
	// Snapshot BEFORE the awaited createTicket/uploadTo: a file attached while
	// this is in flight must not be silently revoked/dropped by settleUpload.
	const staged = snapshotStaged();
	try {
		// Sanitize the editor's HTML before it leaves the browser (TipTap output is
		// already safe, but never store un-sanitized markup); the thread re-runs
		// renderSupportHtml on display regardless.
		const body = DOMPurify.sanitize(bodyHtml.value);
		const name = await store.createTicket(subject.value.trim(), body);
		if (!name) return; // the store toasted; keep the draft so nothing is lost

		if (staged.length) {
			const uploaded = await store.uploadTo(name, staged);
			settleUpload(uploaded);
		}

		// The ticket now exists regardless of the upload outcome, so navigate; any
		// per-file shortfall was already surfaced by the store's toasts, and this
		// component unmounts either way.
		toast.success("Ticket created");
		await store.loadTickets({ quiet: true });
		router.replace({ name: "SupportTicket", params: { ticket: name } });
	} finally {
		creating.value = false;
	}
}

function onEditorKeydown(e) {
	if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
		e.preventDefault();
		create();
	}
}
</script>
