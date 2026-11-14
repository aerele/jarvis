/**
 * Golden jarvis-action blocks shared by the desktop (vitest) and phone (node:test)
 * suites. `block` is the raw text between the fences and may be invalid JSON.
 * `expect` is what a client must do with it: show a "card" or the "error" view.
 *
 * Relative-import friendly and dependency-free: the phone app's node tests load it.
 */
const rows = [
	{ time: "2026-11-02", hours: 2 },
	{ time: "2026-11-03", hours: 3 },
	{ time: "2026-11-04", hours: 4 },
];
const fields = [{ label: "employee", value: "HR-EMP-1" }];
const base = { kind: "doc", doctype: "Timesheet" };
const block = (o) => JSON.stringify(o);

export const ACTION_FIXTURES = [
	{
		id: "canonical",
		block: block({
			...base,
			verb: "create",
			fields,
			tables: [{ fieldname: "time_logs", rows }],
		}),
		expect: "card",
		tableRows: 3,
	},
	{
		id: "tables-object-list",
		block: block({ ...base, verb: "create", fields, tables: { time_logs: rows } }),
		expect: "card",
		tableRows: 3,
	},
	{
		id: "tables-object-rows",
		block: block({ ...base, verb: "create", fields, tables: { time_logs: { rows } } }),
		expect: "card",
		tableRows: 3,
	},
	{
		id: "fields-object",
		block: block({ ...base, verb: "create", fields: { employee: "HR-EMP-1" } }),
		expect: "card",
	},
	{
		id: "kind-missing",
		block: block({ doctype: "Timesheet", verb: "create", fields }),
		expect: "card",
	},
	{
		id: "verb-capitalised",
		block: block({ ...base, verb: "Create", fields }),
		expect: "card",
	},
	{
		id: "verb-missing",
		block: block({ ...base, fields }),
		expect: "card",
	},
	{
		id: "verb-unknown",
		block: block({ ...base, verb: "draft", fields }),
		expect: "error",
	},
	{
		id: "docs-batch",
		block: block({ kind: "doc", verb: "create", docs: [{ doctype: "Timesheet", fields }] }),
		expect: "error",
	},
	{
		id: "create-empty",
		block: block({ ...base, verb: "create" }),
		expect: "error",
	},
	{
		id: "tables-string",
		block: block({ ...base, verb: "create", fields, tables: "time_logs" }),
		expect: "error",
	},
	{
		id: "tables-object-bad-rows",
		block: block({ ...base, verb: "create", fields, tables: { time_logs: "x" } }),
		expect: "error",
	},
	{
		id: "fields-bad-items",
		block: block({
			...base,
			verb: "create",
			fields: [null, { label: "employee", value: "E" }],
		}),
		expect: "error",
	},
	{
		id: "tables-bad-items",
		block: block({ ...base, verb: "create", fields, tables: [null, 5] }),
		expect: "error",
	},
	{ id: "json-broken", block: '{"kind":"doc",', expect: "error" },
	{ id: "json-array", block: "[1,2]", expect: "error" },
];
