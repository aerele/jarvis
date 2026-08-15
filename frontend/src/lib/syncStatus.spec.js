import { describe, it, expect } from "vitest";
import {
	humaniseSyncStatus,
	isSyncPending,
	isSyncFailed,
	isSyncDisconnected,
} from "@/lib/syncStatus";

// Every real value below was copied from a backend write site, not invented, so a
// wording change in jarvis/onboarding.py that breaks the prefix contract fails here.
const REAL_PENDING = [
	"pending: provisioning container (pool)",
	"pending: admin applying config",
	"pending: applying skills",
];
const REAL_OK = ["ok", "ok (restart via admin)", "ok (pool_update via admin)"];

describe("humaniseSyncStatus", () => {
	it("reads an empty status as never-synced rather than as an error", () => {
		for (const raw of ["", "   ", null, undefined]) {
			expect(humaniseSyncStatus(raw)).toEqual({
				kind: "unknown",
				text: "Not synced yet",
				detail: "",
			});
		}
	});

	it("maps every pending variant to one in-progress sentence", () => {
		for (const raw of REAL_PENDING) {
			expect(humaniseSyncStatus(raw)).toEqual({
				kind: "pending",
				text: "Applying your changes",
				detail: "",
			});
		}
	});

	it("maps every ok variant to one settled sentence", () => {
		for (const raw of REAL_OK) {
			expect(humaniseSyncStatus(raw)).toEqual({
				kind: "ok",
				text: "Up to date",
				detail: "",
			});
		}
	});

	// The bug this module exists for: "restart via admin" is an audit note about a
	// restart that already happened, and it was on screen reading like a chore.
	it("never turns an ok status into an instruction", () => {
		const { text } = humaniseSyncStatus("ok (restart via admin)");
		expect(text).toBe("Up to date");
		expect(text.toLowerCase()).not.toContain("restart");
		expect(text.toLowerCase()).not.toContain("admin");
	});

	it("splits a failure into a short label and a separate reason", () => {
		expect(humaniseSyncStatus("failed: fleet returned 502")).toEqual({
			kind: "failed",
			text: "Last sync failed",
			detail: "fleet returned 502",
		});
		// No colon, and no reason at all, both stay safe.
		expect(humaniseSyncStatus("failed")).toEqual({
			kind: "failed",
			text: "Last sync failed",
			detail: "",
		});
		expect(humaniseSyncStatus("failed connecting to admin")).toEqual({
			kind: "failed",
			text: "Last sync failed",
			detail: "connecting to admin",
		});
	});

	it("flattens and caps a failure reason so a traceback cannot blow out the layout", () => {
		const { detail } = humaniseSyncStatus("failed: line one\n  line two\n\tline three");
		expect(detail).toBe("line one line two line three");

		const long = humaniseSyncStatus("failed: " + "x".repeat(500)).detail;
		expect(long.length).toBeLessThanOrEqual(240);
		expect(long.endsWith("…")).toBe(true);
	});

	it("degrades an unrecognised status without ever rendering it", () => {
		for (const raw of ["queued", "PARTIAL: 2 of 3", "{'state': 'weird'}", "42"]) {
			const out = humaniseSyncStatus(raw);
			expect(out.kind).toBe("unknown");
			expect(out.text).toBe("Status unavailable");
			expect(out.detail).toBe("");
			expect(out.text).not.toContain(raw);
		}
	});

	it("is case insensitive and tolerates surrounding whitespace", () => {
		expect(humaniseSyncStatus("  OK (restart via admin)  ").kind).toBe("ok");
		expect(humaniseSyncStatus("Pending: whatever").kind).toBe("pending");
		expect(humaniseSyncStatus("FAILED: nope").detail).toBe("nope");
	});

	it("accepts a non-string without throwing", () => {
		expect(humaniseSyncStatus(7).kind).toBe("unknown");
		expect(humaniseSyncStatus({}).kind).toBe("unknown");
	});
});

describe("isSyncPending / isSyncFailed", () => {
	it("agree with the kind they wrap", () => {
		expect(isSyncPending("pending: anything")).toBe(true);
		expect(isSyncPending("ok")).toBe(false);
		expect(isSyncFailed("failed: nope")).toBe(true);
		expect(isSyncFailed("")).toBe(false);
	});
});

// jarvis#574: BOTH teardown paths write this exact marker, jarvis/onboarding.py's
// _DISCONNECTED_LLM_FIELDS and jarvis/oauth/api.py's disconnect. It used to fall
// through to "unknown", so a disconnect that deleted every credential showed
// nothing, and the customer could not tell a success from an operation that never
// ran. Copied from the backend write site, not invented.
describe("humaniseSyncStatus, disconnected", () => {
	it("names a deliberate teardown instead of reading it as a fault", () => {
		const out = humaniseSyncStatus("disconnected");
		expect(out.kind).toBe("disconnected");
		expect(out.text).toBe("Disconnected");
	});

	it("is neither pending nor failed, so no poller waits on it", () => {
		expect(isSyncPending("disconnected")).toBe(false);
		expect(isSyncFailed("disconnected")).toBe(false);
		expect(isSyncDisconnected("disconnected")).toBe(true);
	});

	it("does not claim a disconnect for any other status", () => {
		for (const s of ["", "ok", "ok (pool via admin)", "pending: anything", "failed: nope"]) {
			expect(isSyncDisconnected(s)).toBe(false);
		}
	});
});
