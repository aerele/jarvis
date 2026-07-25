import { describe, it, expect, vi, beforeEach } from "vitest";

// router/index.js statically imports ChatView.vue (the whole chat SPA) and
// onboarding/readiness.js — both are stubbed so importing the module here
// doesn't pull in that entire graph. Every other page route is a dynamic
// `() => import(...)`, so it never executes just from importing this module.
vi.mock("@/views/ChatView.vue", () => ({ default: { name: "ChatView", template: "<div/>" } }));
vi.mock("@/onboarding/readiness.js", () => ({ isWorkspaceReady: vi.fn(async () => true) }));

import { supportGuard } from "@/router/index.js";

describe("supportGuard", () => {
	beforeEach(() => {
		window.support_available = false;
		window.has_support_access = false;
	});

	it("allows navigation when both flags are true", () => {
		window.support_available = true;
		window.has_support_access = true;
		let arg;
		supportGuard({}, {}, (v) => (arg = v));
		expect(arg).toBeUndefined();
	});

	it("redirects to Chat when support is fleet-wide disabled", () => {
		window.support_available = false;
		window.has_support_access = true;
		let arg;
		supportGuard({}, {}, (v) => (arg = v));
		expect(arg).toEqual({ name: "Chat" });
	});

	it("redirects to Chat when this user lacks support access", () => {
		window.support_available = true;
		window.has_support_access = false;
		let arg;
		supportGuard({}, {}, (v) => (arg = v));
		expect(arg).toEqual({ name: "Chat" });
	});

	it("redirects to Chat when both flags are false", () => {
		let arg;
		supportGuard({}, {}, (v) => (arg = v));
		expect(arg).toEqual({ name: "Chat" });
	});
});
