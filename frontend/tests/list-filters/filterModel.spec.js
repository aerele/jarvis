import { describe, it, expect } from "vitest";
import {
	URL_PARAM,
	schemaIndex,
	fieldOptions,
	fieldKey,
	limitsOf,
	clauseForEntry,
	retargetClause,
	setOperator,
	makeClause,
	controlFor,
	selectControlOptions,
	linkTarget,
	isComplete,
	toWire,
	activeCount,
	operatorLabel,
	serializeClauses,
	parseClauseParam,
	reconcileClauses,
	droppedNotice,
	filterErrorInfo,
	attributeError,
	isUrlPayloadTooLarge,
	skippedNotice,
	OPERATORS,
	timespanLabel,
	toDatetimeInput,
	fromDatetimeInput,
	MAX_URL_CHARS,
} from "@/components/list/filterModel";

// The entries come from fixtures.js, which mirrors jarvis/chat/list_filters.py
// `_entry()` exactly — the same doubles the panel specs mount against.
import {
	DESCRIPTION,
	ENABLED,
	SCOPE,
	OWNER,
	CREATION,
	IDX,
	STEP_PROMPT,
	SKILLS_SCHEMA,
} from "./fixtures.js";

const SCHEMA = {
	...SKILLS_SCHEMA,
	fields: [DESCRIPTION, ENABLED, SCOPE, OWNER, CREATION, IDX, STEP_PROMPT],
};

const index = schemaIndex(SCHEMA);

describe("field catalog", () => {
	it("identifies a field by (doctype, fieldname), so a child field is not the parent's", () => {
		expect(index.get(fieldKey("Jarvis Custom Skill", "description"))).toBe(DESCRIPTION);
		expect(index.get(fieldKey("Jarvis Macro Step", "prompt"))).toBe(STEP_PROMPT);
		expect(index.get(fieldKey("Jarvis Custom Skill", "prompt"))).toBeUndefined();
	});

	it("groups the picker by the section the SERVER named", () => {
		// The server files each field: the list's own name, "General" for the
		// generic standard fields, and the PARENT's Table-field label for a child
		// table (D15) — never the child DocType name, which the user has not seen.
		const withGroups = {
			...SCHEMA,
			fields: [
				{ ...DESCRIPTION, group: "Skills" },
				{ ...OWNER, group: "General" },
				{ ...STEP_PROMPT, group: "Steps", label: "Prompt (Steps)" },
			],
		};
		const groups = fieldOptions(withGroups);
		expect(groups.map((g) => g.group)).toEqual(["Skills", "General", "Steps"]);
		expect(groups[2].items[0].label).toBe("Prompt (Steps)");
	});

	it("falls back to the old derivation for a schema with no group marker", () => {
		const groups = fieldOptions(SCHEMA);
		expect(groups.map((g) => g.group)).toEqual(["Skills", "Jarvis Macro Step"]);
	});

	it("reads the server's limits, falling back when the schema is absent", () => {
		expect(limitsOf(SCHEMA).max_clauses).toBe(20);
		expect(limitsOf(null).max_in_values).toBe(100);
	});
});

describe("clause construction", () => {
	it("seeds a new row with the SERVER's default operator, not a hardcoded one", () => {
		expect(clauseForEntry(DESCRIPTION).operator).toBe("like");
		expect(clauseForEntry(CREATION).operator).toBe("Between");
		expect(clauseForEntry(ENABLED).operator).toBe("=");
	});

	it("gives Between two empty bounds and `in` a real list, never a comma string", () => {
		expect(clauseForEntry(CREATION).value).toEqual(["", ""]);
		expect(setOperator(clauseForEntry(DESCRIPTION), "in").value).toEqual([]);
	});

	it("keeps the operator when retargeting to a field that allows it, else falls back", () => {
		const like = clauseForEntry(DESCRIPTION);
		expect(retargetClause(like, STEP_PROMPT).operator).toBe("like");
		// Check offers only "="; `like` cannot survive the move.
		expect(retargetClause(like, ENABLED).operator).toBe("=");
		expect(retargetClause(like, ENABLED).value).toBe("");
	});

	it("promotes a scalar into a one-element list when switching to `in`", () => {
		const clause = { ...clauseForEntry(DESCRIPTION), value: "month end" };
		expect(setOperator(clause, "in").value).toEqual(["month end"]);
		// and back again, taking the first value rather than losing everything
		expect(setOperator(setOperator(clause, "in"), "=").value).toBe("month end");
	});

	it("does not carry a value across incompatible shapes", () => {
		const between = { ...clauseForEntry(CREATION), value: ["2026-01-01", "2026-02-01"] };
		expect(setOperator(between, "Timespan").value).toBe("");
	});

	it("sentence-cases a timespan for reading without touching the wire token", () => {
		expect(timespanLabel("last 7 days")).toBe("Last 7 days");
		expect(timespanLabel("this quarter")).toBe("This quarter");
		expect(timespanLabel("")).toBe("");
	});

	it("labels date comparisons the way Frappe does", () => {
		expect(operatorLabel(">", "Datetime")).toBe("after");
		expect(operatorLabel("<=", "Date")).toBe("on or before");
		expect(operatorLabel(">", "Int")).toBe("greater than");
		expect(operatorLabel("Between")).toBe("between");
	});
});

