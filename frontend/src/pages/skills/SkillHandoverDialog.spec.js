import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * Who an admin hands another user's skill to. What matters: the people offered are
 * the server's search results, found as the admin types; the owner and a person at
 * the cap cannot be picked, and say why; "Hand to myself" is there only when the
 * signed-in user is a valid target; nothing is sent until someone is picked, and
 * then the dialog says what arrives; a role or company skill says only a reviewer
 * can receive it; a refusal (a name clash) stays in the dialog, and the one for a
 * changed owner offers Reload; the owner the dialog showed is sent; names go into
 * the toast escaped; the dialog cannot be closed while the hand over is in flight.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "loading", "disabled", "theme", "size"],
		emits: ["click"],
		template: `<button class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" @click="$emit('click')">{{ label }}</button>`,
	},
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="stub-dialog"><button class="stub-close">x</button><slot name="body-title"><h4>{{ options && options.title }}</h4></slot><slot name="body-content" /><slot name="actions" /></div>`,
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
		template: `<input type="text" class="stub-search" :value="modelValue" :disabled="disabled" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
}));

const api = vi.hoisted(() => ({ adminHandover: vi.fn(), adminHandoverTargets: vi.fn() }));
vi.mock("@/api/skillsAdmin", () => api);

const me = vi.hoisted(() => ({ session: { user: "boss@example.test" } }));
vi.mock("@/data/session", () => me);

import { toast } from "frappe-ui";
import SkillHandoverDialog from "./SkillHandoverDialog.vue";

const ASHA = { user: "asha@example.test", full_name: "Asha Rao", at_cap: false };
const BEN = { user: "ben@example.test", full_name: "Ben Ode", at_cap: false };
const FULL = { user: "full@example.test", full_name: "Full User", at_cap: true };
const BOSS = { user: "boss@example.test", full_name: "Boss Person", at_cap: false };
const TARGETS = [ASHA, BEN, FULL];

async function open(props = {}) {
	const w = mount(SkillHandoverDialog, {
		props: {
			modelValue: true,
			name: "s1",
			skillName: "Month end",
			owner: "asha@example.test",
			ownerLabel: "Asha Rao (asha@example.test)",
			...props,
		},
	});
	await flushPromises();
	return w;
}
const handButton = (w) => w.findAll("button.stub-button").find((b) => b.text() === "Hand over");
const selfButton = (w) => w.find(".jv-skill-handover-self");
const radio = (w, user) => w.find(`.jv-skill-handover-user[data-user="${user}"] input`);
const pick = (w, user) => radio(w, user).setValue(true);
const summary = (w) => w.find(".jv-skill-handover-summary");
const chips = (w) => w.findAll(".jv-skill-handover-chip").map((c) => c.attributes("data-user"));
const CARA = { user: "cara@example.test", full_name: "Cara Lim", at_cap: false };

beforeEach(() => {
	vi.clearAllMocks();
	me.session.user = "boss@example.test";
	api.adminHandoverTargets.mockResolvedValue({ targets: TARGETS, more: false, cap: 25 });
	api.adminHandover.mockResolvedValue({
		ok: true,
		new_owner: "ben@example.test",
		cleared: { shares: 0, armed: 0, file_box: 0, assignments: 0 },
		hold_lifted: false,
		requests_cancelled: 0,
		needs_apply: false,
	});
});
afterEach(() => vi.useRealTimers());

