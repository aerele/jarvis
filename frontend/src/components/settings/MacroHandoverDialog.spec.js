import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * Who an admin hands another user's macro to. What matters: the people offered
 * are the server's search results; the owner and a full user cannot be picked;
 * nothing is sent until someone is picked, and then the dialog says what arrives
 * (off, unscheduled, not armed, summary, step skills, shares and assignments
 * cleared) naming the macro and both owners; a refusal stays in the dialog;
 * names go into the toast escaped.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "loading", "disabled", "theme"],
		emits: ["click"],
		template: `<button class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" @click="$emit('click')">{{ label }}</button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="stub-dialog"><h4>{{ options && options.title }}</h4><slot name="body-content" /><slot name="actions" /></div>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<span class="stub-error">{{ message }}</span>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["type", "modelValue", "label", "disabled"],
		emits: ["update:modelValue"],
		template: `<input class="stub-search" :value="modelValue" :disabled="disabled" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
}));

const api = vi.hoisted(() => ({ adminHandover: vi.fn(), adminHandoverTargets: vi.fn() }));
vi.mock("@/api/macrosAdmin", () => api);

import { toast } from "frappe-ui";
import MacroHandoverDialog from "./MacroHandoverDialog.vue";

const USERS = [
	{ user: "asha@example.test", full_name: "Asha Rao", macros: 3 },
	{ user: "ben@example.test", full_name: "Ben Ode", macros: 0 },
	{ user: "full@example.test", full_name: "Full User", macros: 25 },
];

async function open(props = {}) {
	const w = mount(MacroHandoverDialog, {
		props: {
			modelValue: true,
			name: "m1",
			macroName: "Month end",
			owner: "asha@example.test",
			ownerLabel: "Asha Rao (asha@example.test)",
			...props,
		},
	});
	await flushPromises();
	return w;
}
const handButton = (w) => w.findAll("button.stub-button").find((b) => b.text() === "Hand over");
const radio = (w, user) => w.find(`.jv-macro-handover-user[data-user="${user}"] input`);
async function pick(w, user) {
	await radio(w, user).setValue(true);
}

beforeEach(() => {
	vi.clearAllMocks();
	api.adminHandoverTargets.mockResolvedValue({ users: USERS, max: 25 });
	api.adminHandover.mockResolvedValue({
		ok: true,
		handed_over: true,
		new_owner: "ben@example.test",
		stopped_runs: 0,
	});
});
afterEach(() => vi.useRealTimers());

