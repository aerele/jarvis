// Maintenance hold, delivered by the www/jarvis.py boot payload as
// window.maintenance = { active, message }. The sibling of noticeGate.js, with one
// deliberate difference: this state is REACTIVE, not a page-lifetime constant. A
// release nudge is true for the whole session; an upgrade hold is raised and
// cleared WHILE the customer is mid-chat, so the banner, the avatar's "upgrading"
// mood and the send refusal must all be able to flip live.
//
// It is a PURE TOGGLE (no TTL, no tier) already resolved on the control plane
// (tenant -> host -> cell) with the fleet-wide kill-switch applied; this module
// only decides whether to show the "back shortly" banner and with what text, and
// re-checks the CP so an open tab lifts the hold promptly when the roll clears it.
import { computed, ref } from "vue";
import { call } from "frappe-ui";
import { holdShouldShow, holdMessage, nextNotice } from "@/maintenanceHold";
import { brand } from "@/branding";

const boot = window.maintenance || {};
const notice = ref({ active: !!boot.active, message: (boot.message || "").trim() });

// True while the upgrade hold is up. Single source of truth for the top-of-chat
// banner, the presence avatar's "upgrading" mood, and the send refusal.
export const holdActive = computed(() => holdShouldShow(notice.value));

// The banner / refusal sentence, white-label aware: brand.agentName comes from branding
// (the operator's custom message, already brand-scrubbed server-side, wins when set).
export const holdText = computed(() => holdMessage(notice.value, brand.agentName));

export const rechecking = ref(false);

// Raise the hold locally from a server refusal. If the customer already had the tab
// open when the operator set the hold, boot never carried it; the first blocked send
// comes back with reason "maintenance", and ChatView calls this so the banner + mood
// appear without a reload. The server refusal carries only a reason today (no message),
// so `message` is normally empty and the boot-seeded / branded-default copy is kept; a
// future server that attaches a pre-scrubbed message would surface it here.
export function raiseHold(message) {
	const msg = (message || "").trim();
	notice.value = { active: true, message: msg || notice.value.message };
	_startPoll();
}

// Clear the hold locally. Called when a send is ACCEPTED - which proves the CP-side
// gate passed, i.e. the roll has finished - so the banner + avatar lift immediately
// instead of waiting for a reload or the next poll. Idempotent.
export function clearHold() {
	if (notice.value.active) notice.value = { active: false, message: "" };
	_stopPoll();
}

// Re-pull the hold from the control plane. Keeps the last-known notice on any error or
// malformed shape (via nextNotice) - a failed poll must never flip a live hold off.
export async function recheck() {
	if (rechecking.value) return holdActive.value;
	rechecking.value = true;
	try {
		const fresh = await call("jarvis.maintenance_notice.check");
		notice.value = nextNotice(notice.value, fresh);
	} catch (e) {
		/* keep last-known: a failed poll must not clear a live hold */
	} finally {
		rechecking.value = false;
	}
	// Keep the poll in lockstep with the actual state: a recheck usually CLEARS the
	// hold (stop polling), but folding in a genuinely-new CP hold could re-raise it, so
	// (re)start rather than only ever stopping.
	if (holdActive.value) _startPoll();
	else _stopPoll();
	return holdActive.value;
}

// While a hold is up, re-check the CP on a slow cadence so an IDLE tab (one that never
// sends again after the roll finishes) still lifts the banner. The send path clears it
// instantly for an active user (clearHold); this covers the rest. Mirrors the release
// gate's 60s poll (UpdateNoticeGate.vue). Runs only while held: started on raise / boot,
// stopped on clear.
const POLL_MS = 60000;
let _pollTimer = null;
function _startPoll() {
	if (_pollTimer || typeof setInterval !== "function") return;
	_pollTimer = setInterval(() => {
		recheck();
	}, POLL_MS);
}
function _stopPoll() {
	if (_pollTimer) {
		clearInterval(_pollTimer);
		_pollTimer = null;
	}
}
if (notice.value.active) _startPoll();
