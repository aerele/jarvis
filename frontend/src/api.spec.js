// Contract test for the api.js send path (vitest). The Action-menu / trigger
// feature rides on sendMessage forwarding a page marker to the backend; this
// pins exactly WHICH contexts cross the wire, so a future edit to the allow-list
// in sendMessage() cannot silently drop (or leak) a page marker.
import { describe, it, expect, beforeEach, vi } from "vitest";

// api.js imports `call` from frappe-ui; the ESM entry does not resolve under
// vitest, so we stub the one function it uses and read its recorded args.
vi.mock("frappe-ui", () => ({ call: vi.fn(async () => ({})) }));

import { call } from "frappe-ui";
import { sendMessage, setSidebarOrder } from "./api.js";

// The args object handed to `call("jarvis.chat.api.send_message", args)`.
function lastSendArgs() {
	const sends = call.mock.calls.filter((c) => c[0] === "jarvis.chat.api.send_message");
	return sends.length ? sends[sends.length - 1][1] : null;
}

beforeEach(() => call.mockClear());

describe("sendMessage context forwarding", () => {
	it("posts to the send_message endpoint with conversation + message", async () => {
		await sendMessage("C1", "hi");
		expect(call).toHaveBeenCalledWith("jarvis.chat.api.send_message", expect.any(Object));
		const args = lastSendArgs();
		expect(args.conversation).toBe("C1");
		expect(args.message).toBe("hi");
		expect(args.context).toBeUndefined();
	});

	it("forwards a triggers page marker as a JSON string", async () => {
		await sendMessage("C1", "make a trigger", undefined, undefined, { page: "triggers" });
		const args = lastSendArgs();
		expect(typeof args.context).toBe("string");
		expect(JSON.parse(args.context)).toEqual({ page: "triggers" });
	});

	it("forwards a dashboards page marker", async () => {
		await sendMessage("C1", "build a dashboard", undefined, undefined, { page: "dashboards" });
		expect(JSON.parse(lastSendArgs().context)).toEqual({ page: "dashboards" });
	});

	it("does NOT forward an unknown page marker", async () => {
		await sendMessage("C1", "hi", undefined, undefined, { page: "bogus" });
		expect(lastSendArgs().context).toBeUndefined();
	});

	it("does NOT forward an empty context", async () => {
		await sendMessage("C1", "hi", undefined, undefined, {});
		expect(lastSendArgs().context).toBeUndefined();
	});

	it("still forwards a viewing-context doctype (regression)", async () => {
		await sendMessage("C1", "hi", undefined, undefined, {
			doctype: "Sales Invoice",
			name: "S-1",
		});
		expect(JSON.parse(lastSendArgs().context)).toMatchObject({ doctype: "Sales Invoice" });
	});

	it("still forwards the one-shot ground_wiki flag (regression)", async () => {
		await sendMessage("C1", "hi", undefined, undefined, { ground_wiki: 1 });
		expect(JSON.parse(lastSendArgs().context)).toMatchObject({ ground_wiki: 1 });
	});

	it("serialises attachments and model override when given", async () => {
		await sendMessage("C1", "hi", "gpt-x", [{ file_url: "/f.png" }]);
		const args = lastSendArgs();
		expect(args.model_override).toBe("gpt-x");
		expect(JSON.parse(args.attachments)).toEqual([{ file_url: "/f.png" }]);
	});
});

describe("setSidebarOrder", () => {
	it("posts the order as a JSON string to the user-settings endpoint", async () => {
		await setSidebarOrder({ top: ["Chat", "Dashboards"], more: ["Skills"] });
		expect(call).toHaveBeenCalledWith("jarvis.chat.user_settings_api.set_sidebar_order", {
			order: JSON.stringify({ top: ["Chat", "Dashboards"], more: ["Skills"] }),
		});
	});

	it("sends an empty object rather than undefined when called with nothing", async () => {
		await setSidebarOrder();
		expect(call).toHaveBeenCalledWith("jarvis.chat.user_settings_api.set_sidebar_order", {
			order: "{}",
		});
	});
});
