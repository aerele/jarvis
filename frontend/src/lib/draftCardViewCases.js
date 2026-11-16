// The checks for draftCardView, written once. The vitest spec and the PWA's `node --test`
// both run these, so the two runners cannot drift. Each case is {name, actual, expected}
// and the runner only compares them with a deep equal.
//
import { isDraftCard, isSensitiveCard } from "./draftCardView.js";

// Each case names the break it catches that no other case catches.

const card = (fixtures, id) => structuredClone(fixtures.find((f) => f.id === id).card);
const shape = (view) => view.buttons.map((b) => [b.id, b.label, b.primary, b.disabled, b.loading]);
const btn = (id, label, o = {}) => [id, label, !!o.primary, !!o.disabled, !!o.loading];

// The State Table, as data: [state, ctx, the create-verb button list, the update-verb one].
// Desktop and phone must give the same list (the owner's rule: the phone has every option).
const ready = (verb) => [
	btn(verb === "update" ? "save" : "create", verb === "update" ? "Save changes" : "Create", {
		primary: true,
	}),
	btn("preview", "Preview"),
	btn("edit", "Edit"),
	btn("cancel", "Cancel"),
];
const locked = (verb, o) =>
	ready(verb).map((b) => (o.keep === b[0] ? b : [b[0], b[1], b[2], true, o.spin === b[0]]));
export const STATE_BUTTONS = [
	["S1", {}, ready],
	["S2", { earlier: true }, ready],
	["S3", { editing: true }, (v) => locked(v, { keep: "cancel" })],
	["S4", { busy: "create" }, (v) => locked(v, { spin: v === "update" ? "save" : "create" })],
	["S4", { busy: "cancel" }, (v) => locked(v, { spin: "cancel" })],
	["S13", { streaming: true }, (v) => locked(v, {})],
];

export const GARBAGE = [
	null,
	undefined,
	"a string",
	[],
	{},
	{ draft: 5 },
	{ draft: {} },
	{ draft: { verb: "delete", values: {} } },
	{ draft: { verb: "create", values: "x" } },
	{ draft: { verb: "create", values: [] } },
	{ draft: { verb: "create", values: null } },
];

// Timesheet-like table: Activity Type empty, Is Billable "No" in every row, Hrs real.
export const TIMESHEET_TABLE = {
	label: "Time Logs",
	count: 2,
	columns: ["Activity Type", "From Time", "Hrs", "Is Billable"],
	rows: [{ cells: ["", "10:00", "3", "No"] }, { cells: ["", "11:00", "1.50", "No"] }],
	extra: 0,
};