describe("MacroHandoverDialog", () => {
	it("names the macro and offers the server's users, the owner and a full user not pickable", async () => {
		const w = await open();
		expect(w.find("h4").text()).toBe("Hand over “Month end”");
		expect(api.adminHandoverTargets).toHaveBeenCalledWith("");
		const rows = w.findAll(".jv-macro-handover-user");
		expect(rows.map((r) => r.attributes("data-user"))).toEqual(USERS.map((u) => u.user));
		expect(radio(w, "asha@example.test").attributes("disabled")).toBe("");
		expect(rows[0].text()).toContain("owns it");
		expect(radio(w, "full@example.test").attributes("disabled")).toBe("");
		expect(rows[2].text()).toContain("has 25 macros, the most");
		expect(radio(w, "ben@example.test").attributes("disabled")).toBeUndefined();
	});

	it("sends nothing until someone is picked", async () => {
		const w = await open();
		expect(handButton(w).attributes("disabled")).toBe("");
		expect(w.find(".jv-macro-handover-summary").exists()).toBe(false);
		await handButton(w).trigger("click");
		expect(api.adminHandover).not.toHaveBeenCalled();
	});

	it("says what arrives, naming the macro and both owners, once someone is picked", async () => {
		const w = await open();
		await pick(w, "ben@example.test");
		const said = w.find(".jv-macro-handover-summary").text().replace(/\s+/g, " ");
		expect(said).toContain(
			"Hand “Month end” from Asha Rao (asha@example.test) to Ben Ode (ben@example.test)?"
		);
		expect(said).toContain("It arrives switched off, unscheduled and not armed.");
		expect(said).toContain(
			"Its summary, the skills on its steps, its shares and its assignments are cleared"
		);
		expect(said).not.toContain(String.fromCharCode(0x2014)); // no em-dash
		expect(handButton(w).attributes("disabled")).toBeUndefined();
	});

	it("hands over, says so, tells the pane and closes", async () => {
		api.adminHandover.mockResolvedValue({
			ok: true,
			handed_over: true,
			new_owner: "ben@example.test",
			stopped_runs: 1,
		});
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(api.adminHandover).toHaveBeenCalledWith("m1", "ben@example.test");
		expect(toast.success).toHaveBeenCalledWith(
			"Handed “Month end” to Ben Ode (ben@example.test); 1 run stopped"
		);
		expect(w.emitted("handed")).toEqual([[{ name: "m1", new_owner: "ben@example.test" }]]);
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});

	it("shows the hand-over in flight and does not send it twice", async () => {
		let answer;
		api.adminHandover.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		expect(handButton(w).attributes("data-loading")).toBe("1");
		await handButton(w).trigger("click");
		expect(api.adminHandover).toHaveBeenCalledTimes(1);
		answer({ ok: true, new_owner: "ben@example.test", stopped_runs: 0 });
		await flushPromises();
	});

	it("keeps a refusal in the dialog, stays open, and tells the pane to re-read", async () => {
		api.adminHandover.mockRejectedValue(
			new Error("ben@example.test already has a macro named Month end.")
		);
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toContain("already has a macro named Month end");
		expect(w.emitted("handed")).toBeUndefined();
		expect(w.emitted("update:modelValue")).toBeUndefined();
		expect(w.emitted("failed")).toEqual([[{ name: "m1" }]]);
		expect(toast.success).not.toHaveBeenCalled();
	});

	it("puts names in the toast escaped", async () => {
		api.adminHandoverTargets.mockResolvedValue({
			users: [{ user: "x@example.test", full_name: "<b>X</b>", macros: 0 }],
			max: 25,
		});
		api.adminHandover.mockResolvedValue({
			ok: true,
			new_owner: "x@example.test",
			stopped_runs: 0,
		});
		const w = await open({ macroName: "<img src=x onerror=alert(1)>" });
		await pick(w, "x@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(toast.success.mock.calls[0][0]).toBe(
			"Handed “&lt;img src=x onerror=alert(1)&gt;” to &lt;b&gt;X&lt;/b&gt; (x@example.test)"
		);
	});

	it("searches when the typing stops, and drops a pick the new results do not show", async () => {
		vi.useFakeTimers();
		const w = await open();
		await pick(w, "ben@example.test");
		api.adminHandoverTargets.mockResolvedValue({ users: [USERS[2]], max: 25 });
		await w.find(".stub-search").setValue("ful");
		expect(api.adminHandoverTargets).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(api.adminHandoverTargets).toHaveBeenLastCalledWith("ful");
		expect(w.findAll(".jv-macro-handover-user")).toHaveLength(1);
		expect(handButton(w).attributes("disabled")).toBe("");
		expect(w.find(".jv-macro-handover-summary").exists()).toBe(false);
	});

	it("says when nobody matches, and when the users cannot be loaded", async () => {
		api.adminHandoverTargets.mockResolvedValue({ users: [], max: 25 });
		let w = await open();
		expect(w.text()).toContain("Nobody with access to Jarvis matches.");
		w.unmount();
		api.adminHandoverTargets.mockRejectedValue(new Error("Not permitted"));
		w = await open();
		expect(w.find(".jv-macro-handover-load-error").text()).toBe("Not permitted");
		expect(handButton(w).attributes("disabled")).toBe("");
	});

	it("starts over each time it opens", async () => {
		const w = await open();
		await pick(w, "ben@example.test");
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(api.adminHandoverTargets).toHaveBeenCalledTimes(2);
		expect(w.find(".jv-macro-handover-summary").exists()).toBe(false);
	});
});
