// AskCard, the ONE renderer for the agent's ```jarvis-ask blocks (extracted
// from ChatView so the Dashboards builder pane shows the same cards instead of
// deleting the block into an empty bubble). chatAsk.test.js proves the parsing
// and answer formatting headlessly; this drives the CARDS - picking, toggling,
// the Other/pick exclusivity, and what the Submit button finally emits.
import { expect, test, vi } from "vitest";

// AskCard's link-type questions search records through @/api, whose frappe-ui
// resources chain does not resolve under vitest. Only searchLink is used here.
vi.mock("@/api", () => ({ searchLink: vi.fn(async () => []) }));

import AskCard from "../../src/components/chat/AskCard.vue";
import { parseAsk } from "../../src/lib/chatAsk.js";
import { mountWithPalette } from "./fixtures.js";

const spec = (json) => parseAsk("Lead-in.\n\n```jarvis-ask\n" + json + "\n```");
const CHOICES = spec(
	'[{"q":"Which records?","type":"single","options":["Open","Closed"]},' +
		'{"q":"Send it?","type":"yesno","options":["Approve","Reject"]}]'
);
const findCard = (w) => w.findComponent(AskCard);
const optionNamed = (w, label) =>
	w.findAll("button.jv-ask-opt").find((b) => b.text().includes(label));
const submit = (w) => w.find("button.jv-ask-submit");

test("renders one question block per question, with its options", () => {
	const w = mountWithPalette(AskCard, { spec: CHOICES });
	expect(w.findAll(".jv-ask-q")).toHaveLength(2);
	expect(w.text()).toContain("Which records?");
	// a yesno with two custom labels uses them instead of Yes/No
	expect(w.text()).toContain("Approve");
	expect(w.text()).toContain("Reject");
	expect(w.text()).not.toContain("Yes");
});

test("Submit is disabled with a hint until every question is answered", async () => {
	const w = mountWithPalette(AskCard, { spec: CHOICES });
	expect(submit(w).attributes("disabled")).toBeDefined();
	expect(w.text()).toContain("Answer each question to continue");

	await optionNamed(w, "Open").trigger("click");
	expect(submit(w).attributes("disabled")).toBeDefined();

	await optionNamed(w, "Approve").trigger("click");
	expect(submit(w).attributes("disabled")).toBeUndefined();
	expect(w.text()).not.toContain("Answer each question to continue");
});

test("Submit emits the formatted answers once", async () => {
	const w = mountWithPalette(AskCard, { spec: CHOICES });
	await optionNamed(w, "Open").trigger("click");
	await optionNamed(w, "Approve").trigger("click");
	await submit(w).trigger("click");
	const emitted = findCard(w).emitted("submit");
	expect(emitted).toHaveLength(1);
	expect(emitted[0][0]).toBe(
		"Here are my answers:\n1. Which records? → Open\n2. Send it? → Approve"
	);
});

test("the picks survive Submit, so a host that refuses the text loses nothing", async () => {
	// DashboardChatPane.sendText and ChatView.send both return early while a send
	// is in flight (or a dictation is still transcribing). Clearing sel/other
	// inside submit() emptied the card anyway - the answers were gone and the
	// button was disabled again. The card unmounts on the next assistant message
	// (it is keyed by message name), so there is nothing to reset here.
	const w = mountWithPalette(AskCard, { spec: CHOICES });
	await optionNamed(w, "Open").trigger("click");
	await optionNamed(w, "Approve").trigger("click");
	await submit(w).trigger("click");
	expect(optionNamed(w, "Open").classes()).toContain("on");
	expect(submit(w).attributes("disabled")).toBeUndefined();
	// ...and a second click re-sends the same answers rather than an empty one
	await submit(w).trigger("click");
	const emitted = findCard(w).emitted("submit");
	expect(emitted).toHaveLength(2);
	expect(emitted[1][0]).toBe(emitted[0][0]);
});

test("clicking a picked option unpicks it (and disarms Submit)", async () => {
	const w = mountWithPalette(AskCard, { spec: CHOICES });
	await optionNamed(w, "Open").trigger("click");
	await optionNamed(w, "Approve").trigger("click");
	expect(optionNamed(w, "Open").classes()).toContain("on");
	await optionNamed(w, "Open").trigger("click");
	expect(optionNamed(w, "Open").classes()).not.toContain("on");
	expect(submit(w).attributes("disabled")).toBeDefined();
});

test('typing "Other…" clears the pick on a single-answer question', async () => {
	const w = mountWithPalette(AskCard, { spec: CHOICES });
	await optionNamed(w, "Open").trigger("click");
	const other = w.findAll("input.jv-ask-other")[0];
	await other.setValue("only the escalated ones");
	expect(optionNamed(w, "Open").classes()).not.toContain("on");
	await optionNamed(w, "Approve").trigger("click");
	await submit(w).trigger("click");
	// the typed answer replaces the pick - never both
	expect(findCard(w).emitted("submit")[0][0]).toContain(
		"1. Which records? → only the escalated ones"
	);
});

test("multi keeps several picks and adds the Other text alongside them", async () => {
	const w = mountWithPalette(AskCard, {
		spec: spec('[{"q":"Which?","type":"multi","options":["a","b","c"]}]'),
	});
	await optionNamed(w, "a").trigger("click");
	await optionNamed(w, "c").trigger("click");
	await w.find("input.jv-ask-other").setValue("and d");
	expect(optionNamed(w, "a").classes()).toContain("on");
	await submit(w).trigger("click");
	expect(findCard(w).emitted("submit")[0][0]).toBe(
		"Here are my answers:\n1. Which? → a, c, and d"
	);
});

test("an all-field-type ask renders as the compact mini-form", async () => {
	const w = mountWithPalette(AskCard, {
		spec: spec('[{"q":"From","type":"date"},{"q":"Note","type":"text"}]'),
	});
	expect(w.find(".jv-ask").classes()).toContain("jv-ask--form");
	// field questions have no Other box
	expect(w.findAll("input.jv-ask-other")).toHaveLength(0);
	await w.find('input[type="date"]').setValue("2026-07-27");
	expect(submit(w).attributes("disabled")).toBeDefined();
	await w.find('input[type="text"]').setValue("quarter close");
	expect(submit(w).attributes("disabled")).toBeUndefined();
	await submit(w).trigger("click");
	expect(findCard(w).emitted("submit")[0][0]).toBe(
		"Here are my answers:\n1. From → 2026-07-27\n2. Note → quarter close"
	);
});

test("a mixed ask keeps the numbered-list look", () => {
	const w = mountWithPalette(AskCard, {
		spec: spec('[{"q":"From","type":"date"},{"q":"Scope","type":"single","options":["All"]}]'),
	});
	expect(w.find(".jv-ask").classes()).not.toContain("jv-ask--form");
});
