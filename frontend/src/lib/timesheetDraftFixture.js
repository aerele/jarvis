// A Timesheet create draft as the app really meets it, for the specs of the server draft card's
// form and Preview (not part of the app bundle). Shape taken from a real run (the draft in
// docs/superpowers/plans/2026-11-16-tool-drafts-scripts/b2-browser/scripts/ts12.json): the model
// sent three time logs with only the times, a project and a description; the server's trial
// filled Hrs and an Is Billable default, which the card shows. The child DocType's columns and
// read-only flags are those of ERPNext's "Timesheet Detail": `hours` and `is_billable` are NOT
// read-only (only the billing and costing amounts are), which is why a rule that fills calculated
// cells only in read-only columns leaves them blank.
import { panelField } from "./docFields.js";
import { buildDraftTable } from "./draftTables.js";

const col = (fieldname, label, fieldtype, extra = {}) => ({
	fieldname,
	label,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});

export const TIMESHEET_DETAIL = {
	label: "Time Sheets",
	child_doctype: "Timesheet Detail",
	columns: [
		col("activity_type", "Activity Type", "Link", { options: "Activity Type" }),
		col("from_time", "From Time", "Datetime"),
		col("hours", "Hrs", "Float"),
		col("project", "Project", "Link", { options: "Project" }),
		col("is_billable", "Is Billable", "Check"),
	],
	extra_columns: [
		col("to_time", "To Time", "Datetime"),
		col("description", "Description", "Small Text"),
		col("billing_amount", "Billing Amount", "Currency", { read_only: 1 }),
	],
	form_fields: [],
};

export const TIMESHEET_LOGS = [
	{
		description: "Module build",
		from_time: "2026-12-08 09:30:00",
		to_time: "2026-12-08 12:30:00",
		project: "PROJ-0001",
	},
	{
		description: "Review",
		from_time: "2026-12-08 13:30:00",
		to_time: "2026-12-08 15:00:00",
		project: "PROJ-0001",
	},
	{
		description: "Fixes",
		from_time: "2026-12-08 15:00:00",
		to_time: "2026-12-08 17:00:00",
		project: "PROJ-0001",
	},
];

// What the server's draft card carries for it (draft_card.py): `rows` formatted for the person's
// date format, `tables` with the list-view columns and the trial's computed cells.
export function timesheetCard() {
	return {
		kind: "create",
		doctype: "Timesheet",
		name: null,
		rows: [
			{ label: "Series", value: "TS-.YYYY.-" },
			{ label: "Employee", value: "HR-EMP-00001" },
			{ label: "Project", value: "PROJ-0001" },
			{ label: "Start Date", value: "08-12-2026" },
		],
		tables: [
			{
				label: "Time Sheets",
				count: 3,
				columns: ["From Time", "Hrs", "Project", "Is Billable", "Description", "To Time"],
				fieldnames: [
					"from_time",
					"hours",
					"project",
					"is_billable",
					"description",
					"to_time",
				],
				rows: [
					{
						cells: [
							"08-12-2026 09:30:00",
							"3",
							"PROJ-0001",
							"No",
							"Module build",
							"08-12-2026 12:30:00",
						],
					},
					{
						cells: [
							"08-12-2026 13:30:00",
							"1.50",
							"PROJ-0001",
							"No",
							"Review",
							"08-12-2026 15:00:00",
						],
					},
					{
						cells: [
							"08-12-2026 15:00:00",
							"2",
							"PROJ-0001",
							"No",
							"Fixes",
							"08-12-2026 17:00:00",
						],
					},
				],
				extra: 0,
				extra_columns: 0,
				unknown_columns: 0,
				unknown_fields: [],
			},
		],
		draft: {
			verb: "create",
			doctype: "Timesheet",
			name: "",
			title: "New Timesheet",
			values: {
				naming_series: "TS-.YYYY.-",
				employee: "HR-EMP-00001",
				parent_project: "PROJ-0001",
				start_date: "2026-12-08",
				time_logs: TIMESHEET_LOGS.map((r) => ({ doctype: "Timesheet Detail", ...r })),
			},
			ignored: [],
			dropped: [],
			continue: false,
		},
	};
}

// The model the builder gives that card (the real field and table builders).
export function timesheetModel() {
	const f = (fieldname, label, fieldtype, value) => ({
		...panelField(col(fieldname, label, fieldtype), value),
		proposed: true,
	});
	return {
		verb: "create",
		doctype: "Timesheet",
		fields: [
			f("naming_series", "Series", "Data", "TS-.YYYY.-"),
			f("employee", "Employee", "Link", "HR-EMP-00001"),
			f("parent_project", "Project", "Link", "PROJ-0001"),
			f("start_date", "Start Date", "Date", "2026-12-08"),
		],
		tables: [
			buildDraftTable({
				fieldname: "time_logs",
				spec: TIMESHEET_DETAIL,
				required: true,
				proposed: TIMESHEET_LOGS,
				verb: "create",
			}),
		],
		applying: false,
		error: "",
	};
}
