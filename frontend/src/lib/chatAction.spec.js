import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

import { normaliseAction } from "./chatAction";

/**
 * The regression this module exists for: a jarvis-action block missing `kind`
 * rendered a summary card whose model was never built, so the card sat on
 * "Preparing summary..." forever with no error and no retry. The card's render
 * gate (isEditVerb) and the build watcher disagreed about whether `kind` was
 * required, and the disagreement failed silently.
 */

describe("normaliseAction", () => {
	it("infers kind:doc for a block that omits it", () => {
		// THE bug: the model emits verb+doctype+fields and no `kind`.
		const a = normaliseAction({ verb: "create", doctype: "Supplier" });
		expect(a.kind).toBe("doc");
	});

	it("converts a fields OBJECT into the [{label,value}] array the builder walks", () => {
		const a = normaliseAction({
			verb: "create",
			doctype: "Supplier",
			fields: { supplier_name: "Test", country: "India" },
		});
		expect(a.fields).toEqual([
			{ label: "supplier_name", value: "Test" },
			{ label: "country", value: "India" },
		]);
	});

	it("normalises the exact block that stranded conversation o3fike0mm0", () => {
		const raw = {
			verb: "create",
			doctype: "Supplier",
			continue: 1,
			fields: {
				supplier_name: "Test",
				supplier_type: "Company",
				country: "India",
				supplier_group: "Demo Supplier Group",
			},
		};
		const a = normaliseAction(raw);
		expect(a.kind).toBe("doc");
		expect(Array.isArray(a.fields)).toBe(true);
		expect(a.fields).toHaveLength(4);
		// buildDraftModel throws "no fields to show" on a create whose fields have
		// no .length, and an object silently has none. This is what unblocks the card.
		expect(a.fields.length).toBeGreaterThan(0);
		expect(a.continue).toBe(1); // chain marker must survive
	});

	it("converts a tables OBJECT keyed by fieldname into the [{fieldname,rows}] array", () => {
		// This shape THREW rather than hung: for..of on a plain object is a
		// TypeError, which surfaced as "Could not load this draft. Tell me to try
		// again." on the card.
		const a = normaliseAction({
			verb: "create",
			doctype: "Purchase Order",
			tables: { items: { rows: [{ item_code: "DUMMY-ITEM-002", qty: 1 }] } },
		});
		expect(a.tables).toEqual([
			{ fieldname: "items", rows: [{ item_code: "DUMMY-ITEM-002", qty: 1 }] },
		]);
	});

	it("accepts the bare-array table form too ({items: [...]})", () => {
		const a = normaliseAction({
			verb: "create",
			doctype: "Purchase Order",
			tables: { items: [{ item_code: "X", qty: 2 }] },
		});
		expect(a.tables).toEqual([{ fieldname: "items", rows: [{ item_code: "X", qty: 2 }] }]);
	});

	it("drops a table whose rows cannot be read as a list rather than guessing", () => {
		// A half-populated grid the user confirms believing it is the full set is
		// worse than no grid at all.
		const a = normaliseAction({
			verb: "create",
			doctype: "Purchase Order",
			tables: { items: { nonsense: true }, taxes: { rows: [{ rate: 18 }] } },
		});
		expect(a.tables).toEqual([{ fieldname: "taxes", rows: [{ rate: 18 }] }]);
	});

	it("normalises the second stranded block (Purchase Order, object fields AND tables)", () => {
		const a = normaliseAction({
			verb: "create",
			doctype: "Purchase Order",
			fields: { supplier: "Test", transaction_date: "2026-07-31" },
			tables: { items: { rows: [{ item_code: "DUMMY-ITEM-002", qty: 1, rate: 1 }] } },
		});
		expect(a.kind).toBe("doc");
		expect(a.fields).toHaveLength(2);
		expect(a.tables).toHaveLength(1);
		expect(a.tables[0].fieldname).toBe("items");
		// buildDraftModel walks BOTH with for..of; neither may be a plain object.
		expect(Array.isArray(a.fields)).toBe(true);
		expect(Array.isArray(a.tables)).toBe(true);
	});

	it("leaves a spec-shaped block untouched", () => {
		const good = {
			kind: "doc",
			verb: "create",
			doctype: "Sales Order",
			fields: [{ label: "customer", value: "Palmer Productions Ltd." }],
			tables: [{ fieldname: "items", rows: [{ item_code: "Widget", qty: 5 }] }],
		};
		expect(normaliseAction({ ...good })).toEqual(good);
	});

	it("never rewrites an explicit kind, so an email block stays an email block", () => {
		// The template branches on kind === 'email' BEFORE the summary card, so
		// inferring 'doc' here would swap the rendered card entirely.
		const a = normaliseAction({ kind: "email", verb: "create", doctype: "Contact" });
		expect(a.kind).toBe("email");
	});

	it("does not invent a kind when there is no doctype to justify one", () => {
		const a = normaliseAction({ verb: "create" });
		expect(a.kind).toBeUndefined();
	});

	it("passes non-objects through as null rather than throwing", () => {
		expect(normaliseAction(null)).toBeNull();
		expect(normaliseAction(undefined)).toBeNull();
		expect(normaliseAction("nope")).toBeNull();
		expect(normaliseAction(42)).toBeNull();
		// An array is JSON-valid and typeof "object": it must not be treated as a
		// block, or Object.entries would turn it into nonsense fields.
		expect(normaliseAction([{ label: "a", value: 1 }])).toBeNull();
	});

	it("tolerates a block with no fields at all", () => {
		// A tables-only create is legal per the spec; it must not crash here.
		const a = normaliseAction({ verb: "create", doctype: "Sales Order", tables: [] });
		expect(a.kind).toBe("doc");
		expect(a.fields).toBeUndefined();
	});
});

describe("the two gates that disagreed stay mirrored", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");

	it("the build watcher gates on the same predicate the card renders on", () => {
		// The card is `v-else-if="isEditVerb(activeAction)"` after an email branch.
		// The watcher must mirror that, NOT add its own kind === "doc" requirement:
		// anything that draws a summary card has to attempt its model, so a shape it
		// cannot build produces the error card instead of a stuck placeholder.
		expect(src).toContain('v-else-if="isEditVerb(activeAction)"');
		expect(src).toContain('if (!a || a.kind === "email" || !isEditVerb(a)) return;');
		expect(src).not.toContain(
			'if (!(a && a.kind === "doc" && (a.verb === "create" || a.verb === "update" || !a.verb)))'
		);
	});

	it("never dumps raw dry-run JSON into a confirmation card body", () => {
		// PendingCard puts the raw preview behind a "Details" expander. The
		// no-structured-card FALLBACK used to <pre> the same JSON straight into the
		// card body, so a confirmation whose preview did not summarise landed as a
		// wall of JSON above the Confirm button. Both surfaces must collapse it.
		expect(src).toContain('<details\n\t\t\t\t\t\t\t\t\t\tv-else-if="pendingPreviewOf(pa)"');
		expect(src).not.toMatch(/<pre\s+v-else-if="pendingPreviewOf\(pa\)"/);
	});

	it("actions are canonicalised at the single parse point", () => {
		// If a second _ACTION_RE parser appears without this call, the shapes
		// diverge again and the stuck card comes back.
		expect(src).toContain("normaliseAction(a)");
		expect(src).toContain('import { normaliseAction } from "@/lib/chatAction";');
	});
});
