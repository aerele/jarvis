// The phone's host for the shared server draft card: the decisions that are the phone's own
// (where the state lives, what to reload) around the shared libs in frontend/src/lib. Relative
// imports only. It imports toolDraftHost.js, whose import chain `node --test` cannot resolve, so
// this file is tested by vitest (frontend/src/components/PwaDraftCard.spec.js); the pure parts
// are draftCardUi.js (node tests run it) and the shared decision in chatCardActions.js.
import { cardOfItem, isToolDraftItem } from "../../../frontend/src/lib/toolDraft.js";
import {
	hasParkedDraft,
	orphanStackHint,
	toolDraftCtxMap,
	withBusyKind,
} from "../../../frontend/src/lib/toolDraftHost.js";
import { pendingExpiry } from "../../../frontend/src/lib/actionSummary.js";
import {
	confirmReplyOutcome,
	dismissReplyOutcome,
	thrownCardError,
} from "../../../frontend/src/lib/chatCardActions.js";

// The line under a card when the server's own words are missing (a storage reply with no message).
export const ACTION_FAILED_TEXT = "This action could not be completed.";

export { cardOfItem, isToolDraftItem };

// What the chat draws for its pending list: each draft card's ctx (by token), the stack's typed
// hint line, and whether a draft card is parked (the one-card rule).
//   items       the pending list in the order it is shown (the numbers follow this order)
//   stackHint   the phone's hint for the whole stack (typedCardReply.typedApprovalHint)
//   ui          the token-keyed store (draftCardUi.makeDraftUi)
//   editingToken  the card whose edit sheet is open (it shows "Editing")
//   buildingToken the card whose sheet is being built (every draft card is locked meanwhile)
export function phoneDraftModel({
	items,
	streaming,
	stackHint,
	ui,
	editingToken = "",
	buildingToken = "",
}) {
	const list = Array.isArray(items) ? items : [];
	const ctxMap = toolDraftCtxMap({
		items: list,
		streaming,
		stackHint,
		surface: "phone",
		readBusy: ui.where.read,
		editingToken,
		buildingToken,
	});
	const last = list[list.length - 1];
	// The legacy tiles have no hint of their own here: the phone draws the stack hint under the
	// stack. When the last card is a draft card it draws that hint itself (in S1 and S2), so the
	// line under the stack is only what the card left undrawn. With no draft card at all the line
	// is the stack hint, exactly as before.
	const hintLine =
		last && isToolDraftItem(last)
			? orphanStackHint({ items: list, ctxMap, stackHint })
			: stackHint || "";
	return { ctxMap, hintLine, parked: hasParkedDraft(list) };
}

// loadPending replaces the list on every read. A card whose Create or Cancel is in flight stays
// in it, whatever the read says: the server may already have spent it, and the reply (not a read
// that raced it) decides what happens to the card. Returns `next` itself when nothing is added.
export function keepBusyItems(next, previous, busyTokens) {
	const busy = new Set(busyTokens || []);
	if (!busy.size) return next;
	const have = new Set((next || []).map((c) => c && c.token));
	const kept = (previous || []).filter((c) => c && busy.has(c.token) && !have.has(c.token));
	return kept.length ? [...(next || []), ...kept] : next;
}

// Create or Cancel on a card.
//   kind  "create" (confirm_tool) or "cancel" (dismiss_tool)
//   d     { ui, confirm(token, conversation), dismiss(token, conversation), streaming() -> bool,
//           current() -> bool (this chat is still the one on screen), removePending(token),
//           settle(token) (keep a stale read from putting the card back), say(text) (the chat's
//           banner), reload() (the conversation), reloadPending(), receiptShown(token) (the
//           reloaded rows hold a decided row for the card), showQueued({run_id, position}) (a
//           continuation turn is waiting for a slot) }
// What a reply means is the shared decision (frontend/src/lib/chatCardActions.js, the one the
// desktop's handlers apply): a card is kept with the server's words, or gone and the chat and the
// pending list are reloaded. A thrown call says the shared sanitised text, never the raw error.
// Resolves to "ignored" (a second press, the turn is running, no token), "stale" (the person left
// the chat), "kept" (the card stays, usable again, with an error line) or "gone" (the card is
// spent, both lists were reloaded). The busy kind is held for the whole call and cleared at the
// end whatever happened.
export async function runDraftDecision(kind, pa, d) {
	if (!pa || !pa.token || d.streaming()) return "ignored";
	const { token, conversation } = pa;
	let result = "ignored";
	const started = await withBusyKind(
		pa,
		kind,
		async () => {
			d.ui.setError(token, "");
			let out;
			try {
				const reply =
					kind === "create"
						? await d.confirm(token, conversation)
						: await d.dismiss(token, conversation);
				out =
					kind === "create"
						? confirmReplyOutcome(reply, {
								draft: true,
								expired: pendingExpiry(pa.expires_at, Date.now()).expired,
						  })
						: dismissReplyOutcome(reply);
			} catch (e) {
				out = {
					fate: "kept",
					message: thrownCardError(e, kind === "create" ? "confirm" : "dismiss").message,
				};
			}
			if (!d.current()) {
				result = "stale";
				return;
			}
			if (out.fate === "kept") {
				d.ui.setError(token, out.message || ACTION_FAILED_TEXT);
				result = "kept";
				return;
			}
			d.removePending(token);
			d.settle(token);
			d.ui.forget(token);
			// a draft that continues the chat: its turn is waiting for a slot (the queued card, as after a send)
			if (out.queued) d.showQueued(out.queued);
			// The decision is final and starts no agent turn, so nothing else will bring the receipt:
			// reload the chat, then the pending list (it reads the reloaded rows).
			try {
				await d.reload();
				if (d.current()) await d.reloadPending();
			} catch {
				/* the write already happened; a failed read must not turn it into an error line */
			}
			// A card spent with no receipt of its own to say why (it was handled elsewhere, its token was
			// used) gets one line in the chat's banner. When the reload brought its receipt, the receipt
			// is the line (State Table: receipt only), so the banner would say it twice.
			if (d.current() && out.message && !d.receiptShown(token)) d.say(out.message);
			result = d.current() ? "gone" : "stale";
		},
		d.ui.where
	);
	return started ? result : "ignored";
}
