// The glue in ChatView.vue for the panels of a server draft card: the keyboard, where focus goes
// and returns, the 5-second pending-list refresh while a form is open, and the filled cells that
// follow the person's edits. The real source is cut out and executed with real Vue refs and a
// real DOM (jsdom); the pure decisions behind it are tested in lib/toolDraftKeys.spec.js and
// lib/toolDraftPanel.spec.js.
import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "fs";
import path from "path";
import { computed, nextTick, reactive, ref, watch } from "vue";
import { draftCardValues } from "../lib/draftCardValues.js";
import { draftIsDirty } from "../lib/draftSave.js";
import { timesheetCard, timesheetModel } from "../lib/timesheetDraftFixture.js";
import {
	firstEditable,
	openerButton,
	tabWrapTarget,
	toolPanelKeyAction,
} from "../lib/toolDraftKeys.js";
import { prepareToolModel } from "../lib/toolDraftPanel.js";
import { TOOL_FORM_POLL_MS, closeAsksFirst, watchCalculatedCells } from "../lib/toolDraftSheet.js";

const src = fs.readFileSync(path.resolve(__dirname, "ChatView.vue"), "utf8");
function cut(from, to) {
	const a = src.indexOf(from);
	expect(a, `not found: ${from}`).toBeGreaterThan(-1);
	const b = src.indexOf(to, a);
	expect(b, `not found: ${to}`).toBeGreaterThan(a);
	return src.slice(a, b);
}
// Run source text with `scope` as its free variables; returns what `ret` names.
const run = (code, scope, ret) =>
	new Function(...Object.keys(scope), `${code}\nreturn { ${ret.join(", ")} };`)(
		...Object.values(scope)
	);

afterEach(() => {
	document.body.innerHTML = "";
});

describe("the keyboard handler of the panels", () => {
	const code = cut("function onToolPanelKey(e) {", "onMounted(() => window.addEventListener");
	const make = (over = {}) => {
		const calls = [];
		const panelEl = document.createElement("aside");
		panelEl.innerHTML = `<button id="a">a</button><button id="b">b</button>`;
		panelEl.tabIndex = -1;
		document.body.appendChild(panelEl);
		const scope = {
			toolPreview: ref(null),
			toolFormOpen: computed(() => !!draftPanel.value && !!draftPanel.value.tool),
			draftPanel: ref(null),
			draftPanelEl: ref(panelEl),
			toolPreviewPanel: () => null,
			store: { settingsOpen: false },
			modalIsOpen: () => false,
			tabWrapTarget,
			toolPanelKeyAction,
			draftLink: ref({ key: "", items: [], open: false, anchor: null }),
			draftIsDirty: (m) => !!m.dirty,
			document,
			closeDraftPanel: () => calls.push("closeDraftPanel"),
			...over,
		};
		const draftPanel = scope.draftPanel;
		const { onToolPanelKey } = run(code, scope, ["onToolPanelKey"]);
		return { scope, calls, panelEl, key: onToolPanelKey };
	};
	const press = (key, extra = {}) => {
		const e = {
			key,
			defaultPrevented: false,
			shiftKey: false,
			preventDefault: vi.fn(),
			...extra,
		};
		return e;
	};

	// Break: Escape on the Preview does nothing, or closes a form that holds edits, or a typed edit
	// panel (no `tool`) reacts to the keys of the server draft's panel.
	it("Escape: closes the Preview, closes a clean form, refocuses a form with edits", () => {
		const a = make();
		a.scope.toolPreview.value = { token: "t1" };
		const e1 = press("Escape");
		a.key(e1);
		expect([a.scope.toolPreview.value, e1.preventDefault.mock.calls.length]).toEqual([
			null,
			1,
		]);

		const b = make();
		b.scope.draftPanel.value = { tool: true, dirty: false };
		b.key(press("Escape"));
		expect(b.calls).toEqual(["closeDraftPanel"]);

		const c = make();
		c.scope.draftPanel.value = { tool: true, dirty: true };
		const focus = vi.spyOn(c.panelEl, "focus");
		c.key(press("Escape"));
		expect([c.calls, focus.mock.calls.length]).toEqual([[], 1]);
	});

	it("does nothing for a typed panel, no panel, a handled key or an open dialog", () => {
		const typed = { dirty: false };
		const tool = { tool: true, dirty: false };
		for (const [name, panel, scope, ev] of [
			["a typed panel", typed, {}, press("Escape")],
			["no panel", null, {}, press("Escape")],
			["handled inside the panel", tool, {}, press("Escape", { defaultPrevented: true })],
			["a dialog is open", tool, { modalIsOpen: () => true }, press("Escape")],
		]) {
			const m = make(scope);
			m.scope.draftPanel.value = panel;
			m.key(ev);
			expect([name, m.calls, ev.preventDefault.mock.calls.length]).toEqual([name, [], 0]);
		}
	});

	// Break: Escape in a Link input with its menu open closes the whole clean form (or does nothing
	// for a form with edits) instead of closing only the menu; or a menu with no items counts as open.
	for (const [name, dirty, items, open, closed, stillOpen] of [
		["clean form, menu open", false, ["A"], true, 0, false],
		["form with edits, menu open", true, ["A"], true, 0, false],
		["clean form, menu open but empty", false, [], true, 1, true],
		["clean form, menu closed", false, ["A"], false, 1, false],
	])
		it(`Escape with the link menu: ${name}`, () => {
			const m = make();
			m.scope.draftPanel.value = { tool: true, dirty };
			m.scope.draftLink.value = { key: "f:x", items, open, anchor: null };
			const e = press("Escape");
			m.key(e);
			expect([
				m.calls.length,
				m.scope.draftLink.value.open,
				e.preventDefault.mock.calls.length,
			]).toEqual([closed, stillOpen, 1]);
		});

	// Break: Tab leaves the open form from its last control.
	it("Tab at the last control of the form wraps to the first", () => {
		const m = make();
		m.scope.draftPanel.value = { tool: true };
		m.panelEl.querySelector("#b").focus();
		const e = press("Tab");
		m.key(e);
		expect([e.preventDefault.mock.calls.length, document.activeElement.id]).toEqual([1, "a"]);
	});
});

