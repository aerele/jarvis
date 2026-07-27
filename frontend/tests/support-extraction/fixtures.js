// Fixture message data + the mount helper shared by every support-extraction
// snapshot test. See docs/superpowers/plans/2026-07-24-support-ui-pr1-extraction.md
// (Task 1) for the contract this file implements.
import { mount } from "@vue/test-utils";
import { h } from "vue";
import { LIGHT_VARS, DARK_VARS } from "../../src/theme.js";

// A plain user chat bubble with one image attachment, matching the shape of
// a ChatView message object (`m.content` / `m.canvas`).
export const USER_MSG = {
	name: "msg-user-1",
	role: "user",
	content: "Can you summarise the attached screenshot and open a ticket for it?",
	canvas: [
		{
			name: "cv-1",
			type: "image",
			title: "screenshot.png",
			file_url: "/files/screenshot.png",
		},
	],
};

// Assistant markdown covering every element the `.jv-md` body styling
// targets: heading, list, code block, table, link, image.
export const ASSISTANT_MSG = {
	name: "msg-assistant-1",
	role: "assistant",
	content: [
		"## Summary",
		"",
		"Here is what I found:",
		"",
		"- First item",
		"- Second item with `inline code`",
		"",
		"```js",
		"console.log('hello');",
		"```",
		"",
		"| Field | Value |",
		"| --- | --- |",
		"| Status | Open |",
		"",
		"See the [ticket](https://example.com/ticket/1) for details.",
		"",
		"![diagram](/files/diagram.png)",
	].join("\n"),
};

// Bare Helpdesk HTML — no markdown pipeline, just the raw `<p>/<ul>/<a>`
// tags a Helpdesk ticket body ships as. Exercises the `.jv-html` body seam
// (Task 3) that restores element-level styling Tailwind preflight strips.
export const SUPPORT_HTML_MSG = {
	name: "msg-support-1",
	role: "assistant",
	html: [
		"<p>Thanks for reaching out. Here is what we found:</p>",
		"<ul><li>Step one</li><li>Step two</li></ul>",
		'<p>See <a href="https://example.com/kb/1">this article</a> for more.</p>',
	].join(""),
};

export const PALETTE_LIGHT = LIGHT_VARS;
export const PALETTE_DARK = DARK_VARS;

export const varsToStyle = (v) =>
	Object.entries(v)
		.map(([k, val]) => `${k}:${val}`)
		.join(";");

// Mounts `Component` inside a `.jv-root` wrapper with the palette vars bound
// inline (mirroring ChatView's `:style="paletteVars"` root), so extracted
// children can resolve `var(--…)` by cascade exactly like they do in the app.
export function mountWithPalette(Component, props = {}, { dark = false, slots = {} } = {}) {
	const vars = dark ? DARK_VARS : LIGHT_VARS;
	return mount({
		render() {
			return h(
				"div",
				{ class: ["jv-root", { "jv-dark": dark }], style: varsToStyle(vars) },
				[h(Component, props, slots)]
			);
		},
	});
}
