// Where the live draft's card is drawn. The draft can sit several messages up; its
// card is drawn under the LAST assistant message so it is always at the bottom,
// while Create / Save / Cancel keep using the DRAFT row's own name.
import { liveDraftRow } from "../../../frontend/src/lib/liveDraft.js";
import { parseAction, actionBlockState } from "./blocks.js";

/**
 * @param {Array<object>} rows the conversation's messages in order
 * @param {Array<object>} items the thread items built from them
 * @returns {{anchorKey: string, draftKey: string, action: object|null, bad: boolean}|null}
 */
export function draftAnchor(rows, items) {
	const draft = liveDraftRow(rows);
	if (!draft) return null;
	let anchor = null;
	for (let i = (items || []).length - 1; i >= 0; i--) {
		if (items[i].type === "assistant" && items[i].msg) {
			anchor = items[i];
			break;
		}
	}
	if (!anchor) return null;
	return {
		anchorKey: anchor.key,
		draftKey: draft.name,
		action: parseAction(draft.content),
		bad: actionBlockState(draft.content) === "bad",
	};
}