describe("focus goes into a panel and returns to the card button that opened it", () => {
	const code = cut("let toolOpener = null;", "// While the form is open its card");
	const card = (disabled = false) => {
		document.body.innerHTML = `<div data-draft-token="t1">
			<button class="dc-btn"><span class="dc-label">Preview</span></button>
			<button class="dc-btn" ${
				disabled ? "disabled" : ""
			}><span class="dc-label">Edit</span></button></div>
			<aside id="panel"><div class="jv-draft-body"><input id="first"></div></aside>`;
	};
	const make = () => {
		const draftPanel = ref(null);
		const toolPreview = ref(null);
		const composer = { focusInput: vi.fn() };
		const scope = {
			toolFormOpen: computed(() => !!draftPanel.value && !!draftPanel.value.tool),
			draftPanel,
			toolPreview,
			toolDraftBuilding: ref(""),
			toolPreviewPanel: () => null,
			draftPanelEl: ref(null),
			composerRef: ref(composer),
			currentId: ref("c1"),
			nextTick,
			watch,
			firstEditable,
			tabWrapTarget,
			openerButton,
			document,
		};
		const fns = run(code, scope, ["rememberToolOpener", "focusAfterFailedOpen"]);
		return { scope, composer, ...fns };
	};
	const settle = async () => {
		await nextTick();
		await nextTick();
		await nextTick();
	};

	// Break: closing the form drops focus on the page (a keyboard user starts again from the top).
	for (const [name, setup, want] of [
		["Edit button", () => card(), "Edit"],
		["Preview button", () => card(), "Preview"],
	])
		it(`closing returns focus to the ${name}`, async () => {
			setup();
			const m = make();
			m.rememberToolOpener({ token: "t1", conversation: "c1" }, want);
			if (want === "Preview") m.scope.toolPreview.value = { token: "t1" };
			else m.scope.draftPanel.value = { tool: true, token: "t1" };
			await settle();
			if (want === "Preview") m.scope.toolPreview.value = null;
			else m.scope.draftPanel.value = null;
			await settle();
			expect(document.activeElement.textContent.trim()).toBe(want);
			expect(m.composer.focusInput).not.toHaveBeenCalled();
		});

	// Break: the Preview's Edit fails to build the form and focus is left on nothing (the Preview is
	// already closed and the watcher skipped its return while the build ran).
	for (const [name, setup, composerCalls, label] of [
		["the Edit button", () => card(), 0, "Edit"],
		["the composer when Edit is disabled", () => card(true), 1, ""],
	])
		it(`a failed form open puts focus on ${name}`, () => {
			setup();
			const m = make();
			m.rememberToolOpener({ token: "t1", conversation: "c1" }, "Edit");
			m.focusAfterFailedOpen();
			expect([
				document.activeElement.tagName === "BUTTON"
					? document.activeElement.textContent.trim()
					: "",
				m.composer.focusInput.mock.calls.length,
			]).toEqual([label, composerCalls]);
		});

	// Break: focus is lost when the card is gone or its button is disabled, or follows a person who
	// left the chat, or fights the next panel (Preview, then its Edit).
	for (const [name, setup, arrange, focused] of [
		["the card is gone", () => (document.body.innerHTML = ""), () => {}, true],
		["its button is disabled", () => card(true), () => {}, true],
		[
			"another chat is on screen",
			() => card(),
			(m) => (m.scope.currentId.value = "c2"),
			false,
		],
		[
			"another form is being built",
			() => card(),
			(m) => (m.scope.toolDraftBuilding.value = "t1"),
			false,
		],
	])
		it(`closing with ${name}: ${
			focused ? "the composer" : "nothing is focused"
		}`, async () => {
			setup();
			const m = make();
			m.rememberToolOpener({ token: "t1", conversation: "c1" }, "Edit");
			m.scope.draftPanel.value = { tool: true, token: "t1" };
			await settle();
			arrange(m);
			m.scope.draftPanel.value = null;
			await settle();
			expect(m.composer.focusInput).toHaveBeenCalledTimes(focused ? 1 : 0);
		});

	// Break: the form opens with focus still on the card button behind the overlay.
	it("opening the form focuses its first editable field", async () => {
		card();
		const m = make();
		m.scope.draftPanelEl.value = document.querySelector("#panel");
		m.scope.draftPanel.value = { tool: true, token: "t1" };
		await settle();
		expect(document.activeElement.id).toBe("first");
	});
});

