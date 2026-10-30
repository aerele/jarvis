import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// api.js imports `call` from frappe-ui, which does not resolve under vitest.
vi.mock("frappe-ui", () => ({ call: vi.fn(async () => ({})) }));

import { getHolidayToday } from "./api.js";

const reply = (body, init = {}) =>
	Promise.resolve({
		ok: init.ok ?? true,
		status: init.status ?? 200,
		json: async () => body,
	});

describe("getHolidayToday", () => {
	let fetchStub;
	beforeEach(() => {
		fetchStub = vi.fn();
		vi.stubGlobal("fetch", fetchStub);
	});
	afterEach(() => vi.unstubAllGlobals());

	it("asks for today's answer with a GET, the session cookie, no cache and a 10 s limit", async () => {
		fetchStub.mockReturnValue(reply({ message: { v: 1, kind: "none" } }));
		await getHolidayToday();
		const [url, init] = fetchStub.mock.calls[0];
		expect(url).toBe("/api/method/jarvis.holiday_touch.api.get_today");
		expect(init).toMatchObject({
			method: "GET",
			credentials: "same-origin",
			cache: "no-store",
		});
		expect(init.signal).toBeInstanceOf(AbortSignal);
	});

	it("returns the message of a 200", async () => {
		const message = { v: 1, kind: "day", design: "diwali", occasion: "diwali" };
		fetchStub.mockReturnValue(reply({ message }));
		expect(await getHolidayToday()).toEqual(message);
	});

	it("rejects a 403 with its status, so the store can read it as none", async () => {
		fetchStub.mockReturnValue(reply({}, { ok: false, status: 403 }));
		await expect(getHolidayToday()).rejects.toMatchObject({ status: 403 });
	});

	it("rejects a 500 with its status", async () => {
		fetchStub.mockReturnValue(reply({}, { ok: false, status: 500 }));
		await expect(getHolidayToday()).rejects.toMatchObject({ status: 500 });
	});

	it("rejects a 200 whose message is not an object, so the old answer is kept", async () => {
		for (const body of [
			{},
			{ message: null },
			{ message: "nope" },
			{ message: 7 },
			{ message: [] },
		]) {
			fetchStub.mockReturnValue(reply(body));
			await expect(getHolidayToday(), JSON.stringify(body)).rejects.toMatchObject({
				malformed: true,
			});
		}
	});

	it("passes a network failure through untouched", async () => {
		fetchStub.mockRejectedValue(new TypeError("Failed to fetch"));
		await expect(getHolidayToday()).rejects.toThrow("Failed to fetch");
	});

	it("does without a timeout signal where the browser has none", async () => {
		const timeout = AbortSignal.timeout;
		AbortSignal.timeout = undefined;
		try {
			fetchStub.mockReturnValue(reply({ message: { v: 1, kind: "none" } }));
			await getHolidayToday();
			expect(fetchStub.mock.calls[0][1].signal).toBeUndefined();
		} finally {
			AbortSignal.timeout = timeout;
		}
	});
});