describe("control families", () => {
	it("picks one control per family", () => {
		expect(controlFor(ENABLED, "=")).toBe("check");
		expect(controlFor(SCOPE, "=")).toBe("select");
		expect(controlFor(OWNER, "=")).toBe("link");
		expect(controlFor(CREATION, "Between")).toBe("between-datetime");
		expect(controlFor(IDX, "=")).toBe("number");
		expect(controlFor(DESCRIPTION, "like")).toBe("text");
	});

	it("lets the operator win over the family", () => {
		expect(controlFor(ENABLED, "is")).toBe("is");
		expect(controlFor(CREATION, "Timespan")).toBe("timespan");
		expect(controlFor(OWNER, "in")).toBe("multi-link");
		expect(controlFor(SCOPE, "in")).toBe("multi-select");
		expect(controlFor(DESCRIPTION, "in")).toBe("multi");
	});

	it("renders a Select's leading blank as 'Not set' instead of an empty row", () => {
		expect(selectControlOptions(SCOPE)).toEqual([
			{ label: "Not set", value: "" },
			{ label: "Org", value: "Org" },
			{ label: "Personal", value: "Personal" },
		]);
	});

	it("degrades a dynamically-sourced Select to free text rather than showing a lie", () => {
		const dynamic = { ...SCOPE, options: "link:Jarvis Role" };
		expect(selectControlOptions(dynamic)).toEqual([]);
		expect(controlFor(dynamic, "=")).toBe("text");
	});

	it("knows which DocType a Link searches, and admits when it cannot", () => {
		expect(linkTarget(OWNER)).toBe("User");
		// Dynamic Link's options name the controlling FIELD, not a DocType.
		expect(linkTarget({ ...OWNER, fieldtype: "Dynamic Link" })).toBe("");
		expect(controlFor({ ...OWNER, fieldtype: "Dynamic Link", options: "ref_dt" }, "=")).toBe(
			"text"
		);
	});
});

describe("datetime-local ⇄ Frappe's datetime string", () => {
	// The browser control speaks "2026-01-01T09:30". `_between_bounds` only calls
	// get_datetime() when a bound contains a SPACE — a T-form bound falls through
	// to getdate(), which parses the date and DISCARDS the time, so a range from
	// 09:30 would silently become one from midnight.
	it("converts the input's T-form into what the compiler parses", () => {
		expect(fromDatetimeInput("2026-01-01T09:30")).toBe("2026-01-01 09:30:00");
		expect(fromDatetimeInput("2026-01-01T09:30:45")).toBe("2026-01-01 09:30:45");
		expect(fromDatetimeInput("2026-01-01 09:30:00")).toBe("2026-01-01 09:30:00");
	});

	it("converts a stored value back into what the input can display", () => {
		expect(toDatetimeInput("2026-01-01 09:30:00")).toBe("2026-01-01T09:30");
		expect(toDatetimeInput("2026-01-01T09:30")).toBe("2026-01-01T09:30");
	});

	it("leaves a blank blank and does not invent a value it cannot read", () => {
		expect(fromDatetimeInput("")).toBe("");
		expect(fromDatetimeInput(null)).toBe("");
		expect(toDatetimeInput("")).toBe("");
		expect(toDatetimeInput("not a date")).toBe("");
		// something unparseable is handed on untouched rather than silently zeroed:
		// the server is the authority on whether it is a date.
		expect(fromDatetimeInput("tomorrow")).toBe("tomorrow");
	});
});