describe("the pending list is re-read every 5 seconds while a form is open, and only then", () => {
	const code = cut("let _toolFormPoll = null;", "onBeforeUnmount(stopToolFormPoll);");
	it("starts on open, stops on close, never polls for a typed panel", async () => {
		vi.useFakeTimers();
		try {
			const draftPanel = ref(null);
			const resync = vi.fn();
			run(
				code,
				{
					watch,
					TOOL_FORM_POLL_MS,
					toolFormOpen: computed(() => !!draftPanel.value && !!draftPanel.value.tool),
					draftPanel,
					currentId: ref("c1"),
					resyncPendingConfirmations: resync,
					setInterval,
					clearInterval,
				},
				[]
			);
			draftPanel.value = { token: "t1" }; // a typed panel
			await nextTick();
			vi.advanceTimersByTime(20000);
			expect(resync).not.toHaveBeenCalled();
			draftPanel.value = { tool: true, token: "t1" };
			await nextTick();
			vi.advanceTimersByTime(4999);
			expect(resync).not.toHaveBeenCalled();
			vi.advanceTimersByTime(1);
			vi.advanceTimersByTime(10000);
			expect(resync.mock.calls).toEqual([
				["c1", "auto"],
				["c1", "auto"],
				["c1", "auto"],
			]);
			draftPanel.value = null;
			await nextTick();
			vi.advanceTimersByTime(30000);
			expect(resync).toHaveBeenCalledTimes(3);
		} finally {
			vi.useRealTimers();
		}
	});
});

