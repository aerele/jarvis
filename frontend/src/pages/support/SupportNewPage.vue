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
					     row. Ctrl/Cmd+Enter submits.
					     @paste/@drop capture: images pasted or dropped into the editor
					     must NOT go inline — the editor's default upload targets the
					     customer bench (a /files/ URL that 404s in our thread proxy and is
					     wrong-origin for the agent), and support uploads are ticket-scoped
					     with no ticket during composition. Intercept at capture BEFORE
					     ProseMirror inserts its optimistic node (so no broken placeholder)
					     and route the files into the ATTACHMENT flow, where they reach the
					     agent correctly. -->
					<div
						class="rounded-lg border border-outline-gray-2"
						@keydown="onEditorKeydown"
						@paste.capture="onEditorPaste"
						@drop.capture="onEditorDrop"
						@dragover.capture.prevent
					>
						<TextEditor
							:content="initialBody"
							editor-class="prose-sm max-w-none min-h-[9rem] px-3 py-2.5"
							:fixed-menu="TOOLBAR"
							:upload-function="rejectInlineUpload"
							placeholder="Describe the issue - what you expected, what happened, and where."
							@change="(v) => (bodyHtml = v)"
						/>
						<div
							class="flex flex-wrap items-center gap-2 border-t border-outline-gray-2 px-2 py-2"
						>
							<!-- A real <button> (not a bare label wrapping a hidden input) so
							     keyboard users can reach and trigger Attach. -->
							<button
								type="button"
								class="inline-flex items-center gap-1.5 rounded-md border border-outline-gray-2 px-2 py-1 text-sm text-ink-gray-7 hover:bg-surface-gray-2"
								@click="pickFiles"
							>
								<FeatherIcon name="paperclip" class="size-4" />
								<span>Attach</span>
							</button>
							<input
								ref="attachInput"
								type="file"
								multiple
								class="hidden"
								@change="onFileInput"
							/>
							<span
								v-for="(p, i) in pending"
								:key="p.key"
								class="jv-supn-chip inline-flex max-w-[14rem] items-center gap-1.5 rounded-md bg-surface-gray-2 px-2 py-1 text-sm text-ink-gray-7"
							>
								<span class="truncate">{{ p.file_name }}</span>
								<button
									type="button"
									:aria-label="`Remove ${p.file_name}`"
									class="shrink-0 text-ink-gray-5 hover:text-ink-gray-8"
									@click="removeFile(i)"
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
import { computed, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { FormControl, TextEditor, Button, FeatherIcon, toast } from "frappe-ui";
import DOMPurify from "dompurify";
import SupportShell from "@/components/support/SupportShell.vue";
import { useSupportStore } from "@/stores/support";
import { useStagedFiles, withinSize } from "@/composables/useStagedFiles";

const route = useRoute();
const router = useRouter();
const store = useSupportStore();

// Rich-but-safe toolbar. This is frappe-ui's default set MINUS the buttons that
// don't round-trip through our create -> Helpdesk -> sanitized-render pipeline:
//   - Image/Video: file uploads with nowhere to go during composition (no ticket
//     yet); routed to the attach flow instead (see rejectInlineUpload).
//   - Iframe: stripped by our thread DOMPurify -> vanishes on display.
//   - FontColor: emits `color: var(--prose-color-*)`, a variable defined only
//     inside the live editor, so the customer sees it while composing then it
//     disappears in the rendered thread.
//   - Task List: our thread's `ul { list-style: revert }` shows a bullet AND a
//     checkbox per item, and the checkboxes are clickable but never persist.
//   - TableOfContents: noise for a ticket, and embeds a literal "No headings
//     found" string when clicked empty.
const TOOLBAR = [
	"Paragraph",
	["Heading 1", "Heading 2", "Heading 3", "Heading 4", "Heading 5", "Heading 6"],
	"Separator",
	"Bold",
	"Italic",
	"Strikethrough",
	"Link",
	"Separator",
	"Bullet List",
	"Numbered List",
	"Separator",
	"Align Left",
	"Align Center",
	"Align Right",
	"Separator",
	"Blockquote",
	"Code",
	"Separator",
	"Horizontal Rule",
	[
		"InsertTable",
		"AddColumnBefore",
		"AddColumnAfter",
		"DeleteColumn",
		"AddRowBefore",
		"AddRowAfter",
		"DeleteRow",
		"MergeCells",
		"SplitCell",
		"ToggleHeaderColumn",
		"ToggleHeaderRow",
		"ToggleHeaderCell",
		"DeleteTable",
	],
	"Separator",
	"Undo",
	"Redo",
];

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
		.replace(/\r\n?/g, "\n") // normalize CRLF so paragraph breaks (\n\n) still match
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
	return esc ? "<p>" + esc.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>") + "</p>" : "";
}

const subject = ref(String(firstOf(route.query.subject) || ""));
const initialBody = plainToHtml(firstOf(route.query.body));
const bodyHtml = ref(initialBody);

const creating = ref(false);
// The New page navigates + unmounts on success, so a create()/uploadTo() chain
// can outlive the component. `alive` gates the post-await navigation so a user
// who pressed Back mid-submit isn't yanked to the new ticket.
let alive = true;
onUnmounted(() => {
	alive = false;
});

// Staged-file / object-URL lifecycle shared with SupportThreadPage.
const { files, pending, onFiles, removeFile, snapshotStaged, settleUpload } = useStagedFiles();

