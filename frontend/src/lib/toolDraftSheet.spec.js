// lib/toolDraftSheet.js: what the footer, the close, the poll and the filled cells of a server
// draft's form do, shared by the desktop panel and the phone sheet. The desktop's ChatView calls
// these functions itself (its template reads toolFooterState and previewPrimaryDisabled, it imports
// the poll interval and wires watchCalculatedCells); ChatView.toolDraftKeys.spec.js runs the view's
// poll and watcher text. This file holds the rules as tables, and that the view still reads them.
import fs from "fs";
import path from "path";
import { describe, expect, it } from "vitest";
import { timesheetCard, timesheetModel } from "./timesheetDraftFixture.js";
import { prepareToolModel } from "./toolDraftPanel.js";
import { TOOL_FORM_POLL_MS, closeAsksFirst, toolFooterState } from "./toolDraftSheet.js";

const view = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const squash = (t) => t.replace(/\s+/g, " ").trim();
const cut = (from, to, start = 0) => {
	const a = view.indexOf(from, start);
	expect(a, `not found: ${from}`).toBeGreaterThan(-1);
	const b = view.indexOf(to, a);
	expect(b, `not found: ${to}`).toBeGreaterThan(a);
	return view.slice(a, b);
};

describe("toolFooterState is the footer of the form, on the desktop and the phone", () => {
	// The table was recorded by running the desktop footer's own template expressions (the two
	// `:disabled` bindings and the label) before the view started calling this function; the
	// view's template now reads toolFooterState, so the table holds what those expressions said.
	const WAITING = "Waiting for the current reply to finish";
	const STATES = [
		["idle", {}, {}, ["Create", false, false, ""]],
		["saving", { applying: true }, {}, ["Saving…", true, true, ""]],
		["blocked form", { saveBlocked: "cannot" }, {}, ["Create", true, false, ""]],
		["dead", {}, { dead: true }, ["Create", true, true, ""]],
		["turn running", {}, { streaming: true }, ["Create", true, false, WAITING]],
		["saving and dead", { applying: true }, { dead: true }, ["Saving…", true, true, ""]],
		["update draft", { verb: "update" }, {}, ["Save changes", false, false, ""]],
		[
			"blocked and running",
			{ saveBlocked: "cannot" },
			{ streaming: true },
			["Create", true, false, WAITING],
		],
	];
	// Break: Save or Discard is enabled when it must be off (or the other way round), the primary
	// is worded differently, or the wait line goes missing.
	it.each(STATES)(
		"%s",
		(_, panel, ctx, [primaryLabel, primaryDisabled, discardDisabled, primaryTitle]) => {
			expect(toolFooterState({ tool: true, ...panel }, ctx)).toEqual({
				primaryLabel,
				primaryDisabled,
				discardDisabled,
				primaryTitle,
			});
		}
	);

	// Break: the desktop view goes back to its own copy of the rules (the two would drift).
	it("is what the desktop's template reads", () => {
		expect(view).toContain(
			"toolFooterState(draftPanel.value, {\n\t\tdead: draftPanelDead.value,\n\t\tstreaming: convStreaming.value,\n\t})"
		);
		const foot = squash(
			cut(
				'<div v-else class="jv-draft-foot">',
				"</aside>",
				view.indexOf('<div v-else class="jv-draft-foot">')
			)
		);
		for (const text of [
			':disabled="toolFoot.primaryDisabled"',
			':title="toolFoot.primaryTitle"',
			"{{ toolFoot.primaryLabel }}",
			':disabled="toolFoot.discardDisabled"',
		])
			expect(foot).toContain(text);
		expect(foot).not.toContain("convStreaming");
		expect(view.replace(/\s+/g, "")).toContain(
			':disabled="previewPrimaryDisabled({streaming:convStreaming,busy:toolPreviewItem&&toolPreviewItem.busy,})"'
		);
	});
});

describe("the poll interval", () => {
	// Break: the desktop declares its own interval again, beside the shared one the phone reads
	// (the dead banner would arrive at different times on the two).
	it("is the shared one on the desktop", () => {
		expect(TOOL_FORM_POLL_MS).toBe(5000);
		expect(view).toMatch(
			/import \{[^}]*\bTOOL_FORM_POLL_MS\b[^}]*\} from "@\/lib\/toolDraftSheet"/
		);
		expect(view).not.toMatch(/const TOOL_FORM_POLL_MS\b/);
		expect(view).toContain("}, TOOL_FORM_POLL_MS);");
	});
});

describe("closeAsksFirst", () => {
	// A real form of the Timesheet draft, clean or with one edited field.
	const form = (edit = false, over = {}) => {
		const m = timesheetModel();
		prepareToolModel(m, {
			token: "t1",
			conversation: "c1",
			card: timesheetCard(),
			fill: { readOnly: true },
		});
		if (edit) m.fields.find((f) => f.fieldname === "parent_project").value = "PROJ-0002";
		return Object.assign(m, over);
	};
	// Break: a clean form asks (an extra tap for nothing), a dirty one does not (silent loss of the
	// edits), a saving or dead form asks about edits that are being sent or can no longer be sent.
	it.each([
		["clean", () => form(), {}, false],
		["dirty", () => form(true), {}, true],
		["dirty but saving", () => form(true, { applying: true }), {}, false],
		["dirty but dead", () => form(true), { dead: true }, false],
		["no form", () => null, {}, false],
	])("%s", (_, panel, ctx, want) => expect(closeAsksFirst(panel(), ctx)).toBe(want));
});