describe("SkillHandoverDialog, the picker", () => {
	it("names the skill and offers the server's people, the owner and a person at the cap not pickable", async () => {
		const w = await open();
		expect(w.find("h3").text()).toBe("Hand over “Month end”");
		// With the skill: the server leaves its owner out.
		expect(api.adminHandoverTargets).toHaveBeenCalledWith("", "s1");
		const rows = w.findAll(".jv-skill-handover-user");
		expect(rows.map((r) => r.attributes("data-user"))).toEqual(TARGETS.map((u) => u.user));
		// aria-disabled, not `disabled`: a keyboard or screen reader user hears why.
		for (const [user, row, why] of [
			["asha@example.test", rows[0], "is already its owner"],
			["full@example.test", rows[2], "has 25 skills already"],
		]) {
			const r = radio(w, user);
			expect(r.attributes("disabled")).toBeUndefined();
			expect(r.attributes("aria-disabled")).toBe("true");
			expect(row.text()).toContain(why);
			expect(w.find(`#${r.attributes("aria-describedby")}`).text()).toBe(why);
		}
		expect(radio(w, "ben@example.test").attributes("aria-disabled")).toBeUndefined();
	});

	it("says the cap the server gives, and no number when it gives none", async () => {
		api.adminHandoverTargets.mockResolvedValue({ targets: TARGETS, more: false, cap: 30 });
		let w = await open();
		expect(radio(w, "full@example.test").attributes("aria-describedby")).toBeTruthy();
		expect(w.find('.jv-skill-handover-user[data-user="full@example.test"]').text()).toContain(
			"has 30 skills already"
		);
		w.unmount();
		api.adminHandoverTargets.mockResolvedValue({ targets: TARGETS, more: false });
		w = await open();
		expect(w.find('.jv-skill-handover-user[data-user="full@example.test"]').text()).toContain(
			"has the most skills one person may"
		);
	});

	it("does not let an unavailable person be chosen, and keeps a pick it has", async () => {
		const w = await open();
		for (const user of ["asha@example.test", "full@example.test"]) {
			await radio(w, user).trigger("click");
			await radio(w, user).setValue(true);
			expect(radio(w, user).element.checked).toBe(false);
			expect(summary(w).exists()).toBe(false);
			expect(handButton(w).attributes("disabled")).toBe("");
		}
		await pick(w, "ben@example.test");
		await radio(w, "full@example.test").setValue(true);
		expect(radio(w, "ben@example.test").element.checked).toBe(true);
		expect(summary(w).text()).toContain("Ben Ode");
	});

	it("searches when the typing stops, and keeps a pick the new results do not show", async () => {
		vi.useFakeTimers();
		const w = await open();
		await pick(w, "ben@example.test");
		api.adminHandoverTargets.mockResolvedValue({ targets: [FULL], more: false });
		await w.find(".jv-skill-handover-search").setValue("ful");
		expect(api.adminHandoverTargets).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(api.adminHandoverTargets).toHaveBeenLastCalledWith("ful", "s1");
		expect(w.findAll(".jv-skill-handover-user")).toHaveLength(1);
		// Several people are gathered from several searches: the pick stays, as a chip.
		expect(chips(w)).toEqual(["ben@example.test"]);
		expect(handButton(w).attributes("disabled")).toBeUndefined();
		expect(summary(w).text()).toContain("Ben Ode");
	});

	it("shows that a new search is under way until its answer comes", async () => {
		vi.useFakeTimers();
		const w = await open();
		expect(w.find(".jv-skill-handover-searching").exists()).toBe(false);
		let answer;
		api.adminHandoverTargets.mockReturnValue(new Promise((r) => (answer = r)));
		await w.find(".jv-skill-handover-search").setValue("be");
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(w.find(".jv-skill-handover-searching").text()).toBe("Searching…");
		expect(w.find("fieldset").attributes("aria-busy")).toBe("true");
		answer({ targets: [BEN], more: false });
		await flushPromises();
		expect(w.find(".jv-skill-handover-searching").exists()).toBe(false);
	});

	it("says when the list is cut, and not otherwise", async () => {
		api.adminHandoverTargets.mockResolvedValue({ targets: TARGETS, more: true });
		let w = await open();
		expect(w.find(".jv-skill-handover-more").text()).toBe(
			"Showing the first 3. Type a name to narrow the search."
		);
		w.unmount();
		api.adminHandoverTargets.mockResolvedValue({ targets: TARGETS, more: false });
		w = await open();
		expect(w.find(".jv-skill-handover-more").exists()).toBe(false);
	});

	it("says when nobody matches, and when the people cannot be loaded", async () => {
		api.adminHandoverTargets.mockResolvedValue({ targets: [], more: false });
		let w = await open();
		expect(w.text()).toContain("Nobody who can use Jarvis matches.");
		w.unmount();
		api.adminHandoverTargets.mockRejectedValue(new Error("Not permitted"));
		w = await open();
		expect(w.find(".jv-skill-handover-load-error").text()).toBe("Not permitted");
		expect(handButton(w).attributes("disabled")).toBe("");
	});

	it("says in plain words when the site is not updated for hand overs yet", async () => {
		const missing = (m) =>
			new Error(
				`Failed to get method for command jarvis.chat.skills_admin_api.${m} with module ` +
					`'jarvis.chat.skills_admin_api' has no attribute '${m}'`
			);
		const plain =
			"This site has not been updated for handing skills over yet. Ask whoever looks after it to update Jarvis.";
		api.adminHandoverTargets.mockRejectedValue(missing("admin_handover_targets"));
		let w = await open();
		expect(w.find(".jv-skill-handover-load-error").text()).toBe(plain);
		w.unmount();
		api.adminHandoverTargets.mockResolvedValue({ targets: TARGETS, more: false });
		api.adminHandover.mockRejectedValue(missing("admin_handover"));
		w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toBe(plain);
	});

	it("starts over each time it opens, with focus on the search box", async () => {
		const w = mount(SkillHandoverDialog, {
			props: {
				modelValue: true,
				name: "s1",
				skillName: "Month end",
				owner: "asha@example.test",
			},
			attachTo: document.body,
		});
		w.find(".stub-close").element.focus();
		await flushPromises();
		await new Promise((r) => requestAnimationFrame(r));
		expect(document.activeElement).toBe(w.find(".jv-skill-handover-search").element);
		await pick(w, "ben@example.test");
		await w.setProps({ modelValue: false });
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(api.adminHandoverTargets).toHaveBeenCalledTimes(2);
		expect(summary(w).exists()).toBe(false);
		w.unmount();
	});
});

