// The phone's host for the shared server draft card: the decisions that are the phone's own
// (where the state lives, what to reload) around the shared libs in frontend/src/lib. Relative
// imports only. It imports toolDraftHost.js, whose import chain `node --test` cannot resolve, so
// this file is tested by vitest (frontend/src/components/PwaDraftCard.spec.js); the pure parts
// are in draftCardDecision.js and draftCardUi.js, which node tests run.
import { cardOfItem, isToolDraftItem } from "../../../frontend/src/lib/toolDraft.js";
import {
	hasParkedDraft,
	orphanStackHint,
	toolDraftCtxMap,
	withBusyKind,
} from "../../../frontend/src/lib/toolDraftHost.js";
import { cancelOutcome, createOutcome, thrownOutcome } from "./draftCardDecision.js";

export { cardOfItem, isToolDraftItem };

// What the chat draws for its pending list: each draft card's ctx (by token), the stack's typed
// hint line, and whether a draft card is parked (the one-card rule).
//   items       the pending list in the order it is shown (the numbers follow this order)
//   stackHint   the phone's hint for the whole stack (typedCardReply.typedApprovalHint)
//   ui          the token-keyed store (draftCardUi.makeDraftUi)
// Preview and Edit are drawn disabled (`readOnly`) until their sheets exist; this is the one
// place that stops passing it.
export function phoneDraftModel({ items, streaming, stackHint, ui }) {
	const list = Array.isArray(items) ? items : [];
	const ctxMap = toolDraftCtxMap({
		items: list,
		streaming,
		stackHint,
		surface: "phone",
		readBusy: ui.where.read,
		readOnly: true,
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
//           banner), reload() (the conversation), reloadPending(), agent }
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
			let outcome;
			try {
				const reply =
					kind === "create"
						? await d.confirm(token, conversation)
						: await d.dismiss(token, conversation);
				outcome =
					kind === "create"
						? createOutcome(reply, { agent: d.agent })
						: cancelOutcome(reply);
			} catch (e) {
				outcome = thrownOutcome(kind, e);
			}
			if (!d.current()) {
				result = "stale";
				return;
			}
			if (!outcome.gone) {
				d.ui.setError(token, outcome.error);
				result = "kept";
				return;
			}
			d.removePending(token);
			d.settle(token);
			d.ui.forget(token);
			if (outcome.say) d.say(outcome.say);
			// The decision is final and starts no agent turn, so nothing else will bring the receipt:
			// reload the chat, then the pending list (it reads the reloaded rows).
			try {
				await d.reload();
				if (d.current()) await d.reloadPending();
			} catch {
				/* the write already happened; a failed read must not turn it into an error line */
			}
			result = d.current() ? "gone" : "stale";
		},
		d.ui.where
	);
	return started ? result : "ignored";
}
