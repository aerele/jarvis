<template>
	<!-- No `chat-surface`: this is a frappe-ui form (like Helpdesk's customer
	     portal new-ticket), NOT a chat surface. Rendering outside the jv-root
	     forms reset is what lets the Subject FormControl keep its stock frappe-ui
	     look instead of the bare UA input the chat reset produces. (The
	     SupportComposer below is reset-proof either way — see its header.) -->
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
					<!-- The SAME rich composer the thread reply uses, so the two feel
					     unanimous. It owns the editor, toolbar, attach flow and body
					     sanitize; the page keeps only the subject + create wiring. -->
					<SupportComposer
						v-model="bodyHtml"
						:pending="pending"
						:can-submit="canSubmit"
						:loading="creating"
						placeholder="Describe the issue - what you expected, what happened, and where."
						@submit="create"
						@files-added="(fs) => onFiles(fs)"
						@remove-attachment="removeFile"
					/>
				</div>
			</div>
		</div>
	</SupportShell>
</template>

<script setup>
import { computed, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { FormControl, toast } from "frappe-ui";
import SupportShell from "@/components/support/SupportShell.vue";
import SupportComposer from "@/components/support/SupportComposer.vue";
import { useSupportStore } from "@/stores/support";
import { useStagedFiles } from "@/composables/useStagedFiles";
import { supportBodyIsEmpty, prepareSupportBody } from "@/lib/supportBody";

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

// Staged-file / object-URL lifecycle shared with SupportThreadPage. The composer
// size-caps files before handing them here, so onFiles receives accepted files.
const { files, pending, onFiles, removeFile, snapshotStaged, settleUpload } = useStagedFiles();

// Subject AND description are both required, matching Helpdesk's new-ticket form
// (its Submit is disabled while the editor is empty or the subject is blank).
const canSubmit = computed(
	() => !creating.value && !!subject.value.trim() && !supportBodyIsEmpty(bodyHtml.value)
);

async function create() {
	if (!canSubmit.value) return;
	creating.value = true;
	// Snapshot BEFORE the awaited createTicket/uploadTo: a file attached while
	// this is in flight must not be silently revoked/dropped by settleUpload.
	const staged = snapshotStaged();
	try {
		// Clean the editor HTML here (the host owns the send): strip inline
		// data:/blob: images + DOMPurify. canSubmit already guaranteed real text is
		// present, so `body` is non-empty. slice(0, 140): a ?subject= prefill
		// bypasses the input's maxlength, and Helpdesk hard-rejects a >140-char subject.
		const { body, stripped } = prepareSupportBody(bodyHtml.value);
		if (stripped) {
			toast.info("Embedded images were removed - add them with the Attach button instead.");
		}
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
</script>
