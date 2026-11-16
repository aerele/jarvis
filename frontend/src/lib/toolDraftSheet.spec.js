// lib/toolDraftSheet.js: what the footer, the close and the filled cells of a server draft's form
// do, shared by the desktop panel and the phone sheet. The desktop's ChatView keeps its own text
// for the footer expressions, the poll interval and the watcher (specs cut that text out and run
// it, so it cannot move); these specs run THAT text and the shared functions on the same inputs,
// so the two cannot drift apart.
import fs from "fs";
import path from "path";
import { describe, expect, it } from "vitest";
import { nextTick, reactive, ref, watch } from "vue";
import { draftCardValues } from "./draftCardValues.js";
import { timesheetCard, timesheetModel } from "./timesheetDraftFixture.js";
import { prepareToolModel, settleCalculatedCells, toolPrimaryLabel } from "./toolDraftPanel.js";
import {
	TOOL_FORM_POLL_MS,
	calcCellsSource,
	closeAsksFirst,
	toolFooterState,
	watchCalculatedCells,
} from "./toolDraftSheet.js";

const view = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const squash = (t) => t.replace(/\s+/g, " ").trim();
const cut = (from, to, start = 0) => {
	const a = view.indexOf(from, start);
	expect(a, `not found: ${from}`).toBeGreaterThan(-1);
	const b = view.indexOf(to, a);
	expect(b, `not found: ${to}`).toBeGreaterThan(a);
	return view.slice(a, b);
};

// The desktop's footer for a server draft: the two `:disabled` expressions and the label.
const foot = squash(
	cut(
		'<div v-else class="jv-draft-foot">',
		"</aside>",
		view.indexOf('<div v-else class="jv-draft-foot">')
	)
);
const expr = (afterText) => {
	const a = foot.indexOf(afterText);
	expect(a).toBeGreaterThan(-1);
	const open = foot.indexOf(':disabled="', a) + ':disabled="'.length;
	return foot.slice(open, foot.indexOf('"', open));
};
const primaryOff = new Function(
	"draftPanel",
	"draftPanelDead",
	"convStreaming",
	`return !!(${expr('class="jv-action-primary"')});`
);
const discardOff = new Function(
	"draftPanel",
	"draftPanelDead",
	`return !!(${expr('class="jv-action-discard"')});`
);
const label = new Function(
	"draftPanel",
	"toolPrimaryLabel",
	`return ${foot.slice(foot.indexOf("{{ ") + 3, foot.indexOf(" }}"))};`
);

describe("toolFooterState is the desktop footer's own rule", () => {
	const STATES = [
		["idle", {}],
		["saving", { applying: true }],
		["blocked form", { saveBlocked: "cannot" }],
		["dead", {}, { dead: true }],
		["turn running", {}, { streaming: true }],
		["saving and dead", { applying: true }, { dead: true }],
		["update draft", { verb: "update" }],
		["blocked and running", { saveBlocked: "cannot" }, { streaming: true }],
	];
	// Break: the phone's Save or Discard is enabled when the desktop's is off (or the other way
	// round), or it words the primary differently; the desktop template text is the reference.
	it.each(STATES)("%s", (_, panel, ctx = {}) => {
		const dead = !!ctx.dead;
		const streaming = !!ctx.streaming;
		const want = {
			primaryLabel: label(panel, toolPrimaryLabel),
			primaryDisabled: primaryOff(panel, dead, streaming),
			discardDisabled: discardOff(panel, dead),
		};
		const got = toolFooterState({ tool: true, ...panel }, { dead, streaming });
		expect({
			primaryLabel: got.primaryLabel,
			primaryDisabled: got.primaryDisabled,
			discardDisabled: got.discardDisabled,
		}).toEqual(want);
	});
});

describe("the poll interval", () => {
	// Break: the phone polls at another rate than the desktop (the dead banner arrives later or
	// the server is asked twice as often).
	it("is the desktop's", () => {
		const m = /const TOOL_FORM_POLL_MS = (\d+);/.exec(view);
		expect(m).not.toBeNull();
		expect(TOOL_FORM_POLL_MS).toBe(Number(m[1]));
	});
});

describe("watchCalculatedCells runs the desktop watcher's sequence", () => {
	// The desktop watcher, cut out of the view and executed with the same Vue and the same lib.
	const desktop = (draftPanel) =>
		new Function(
			"watch",
			"draftPanel",
			"settleCalculatedCells",
			`${cut(
				"watch(\n\t() => {\n\t\tconst p = draftPanel.value;\n\t\treturn p && p.tool && Array.isArray(p.calc)",
				"// Resync (R3 fix for #3)"
			)}`
		)(watch, draftPanel, settleCalculatedCells);
	const model = () => {
		const m = timesheetModel();
		prepareToolModel(m, {
			token: "t1",
			conversation: "c1",
			card: timesheetCard(),
			fill: { readOnly: true },
		});
		return reactive(m);
	};
	// An edit of a cell that feeds a calculation blanks that row's filled Hrs; undoing it gives the
	// number back; a typed panel is left alone. Returns the Hrs column after each step.
	async function sequence(wire) {
		const panel = ref(null);
		wire(panel);
		panel.value = model();
		const rows = panel.value.tables[0].rows;
		const hours = () => rows.map((r) => r.hours);
		const out = [];
		await nextTick();
		out.push(hours());
		rows[1].project = "PROJ-0002";
		await nextTick();
		out.push(hours());
		rows[1].project = "PROJ-0001";
		await nextTick();
		out.push(hours());
		out.push(draftCardValues(panel.value, panel.value.snapshot, panel.value.card));
		return out;
	}
	// Break: the phone's wiring watches a different part of the model, or at a different depth, so
	// a cell edit leaves a stale Hrs beside it (or the settle runs for a typed panel).
	it("gives the same Hrs after every step and the same payload as the desktop's own text", async () => {
		const a = await sequence((panel) => desktop(panel));
		const b = await sequence((panel) => watchCalculatedCells(watch, () => panel.value));
		expect(b).toEqual(a);
		expect(b[0]).toEqual(["3", "1.50", "2"]);
		expect(b[1]).toEqual(["3", "", "2"]);
		expect(b[2]).toEqual(["3", "1.50", "2"]);
	});

	it("a typed panel (no `tool`) and a form with no filled cell give no source", () => {
		expect(calcCellsSource({ calc: [{}], tables: [] })).toBeNull();
		expect(calcCellsSource({ tool: true, calc: [], tables: [] })).toBeNull();
		expect(calcCellsSource(null)).toBeNull();
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