describe("SkillHandoverDialog, hand to myself", () => {
	it("is there when the signed-in user is a valid target, and picks them", async () => {
		api.adminHandoverTargets.mockResolvedValue({ targets: [...TARGETS, BOSS], more: false });
		const w = await open();
		expect(selfButton(w).exists()).toBe(true);
		await selfButton(w).trigger("click");
		expect(summary(w).text()).toContain("to Boss Person (boss@example.test)?");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(api.adminHandover).toHaveBeenCalledWith(
			"s1",
			"boss@example.test",
			"asha@example.test",
			false
		);
	});

	it("is not there when the signed-in user is not offered, is at the cap, or owns the skill", async () => {
		let w = await open();
		expect(selfButton(w).exists()).toBe(false);
		w.unmount();
		api.adminHandoverTargets.mockResolvedValue({
			targets: [{ ...BOSS, at_cap: true }],
			more: false,
		});
		w = await open();
		expect(selfButton(w).exists()).toBe(false);
		w.unmount();
		api.adminHandoverTargets.mockResolvedValue({ targets: [BOSS], more: false });
		w = await open({ owner: "boss@example.test" });
		expect(selfButton(w).exists()).toBe(false);
	});

	it("looks the user up by address when a cut list does not hold them", async () => {
		api.adminHandoverTargets.mockImplementation((search) =>
			Promise.resolve(
				search === "boss@example.test"
					? { targets: [BOSS], more: false }
					: { targets: TARGETS, more: true }
			)
		);
		const w = await open();
		expect(api.adminHandoverTargets).toHaveBeenLastCalledWith("boss@example.test", "s1");
		expect(selfButton(w).exists()).toBe(true);
		// Picked, they stay picked though the list on screen does not show them.
		await selfButton(w).trigger("click");
		expect(w.findAll(".jv-skill-handover-user")).toHaveLength(3);
		expect(summary(w).text()).toContain("Boss Person");
		expect(handButton(w).attributes("disabled")).toBeUndefined();
	});

	it("does not look them up when the list is not cut", async () => {
		await open();
		expect(api.adminHandoverTargets).toHaveBeenCalledTimes(1);
	});
});

