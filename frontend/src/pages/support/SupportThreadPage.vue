<template>
	<SupportShell :title="row ? row.subject || 'Ticket' : 'Ticket'" :back-to="{ name: 'Support' }">
		<template #actions>
			<Badge variant="subtle" :theme="badge.theme" :label="badge.label" />
			<Button
				v-if="row && !store.isClosed(row.status)"
				label="Resolve"
				:loading="resolving"
				@click="resolve"
			/>
		</template>

		<div class="jv-sup-thread" aria-live="polite">
			<div
				v-if="store.thread.loading && !store.thread.messages.length"
				class="jv-sup-center"
			>
				<LoadingIndicator class="size-5" />
			</div>

			<div
				v-else-if="store.thread.error && !store.thread.messages.length"
				class="jv-sup-center"
			>
				<FeatherIcon name="alert-circle" class="size-7 text-ink-red-4" />
				<p>Couldn't load this conversation.</p>
				<Button label="Try again" @click="store.loadThread(ticketName)" />
			</div>

			<template v-else>
				<div v-if="store.thread.attachments.length" class="jv-sup-files">
					<a
						v-for="a in store.thread.attachments"
						:key="a.file_url"
						class="jv-sup-file"
						:href="downloadUrl(a.file_url)"
						target="_blank"
						rel="noopener"
					>
						<FeatherIcon name="paperclip" class="size-3.5" />{{ a.file_name }}
					</a>
				</div>

				<Message
					v-for="m in store.thread.messages"
					:key="m.name"
					:variant="fromSupport(m) ? 'row' : 'bubble'"
					:html="renderSupportHtml(m.content, ticketName)"
					body-class="jv-html"
					:sender="fromSupport(m) ? 'Support' : ''"
					:attachments="attachmentsOf(m)"
					:timestamp="shortTime(m.creation)"
					:timestamp-full="fullTime(m.creation)"
					:copyable="false"
				>
					<template v-if="fromSupport(m)" #avatar>
						<div class="jv-sup-avatar">S</div>
					</template>
				</Message>
			</template>
		</div>

		<div class="jv-sup-composer">
			<!-- `busy` is deliberately NOT passed: it swaps Send for a Stop button
			     that emits `stop`, and support has nothing to stop — a reply is a
			     single POST, not a stream. A dead Stop control is worse than none.
			     `canSend` already goes false while sending, which disarms Send. -->
			<Composer
				v-model="draft"
				:attachments="pending"
				:can-send="canSend"
				placeholder="Reply to Aerele Support…"
				:disclaimer="disclaimer"
				@files-added="onFiles"
				@remove-attachment="removeFile"
				@submit="send"
			/>
		</div>
	</SupportShell>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Badge, Button, FeatherIcon, LoadingIndicator, toast } from "frappe-ui";
import Composer from "@/components/chat/Composer.vue";
import Message from "@/components/chat/Message.vue";
import SupportShell from "@/components/support/SupportShell.vue";
import { renderSupportHtml } from "@/lib/supportHtml";
import { supportDownloadUrl } from "@/api";
import { useSupportStore } from "@/stores/support";

// The #avatar slot renders a round human avatar, deliberately unlike Jarvis's
// gradient square: human-vs-AI must never be ambiguous. The rationale lives
// here rather than as a template comment — Vue keeps template comments as real
// DOM comment nodes, and a comment as a slot/root sibling compiles the branch
// into a multi-root fragment (which is what made a sibling page's
// `wrapper.element` silently resolve to the mount container).

const route = useRoute();
const store = useSupportStore();
const resolving = ref(false);
const draft = ref("");
const sending = ref(false);
const files = ref([]); // local File objects — uploaded on submit, never before

const ticketName = computed(() => String(route.params.ticket || ""));
const row = computed(() => store.ticketRow(ticketName.value));
const badge = computed(() => store.badgeFor(row.value && row.value.status));

