// The draft form builder (lib/draftModel.js) builds the SAME models it built while it was a closure
// inside ChatView.vue. Both the desktop's typed drafts and a server draft card's form go through
// this one builder, so a change in what it builds is a change in what people edit and save.
//
// draftModel.golden.json holds what the OLD in-view code built for every case in
// draftModelCases.js (typed actions and card actions, the API calls made, a slow rule-values
// answer). The default test here builds the same cases with the shared module and requires the
// same result.
//
// To regenerate the golden from the old source (needs git history; the in-view code no longer
// exists in the working tree):
//   DRAFT_MODEL_GOLDEN_FROM=4678e1c1e npx vitest run src/lib/draftModel.spec.js
// It reads `git show <ref>:frontend/src/views/ChatView.vue`, cuts out `_normKey` / `_actField`,
// the form-meta cache and `_formMeta`, `buildDraftModel`, and `RULE_VALUES_WAIT_MS` /
// `fillRuleValues`, evaluates them with the real lib helpers and a fake server, runs the cases
// and rewrites the golden. Run it on a pre-move line (a hotfix branch's own ChatView.vue works the
// same way: `git diff` on the golden must stay empty) before trusting the move there.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { panelField } from "./docFields.js";
import { checkToYesNo } from "./draftApply.js";
import {
	applyRuleValues,
	buildDraftTable,
	parentDefaults,
	ruleFetchLinks,
} from "./draftTables.js";
import { FROM_CARD, resolveDraftField } from "./toolDraftPanel.js";
import { makeDraftModelBuilder } from "./draftModel.js";
import { CASES, runCases } from "./draftModelCases.js";

const GOLDEN = path.resolve(__dirname, "draftModel.golden.json");
const timers = { advance: (ms) => vi.advanceTimersByTimeAsync(ms) };
// Through JSON, as the golden file is: what JSON cannot hold is already a visible marker.
const viaJson = (v) => JSON.parse(JSON.stringify(v));

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

// The old in-view code, evaluated from the source of ChatView.vue at some commit.
function oldBuilderFactory(source) {
	const cut = (from, to) => {
		const a = source.indexOf(from);
		if (a < 0) throw new Error(`not found: ${from}`);
		const b = source.indexOf(to, a + from.length);
		if (b < 0) throw new Error(`not found: ${to}`);
		return source.slice(a, b);
	};
	const code = [
		cut("function _normKey(s) {", "// --- Record draft panel"),
		cut("const _formMetaCache = {};", "// Build the draft model from an action"),
		cut("async function buildDraftModel(a) {", "\n// Open the editable panel"),
		cut("const RULE_VALUES_WAIT_MS = 1500;", "\nconst draftCta"),
		"return buildDraftModel;",
	].join("\n");
	return (deps) => {
		const scope = {
			api: deps,
			_panelField: panelField,
			buildDraftTable,
			checkToYesNo,
			parentDefaults,
			ruleFetchLinks,
			applyRuleValues,
			resolveDraftField,
			FROM_CARD,
			// late-bound, so the spec's fake timers are the ones used
			setTimeout: (...a) => globalThis.setTimeout(...a),
		};
		return new Function(...Object.keys(scope), code)(...Object.values(scope));
	};
}

const newBuilderFactory = (deps) => makeDraftModelBuilder(deps).buildDraftModel;

const ref = process.env.DRAFT_MODEL_GOLDEN_FROM;
describe.skipIf(!ref)("regenerate the golden from the old in-view source", () => {
	it("writes draftModel.golden.json", async () => {
		const root = execFileSync("git", ["rev-parse", "--show-toplevel"], {
			encoding: "utf8",
		}).trim();
		const source = execFileSync("git", ["show", `${ref}:frontend/src/views/ChatView.vue`], {
			cwd: root,
			encoding: "utf8",
			maxBuffer: 64 * 1024 * 1024,
		});
		const record = viaJson(await runCases(oldBuilderFactory(source), timers));
		fs.writeFileSync(GOLDEN, JSON.stringify(record, null, 1) + "\n");
		expect(Object.keys(record).length).toBe(CASES.length);
	});
});

describe.skipIf(!!ref)("the shared builder builds what the in-view builder built", () => {
	const loadGolden = () => JSON.parse(fs.readFileSync(GOLDEN, "utf8"));

	// Break: any change in a built model (a field's value, control or marks, a table's rows and
	// columns, the rule values, the stored-record overlay, `storedUnavailable`), in which fields a
	// label or fieldname resolves to, in when the form meta or the record is read, or in the wait
	// for rule values. The typed payload golden and the form specs start from a built model, so
	// none of them sees a change in how it is built.
	it("every case is deep-equal to the golden, API calls included", async () => {
		const golden = loadGolden();
		const got = viaJson(await runCases(newBuilderFactory, timers));
		expect(Object.keys(got)).toEqual(Object.keys(golden));
		for (const name of Object.keys(golden)) expect(got[name], name).toEqual(golden[name]);
	});

	// The golden is only worth something while it holds real models. These read it, not the
	// builder: a regenerated-empty golden (a cut that matched nothing) would pass the test above.
	it("the golden holds models, errors and undefined for the kinds of input the view meets", () => {
		const golden = loadGolden();
		const outcomes = (name) =>
			golden[name].builds.map((b) =>
				b.model ? "model" : b.error ? "error" : b.built ? "slow" : "undefined"
			);
		expect(outcomes("typed create, fields by label, fieldname and fuzzy")).toEqual(["model"]);
		expect(
			outcomes("refusals: a docs batch, an empty create, and a non-doc action make no call")
		).toEqual(["error", "error", "error", "undefined", "undefined", "undefined"]);
		expect(
			golden["refusals: a docs batch, an empty create, and a non-doc action make no call"]
				.calls
		).toEqual([]);
		const stored =
			golden[
				"card update, stored record unloadable: shown without it and marked, tables or not"
			];
		expect(stored.builds.map((b) => b.model.storedUnavailable)).toEqual([true, true]);
		const slow = Object.values(golden).find((r) => r.builds[0] && r.builds[0].built);
		expect(slow.builds[0].built.model.tables[0].rows[0].__rule).toEqual({
			is_fixed_asset: "0",
		});
		expect(slow.builds[0].afterLateAnswer.tables[0].rows[0].__rule).toEqual({
			is_fixed_asset: 1,
		});
	});
});