describe("SkillHandoverDialog, what arrives", () => {
	it("sends nothing until someone is picked", async () => {
		const w = await open();
		expect(handButton(w).attributes("disabled")).toBe("");
		expect(summary(w).exists()).toBe(false);
		await handButton(w).trigger("click");
		expect(api.adminHandover).not.toHaveBeenCalled();
	});

	it("says who to whom once, then what happens as short points that name nobody", async () => {
		const w = await open();
		await pick(w, "ben@example.test");
		const points = summary(w)
			.findAll("li")
			.map((li) => li.text().replace(/\s+/g, " "));
		expect(points).toEqual([
			"It arrives switched off.",
			"Its shares are removed.",
			"It no longer writes without asking.",
			"Its File Box opt-in is switched off.",
			"Its assignments are cleared.",
			"Any hold on it is lifted.",
		]);
		const question = summary(w).find("p").text().replace(/\s+/g, " ");
		expect(question).toBe(
			"Hand “Month end” from Asha Rao (asha@example.test) to Ben Ode (ben@example.test)?"
		);
		const said = w.text();
		for (const word of [
			"Org",
			"scope",
			"armed",
			String.fromCharCode(0x2014),
			String.fromCharCode(0x2013),
		])
			expect(said).not.toContain(word);
		// Long names wrap inside the dialog, and the list of people cannot widen it.
		expect(summary(w).find("p").classes()).toContain("break-words");
		expect(w.find("fieldset").classes()).toContain("min-w-0");
	});

	it("says only a skill reviewer can receive a role or company skill, and nothing for a private one", async () => {
		let w = await open({ shared: true });
		expect(w.find(".jv-skill-handover-reviewer").text()).toContain(
			"only a skill reviewer can receive it"
		);
		w.unmount();
		w = await open();
		expect(w.find(".jv-skill-handover-reviewer").exists()).toBe(false);
	});
});

describe("SkillHandoverDialog, handing over", () => {
	it("hands over with the owner it showed, says so, tells the tab and closes", async () => {
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(api.adminHandover).toHaveBeenCalledWith(
			"s1",
			"ben@example.test",
			"asha@example.test",
			false
		);
		expect(toast.success).toHaveBeenCalledWith(
			"Handed “Month end” to Ben Ode (ben@example.test)."
		);
		expect(w.emitted("handed")).toEqual([
			[{ name: "s1", new_owner: "ben@example.test", needs_apply: false, copies: [] }],
		]);
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});

	it("tells the admin to press Apply when the catalog needs it", async () => {
		api.adminHandover.mockResolvedValue({
			ok: true,
			new_owner: "ben@example.test",
			needs_apply: true,
		});
		const w = await open({ shared: true });
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(toast.success.mock.calls[0][0]).toContain(
			"Press Apply to update the shared assistant."
		);
		expect(w.emitted("handed")[0][0].needs_apply).toBe(true);
	});

	it("puts names in the toast escaped", async () => {
		api.adminHandoverTargets.mockResolvedValue({
			targets: [{ user: "x@example.test", full_name: "<b>X</b>", at_cap: false }],
			more: false,
		});
		api.adminHandover.mockResolvedValue({ ok: true, new_owner: "x@example.test" });
		const w = await open({ skillName: "<img src=x onerror=alert(1)>" });
		await pick(w, "x@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(toast.success.mock.calls[0][0]).toBe(
			"Handed “&lt;img src=x onerror=alert(1)&gt;” to &lt;b&gt;X&lt;/b&gt; (x@example.test)."
		);
	});

	it("shows the hand over in flight, does not send it twice, and cannot be closed meanwhile", async () => {
		let answer;
		api.adminHandover.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		expect(handButton(w).attributes("data-loading")).toBe("1");
		await handButton(w).trigger("click");
		expect(api.adminHandover).toHaveBeenCalledTimes(1);
		// Escape or a click outside: the Dialog asks to close.
		w.findComponent({ name: "Dialog" }).vm.$emit("update:modelValue", false);
		await flushPromises();
		expect(w.emitted("update:modelValue")).toBeUndefined();
		answer({ ok: true, new_owner: "ben@example.test" });
		await flushPromises();
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});
});

