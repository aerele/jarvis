// Static guard for jarvis#823: the chat turn error card's Retry button must
// stay gated on the server's `retryable` flag.
//
// The defect #823 fixed was a Retry button rendered on EVERY turn failure
// except `cancelled`, so a revoked key, a model that does not exist and an
// exhausted quota each offered an action that could not possibly work. The fix
// lives in ONE `v-if` in a 10,000-line template, which is exactly the kind of
// line a later edit drops without noticing: the button would simply come back,
// no test would fail, and the regression would only ever surface as a customer
// clicking Retry forever.
//
// So this is a source-scanning ratchet, in the same spirit as
// src/lib/noInlineErrorFormatter.test.js. It cannot prove the button renders
// correctly - lib/errors.test.js owns the taxonomy behaviour, and a real
// browser pass owns the visual - but it does prove the gate is still WIRED,
// which is the half that silently rots.
import fs from "node:fs";
import path from "node:path";

const CHAT_VIEW = path.join(process.cwd(), "src/views/ChatView.vue");

describe("jarvis#823: the turn error card's Retry gate", () => {
	const src = fs.readFileSync(CHAT_VIEW, "utf8");

	it("renders the Retry button only when the failure is retryable", () => {
		// The button is identified by its own class, so this does not depend on
		// the surrounding markup staying byte-identical.
		const retryButton = src.indexOf('class="jv-retry"');
		expect(retryButton, "the turn error card's Retry button went missing").toBeGreaterThan(-1);
		// The gate sits on the same element, immediately before the class.
		const window = src.slice(Math.max(0, retryButton - 200), retryButton + 200);
		expect(
			window,
			"Retry must be gated on errorInfo(m).retryable - an ungated Retry is the #823 defect"
		).toMatch(/v-if="errorInfo\(m\)\.retryable"/);
	});

	it("offers a corrective action where a terminal failure has one", () => {
		// Book rule 8: a customer who cannot retry must still be given one next
		// step. The alternative branch is what carries it.
		expect(src).toMatch(/v-else-if="errorInfo\(m\)\.action"/);
		expect(src).toMatch(/runErrorAction\(errorInfo\(m\)\.action\)/);
	});

	it("reads the persisted envelope, not just the live event", () => {
		// Persistence parity: the reload path must use the row's own code and
		// retryable, or a refresh re-guesses from the error string and can
		// contradict the card the customer just read.
		expect(src).toMatch(/m\.error_code/);
		expect(src).toMatch(/retryable: !!m\.error_retryable/);
	});
});
