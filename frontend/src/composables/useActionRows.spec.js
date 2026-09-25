import { mount, flushPromises } from "@vue/test-utils";
import { defineComponent } from "vue";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("@/api/approvals", () => ({ listPendingActionsLane: vi.fn() }));
vi.mock("@/api", () => ({ listWikiWriteProposals: vi.fn() }));

import * as approvals from "@/api/approvals";
import * as api from "@/api";
import { useActionRows, filterActionRows, actionRowType, actionRowTypes } from "./useActionRows";

const held = (over = {}) => ({
	name: "PA-1",
	kind: "file_box_held",
	status: "Pending",
	summary: "New supplier: Acme",
	document_type: "Supplier",
	waiters_count: 1,
	created_at: "2026-09-01 10:00:00",
	for_user: "",
	...over,
});
const chat = (over = {}) => ({
	name: "PA-9",
	kind: "chat",
	status: "Pending",
	summary: "Create a ToDo",
	document_type: "ToDo",
	created_at: "2026-09-01 09:00:00",
	conversation: "conv-1",
	conversation_title: "Quarter close",
	origin_page: "",
	...over,
});
const wiki = (over = {}) => ({
	name: "AR-7",
	title: "Fake Co",
	status: "Pending",
	can_approve: 1,
	needs_retry: 0,
	wiki_digest: "d1",
	dropper: "asha@example.com",
	creation: "2026-09-01 11:00:00",
	preview: { slug: "party-fake-co", page_type: "Reference", append_md: "note" },
	...over,
});

const sheet = (over = {}) => ({
	name: "PA-S1",
	kind: "file_box_sheet",
	status: "Pending",
	summary: "Approval sheet: loreal.pdf",
	file_name: "loreal.pdf",
	conversation: "conv-1",
	conversation_title: "Invoice from Loreal",
	document_type: "",
	collecting: 0,
	record_count: 101,
	question_count: 2,
	counts: { Supplier: 1, Item: 100 },
	counts_line: "1 supplier · 100 items · 2 questions",
	for_user: "",
	created_at: "2026-09-01 08:00:00",
	...over,
});

const hosts = [];
function host(socket = null) {
	const Host = defineComponent({ setup: () => useActionRows(), template: "<div/>" });
	const w = mount(Host, { global: { provide: { $socket: socket } } });
	hosts.push(w);
	return w;
}

function deferred() {
	let resolve, reject;
	const promise = new Promise((res, rej) => ((resolve = res), (reject = rej)));
	return { promise, resolve, reject };
}

