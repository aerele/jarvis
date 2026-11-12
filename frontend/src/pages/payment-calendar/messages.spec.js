import { describe, expect, it } from "vitest";
import { explain } from "./messages";

describe("explain", () => {
	it("does not blame access for an internal error", () => {
		const text = explain("internal_error");
		expect(text).toMatch(/something went wrong/i);
		expect(text).not.toMatch(/access|permission|finance/i);
	});
	it("keeps the access copy for the generic fallback", () => {
		expect(explain("no_such_code")).toMatch(/finance access/i);
	});
	it("uses no snapshot jargon in the expiry copy", () => {
		expect(explain("expired")).not.toMatch(/snapshot/i);
		expect(explain("not_found_or_expired")).not.toMatch(/snapshot/i);
	});
});

describe("recovery limit copy", () => {
	it("tells the user to refresh and blames neither access nor expiry", () => {
		expect(explain("recovery_limit")).toBe(
			"The calendar keeps changing. Refresh to try again."
		);
	});
});
