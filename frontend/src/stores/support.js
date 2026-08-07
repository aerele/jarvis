// Support state — a module-scope singleton, house style (no pinia), mirroring
// stores/shell.js. The three support pages AND the shell's resting badge all
// read this one instance, so the badge and the list can never disagree.
//
// Every action catches and toasts. The page this replaces had zero catch blocks,
// so a failed call showed the user nothing at all — that is the defect being fixed.
import { reactive, ref } from "vue";
import { toast } from "frappe-ui";
import { errMessage as errMsg, errHtml, escapeHtml } from "@/lib/errors";
import {
	supportListTickets,
	supportGetThread,
	supportCreateTicket,
	supportReply,
	supportCloseTicket,
	supportAwaitingCount,
	supportUpload,
} from "@/api";

// Mirrors jarvis_helpdesk/setup/install.py:39 AWAITING_STATUSES and
// jarvis_admin_v2/support/awaiting.py:10 AWAITING — both ("Replied", "Resolved").
// These are the only statuses meaning "the ball is in the customer's court".
const AWAITING = new Set(["Replied", "Resolved"]);

export function isAwaiting(status) {
	return AWAITING.has(status);
}
export function isClosed(status) {
	return status === "Closed";
}

// Catch-all on purpose: Helpdesk ships Paused today and can add statuses without
// a frontend deploy, so anything unrecognised reads as Open rather than blank.
// `theme` is a frappe-ui Badge theme, so every status pill in the app is drawn
// by the design system rather than hand-rolled colours. This also retires the
// spec's AA-contrast workaround: the failure was raw --amber on --amber-bg, and
// Badge's own token pairs are the system's answer to exactly that.
export function badgeFor(status) {
	if (isAwaiting(status)) return { label: "Awaiting you", theme: "orange" };
	if (isClosed(status)) return { label: "Closed", theme: "gray" };
	return { label: "Open", theme: "blue" };
}

const tickets = ref([]);
const ticketsLoading = ref(false);
const ticketsError = ref("");
const awaitingCount = ref(0);

const thread = reactive({
	// The NAME of the ticket currently loaded/loading — not the row (read row
	// data via ticketRow()/fingerprintOf()). This is the "which ticket is this"
	// marker SupportThreadPage compares against before a fetch, to tell a real
	// ticket switch apart from a same-ticket refresh (quiet poll/retry) and
	// reset messages/attachments/error before a different ticket's fetch
	// starts. Must always be a name string, never a row object — see loadThread.
	ticket: null,
	messages: [],
	attachments: [],
	// Ticket-details metadata ({name, subject, status, priority, agent_group, SLA
	// fields, creation}) for the thread's right-side panel — carried by get_thread
	// (not the list row, which is null for deep-linked tickets outside the newest 50).
	meta: null,
	loading: false,
	error: "",
});

async function loadTickets({ quiet = false } = {}) {
	if (!quiet) ticketsLoading.value = true;
	try {
		const r = await supportListTickets();
		tickets.value = (r && r.data && r.data.tickets) || [];
		ticketsError.value = "";
	} catch (e) {
		// Keep the last-good rows on screen (useListPage does the same) — a blank
		// list would read as "you have no tickets", which is a lie.
		ticketsError.value = errMsg(e);
		if (!quiet) toast.error(ticketsError.value);
	} finally {
		ticketsLoading.value = false;
	}
}

async function loadThread(name, { quiet = false } = {}) {
	if (!name) return;
	// Set BEFORE the fetch, and re-check it AFTER: the ticket name is the
	// monotonic request key here (same idea as useListPage's reqId counter,
	// but the name is already the natural "which request is this" identity).
	// Two loadThread calls can overlap (user opens T1, backs out, opens T2
	// while T1's multi-second bench→CP→Helpdesk round-trip is still in
	// flight) and resolve out of order — without the re-check below, a T1
	// response landing AFTER T2's would overwrite T2's conversation with
	// T1's under T2's title.
	thread.ticket = name;
	if (!quiet) thread.loading = true;
	try {
		const r = await supportGetThread(name);
		if (thread.ticket !== name) return; // superseded by a later loadThread()
		const d = (r && r.data) || {};
		thread.messages = d.messages || [];
		thread.attachments = d.ticket_attachments || [];
		thread.meta = d.ticket_meta || null;
		thread.error = "";
	} catch (e) {
		if (thread.ticket !== name) return; // superseded by a later loadThread()
		thread.error = errMsg(e);
		if (!quiet) toast.error(thread.error);
	} finally {
		// Only THIS call's own ticket may clear loading — a superseded call's
		// finally must not stomp the flag the newer call is still using.
		if (thread.ticket === name) thread.loading = false;
	}
}

