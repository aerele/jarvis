// The schema fixtures every list-filter spec shares.
//
// Each entry is the exact dict jarvis/chat/list_filters.py `_entry()` emits, and
// the operator lists are the ones its `allowed_operators()` derives from
// Frappe's inverted `invalid_condition_map`. Inventing a friendlier shape here
// would let the panel pass tests against a contract the server does not have.
export const DESCRIPTION = {
	doctype: "Jarvis Custom Skill",
	fieldname: "description",
	label: "Description",
	fieldtype: "Small Text",
	options: "",
	is_standard: false,
	is_child: false,
	json_array: false,
	default_operator: "like", // D5-a
	operators: ["=", "!=", "like", "not like", "in", "not in", "is", ">", "<", ">=", "<="],
};

export const ENABLED = {
	doctype: "Jarvis Custom Skill",
	fieldname: "enabled",
	label: "Enabled",
	fieldtype: "Check",
	options: "",
	is_standard: false,
	is_child: false,
	json_array: false,
	default_operator: "=",
	operators: ["="],
};

export const SCOPE = {
	doctype: "Jarvis Custom Skill",
	fieldname: "scope",
	label: "Scope",
	fieldtype: "Select",
	options: "\nOrg\nPersonal", // leading blank = "not set"
	is_standard: false,
	is_child: false,
	json_array: false,
	default_operator: "=",
	operators: ["=", "!=", "in", "not in", "is", ">", "<", ">=", "<="],
};

export const OWNER = {
	doctype: "Jarvis Custom Skill",
	fieldname: "owner",
	label: "Created By",
	fieldtype: "Link",
	options: "User",
	is_standard: true,
	is_child: false,
	json_array: false,
	default_operator: "=",
	operators: ["=", "!=", "like", "not like", "in", "not in", "is"],
};

export const CREATION = {
	doctype: "Jarvis Custom Skill",
	fieldname: "creation",
	label: "Created On",
	fieldtype: "Datetime",
	options: "",
	is_standard: true,
	is_child: false,
	json_array: false,
	default_operator: "Between",
	operators: ["is", ">", "<", ">=", "<=", "Between", "Timespan"],
};

export const MODIFIED_ON = {
	...CREATION,
	fieldname: "modified",
	label: "Last Updated On",
	fieldtype: "Date",
	operators: ["is", "in", "not in", "=", "!=", ">", "<", ">=", "<=", "Between", "Timespan"],
};

export const IDX = {
	doctype: "Jarvis Custom Skill",
	fieldname: "idx",
	label: "Index",
	fieldtype: "Int",
	options: "",
	is_standard: true,
	is_child: false,
	json_array: false,
	default_operator: "=",
	operators: ["=", "!=", "is", ">", "<", ">=", "<="],
};

export const STEP_PROMPT = {
	doctype: "Jarvis Macro Step",
	fieldname: "prompt",
	label: "Prompt (Jarvis Macro Step)",
	fieldtype: "Small Text",
	options: "",
	is_standard: false,
	is_child: true,
	json_array: false,
	default_operator: "like",
	operators: ["=", "!=", "like", "not like", "in", "not in", "is"],
};

export const SKILLS_SCHEMA = {
	contract_version: 1,
	view_key: "skills",
	label: "Skills",
	root_doctype: "Jarvis Custom Skill",
	is_large_table: false,
	schema_revision: "rev1",
	limits: { max_clauses: 20, max_in_values: 100, max_value_chars: 1000, max_page_length: 100 },
	fields: [DESCRIPTION, ENABLED, SCOPE, OWNER, CREATION, MODIFIED_ON, IDX, STEP_PROMPT],
};
