// Snapshot + interaction tests for Message.vue's variant="bubble" path (the
// chat user row extracted in Task 2 of docs/superpowers/plans/2026-07-24-
// support-ui-pr1-extraction.md). variant="row" is stubbed empty until Task 3.
import { expect, test } from "vitest";
import Message from "../../src/components/chat/Message.vue";
import { mountWithPalette, USER_MSG } from "./fixtures.js";

test("user bubble renders identically (light)", () => {
	const w = mountWithPalette(Message, {
		variant: "bubble",
		text: USER_MSG.content,
		attachments: USER_MSG.canvas,
		timestamp: "9:12",
		editable: true,
	});
	expect(w.html()).toMatchSnapshot();
});

test("user bubble renders identically (dark)", () => {
	const w = mountWithPalette(
		Message,
		{
			variant: "bubble",
			text: USER_MSG.content,
			attachments: USER_MSG.canvas,
			timestamp: "9:12",
			editable: true,
		},
		{ dark: true }
	);
	expect(w.html()).toMatchSnapshot();
});

test("failed user bubble shows retry", () => {
	const w = mountWithPalette(Message, { variant: "bubble", text: "x", failed: true });
	expect(w.text()).toContain("Not sent");
	expect(w.text()).toContain("Retry");
});

test("failed user bubble emits retry on click", async () => {
	const w = mountWithPalette(Message, { variant: "bubble", text: "x", failed: true });
	await w.find("button").trigger("click");
	expect(w.findComponent(Message).emitted("retry")).toBeTruthy();
});

test("copy button reflects the copied prop and emits copy", async () => {
	const w = mountWithPalette(Message, { variant: "bubble", text: "hi", copied: true });
	expect(w.find(".jv-msgtime").exists()).toBe(false); // no timestamp passed
	const copyBtn = w.findAll(".jv-msgbtn").at(-1);
	await copyBtn.trigger("click");
	expect(w.findComponent(Message).emitted("copy")).toBeTruthy();
});

test("editable gates the Edit button; non-editable hides it", () => {
	const editableWrap = mountWithPalette(Message, {
		variant: "bubble",
		text: "hi",
		editable: true,
	});
	expect(editableWrap.findAll(".jv-msgbtn").length).toBe(2); // edit + copy

	const readOnlyWrap = mountWithPalette(Message, {
		variant: "bubble",
		text: "hi",
		editable: false,
	});
	expect(readOnlyWrap.findAll(".jv-msgbtn").length).toBe(1); // copy only
});

test("clicking an image attachment emits open-attachment with the canvas record", async () => {
	const w = mountWithPalette(Message, {
		variant: "bubble",
		text: "see attached",
		attachments: USER_MSG.canvas,
	});
	await w.find(".jv-img-artifact").trigger("click");
	const emitted = w.findComponent(Message).emitted("open-attachment");
	expect(emitted).toBeTruthy();
	expect(emitted[0][0]).toEqual(USER_MSG.canvas[0]);
});

test("variant=row now renders the assistant body (Task 3, no longer a stub)", () => {
	const w = mountWithPalette(Message, { variant: "row", html: "<p>hi</p>" });
	expect(w.find(".jv-umsg").exists()).toBe(false); // not the bubble path
	expect(w.find(".jv-md-body").exists()).toBe(true);
	expect(w.html()).toContain("hi");
});
