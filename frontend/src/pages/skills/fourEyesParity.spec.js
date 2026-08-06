import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

/**
 * Four-eyes parity between the Review page and the two server endpoints that
 * actually enforce it.
 *
 * The rule: a reviewer may not decide their own promotion request. Both servers
 * spell it `reviewer == req.owner and reviewer != "Administrator"`, so the
 * break-glass account is deliberately exempt.
 *
 * The client had the first half and not the carve-out. That is a silent failure
 * mode rather than a loud one: the page disabled Approve/Reject on a request the
 * server would have accepted, so an Administrator-owned request could not be
 * decided from the UI at all and there was no error to explain why. These tests
 * pin BOTH sides, because a client gate stricter than its server is still a bug.
 */

const APP = path.resolve(__dirname, "../../../..");
const read = (p) => fs.readFileSync(path.join(APP, p), "utf8");

const reviewTab = read("frontend/src/pages/skills/ReviewTab.vue");
const wikiPy = read("jarvis/chat/wiki.py");
const skillsPy = read("jarvis/chat/custom_skills_api.py");

describe("four-eyes: client gate mirrors the server rule", () => {
	it("the client exempts Administrator before comparing the requester", () => {
		const fn = reviewTab.slice(reviewTab.indexOf("function isMyPromo(p) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toContain('session.user === "Administrator"');
		expect(body).toContain("p.requested_by === session.user");
	});

	it("both servers carry the same Administrator carve-out", () => {
		// If either server ever drops the carve-out, the client must drop it too
		// or it becomes the LENIENT side and users get a 403 on a live button.
		const rule = 'reviewer == (req.owner or "") and reviewer != "Administrator"';
		expect(wikiPy).toContain(rule);
		expect(skillsPy).toContain(rule);
	});

	it("the gate still refuses a non-Administrator deciding their own request", () => {
		// The carve-out must not swallow the rule it is an exception to.
		const fn = reviewTab.slice(reviewTab.indexOf("function isMyPromo(p) {"));
		const body = fn.slice(0, fn.indexOf("\n}"));
		expect(body).toMatch(
			/return\s+!!p\s+&&\s+!!p\.requested_by\s+&&\s+p\.requested_by === session\.user;/
		);
	});

	it("Approve, Reject and the explanation all hang off the same gate", () => {
		// Three call sites per promotion kind. If one drifts, a user sees an
		// enabled button next to "another reviewer must decide it".
		const uses = reviewTab.match(/isMyPromo\(p\)/g) || [];
		expect(uses.length).toBeGreaterThanOrEqual(6);
	});
});
