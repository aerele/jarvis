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
	try {
		const name = await store.createTicket(subject.value.trim(), body.value.trim());
		if (!name) return; // the store toasted; keep everything so nothing is lost
		if (files.value.length) await store.uploadTo(name, files.value);
		files.value.forEach(releasePreview);
		files.value = [];
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
