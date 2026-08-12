<template>
	<SupportShell
		chat-surface
		:crumbs="[
			{ label: 'Support', route: { name: 'Support' } },
			{ label: row ? row.subject || 'Ticket' : 'Ticket' },
		]"
	>
		<template #actions>
			<!-- ticketStatus falls back to store.thread.meta.status, so a deep-linked
			     ticket outside the newest-50 list (row=null) still shows the correct
			     badge + Resolve instead of a blank header contradicting the panel. -->
			<Badge
				v-if="ticketStatus"
				:variant="badge.variant"
				:theme="badge.theme"
				:label="badge.label"
			/>
			<Button
				v-if="ticketStatus && !store.isClosed(ticketStatus)"
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
				<JvSpinner />
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
				<!-- Centered column, same treatment as chat's jv-thread-inner (just
				     narrower — 820px vs chat's 1280 — to suit a slimmer conversation)
				     so bubbles/rows read as a chat thread instead of stretching
				     full-bleed with the user's replies hugging the far edge. -->
				<div class="jv-sup-thread-inner">
					<!-- Ticket-level fallback only: files a NEW ticket's opening message
					     attaches now ride the opening Communication and render inline
					     in that bubble instead (see SupportNewPage's openingComm). This
					     block only ever fires for a ticket opened before that shipped,
					     or on a Helpdesk build that doesn't auto-mirror the opening
					     message - hence the caption, so a legacy cluster of files reads
					     as "from opening this ticket", not as an unexplained orphan list. -->
					<div v-if="ticketAttachments.length" class="jv-sup-files-wrap">
						<span class="jv-sup-files-label"
							>Attached when this ticket was opened</span
						>
						<div class="jv-sup-files">
							<!-- Same filename-chip treatment as every other attachment (see
							     Message's imagesAsChips) - an image here used to get a bare
							     56px crop with no name at all, worse than the "vague" preview
							     everything else was fixed for. -->
							<a
								v-for="a in ticketAttachments"
								:key="a.name"
								class="jv-sup-file"
								:href="a.file_url"
								target="_blank"
								rel="noopener"
							>
								<FeatherIcon name="paperclip" class="jv-sup-file-clip" />{{
									a.title
								}}
							</a>
						</div>
					</div>

					<!-- Reachable: a brand-new ticket created with an empty body and no
					     files has zero Communications (the initial text is the HD Ticket's
					     `description`, not a reply) — without this, the thread area was
					     just a blank void with no explanation. -->
					<div v-if="!displayMessages.length" class="jv-sup-center">
						<p>This is the start of your conversation.</p>
					</div>

					<template v-for="m in displayMessages" :key="m.key">
						<div v-if="m.dayDivider" class="jv-sup-daydivider">
							<span>{{ m.dayDivider }}</span>
						</div>
						<Message
							:variant="m.variant"
							:html="m.html"
							body-class="jv-html"
							:sender="m.sender"
							:attachments="m.attachments"
							:timestamp="m.timestamp"
							:timestamp-full="m.timestampFull"
							:copyable="false"
							images-as-chips
						>
							<template v-if="m.fromSupport" #avatar>
								<div class="jv-sup-avatar" aria-hidden="true">S</div>
							</template>
						</Message>
					</template>
				</div>
			</template>
		</div>

		<div class="jv-sup-composer">
			<!-- Same 820px column as jv-sup-thread-inner, so the reply box aligns
			     under the conversation instead of stretching full-width. -->
			<div class="jv-sup-composer-inner">
				<!-- Collapse-to-expand reply: a one-line bar that opens the SAME rich
				     composer the new-ticket form uses (rich toolbar, Ctrl/Cmd+Enter to
				     send, Enter = newline). No Stop control — a reply is a single POST;
				     `canSend` disarms Send while sending. Collapses after a clean send. -->
				<SupportReplyBox
					v-model="draft"
					v-model:expanded="replyExpanded"
					:pending="pending"
					:can-submit="canSend"
					:loading="sending"
					submit-label="Send"
					placeholder="Reply to Aerele Support…"
					:disclaimer="disclaimer"
					@submit="send"
					@files-added="(fs) => onFiles(fs)"
					@remove-attachment="removeFile"
				/>
			</div>
		</div>

		<!-- Right details panel (desktop). Its Reply button opens the composer. -->
		<template #aside>
			<SupportTicketPanel @open="replyExpanded = true" />
		</template>
	</SupportShell>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Badge, Button, FeatherIcon, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import Message from "@/components/chat/Message.vue";
import SupportShell from "@/components/support/SupportShell.vue";
import SupportReplyBox from "@/components/support/SupportReplyBox.vue";
import SupportTicketPanel from "@/components/support/SupportTicketPanel.vue";
import { renderSupportHtml } from "@/lib/supportHtml";
import { supportBodyIsEmpty, prepareSupportBody } from "@/lib/supportBody";
import { supportDownloadUrl } from "@/api";
import { useSupportStore } from "@/stores/support";
import { useStagedFiles } from "@/composables/useStagedFiles";
import { formatDate, exactDate, dayLabel } from "@/utils/datetime";

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
// The reply box starts as a collapsed one-line bar; expands on click / the panel's
// Reply button, collapses after a clean send and on a ticket switch.
const replyExpanded = ref(false);

// I4: staged-file / object-URL lifecycle shared with SupportNewPage.
const { files, pending, onFiles, removeFile, snapshotStaged, settleUpload, reset } =
	useStagedFiles();

const ticketName = computed(() => String(route.params.ticket || ""));
const row = computed(() => store.ticketRow(ticketName.value));
// A deep-linked ticket outside the newest-50 list has row=null, but get_thread now
// carries the status via store.thread.meta — so the badge/Resolve/disclaimer read
// the row first, then fall back to the panel meta (fixes: no badge, an unreachable
// Resolve, and no reopen disclaimer on open deep-linked tickets).
const ticketStatus = computed(
	() =>
		(row.value && row.value.status) || (store.thread.meta && store.thread.meta.status) || null
);
const badge = computed(() => store.badgeFor(ticketStatus.value));

// draft is now the editor's HTML (not plain text): a blank TipTap doc is
// "<p></p>", so `.trim()` would be truthy and arm Send on an empty editor —
// gate on supportBodyIsEmpty (strips tags + nbsp) instead. Files alone still arm.
const canSend = computed(
	() => !sending.value && (!supportBodyIsEmpty(draft.value) || files.value.length > 0)
);

// Reopen is reply-driven: there is no reopen endpoint, so the composer stays
// ENABLED on a resolved/closed ticket and says what replying will do. Resolved
// gets its own wording: it's the one state where Resolve (still visible, see
// the header Button's v-if) is ALSO a valid next step, so "Replying reopens
// this ticket" alone reads as contradicting the "Awaiting you" header — this
// spells out both paths instead.
const disclaimer = computed(() => {
	const st = ticketStatus.value;
	if (!st) return "";
	if (st === "Resolved") return "Resolve to confirm and close, or reply to reopen.";
	if (store.isClosed(st)) return "Replying reopens this ticket.";
	return "";
});

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

// Ticket-level attachments (shown above the message list) share classifyAttachment
// with the per-message path below, so `.type` still gets computed here - but the
// template no longer branches on it (every support attachment, ticket-level or
// per-message, renders as a plain paperclip+filename chip now; no image preview).
// Left in rather than forked so ticket-level and per-message attachments can never
// silently classify the same file differently.
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
//
// dayDivider mirrors ChatView's dayDividers computed (UX #23): a label shows
// only on the first message of a new day, timezone-safe via dayLabel. A
// dateless message is skipped WITHOUT resetting the running day (same as
// chat) so it can never split a day group of its own accord.
const displayMessages = computed(() => {
	let prevDay = "";
	return store.thread.messages.map((m) => {
		const support = fromSupport(m);
		const day = dayLabel(m.creation);
		const dayDivider = day && day !== prevDay ? day : "";
		if (day) prevDay = day;
		return {
			key: m.name,
			variant: support ? "row" : "bubble",
			html: renderSupportHtml(m.content, ticketName.value),
			attachments: attachmentsOf(m),
			sender: support ? "Support" : "",
			timestamp: formatDate(m.creation, "h:mm A"),
			timestampFull: exactDate(m.creation),
			fromSupport: support,
			dayDivider,
		};
	});
});

// M10: named for what this actually does — it calls store.closeTicket and the
// server sets status Closed — not the button's visible "Resolve" label.
// "Resolved" is a distinct status in the awaiting-set contract (see the
// store), so the two words are NOT interchangeable. The button keeps saying
// "Resolve" (the customer's intent verb, mockup-blessed), but the toast now
// names the actual resulting state AND pre-answers "can I still reply?" —
// "Ticket resolved" followed a beat later by a "Closed" badge read as two
// different outcomes in one second.
async function closeThisTicket() {
	closing.value = true;
	const ok = await store.closeTicket(ticketName.value);
	if (ok) {
		toast.success("Ticket closed. Reply anytime to reopen it.");
		await store.loadTickets({ quiet: true });
		// Refresh the thread too, so the details panel's status/SLA and the header
		// badge/Resolve reflect Closed immediately instead of lagging 30s to the
		// next poll (they read store.thread.meta, which only get_thread repopulates).
		await store.loadThread(ticketName.value, { quiet: true });
		// Advance the watermark so the next 30s tick doesn't do a redundant refetch
		// (the close moved `modified`, which the fingerprint would read as a change).
		if (!store.thread.error) lastPrint = store.fingerprintOf(ticketName.value);
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
		// The details panel reads store.thread.meta — clear it too, so a stale panel
		// doesn't linger under the new ticket during its fetch (get_thread repopulates it).
		store.thread.meta = null;
		// Composer state is per-TICKET: a draft or staged files left over from the
		// PREVIOUS ticket must not silently ride along and post to this one. Collapse
		// the reply box so the new ticket starts at the one-line bar.
		draft.value = "";
		replyExpanded.value = false;
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
	// Clean the editor HTML here (the host owns the send): strip inline data:/blob:
	// images + DOMPurify. prepareSupportBody returns "" for an empty result, which
	// is how a files-only reply is detected — NOT `.trim()` (truthy for "<p></p>").
	const { body: userBody, stripped } = prepareSupportBody(draft.value);
	if (stripped) {
		toast.info("Embedded images were removed - add them with the Attach button instead.");
	}
	// Snapshot the RAW draft for the reference-safe clear below: the compare must be
	// against what the editor held at send-start (the raw HTML), so text the user
	// keeps typing during the in-flight reply is never wiped.
	const draftSnapshot = draft.value;
	// Snapshot BEFORE the awaited reply/uploadTo below — see useStagedFiles.
	const staged = snapshotStaged();
	// Snapshot the ticket too: `send()` awaits store.reply/uploadTo, and the
	// user can switch tickets (route.params.ticket changes) while a reply is
	// still in flight. Reading ticketName.value AFTER those awaits would then
	// attach this reply's body/files to the NEW ticket — permanent, since
	// there's no un-attach. Every ticket reference below this line uses tName,
	// never ticketName.value.
	const tName = ticketName.value;
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
		// Whether the draft was cleanly cleared (the user didn't retype mid-send) —
		// gates both the reference-safe clear and the reply-box collapse below.
		let cleared = false;
		// The reply's Communication name — attaching the files to it (below) is what makes
		// Helpdesk render them inline; a ticket-level File only shows in the sidebar. Hoisted
		// out of the `if (body)` block so the upload can thread it through.
		let replyComm = "";
		if (body) {
			const ok = await store.reply(tName, body);
			if (!ok) return; // store already toasted; keep the draft so it isn't lost
			// store.reply returns the new Communication's name on success, or `true` when the CP
			// didn't echo one (older CP). Keep only a real string id to thread to the upload —
			// `true` would post a bogus comm the CP would reject.
			if (typeof ok === "string") replyComm = ok;
			// I2: only clear if the editor STILL holds exactly what was at send-start
			// — a blanket clear would wipe text the user kept typing during the
			// in-flight reply. The raw-snapshot compare makes this safe for the
			// synthesized (files-only) case too: if the draft held only stripped
			// inline images (body came back empty), the snapshot still matches and we
			// clear it, so the editor doesn't strand a "removed" image the toast just
			// said was gone; if the user typed during the flight, it differs and we
			// keep it. Setting draft = "" drives SupportComposer's :content watch,
			// which setContent()s the editor empty.
			if (draft.value === draftSnapshot) {
				draft.value = "";
				cleared = true;
			}
		}

		if (staged.length) {
			// Upload only files STILL staged: a chip removed during this in-flight
			// send must not be uploaded (Helpdesk has no un-attach).
			const live = staged.filter((f) => files.value.includes(f));
			if (live.length) {
				// Thread the reply's Communication so each File attaches to it and renders inline.
				// replyComm is "" when there was no reply (never reached here — a files-only send
				// posts a synthesized reply) or when the CP didn't echo one; supportUpload then
				// omits the field and the File attaches to the ticket, as before.
				const uploaded = await store.uploadTo(tName, live, replyComm);
				settleUpload(uploaded);
			}
		}

		await store.loadTickets({ quiet: true });
		// The reply/upload above correctly target tName (the ticket we replied
		// to), but this DISPLAY refresh must not stomp the thread if the user has
		// since navigated to a different ticket — if store.thread.ticket no
		// longer matches tName, that other ticket already loaded itself, so skip
		// the refresh entirely rather than overwrite it with tName's messages
		// under the new ticket's URL/composer. Race-safe: the store's own
		// out-of-order guard covers anything past this synchronous check.
		if (store.thread.ticket === tName) {
			await store.loadThread(tName);
			// Re-check AFTER the await: the user can switch tickets DURING loadThread,
			// so bail before touching this component's watermark/reply state if we've
			// moved on (the store's own out-of-order guard already protected the fetch).
			if (store.thread.ticket !== tName) return;
			// Same guard as pollSignal/onFocus/open: advance the watermark only after
			// a successful refetch. Without it, a reply that posts fine but whose
			// follow-up loadThread fails would swallow the change — the user's own
			// reply stays invisible until the next focus-return refetch.
			if (!store.thread.error) {
				lastPrint = store.fingerprintOf(tName);
				// Collapse the reply box back to the one-line bar only on a fully clean
				// send: still on this ticket (checked above), the refetch didn't error,
				// the draft cleanly cleared AND is still empty NOW (a follow-up typed
				// during the upload/refetch window keeps it open), and no attachments
				// remain to retry.
				if (cleared && !files.value.length && supportBodyIsEmpty(draft.value)) {
					replyExpanded.value = false;
				}
			}
		}
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
}
/* Centered column — the same treatment as ChatView's .jv-thread-inner, just
   narrower (820px vs chat's 1280) to suit a slimmer support conversation.
   gap:24px (was 4px on the old full-width flex) is what stops consecutive
   bubbles/rows from visually fusing into one block. */
.jv-sup-thread-inner {
	max-width: 820px;
	margin: 0 auto;
	padding: 20px 40px 28px;
	display: flex;
	flex-direction: column;
	gap: 24px;
}
.jv-sup-composer {
	flex: 0 0 auto;
	padding: 0 0 16px;
}
/* Same max-width/centering as jv-sup-thread-inner so the reply box aligns
   under the conversation column instead of stretching edge-to-edge. The
   horizontal padding is a deliberate mirror of jv-sup-thread-inner's (40px,
   16px under the mobile breakpoint below) — it used to live on jv-sup-composer
   instead, fixed at 16px with no breakpoint of its own, which put the
   composer's inset out of step with the thread's in the 640-852px band. */
.jv-sup-composer-inner {
	max-width: 820px;
	margin: 0 auto;
	padding: 0 40px;
}
@media (max-width: 640px) {
	.jv-sup-thread-inner {
		padding-left: 16px;
		padding-right: 16px;
	}
	.jv-sup-composer-inner {
		padding-left: 16px;
		padding-right: 16px;
	}
}
.jv-sup-daydivider {
	display: flex;
	align-items: center;
	justify-content: center;
	margin: 6px 0 2px;
}
.jv-sup-daydivider span {
	font-size: 11px;
	font-weight: 550;
	color: var(--text-3);
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 999px;
	padding: 2px 10px;
}
.jv-sup-center {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 10px;
	padding: 48px 0;
	color: var(--text-2);
}
.jv-sup-files-wrap {
	margin-bottom: 12px;
}
.jv-sup-files-label {
	display: block;
	margin-bottom: 6px;
	font-size: 11.5px;
	color: var(--text-3);
}
.jv-sup-files {
	display: flex;
	flex-wrap: wrap;
	gap: 8px;
}
/* Same shape as a per-message attachment chip (Message.vue's .jv-file-chip) -
   radius, padding, font-size, icon size all match, so a file attached at
   ticket-open time and one attached in a reply read as the same kind of
   object, not two different ones a few pixels apart. */
.jv-sup-file {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	padding: 6px 11px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-2);
	color: var(--link);
	font-size: 12.5px;
	line-height: 1.3;
	text-decoration: none;
	overflow-wrap: anywhere;
}
.jv-sup-file:hover {
	text-decoration: underline;
}
.jv-sup-file-clip {
	flex: 0 0 auto;
	width: 13px;
	height: 13px;
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
