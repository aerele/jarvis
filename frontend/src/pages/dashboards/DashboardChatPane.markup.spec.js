// The pane's parked-card strip is a v-if / v-else pair per card: a server draft card (flag
// jarvis_draft_cards) gets its title and one Review in full chat button; every other card keeps
// the markup it had before drafts existed. With the flag off no card is a draft, so nothing here
// may change.
import { describe, expect, it } from "vitest";
import crypto from "crypto";
import fs from "fs";
import path from "path";

const src = fs.readFileSync(path.resolve(__dirname, "DashboardChatPane.vue"), "utf8");

describe("DashboardChatPane parked-card markup", () => {
	// the opening tag, however prettier wraps its attributes
	const OPEN =
		/<div\s+v-else\s+class="flex flex-col gap-2 rounded-md p-3 ring-1 ring-outline-gray-modals"\s*>/;
	const found = OPEN.exec(src);
	const start = found ? found.index : -1;
	const bodyStart = found ? start + found[0].length : -1;
	const end = src.indexOf("</template>", start);

	// LEGACY GUARD. The inner markup of the non-draft card (title, meta line, Approve, Dismiss),
	// with every whitespace character removed so prettier's re-wrapping does not count, is what the
	// pane drew before it knew about drafts (proved equal to HEAD fca3f67fe: 486 characters).
	// TO CHANGE THE LEGACY CARD ON PURPOSE: edit it, run this spec, copy the new length and hash
	// from the failure into LEGACY_INNER_*, and say in the commit why the card changed.
	const LEGACY_INNER_SHA256 = "51bebb022e5cc8f1fe2a222f459a75bb0290fd6a9328c45ec200845a60bcb3de";
	const LEGACY_INNER_LENGTH = 486;

	// Break: the card of an ordinary item (a plain confirmation, a sensitive card, any item with the
	// flag off) changes: its title source, meta line, buttons, labels or handlers.
	it("the legacy card's inner markup is exactly what it was before drafts", () => {
		expect(start, "the v-else card is in DashboardChatPane.vue").toBeGreaterThan(-1);
		const body = src.slice(bodyStart, end);
		const inner = body.slice(0, body.lastIndexOf("</div>")).replace(/\s+/g, "");
		expect([inner.length, crypto.createHash("sha256").update(inner).digest("hex")]).toEqual([
			LEGACY_INNER_LENGTH,
			LEGACY_INNER_SHA256,
		]);
	});

	// Break: the draft card gets a second route to a decision (an Approve, a Dismiss, a handler that
	// confirms or discards), or it stops being the v-if of the ONE pair inside the one loop.
	it("the draft branch is the v-if of the pair, with no way to decide it", () => {
		expect(src.split('v-for="pa in orderedCards"').length - 1).toBe(1);
		const draft = src.slice(src.indexOf('v-if="isDraftPendingItem(pa)"'), start);
		expect(draft.length).toBeGreaterThan(0);
		for (const gone of [
			"approve(",
			"dismiss(",
			"confirmTool",
			"dismissTool",
			"Approve",
			"Dismiss",
		]) {
			expect(draft, gone).not.toContain(gone);
		}
		expect(draft).toContain('label="Review in full chat"');
		expect(draft).toContain("draftLineOfItem(pa)");
	});
});
