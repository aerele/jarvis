import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	FormControl: { template: "<input />" },
	LoadingIndicator: { template: "<span>Loading</span>" },
	Select: { template: "<select />" },
}));
vi.mock("@/api", () => ({ getDoctypeFields: vi.fn(), searchLink: vi.fn() }));

import { getDoctypeFields, searchLink } from "@/api";
import { askAnswerText, parseAsk } from "@/lib/chatAsk";
import AskCard from "./AskCard.vue";

const question = {
	q: "Item",
	type: "field",
	doctype: "Sales Invoice Item",
	fieldname: "item_code",
	parent: "Sales Invoice",
	parentfield: "items",
};
const filters = [
	["Item", "is_sales_item", "=", 1],
	["Item", "disabled", "=", 0],
	["Item", "has_variants", "=", 0],
];
const metadata = (extra = {}) => ({
	ok: true,
	fields: [
		{
			fieldname: "item_code",
			label: "Item Code",
			fieldtype: "Link",
			options: "Item",
			link_query_filters: filters,
			...extra,
		},
	],
});
const fence = (q) => "```jarvis-ask\n" + JSON.stringify([q]) + "\n```";
const card = (q = question) => mount(AskCard, { props: { spec: parseAsk(fence(q)) } });
const submit = (w) => w.get(".jv-ask-submit");
const deferred = () => {
	let resolve;
	const promise = new Promise((r) => (resolve = r));
	return { promise, resolve };
};

beforeEach(() => {
	getDoctypeFields.mockReset().mockResolvedValue(metadata());
	searchLink.mockReset().mockResolvedValue([{ value: "SELL-ITEM", description: "Sellable" }]);
});

describe("contextual field-ask contract", () => {
	it("keeps parent/table identity for flat and nested fields without changing answer text", () => {
		for (const q of [question, { q: "Item", field: { ...question } }]) {
			const spec = parseAsk(fence(q));
			expect(spec.questions[0]).toMatchObject(question);
			expect(askAnswerText(spec, { 0: "SELL-ITEM" }, {})).toBe(
				"Here are my answers:\n1. Item → SELL-ITEM"
			);
		}
	});
	it.each([{ parent: "Sales<script>" }, { parentfield: "items.code" }])(
		"refuses malformed supplied context instead of an unscoped child lookup: %j",
		(context) => {
			expect(parseAsk(fence({ ...question, ...context })).questions[0].type).toBe("text");
		}
	);
});

describe("AskCard field-backed Link search", () => {
	it("uses parent-aware metadata and passes the field constraints to Frappe", async () => {
		const w = card();
		await flushPromises();
		expect(getDoctypeFields).toHaveBeenCalledWith(
			"Sales Invoice Item",
			"Sales Invoice",
			"items"
		);
		await w.get(".jv-ask-link input").setValue("SELL");
		await flushPromises();
		expect(searchLink).toHaveBeenLastCalledWith(
			"Item",
			"SELL",
			8,
			"Sales Invoice",
			"item_code",
			filters
		);
		await w.get(".jv-ask-linkmenu button").trigger("mousedown");
		expect(submit(w).element.disabled).toBe(false);
		await submit(w).trigger("click");
		await submit(w).trigger("click");
		expect(w.emitted("submit")).toEqual([["Here are my answers:\n1. Item → SELL-ITEM"]]);
		w.unmount();
	});
	it("keeps a generic Item lookup generic", async () => {
		const w = card({ q: "Item", type: "link", doctype: "Item" });
		await w.get(".jv-ask-link input").trigger("focus");
		await flushPromises();
		expect(searchLink).toHaveBeenLastCalledWith("Item", "");
		expect(getDoctypeFields).not.toHaveBeenCalled();
		w.unmount();
	});
	it("does not reuse child metadata from another parent/table context", async () => {
		const w = mount(AskCard, {
			props: {
				spec: {
					questions: [
						question,
						{ ...question, parent: "Custom Invoice", parentfield: "lines" },
					],
				},
			},
		});
		await flushPromises();
		expect(getDoctypeFields).toHaveBeenCalledTimes(2);
		expect(getDoctypeFields).toHaveBeenCalledWith(
			"Sales Invoice Item",
			"Custom Invoice",
			"lines"
		);
		w.unmount();
	});
	it("falls back to an explained typed answer when a required filter cannot be resolved", async () => {
		getDoctypeFields.mockResolvedValue(
			metadata({ link_filters: [["Item", "company", "=", "eval:parent.company"]] })
		);
		const w = card();
		await flushPromises();
		await w.get(".jv-ask-link input").trigger("focus");
		await flushPromises();
		expect(searchLink).not.toHaveBeenCalled();
		expect(w.text()).toContain("Set Company before searching this field.");
		expect(w.find(".jv-ask-link").exists()).toBe(false);
		await w.get('input[type="text"]').setValue("known-code");
		expect(submit(w).element.disabled).toBe(false);
		w.unmount();
	});
	it("does not carry a pick or late response into a changed parent context", async () => {
		const w = card();
		await flushPromises();
		await w.get(".jv-ask-link input").setValue("SELL");
		await flushPromises();
		await w.get(".jv-ask-linkmenu button").trigger("mousedown");
		expect(submit(w).element.disabled).toBe(false);
		const late = deferred();
		searchLink.mockReturnValueOnce(late.promise);
		await w.get(".jv-ask-link input").trigger("focus");
		await w.setProps({ spec: { questions: [{ ...question, parent: "Custom Invoice" }] } });
		await flushPromises();
		late.resolve([{ value: "OLD-CONTEXT" }]);
		await flushPromises();
		expect(submit(w).element.disabled).toBe(true);
		expect(w.find(".jv-ask-linkmenu").exists()).toBe(false);
		w.unmount();
	});
	it("ignores an older search response and does not reopen a blurred menu", async () => {
		const first = deferred(),
			second = deferred(),
			third = deferred();
		searchLink
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(second.promise)
			.mockReturnValueOnce(third.promise);
		const w = card();
		await flushPromises();
		const input = w.get(".jv-ask-link input");
		await input.setValue("old");
		await input.setValue("new");
		second.resolve([{ value: "NEW" }]);
		await flushPromises();
		first.resolve([{ value: "OLD" }]);
		await flushPromises();
		expect(w.get(".jv-ask-linkmenu").text()).toBe("NEW");
		await w.get(".jv-ask-linkmenu button").trigger("mousedown");
		await input.setValue("editing");
		expect(submit(w).element.disabled).toBe(true);
		await input.trigger("blur");
		third.resolve([{ value: "LATE" }]);
		await flushPromises();
		expect(w.find(".jv-ask-linkmenu").exists()).toBe(false);
		w.unmount();
	});
});