describe("the filled cells follow the person's edits through the real form model", () => {
	// The view wires the shared watcher (lib/toolDraftSheet.watchCalculatedCells): the cut is that
	// one line, run with the real function, so a different source or depth in the lib shows here.
	const code = cut(
		"watchCalculatedCells(watch, () => draftPanel.value);",
		"// Resync (R3 fix for #3)"
	);
	// Break: the watcher misses a write made by a v-model on a cell (a deep write), so Hrs keeps its
	// number beside an edit; or it re-runs forever.
	it("a cell edit blanks that row's Hrs; undoing it gives the number back; nothing is sent for it", async () => {
		const draftPanel = ref(null);
		run(code, { watch, draftPanel, watchCalculatedCells }, []);
		const card = timesheetCard();
		const model = timesheetModel();
		prepareToolModel(model, {
			token: "t1",
			conversation: "c1",
			card,
			fill: { readOnly: true },
		});
		draftPanel.value = reactive(model);
		const rows = draftPanel.value.tables[0].rows;
		await nextTick();
		expect(rows.map((r) => r.hours)).toEqual(["3", "1.50", "2"]);
		rows[1].project = "PROJ-0002";
		await nextTick();
		expect(rows.map((r) => r.hours)).toEqual(["3", "", "2"]);
		expect(
			draftCardValues(draftPanel.value, draftPanel.value.snapshot, draftPanel.value.card)
		).toEqual({
			time_logs: [
				{ _draft_row: 0 },
				{ _draft_row: 1, project: "PROJ-0002" },
				{ _draft_row: 2 },
			],
		});
		rows[1].project = "PROJ-0001";
		await nextTick();
		expect(rows.map((r) => r.hours)).toEqual(["3", "1.50", "2"]);
		expect(draftIsDirty(draftPanel.value)).toBe(false);
	});

	it("watches nothing for a typed panel", async () => {
		const draftPanel = ref(null);
		const sources = [];
		// the view's `watch`, noting what its source function returns
		const spyWatch = (source, ...rest) =>
			watch(() => {
				const value = source();
				sources.push(value);
				return value;
			}, ...rest);
		run(code, { watch: spyWatch, draftPanel, watchCalculatedCells }, []);
		draftPanel.value = reactive({ tables: [{ rows: [{ a: 1 }] }], calc: [{}] });
		await nextTick();
		draftPanel.value.tables[0].rows[0].a = 2;
		await nextTick();
		expect(sources.length).toBeGreaterThan(0);
		expect(sources.every((value) => value === null)).toBe(true);
	});
});

describe("the template", () => {
	// Break: focus cannot be returned to the card button (the card has no token), the item error
	// sits against the card, or the stack hint under the stack is gone.
	it("marks the card with its token, spaces the item error and draws the stack hint line", () => {
		const draft = cut(
			'<div v-if="isToolDraftItem(pa)"',
			'<div v-else class="jv-action jv-pending">'
		);
		expect(draft.replace(/\s+/g, " ")).toContain(':data-draft-token="pa.token"');
		expect(src).toContain(
			'<div v-if="orphanHint" class="jv-stack-hint">{{ orphanHint }}</div>'
		);
	});
});

describe("the X button of the form", () => {
	const code = cut("let draftCloseAsking = false;", "\nconst draftCta");
	const make = (panel, answer) => {
		const closed = [];
		const draftPanel = ref(panel);
		const confirm = vi.fn(answer);
		const { onDraftCloseClick } = run(
			code,
			{
				draftPanel,
				draftPanelDead: ref(false),
				closeAsksFirst,
				confirm,
				closeDraftPanel: () => closed.push(1),
			},
			["onDraftCloseClick"]
		);
		return { draftPanel, confirm, closed, click: onDraftCloseClick };
	};

	// Break: the X drops edits without asking, asks for a clean form, closes after "Keep editing",
	// asks twice for a double press, or closes a different form that opened while the dialog was up.
	it("asks only for a dirty server form, once, and closes only the same panel on Discard", async () => {
		// A real form model: dirty means one edited field (see closeAsksFirst's own table).
		const m = timesheetModel();
		prepareToolModel(m, {
			token: "t1",
			conversation: "c1",
			card: timesheetCard(),
			fill: { readOnly: true },
		});
		const clean = make(m, async () => true);
		await clean.click();
		expect([clean.confirm.mock.calls.length, clean.closed.length]).toEqual([0, 1]);

		m.fields.find((f) => f.fieldname === "parent_project").value = "PROJ-0002";
		const keep = make(m, async () => false);
		await keep.click();
		expect(keep.confirm.mock.calls[0][0]).toMatchObject({
			title: "Discard your changes?",
			confirmLabel: "Discard",
			cancelLabel: "Keep editing",
		});
		expect(keep.closed.length).toBe(0);

		let answer;
		const twice = make(m, () => new Promise((r) => (answer = r)));
		const first = twice.click();
		await twice.click();
		expect(twice.confirm.mock.calls.length).toBe(1);
		twice.draftPanel.value = { tool: true, other: true };
		answer(true);
		await first;
		expect(twice.closed.length).toBe(0);

		const yes = make(m, async () => true);
		await yes.click();
		expect(yes.closed.length).toBe(1);
	});
});
