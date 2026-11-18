// The link-search state machine the draft form's hosts share (desktop panel, phone sheet).
// Each test names the break it catches; the pure filter rules are in draftLinkFilters.spec.js
// and the emits the form sends in DraftEditForm.spec.js.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp, ref } from "vue";
import { useDraftLink } from "./useDraftLink";

const customer = (extra = {}) => ({
	fieldname: "customer",
	fieldtype: "Link",
	options: "Customer",
	value: "",
	...extra,
});
const model = (fields = [customer()]) => ({ doctype: "Timesheet", tables: [], fields });

// Runs the composable inside a real component setup, so onBeforeUnmount is registered as in a host.
function mountComposable(options = {}) {
	const draftPanel = ref(options.model === undefined ? model() : options.model);
	const searchLink = options.searchLink || vi.fn(async () => []);
	const fillRuleValues = options.fillRuleValues || vi.fn();
	let link;
	const app = createApp({
		setup() {
			link = useDraftLink({ draftPanel, searchLink, fillRuleValues });
			return () => null;
		},
	});
	app.mount(document.createElement("div"));
	return { link, app, draftPanel, searchLink, fillRuleValues };
}
const input = () => document.createElement("input");
const search = (link, key = "f:customer", value = "ab", target = input()) =>
	link.onDraftLink(key, () => value, "Customer", { target });
