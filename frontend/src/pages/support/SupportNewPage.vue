<template>
	<SupportShell title="New ticket" :back-to="{ name: 'Support' }">
		<div class="jv-sup-new">
			<h1 class="jv-sup-h1">How can we help?</h1>

			<label class="jv-sup-label" for="jv-sup-subject">Subject</label>
			<FormControl
				id="jv-sup-subject"
				v-model="subject"
				type="text"
				placeholder="A short summary — e.g. Invoice total is wrong"
			/>

			<!-- Guidance is the placeholder only: a persistent helper line was
			     considered and dropped as clutter. -->
			<div class="jv-sup-newcomposer">
				<!-- No `busy`: see the reply composer — a Stop button with nothing to
				     stop. `canSend` disarms Send while the create is in flight. -->
				<Composer
					v-model="body"
					:attachments="pending"
					:can-send="canSend"
					placeholder="Describe the issue — what you expected, what happened, and where."
					@files-added="onFiles"
					@remove-attachment="removeFile"
					@submit="create"
				/>
			</div>
		</div>
	</SupportShell>
</template>

<script setup>
import { computed, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { FormControl, toast } from "frappe-ui";
import Composer from "@/components/chat/Composer.vue";
import SupportShell from "@/components/support/SupportShell.vue";
import { useSupportStore } from "@/stores/support";

const route = useRoute();
const router = useRouter();
const store = useSupportStore();

// Arriving from chat: the reference is carried as READABLE, EDITABLE body text.
// create_ticket takes only (subject, body) — there are no context params, and
// the tenant is derived server-side from the API key.
const subject = ref(String(route.query.subject || ""));
const body = ref(String(route.query.body || ""));

const creating = ref(false);
const files = ref([]);
const previews = new Map();

function previewFor(f) {
	if (!previews.has(f)) previews.set(f, /^image\//.test(f.type) ? URL.createObjectURL(f) : "");
	return previews.get(f);
}
function releasePreview(f) {
	const url = previews.get(f);
	if (url) URL.revokeObjectURL(url);
	previews.delete(f);
}

const pending = computed(() =>
	files.value.map((f, i) => ({
		key: `${f.name}-${i}`,
		file_name: f.name,
		preview_url: previewFor(f),
		removable: true,
	}))
);

// Subject is required — it is the ticket's identity in the list and in Helpdesk.
const canSend = computed(() => !creating.value && !!subject.value.trim());

function onFiles(added) {
	files.value = files.value.concat(added);
}
function removeFile(i) {
	const f = files.value[i];
	if (f) releasePreview(f);
	files.value = files.value.filter((_, n) => n !== i);
}

async function create() {
	if (!canSend.value) return;
	creating.value = true;
	// Snapshot BEFORE the awaited createTicket/uploadTo, exactly like send():
	// otherwise a file the user attaches while this create() is in flight is
	// never uploaded and gets silently revoked/dropped by the cleanup below,
	// which operates on whatever `files.value` happens to be afterward.
	const staged = files.value.slice();
	try {
		const name = await store.createTicket(subject.value.trim(), body.value.trim());
		if (!name) return; // the store toasted; keep everything so nothing is lost

		if (staged.length) {
			const uploaded = await store.uploadTo(name, staged);
			// Same reasoning as send(): uploadTo returns a COUNT of successes, not
			// which files made it, and there is no un-attach to undo a partial
			// batch — the store already toasted each failure. Only clear/revoke
			// when EVERY staged file uploaded; on any shortfall, leave ALL staged
			// files in place rather than guess which to drop. Remove by reference
			// (never blanket `= []`) so a file attached WHILE this create() is in
			// flight — never in `staged` — survives either branch untouched.
			if (uploaded === staged.length) {
				files.value = files.value.filter((f) => !staged.includes(f));
				staged.forEach(releasePreview);
			}
		}

		// Unlike send(), this page navigates away on success — so "leave staged
		// files in place" cannot mean "retry here", this component unmounts
		// either way. The ticket itself now exists regardless of the upload
		// outcome (same as send() posting the body regardless of what upload
		// does next), so navigation still happens; any shortfall was already
		// surfaced per-file by the store's toasts (no double-toast here), and
		// the un-uploaded local Files simply go away with the component instead
		// of being resurrected on a page that can no longer submit them.
		toast.success("Ticket created");
		await store.loadTickets({ quiet: true });
		router.replace({ name: "SupportTicket", params: { ticket: name } });
	} finally {
		creating.value = false;
	}
}

onUnmounted(() => files.value.forEach(releasePreview));
</script>

<style scoped>
.jv-sup-new {
	width: 100%;
	max-width: 680px;
	margin: 0 auto;
	padding: 40px 16px;
}
.jv-sup-h1 {
	margin: 0 0 24px;
	font-size: 24px;
	font-weight: 600;
	color: var(--text);
}
.jv-sup-label {
	display: block;
	margin-bottom: 6px;
	font-size: 13px;
	font-weight: 500;
	color: var(--text-2);
}
.jv-sup-newcomposer {
	margin-top: 20px;
}
</style>
