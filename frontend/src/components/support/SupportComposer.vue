<template>
	<!-- The shared Support rich-text composer: a bordered frappe-ui TextEditor +
	     an attach/submit footer, used by BOTH the new-ticket page and the thread
	     reply so the two feel identical ("unanimous"). Enter inserts a newline;
	     Ctrl/Cmd+Enter submits — the right gesture for a formatting editor that
	     can hold lists/tables (a bare Enter must make a new line there).

	     Mounted in two surfaces: the new-ticket page renders it OUTSIDE the jv-root
	     forms reset; the thread page renders it INSIDE a chat-surface (jv-root).
	     index.css's reset only reverts bare input/textarea/select — the ProseMirror
	     editable is a contenteditable <div> and the toolbar are <button>s, so the
	     editor surface is untouched. The scoped rule at the bottom re-asserts a
	     stock input look for the few transient inputs the editor spawns (its
	     Link-URL popover) so it looks the same in both mounts.

	     @paste/@drop capture: images pasted or dropped into the editor must NOT go
	     inline — the editor's default upload targets the customer bench (a /files/
	     URL that 404s in our thread proxy and is wrong-origin for the agent), and
	     support uploads are ticket-scoped (no ticket exists during new-ticket
	     composition). Intercept at capture BEFORE ProseMirror inserts its optimistic
	     node (so no broken placeholder) and route the files into the ATTACHMENT flow. -->
	<div class="jv-supc">
		<div
			class="rounded-lg border border-outline-gray-2"
			@keydown="onEditorKeydown"
			@paste.capture="onEditorPaste"
			@drop.capture="onEditorDrop"
			@dragover.capture.prevent
		>
			<TextEditor
				:content="modelValue"
				editor-class="prose-sm max-w-none min-h-[9rem] px-3 py-2.5"
				:fixed-menu="TOOLBAR"
				:upload-function="rejectInlineUpload"
				:placeholder="placeholder"
				@change="(v) => emit('update:modelValue', v)"
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
					class="jv-supc-chip inline-flex max-w-[14rem] items-center gap-1.5 rounded-md bg-surface-gray-2 px-2 py-1 text-sm text-ink-gray-7"
				>
					<span class="truncate">{{ p.file_name }}</span>
					<button
						type="button"
						:aria-label="`Remove ${p.file_name}`"
						class="shrink-0 text-ink-gray-5 hover:text-ink-gray-8"
						@click="emit('remove-attachment', i)"
					>
						<FeatherIcon name="x" class="size-3.5" />
					</button>
				</span>
				<div class="ml-auto">
					<Button
						variant="solid"
						:label="submitLabel"
						:loading="loading"
						:disabled="!canSubmit"
						@click="onSubmit"
					/>
				</div>
			</div>
		</div>
		<!-- Fine print under the card (the thread's reopen note). "" hides it, so
		     the new-ticket page renders nothing here. -->
		<div v-if="disclaimer" class="jv-supc-disclaimer">{{ disclaimer }}</div>
	</div>
</template>

<script setup>
import "frappe-ui/editor-style.css";
import { ref } from "vue";
import { TextEditor, Button, FeatherIcon, toast } from "frappe-ui";
import { withinSize } from "@/composables/useStagedFiles";

const props = defineProps({
	// v-model: the editor's HTML content. The HOST owns the value (it needs it for
	// its own arm/empty gating and for the send payload). Setting it to "" clears
	// the editor — TextEditor watches `content` and setContent()s on a real change;
	// there is no caret jump while typing because the host stores the emitted HTML
	// verbatim, so content === getHTML() on every keystroke and the watcher no-ops.
	modelValue: { type: String, default: "" },
	// Staged-attachment display chips from the host's useStagedFiles ({key, file_name}).
	pending: { type: Array, default: () => [] },
	placeholder: { type: String, default: "" },
	submitLabel: { type: String, default: "Submit" },
	// HOST-computed arming: create() needs subject+body, reply() arms on body OR
	// files — the two differ, so the component never derives this. It only disables
	// Submit and gates the Ctrl/Cmd+Enter path on it.
	canSubmit: { type: Boolean, default: false },
	// Submit-button spinner (the host's creating/sending flag).
	loading: { type: Boolean, default: false },
	// Fine print under the card; "" hides the line entirely.
	disclaimer: { type: String, default: "" },
});

const emit = defineEmits(["update:modelValue", "submit", "files-added", "remove-attachment"]);

// Rich-but-safe toolbar: frappe-ui's default set MINUS the buttons that don't
// round-trip through create -> Helpdesk -> sanitized-render:
//   - Image/Video: uploads with nowhere to go during composition; routed to the
//     attach flow instead (see rejectInlineUpload).
//   - Iframe: stripped by the thread's DOMPurify -> vanishes on display.
//   - FontColor: emits `color: var(--prose-color-*)`, a variable defined only
//     inside the live editor, so it disappears in the rendered thread.
//   - Task List: the thread's `ul { list-style: revert }` shows a bullet AND a
//     checkbox per item, and the checkboxes never persist.
//   - TableOfContents: noise for a ticket; embeds a literal "No headings found".
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

// Submit (Ctrl/Cmd+Enter or the button). Gate on canSubmit HERE too, not only via
// the disabled button: the keyboard path bypasses it. The component only SIGNALS
// submit — the host owns body sanitization + the send (it holds the model and the
// business rules), so it reads its own value via prepareSupportBody on @submit.
function onSubmit() {
	if (props.canSubmit) emit("submit");
}

function onEditorKeydown(e) {
	// Ignore Ctrl/Cmd+Enter raised from a real <input> inside the editor — the
	// Link-URL popover's own field applies the link on Enter (Vue's .enter fires
	// on Ctrl+Enter too), and without this guard the same chord would ALSO bubble
	// here and send the reply / create the ticket mid-link-edit. The ProseMirror
	// editable is a contenteditable <div>, not an <input>, so real typing is unaffected.
	if (e.target.closest && e.target.closest("input")) return;
	if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
		e.preventDefault();
		onSubmit();
	}
}