// The ticket row for `name`, or null. Pages read subject/status from here rather
// than refetching — list_tickets already carries them.
function ticketRow(name) {
	return tickets.value.find((t) => t.name === name) || null;
}

// A cheap change signal for the open thread. Deliberately fingerprints the WHOLE
// row rather than reading `modified`: the control plane's payload is not defined
// in this repo, so `modified` may or may not be there. Stringifying the row picks
// it up when present and still catches a status flip when it is not.
function fingerprintOf(name) {
	const row = ticketRow(name);
	return row ? JSON.stringify(row) : "";
}

async function refreshAwaiting() {
	try {
		const r = await supportAwaitingCount();
		awaitingCount.value = (r && r.data && r.data.count) || 0;
	} catch (e) {
		// Best-effort: this drives an ambient badge, so a failure must never
		// interrupt whatever the user is actually doing. Intentionally silent.
	}
}

async function createTicket(subject, body) {
	try {
		const r = await supportCreateTicket(subject, body);
		const name = r && r.data && r.data.ticket;
		if (!name) {
			// A 200 with no ticket name is a malformed success — the caller's
			// "the store toasted" contract must hold, so surface it here rather
			// than returning null silently (spinner stops, nothing happens).
			toast.error("The ticket couldn't be created. Please try again.");
			return null;
		}
		return name;
	} catch (e) {
		toast.error(errHtml(e));
		return null;
	}
}

async function reply(name, body) {
	try {
		const r = await supportReply(name, body);
		// Return the new Communication's name so the caller can thread a files-only/attachment
		// upload to it — attaching a File to the Communication is what makes it render inline.
		// Fall back to `true` when the CP didn't echo one, so existing truthiness-checking
		// callers (the `if (!ok) return` guard in send()) still read this as success.
		return (r && r.data && r.data.comm) || true;
	} catch (e) {
		toast.error(errHtml(e));
		return false;
	}
}

async function closeTicket(name) {
	try {
		await supportCloseTicket(name);
		return true;
	} catch (e) {
		toast.error(errHtml(e));
		return false;
	}
}

// Uploads happen AFTER the ticket exists — media.upload takes a ticket name and
// attaches server-side immediately, with no un-attach endpoint. Files are
// uploaded one at a time so a single failure doesn't discard the rest. Returns
// the File REFERENCES that actually succeeded (not a count) — the caller
// (useStagedFiles's settleUpload) needs to know exactly which ones landed so a
// retry re-sends only the failures, never the ones already attached.
async function uploadTo(name, files, comm) {
	const succeeded = [];
	for (const f of files || []) {
		try {
			await supportUpload(name, f, comm);
			succeeded.push(f);
		} catch (e) {
			// Toast binds `message` with v-html, so the FILENAME is a sink here
			// too: it is user-chosen and reaches this string unescaped (#699).
			toast.error(`Couldn't attach ${escapeHtml(f.name)}: ${errHtml(e)}`);
		}
	}
	return succeeded;
}

const store = reactive({
	tickets,
	ticketsLoading,
	ticketsError,
	awaitingCount,
	thread,
	badgeFor,
	isAwaiting,
	isClosed,
	ticketRow,
	fingerprintOf,
	loadTickets,
	loadThread,
	refreshAwaiting,
	createTicket,
	reply,
	closeTicket,
	uploadTo,
});

export function useSupportStore() {
	return store;
}
