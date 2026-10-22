import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi } from "vitest";
import SharedTurnActivity from "./SharedTurnActivity.vue";
vi.mock("@/api/chatSharing", () => ({ getSharedToolDetail: vi.fn() }));
const tool = (name, status = "completed") => ({ name, tool_name: name, tool_status: status });
describe("shared reply activity", () => {
	it("shows one summary, ordered narration and individually expandable tools", async () => {
		const load = vi.fn(async () => ({ tool_result: "safe result" }));
		const w = mount(SharedTurnActivity, {
			props: {
				message: { name: "A", steps: [{ text: "Reading records" }] },
				tools: [tool("get_doc"), tool("search", "error")],
				loadDetail: load,
			},
		});
		expect(w.findAll(".jv-steps-box")).toHaveLength(1);
		expect(w.text()).toContain("2 tools");
		expect(load).not.toHaveBeenCalled();
		await w.get(".jv-steps-head").trigger("click");
		expect(w.findAll(".jv-tool")).toHaveLength(2);
		expect(w.text()).toContain("Reading records");
		expect(w.text()).toContain("Failed");
		expect(load).not.toHaveBeenCalled();
		await w.get(".jv-tool-head").trigger("click");
		await flushPromises();
		expect(load).toHaveBeenCalledWith("get_doc");
		expect(w.text()).toContain("safe result");
		w.unmount();
	});
	it("opens a single tool directly and keeps narration-only replies inspectable", async () => {
		const load = vi.fn(async () => ({ tool_result: "one result" }));
		const w = mount(SharedTurnActivity, {
			props: { message: { name: "A" }, tools: [tool("get_doc")], loadDetail: load },
		});
		expect(w.get("button").text()).toContain("get_doc");
		await w.get("button").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("one result");
		w.unmount();
		const steps = mount(SharedTurnActivity, {
			props: { message: { name: "B", steps: [{ text: "Reasoning" }] }, tools: [] },
		});
		await steps.get("button").trigger("click");
		expect(steps.text()).toContain("Reasoning");
		steps.unmount();
	});
	it("uses projected live narration while running and folds after settlement", async () => {
		const w = mount(SharedTurnActivity, {
			props: {
				message: {
					name: "A",
					content: "",
					live_steps: [{ text: "Checking the supplier" }],
				},
				turnState: "streaming",
				tools: [tool("get_doc")],
			},
		});
		expect(w.find(".jv-steps-live").exists()).toBe(true);
		expect(w.text()).toContain("Checking the supplier");
		await w.setProps({
			turnState: "done",
			message: { name: "A", content: "Done", steps: [{ text: "Checked the supplier" }] },
		});
		expect(w.find(".jv-steps-live").exists()).toBe(false);
		expect(w.text()).toContain("1 tool");
		w.unmount();
	});
	it("prefers durable narration over its retained live copy", async () => {
		const w = mount(SharedTurnActivity, {
			props: {
				message: {
					name: "A",
					content: "Done",
					steps: [{ text: "Checked supplier" }],
					live_steps: [{ text: "Checked supplier" }],
				},
				tools: [],
			},
		});
		await w.get("button").trigger("click");
		expect(w.findAll(".jv-tool-step")).toHaveLength(1);
		w.unmount();
	});
	it("does not call a successful reply failed when it recovered from a tool error", async () => {
		const w = mount(SharedTurnActivity, {
			props: {
				message: { name: "A", content: "Recovered answer" },
				turnState: "done",
				tools: [tool("get_doc"), tool("search", "error")],
			},
		});
		expect(w.get(".jv-steps-head").text()).not.toContain("Failed");
		await w.get(".jv-steps-head").trigger("click");
		expect(w.text()).toContain("Failed");
		await w.setProps({ turnState: "errored" });
		expect(w.get(".jv-steps-head").text()).toContain("Failed");
		await w.setProps({ turnState: "cancelled" });
		expect(w.get(".jv-steps-head").text()).toContain("Stopped");
		await w.setProps({ message: { name: "A", error_headline: "Request cancelled" } });
		expect(w.get(".jv-steps-head").text()).toContain("Stopped");
		expect(w.get(".jv-steps-head").text()).not.toContain("Failed");
		await w.setProps({
			turnState: "",
			message: { name: "A", stopped: 1, error_headline: "Request cancelled" },
		});
		expect(w.get(".jv-steps-head").text()).toContain("Stopped");
		expect(w.get(".jv-steps-head").text()).not.toContain("Failed");
		await w.setProps({
			turnState: "",
			message: { name: "A", error_headline: "Reply failed" },
		});
		expect(w.get(".jv-steps-head").text()).toContain("Failed");
		w.unmount();
	});
});