const attachInput = ref(null);
function pickFiles() {
	attachInput.value && attachInput.value.click();
}
function onFileInput(e) {
	// e.target.files is a FileList; spread it (concat would append the whole list
	// as one element, so every multi-select would show a single nameless chip).
	onFiles(withinSize(e.target.files));
	e.target.value = ""; // let the same file be picked again after removal
}

// Any files pasted or dropped into the editor card become ATTACHMENTS, not
// inline media (see the TOOLBAR comment). The handlers preventDefault +
// stopPropagation FIRST — before onFiles/toast — so the event is contained
// (ProseMirror never inserts an optimistic node) even if staging or the toast
// were to throw; otherwise a throw would skip the prevent and resurrect the
// inline-upload path.
function stageFiles(list) {
	const accepted = withinSize(list);
	if (!accepted.length) return;
	onFiles(accepted);
	toast.info(
		accepted.length === 1
			? `"${accepted[0].name}" added as an attachment.`
			: `${accepted.length} files added as attachments.`
	);
}
function onEditorPaste(e) {
	const files = e.clipboardData && e.clipboardData.files;
	if (!files || !files.length) return; // plain-text paste passes through untouched
	e.preventDefault();
	e.stopPropagation();
	stageFiles(files);
}
function onEditorDrop(e) {
	const files = e.dataTransfer && e.dataTransfer.files;
	if (!files || !files.length) return;
	e.preventDefault();
	e.stopPropagation();
	stageFiles(files);
}
// Catch-all for the paths a capture handler can't reach (the slash "/image"
// command). Stage the file as an attachment and reject the inline embed; the
// editor leaves an errored placeholder the user can delete — acceptable for this
// rare path, and far better than a silently-broken image.
function rejectInlineUpload(file) {
	const accepted = withinSize([file]); // enforce the size cap here too (withinSize toasts if over)
	if (accepted.length) {
		onFiles(accepted);
		toast.info(`"${file.name}" added as an attachment.`);
	}
	return Promise.reject(new Error("inline media is staged as an attachment"));
}

// TipTap emits "<p></p>" for an empty doc — strip tags to detect real emptiness.
// Inline images are disabled (they go to attachments), so the body is text: no
// <img> exception is needed, and omitting it means a stray errored placeholder
// can't masquerade as content and arm Submit. Also strip &nbsp; (both the entity
// and U+00A0) — a "<p>&nbsp;</p>" from a stray space would otherwise look empty
// to the user yet arm Submit.
const bodyEmpty = computed(
	() =>
		!bodyHtml.value
			.replace(/<[^>]*>/g, "")
			.replace(/&nbsp;|&#160;|&#xa0;/gi, "")
			.replace(/\u00a0/g, "")
			.trim()
);
// Subject AND description are both required, matching Helpdesk's new-ticket form
// (its Submit is disabled while the editor is empty or the subject is blank).
const canSubmit = computed(() => !creating.value && !!subject.value.trim() && !bodyEmpty.value);

// Strip inline data:/blob: images before sending. TipTap can leave them from an
// HTML/Word paste or a failed inline-upload placeholder; Helpdesk's server-side
// nh3 sanitize drops a data: src anyway (so the agent sees a broken image) and
// blob: URLs are per-session. Images belong in the attach flow, not the body.
// Sanitize (DOMPurify) LAST regardless — never store un-sanitized markup.
function cleanBody(html) {
	const doc = new DOMParser().parseFromString(html || "", "text/html");
	let stripped = 0;
	for (const img of doc.querySelectorAll('img[src^="data:" i], img[src^="blob:" i]')) {
		img.remove();
		stripped += 1;
	}
	if (stripped) {
		toast.info("Embedded images were removed - add them with the Attach button instead.");
	}
	return DOMPurify.sanitize(doc.body.innerHTML);
}

async function create() {
	if (!canSubmit.value) return;
	creating.value = true;
	// Snapshot BEFORE the awaited createTicket/uploadTo: a file attached while
	// this is in flight must not be silently revoked/dropped by settleUpload.
	const staged = snapshotStaged();
	try {
		const body = cleanBody(bodyHtml.value);
		// slice(0, 140): a ?subject= prefill bypasses the input's maxlength, and
		// Helpdesk hard-rejects a >140-char subject.
		const name = await store.createTicket(subject.value.trim().slice(0, 140), body);
		if (!name) return; // the store toasted; keep the draft so nothing is lost

		if (staged.length) {
			// Upload only files STILL staged: a chip removed during this in-flight
			// submit must not be uploaded (Helpdesk has no un-attach).
			const live = staged.filter((f) => files.value.includes(f));
			if (live.length) {
				const uploaded = await store.uploadTo(name, live);
				settleUpload(uploaded);
			}
		}

		// The user navigated away mid-submit: the ticket + uploads are done, but
		// this component no longer owns the view — don't hijack the router/toast
		// over wherever they went. (The uploads above were allowed to finish.)
		if (!alive) return;

		// Files attached AFTER submit began were never in `staged`; the page
		// navigates + unmounts, so they'd vanish with no feedback.
		if (files.value.some((f) => !staged.includes(f))) {
			toast.info(
				"Files added after you submitted weren't attached - add them from the ticket."
			);
		}

		toast.success("Ticket created");
		await store.loadTickets({ quiet: true });
		if (!alive) return; // unmounted during the refetch — don't hijack the router
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
