// The seven bench methods the private-network onboarding calls (jarvis/private_network.py).
import { describe, it, expect, beforeEach, vi } from "vitest";

// The real call(), so the HTTP method that reaches fetch is what is asserted. Wrapped in a spy
// so the argument shape can be asserted too.
vi.mock("frappe-ui", async () => {
	const real = (await import("../node_modules/frappe-ui/src/utils/call.js")).default;
	return { call: vi.fn(real) };
});

import { call } from "frappe-ui";
import {
	networkStatus,
	privateSetupTargets,
	checkAdminConnection,
	savePublicAddress,
	reserveHost,
	startConnectionTest,
	connectionTestStatus,
} from "./api.js";

const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ message: {} }) }));
beforeEach(() => {
	call.mockClear();
	fetchMock.mockClear();
	vi.stubGlobal("fetch", fetchMock);
});

describe("private network api wrappers", () => {
	it("calls the exact bench methods", async () => {
		await networkStatus();
		expect(call).toHaveBeenLastCalledWith("jarvis.private_network.network_status");
		await privateSetupTargets();
		expect(call).toHaveBeenLastCalledWith("jarvis.private_network.private_setup_targets");
		await checkAdminConnection();
		expect(call).toHaveBeenLastCalledWith("jarvis.private_network.check_admin_connection");
		await reserveHost();
		expect(call).toHaveBeenLastCalledWith("jarvis.private_network.reserve_host");
		await startConnectionTest();
		expect(call).toHaveBeenLastCalledWith("jarvis.private_network.start_connection_test");
	});

	it("passes the address and the check name as the only arguments", async () => {
		await savePublicAddress("https://erp.example.com");
		expect(call).toHaveBeenLastCalledWith("jarvis.private_network.save_public_address", {
			address: "https://erp.example.com",
		});
		await connectionTestStatus("JCC-000123");
		expect(call).toHaveBeenLastCalledWith("jarvis.private_network.connection_test_status", {
			check: "JCC-000123",
		});
	});

	it("startConnectionTest sends no target: the bench takes it from its stored hold", async () => {
		await startConnectionTest();
		expect(call.mock.calls.at(-1)).toHaveLength(1);
	});

	it("POSTs every endpoint: the bench refuses a GET for the three that change state", async () => {
		await networkStatus();
		await privateSetupTargets();
		await checkAdminConnection();
		await savePublicAddress("https://erp.example.com");
		await reserveHost();
		await startConnectionTest();
		await connectionTestStatus("JCC-000123");
		expect(fetchMock).toHaveBeenCalledTimes(7);
		for (const [url, init] of fetchMock.mock.calls) {
			expect(init.method, url).toBe("POST");
		}
		const urls = fetchMock.mock.calls.map(([url]) => url);
		expect(urls).toContain("/api/method/jarvis.private_network.save_public_address");
		expect(urls).toContain("/api/method/jarvis.private_network.reserve_host");
		expect(urls).toContain("/api/method/jarvis.private_network.start_connection_test");
	});
});
