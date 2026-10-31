import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
	it("explains an unresolved filter as soon as metadata arrives, before focus", async () => {
		getDoctypeFields.mockResolvedValue(
			metadata({ link_filters: [["Item", "company", "=", "eval:parent.company"]] })
		);
		const w = card();
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

// Keyboard and focus behaviour of the Link suggestion menu. jsdom focus() sends
// real blur/focusout/focus events with relatedTarget, and a native Enter/Space on
// a button arrives as a click, so a click-only trigger models keyboard activation.
// Native Tab order itself is verified in a real browser, not here.
describe("AskCard Link suggestions: keyboard and focus", () => {
	const cleanups = [];
	afterEach(() => {
		cleanups.splice(0).forEach((fn) => fn());
	});
	beforeEach(() => {
		searchLink.mockResolvedValue([
			{ value: "ITEM-A", description: "First" },
			{ value: "ITEM-B", description: "Second" },
		]);
	});

	const askAll = (qs) => parseAsk("```jarvis-ask\n" + JSON.stringify(qs) + "\n```");
	async function attached(qs = [question]) {
		const w = mount(AskCard, { attachTo: document.body, props: { spec: askAll(qs) } });
		cleanups.push(() => w.unmount());
		await flushPromises();
		return w;
	}
	function outsideButton() {
		const b = document.createElement("button");
		document.body.appendChild(b);
		cleanups.push(() => b.remove());
		return b;
	}
	function hostKeydown() {
		const seen = vi.fn();
		document.body.addEventListener("keydown", seen);
		cleanups.push(() => document.body.removeEventListener("keydown", seen));
		return seen;
	}
	function pressEscape(el) {
		const ev = new KeyboardEvent("keydown", {
			key: "Escape",
			bubbles: true,
			cancelable: true,
		});
		el.dispatchEvent(ev);
		return ev;
	}
	const field = (w, qi = 0) => w.findAll(".jv-ask-link")[qi];
	const input = (w, qi = 0) => field(w, qi).get("input");
	const menu = (w, qi = 0) => field(w, qi).find(".jv-ask-linkmenu");
	const buttons = (w, qi = 0) => field(w, qi).findAll(".jv-ask-linkmenu button");
	const searches = () => searchLink.mock.calls.length;
	async function open(w, qi = 0) {
		input(w, qi).element.focus();
		await flushPromises();
		return buttons(w, qi);
	}

	describe("click-only activation (Enter/Space on a focused suggestion)", () => {
		it("chooses the Item and enables Submit, but sends nothing until Submit is used", async () => {
			const w = await attached();
			const [first] = await open(w);
			await first.trigger("click");
			expect(menu(w).exists()).toBe(false);
			expect(input(w).element.value).toBe("ITEM-A");
			expect(submit(w).element.disabled).toBe(false);
			expect(w.emitted("submit")).toBeUndefined();
			await submit(w).trigger("click");
			await submit(w).trigger("click");
			expect(w.emitted("submit")).toEqual([["Here are my answers:\n1. Item → ITEM-A"]]);
		});
		it("does not submit an enclosing form", async () => {
			const form = document.createElement("form");
			document.body.appendChild(form);
			const onSubmit = vi.fn((e) => e.preventDefault());
			form.addEventListener("submit", onSubmit);
			const w = mount(AskCard, { attachTo: form, props: { spec: askAll([question]) } });
			cleanups.push(() => {
				w.unmount();
				form.remove();
			});
			await flushPromises();
			const [first] = await open(w);
			await first.trigger("click");
			expect(onSubmit).not.toHaveBeenCalled();
		});
		it("keeps mouse mousedown plus click as one choice with no extra search", async () => {
			const w = await attached();
			const [first] = await open(w);
			const before = searches();
			await first.trigger("mousedown");
			await first.trigger("click");
			await flushPromises();
			expect(searches()).toBe(before);
			expect(menu(w).exists()).toBe(false);
			expect(input(w).element.value).toBe("ITEM-A");
			expect(submit(w).element.disabled).toBe(false);
			expect(w.emitted("submit")).toBeUndefined();
			await submit(w).trigger("click");
			expect(w.emitted("submit")).toEqual([["Here are my answers:\n1. Item → ITEM-A"]]);
		});
	});

	describe("focus inside and outside the suggestion menu", () => {
		it("keeps the menu while focus moves input → suggestion → suggestion → input, without a new search", async () => {
			const w = await attached();
			const [first, second] = await open(w);
			const before = searches();
			first.element.focus();
			await flushPromises();
			expect(menu(w).exists()).toBe(true);
			expect(document.activeElement).toBe(first.element);
			second.element.focus();
			await flushPromises();
			expect(menu(w).exists()).toBe(true);
			expect(document.activeElement).toBe(second.element);
			input(w).element.focus();
			await flushPromises();
			expect(menu(w).exists()).toBe(true);
			expect(buttons(w)).toHaveLength(2);
			expect(document.activeElement).toBe(input(w).element);
			expect(searches()).toBe(before);
		});
		it("returns focus to the input after a keyboard pick, with no extra search", async () => {
			const w = await attached();
			const [first] = await open(w);
			const before = searches();
			first.element.focus();
			await flushPromises();
			await first.trigger("click");
			await flushPromises();
			expect(document.activeElement).toBe(input(w).element);
			expect(menu(w).exists()).toBe(false);
			expect(input(w).element.value).toBe("ITEM-A");
			expect(searches()).toBe(before);
			expect(submit(w).element.disabled).toBe(false);
			expect(w.emitted("submit")).toBeUndefined();
		});
		it("treats a FocusEvent relatedTarget inside the field as internal and outside as leaving", async () => {
			const w = await attached();
			const [first] = await open(w);
			const away = outsideButton();
			const fire = (el, type, relatedTarget) =>
				el.dispatchEvent(
					new FocusEvent(type, { bubbles: type === "focusout", relatedTarget })
				);
			fire(input(w).element, "blur", first.element);
			await flushPromises();
			expect(menu(w).exists()).toBe(true);
			fire(first.element, "focusout", away);
			await flushPromises();
			expect(menu(w).exists()).toBe(false);
		});
		it("closes when focus leaves from a suggestion, such as Tab past the last item", async () => {
			const w = await attached();
			const [, last] = await open(w);
			last.element.focus();
			await flushPromises();
			expect(menu(w).exists()).toBe(true);
			const next = outsideButton();
			next.focus();
			await flushPromises();
			expect(document.activeElement).toBe(next);
			expect(menu(w).exists()).toBe(false);
		});
		it("closes on an external blur and ignores a search that returns late", async () => {
			const w = await attached();
			const late = deferred();
			searchLink.mockReturnValueOnce(late.promise);
			input(w).element.focus();
			await flushPromises();
			const away = outsideButton();
			away.focus();
			late.resolve([{ value: "LATE" }]);
			await flushPromises();
			expect(menu(w).exists()).toBe(false);
			expect(submit(w).element.disabled).toBe(true);
		});
	});

	describe("Escape", () => {
		it("closes an open menu, consumes the key, keeps the query and restores input focus", async () => {
			const w = await attached();
			const host = hostKeydown();
			await input(w).setValue("SEL");
			await flushPromises();
			const [first] = buttons(w);
			first.element.focus();
			const before = searches();
			const ev = pressEscape(first.element);
			await flushPromises();
			expect(ev.defaultPrevented).toBe(true);
			expect(host).not.toHaveBeenCalled();
			expect(menu(w).exists()).toBe(false);
			expect(document.activeElement).toBe(input(w).element);
			expect(input(w).element.value).toBe("SEL");
			expect(searches()).toBe(before);
			expect(submit(w).element.disabled).toBe(true);
		});
		it("keeps an existing selection", async () => {
			const w = await attached();
			const [first] = await open(w);
			await first.trigger("mousedown");
			outsideButton().focus();
			await open(w);
			expect(menu(w).exists()).toBe(true);
			const before = searches();
			const ev = pressEscape(input(w).element);
			await flushPromises();
			expect(ev.defaultPrevented).toBe(true);
			expect(menu(w).exists()).toBe(false);
			expect(input(w).element.value).toBe("ITEM-A");
			expect(submit(w).element.disabled).toBe(false);
			expect(searches()).toBe(before);
		});
		it("while a read is pending consumes the key, cancels the read and blocks a late reopen", async () => {
			const w = await attached();
			const host = hostKeydown();
			const late = deferred();
			searchLink.mockReturnValueOnce(late.promise);
			input(w).element.focus();
			await flushPromises();
			const before = searches();
			const ev = pressEscape(input(w).element);
			expect(ev.defaultPrevented).toBe(true);
			expect(host).not.toHaveBeenCalled();
			expect(document.activeElement).toBe(input(w).element);
			late.resolve([{ value: "LATE" }]);
			await flushPromises();
			expect(menu(w).exists()).toBe(false);
			expect(searches()).toBe(before);
			expect(submit(w).element.disabled).toBe(true);
		});
		it("lets the host see Escape when there is nothing to close", async () => {
			const w = await attached();
			const host = hostKeydown();
			const idle = pressEscape(input(w).element);
			expect(idle.defaultPrevented).toBe(false);
			expect(host).toHaveBeenCalledTimes(1);
			await open(w);
			expect(pressEscape(input(w).element).defaultPrevented).toBe(true);
			await flushPromises();
			expect(host).toHaveBeenCalledTimes(1);
			const closed = pressEscape(input(w).element);
			expect(closed.defaultPrevented).toBe(false);
			expect(host).toHaveBeenCalledTimes(2);
		});
	});

	describe("two Link questions on one card", () => {
		const two = [question, { ...question, q: "Second item" }];
		it("keeps focus, menu and late search state separate per question", async () => {
			const w = await attached(two);
			const late = deferred();
			searchLink.mockReturnValueOnce(late.promise);
			input(w, 0).element.focus();
			await flushPromises();
			input(w, 1).element.focus();
			await flushPromises();
			expect(menu(w, 0).exists()).toBe(false);
			expect(buttons(w, 1)).toHaveLength(2);
			late.resolve([{ value: "LATE-0" }]);
			await flushPromises();
			expect(menu(w, 0).exists()).toBe(false);
			expect(buttons(w, 1).map((b) => b.text())).toEqual([
				"ITEM-A · First",
				"ITEM-B · Second",
			]);
			const before = searches();
			input(w, 0).element.focus();
			await flushPromises();
			expect(searches()).toBe(before + 1);
			expect(buttons(w, 0)).toHaveLength(2);
			expect(menu(w, 1).exists()).toBe(false);
		});
		it("closes a question's menu when focus moves from its suggestion into the other input; picks stay separate", async () => {
			const w = await attached(two);
			const [firstOfA] = await open(w, 0);
			firstOfA.element.focus();
			await flushPromises();
			expect(menu(w, 0).exists()).toBe(true);
			await open(w, 1);
			expect(menu(w, 0).exists()).toBe(false);
			expect(menu(w, 1).exists()).toBe(true);
			const [pickA] = await open(w, 0);
			pickA.element.focus();
			await pickA.trigger("click");
			await flushPromises();
			expect(input(w, 0).element.value).toBe("ITEM-A");
			expect(input(w, 1).element.value).toBe("");
			expect(submit(w).element.disabled).toBe(true);
			const [, pickB] = await open(w, 1);
			pickB.element.focus();
			await pickB.trigger("click");
			await flushPromises();
			expect(input(w, 0).element.value).toBe("ITEM-A");
			expect(input(w, 1).element.value).toBe("ITEM-B");
			expect(submit(w).element.disabled).toBe(false);
			expect(w.emitted("submit")).toBeUndefined();
			await submit(w).trigger("click");
			expect(w.emitted("submit")).toEqual([
				["Here are my answers:\n1. Item → ITEM-A\n2. Second item → ITEM-B"],
			]);
		});
	});

	describe("late search results", () => {
		it("never select a result, even a single exact match for the typed text", async () => {
			const w = await attached();
			const late = deferred();
			searchLink.mockReturnValueOnce(late.promise);
			await input(w).setValue("ONLY");
			late.resolve([{ value: "ONLY" }]);
			await flushPromises();
			expect(buttons(w).map((b) => b.text())).toEqual(["ONLY"]);
			expect(submit(w).element.disabled).toBe(true);
			expect(w.emitted("submit")).toBeUndefined();
			await buttons(w)[0].trigger("mousedown");
			expect(submit(w).element.disabled).toBe(false);
		});
	});
});