describe("useActionRows", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		approvals.listPendingActionsLane.mockResolvedValue({ rows: [] });
		api.listWikiWriteProposals.mockResolvedValue({ rows: [], total: 0 });
	});
	afterEach(() => {
		// an unmounted host leaves no visibility listener behind for the next test
		hosts.splice(0).forEach((w) => w.unmount());
		vi.useRealTimers();
	});

	it("normalizes held, chat and wiki rows into one oldest-first list", async () => {
		approvals.listPendingActionsLane.mockResolvedValue({
			rows: [
				chat(),
				held({ status: "Executing", waiters_count: 3, for_user: "Asha" }),
				held({ name: "PA-2", summary: "", document_type: "" }),
			],
		});
		api.listWikiWriteProposals.mockResolvedValue({
			rows: [wiki({ needs_retry: 1, status: "Approved" })],
			total: 1,
		});
		const w = host();
		await flushPromises();
		const rows = w.vm.rows;
		expect(rows.map((r) => r.key)).toEqual([
			"chat:PA-9",
			"held:PA-1",
			"held:PA-2",
			"wiki:AR-7",
		]);
		const [c, h, bare, n] = rows;
		expect(c).toMatchObject({
			kind: "chat",
			title: "Create a ToDo",
			meta: "Quarter close",
			document_type: "ToDo",
			badge: { label: "Pending", theme: "orange" },
		});
		expect(h).toMatchObject({
			kind: "held",
			meta: "3 files waiting · dropped by Asha",
			badge: { label: "Creating…", theme: "blue" },
		});
		expect(bare.title).toBe("New record");
		expect(n).toMatchObject({
			kind: "wiki",
			title: "Fake Co",
			meta: "party-fake-co · Reference · dropped by asha@example.com",
			document_type: "",
			created_at: "2026-09-01 11:00:00",
			badge: { label: "Needs retry", theme: "red" },
		});
		expect(n.raw.wiki_digest).toBe("d1");
		expect(w.vm.loaded).toBe(true);
	});

	it("searches the summary, conversation title, for-user and wiki slug", async () => {
		approvals.listPendingActionsLane.mockResolvedValue({
			rows: [chat(), held({ for_user: "Asha Rao" })],
		});
		api.listWikiWriteProposals.mockResolvedValue({
			rows: [wiki({ title: "", preview: { slug: "party-fake-co" } })],
			total: 1,
		});
		const w = host();
		await flushPromises();
		const find = (search) => filterActionRows(w.vm.rows, { search }).map((r) => r.key);
		expect(find("quarter")).toEqual(["chat:PA-9"]);
		expect(find("to do")).toEqual([]);
		expect(find("todo")).toEqual(["chat:PA-9"]);
		expect(find("asha rao")).toEqual(["held:PA-1"]);
		expect(find("acme")).toEqual(["held:PA-1"]);
		expect(find("fake-co")).toEqual(["wiki:AR-7"]);
	});

	it("names a running chat card and falls back for a card without a summary", async () => {
		approvals.listPendingActionsLane.mockResolvedValue({
			rows: [chat({ status: "Executing", summary: "", conversation_title: "" })],
		});
		const w = host();
		await flushPromises();
		expect(w.vm.rows[0]).toMatchObject({
			title: "Action waiting",
			meta: "Chat",
			badge: { label: "Running…", theme: "blue" },
		});
	});

	it("is not loaded until both sources settle", async () => {
		const wikiCall = deferred();
		api.listWikiWriteProposals.mockReturnValue(wikiCall.promise);
		approvals.listPendingActionsLane.mockResolvedValue({ rows: [held()] });
		const w = host();
		await flushPromises();
		expect(w.vm.loaded).toBe(false);
		wikiCall.resolve({ rows: [], total: 0 });
		await flushPromises();
		expect(w.vm.loaded).toBe(true);
	});

	it("is loaded by the first answer it applies, and a burst of newer loads never starves it", async () => {
		const answers = [deferred(), deferred(), deferred()];
		approvals.listPendingActionsLane
			.mockReturnValueOnce(answers[0].promise)
			.mockReturnValueOnce(answers[1].promise)
			.mockReturnValueOnce(answers[2].promise);
		const names = (w) => w.vm.rows.map((r) => r.name);
		const w = host();
		w.vm.load();
		w.vm.load();
		await flushPromises();
		expect(w.vm.loaded).toBe(false);
		answers[0].resolve({ rows: [held({ name: "PA-OLD" })] });
		await flushPromises();
		expect(w.vm.loaded).toBe(true);
		expect(names(w)).toEqual(["PA-OLD"]);
		answers[2].resolve({ rows: [held({ name: "PA-NEW" })] });
		await flushPromises();
		answers[1].resolve({ rows: [held({ name: "PA-MID" })] });
		await flushPromises();
		expect(names(w)).toEqual(["PA-NEW"]);
	});

	it("an older wiki answer landing after a newer one is dropped", async () => {
		const wikiAnswers = [deferred(), deferred()];
		api.listWikiWriteProposals
			.mockReturnValueOnce(wikiAnswers[0].promise)
			.mockReturnValueOnce(wikiAnswers[1].promise);
		const w = host();
		w.vm.load();
		wikiAnswers[1].resolve({ rows: [], total: 0 });
		await flushPromises();
		expect(w.vm.loaded).toBe(true);
		wikiAnswers[0].resolve({ rows: [wiki()], total: 1 });
		await flushPromises();
		expect(w.vm.rows).toHaveLength(0);
	});

	it("asks for the oldest wiki notes first, as many as a page allows", async () => {
		host();
		await flushPromises();
		expect(api.listWikiWriteProposals).toHaveBeenCalledWith({
			order: "oldest",
			page_length: 100,
		});
	});

	it("a non-reviewer's wiki 403 is silent, and the list is never asked again", async () => {
		api.listWikiWriteProposals.mockRejectedValue(
			Object.assign(new Error("Not permitted"), { exc_type: "PermissionError" })
		);
		approvals.listPendingActionsLane.mockResolvedValue({ rows: [held()] });
		const w = host();
		await flushPromises();
		expect(w.vm.error).toBe("");
		expect(w.vm.rows.map((r) => r.key)).toEqual(["held:PA-1"]);
		await w.vm.load();
		expect(api.listWikiWriteProposals).toHaveBeenCalledTimes(1);
		expect(approvals.listPendingActionsLane).toHaveBeenCalledTimes(2);
	});

	it("a lane failure keeps the last rows and shows why until a retry succeeds", async () => {
		approvals.listPendingActionsLane.mockResolvedValueOnce({ rows: [held()] });
		const w = host();
		await flushPromises();
		approvals.listPendingActionsLane.mockRejectedValueOnce(new Error("boom"));
		await w.vm.load();
		expect(w.vm.error).toBe("boom");
		expect(w.vm.rows).toHaveLength(1);
		approvals.listPendingActionsLane.mockResolvedValueOnce({ rows: [] });
		await w.vm.load();
		expect(w.vm.error).toBe("");
		expect(w.vm.rows).toHaveLength(0);
	});

	it("a wiki failure other than a 403 is shown, not hidden", async () => {
		api.listWikiWriteProposals.mockRejectedValue(new Error("wiki down"));
		const w = host();
		await flushPromises();
		expect(w.vm.error).toBe("wiki down");
	});

	it("keeps the latest answer per source when an older one lands last", async () => {
		const first = deferred();
		approvals.listPendingActionsLane
			.mockReturnValueOnce(first.promise)
			.mockResolvedValueOnce({ rows: [held({ name: "PA-NEW" })] });
		const w = host();
		await w.vm.load();
		first.resolve({ rows: [held({ name: "PA-OLD" })] });
		await flushPromises();
		expect(w.vm.rows.map((r) => r.name)).toEqual(["PA-NEW"]);
	});

	it("counts the wiki notes shown against the reviewer's total", async () => {
		api.listWikiWriteProposals.mockResolvedValue({ rows: [wiki()], total: 34 });
		const w = host();
		await flushPromises();
		expect([w.vm.wikiShown, w.vm.wikiTotal]).toEqual([1, 34]);
	});

	it("a decided row never comes back from an answer asked for before the decision", async () => {
		approvals.listPendingActionsLane.mockResolvedValueOnce({ rows: [held(), chat()] });
		const w = host();
		await flushPromises();
		const inFlight = deferred();
		const next = deferred();
		approvals.listPendingActionsLane
			.mockReturnValueOnce(inFlight.promise)
			.mockReturnValueOnce(next.promise);
		w.vm.load(); // k: asked before the decision
		w.vm.remove("held:PA-1");
		w.vm.load(); // k+1
		inFlight.resolve({ rows: [held(), chat()] });
		await flushPromises();
		expect(w.vm.rows.map((r) => r.key)).toEqual(["chat:PA-9"]);
		next.reject(new Error("boom"));
		await flushPromises();
		expect(w.vm.error).toBe("boom");
		expect(w.vm.rows.map((r) => r.key)).toEqual(["chat:PA-9"]);
		// a later answer is the server's word again
		approvals.listPendingActionsLane.mockResolvedValueOnce({ rows: [held(), chat()] });
		await w.vm.load();
		expect(w.vm.rows.map((r) => r.key)).toEqual(["chat:PA-9", "held:PA-1"]);
	});

	it("a decided wiki note never comes back from an earlier wiki answer", async () => {
		api.listWikiWriteProposals.mockResolvedValueOnce({ rows: [wiki()], total: 1 });
		const w = host();
		await flushPromises();
		const inFlight = deferred();
		api.listWikiWriteProposals.mockReturnValueOnce(inFlight.promise);
		w.vm.load();
		w.vm.remove("wiki:AR-7");
		inFlight.resolve({ rows: [wiki()], total: 1 });
		await flushPromises();
		expect(w.vm.rows).toHaveLength(0);
	});

	it("an older 403 overtaken by a newer answer does not stop asking for wiki notes", async () => {
		const first = deferred();
		const second = deferred();
		api.listWikiWriteProposals
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(second.promise);
		const w = host();
		w.vm.load();
		first.reject(Object.assign(new Error("Not permitted"), { exc_type: "PermissionError" }));
		await flushPromises();
		second.resolve({ rows: [wiki()], total: 1 });
		await flushPromises();
		expect(w.vm.rows.map((r) => r.key)).toEqual(["wiki:AR-7"]);
		api.listWikiWriteProposals.mockResolvedValue({ rows: [], total: 0 });
		await w.vm.load();
		expect(api.listWikiWriteProposals).toHaveBeenCalledTimes(3);
	});

	it("remove drops one row by key", async () => {
		approvals.listPendingActionsLane.mockResolvedValue({ rows: [held(), chat()] });
		const w = host();
		await flushPromises();
		w.vm.remove("held:PA-1");
		expect(w.vm.rows.map((r) => r.key)).toEqual(["chat:PA-9"]);
	});

	it("refetches once per burst of frames that mean something is waiting or settled", async () => {
		vi.useFakeTimers();
		const handlers = {};
		const socket = { on: (ev, fn) => (handlers[ev] = fn), off: vi.fn() };
		const w = host(socket);
		await flushPromises();
		expect(approvals.listPendingActionsLane).toHaveBeenCalledTimes(1);
		handlers["jarvis:event"]({ kind: "approval:new" });
		handlers["jarvis:event"]({ kind: "action:pending" });
		handlers["jarvis:event"]({ kind: "run:end" });
		vi.advanceTimersByTime(1000);
		await flushPromises();
		expect(approvals.listPendingActionsLane).toHaveBeenCalledTimes(2);
		for (const kind of ["action:confirmed", "action:settled"]) {
			const calls = approvals.listPendingActionsLane.mock.calls.length;
			handlers["jarvis:event"]({ kind });
			vi.advanceTimersByTime(1000);
			await flushPromises();
			expect(approvals.listPendingActionsLane).toHaveBeenCalledTimes(calls + 1);
		}
		handlers["jarvis:event"]({ kind: "run:end" });
		vi.advanceTimersByTime(1000);
		await flushPromises();
		expect(approvals.listPendingActionsLane).toHaveBeenCalledTimes(4);
		hosts.splice(0).forEach((h) => h.unmount());
		expect(socket.off).toHaveBeenCalledWith("jarvis:event", handlers["jarvis:event"]);
	});

	it("a sheet reads as its file, its counts and its state; one still listing is marked", async () => {
		approvals.listPendingActionsLane.mockResolvedValue({
			rows: [
				sheet(),
				sheet({ name: "PA-S2", status: "Executing", for_user: "Asha" }),
				sheet({
					name: "PA-S3",
					collecting: 1,
					counts: {},
					counts_line: "",
					file_name: "",
					conversation_title: "",
				}),
				sheet({ name: "PA-S4", file_name: "" }),
			],
		});
		const w = host();
		await flushPromises();
		const [s1, s2, s3, s4] = w.vm.rows;
		expect(s1).toMatchObject({
			key: "sheet:PA-S1",
			kind: "sheet",
			title: "loreal.pdf",
			meta: "1 supplier · 100 items · 2 questions",
			types: ["Supplier", "Item"],
			collecting: false,
			badge: { label: "Pending", theme: "orange" },
		});
		expect(s2.meta).toBe("1 supplier · 100 items · 2 questions · dropped by Asha");
		expect(s2.badge).toEqual({ label: "Applying…", theme: "blue" });
		expect(s3).toMatchObject({
			title: "Approval sheet",
			meta: "Approval sheet",
			collecting: true,
			badge: { label: "Still listing…", theme: "gray" },
		});
		expect(s4.title).toBe("Invoice from Loreal"); // no file: its chat, never the summary
		expect(actionRowTypes(s3)).toEqual(["Unclassified"]);
		expect(keysOf(filterActionRows(w.vm.rows, { search: "LOREAL" }))).toEqual([
			"sheet:PA-S1",
			"sheet:PA-S2",
			"sheet:PA-S4",
		]);
		expect(keysOf(filterActionRows(w.vm.rows, { search: "100 items" }))).toHaveLength(3);
		expect(keysOf(filterActionRows(w.vm.rows, { document_type: "Item" }))).toEqual([
			"sheet:PA-S1",
			"sheet:PA-S2",
			"sheet:PA-S4",
		]);
	});

	it("refetches when a sheet's apply starts, ends or bounces, not on each step", async () => {
		vi.useFakeTimers();
		const handlers = {};
		const socket = { on: (ev, fn) => (handlers[ev] = fn), off: vi.fn() };
		host(socket);
		await flushPromises();
		const frame = (state, done) =>
			handlers["jarvis:event"]({
				kind: "sheet:progress",
				name: "PA-S1",
				state,
				done,
				total: 9,
			});
		const calls = () => approvals.listPendingActionsLane.mock.calls.length;
		for (const [state, done, more] of [
			["applying", 0, 1],
			["applying", 10, 0],
			["applied", 9, 1],
			["returned", 0, 1],
		]) {
			const before = calls();
			frame(state, done);
			vi.advanceTimersByTime(1000);
			await flushPromises();
			expect(calls()).toBe(before + more);
		}
	});

	it("refetches when the tab becomes visible", async () => {
		const w = host();
		await flushPromises();
		document.dispatchEvent(new Event("visibilitychange"));
		await flushPromises();
		expect(approvals.listPendingActionsLane).toHaveBeenCalledTimes(2);
		hosts.splice(0).forEach((h) => h.unmount());
		document.dispatchEvent(new Event("visibilitychange"));
		await flushPromises();
		expect(approvals.listPendingActionsLane).toHaveBeenCalledTimes(2);
	});
});

