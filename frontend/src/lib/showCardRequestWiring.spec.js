import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * Layered re-check phase 2 — the typed "show it" fast-path, wired into send().
 *
 * The phrase detector itself is unit-tested in showCardRequest.spec.js. What a
 * unit test can NOT see is whether send() calls it with the right guards and,
 * crucially, whether it clears the composer only when a card actually surfaced.
 * Get that wrong and a false positive silently eats the user's message. There is
 * no vitest harness for the ChatView send() handler, so — like typedConfirm.spec
 * and pendingConfirmResync.spec — this pins the wiring at the source level.
 */

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

// The one condition that opens the typed fast-path. Slice the block from it so
// every ordering assertion below is scoped to THIS branch, not the first
// same-named token anywhere in a 9k-line file.
const COND =
	"if (fromMain && currentId.value && !pendingFiles.value.length && isShowCardRequest(text)) {";
const condAt = src.indexOf(COND);
const typedBlock = src.slice(condAt, condAt + 700);

describe("send() wires the typed 'show it' fast-path", () => {
	it("imports the shared detector rather than inlining a phrase list", () => {
		// One detector, three surfaces (SPA/PWA/widget) — an inline copy here would
		// drift from the tested one.
		expect(src).toContain('import { isShowCardRequest } from "@/lib/showCardRequest";');
	});

	it("opens the fast-path only from the main composer", () => {
		// resend/prefill sends set fromMain=false; a typed re-check there would
		// hijack a retry of a real message.
		expect(condAt).toBeGreaterThan(-1);
		expect(COND).toContain("fromMain");
	});

	it("routes the re-surface through the single shared choke-point, tagged typed", () => {
		// Every layer (auto/pill/menu/typed) goes through resyncPendingConfirmations
		// so they all inherit its ok:false-keeps-queue + freshness posture. No layer
		// does its own merge.
		expect(typedBlock).toContain('resyncPendingConfirmations(_typedConv, "typed")');
	});

	it("seeds durable rows before the server resync, so a rendered card is instant", () => {
		expect(typedBlock).toContain("seedPendingFromRows(messages.value, _typedConv)");
		expect(typedBlock.indexOf("seedPendingFromRows(messages.value, _typedConv)")).toBeLessThan(
			typedBlock.indexOf('resyncPendingConfirmations(_typedConv, "typed")')
		);
	});
});

describe("the fast-path swallows the message ONLY when a card actually surfaced", () => {
	// The whole reason a false positive is safe: if nothing surfaced, the phrase
	// falls through to a normal send and the persona backstop still handles it.
	const SURFACED_GUARD =
		"if (chatViewIsCurrent(_typedView) && visiblePendingActions.value.length > before) {";

	it("guards the swallow on a real increase in visible cards", () => {
		expect(typedBlock).toContain("const before = visiblePendingActions.value.length;");
		expect(typedBlock).toContain(SURFACED_GUARD);
	});

	it("clears the composer INSIDE the surfaced-check, never before it", () => {
		// This is the bug the reviewer flagged: input.value = "" above the guard
		// would eat every typed message that merely looked like a re-check.
		const guardAt = typedBlock.indexOf(SURFACED_GUARD);
		const clearAt = typedBlock.indexOf('input.value = "";');
		expect(guardAt).toBeGreaterThan(-1);
		expect(clearAt).toBeGreaterThan(guardAt);
	});
});

describe("a kept draft card at the tail is shown once per draft, never silently", () => {
	const block = src.slice(condAt, condAt + 2000);
	const GUARD =
		"if (chatViewIsCurrent(_typedView) && shownDraft && !draftsShownOnRequest.has(shownDraft)) {";

	it("only when the card at the tail is the kept draft's", () => {
		// an own card of the last message (email, gated or unknown verb) is not it
		expect(block).toContain(
			'const shownDraft = _tailCard.value === "draft" ? _liveDraft.value?.name : "";'
		);
		expect(block).not.toContain("actionFor.value || draftBlockErrorText.value");
	});

	it("once per draft, says so, and scrolls to the card itself", () => {
		const at = block.indexOf(GUARD);
		expect(at).toBeGreaterThan(-1);
		const branch = block.slice(at, at + 400);
		expect(branch).toContain("draftsShownOnRequest.add(shownDraft);");
		expect(branch).toContain('input.value = "";');
		expect(branch).toContain('notify("Your draft is at the bottom.", { type: "info" });');
		expect(branch).toContain("scrollDraftCardIntoView();");
		expect(src).toContain(
			'const el = threadEl.value?.querySelector(".jv-action.jv-summary");'
		);
	});
});