export function draftCardCases(rawView, fixtures) {
	const cases = [];
	const seen = new Set();
	// Records every state a case reaches, so the last case can prove the table is covered.
	const draftCardView = (c, x) => {
		const v = rawView(c, x);
		seen.add(v.state);
		return v;
	};
	const add = (name, actual, expected) => cases.push({ name, actual, expected });

	// Break: a fixture is classed as the wrong state (a sensitive card drawn as a draft, a bad one as ready).
	const KIND = { S1: "draft", S11: "sensitive", S12: "unreadable" };
	for (const f of fixtures) {
		add(`fixture ${f.id} is ${f.expect}`, KIND[draftCardView(f.card, {}).state], f.expect);
	}

	// Break: any button changes label, order, primary or enabled-ness in any state, on either surface.
	for (const verb of ["create", "update"]) {
		const fx =
			verb === "update" ? card(fixtures, "update-diff") : card(fixtures, "create-simple");
		for (const surface of ["desktop", "phone"]) {
			for (const [state, ctx, expected] of STATE_BUTTONS) {
				const view = draftCardView(fx, { ...ctx, surface });
				add(
					`${state} ${JSON.stringify(ctx)} ${verb} on ${surface}: state`,
					view.state,
					state
				);
				add(
					`${state} ${JSON.stringify(ctx)} ${verb} on ${surface}: buttons`,
					shape(view),
					expected(verb)
				);
			}
		}
	}

	// Break: a sensitive card gets content or buttons here, so a host could confirm it with less
	// detail than its PendingCard (whose buttons read Confirm / Discard). It is a routing signal.
	for (const surface of ["desktop", "phone"]) {
		const s = draftCardView(card(fixtures, "sensitive"), { surface });
		add(
			`S11 is a routing signal on ${surface}`,
			[s.state, s.useLegacyCard, shape(s), s.rows, s.tables, s.diff],
			["S11", true, [], [], [], []]
		);
		const u = draftCardView(card(fixtures, "unreadable"), { surface });
		add(`S12 buttons on ${surface}`, shape(u), [btn("cancel", "Cancel")]);
	}
	// Break: a host cannot tell which card to mount.
	add(
		"routing helpers",
		fixtures.map((f) => [f.id, isDraftCard(f.card), isSensitiveCard(f.card)]),
		fixtures.map((f) => [f.id, f.expect === "draft", f.expect === "sensitive"])
	);

	// Break: the unreadable card cannot be dismissed, or is pressed twice.
	const bust = card(fixtures, "unreadable");
	add("S12 under busy cancel", shape(draftCardView(bust, { busy: "cancel" })), [
		btn("cancel", "Cancel", { disabled: true, loading: true }),
	]);
	add("S12 stays dismissable while streaming", shape(draftCardView(bust, { streaming: true })), [
		btn("cancel", "Cancel"),
	]);

	// Break: the unreadable sentence changes or the card shows fields.
	const bad = draftCardView(card(fixtures, "unreadable"), {});
	add("S12 message", bad.message, "Couldn't show this draft. Ask Jarvis to send it again.");
	add("S12 shows nothing else", [bad.title, bad.rows, bad.tables, bad.diff], ["", [], [], []]);

	// Break: garbage input throws, or draws as a card.
	for (const g of GARBAGE) {
		let state;
		try {
			state = draftCardView(g, { busy: "create", editing: true }).state;
		} catch {
			state = "threw";
		}
		add(`garbage ${JSON.stringify(g)} is S12`, state, "S12");
	}
	let withBadCtx;
	try {
		withBadCtx = draftCardView(card(fixtures, "create-simple"), 7).state;
	} catch {
		withBadCtx = "threw";
	}
	add("a non-object ctx counts as no flags", withBadCtx, "S1");

	// Break: precedence of ctx flags changes (e.g. Create stays clickable while the turn streams).
	const ok = card(fixtures, "create-simple");
	const prec = (ctx) => draftCardView(ok, ctx).state;
	add(
		"busy beats streaming, editing, earlier",
		prec({ busy: "create", streaming: true, editing: true, earlier: true }),
		"S4"
	);
	add(
		"streaming beats editing and earlier",
		prec({ streaming: true, editing: true, earlier: true }),
		"S13"
	);
	add("editing beats earlier", prec({ editing: true, earlier: true }), "S3");
	add(
		"sensitive beats busy",
		draftCardView(card(fixtures, "sensitive"), { busy: "create", editing: true }).state,
		"S11"
	);

	// Break: the hint wording, or a hint outside S1 and S2.
	add(
		"hints",
		[{}, { earlier: true }, { editing: true }].map((c) => draftCardView(ok, c).hint),
		['or type "go ahead"', 'or type "confirm 1"', ""]
	);
	add(
		"footers",
		["S3", "S13"].map(
			(s) => draftCardView(ok, s === "S3" ? { editing: true } : { streaming: true }).footer
		),
		["Editing", "Jarvis is replying"]
	);
	add(
		"Earlier tag only in S2",
		[draftCardView(ok, { earlier: true }).tag, draftCardView(ok, {}).tag],
		["Earlier", ""]
	);

	// Break: the ignored / dropped sentences change or leak when empty.
	add(
		"ignored note",
		draftCardView(card(fixtures, "create-ignored-field"), {}).ignoredNote,
		"Left out: colour. These are not fields on this form."
	);
	const both = card(fixtures, "create-ignored-field");
	both.draft.ignored = ["colour", "time_logs.x"];
	both.draft.dropped = ["rate", "Cost"];
	add(
		"ignored and dropped notes",
		draftCardView(both, {}).ignoredNote,
		"Left out: colour, time_logs.x. These are not fields on this form. Not saved because you can't edit them: rate, Cost."
	);
	add("no note when nothing was left out", draftCardView(ok, {}).ignoredNote, "");

	// Break: an all-empty column is drawn, or a deliberate "No" / "0" is hidden from the person.
	const ts = card(fixtures, "create-simple");
	ts.tables = [TIMESHEET_TABLE];
	const t = draftCardView(ts, {}).tables[0];
	add(
		"an all-empty column is dropped, an all-No column is kept",
		[t.columns, t.rows],
		[
			["From Time", "Hrs", "Is Billable"],
			[
				["10:00", "3", "No"],
				["11:00", "1.50", "No"],
			],
		]
	);
	const zero = structuredClone(TIMESHEET_TABLE);
	zero.rows.forEach((r) => (r.cells[2] = "0"));
	zero.rows[0].cells.push(0, false);
	zero.columns.push("N", "F");
	zero.rows[1].cells.push(0, false);
	ts.tables = [zero];
	add("zero and false cells are kept", draftCardView(ts, {}).tables[0].rows[0], [
		"10:00",
		"0",
		"No",
		"0",
		"false",
	]);
	add(
		"a table of only empty columns keeps its first column",
		draftCardView(
			{
				...ts,
				tables: [{ ...TIMESHEET_TABLE, columns: ["A", "B"], rows: [{ cells: ["", ""] }] }],
			},
			{}
		).tables[0].columns,
		["A"]
	);
	// Break: a row wider than the header silently loses cells; a bad count shows "-3 more"; a table
	// with no label draws an empty caption.
	const odd = draftCardView(
		{
			...ts,
			tables: [
				{ columns: ["A"], rows: [{ cells: ["x", "y"] }], count: -3, extra_columns: 2 },
			],
		},
		{}
	).tables[0];
	add(
		"odd table parts",
		[odd.columns, odd.rows, odd.count, odd.more, odd.moreColumns, odd.label],
		[["A", ""], [["x", "y"]], 1, 0, 2, ""]
	);
	// Break: objects print as [object Object]; "null" / "undefined" strings print.
	add(
		"unprintable values are empty",
		draftCardView(
			{
				...ts,
				rows: [
					{ label: "A", value: {} },
					{ label: "B", value: "undefined" },
					{ label: "C", value: [1] },
				],
			},
			{}
		).rows,
		[]
	);
	// Break: the trial warning is lost on the new card, or says something other than today's card.
	const warned = { ...card(fixtures, "create-simple"), warning: { jobs: 2 } };
	add(
		"warning sentence",
		draftCardView(warned, {}).warning,
		"This will also start 2 background jobs."
	);
	add("no warning when none", draftCardView(card(fixtures, "create-simple"), {}).warning, "");
	// Break: new rows of an update are not shown under their diff entry.
	const withRows = card(fixtures, "update-table-rows");
	withRows.diff[0].to_table = {
		columns: ["From"],
		rows: [{ cells: ["10:00"] }, { cells: ["11:00"] }],
	};
	const dt = draftCardView(withRows, {}).diff[0].table;
	add(
		"diff to_table",
		[dt.label, dt.columns, dt.rows],
		["New rows", ["From"], [["10:00"], ["11:00"]]]
	);
	add(
		"diff without to_table has none",
		draftCardView(card(fixtures, "update-diff"), {}).diff[0].table,
		null
	);

	// Break: odd rows / tables / diff parts on a readable draft throw or hide the card.
	const junk = { draft: { verb: "create", values: {} }, tables: "nope", rows: 7, diff: {} };
	add(
		"junk display parts leave a readable card",
		[draftCardView(junk, {}).state, draftCardView(junk, {}).rows],
		["S1", []]
	);

	// Break: a long table is not bounded per surface, or "more" miscounts.
	const long = {
		label: "Rows",
		count: 12,
		columns: ["N"],
		rows: Array.from({ length: 12 }, (_, i) => ({ cells: [String(i)] })),
	};
	ts.tables = [long];
	add(
		"rows shown and more, desktop vs phone",
		["desktop", "phone"].map((surface) => {
			const x = draftCardView(ts, { surface }).tables[0];
			return [x.rows.length, x.more];
		}),
		[
			[8, 4],
			[5, 7],
		]
	);

	// Break: an empty value is drawn as "null" or "-".
	const holes = card(fixtures, "create-simple");
	holes.rows = [
		{ label: "A", value: null },
		{ label: "B", value: "" },
		{ label: "C", value: "-" },
		{ label: "D", value: "x" },
	];
	add("empty values are skipped", draftCardView(holes, {}).rows, [{ label: "D", value: "x" }]);

	// Break: an update of a child table shows "3 rows -> 3 rows" or hides a removal.
	const tableDiff = (rc, extra = {}) => {
		const c = card(fixtures, "update-table-rows");
		c.diff[0] = { ...c.diff[0], ...extra, row_changes: rc };
		return draftCardView(c, { surface: "desktop" }).diff[0];
	};
	const rcv = draftCardView(card(fixtures, "update-table-rows"), { surface: "desktop" }).diff[0];
	add("update-table-rows row changes", rcv.rowChanges, {
		columns: ["From Time", "Hrs"],
		summary: "1 row changed, 1 row added, 1 row will be removed, 1 row unchanged",
		rows: [
			{
				status: "kept",
				statusLabel: "",
				note: "",
				cells: [
					{ text: "09:00:00", was: "" },
					{ text: "1.5", was: "" },
				],
			},
			{
				status: "changed",
				statusLabel: "Changed",
				note: "",
				cells: [
					{ text: "11:00:00", was: "10:30:00" },
					{ text: "2.0", was: "" },
				],
			},
			{
				status: "new",
				statusLabel: "New",
				note: "",
				cells: [
					{ text: "14:00:00", was: "" },
					{ text: "2.0", was: "" },
				],
			},
		],
		removed: [{ cells: ["13:00:00", "0.5"] }],
		more: "",
		reordered: false,
		unverified: false,
		verifyNote: "",
		keptLabel: "Unchanged",
		notShown: "",
	});
	add("row changes hide the plain row text", [rcv.old, rcv.new], ["", ""]);

	// Break: a summary part has the wrong wording, order or plural, or a zero part appears.
	const word = (counts) => {
		const some = (n, o) => Array.from({ length: n }, () => ({ cells: ["a"], ...o }));
		const rows = [
			...some(counts.changed, { status: "changed", was: ["b"] }),
			...some(counts.new, { status: "new" }),
			...some(counts.kept, {}),
		];
		return tableDiff({ rows, removed: some(counts.removed, {}), counts }).rowChanges.summary;
	};
	for (const [counts, expected] of [
		[{ changed: 1, new: 0, kept: 0, removed: 0 }, "1 row changed"],
		[{ changed: 3, new: 0, kept: 0, removed: 0 }, "3 rows changed"],
		[{ changed: 0, new: 1, kept: 0, removed: 0 }, "1 row added"],
		[{ changed: 0, new: 2, kept: 0, removed: 0 }, "2 rows added"],
		[{ changed: 0, new: 0, kept: 0, removed: 1 }, "1 row will be removed"],
		[{ changed: 0, new: 0, kept: 0, removed: 4 }, "4 rows will be removed"],
		[{ changed: 0, new: 0, kept: 1, removed: 0 }, "1 row unchanged"],
		[{ changed: 0, new: 0, kept: 3, removed: 0 }, "3 rows unchanged"],
		[
			{ changed: 2, new: 2, kept: 2, removed: 2 },
			"2 rows changed, 2 rows added, 2 rows will be removed, 2 rows unchanged",
		],
	])
		add(`summary ${JSON.stringify(counts)}`, word(counts), expected);
	// Break: a bad `counts` throws or reads 0 rows.
	const mixed = {
		rows: [{ status: "new", cells: ["a"] }, { status: "new", cells: ["b"] }, { cells: ["c"] }],
		removed: [{ cells: ["d"] }],
	};
	for (const counts of [undefined, null, 5, { changed: "x" }, { changed: -1, new: 1 }])
		add(
			`summary counted from rows when counts is ${JSON.stringify(counts)}`,
			tableDiff({ ...mixed, counts }).rowChanges.summary,
			"2 rows added, 1 row will be removed, 1 row unchanged"
		);

	// Break: the "more" line is worded wrong, or the bounded rows are not counted into it.
	const many = (n) => Array.from({ length: n }, (_, i) => ({ cells: [String(i)] }));
	for (const [more, rows, removed, expected] of [
		[{ rows: 3 }, 1, 1, ["", "and 3 more rows not shown"]],
		[{ removed: 1 }, 1, 1, ["and 1 more row will be removed", ""]],
		[
			{ rows: 2, removed: 5 },
			1,
			1,
			["and 5 more rows will be removed", "and 2 more rows not shown"],
		],
		[undefined, 1, 1, ["", ""]],
		[undefined, 10, 9, ["and 2 more unchanged rows, and 1 more row will be removed", ""]],
		[undefined, 9, 1, ["and 1 more unchanged row", ""]],
	]) {
		const rc = tableDiff({ rows: many(rows), removed: many(removed), more }).rowChanges;
		add(`more ${JSON.stringify([more, rows, removed])}`, [rc.more, rc.notShown], expected);
	}
	const phone = (() => {
		const c = card(fixtures, "update-table-rows");
		c.diff[0].row_changes = { rows: many(7), removed: many(7) };
		return draftCardView(c, { surface: "phone" }).diff[0].rowChanges;
	})();
	add(
		"rows and removed are capped separately on a phone",
		[phone.rows.length, phone.removed.length, phone.more],
		[5, 5, "and 2 more unchanged rows, and 2 more rows will be removed"]
	);

	// Break: odd row_changes throws, or hides the old text when there is nothing to draw instead.
	for (const rc of [5, "x", [], null, {}, { rows: null }, { rows: [], removed: [] }]) {
		const d = tableDiff(rc);
		add(
			`row_changes ${JSON.stringify(rc)} is null and the text stays`,
			[d.rowChanges, d.old, d.new],
			[null, "3 rows", "3 rows"]
		);
	}
	const oddRc = tableDiff({
		columns: ["A", "B", "C"],
		rows: [
			{ status: "weird", cells: ["x", { o: 1 }, null] },
			{ status: "changed", cells: ["y"], was: "nope" },
			{},
			7,
		],
		removed: [{}, { cells: "no" }, null],
		counts: 9,
	}).rowChanges;
	add("odd rows draw safely", oddRc, {
		columns: ["A"],
		summary: "1 row changed, 3 rows will be removed, 3 rows unchanged",
		rows: [
			{ status: "kept", statusLabel: "", note: "", cells: [{ text: "x", was: "" }] },
			{
				status: "changed",
				statusLabel: "Changed",
				note: "A value not shown here changes.",
				cells: [{ text: "y", was: "" }],
			},
			{ status: "kept", statusLabel: "", note: "", cells: [{ text: "", was: "" }] },
			{ status: "kept", statusLabel: "", note: "", cells: [{ text: "", was: "" }] },
		],
		removed: [{ cells: [""] }, { cells: [""] }, { cells: [""] }],
		more: "",
		reordered: false,
		unverified: false,
		verifyNote: "",
		keptLabel: "Unchanged",
		notShown: "",
	});
	// Break: a column holding only an old value, or only a removed row's value, is dropped.
	const keepCols = tableDiff({
		columns: ["Old", "Gone", "Empty"],
		rows: [{ status: "changed", cells: ["", "", ""], was: ["a", "", ""] }],
		removed: [{ cells: ["", "z", ""] }],
	}).rowChanges;
	add("a column is dropped only when empty everywhere", keepCols.columns, ["Old", "Gone"]);

	// Break: a hidden change is called unchanged, or struck through with nothing to strike.
	const inline = (rc) => {
		const c = card(fixtures, "update-diff");
		c.diff[0] = { label: "Items", from: "2 rows", to: "2 rows", row_changes: rc };
		return draftCardView(c, {}).diff[0].rowChanges;
	};
	const hid = inline({
		columns: ["Qty"],
		rows: [
			{ status: "changed", hidden_change: true, cells: ["5"], was: ["5"] },
			{ status: "kept", cells: ["1"] },
		],
	});
	add(
		"hidden_change row is Changed with a note and no was",
		[hid.rows[0].statusLabel, hid.rows[0].note, hid.rows[0].cells, hid.rows[1].note],
		["Changed", "A value not shown here changes.", [{ text: "5", was: "" }], ""]
	);
	add("hidden_change counts as changed", hid.summary, "1 row changed, 1 row unchanged");
	// Break: a truthy-but-not-true flag from a bad payload turns a note or a reorder on.
	const loose = inline({
		rows: [{ status: "changed", hidden_change: "yes", cells: ["a"], was: ["b"] }],
		reordered: 1,
	});
	add(
		"new flags need === true",
		[loose.rows[0].note, loose.rows[0].cells[0].was, loose.reordered, loose.summary],
		["", "b", false, "1 row changed"]
	);
	// Break: the reorder part is missing, misplaced, or wrongly capitalised.
	const order = (rows, reordered) => inline({ rows, reordered, removed: [] }).summary;
	const two = [
		{ status: "kept", cells: ["a"] },
		{ status: "kept", cells: ["b"] },
	];
	add(
		"reordered at the end",
		order([{ status: "changed", cells: ["a"] }, ...two], true),
		"1 row changed, 2 rows unchanged, row order changes"
	);
	add(
		"reordered after removals",
		inline({ rows: two, removed: [{ cells: ["x"] }], reordered: true }).summary,
		"1 row will be removed, 2 rows unchanged, row order changes"
	);
	add("not reordered adds nothing", order(two, false), "2 rows unchanged");
	// Break: a table of only unchanged rows is hidden although the user asked for it.
	add(
		"an all-kept table is drawn",
		[
			inline({ rows: two, removed: [], counts: { changed: 0, new: 0, kept: 2, removed: 0 } })
				.summary,
			inline({ rows: two }).rows.length,
		],
		["2 rows unchanged", 2]
	);

	// Break: with many rows and one change deep in the list, the cap hides the change.
	const fifty = Array.from({ length: 50 }, (_, i) =>
		i === 29
			? { status: "changed", cells: ["row 30"], was: ["old"] }
			: { status: "kept", cells: [`row ${i + 1}`] }
	);
	for (const [surface, shown] of [
		["desktop", 8],
		["phone", 5],
	]) {
		const c = card(fixtures, "update-diff");
		c.diff[0] = { label: "Items", row_changes: { columns: ["Item"], rows: fifty } };
		const rc = draftCardView(c, { surface }).diff[0].rowChanges;
		add(
			`50 rows, one change at row 30, on ${surface}`,
			[
				rc.rows.length,
				rc.rows.some((r) => r.cells[0].text === "row 30"),
				rc.more,
				rc.summary,
			],
			[
				shown,
				true,
				`and ${50 - shown} more unchanged rows`,
				"1 row changed, 49 rows unchanged",
			]
		);
	}
	// Break: changed rows are cut by the cap, or kept rows are not shown in the call's order.
	const twelve = Array.from({ length: 12 }, (_, i) => ({
		status: "changed",
		cells: [`c${i}`],
		was: ["x"],
	}));
	const cap = card(fixtures, "update-diff");
	cap.diff[0] = {
		label: "Items",
		row_changes: {
			columns: ["Item"],
			rows: [{ cells: ["k0"] }, ...twelve, { cells: ["k1"] }],
		},
	};
	const capped = draftCardView(cap, { surface: "phone" }).diff[0].rowChanges;
	add(
		"12 changed rows on a phone are all shown, no kept row fills",
		[capped.rows.length, capped.more],
		[12, "and 2 more unchanged rows"]
	);
	cap.diff[0].row_changes.rows = [{ cells: ["k0"] }, twelve[0], { cells: ["k1"] }, twelve[1]];
	add(
		"kept rows fill the cap in the call's order",
		draftCardView(cap, { surface: "phone" }).diff[0].rowChanges.rows.map(
			(r) => r.cells[0].text
		),
		["k0", "c0", "k1", "c1"]
	);
	// Break: the sentence says fewer rows than the lists show.
	const lists = (counts, more) =>
		tableDiff({
			rows: [{ status: "new", cells: ["a"] }],
			removed: [{ cells: ["x"] }, { cells: ["y"] }],
			counts,
			more,
		}).rowChanges.summary;
	add(
		"counts.removed 0 with 2 removed rows",
		lists({ changed: 0, new: 1, kept: 0, removed: 0 }),
		"1 row added, 2 rows will be removed"
	);
	add(
		"counts.removed 1 with 2 removed rows",
		lists({ changed: 0, new: 1, kept: 0, removed: 1 }),
		"1 row added, 2 rows will be removed"
	);
	add(
		"counts.removed 5 with 2 shown and more 3",
		lists({ changed: 0, new: 1, kept: 0, removed: 5 }, { removed: 3 }),
		"1 row added, 5 rows will be removed"
	);
	// Break: a whitespace-only change shows "Changed" with no reason.
	const ws = inline({
		columns: ["Name"],
		rows: [{ status: "changed", cells: [" Bob "], was: ["Bob"] }],
	});
	add(
		"a changed row with equal shown cells gets the note",
		[ws.rows[0].statusLabel, ws.rows[0].note, ws.rows[0].cells],
		["Changed", "A value not shown here changes.", [{ text: "Bob", was: "" }]]
	);
	// Break: a middle column empty everywhere shifts was out of line with cells, or the note row spans wrong.
	const mid = inline({
		columns: ["A", "B", "C"],
		rows: [
			{ status: "changed", hidden_change: true, cells: ["a", "", "c"], was: ["a", "", "c"] },
			{ status: "changed", cells: ["a2", "", "c2"], was: ["a1", "", "c1"] },
		],
	});
	add(
		"an empty middle column is dropped from cells and was alike",
		[mid.columns, mid.rows[1].cells, mid.columns.length + 1],
		[
			["A", "C"],
			[
				{ text: "a2", was: "a1" },
				{ text: "c2", was: "c1" },
			],
			3,
		]
	);

	// Break: a table the server could not fully compare is shown as plainly unchanged.
	const unv = (flag) =>
		inline({ columns: ["A"], rows: [{ status: "kept", cells: ["a"] }], unverified: flag });
	const unvShape = (rc) => [rc.unverified, rc.verifyNote, rc.keptLabel, rc.summary];
	add("unverified true", unvShape(unv(true)), [
		true,
		"This table could not be fully compared. Check its rows before you save.",
		"Not compared",
		"1 row unchanged",
	]);
	for (const flag of [undefined, false, "yes", 1])
		add(`unverified ${JSON.stringify(flag)} says nothing`, unvShape(unv(flag)), [
			false,
			"",
			"Unchanged",
			"1 row unchanged",
		]);

	// Break: the multi-line flag is lost, or set for a plain value.
	add(
		"multiline flag passes through",
		[
			tableDiff(null, { multiline: true }).multiline,
			tableDiff(null, { multiline: "yes" }).multiline,
			tableDiff(null).multiline,
		],
		[true, false, false]
	);
	// Break: a card without row_changes changed shape (to_table and plain text still hold).
	const plain = card(fixtures, "update-table-rows");
	delete plain.diff[0].row_changes;
	plain.diff[0].to_table = { count: 1, columns: ["N"], rows: [{ cells: ["1"] }] };
	const pv = draftCardView(plain, {}).diff[0];
	add(
		"without row_changes the old text and to_table stay",
		[pv.rowChanges, pv.old, pv.new, pv.table.label, pv.table.rows],
		[null, "3 rows", "3 rows", "New rows", [["1"]]]
	);

	// Break: an update draws no diff or the wrong primary; a create draws a diff.
	const up = draftCardView(card(fixtures, "update-diff"), {});
	add("update diff", up.diff, [
		{
			label: "Status",
			old: "Open",
			new: "Closed",
			multiline: false,
			rowChanges: null,
			table: null,
		},
	]);
	add("update primary", up.buttons[0].label, "Save changes");
	add("create has no diff", draftCardView(card(fixtures, "create-simple"), {}).diff, []);

	// Break: the raw tool name, or the record's own title, becomes the card title.
	const named = card(fixtures, "update-diff");
	named.draft.title = "update_doc";
	add(
		"update title is doctype and name, not the record title",
		draftCardView(card(fixtures, "update-diff"), {}).title,
		"ToDo TD-0001"
	);
	add("a tool name never becomes the title", draftCardView(named, {}).title, "ToDo TD-0001");
	add("create title", draftCardView(ok, {}).title, "New ToDo");
	// Break: a State Table state is never exercised, so its look is undefined.
	add("every card state is covered", [...seen].sort(), [
		"S1",
		"S11",
		"S12",
		"S13",
		"S2",
		"S3",
		"S4",
	]);

	return cases;
}
