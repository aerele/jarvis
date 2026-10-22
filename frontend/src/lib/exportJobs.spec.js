import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
	cancelExport,
	exportJobs,
	formatBytes,
	refreshExports,
	watchExport,
} from "./exportJobs.js";

const reply = (message) => ({ ok: true, json: async () => ({ message }) });
const defer = () => {
	let resolve;
	const promise = new Promise((r) => (resolve = r));
	return { promise, resolve };
};
const bodyOf = (call) => JSON.parse(call[1].body);
const urlOf = (call) => call[0];
const polls = () => fetch.mock.calls.filter((call) => urlOf(call).endsWith(".get_jobs"));

let unwatch;
beforeEach(() => {
	vi.useFakeTimers();
	unwatch = [];
	for (const id of Object.keys(exportJobs)) delete exportJobs[id];
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => reply({}))
	);
});
afterEach(() => {
	unwatch.forEach((off) => off());
	vi.useRealTimers();
	vi.unstubAllGlobals();
});
const watch = (id) => {
	unwatch.push(watchExport(id));
};

describe("refresh races", () => {
	it("runs a forced refresh asked for while a poll is in flight", async () => {
		const first = defer();
		fetch.mockImplementationOnce(() => first.promise);
		watch("a");
		await vi.advanceTimersByTimeAsync(1);
		expect(polls()).toHaveLength(1);
		fetch.mockResolvedValue(reply({ a: { status: "ready" } }));
		const forced = refreshExports(true, ["a"]);
		first.resolve(reply({ a: { status: "running" } }));
		await forced;
		await vi.advanceTimersByTimeAsync(1);
		// Mutation killed: dropping the pending merge leaves one request and the stale state.
		expect(polls()).toHaveLength(2);
		expect(exportJobs.a.state.status).toBe("ready");
	});

	it("merges queued refreshes: force wins, names are united, null means all", async () => {
		const first = defer();
		fetch.mockImplementationOnce(() => first.promise);
		watch("a");
		watch("b");
		watch("c");
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockResolvedValue(reply({}));
		refreshExports(true, ["a"]);
		refreshExports(true, ["b"]);
		first.resolve(reply({}));
		await vi.advanceTimersByTimeAsync(1);
		expect(polls()).toHaveLength(2);
		expect(bodyOf(polls()[1]).names.sort()).toEqual(["a", "b"]);
	});

	it("a poll that started before a cancel does not overwrite the cancel's state", async () => {
		const poll = defer();
		fetch.mockImplementation((url) =>
			url.endsWith(".get_jobs")
				? poll.promise
				: Promise.resolve(reply({ status: "cancelled" }))
		);
		watch("a");
		await vi.advanceTimersByTimeAsync(1);
		await cancelExport("a");
		expect(exportJobs.a.state.status).toBe("cancelled");
		poll.resolve(reply({ a: { status: "running" } }));
		await vi.advanceTimersByTimeAsync(1);
		// Mutation killed: removing the per-id sequence check lets the stale poll win.
		expect(exportJobs.a.state.status).toBe("cancelled");
	});

	it("does not write results for a card that unsubscribed meanwhile", async () => {
		const poll = defer();
		fetch.mockImplementationOnce(() => poll.promise);
		watch("a");
		const off = watchExport("b");
		await vi.advanceTimersByTimeAsync(1);
		off();
		poll.resolve(reply({ a: { status: "running" }, b: { status: "running" } }));
		await vi.advanceTimersByTimeAsync(1);
		// Mutation killed: removing the subscriber filter resurrects the entry for "b".
		expect(exportJobs.a.state.status).toBe("running");
		expect(exportJobs.b).toBeUndefined();
	});
});

describe("server errors", () => {
	const failure = (body) => ({ ok: false, status: 417, json: async () => body });
	const frappeMessage = (text) => ({
		_server_messages: JSON.stringify([JSON.stringify({ message: text })]),
	});

	it("shows the server's own text for a failed cancel, without markup", async () => {
		watch("a");
		await vi.advanceTimersByTimeAsync(1);
		exportJobs.a = { state: { status: "running" }, error: "" };
		fetch.mockResolvedValue(failure(frappeMessage("<b>Too late</b>: it already finished.")));
		const error = await cancelExport("a").catch((e) => e);
		expect(error.message).toBe("Too late: it already finished.");
		expect(error.method).toBe("cancel");
		// A failed cancel is the card's own alert, not a refresh failure on the entry.
		expect(exportJobs.a.error).toBe("");
		expect(exportJobs.a.state.status).toBe("running");
	});

	it("falls back to a per-method sentence when the body has nothing usable", async () => {
		watch("a");
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockResolvedValue({
			ok: false,
			status: 502,
			json: async () => Promise.reject(new Error("html")),
		});
		expect((await cancelExport("a").catch((e) => e)).message).toBe(
			"Could not cancel. The export may still be running."
		);
		await refreshExports(true, ["a"]);
		expect(exportJobs.a.error).toBe("Could not refresh the export.");
	});

	it("reads the exception text when there are no server messages", async () => {
		watch("a");
		await vi.advanceTimersByTimeAsync(1);
		fetch.mockResolvedValue(
			failure({ exception: "frappe.exceptions.ValidationError: Export not found" })
		);
		expect((await cancelExport("a").catch((e) => e)).message).toBe("Export not found");
	});
});

describe("formatBytes", () => {
	it("reads sizes the way a person would", () => {
		expect(formatBytes(500)).toBe("500 B");
		expect(formatBytes(2048)).toBe("2 KB");
		expect(formatBytes(1)).toBe("1 B");
		expect(formatBytes(1100)).toBe("1 KB");
		expect(formatBytes(3.4 * 1024 * 1024)).toBe("3.4 MB");
		expect(formatBytes(1048575)).toBe("1.0 MB");
		expect(formatBytes(null)).toBe("0 B");
	});
});
