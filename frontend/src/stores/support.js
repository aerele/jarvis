// Support state — a module-scope singleton, house style (no pinia), mirroring
// stores/shell.js. The three support pages AND the shell's resting badge all
// read this one instance, so the badge and the list can never disagree.
//
// Every action catches and toasts. The page this replaces had zero catch blocks,
// so a failed call showed the user nothing at all — that is the defect being fixed.
import { reactive, ref } from "vue";
import { toast } from "frappe-ui";
import {
	supportListTickets,
	supportGetThread,
	supportCreateTicket,
	supportReply,
	supportCloseTicket,
	supportAwaitingCount,
	supportUpload,
} from "@/api";

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

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
	if (isAwaiting(status)) return { label: "Awaiting you", tone: "awaiting", theme: "orange" };
	if (isClosed(status)) return { label: "Closed", tone: "closed", theme: "gray" };
	return { label: "Open", tone: "open", theme: "blue" };
}

const tickets = ref([]);
const ticketsLoading = ref(false);
const ticketsError = ref("");
const awaitingCount = ref(0);

const thread = reactive({
	ticket: null, // the ticket ROW (name/subject/status), not just the id
	messages: [],
	attachments: [],
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
	if (!quiet) thread.loading = true;
	try {
		const r = await supportGetThread(name);
		const d = (r && r.data) || {};
		thread.messages = d.messages || [];
		thread.attachments = d.ticket_attachments || [];
		thread.error = "";
	} catch (e) {
		thread.error = errMsg(e);
		if (!quiet) toast.error(thread.error);
	} finally {
		thread.loading = false;
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
		return (r && r.data && r.data.ticket) || null;
	} catch (e) {
		toast.error(errMsg(e));
		return null;
	}
}

async function reply(name, body) {
	try {
		await supportReply(name, body);
		return true;
	} catch (e) {
		toast.error(errMsg(e));
		return false;
	}
}

async function closeTicket(name) {
	try {
		await supportCloseTicket(name);
		return true;
	} catch (e) {
		toast.error(errMsg(e));
		return false;
	}
}

// Uploads happen AFTER the ticket exists — media.upload takes a ticket name and
// attaches server-side immediately, with no un-attach endpoint. Files are
// uploaded one at a time so a single failure doesn't discard the rest.
async function uploadTo(name, files) {
	let done = 0;
	for (const f of files || []) {
		try {
			await supportUpload(name, f);
			done += 1;
		} catch (e) {
			toast.error(`Couldn't attach ${f.name}: ${errMsg(e)}`);
		}
	}
	return done;
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