const attachInput = ref(null);
function pickFiles() {
	attachInput.value && attachInput.value.click();
}
function onFileInput(e) {
	// e.target.files is a FileList; spread it ([...] not concat, which would append
	// the whole list as ONE nameless entry). withinSize enforces the cap (and toasts
	// on an oversize file). The picker path is silent — no "added as attachment"
	// toast (the chip appearing IS the feedback); paste/drop/slash announce because
	// there the user's gesture targeted the editor, not the attach control.
	const accepted = withinSize([...e.target.files]);
	if (accepted.length) emit("files-added", accepted);
	e.target.value = ""; // let the same file be re-picked after removal
}

// Any files pasted or dropped onto the editor card become ATTACHMENTS, not inline
// media (see the TOOLBAR comment). preventDefault + stopPropagation FIRST — before
// withinSize/emit/toast — so the event is contained (ProseMirror never inserts an
// optimistic node) even if a later call throws; otherwise a throw would skip the
// prevent and resurrect the inline-upload path.
function stageDropped(list) {
	const accepted = withinSize(list);
	if (!accepted.length) return;
	emit("files-added", accepted);
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
	stageDropped(files);
}
function onEditorDrop(e) {
	const files = e.dataTransfer && e.dataTransfer.files;
	if (!files || !files.length) return;
	e.preventDefault();
	e.stopPropagation();
	stageDropped(files);
}
// Catch-all for the path a capture handler can't reach (the slash "/image"
// command). Stage the file as an attachment and reject the inline embed; the editor
// leaves an errored placeholder the user can delete — acceptable for this rare path,
// and far better than a silently-broken image.
function rejectInlineUpload(file) {
	const accepted = withinSize([file]); // enforce the cap here too (withinSize toasts if over)
	if (accepted.length) {
		emit("files-added", accepted);
		toast.info(`"${file.name}" added as an attachment.`);
	}
	return Promise.reject(new Error("inline media is staged as an attachment"));
}
</script>

<style scoped>
/* Mounted both outside jv-root (new-ticket) and inside a chat-surface jv-root
   (thread). index.css's jv-root reset reverts bare input/textarea to UA defaults;
   the ProseMirror editable is a <div> (untouched), but the editor's transient
   Link-URL popover uses a real <input> — give any input inside this card a stock
   bordered look so the editor is usable and consistent in both mounts. :deep()
   because those inputs live inside the child TextEditor. The file input is hidden,
   so it is excluded. */
.jv-supc :deep(input:not([type="checkbox"]):not([type="radio"]):not([type="file"])) {
	font-size: 0.875rem;
	line-height: 1.5;
	padding: 0.375rem 0.5rem;
	border: 1px solid var(--outline-gray-2, #d1d5db);
	border-radius: 0.5rem;
	background-color: var(--surface-white, #fff);
}
.jv-supc-disclaimer {
	text-align: center;
	font-size: 10.5px;
	color: var(--text-3, #6b7280);
	margin-top: 8px;
}
</style>
