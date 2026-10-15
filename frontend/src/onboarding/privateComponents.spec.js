import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "size", "iconLeft"],
		emits: ["click"],
		template: '<button type="button" @click="$emit(\'click\')">{{ label }}</button>',
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: '<i :data-icon="name" />' },
}));

import NetworkRulesTable from "./NetworkRulesTable.vue";
import ConnectionTestResult from "./ConnectionTestResult.vue";
import { adminRuleRows, serverRuleRows, rulesToText } from "./privateNetwork.js";

const SERVER_ROWS = serverRuleRows({ hostIp: "198.51.100.27", publicHost: "203.0.113.24" });
const ADMIN_ROWS = adminRuleRows({ ips: ["178.104.214.114"], host: "fleet.klerk.in" });

function setClipboard(writeText) {
	Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
}
afterEach(() => {
	Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
});

describe("NetworkRulesTable", () => {
	it("server variant: Direction, From, To and Port, with the two rules", () => {
		const w = mount(NetworkRulesTable, {
			props: { rows: SERVER_ROWS, variant: "server", title: "Rules for your network team" },
		});
		const headers = w.findAll('[role="columnheader"]').map((h) => h.text());
		expect(headers).toEqual(["Direction", "From", "To", "Port"]);
		const text = w.text();
		expect(text).toContain("Rules for your network team");
		expect(text).toContain("Outbound");
		expect(text).toContain("Your ERPNext server");
		expect(text).toContain("Inbound");
		expect(text).toContain("198.51.100.27");
		expect(text).toContain("203.0.113.24");
		expect(text).toContain("your public address");
		expect(text).toContain("TCP 443");
	});

	it("admin variant: Direction, Destination and Port, the host name beside the IP", () => {
		const w = mount(NetworkRulesTable, {
			props: { rows: ADMIN_ROWS, variant: "admin", title: "Rule for your network team", copyLabel: "Copy rule" },
		});
		expect(w.findAll('[role="columnheader"]').map((h) => h.text())).toEqual([
			"Direction",
			"Destination",
			"Port",
		]);
		expect(w.text()).toContain("178.104.214.114");
		expect(w.text()).toContain("fleet.klerk.in");
		expect(w.text()).toContain("TCP 443 (HTTPS)");
		expect(w.find("button").text()).toBe("Copy rule");
	});

	it("Copy writes the exact tab-separated rules and says Copied", async () => {
		const writeText = vi.fn(async () => {});
		setClipboard(writeText);
		const w = mount(NetworkRulesTable, { props: { rows: SERVER_ROWS, variant: "server" } });
		expect(w.find("button").text()).toBe("Copy rules");
		await w.find("button").trigger("click");
		await flushPromises();
		expect(writeText).toHaveBeenCalledWith(rulesToText(SERVER_ROWS, "server"));
		expect(w.find("button").text()).toBe("Copied");
	});

	it("a blocked clipboard leaves the label alone and does not throw", async () => {
		setClipboard(vi.fn(async () => { throw new Error("denied"); }));
		const w = mount(NetworkRulesTable, { props: { rows: SERVER_ROWS, variant: "server" } });
		await w.find("button").trigger("click");
		await flushPromises();
		expect(w.find("button").text()).toBe("Copy rules");
	});

	it("no clipboard API at all is also survivable", async () => {
		const w = mount(NetworkRulesTable, { props: { rows: SERVER_ROWS, variant: "server" } });
		await w.find("button").trigger("click");
		await flushPromises();
		expect(w.find("button").text()).toBe("Copy rules");
	});
});

describe("ConnectionTestResult", () => {
	const ROWS = [
		{ label: "Jarvis server → your ERPNext", state: "pass", detail: "Connected. The right ERPNext site answered." },
		{ label: "Your ERPNext → Jarvis server", state: "fail", detail: "Couldn’t connect to 198.51.100.27 on port 443." },
	];

	it("renders one row per half with its icon and detail", () => {
		const w = mount(ConnectionTestResult, { props: { rows: ROWS, banner: null } });
		expect(w.text()).toContain("Connection test");
		expect(w.text()).toContain("Jarvis server → your ERPNext");
		expect(w.text()).toContain("Couldn’t connect to 198.51.100.27 on port 443.");
		expect(w.find('[data-icon="check-circle"]').exists()).toBe(true);
		expect(w.find('[data-icon="x-circle"]').exists()).toBe(true);
	});

	it("a checking row shows the spinner, not a result icon", () => {
		const w = mount(ConnectionTestResult, {
			props: { rows: [{ label: "A", state: "checking", detail: "Checking…" }], banner: null },
		});
		expect(w.find('[data-icon="check-circle"]').exists()).toBe(false);
		expect(w.find('[data-icon="x-circle"]').exists()).toBe(false);
		expect(w.find('[role="status"]').exists()).toBe(true);
	});

	it("shows the banner title and text with the right alert role", () => {
		const success = mount(ConnectionTestResult, {
			props: { rows: ROWS, banner: { type: "success", title: "Connection OK", text: "Your network is ready for Jarvis. You can pay now." } },
		});
		expect(success.text()).toContain("Connection OK");
		expect(success.text()).toContain("You can pay now.");
		expect(success.find('[role="status"]').exists()).toBe(true);
		const warning = mount(ConnectionTestResult, {
			props: { rows: ROWS, banner: { type: "warning", title: "We couldn’t reach your public address", text: "Check the inbound rule." } },
		});
		expect(warning.find('[role="alert"]').exists()).toBe(true);
	});

	it("renders nothing for an idle test", () => {
		const w = mount(ConnectionTestResult, { props: { rows: [], banner: null } });
		expect(w.text()).toBe("");
	});
});
