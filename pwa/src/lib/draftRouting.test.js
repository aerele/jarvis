import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const view = fs.readFileSync(new URL("../views/ChatView.vue", import.meta.url), "utf8");
const squash = (t) => t.replace(/\s+/g, " ").trim();
const tag = (open, from = 0) => {
	const at = view.indexOf(open, from);
	assert.ok(at >= 0, `${open} is in ChatView.vue`);
	return view.slice(at, view.indexOf("/>", at) + 2);
};

// LEGACY GUARD. A pending item that is not a server draft card keeps the amber tile and its
// sheet exactly as before the phone drew draft cards; with the site flag off no item is a draft
// card, so nothing on the phone may change. This stores the whitespace-squashed markup of the
// legacy branch. Before the phone drew draft cards the tile was
// `<DecisionCard v-for="(p, pi) in orderedPending" :key="p.token" ...rest...>`; the rest below is
// that markup unchanged, and `v-else` replaces the loop attributes that moved to the <template>.
// TO CHANGE IT ON PURPOSE: edit the legacy markup, run this file, copy the squashed text printed in
// the failure into the constant, and say in the commit why the legacy tile changed.
const LEGACY_TILE =
	'<DecisionCard v-else :summary=" (orderedPending.length > 1 ? `${pi + 1} of ${orderedPending.length}: ` : \'\') + (p.summary || p.tool || `${agentName} needs your approval`) " :earlier="!isRecentCard(p)" :proposed="proposedLabel(p.created_at, cardClock)" @open="openDecision(p)" />';
const LEGACY_NOTE =
	'<p v-if="showOlderCardsNote" class="jv-typehint"> An earlier action card is still waiting. Tap it to approve or discard. </p>';
const LEGACY_SHEET =
	'<DecisionSheet :key="`${decisionContext?.generation ?? viewGeneration}:${ decisionContext?.conversation ?? \'\' }:${decisionContext?.token ?? \'\'}`" :action="decision" :view-generation="decisionContext?.generation ?? viewGeneration" :streaming="sending" @close="onDecisionClose" @resolved="onResolved" @settled="onDecisionSettled" @busy="onDecisionBusy" />';

// Break: the amber tile, its note or its sheet changes for a pending item that is not a draft card
// (a sensitive card, a submit, anything with the flag off).
test("the legacy tile, its note and its sheet are unchanged", () => {
	assert.equal(squash(tag("<DecisionCard")), LEGACY_TILE);
	const note = view.indexOf('<p v-if="showOlderCardsNote"');
	assert.equal(squash(view.slice(note, view.indexOf("</p>", note) + 4)), LEGACY_NOTE);
	assert.equal(squash(tag("<DecisionSheet")), LEGACY_SHEET);
});

// Break: a draft item draws the tile too (two controls for one card), or a non-draft item is
// drawn by the draft card. The route is ONE v-if / v-else on the item, inside the one loop.
test("one loop over the shown order: a draft item is the inline card, everything else the tile", () => {
	assert.equal(view.split('v-for="(p, pi) in orderedPending"').length - 1, 1);
	const loop = view.slice(view.indexOf('<template v-for="(p, pi) in orderedPending"'));
	const draft = loop.indexOf('<div v-if="isToolDraftItem(p)"');
	const tile = loop.indexOf("<DecisionCard\n");
	assert.ok(
		draft > 0 && tile > draft,
		"the v-if draft branch comes first, the tile is its v-else"
	);
	assert.match(loop.slice(tile, tile + 40), /<DecisionCard\s+v-else/);
	assert.match(loop.slice(draft, tile), /class="jv-tool-draft jv-draft-tokens"/);
	assert.match(loop.slice(draft, tile), /:ctx="draftModel\.ctxMap\[p\.token\]"/);
	// At this commit only Create and Cancel are wired; Preview and Edit are drawn disabled.
	assert.match(loop.slice(draft, tile), /@create="decideDraft\('create', p\)"/);
	assert.match(loop.slice(draft, tile), /@cancel="decideDraft\('cancel', p\)"/);
	assert.doesNotMatch(loop.slice(draft, tile), /@preview|@edit/);
});

// P5. A typed "confirm 2" binds to the token sitting at position 2 of `approval_tokens`
// (jarvis/chat/api.py _typed_confirmation: client_tokens[i]). The card numbers drawn on screen
// are the position in `orderedPending`; the send path passes exactly that list in that order. A
// draft card is an item of the same list, so its "confirm N" hint names the record it shows.
// Break: the send path takes the tokens from another list or order than the one the cards are
// numbered from (a typed "confirm 2" would approve a different record).
test("typed approval sends the tokens in the order the cards are shown and numbered", () => {
	assert.match(
		view,
		/sendRecovery\.stage\(\s*cid,\s*input\.value,\s*ready,\s*orderedPending\.value\.map\(\(p\) => p\.token\)\s*\)/
	);
	assert.match(view, /items: orderedPending\.value,/); // the draft cards' numbers
	assert.match(view, /`\$\{pi \+ 1\} of \$\{orderedPending\.length\}: `/); // the tiles' numbers
});

// Break: the view stops passing one of the three signals that make the draft card safe on the
// phone: the one-card flag, the in-flight keep in loadPending, the reset on a chat switch. The
// libs are tested on their own; only the view can lose the wiring.
test("the view wires the one-card flag, the in-flight keep and the chat-switch reset", () => {
	assert.match(
		view,
		/draftAnchor\(messages\.value, items\.value, \{ parkedDraft: draftModel\.value\.parked \}\)/
	);
	const load = view.slice(view.indexOf("async function loadPending("));
	assert.match(
		load.slice(0, load.indexOf("pending.value = next;")),
		/keepBusyItems\([\s\S]*pending\.value,\s*draftUi\.busyTokens\(\)/
	);
	const sw = view.slice(view.indexOf("() => props.id,"));
	assert.match(sw.slice(0, sw.indexOf("\n);")), /draftUi\.reset\(\);/);
});

// Break: a draft card asks the person to confirm in a window.confirm (the card's Cancel asks
// nothing more than the desktop's does); only the legacy sheet keeps its own.
test("the draft card's handlers never call window.confirm", () => {
	const at = view.indexOf("function decideDraft(");
	assert.ok(at > 0);
	assert.doesNotMatch(view.slice(at, view.indexOf("\n}\n", at)), /confirm\(/);
});
