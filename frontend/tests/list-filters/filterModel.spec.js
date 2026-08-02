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

const SCHEMA = { ...SKILLS_SCHEMA, fields: [DESCRIPTION, ENABLED, SCOPE, OWNER, CREATION, IDX, STEP_PROMPT] };

const index = schemaIndex(SCHEMA);

describe("field catalog", () => {
	it("identifies a field by (doctype, fieldname), so a child field is not the parent's", () => {
		expect(index.get(fieldKey("Jarvis Custom Skill", "description"))).toBe(DESCRIPTION);
		expect(index.get(fieldKey("Jarvis Macro Step", "prompt"))).toBe(STEP_PROMPT);
		expect(index.get(fieldKey("Jarvis Custom Skill", "prompt"))).toBeUndefined();
	});

	it("groups the picker parent-first, then one group per child DocType", () => {
		const groups = fieldOptions(SCHEMA);
		expect(groups.map((g) => g.group)).toEqual(["Skills", "Jarvis Macro Step"]);
		expect(groups[0].items.map((i) => i.label)).toContain("Description");
		expect(groups[1].items[0].label).toBe("Prompt (Jarvis Macro Step)");
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
		expect(controlFor({ ...OWNER, fieldtype: "Dynamic Link", options: "ref_dt" }, "=")).toBe("text");
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
		expect(isComplete({ ...clause, value: ["2026-01-01", "2026-02-01"] }, CREATION)).toBe(true);
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
		expect(isComplete({ ...dateIs, operator: "Timespan", value: "last week" }, CREATION)).toBe(true);
		expect(isComplete({ ...dateIs, operator: "Timespan", value: "last fortnight" }, CREATION)).toBe(
			false
		);
	});

	it("rejects an operator the schema does not offer for that field", () => {
		expect(isComplete({ ...clauseForEntry(ENABLED), operator: "like", value: "x" }, ENABLED)).toBe(
			false
		);
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
		const param = serializeClauses("macros", [{ ...clauseForEntry(DESCRIPTION), value: "x" }], index);
		expect(parseClauseParam(param, "skills", SCHEMA)).toBeNull();
		expect(parseClauseParam(param, "macros", SCHEMA).clauses).toHaveLength(1);
	});

	it("reads junk, a wrong version and an oversized payload as 'no filters'", () => {
		expect(parseClauseParam("not json", "skills")).toBeNull();
		expect(parseClauseParam(JSON.stringify({ v: 99, k: "skills", c: [] }), "skills")).toBeNull();
		expect(parseClauseParam(JSON.stringify({ v: 1, k: "skills" }), "skills")).toBeNull();
		const huge = JSON.stringify({ v: 1, k: "skills", c: [["a", "b", "=", "x".repeat(MAX_URL_CHARS)]] });
		expect(parseClauseParam(huge, "skills")).toBeNull();
	});

	it("bounds what a hand-edited URL can inflate", () => {
		const rows = [];
		for (let i = 0; i < 40; i += 1) rows.push(["Jarvis Custom Skill", "description", "like", "x"]);
		const parsed = parseClauseParam(JSON.stringify({ v: 1, k: "skills", c: rows }), "skills", SCHEMA);
		expect(parsed.clauses).toHaveLength(20); // schema limits.max_clauses
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
		expect(droppedNotice([{}, {}])).toMatch(/^2 filters from this link are no longer available/);
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
		const info = filterErrorInfo(thrownBy("list_filter_invalid_value", "Created On needs a value."));
		expect(info).toEqual({
			code: "list_filter_invalid_value",
			kind: "row",
			message: "Created On needs a value.",
		});
	});

	it("classifies every code the compiler can raise", () => {
		const kinds = {
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
			filterErrorInfo({ ok: false, error: { code: "list_filter_too_many_clauses", message: "cap" } })
				.kind
		).toBe("cap");
		const e = new Error("x");
		e.messages = [JSON.stringify({ ok: false, error: { code: "list_filter_bad_payload", message: "b" } })];
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
		const error = { code: "list_filter_invalid_value", kind: "row", message: "Created On needs a start and an end." };
		expect(attributeError(error, clauses, index)).toBe(clauses[1].id);
	});

	it("blames nobody when the label is ambiguous or absent", () => {
		const twice = [clauses[1], { ...clauses[1], id: "other" }];
		const error = { code: "list_filter_invalid_value", kind: "row", message: "Created On needs a start and an end." };
		expect(attributeError(error, twice, index)).toBeNull();
		expect(attributeError({ ...error, message: "Nope." }, clauses, index)).toBeNull();
		expect(attributeError({ ...error, kind: "cap" }, clauses, index)).toBeNull();
		expect(attributeError(null, clauses, index)).toBeNull();
	});
});
