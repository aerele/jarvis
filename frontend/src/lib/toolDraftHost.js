// The desktop chat's decisions about a server draft card (a tool draft item), kept pure so
// they run under vitest as data tables. ChatView.vue only wires them: no Vue, no DOM, no
// API. Relative imports only.
import { markMissing } from "./docFields.js";
import { draftCardValues } from "./draftCardValues.js";
import { draftFromCard } from "./draftFromCard.js";
import { draftSaveOutcome } from "./draftSave.js";
import { markMissingRows } from "./draftTables.js";
import { cardOfItem, isToolDraftItem, toolDraftCtx } from "./toolDraft.js";
import {
	CANT_EDIT_TEXT,
	STORED_UNAVAILABLE_TEXT,
	FROM_CARD,
	computedFromCard,
	prepareToolModel,
} from "./toolDraftPanel.js";

// One card always: while the pending list holds a tool draft item, no typed draft is live.
// `items` is the list the screen draws (visiblePendingActions), so another chat's card never
// counts. A sensitive card is not a tool draft item and does not count.
export function hasParkedDraft(items) {
	return Array.isArray(items) && items.some(isToolDraftItem);
}

// What DraftCard is told about an item. `editingToken`: the token whose edit form is open (the
// card then shows "Editing"); `building`: this card's form or preview is being built.
// `position` / `count` / `stackHint` say where the card sits among the pending cards on screen
// (see draftCardView).
export function toolDraftHostCtx({
	pa,
	streaming,
	position,
	count,
	stackHint,
	editingToken = "",
	building = false,
}) {
	return {
		...toolDraftCtx({ pa, editingToken, streaming, building }),
		position,
		count,
		stackHint,
	};
}

// The ctx of every server draft card in the visible stack, keyed by token. `items` is the whole
// stack (legacy cards included: they take a number too, so a draft card's "2 of 3" is real) and
// `stackHint` is the stack's own hint. A computed map in the view keeps each ctx object stable
// between renders; a draft card's view only recomputes when its own inputs change.
export function toolDraftCtxMap({
	items,
	streaming,
	stackHint,
	editingToken = "",
	buildingToken = "",
}) {
	const list = Array.isArray(items) ? items : [];
	const map = {};
	list.forEach((pa, i) => {
		if (isToolDraftItem(pa))
			map[pa.token] = toolDraftHostCtx({
				pa,
				streaming,
				position: i + 1,
				count: list.length,
				stackHint,
				editingToken,
				building: !!buildingToken && buildingToken === pa.token,
			});
	});
	return map;
}

// After Create: should the chat be reloaded to show the receipt? Yes when the card has left
// the pending list, this chat is still the one on screen, and no decided row for the token is
// loaded yet. A refused Create that spends the card (a record that changed after the draft was
// made, State S10) removes the card without a reload, so "Not saved: ..." would not show.
export function receiptReloadNeeded({ token, conversation, currentConversation, items, rows }) {
	if (!token || !conversation || conversation !== currentConversation) return false;
	if ((items || []).some((x) => x && x.token === token)) return false;
	return !(rows || []).some(
		(m) => m && m.role === "tool" && m.tool_call_id === token && !!m.action_outcome
	);
}

// Run one card action (Create -> the confirm handler, Cancel -> the discard handler) with
// the item's `busyKind` set, so the card shows the working state with the spinner on the
// pressed button, and a second press on the same card does nothing while one is in flight.
// The kind is cleared in a `finally`: a refused, failed or thrown action leaves the card
// usable again; a successful one has already taken the item off the list. Resolves to true
// when `run` was started. The handlers keep their own `busy` / `inflightTokens` guards.
export async function withBusyKind(pa, kind, run) {
	if (!pa || pa.busy || pa.busyKind) return false;
	pa.busyKind = kind;
	try {
		await run(pa);
		return true;
	} finally {
		pa.busyKind = "";
	}
}

// Shown inside a typed edit panel that a server draft card has overtaken.
export const DRAFT_STALE_TEXT =
	"This draft changed while you were editing. Your edits were not saved.";

// The typed edit panel's "stale" flag. A panel is stale once a tool draft item is parked
// while it is open, and stays stale until it closes (the card that retired the typed draft
// may itself be decided later, but the panel's save has no draft to act on by then). A panel
// that is a different object from the last one starts from the current parked state.
// This is the TYPED panel's rule: a panel that edits a server draft card itself (commit 3) is
// not stale because a draft is parked, so its host must not feed this reducer.
//   state  = the flag now
//   change = { panelOpen, panelChanged, parked }
export function nextPanelStale(state, { panelOpen, panelChanged, parked }) {
	if (!panelOpen) return false;
	return (panelChanged ? false : !!state) || !!parked;
}