const deferred = () => {
	let resolve, reject;
	const promise = new Promise((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
};
const rows = (...values) => values.map((value) => ({ value, description: `${value} desc` }));

describe("useDraftLink", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it("an older search answering after a newer one is dropped", async () => {
		// Catches: the generation counter removed, so a slow answer for "ab" repaints the menu
		// the user already moved on from ("abc").
		const old = deferred();
		const fresh = deferred();
		const searchLink = vi
			.fn()
			.mockReturnValueOnce(old.promise)
			.mockReturnValueOnce(fresh.promise);
		const { link } = mountComposable({ searchLink });
		const first = search(link, "f:customer", "ab");
		const second = search(link, "f:customer", "abc");
		fresh.resolve(rows("ABC Ltd"));
		await second;
		old.resolve(rows("AB Ltd"));
		await first;
		expect(link.draftLink.value.items).toEqual([{ value: "ABC Ltd", label: "ABC Ltd desc" }]);
		expect(link.draftLink.value.open).toBe(true);
	});

	it("passes the typed text, the field's context and the page size to the injected search", async () => {
		// Catches: the host's search function called with the wrong argument order (the server
		// keys permissions and custom search hooks off reference_doctype and link_fieldname).
		const { link, searchLink } = mountComposable();
		await search(link, "f:customer", "ab");
		expect(searchLink).toHaveBeenCalledWith("Customer", "ab", 8, "Timesheet", "customer", []);
		expect(link.draftLink.value.items).toEqual([]);
	});

	it("blur closes the menu after 160 ms and not before", async () => {
		// Catches: the blur timer shortened (a click on a menu item would be lost) or dropped
		// (the menu would stay over the form after the user tabs away).
		const { link } = mountComposable({ searchLink: vi.fn(async () => rows("A")) });
		const anchor = input();
		await search(link, "f:customer", "a", anchor);
		link.closeDraftLink({ target: anchor });
		vi.advanceTimersByTime(159);
		expect(link.draftLink.value.open).toBe(true);
		vi.advanceTimersByTime(1);
		expect(link.draftLink.value.open).toBe(false);
	});

	it("a pick inside the blur window wins: the value is set and the late timer does nothing", async () => {
		// Catches: the pick not bumping the generation, so the blur timer fires after the pick
		// and reopens or re-closes state that now belongs to the next search.
		const { link } = mountComposable({ searchLink: vi.fn(async () => rows("A")) });
		const anchor = input();
		await search(link, "f:customer", "a", anchor);
		link.closeDraftLink({ target: anchor });
		const setter = vi.fn();
		link.pickDraftLink(setter, link.draftLink.value.items[0]);
		expect(setter).toHaveBeenCalledWith("A");
		expect(link.draftLink.value).toEqual({ key: "", items: [], open: false, anchor: null });
		const next = { key: "f:other", items: [], open: true, anchor };
		link.draftLink.value = next;
		vi.advanceTimersByTime(200);
		expect(link.draftLink.value.open).toBe(true);
	});

	it("a blur while the field is still focused leaves the menu open", async () => {
		// Catches: the focus check dropped, so a blur event from a re-render closes a menu
		// whose field the user is still typing in.
		const { link } = mountComposable({ searchLink: vi.fn(async () => rows("A")) });
		const anchor = input();
		document.body.append(anchor);
		anchor.focus();
		await search(link, "f:customer", "a", anchor);
		link.closeDraftLink({ target: anchor });
		vi.advanceTimersByTime(200);
		expect(link.draftLink.value.open).toBe(true);
		anchor.remove();
	});

	it("a pick calls the setter with the chosen value, closes the menu and refreshes the rule values", async () => {
		// Catches: the pick forgetting to close the menu, or to refresh the hidden values the
		// rows' rules read from the chosen link (Desk fetches them on a link change).
		const draft = model();
		const { link, fillRuleValues } = mountComposable({
			model: draft,
			searchLink: vi.fn(async () => rows("A", "B")),
		});
		await search(link);
		const setter = vi.fn();
		link.pickDraftLink(setter, link.draftLink.value.items[1]);
		expect(setter).toHaveBeenCalledWith("B");
		expect(link.draftLink.value.open).toBe(false);
		expect(fillRuleValues).toHaveBeenCalledWith(
			"Timesheet",
			draft.tables,
			expect.any(Function)
		);
	});

	it("a server error sets the plain message, closes the menu, and the next search still works", async () => {
		// Catches: the error shown as raw exception text, the menu left open and empty, or the
		// error left set so the field stays unusable after a retry succeeds.
		const searchLink = vi
			.fn()
			.mockRejectedValueOnce(new Error("HTTP 500 raw"))
			.mockResolvedValueOnce(rows("A"));
		const { link } = mountComposable({ searchLink });
		await search(link);
		expect(link.draftLinkError.value).toBe(
			"Could not load this field's choices. Try again or open the document in ERPNext."
		);
		expect(link.draftLink.value.open).toBe(false);
		await search(link);
		expect(link.draftLinkError.value).toBe("");
		expect(link.draftLink.value.items).toEqual([{ value: "A", label: "A desc" }]);
	});

	it("a filter that cannot be resolved shows its own message and never calls the server", async () => {
		// Catches: a filter problem (Company not set yet) shown as the generic "could not load"
		// text, or sent to the server with a missing value.
		const filtered = customer({
			link_filters: [["Customer", "company", "=", "eval:doc.company"]],
		});
		const company = { fieldname: "company", fieldtype: "Data", value: "" };
		const { link, searchLink } = mountComposable({ model: model([company, filtered]) });
		await search(link);
		expect(link.draftLinkError.value).toBe("Set Company before searching this field.");
		expect(searchLink).not.toHaveBeenCalled();
		expect(link.draftLink.value.open).toBe(false);
	});

	it("resetDraftLink clears the menu and the error, and a pending answer cannot reopen it", async () => {
		// Catches: closing the panel (or switching chats) leaving an in-flight search free to
		// reopen the menu over the next draft.
		const pending = deferred();
		const { link } = mountComposable({ searchLink: vi.fn(() => pending.promise) });
		const running = search(link);
		link.draftLinkError.value = "old";
		link.resetDraftLink();
		pending.resolve(rows("A"));
		await running;
		expect(link.draftLink.value).toEqual({ key: "", items: [], open: false, anchor: null });
		expect(link.draftLinkError.value).toBe("");
	});

	it("unmounting drops a pending answer", async () => {
		// Catches: the onBeforeUnmount generation bump removed, so a timer or answer that lands
		// after the host unmounted still writes to the old state.
		const pending = deferred();
		const { link, app } = mountComposable({ searchLink: vi.fn(() => pending.promise) });
		const anchor = input();
		const running = search(link, "f:customer", "a", anchor);
		const before = link.draftLink.value;
		app.unmount();
		pending.resolve(rows("A"));
		await running;
		expect(link.draftLink.value).toBe(before);
		expect(link.draftLink.value.items).toEqual([]);
	});

	it("a change of the field's filter context closes the open menu, keeping the typed value", async () => {
		// Catches: the sync context watcher removed, so suggestions fetched for one Company stay
		// pickable after the user changes the Company.
		const company = { fieldname: "company", fieldtype: "Data", value: "A" };
		const filtered = customer({
			link_filters: [["Customer", "company", "=", "eval:doc.company"]],
		});
		const { link, draftPanel } = mountComposable({
			model: model([company, filtered]),
			searchLink: vi.fn(async () => rows("A")),
		});
		await search(link);
		expect(link.draftLink.value.open).toBe(true);
		company.value = "B";
		draftPanel.value = { ...draftPanel.value };
		expect(link.draftLink.value.open).toBe(false);
		expect(link.draftLink.value.items).toEqual([]);
	});

	it("a filter changed and changed back still drops the answer fetched before the change", async () => {
		// Catches: the object-identity guard removed. The watcher replaces the menu object but
		// does not bump the generation, and the context is equal again by the time the answer
		// lands, so only `draftLink.value !== search` stops the stale list from reopening.
		const company = { fieldname: "company", fieldtype: "Data", value: "A" };
		const filtered = customer({
			link_filters: [["Customer", "company", "=", "eval:doc.company"]],
		});
		const pending = deferred();
		const { link, draftPanel } = mountComposable({
			model: model([company, filtered]),
			searchLink: vi.fn(() => pending.promise),
		});
		const running = search(link);
		company.value = "B";
		draftPanel.value = { ...draftPanel.value };
		company.value = "A";
		draftPanel.value = { ...draftPanel.value };
		pending.resolve(rows("A"));
		await running;
		expect(link.draftLink.value.items).toEqual([]);
		expect(link.draftLink.value.open).toBe(false);
	});
});