describe("completeness — an incomplete clause is PENDING, not sent", () => {
	it("needs a value", () => {
		const clause = clauseForEntry(DESCRIPTION);
		expect(isComplete(clause, DESCRIPTION)).toBe(false);
		expect(isComplete({ ...clause, value: "month end" }, DESCRIPTION)).toBe(true);
	});

	it("needs BOTH Between bounds (D14-a: the server rejects a half-open range)", () => {
		const clause = clauseForEntry(CREATION);
		expect(isComplete({ ...clause, value: ["2026-01-01", ""] }, CREATION)).toBe(false);
		expect(isComplete({ ...clause, value: ["", "2026-01-01"] }, CREATION)).toBe(false);
		expect(isComplete({ ...clause, value: ["2026-01-01", "2026-02-01"] }, CREATION)).toBe(
			true
		);
	});

	it("treats a blank numeric as pending, not as the `= 0` the server would compile (D14)", () => {
		const clause = clauseForEntry(IDX);
		expect(isComplete({ ...clause, value: "" }, IDX)).toBe(false);
		expect(isComplete({ ...clause, value: "0" }, IDX)).toBe(true);
	});

	it("needs at least one non-blank value for in / not in", () => {
		const clause = setOperator(clauseForEntry(SCOPE), "in");
		expect(isComplete({ ...clause, value: [] }, SCOPE)).toBe(false);
		expect(isComplete({ ...clause, value: ["", "  "] }, SCOPE)).toBe(false);
		expect(isComplete({ ...clause, value: ["Org"] }, SCOPE)).toBe(true);
	});

	it("only accepts the two `is` tokens and a real timespan", () => {
		const is = setOperator(clauseForEntry(ENABLED), "is");
		expect(isComplete({ ...is, operator: "is", value: "set" }, ENABLED)).toBe(false); // "is" not in Check's operators
		const dateIs = { ...clauseForEntry(CREATION), operator: "is", value: "maybe" };
		expect(isComplete(dateIs, CREATION)).toBe(false);
		expect(isComplete({ ...dateIs, value: "not set" }, CREATION)).toBe(true);
		expect(isComplete({ ...dateIs, operator: "Timespan", value: "last week" }, CREATION)).toBe(
			true
		);
		expect(
			isComplete({ ...dateIs, operator: "Timespan", value: "last fortnight" }, CREATION)
		).toBe(false);
	});

	it("rejects an operator the schema does not offer for that field", () => {
		expect(
			isComplete({ ...clauseForEntry(ENABLED), operator: "like", value: "x" }, ENABLED)
		).toBe(false);
	});

	it("is false for a field that is not in this caller's catalog at all", () => {
		expect(isComplete({ ...clauseForEntry(DESCRIPTION), value: "x" }, null)).toBe(false);
	});
});

describe("the wire payload", () => {
	const clauses = [
		{ ...clauseForEntry(DESCRIPTION), value: "month end" },
		clauseForEntry(CREATION), // pending: no bounds
		{ ...setOperator(clauseForEntry(SCOPE), "in"), value: ["Org", "", "Personal"] },
	];

	it("sends complete clauses only, and counts only those", () => {
		expect(toWire(clauses, index)).toEqual([
			{
				doctype: "Jarvis Custom Skill",
				fieldname: "description",
				operator: "like",
				value: "month end",
			},
			{
				doctype: "Jarvis Custom Skill",
				fieldname: "scope",
				operator: "in",
				value: ["Org", "Personal"],
			},
		]);
		expect(activeCount(clauses, index)).toBe(2);
	});

	it("allows the SAME field twice, ANDed, in order", () => {
		const repeated = [
			{ ...clauseForEntry(DESCRIPTION), value: "month" },
			{ ...clauseForEntry(DESCRIPTION), operator: "not like", value: "draft" },
		];
		const wire = toWire(repeated, index);
		expect(wire).toHaveLength(2);
		expect(wire.map((c) => c.operator)).toEqual(["like", "not like"]);
		expect(activeCount(repeated, index)).toBe(2);
	});
});