// ---- the edit form and preview of a server draft card ------------------------------------
//
// ChatView hands these a `d` object of its own state and effects (so they run under vitest with
// fakes). The form is the ONE `draftPanel` the typed draft uses, stamped `tool: true` with the
// card's token; it is shown only after build -> snapshot -> guard (prepareToolModel).
//   d.find(token, conversation) -> the pending item or undefined
//   d.capture(token) -> view;  d.pendingCurrent(view), d.chatCurrent(view) -> bool
//   d.panel() -> the open form;  d.setPanel(model);  d.closePanel()
//   d.build(action) -> model | undefined (ChatView's buildDraftModel);  d.autoOpen(model)
//   d.building() / d.setBuilding(token);  d.notify(text, opts);  d.warn(...args)
//   d.streaming() -> bool;  d.inflight (a Set);  d.removePending(token)
//   d.reload(conversation), d.refreshChats(), d.showQueued(reply)
//   d.save(token, conversation, values) -> reply;  d.agent() -> the assistant's name
//   d.thrown(error, fallback) -> {message};  d.metaFields(doctype) -> form meta fields
//   d.afterMarks() -> after the DOM caught up, scroll to and focus the first mark
//   d.create(item) -> Create on the card (the confirm handler under busyKind create)
//   d.discard(item) -> Cancel on the card (the discard handler under busyKind cancel), true if started

const UNREADABLE = "Couldn't open this draft. Ask Jarvis to send it again.";
const COULDNT_LOAD = "Could not load this draft. Try again.";
const GONE = "This draft is no longer waiting. Look at the chat for what happened.";

// The open form of a server draft card whose card is no longer pending, and that is not itself
// saving or cancelling it: the person is editing a draft that was replaced, confirmed or
// discarded elsewhere. `items` is the pending list the screen draws.
export function toolPanelDead(panel, items) {
	if (!panel || !panel.tool || panel.applying) return false;
	return !(items || []).some(
		(x) => x && x.token === panel.token && x.conversation === panel.conversation
	);
}

// The lines above the form, in order: what to tell the person and in which style.
export function toolPanelNotices(panel, dead) {
	if (!panel || !panel.tool) return [];
	const out = [];
	if (dead) out.push({ type: "error", text: DRAFT_STALE_TEXT });
	if (panel.saveBlocked) out.push({ type: "error", text: CANT_EDIT_TEXT });
	if (panel.storedUnavailable) out.push({ type: "warning", text: STORED_UNAVAILABLE_TEXT });
	return out;
}

// Build the model of a card's draft for the form or the preview. null when it cannot be shown
// (the person is told why). Order: the model is built (form meta, stored record, rule values),
// then snapshotted and guarded at once (prepareToolModel), before anything can be shown.
async function buildFor(pa, d) {
	const card = cardOfItem(pa);
	const action = draftFromCard(card, pa.token);
	if (!action) {
		d.notify(UNREADABLE, { type: "error" });
		return null;
	}
	const view = d.capture(pa.token);
	if (pa.conversation !== view.conversation || !d.pendingCurrent(view) || d.building())
		return null;
	d.setBuilding(pa.token);
	try {
		let model;
		try {
			// the stored record is optional here: without it the form shows the draft's own cells
			model = await d.build({ ...action, [FROM_CARD]: true });
		} catch (e) {
			if (d.chatCurrent(view))
				d.notify((e && e.jvUserMessage) || COULDNT_LOAD, { type: "error" });
			return null;
		}
		if (!d.chatCurrent(view)) return null;
		if (!model) {
			d.notify(COULDNT_LOAD, { type: "error" });
			return null;
		}
		if (!d.find(pa.token, view.conversation)) {
			d.notify(GONE, { type: "error" });
			return null;
		}
		return {
			...prepareToolModel(model, { token: pa.token, conversation: pa.conversation, card }),
			card,
		};
	} finally {
		d.setBuilding("");
	}
}

// Edit: build, then show the form. Returns "opened", "ignored" (already open) or "failed".
export async function openToolDraftPanel(pa, d) {
	const open = d.panel();
	if (open && open.tool && open.token === pa.token) return "ignored";
	const built = await buildFor(pa, d);
	if (!built) return "failed";
	// an unreadable-by-the-guard form still opens, read-only in effect: Save is blocked
	if (!built.guard.ok)
		d.warn("Jarvis card: this draft form does not match its card", built.guard.why);
	// An update's changed rows open by themselves. That only writes display state, never a value.
	try {
		d.autoOpen(built.model);
	} catch (e) {
		d.warn("Jarvis card: could not auto-open the changed rows", e);
	}
	d.setPanel(built.model);
	return "opened";
}

// Preview: build the same model, fill its calculated cells from the CARD's own rows, then show it.
export async function openToolPreview(pa, d) {
	const built = await buildFor(pa, d);
	if (!built) return "failed";
	built.model.computed = computedFromCard(built.model, built.card);
	d.setPreview({ token: pa.token, model: built.model });
	return "opened";
}