describe("SkillHandoverDialog, refusals", () => {
	const CLASH = "ben@example.test already has a skill named Month end.";
	const CHANGED = "This skill changed while you acted on it. Reload.";
	// The server's refusal for a changed owner carries its own exception class.
	const changedError = () =>
		Object.assign(new Error(CHANGED), { exc_type: "SkillOwnerChangedError" });

	it("shows a name clash in place, stays open and asks the tab to re-read", async () => {
		api.adminHandover.mockRejectedValue(new Error(CLASH));
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toBe(CLASH);
		expect(w.find(".jv-skill-handover-reload").exists()).toBe(false);
		// The pick is still there: the admin can choose someone else.
		expect(radio(w, "ben@example.test").element.checked).toBe(true);
		expect(w.emitted("handed")).toBeUndefined();
		expect(w.emitted("update:modelValue")).toBeUndefined();
		expect(w.emitted("failed")).toEqual([[{ name: "s1", message: CLASH, changed: false }]]);
		expect(toast.success).not.toHaveBeenCalled();
	});

	it("offers no Reload for a refusal that only mentions a change", async () => {
		// A clash with a skill named "changed": the words are the skill's, not the refusal's.
		const words = "ben@example.test already has a skill named changed. Reload?";
		api.adminHandover.mockRejectedValue(
			Object.assign(new Error(words), { exc_type: "SkillHandoverError" })
		);
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toBe(words);
		expect(w.find(".jv-skill-handover-reload").exists()).toBe(false);
		expect(w.emitted("failed")).toEqual([[{ name: "s1", message: words, changed: false }]]);
	});

	it("shows the changed owner refusal with Reload, which asks the tab for the row", async () => {
		api.adminHandover.mockRejectedValue(changedError());
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		expect(w.find(".stub-error").text()).toBe(CHANGED);
		expect(w.emitted("failed")).toEqual([[{ name: "s1", message: CHANGED, changed: true }]]);
		await w.find(".jv-skill-handover-reload").trigger("click");
		expect(w.emitted("reload")).toEqual([[{ name: "s1" }]]);
		// The message and the button go once asked.
		expect(w.find(".stub-error").exists()).toBe(false);
		expect(w.find(".jv-skill-handover-reload").exists()).toBe(false);
	});

	it("sends the owner it is given now: the tab refreshes it after a reload", async () => {
		api.adminHandover.mockRejectedValue(changedError());
		const w = await open();
		await pick(w, "ben@example.test");
		await handButton(w).trigger("click");
		await flushPromises();
		await w.setProps({ owner: "cara@example.test", ownerLabel: "Cara" });
		api.adminHandover.mockResolvedValue({ ok: true, new_owner: "ben@example.test" });
		await handButton(w).trigger("click");
		await flushPromises();
		expect(api.adminHandover).toHaveBeenLastCalledWith(
			"s1",
			"ben@example.test",
			"cara@example.test",
			false
		);
		expect(summary(w).text()).toContain("from Cara");
	});
});

