<template>
	<SupportShell :title="row ? row.subject || 'Ticket' : 'Ticket'" :back-to="{ name: 'Support' }">
		<template #actions>
			<!-- row is null for a deep-linked ticket outside the newest 50 (P2/out-of-list):
			     get_thread carries no status at all, so the row is the ONLY source —
			     rendering "Open" here would be an outright lie for a possibly-Closed
			     ticket. Hide the badge entirely rather than guess. -->
			<Badge v-if="row" variant="subtle" :theme="badge.theme" :label="badge.label" />
			<Button
				v-if="row && !store.isClosed(row.status)"
				label="Resolve"
				:loading="closing"
				@click="closeThisTicket"
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
				<div v-if="ticketAttachments.length" class="jv-sup-files">
					<a
						v-for="a in ticketAttachments"
						:key="a.name"
						class="jv-sup-file"
						:class="{ 'jv-sup-file-img': a.type === 'image' }"
						:href="a.file_url"
						target="_blank"
						rel="noopener"
					>
						<img
							v-if="a.type === 'image'"
							:src="a.file_url"
							:alt="a.title"
							loading="lazy"
							class="jv-sup-thumb"
						/>
						<template v-else>
							<FeatherIcon name="paperclip" class="size-3.5" />{{ a.title }}
						</template>
					</a>
				</div>

				<!-- Reachable: a brand-new ticket created with an empty body and no
				     files has zero Communications (the initial text is the HD Ticket's
				     `description`, not a reply) — without this, the thread area was
				     just a blank void with no explanation. -->
				<div v-if="!displayMessages.length" class="jv-sup-center">
					<p>This is the start of your conversation.</p>
				</div>

				<Message
					v-for="m in displayMessages"
					:key="m.key"
					:variant="m.variant"
					:html="m.html"
					body-class="jv-html"
					:sender="m.sender"
					:attachments="m.attachments"
					:timestamp="m.timestamp"
					:timestamp-full="m.timestampFull"
					:copyable="false"
				>
					<template v-if="m.fromSupport" #avatar>
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
import { useStagedFiles } from "@/composables/useStagedFiles";
import { formatDate, exactDate } from "@/utils/datetime";

// The #avatar slot renders a round human avatar, deliberately unlike Jarvis's
// gradient square: human-vs-AI must never be ambiguous. The rationale lives
// here rather than as a template comment — Vue keeps template comments as real
// DOM comment nodes, and a comment as a slot/root sibling compiles the branch
// into a multi-root fragment (which is what made a sibling page's
// `wrapper.element` silently resolve to the mount container).

const route = useRoute();
const store = useSupportStore();
const closing = ref(false);
const draft = ref("");
const sending = ref(false);

// I4: staged-file / object-URL lifecycle shared with SupportNewPage.
const { files, pending, onFiles, removeFile, snapshotStaged, settleUpload, reset } =
	useStagedFiles();

const ticketName = computed(() => String(route.params.ticket || ""));
const row = computed(() => store.ticketRow(ticketName.value));
const badge = computed(() => store.badgeFor(row.value && row.value.status));

const canSend = computed(() => !sending.value && (!!draft.value.trim() || files.value.length > 0));

// Reopen is reply-driven: there is no reopen endpoint, so the composer stays
// ENABLED on a resolved/closed ticket and says what replying will do.
const disclaimer = computed(() =>
	row.value && (store.isClosed(row.value.status) || row.value.status === "Resolved")
		? "Replying reopens this ticket."
		: ""
);

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

// A File doctype record can legitimately have no file_name (e.g. an upload
// that never got one attached server-side) — falling back to a blank string
// would render Message's chip as an unlabeled "📎 " with nothing readable.
// The URL's own basename is still a usable label; strip query/hash first.
function basenameOf(url) {
	if (!url) return "";
	const clean = url.split("?")[0].split("#")[0];
	const parts = clean.split("/");
	return parts[parts.length - 1] || "";
}

