import { describe, it, expect, vi, afterEach } from "vitest";
import { getSharedConversation, setChatShares } from "./chatSharing";
afterEach(() => vi.unstubAllGlobals());
describe("sharing API boundary", () => {
	it.each(["not_shared", "sharing_off", "owner"])(
		"preserves authoritative %s refusal",
		async (reason) => {
			vi.stubGlobal(
				"fetch",
				vi.fn(async () => ({
					ok: false,
					status: 403,
					json: async () => ({ message: { refused: reason } }),
				}))
			);
			expect(await getSharedConversation("C1")).toEqual({ refused: reason });
		}
	);
	it("posts shares with the real session CSRF token", async () => {
		window.csrf_token = "csrf-fixture";
		const fetch = vi.fn(async () => ({
			ok: true,
			status: 200,
			json: async () => ({ message: { enabled: true, users: [] } }),
		}));
		vi.stubGlobal("fetch", fetch);
		await setChatShares("C1", ["reader@example.com"]);
		const [path, options] = fetch.mock.calls[0];
		expect(path).toContain("jarvis.chat.sharing.set_shares");
		expect(options.method).toBe("POST");
		expect(options.headers["X-Frappe-CSRF-Token"]).toBe("csrf-fixture");
		expect(JSON.parse(options.body)).toEqual({
			conversation: "C1",
			users: ["reader@example.com"],
		});
	});
	it("does not mistake a proxy failure for permission denial", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => ({
				ok: false,
				status: 502,
				json: async () => {
					throw new Error("not json");
				},
			}))
		);
		await expect(getSharedConversation("C1")).rejects.toMatchObject({ status: 502 });
	});
});