describe("URL state", () => {
	it("round-trips through the one versioned param", () => {
		const clauses = [
			{ ...clauseForEntry(DESCRIPTION), value: "month end" },
			{ ...clauseForEntry(CREATION), value: ["2026-01-01", "2026-02-01"] },
		];
		const param = serializeClauses("skills", clauses, index);
		expect(URL_PARAM).toBe("fv2");
		expect(JSON.parse(param)).toEqual({
			v: 1,
			k: "skills",
			c: [
				["Jarvis Custom Skill", "description", "like", "month end"],
				["Jarvis Custom Skill", "creation", "Between", ["2026-01-01", "2026-02-01"]],
			],
		});
		const back = parseClauseParam(param, "skills", SCHEMA);
		expect(toWire(back.clauses, index)).toEqual(toWire(clauses, index));
	});

	it("writes nothing when no clause is complete", () => {
		expect(serializeClauses("skills", [clauseForEntry(CREATION)], index)).toBe("");
	});

	it("ignores a payload written for a sibling tab's list (C08-7)", () => {
		const param = serializeClauses(
			"macros",
			[{ ...clauseForEntry(DESCRIPTION), value: "x" }],
			index
		);
		expect(parseClauseParam(param, "skills", SCHEMA)).toBeNull();
		expect(parseClauseParam(param, "macros", SCHEMA).clauses).toHaveLength(1);
	});

	// P1-2: "could not read this" and "nothing here for us" are DIFFERENT
	// answers. Collapsing them into null is what made a mangled link show a
	// silently unfiltered list.
	it("marks junk, a wrong version and an oversized payload UNREADABLE, not empty", () => {
		for (const raw of [
			"not json",
			JSON.stringify({ v: 99, k: "skills", c: [] }),
			JSON.stringify({ v: 1, k: "skills" }),
			JSON.stringify([1, 2, 3]),
			JSON.stringify({ v: 1, k: "skills", c: [["a", "b", "=", "x".repeat(MAX_URL_CHARS)]] }),
		]) {
			const parsed = parseClauseParam(raw, "skills");
			expect(parsed).not.toBeNull();
			expect(parsed.unreadable).toBe(true);
			expect(parsed.clauses).toEqual([]);
		}
	});

	it("stays SILENT only when there is genuinely nothing addressed to us", () => {
		expect(parseClauseParam("", "skills")).toBeNull();
		expect(parseClauseParam(undefined, "skills")).toBeNull();
		// a sibling tab's payload: not ours to read and not ours to complain about
		expect(
			parseClauseParam(JSON.stringify({ v: 1, k: "learning", c: [] }), "skills")
		).toBeNull();
		// ...even on a contract version we do not know
		expect(
			parseClauseParam(JSON.stringify({ v: 9, k: "learning", c: [] }), "skills")
		).toBeNull();
	});

	it("knows when a payload is too large to be a URL", () => {
		expect(isUrlPayloadTooLarge("x".repeat(MAX_URL_CHARS))).toBe(false);
		expect(isUrlPayloadTooLarge("x".repeat(MAX_URL_CHARS + 1))).toBe(true);
		expect(isUrlPayloadTooLarge("")).toBe(false);
	});

	it("counts the rows it had to skip instead of dropping them silently", () => {
		const parsed = parseClauseParam(
			JSON.stringify({
				v: 1,
				k: "skills",
				c: [
					["Jarvis Custom Skill", "description", "like", "keep"],
					["Jarvis Custom Skill", "description", "DROP TABLE", "x"], // not an operator
					["Jarvis Custom Skill", "description"], // too short
				],
			}),
			"skills",
			SCHEMA
		);
		expect(parsed.clauses).toHaveLength(1);
		expect(parsed.skipped).toBe(2);
		expect(skippedNotice(parsed.skipped)).toMatch(/2 filters in this link were not valid/);
		expect(skippedNotice(0)).toBe("");
	});

	it("rejects an operator outside the closed vocabulary", () => {
		for (const op of ["DROP", "=;--", "BETWEEN", "Like"]) {
			const parsed = parseClauseParam(
				JSON.stringify({
					v: 1,
					k: "skills",
					c: [["Jarvis Custom Skill", "description", op, "x"]],
				}),
				"skills",
				SCHEMA
			);
			expect(parsed.clauses).toEqual([]);
			expect(parsed.skipped).toBe(1);
		}
		expect(OPERATORS).toContain("Between");
		expect(OPERATORS).toContain("like");
	});

	it("bounds what a hand-edited URL can inflate", () => {
		const rows = [];
		for (let i = 0; i < 40; i += 1)
			rows.push(["Jarvis Custom Skill", "description", "like", "x"]);
		const parsed = parseClauseParam(
			JSON.stringify({ v: 1, k: "skills", c: rows }),
			"skills",
			SCHEMA
		);
		expect(parsed.clauses).toHaveLength(20); // schema limits.max_clauses
		expect(parsed.skipped).toBe(20); // and the overflow is REPORTED
		const long = JSON.stringify({
			v: 1,
			k: "skills",
			c: [["Jarvis Custom Skill", "description", "like", "y".repeat(2000)]],
		});
		expect(parseClauseParam(long, "skills", SCHEMA).clauses[0].value).toHaveLength(1000);
	});

	it("skips structurally broken rows without losing the good ones", () => {
		const parsed = parseClauseParam(
			JSON.stringify({
				v: 1,
				k: "skills",
				c: [
					["Jarvis Custom Skill", "description"], // too short
					"nope",
					["", "description", "like", "x"], // no doctype
					["Jarvis Custom Skill", "description", "like", "keep me"],
				],
			}),
			"skills",
			SCHEMA
		);
		expect(parsed.clauses).toHaveLength(1);
		expect(parsed.clauses[0].value).toBe("keep me");
		expect(parsed.skipped).toBe(3);
	});
});

