import { test } from "node:test";
import assert from "node:assert/strict";
import { draftAnchor } from "./draftAnchor.js";

const fence = (body) => "```jarvis-action\n" + body + "\n```";
const GOOD = fence(
	JSON.stringify({ doctype: "ToDo", verb: "create", values: { description: "x" } })
);
const GOOD2 = fence(
	JSON.stringify({ doctype: "ToDo", verb: "create", values: { description: "y" } })
);
const BAD = fence("{not json");

const asst = (name, content) => ({ name, role: "assistant", content });
const user = (name) => ({ name, role: "user", content: "hi" });
const tool = (name, outcome = "") => ({
	name,
	role: "tool",
	content: "",
	action_outcome: outcome,
});

// Mirrors ChatView's items: tool rows hang off the assistant item before them.
function itemsOf(rows) {
	const out = [];
	let current = null;
	for (const m of rows) {
		if (m.role === "user") {
			current = null;
			out.push({ type: "user", key: m.name, msg: m });
		} else if (m.role === "tool") {
			if (!current) {
				current = { type: "assistant", key: m.name, msg: null, tools: [] };
				out.push(current);
			}
			current.tools.push(m);
		} else {
			current = { type: "assistant", key: m.name, msg: m, tools: [] };
			out.push(current);
		}
	}
	return out;
}
const run = (rows) => draftAnchor(rows, itemsOf(rows));

test("the draft is the last assistant: the anchor is the draft", () => {
	const r = run([user("u1"), asst("a1", GOOD)]);
	assert.equal(r.anchorKey, "a1");
	assert.equal(r.draftKey, "a1");
	assert.equal(r.bad, false);
	assert.equal(r.action.verb, "create");
});

test("draft then chat: anchor is the last assistant, draftKey is the draft", () => {
	const r = run([asst("a1", GOOD), user("u1"), asst("a2", "plain answer")]);
	assert.equal(r.anchorKey, "a2");
	assert.equal(r.draftKey, "a1");
	assert.equal(r.action.doctype, "ToDo");
});

test("a receipt after the draft retires it", () => {
	assert.equal(run([asst("a1", GOOD), tool("t1", "confirmed")]), null);
	assert.equal(run([asst("a1", GOOD), tool("t1", "discarded")]), null);
});

test("a newer draft replaces the older one", () => {
	const r = run([asst("a1", GOOD), user("u1"), asst("a2", GOOD2)]);
	assert.equal(r.draftKey, "a2");
	assert.equal(r.action.values.description, "y");
});

test("a broken draft: bad true, action null", () => {
	const r = run([asst("a1", BAD)]);
	assert.equal(r.bad, true);
	assert.equal(r.action, null);
	assert.equal(r.draftKey, "a1");
});

test("a newer broken draft hides the older good one", () => {
	const r = run([asst("a1", GOOD), user("u1"), asst("a2", BAD)]);
	assert.equal(r.draftKey, "a2");
	assert.equal(r.bad, true);
});

test("six user rows later: no live draft", () => {
	const rows = [asst("a1", GOOD)];
	for (let i = 0; i < 6; i++) rows.push(user("u" + i), asst("b" + i, "ok"));
	assert.equal(run(rows), null);
});

test("tool rows after the last assistant do not move the anchor", () => {
	const r = run([asst("a1", GOOD), user("u1"), asst("a2", "text"), tool("t1"), tool("t2")]);
	assert.equal(r.anchorKey, "a2");
	assert.equal(r.draftKey, "a1");
});

test("no draft and empty input give null", () => {
	assert.equal(run([asst("a1", "hello")]), null);
	assert.equal(draftAnchor([], []), null);
});

test("an anchor with no assistant message gives null", () => {
	const rows = [asst("a1", GOOD)];
	assert.equal(draftAnchor(rows, [{ type: "user", key: "u1", msg: {} }]), null);
});

// One card at the tail, as on the desktop: the last message's own block that is not a
// draft (an email, a gated verb, an unknown verb) is the card there; a kept draft hides.
const EMAIL = fence(JSON.stringify({ kind: "email", to: "a@x", subject: "S", body: "B" }));
const SUBMIT = fence(JSON.stringify({ doctype: "ToDo", verb: "submit", name: "T1" }));
const UNKNOWN = fence(JSON.stringify({ doctype: "ToDo", verb: "frobnicate" }));

test("a tail with its own non-draft block hides the kept draft", () => {
	for (const [own, check] of [
		[SUBMIT, (a) => assert.equal(a.verb, "submit")],
		[EMAIL, (a) => assert.equal(a.kind, "email")],
		[UNKNOWN, (a) => assert.equal(a.verb, "frobnicate")],
	]) {
		const r = run([asst("a1", GOOD), user("u1"), asst("a2", own)]);
		assert.equal(r.own, true);
		assert.equal(r.anchorKey, "a2");
		assert.equal(r.draftKey, "a2", "keyed by its own message, never the kept draft");
		assert.equal(r.bad, false);
		check(r.action);
	}
});

test("a tail's own email block is its card even with no draft", () => {
	const r = run([user("u1"), asst("a1", EMAIL)]);
	assert.equal(r.own, true);
	assert.equal(r.action.kind, "email");
});

test("a kept or tail draft is not an own card", () => {
	assert.equal(run([user("u1"), asst("a1", GOOD)]).own, false);
	assert.equal(run([asst("a1", GOOD), user("u1"), asst("a2", "plain answer")]).own, false);
	assert.equal(run([asst("a1", GOOD), user("u1"), asst("a2", BAD)]).own, false);
});

test("an own block in an older message does not hide the kept draft at the tail", () => {
	const r = run([asst("a0", EMAIL), user("u0"), asst("a1", GOOD), user("u1"), asst("a2", "ok")]);
	assert.equal(r.own, false);
	assert.equal(r.draftKey, "a1");
});

// Break: the phone ignores the parked-draft signal, so a typed ActionCard and a server DraftCard
// show together (two cards for one change). Without the signal nothing changes.
test("a pending server draft card means no typed draft card is live", () => {
	const rows = [user("u1"), asst("a1", GOOD)];
	assert.equal(draftAnchor(rows, itemsOf(rows), { parkedDraft: true }), null);
	assert.equal(draftAnchor(rows, itemsOf(rows), { parkedDraft: false }).draftKey, "a1");
	assert.equal(draftAnchor(rows, itemsOf(rows), {}).draftKey, "a1");
});