describe("SkillHandoverDialog, several people", () => {
	it("picks several as chips, says the first gets it, and sends them in order", async () => {
		api.adminHandoverTargets.mockResolvedValue({ targets: [...TARGETS, CARA], more: false });
		api.adminHandover.mockResolvedValue({
			ok: true,
			handed_over: true,
			new_owner: "cara@example.test",
			needs_apply: false,
			copies: [{ user: "ben@example.test", skill: "s2" }],
			copy_failed: [],
		});
		const w = await open();
		await pick(w, "cara@example.test");
		await pick(w, "ben@example.test");
		expect(chips(w)).toEqual(["cara@example.test", "ben@example.test"]);
		expect(w.find(".jv-skill-handover-search").attributes("placeholder")).toBe(
			"Add someone else"
		);
		expect(summary(w).find("p").text()).toBe(
			"Hand “Month end” from Asha Rao (asha@example.test) to Cara Lim and Ben Ode?"
		);
		expect(summary(w).findAll("li")[0].text()).toBe(
			"The first person picked gets it; each of the others gets their own private copy."
		);
		const button = w.find(".jv-skill-handover-confirm");
		expect(button.text()).toBe("Hand over to 2 people");
		await button.trigger("click");
		await flushPromises();
		expect(api.adminHandover).toHaveBeenCalledWith(
			"s1",
			["cara@example.test", "ben@example.test"],
			"asha@example.test",
			false
		);
		expect(toast.success).toHaveBeenCalledWith(
			"Handed “Month end” to Cara Lim (cara@example.test); copies went to Ben Ode (ben@example.test)."
		);
		expect(w.emitted("handed")[0][0].copies).toEqual([
			{ user: "ben@example.test", skill: "s2" },
		]);
	});

	it("unpicks with a chip's x", async () => {
		const w = await open();
		await pick(w, "ben@example.test");
		const x = w.find('.jv-skill-handover-chip[data-user="ben@example.test"] button');
		expect(x.attributes("aria-label")).toBe("Remove Ben Ode (ben@example.test)");
		await x.trigger("click");
		expect(chips(w)).toEqual([]);
		expect(summary(w).exists()).toBe(false);
	});

	it("with Keep, gives a copy and leaves the skill where it is", async () => {
		api.adminHandover.mockResolvedValue({
			ok: true,
			handed_over: false,
			new_owner: null,
			needs_apply: false,
			copies: [{ user: "ben@example.test", skill: "s2" }],
			copy_failed: [],
		});
		const w = await open();
		await pick(w, "ben@example.test");
		await w.find(".jv-skill-handover-keep input").setValue(true);
		expect(summary(w).find("p").text()).toBe(
			"Give Ben Ode (ben@example.test) a copy of “Month end” from Asha Rao (asha@example.test)?"
		);
		expect(w.find(".jv-skill-handover-confirm").text()).toBe("Give a copy");
		await w.find(".jv-skill-handover-confirm").trigger("click");
		await flushPromises();
		expect(api.adminHandover).toHaveBeenCalledWith(
			"s1",
			"ben@example.test",
			"asha@example.test",
			true
		);
		expect(toast.success).toHaveBeenCalledWith(
			"Gave a copy of “Month end” to Ben Ode (ben@example.test)."
		);
		expect(w.emitted("handed")[0][0].new_owner).toBeNull();
	});

	it("says which copies the server could not make", async () => {
		api.adminHandoverTargets.mockResolvedValue({ targets: [...TARGETS, CARA], more: false });
		api.adminHandover.mockResolvedValue({
			ok: true,
			handed_over: true,
			new_owner: "cara@example.test",
			copies: [],
			copy_failed: [
				{ user: "ben@example.test", reason: "Ben has a skill named month-end." },
			],
		});
		const w = await open();
		await pick(w, "cara@example.test");
		await pick(w, "ben@example.test");
		await w.find(".jv-skill-handover-confirm").trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"Not copied for Ben Ode (ben@example.test): Ben has a skill named month-end."
		);
	});
});
