import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * Who an admin hands another user's macro to. What matters: the people offered
 * are the server's search results; the owner and a full user cannot be picked;
 * nothing is sent until someone is picked, and then the dialog says what arrives
 * (off, unscheduled, not armed, summary, step skills, shares and assignments
 * cleared) naming the macro and both owners; a refusal stays in the dialog;
 * names go into the toast escaped. The owner the dialog showed is sent with the
 * hand-over (the server refuses one that changed hands); a cut list says so; the
 * dialog cannot be closed while the hand-over is in flight; an unavailable person
 * stays focusable with the reason readable, and cannot be chosen.
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
		// Not `disabled`: a keyboard or screen reader user reaches them and hears why.
		for (const [user, row, why] of [
			["asha@example.test", rows[0], "is already its owner"],
			["full@example.test", rows[2], "has 25 macros, the most"],
		]) {
			const r = radio(w, user);
			expect(r.attributes("disabled")).toBeUndefined();
			expect(r.attributes("aria-disabled")).toBe("true");
			expect(row.text()).toContain(why);
			const reason = w.find(`#${r.attributes("aria-describedby")}`);
			expect(reason.text()).toBe(why);
		}
		expect(radio(w, "ben@example.test").attributes("aria-disabled")).toBeUndefined();
		expect(radio(w, "ben@example.test").attributes("aria-describedby")).toBeUndefined();
	});

	it("does not let an unavailable person be chosen", async () => {
		const w = await open();
		for (const user of ["asha@example.test", "full@example.test"]) {
			const r = radio(w, user);
			await r.trigger("click");
			await r.setValue(true);
			expect(r.element.checked).toBe(false);
			expect(w.find(".jv-macro-handover-summary").exists()).toBe(false);
			expect(handButton(w).attributes("disabled")).toBe("");
		}
		// With someone picked, landing on an unavailable person keeps the pick checked.
		await pick(w, "ben@example.test");
		await radio(w, "full@example.test").setValue(true);
		expect(radio(w, "ben@example.test").element.checked).toBe(true);
		expect(radio(w, "full@example.test").element.checked).toBe(false);
		expect(w.find(".jv-macro-handover-summary").text()).toContain("Ben Ode");
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
		// With the owner it showed: the server refuses a macro that changed hands since.
		expect(api.adminHandover).toHaveBeenCalledWith(
			"m1",
			"ben@example.test",
			"asha@example.test"
		);
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

	it("cannot be closed while the hand-over is in flight", async () => {
		let answer;
		api.adminHandover.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		// Escape or a click outside: the Dialog asks to close.
		w.findComponent({ name: "Dialog" }).vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(w.emitted("update:modelValue")).toBeUndefined();
		answer({ ok: true, new_owner: "ben@example.test", stopped_runs: 0 });
		await flushPromises();
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
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
		expect(w.emitted("failed")).toEqual([
			[{ name: "m1", message: "ben@example.test already has a macro named Month end." }],
		]);
		expect(toast.success).not.toHaveBeenCalled();
	});

	it("sends the owner it is given now: the pane refreshes it after a refusal", async () => {
		api.adminHandover.mockRejectedValue(
			new Error("This macro changed hands since the list was loaded. Reload and try again.")
		);
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		await w.setProps({ owner: "cara@example.test", ownerLabel: "Cara" });
		api.adminHandover.mockResolvedValue({ ok: true, new_owner: "ben@example.test" });
		await handButton(w).trigger("click");
		await flushPromises();
		expect(api.adminHandover).toHaveBeenLastCalledWith(
			"m1",
			"ben@example.test",
			"cara@example.test"
		);
		expect(w.find(".jv-macro-handover-summary").text()).toContain("from Cara");
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
		// Searched back, the user is offered again but not picked behind the admin's back.
		api.adminHandoverTargets.mockResolvedValue({ users: USERS, max: 25 });
		await w.find(".stub-search").setValue("");
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(w.findAll(".jv-macro-handover-user")).toHaveLength(3);
		expect(w.find(".jv-macro-handover-summary").exists()).toBe(false);
	});

	it("shows that a new search is under way until its answer comes", async () => {
		vi.useFakeTimers();
		const w = await open();
		expect(w.find(".jv-macro-handover-searching").exists()).toBe(false);
		let answer;
		api.adminHandoverTargets.mockReturnValue(new Promise((r) => (answer = r)));
		await w.find(".stub-search").setValue("be");
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(w.find(".jv-macro-handover-searching").text()).toBe("Searching…");
		expect(w.find("fieldset").attributes("aria-busy")).toBe("true");
		answer({ users: [USERS[1]], max: 25 });
		await flushPromises();
		expect(w.find(".jv-macro-handover-searching").exists()).toBe(false);
		expect(w.find("fieldset").attributes("aria-busy")).toBe("false");
	});

	it("says when the list is cut, and not otherwise", async () => {
		api.adminHandoverTargets.mockResolvedValue({ users: USERS, max: 25, more: true });
		let w = await open();
		expect(w.find(".jv-macro-handover-more").text()).toBe(
			"Showing the first 3. Type a name to narrow the search."
		);
		w.unmount();
		api.adminHandoverTargets.mockResolvedValue({ users: USERS, max: 25, more: false });
		w = await open();
		expect(w.find(".jv-macro-handover-more").exists()).toBe(false);
	});

	it("says when nobody matches, and when the users cannot be loaded", async () => {
		api.adminHandoverTargets.mockResolvedValue({ users: [], max: 25 });
		let w = await open();
		expect(w.text()).toContain("Nobody who can use Jarvis macros matches.");
		w.unmount();
		api.adminHandoverTargets.mockRejectedValue(new Error("Not permitted"));
		w = await open();
		expect(w.find(".jv-macro-handover-load-error").text()).toBe("Not permitted");
		expect(handButton(w).attributes("disabled")).toBe("");
	});

	it("says in plain words when the site is not updated for hand-overs yet", async () => {
		const missing = (m) =>
			new Error(
				`Failed to get method for command jarvis.chat.macros_admin_api.${m} with module ` +
					`'jarvis.chat.macros_admin_api' has no attribute '${m}'`
			);
		const plain =
			"This site has not been updated for handing macros over yet. Ask whoever looks after it to update Jarvis.";
		api.adminHandoverTargets.mockRejectedValue(missing("admin_handover_targets"));
		let w = await open();
		expect(w.find(".jv-macro-handover-load-error").text()).toBe(plain);
		w.unmount();
		api.adminHandoverTargets.mockResolvedValue({ users: USERS, max: 25 });
		api.adminHandover.mockRejectedValue(missing("admin_handover"));
		w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toBe(plain);
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