// Shared by per-message attachments AND ticket-level ones below — both are the
// identical {file_url, file_name} shape from the CP, so classifying them twice
// would just be the same regex and proxy-URL call copy-pasted.
function classifyAttachment(a) {
	const label = a.file_name || basenameOf(a.file_url);
	return {
		name: a.file_url, // unique per attachment; used as the render key
		title: label,
		type: IMAGE_EXT.test(label) ? "image" : "file",
		file_url: downloadUrl(a.file_url),
	};
}

function attachmentsOf(m) {
	return ((m && m.attachments) || []).map(classifyAttachment);
}

// Ticket-level attachments (shown above the message list) used to render as a
// plain download chip with no image check at all — a regression from the old
// SupportPage, which did classify these. Message.vue already proves the image
// vs. file split for per-message attachments; this brings ticket-level ones up
// to the same standard instead of leaving them a second, worse code path.
const ticketAttachments = computed(() => store.thread.attachments.map(classifyAttachment));

function downloadUrl(fileUrl) {
	return supportDownloadUrl(ticketName.value, fileUrl);
}

// I3: display objects computed ONCE per messages/ticket change, not re-parsed
// and re-sanitized inline in the v-for on every render — the template also
// depends on `draft` and on `row`, so an inline call re-ran on every keystroke.
// shortTime/fullTime route through datetime.js's dayjsLocal-backed helpers
// (I5): Frappe's `creation` is a naive SITE-timezone string, and parsing it
// with `new Date()` (the previous implementation) treats it as browser-local,
// showing the wrong time for any viewer whose timezone differs from the site's
// — the exact bug datetime.js exists to fix, same as the sibling list page.
const displayMessages = computed(() =>
	store.thread.messages.map((m) => {
		const support = fromSupport(m);
		return {
			key: m.name,
			variant: support ? "row" : "bubble",
			html: renderSupportHtml(m.content, ticketName.value),
			attachments: attachmentsOf(m),
			sender: support ? "Support" : "",
			timestamp: formatDate(m.creation, "h:mm A"),
			timestampFull: exactDate(m.creation),
			fromSupport: support,
		};
	})
);

// M10: named for what this actually does — it calls store.closeTicket and the
// server sets status Closed — not the button's visible "Resolve" label.
// "Resolved" is a distinct status in the awaiting-set contract (see the
// store), so the two words are NOT interchangeable; the label-vs-action
// wording mismatch is a deliberate, separate UX decision for a later pass.
async function closeThisTicket() {
	closing.value = true;
	const ok = await store.closeTicket(ticketName.value);
	if (ok) {
		toast.success("Ticket resolved");
		await store.loadTickets({ quiet: true });
	}
	closing.value = false;
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
	// Out-of-list ticket (deep-linked, outside the newest 50 the list ever
	// fetches): ticketRow(name) is null forever, so fingerprintOf returns "" and
	// `print !== lastPrint` below can NEVER fire — the 30s poll would otherwise
	// be permanently dead for it. Fall back to an unconditional quiet refetch,
	// same as onFocus, so an agent reply still surfaces.
	if (!store.ticketRow(ticketName.value)) {
		await store.loadThread(ticketName.value, { quiet: true });
		if (!store.thread.error) lastPrint = store.fingerprintOf(ticketName.value);
		return;
	}
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
// is the one moment the extra call is worth paying for. Bound ONLY to
// `visibilitychange` (M8) — it already covers tab return, and pairing it with
// a `window "focus"` listener fired this same expensive refetch twice.
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
	// thread.ticket is the NAME of whichever ticket is currently loaded/loading
	// (see the store's comment) — compare BEFORE overwriting it, so a
	// same-ticket refresh (route re-entering the same ticket) can be told apart
	// from an actual switch.
	if (store.thread.ticket !== name) {
		// C1: a DIFFERENT ticket — drop the previous one's messages before this
		// fetch starts. Otherwise the new ticket's title/composer sit over the
		// old conversation for the whole fetch, and a failed fetch would never
		// show the error branch (it's gated on `!thread.messages.length`),
		// leaving the old ticket's conversation on the new ticket's URL with a
		// composer that posts to the new ticket. A same-ticket refresh (the
		// `if` above is false) keeps the in-place last-good messages so the
		// quiet 30s poll doesn't flicker.
		store.thread.messages = [];
		store.thread.attachments = [];
		store.thread.error = "";
		// Composer state is per-TICKET: a draft or staged files left over from the
		// PREVIOUS ticket must not silently ride along and post to this one.
		draft.value = "";
		reset();
	}
	store.thread.ticket = name;
	await store.loadThread(name);
	// M9: advance the watermark only after a successful refetch — same guard
	// as pollSignal/onFocus. Without it, a failed initial open() permanently
	// no-ops the 30s poll (lastPrint would already match, so the poll never
	// sees a "change").
	if (!store.thread.error) lastPrint = store.fingerprintOf(name);
}