describe("reconciliation against this caller's catalog (plan §8 steps 3-4)", () => {
	it("keeps what is filterable and hands back what is not", () => {
		const clauses = [
			{ ...clauseForEntry(DESCRIPTION), value: "x" },
			makeClause({
				doctype: "Jarvis Custom Skill",
				fieldname: "skill_bundle", // permlevel-1: absent from a plain user's schema
				operator: "like",
				value: "secret",
			}),
			// a real field with an operator it does not offer (schema changed under us)
			makeClause({
				doctype: "Jarvis Custom Skill",
				fieldname: "enabled",
				operator: "like",
				value: "1",
			}),
		];
		const { kept, dropped } = reconcileClauses(clauses, index);
		expect(kept).toHaveLength(1);
		expect(dropped.map((c) => c.fieldname)).toEqual(["skill_bundle", "enabled"]);
	});

	it("says how many were dropped, in words a person can read", () => {
		expect(droppedNotice([])).toBe("");
		expect(droppedNotice([{}])).toMatch(/^1 filter from this link is no longer available/);
		expect(droppedNotice([{}, {}])).toMatch(
			/^2 filters from this link are no longer available/
		);
	});
});

describe("server error codes → what the panel does", () => {
	// The exact shape frappe-ui's `call` builds for a deliberate 4xx: the human
	// message from _server_messages, then the response body's `message` key —
	// our {ok:false, error:{code,message}} envelope — left as an OBJECT because
	// its JSON.parse attempt fails.
	function thrownBy(code, message) {
		const e = new Error("list endpoint");
		e.status = 417;
		e.messages = [message, { ok: false, error: { code, message } }];
		return e;
	}

	it("digs the stable code out of e.messages", () => {
		const info = filterErrorInfo(
			thrownBy("list_filter_invalid_value", "Created On needs a value.")
		);
		expect(info).toEqual({
			code: "list_filter_invalid_value",
			kind: "row",
			message: "Created On needs a value.",
		});
	});

	it("gives an over-expensive query its own kind, not a validation kind", () => {
		// "narrow this" is a different instruction to the user than "fix this",
		// and retrying an unchanged expensive filter is futile — so it is neither
		// a row error nor transient.
		const info = filterErrorInfo(
			thrownBy(
				"list_filter_query_too_expensive",
				"That filter is too broad to run on this list."
			)
		);
		expect(info.kind).toBe("cost");
		expect(info.message).toMatch(/too broad/);
	});

	it("classifies every code the compiler can raise", () => {
		const kinds = {
			list_filter_query_too_expensive: "cost",
			list_filter_unknown_field: "schema",
			list_filter_invalid_operator: "schema",
			list_filter_view_not_filterable: "schema",
			list_filter_too_many_clauses: "cap",
			list_filter_too_many_values: "cap",
			list_filter_invalid_value: "row",
			list_filter_value_too_long: "row",
			list_filter_bad_payload: "row",
			list_filter_schema_unavailable: "transient",
		};
		for (const [code, kind] of Object.entries(kinds)) {
			expect(filterErrorInfo(thrownBy(code, "x")).kind).toBe(kind);
		}
	});

	it("also reads the envelope when it arrives as a resolved value or a JSON string", () => {
		expect(
			filterErrorInfo({
				ok: false,
				error: { code: "list_filter_too_many_clauses", message: "cap" },
			}).kind
		).toBe("cap");
		const e = new Error("x");
		e.messages = [
			JSON.stringify({
				ok: false,
				error: { code: "list_filter_bad_payload", message: "b" },
			}),
		];
		expect(filterErrorInfo(e).code).toBe("list_filter_bad_payload");
	});

	it("leaves an ordinary failure alone, so it keeps its existing toast", () => {
		const e = new Error("boom");
		e.messages = ["Something went wrong."];
		expect(filterErrorInfo(e)).toBeNull();
		expect(filterErrorInfo(null)).toBeNull();
		expect(filterErrorInfo({ ok: false, error: { code: "not_ours" } })).toBeNull();
	});

	it("falls back to its own copy when the server sends a blank message", () => {
		expect(filterErrorInfo(thrownBy("list_filter_schema_unavailable", "")).message).toBe(
			"Filters are unavailable for this list right now."
		);
	});
});