// What a settled save does on screen. Close the form BEFORE taking the card off the list, so the
// form is never seen on a dead token during its own save.
async function settleSave(p, view, r, outcome, d) {
	const closeForm = () => {
		if (d.panel() === p) d.closePanel();
	};
	if (outcome.kind === "keep") {
		// fixable: the form stays, marked; busy and "could not be read" have no fields
		p.error = outcome.error;
		// A mark on a TABLE (the server's "keep a saved row" refusal names the table) has no field of
		// its own in the form: never let markMissing add the table as a field. The banner carries it.
		const mainFields = (d.metaFields(p.doctype) || []).filter((f) => f.fieldtype !== "Table");
		markMissing(p, p.error.fields, mainFields);
		markMissingRows(p, p.error.fields);
		d.afterMarks();
		return "keep";
	}
	if (outcome.kind === "closed" && outcome.cardOpen) {
		// the card is still pending and nothing was sent to the chat
		closeForm();
		d.notify(outcome.note, { type: "error" });
		return "closed-open";
	}
	closeForm();
	if (outcome.kind === "closed") d.notify(outcome.note, { type: "error" });
	d.removePending(p.token);
	await d.reload(view.conversation);
	if (!d.chatCurrent(view)) return "stale";
	d.refreshChats();
	d.showQueued(r);
	return outcome.kind === "closed" ? "closed" : "saved";
}

// Nothing was changed in the form: Save is plain Create. `{}` through save_draft_card would be
// the card as drafted too (values == {} is exactly the sealed call) but the server records that
// path as "saved with edited values"; Create leaves the ordinary receipt.
async function createFromForm(p, pa, view, d) {
	p.applying = true;
	p.error = null;
	try {
		await d.create(pa);
	} finally {
		p.applying = false;
	}
	if (!d.chatCurrent(view) || d.panel() !== p) return "stale";
	const still = d.find(p.token, view.conversation);
	if (!still) {
		d.closePanel();
		return "created";
	}
	p.error = still.error || null;
	return "kept";
}

// Save in the form. Sends only what the person changed (draftCardValues); nothing changed is
// Create. The item is busy ("save") and in flight for the whole call, so a second press, the
// card's own buttons and the pending-list resync leave it alone. Resolves to what happened:
// "ignored" | "stale" | "created" | "kept" | "saved" | "keep" | "closed-open" | "closed" | "error".
export async function saveToolDraft(p, d) {
	const pa = p && p.tool ? d.find(p.token, p.conversation) : undefined;
	if (!pa || p.applying || p.saveBlocked || pa.busy || pa.busyKind || d.streaming())
		return "ignored";
	const view = d.capture(p.token);
	if (pa.conversation !== view.conversation || !d.pendingCurrent(view)) return "ignored";
	let values;
	try {
		values = draftCardValues(p, p.snapshot, p.card);
	} catch (e) {
		p.error = { message: "Could not save your changes." };
		return "error";
	}
	if (!Object.keys(values).length) return createFromForm(p, pa, view, d);
	p.applying = true;
	p.error = null;
	pa.busy = true;
	pa.busyKind = "save";
	d.inflight.add(p.token);
	try {
		const r = await d.save(p.token, view.conversation, values);
		if (!d.pendingCurrent(view)) return "stale";
		return await settleSave(p, view, r, draftSaveOutcome(r, d.agent()), d);
	} catch (e) {
		if (!d.pendingCurrent(view)) return "stale";
		p.error = d.thrown(e, "Could not save your changes.");
		return "error";
	} finally {
		if (d.chatCurrent(view)) {
			const item = d.find(p.token, view.conversation);
			if (item) {
				item.busy = false;
				item.busyKind = "";
			}
			d.inflight.delete(p.token);
		}
		p.applying = false;
	}
}

// Cancel on the card, from the card's button or the form's Discard. With the form open on this
// card the form is "applying" while the discard runs (so it is not seen on a dead token), then
// it closes when the card is gone, or shows the card's error when the discard was refused.
export async function cancelToolItem(pa, d) {
	const open = d.panel();
	const p = open && open.tool && open.token === pa.token ? open : null;
	if (p) {
		if (p.applying) return "ignored";
		p.applying = true;
		p.error = null;
	}
	const view = d.capture(pa.token);
	let started;
	try {
		started = await d.discard(pa);
	} finally {
		if (p) p.applying = false;
	}
	if (!started) return "ignored";
	if (!p) return "cancelled";
	if (!d.chatCurrent(view) || d.panel() !== p) return "stale";
	const still = d.find(pa.token, view.conversation);
	if (!still) {
		d.closePanel();
		return "cancelled";
	}
	p.error = still.error || null;
	return "kept";
}

// The form's Discard.
export function discardFromForm(p, d) {
	const pa = p && p.tool ? d.find(p.token, p.conversation) : undefined;
	return pa ? cancelToolItem(pa, d) : Promise.resolve("ignored");
}