// A synthesized reply for a files-only Send: brief and human — a note the
// user would plausibly have typed themselves, not a robotic system notice.
// Never blank: see send() for why an empty body here would skip the reply
// Communication entirely.
function synthesizedBodyFor(staged) {
	return staged.length === 1
		? `Sharing a file: ${staged[0].name}`
		: `Sharing ${staged.length} files`;
}

async function send() {
	if (!canSend.value) return;
	sending.value = true;
	const userBody = draft.value.trim();
	// Snapshot BEFORE the awaited reply/uploadTo below — see useStagedFiles.
	const staged = snapshotStaged();
	// CRITICAL fix: canSend arms on files alone, but a reply Communication is
	// what actually reopens a Resolved/Closed ticket AND notifies the agent
	// (helpdesk_client.py's post_reply: "Received" => auto-reopen) — media.upload
	// is a bare File attach with no Communication at all. Without this, an
	// attachment-only Send looked like success but silently did neither.
	const synthesized = !userBody && staged.length > 0;
	const body = synthesized ? synthesizedBodyFor(staged) : userBody;
	try {
		// Body first, attachments second: media.upload attaches to an existing
		// ticket, and posting the text is what actually reopens a resolved one.
		if (body) {
			const ok = await store.reply(ticketName.value, body);
			if (!ok) return; // store already toasted; keep the draft so it isn't lost
			// I2: only clear if the draft still holds exactly what was posted —
			// a blanket clear would wipe text the user kept typing during the
			// in-flight reply. Same reference-safety idea as the staged files.
			// A SYNTHESIZED body was never in the draft (the user typed nothing),
			// so it must never be compared against or clear draft.value.
			if (!synthesized && draft.value.trim() === body) draft.value = "";
		}

		if (staged.length) {
			const uploaded = await store.uploadTo(ticketName.value, staged);
			settleUpload(uploaded);
		}

		await store.loadTickets({ quiet: true });
		await store.loadThread(ticketName.value);
		// Same guard as pollSignal/onFocus/open: advance the watermark only after
		// a successful refetch. Without it, a reply that posts fine but whose
		// follow-up loadThread fails would swallow the change — the user's own
		// reply stays invisible until the next focus-return refetch.
		if (!store.thread.error) lastPrint = store.fingerprintOf(ticketName.value);
	} finally {
		sending.value = false;
	}
}

onMounted(async () => {
	if (!store.tickets.length) await store.loadTickets({ quiet: true });
	await open(ticketName.value);
	timer = setInterval(pollSignal, POLL_MS);
	document.addEventListener("visibilitychange", onFocus);
});

onUnmounted(() => {
	if (timer) clearInterval(timer);
	document.removeEventListener("visibilitychange", onFocus);
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
/* Image variant: a thumbnail instead of the icon+filename pill, same border
   treatment as Message's per-attachment image thumbnail so the two don't look
   like unrelated features. */
.jv-sup-file-img {
	padding: 0;
	border-radius: 8px;
	overflow: hidden;
	line-height: 0;
}
.jv-sup-thumb {
	display: block;
	width: 56px;
	height: 56px;
	object-fit: cover;
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
