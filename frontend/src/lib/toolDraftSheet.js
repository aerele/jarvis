// What the edit form of a server draft card decides besides the doors in
// toolDraftHost.js: the footer's buttons, when a close must ask first, how often the pending list
// is re-read while a form is open, and the wiring of the calculated cells. Pure (Vue's `watch` is
// handed in), relative imports only, so the phone's host shares it.
//
// The desktop's ChatView keeps its own text for the footer expressions, the poll and the watcher
// (specs cut that text out of the view and run it, so it cannot move without them): the spec
// toolDraftSheet.spec.js runs the desktop's text and these functions on the same inputs, so the two
// cannot drift.
import { draftIsDirty } from "./draftSave.js";
import { settleCalculatedCells, toolPrimaryLabel } from "./toolDraftPanel.js";

// While a form is open its card can be replaced, confirmed or cancelled elsewhere: the pending
// list is read again this often, so the dead-draft banner shows without waiting for a focus event.
export const TOOL_FORM_POLL_MS = 5000;

// The words under Save while a turn runs.
export const WAITING_FOR_REPLY = "Waiting for the current reply to finish";

// The cells the card shows and the draft left blank follow the person's edits (see
// settleCalculatedCells). The deep watcher's source: the form's tables, or null when this is not a
// server draft's form or it has no filled cell, so nothing is walked for it.
export function calcCellsSource(panel) {
	return panel && panel.tool && Array.isArray(panel.calc) && panel.calc.length
		? panel.tables
		: null;
}

// Wire it: `watch` is Vue's, `getPanel()` returns the open form model (or null).
export function watchCalculatedCells(watch, getPanel) {
	return watch(
		() => calcCellsSource(getPanel()),
		(tables) => {
			if (tables) settleCalculatedCells(getPanel());
		},
		{ deep: true }
	);
}

// The footer of the form: the primary button's words and whether each button is off. `dead`: the
// card is no longer pending (toolPanelDead); `streaming`: a turn is running.
export function toolFooterState(panel, { dead, streaming }) {
	const busy = !!(panel && panel.applying);
	return {
		primaryLabel: busy ? "Saving…" : toolPrimaryLabel(panel),
		primaryDisabled: busy || !!(panel && panel.saveBlocked) || !!dead || !!streaming,
		discardDisabled: busy || !!dead,
		primaryTitle: streaming ? WAITING_FOR_REPLY : "",
	};
}

// Closing a form whose edits would be lost asks first. Not while it saves (the edits are being
// sent), and not when the card is dead (the banner has said the edits were not saved and nothing
// can save them: the person only needs to get out).
export function closeAsksFirst(panel, { dead }) {
	return !!panel && !!panel.tool && !panel.applying && !dead && draftIsDirty(panel);
}
