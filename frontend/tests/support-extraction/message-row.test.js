// Snapshot + interaction tests for Message.vue's variant="row" path (the
// assistant / support-agent row extracted in Task 3 of docs/superpowers/plans/
// 2026-07-24-support-ui-pr1-extraction.md). Covers BOTH body seams: the chat
// markdown body (bodyClass="jv-md") and the bare Helpdesk HTML body
// (bodyClass="jv-html"), which PR2's Support page relies on.
import { expect, test } from "vitest";
import { h } from "vue";
import Message from "../../src/components/chat/Message.vue";
import { renderMarkdown } from "../../src/markdown.js";
import { mountWithPalette, ASSISTANT_MSG, SUPPORT_HTML_MSG } from "./fixtures.js";

test("assistant markdown row renders identically (light)", () => {
	const w = mountWithPalette(Message, {
		variant: "row",
		html: renderMarkdown(ASSISTANT_MSG.content),
		bodyClass: "jv-md",
		timestamp: "9:12",
	});
	expect(w.html()).toMatchSnapshot();
});

test("assistant markdown row renders identically (dark)", () => {
	const w = mountWithPalette(
		Message,
		{
			variant: "row",
			html: renderMarkdown(ASSISTANT_MSG.content),
			bodyClass: "jv-md",
			timestamp: "9:12",
		},
		{ dark: true }
	);
	expect(w.html()).toMatchSnapshot();
});

test("markdown body carries the jv-md-body + jv-md seam classes", () => {
	const w = mountWithPalette(Message, {
		variant: "row",
		html: renderMarkdown(ASSISTANT_MSG.content),
		bodyClass: "jv-md",
	});
	const body = w.find(".jv-md-body");
	expect(body.exists()).toBe(true);
	expect(body.classes()).toContain("jv-md");
	// the markdown pipeline produced the classed elements the :deep() rules target
	expect(body.find(".jv-md-h").exists()).toBe(true);
	expect(body.find(".jv-md-list").exists()).toBe(true);
});

test("support HTML row applies the jv-html body seam", () => {
	const w = mountWithPalette(Message, {
		variant: "row",
		html: SUPPORT_HTML_MSG.html,
		bodyClass: "jv-html",
		sender: "Priya",
		role: "Support",
		timestamp: "9:12",
	});
	const body = w.find(".jv-md-body");
	expect(body.exists()).toBe(true);
	// the seam class is applied (its :deep block restores element-level styling
	// that Tailwind preflight strips off bare Helpdesk HTML)
	expect(body.classes()).toContain("jv-html");
	expect(body.classes()).not.toContain("jv-md");
	// the bare list + link actually render under it
	expect(body.find("ul").exists()).toBe(true);
	expect(body.find("li").exists()).toBe(true);
	const a = body.find("a");
	expect(a.exists()).toBe(true);
	expect(a.attributes("href")).toBe("https://example.com/kb/1");
});

test("row shows the identity line only when a sender is passed", () => {
	const withSender = mountWithPalette(Message, {
		variant: "row",
		html: "<p>hi</p>",
		sender: "Priya",
		role: "Support",
	});
	expect(withSender.find(".jv-msg-who").exists()).toBe(true);
	expect(withSender.text()).toContain("Priya");

	const noSender = mountWithPalette(Message, { variant: "row", html: "<p>hi</p>" });
	expect(noSender.find(".jv-msg-who").exists()).toBe(false);
});

test("row Copy bar emits copy and never offers Edit", async () => {
	// with no #below-body slot the built-in trailer (support use) renders; the
	// row is not editable, so there is a Copy button but no Edit button.
	const w = mountWithPalette(Message, {
		variant: "row",
		html: "<p>hi</p>",
		timestamp: "9:12",
	});
	const copyBtn = w.find(".jv-msgbtn");
	expect(copyBtn.exists()).toBe(true);
	expect(copyBtn.attributes("title")).toBe("Copy");
	await copyBtn.trigger("click");
	expect(w.findComponent(Message).emitted("copy")).toBeTruthy();
});

test("#below-body slot suppresses the built-in trailer (chat owns that region)", () => {
	// chat supplies #below-body (its own metabar/canvas live there); the
	// component must NOT also render its built-in Copy bar, or it would double.
	const w = mountWithPalette(
		Message,
		{ variant: "row", html: "<p>hi</p>", timestamp: "9:12" },
		{ slots: { "below-body": () => h("div", { class: "chat-metabar" }, "meta") } }
	);
	expect(w.find(".chat-metabar").exists()).toBe(true);
	expect(w.find(".jv-msgbar").exists()).toBe(false); // built-in trailer suppressed
});