const keysOf = (list) => list.map((r) => r.key);

describe("filterActionRows", () => {
	const rows = [
		{
			key: "held:PA-1",
			kind: "held",
			document_type: "Supplier",
			search: "new supplier: acme asha",
		},
		{ key: "held:PA-2", kind: "held", document_type: " ", search: "new record" },
		{
			key: "chat:PA-9",
			kind: "chat",
			document_type: "ToDo",
			search: "create a todo quarter close",
		},
		{ key: "wiki:AR-7", kind: "wiki", document_type: "", search: "fake co party-fake-co" },
	];
	const keys = (list) => list.map((r) => r.key);

	it("passes everything with no search and no type", () => {
		expect(keys(filterActionRows(rows, {}))).toEqual(keys(rows));
		expect(keys(filterActionRows(rows))).toEqual(keys(rows));
	});

	it("searches the row's words, case-insensitively and trimmed", () => {
		expect(keys(filterActionRows(rows, { search: "  QUARTER " }))).toEqual(["chat:PA-9"]);
		expect(keys(filterActionRows(rows, { search: "party-fake" }))).toEqual(["wiki:AR-7"]);
		expect(keys(filterActionRows(rows, { search: "asha" }))).toEqual(["held:PA-1"]);
	});

	it("filters by type; a blank type reads Unclassified; wiki notes have none", () => {
		expect(keys(filterActionRows(rows, { document_type: "Supplier" }))).toEqual(["held:PA-1"]);
		expect(keys(filterActionRows(rows, { document_type: "Unclassified" }))).toEqual([
			"held:PA-2",
		]);
		expect(actionRowType(rows[1])).toBe("Unclassified");
	});
});
