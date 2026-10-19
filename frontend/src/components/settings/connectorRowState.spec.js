import { describe, it, expect } from "vitest";
import { isDisposableRow } from "./connectorRowState";

describe("isDisposableRow", () => {
	const fresh = { createdThisSession: true, oauthConnected: false, savedClient: false };

	it("a row created this session and never saved is disposable", () => {
		expect(isDisposableRow(fresh)).toBe(true);
	});

	it("an edited row that already existed is never disposable", () => {
		expect(isDisposableRow({ ...fresh, createdThisSession: false })).toBe(false);
	});

	it("a signed-in row is kept", () => {
		expect(isDisposableRow({ ...fresh, oauthConnected: true })).toBe(false);
	});

	it("a row with a saved app client is kept", () => {
		expect(isDisposableRow({ ...fresh, savedClient: true })).toBe(false);
	});
});
