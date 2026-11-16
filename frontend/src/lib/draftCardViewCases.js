// The checks for draftCardView, written once. The vitest spec and the PWA's `node --test`
// both run these, so the two runners cannot drift. Each case is {name, actual, expected}
// and the runner only compares them with a deep equal.
//
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

export function draftCardCases(draftCardView, fixtures) {
	const cases = [];
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

	// Break: Confirm / Discard changes, or Edit / Preview sneak into a sensitive card, on a surface.
	for (const surface of ["desktop", "phone"]) {
		const s = draftCardView(card(fixtures, "sensitive"), { surface });
		add(`S11 buttons on ${surface}`, shape(s), [
			btn("create", "Confirm", { primary: true }),
			btn("cancel", "Discard"),
		]);
		const u = draftCardView(card(fixtures, "unreadable"), { surface });
		add(`S12 buttons on ${surface}`, shape(u), [btn("cancel", "Cancel")]);
	}

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
	add(
		"a busy sensitive card cannot be pressed twice",
		draftCardView(card(fixtures, "sensitive"), { busy: "create" }).buttons.map(
			(b) => b.disabled
		),
		[true, true]
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

	// Break: an empty column (Activity Type) or an unset flag (Is Billable "No") is drawn, or a real
	// zero / non-default value is dropped.
	const ts = card(fixtures, "create-simple");
	ts.tables = [TIMESHEET_TABLE];
	const t = draftCardView(ts, {}).tables[0];
	add(
		"empty and all-No columns are dropped",
		[t.columns, t.rows],
		[
			["From Time", "Hrs"],
			[
				["10:00", "3"],
				["11:00", "1.50"],
			],
		]
	);
	const mixed = structuredClone(TIMESHEET_TABLE);
	mixed.rows[1].cells[3] = "Yes";
	ts.tables = [mixed];
	add(
		"a column with one non-default cell keeps its No cells",
		draftCardView(ts, {}).tables[0].rows.map((r) => r[2]),
		["No", "Yes"]
	);
	const zero = structuredClone(TIMESHEET_TABLE);
	zero.rows.forEach((r) => (r.cells[2] = "0"));
	ts.tables = [zero];
	add("zero is kept", draftCardView(ts, {}).tables[0].columns, ["From Time", "Hrs"]);
	add(
		"a table of only empty or No columns keeps its first column",
		draftCardView(
			{
				...ts,
				tables: [
					{ ...TIMESHEET_TABLE, columns: ["A", "B"], rows: [{ cells: ["", "No"] }] },
				],
			},
			{}
		).tables[0].columns,
		["A"]
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

	// Break: an update draws no diff or the wrong primary; a create draws a diff.
	const up = draftCardView(card(fixtures, "update-diff"), {});
	add("update diff", up.diff, [{ label: "Status", old: "Open", new: "Closed" }]);
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
	add(
		"no fixture view contains a tool name",
		fixtures.some((f) => /_doc\b/.test(JSON.stringify(draftCardView(f.card, {})))),
		false
	);

	return cases;
}