describe("attributing a row-level rejection", () => {
	const clauses = [
		{ ...clauseForEntry(DESCRIPTION), value: "x" },
		{ ...clauseForEntry(CREATION), value: ["2026-01-01", ""] },
	];

	it("blames the row whose field label the server named", () => {
		const error = {
			code: "list_filter_invalid_value",
			kind: "row",
			message: "Created On needs a start and an end.",
		};
		expect(attributeError(error, clauses, index)).toBe(clauses[1].id);
	});

	// P3-1: a substring test blames a one-letter label for every message that
	// happens to contain that letter.
	it("matches on word boundaries, not substrings", () => {
		const shortLabel = { ...DESCRIPTION, fieldname: "note", label: "e" };
		const shortIndex = schemaIndex({ ...SCHEMA, fields: [shortLabel] });
		const rows = [{ ...clauseForEntry(shortLabel), value: "x" }];
		const error = {
			kind: "row",
			code: "list_filter_invalid_value",
			message: "Qty must be a number.",
		};
		expect(attributeError(error, rows, shortIndex)).toBeNull();
		// but it still finds the label when the message really names it
		expect(attributeError({ ...error, message: "e needs a value." }, rows, shortIndex)).toBe(
			rows[0].id
		);
	});

	it("survives a label full of regex punctuation", () => {
		const child = { ...STEP_PROMPT, label: "Prompt (Jarvis Macro Step)" };
		const childIndex = schemaIndex({ ...SCHEMA, fields: [child] });
		const rows = [{ ...clauseForEntry(child), value: "x" }];
		const error = {
			kind: "row",
			code: "list_filter_invalid_value",
			message: "Prompt (Jarvis Macro Step) needs a value.",
		};
		expect(attributeError(error, rows, childIndex)).toBe(rows[0].id);
	});

	it("blames nobody when the label is ambiguous or absent", () => {
		const twice = [clauses[1], { ...clauses[1], id: "other" }];
		const error = {
			code: "list_filter_invalid_value",
			kind: "row",
			message: "Created On needs a start and an end.",
		};
		expect(attributeError(error, twice, index)).toBeNull();
		expect(attributeError({ ...error, message: "Nope." }, clauses, index)).toBeNull();
		expect(attributeError({ ...error, kind: "cap" }, clauses, index)).toBeNull();
		expect(attributeError(null, clauses, index)).toBeNull();
	});
});
