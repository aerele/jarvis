// Which card is drawn at the tail, under the LAST assistant message, and keyed by what.
// One card at the tail, as on the desktop (frontend/src/lib/tailCard.js):
// - the last message's own action block that is not a draft (an email, a gated verb, an
//   unknown verb) is the card there, keyed by that message (`own`); a kept draft from an
//   earlier message is hidden behind it until a newer reply lands;
// - otherwise the live draft's card. The draft can sit several messages up; its card is
//   still drawn under the last assistant message so it is always at the bottom, while
//   Create / Save / Cancel keep using the DRAFT row's own name.
import { liveDraftRow, isOwnActionRow } from "../../../frontend/src/lib/liveDraft.js";
import { parseAction, actionBlockState } from "./blocks.js";

/**
 * @param {Array<object>} rows the conversation's messages in order
 * @param {Array<object>} items the thread items built from them
 * @param {{parkedDraft?: boolean}} [options] `parkedDraft`: a server draft card is pending, so it
 *   is the one card and no typed draft is live (liveDraft.js liveDraftRow)
 * @returns {{anchorKey: string, draftKey: string, action: object|null, bad: boolean, own: boolean}|null}
 */
export function draftAnchor(rows, items, { parkedDraft = false } = {}) {
	let anchor = null;
	for (let i = (items || []).length - 1; i >= 0; i--) {
		if (items[i].type === "assistant" && items[i].msg) {
			anchor = items[i];
			break;
		}
	}
	if (!anchor) return null;
	if (isOwnActionRow(anchor.msg)) {
		return {
			anchorKey: anchor.key,
			draftKey: anchor.msg.name,
			action: parseAction(anchor.msg.content),
			bad: false,
			own: true,
		};
	}
	const draft = liveDraftRow(rows, { parkedDraft });
	if (!draft) return null;
	return {
		anchorKey: anchor.key,
		draftKey: draft.name,
		action: parseAction(draft.content),
		bad: actionBlockState(draft.content) === "bad",
		own: false,
	};
}