// Composer takes DISPLAY objects, never File objects. Object URLs are created
// once per file and revoked on removal/submit so a long thread can't leak them.
const previews = new Map();
function previewFor(f) {
	if (!previews.has(f)) {
		previews.set(f, /^image\//.test(f.type) ? URL.createObjectURL(f) : "");
	}
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

const canSend = computed(() => !sending.value && (!!draft.value.trim() || files.value.length > 0));

// Reopen is reply-driven: there is no reopen endpoint, so the composer stays
// ENABLED on a resolved/closed ticket and says what replying will do.
const disclaimer = computed(() =>
	row.value && (store.isClosed(row.value.status) || row.value.status === "Resolved")
		? "Replying reopens this ticket."
		: ""
);

function onFiles(added) {
	files.value = files.value.concat(added);
}

function removeFile(i) {
	const f = files.value[i];
	if (f) releasePreview(f);
	files.value = files.value.filter((_, n) => n !== i);
}

// "Sent" means the support side sent it (helpdesk_client.py queries
// sent_or_received directly). Anything else — including a missing value — is
// this user's own message.
function fromSupport(m) {
	return m && m.sent_or_received === "Sent";
}

// NOTE: the identity line is the literal "Support", hardcoded in the template.
// The payload carries `sender` as an EMAIL and no display name at all (Global
// Constraint 6), and that email is frequently the service account
// jarvis-support-bot@jarvis.internal — showing it would leak plumbing at best
// and mislabel a human agent at worst. Do not add a constant for it; the
// template says "Support" directly.

// Message renders an image attachment only when `type === 'image' && file_url`
// and a file chip otherwise, keyed on `name` and titled `title`. The CP sends
// only {file_url, file_name}, so a naive pass-through renders NOTHING. Map into
// Message's shape and classify by extension, as the page being replaced did.
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|avif|bmp)$/i;

function attachmentsOf(m) {
	return ((m && m.attachments) || []).map((a) => ({
		name: a.file_url, // Message's :key — file_url is unique per attachment
		title: a.file_name,
		type: IMAGE_EXT.test(a.file_name || "") ? "image" : "file",
		file_url: downloadUrl(a.file_url),
	}));
}

function downloadUrl(fileUrl) {
	return supportDownloadUrl(ticketName.value, fileUrl);
}

function shortTime(v) {
	if (!v) return "";
	return new Date(v.replace(" ", "T")).toLocaleTimeString([], {
		hour: "numeric",
		minute: "2-digit",
	});
}
function fullTime(v) {
	if (!v) return "";
	return new Date(v.replace(" ", "T")).toLocaleString();
}

async function resolve() {
	resolving.value = true;
	const ok = await store.closeTicket(ticketName.value);
	if (ok) {
		toast.success("Ticket resolved");
		await store.loadTickets({ quiet: true });
	}
	resolving.value = false;
}

// ── live thread, cost-aware ────────────────────────────────────────────────
// A full get_thread is bench → CP → Helpdesk (4 sequential calls), so it is NOT
// polled. The cheap list call is polled instead, and the thread is refetched
// only when THIS ticket's row actually changed. Paused while the tab is hidden.
const POLL_MS = 30000;
let timer = null;
let lastPrint = "";

async function pollSignal() {
	if (document.hidden) return;
	await store.loadTickets({ quiet: true });
	const print = store.fingerprintOf(ticketName.value);
	if (print && print !== lastPrint) {
		await store.loadThread(ticketName.value, { quiet: true });
		// Advance the watermark only AFTER a successful refetch — otherwise one
		// failed quiet fetch swallows that change permanently and the thread
		// silently stops updating.
		if (!store.thread.error) lastPrint = print;
	}
}

// Focus refetches UNCONDITIONALLY, unlike the 30s tick. The fingerprint can only
// see what the list row exposes, and a second consecutive agent reply moves
// `modified` but nothing the user would notice — worse, a reply that lands while
// the ticket row is already "Replied" is invisible to it. Coming back to the tab
// is the one moment the extra call is worth paying for.
async function onFocus() {
	if (document.hidden) return;
	await store.loadTickets({ quiet: true });
	await store.loadThread(ticketName.value, { quiet: true });
	// Same guard as the tick: advancing the watermark after a FAILED refetch
	// would neuter the 30s poll until the row happens to change again.
	if (!store.thread.error) lastPrint = store.fingerprintOf(ticketName.value);
}

async function open(name) {
	if (!name) return;
	store.thread.ticket = name;
	await store.loadThread(name);
	lastPrint = store.fingerprintOf(name);
}

async function send() {
	if (!canSend.value) return;
	sending.value = true;
	const body = draft.value.trim();
	const staged = files.value.slice();
	try {
		// Body first, attachments second: media.upload attaches to an existing
		// ticket, and posting the text is what actually reopens a resolved one.
		if (body) {
			const ok = await store.reply(ticketName.value, body);
			if (!ok) return; // store already toasted; keep the draft so it isn't lost
			draft.value = ""; // text is posted regardless of what upload does next
		}

		if (staged.length) {
			const uploaded = await store.uploadTo(ticketName.value, staged);
			// uploadTo returns a COUNT of successes, not which files made it — the
			// store already toasted each failure, and Helpdesk's media.upload has
			// no un-attach to undo a partial batch. Guessing which File to drop
			// would be worse than doing nothing, so: only clear/revoke when EVERY
			// staged file uploaded; on any shortfall, leave ALL staged files in
			// place so the user can just hit Send again instead of re-picking
			// from disk. Removing by reference (never `files.value = []`) means a
			// file the user attaches WHILE this send is in flight — it is never in
			// `staged` — survives either branch untouched, and its preview is
			// never revoked out from under it.
			if (uploaded === staged.length) {
				files.value = files.value.filter((f) => !staged.includes(f));
				staged.forEach(releasePreview);
			}
		}

		await store.loadTickets({ quiet: true });
		await store.loadThread(ticketName.value);
		lastPrint = store.fingerprintOf(ticketName.value);
	} finally {
		sending.value = false;
	}
}

onMounted(async () => {
	if (!store.tickets.length) await store.loadTickets({ quiet: true });
	await open(ticketName.value);
	timer = setInterval(pollSignal, POLL_MS);
	window.addEventListener("focus", onFocus);
	document.addEventListener("visibilitychange", onFocus);
});

onUnmounted(() => {
	if (timer) clearInterval(timer);
	window.removeEventListener("focus", onFocus);
	document.removeEventListener("visibilitychange", onFocus);
	files.value.forEach(releasePreview);
});

watch(ticketName, (n) => open(n));
</script>

<style scoped>
.jv-sup-thread {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	padding: 20px 16px 28px;
	display: flex;
	flex-direction: column;
	gap: 4px;
}
.jv-sup-composer {
	flex: 0 0 auto;
	padding: 0 16px 16px;
}
.jv-sup-center {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 10px;
	padding: 48px 0;
	color: var(--text-2);
}
.jv-sup-files {
	display: flex;
	flex-wrap: wrap;
	gap: 8px;
	margin-bottom: 12px;
}
.jv-sup-file {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	padding: 4px 10px;
	border: 1px solid var(--border);
	border-radius: 999px;
	color: var(--link);
	font-size: 12px;
	text-decoration: none;
}
.jv-sup-avatar {
	display: flex;
	align-items: center;
	justify-content: center;
	width: 28px;
	height: 28px;
	margin-top: 2px;
	border-radius: 999px;
	background: var(--surface-3);
	color: var(--text-2);
	font-size: 12px;
	font-weight: 600;
}
/* No status-badge CSS here: the header pill is a frappe-ui Badge, same as the
   list. Hand-rolled pills are what produced the AA-contrast problem the spec
   flagged in the first place. */
</style>
