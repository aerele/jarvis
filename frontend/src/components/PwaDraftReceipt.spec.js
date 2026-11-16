// The phone's draft receipt: the Submit next step (pwa/src/lib/draftReceiptHost.js) and the line
// component (pwa/src/components/DraftReceipt.vue). The phone's own node runner cannot import the
// shared chatCardActions chain, so these run here.
import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { reactive } from "vue";
import DraftReceipt from "../../../pwa/src/components/DraftReceipt.vue";
import { makeReceiptUi } from "../../../pwa/src/lib/receiptUi.js";
import { hiddenSteps, proposeSubmitStep } from "../../../pwa/src/lib/draftReceiptHost.js";
import { receiptLines } from "../../../pwa/src/lib/draftReceiptLines.js";

const STEP = {
	kind: "submit",
	doctype: "Timesheet",
	name: "TS-1",
	key: "Timesheet|TS-1",
	label: "Submit",
};
const deps = (over = {}) => {
	const ui = makeReceiptUi(reactive);
	return {
		ui,
		propose: vi.fn(async () => ({ ok: true })),
		current: () => true,
		reload: vi.fn(async () => {}),
		reloadPending: vi.fn(async () => {}),
		...over,
	};
};

describe("proposeSubmitStep", () => {
	// Break: a refusal is read differently from the desktop's (a permanent refusal keeps a dead
	// button, a temporary one takes the button away), or the card is not reloaded after a success.
	// [name, the server's answer, result, note, step gone, reloaded]
	const ROWS = [
		["parked", { ok: true }, "proposed", "", false, true],
		[
			"another card is waiting (temporary): the button stays",
			{
				ok: false,
				error: {
					code: "ConfirmationPendingError",
					person_message: "Answer the other card first.",
				},
			},
			"refused",
			"Answer the other card first.",
			false,
			false,
		],
		[
			"the step is not available (permanent): the button goes",
			{ ok: false, error: { code: "InvalidArgumentError" } },
			"refused",
			"That next step is not available.",
			true,
			false,
		],
	];
	it.each(ROWS)("%s", async (_, answer, result, note, gone, reloaded) => {
		const d = deps({ propose: vi.fn(async () => answer) });
		expect(await proposeSubmitStep(STEP, d)).toBe(result);
		expect(d.propose).toHaveBeenCalledWith(STEP);
		expect(d.ui.note(STEP.key)).toBe(note);
		expect(d.ui.goneKeys()).toEqual(gone ? [STEP.key] : []);
		// the parked card shows only after the thread and the pending list are both read
		expect([d.reload, d.reloadPending].map((f) => f.mock.calls.length)).toEqual(
			reloaded ? [1, 1] : [0, 0]
		);
		expect(d.ui.busyKey()).toBe("");
	});

	// Break: a dropped connection hides Submit for good (nothing ran, so the person must be able to
	// press it again), or shows no reason.
	it("keeps the button and says so when the call throws", async () => {
		const d = deps({
			propose: vi.fn(async () => {
				throw new Error("Failed to fetch");
			}),
		});
		expect(await proposeSubmitStep(STEP, d)).toBe("failed");
		expect(d.ui.note(STEP.key)).toBe("Failed to fetch");
		expect(d.ui.goneKeys()).toEqual([]);
		expect(d.ui.busyKey()).toBe("");
	});

	// Break: a second tap parks a second card.
	it("ignores a second tap while one is in flight", async () => {
		let release;
		const d = deps({ propose: vi.fn(() => new Promise((r) => (release = r))) });
		const first = proposeSubmitStep(STEP, d);
		expect(d.ui.busyKey()).toBe(STEP.key);
		expect(await proposeSubmitStep(STEP, d)).toBe("ignored");
		release({ ok: true });
		expect(await first).toBe("proposed");
		expect(d.propose).toHaveBeenCalledTimes(1);
	});

	// Break: an answer that lands after the person left the chat reloads or marks the new chat.
	it("touches nothing when the chat was left", async () => {
		let here = true;
		const d = deps({
			current: () => here,
			propose: vi.fn(async () => {
				here = false;
				return { ok: false, error: { code: "InvalidArgumentError" } };
			}),
		});
		expect(await proposeSubmitStep(STEP, d)).toBe("ignored");
		expect(d.ui.goneKeys()).toEqual([]);
		expect(d.reloadPending).not.toHaveBeenCalled();
	});
});

describe("hiddenSteps", () => {
	// Break: Submit stays on a receipt whose record a later receipt in the thread already submitted
	// (the desktop hides it by the same rule), or a failed submit hides it.
	it("hides a step a later successful submit receipt acted on, and any refused for good", () => {
		const submit = (outcome, name) => ({
			role: "tool",
			tool_name: "submit_doc",
			action_outcome: outcome,
			tool_args: JSON.stringify({ doctype: "Timesheet", name }),
			tool_result: null,
		});
		const keys = hiddenSteps([submit("confirmed", "TS-1"), submit("failed", "TS-2")], ["X|y"]);
		expect([...keys].sort()).toEqual(["Timesheet|TS-1", "X|y"]);
	});
});

describe("DraftReceipt", () => {
	const lineOf = (over = {}) => ({
		key: "m1",
		stepKey: "Timesheet|TS-1",
		text: "Created Timesheet TS-1",
		tone: "ok",
		muted: false,
		reason: "",
		open: { href: "/app/timesheet/TS-1", label: "Open" },
		submit: STEP,
		...over,
	});
	// Break: Open does not link to the record (or opens in the PWA's own window), Submit emits the
	// wrong step or fires while busy, or a reason / note is not shown.
	it("draws the text, Open to the record, and Submit that emits its step", async () => {
		const w = mount(DraftReceipt, { props: { line: lineOf(), note: "Try later" } });
		expect(w.text()).toContain("Created Timesheet TS-1");
		const a = w.get("a");
		expect([
			a.text(),
			a.attributes("href"),
			a.attributes("target"),
			a.attributes("rel"),
		]).toEqual(["Open", "/app/timesheet/TS-1", "_blank", "noopener"]);
		await w.get("button").trigger("click");
		expect(w.emitted("submit")).toEqual([[STEP]]);
		expect(w.get('[role="alert"]').text()).toBe("Try later");
	});

	it("a busy Submit is disabled and emits nothing; a line without Submit has no button", async () => {
		const busy = mount(DraftReceipt, { props: { line: lineOf(), busy: true } });
		expect(busy.get("button").attributes("disabled")).toBeDefined();
		await busy.get("button").trigger("click");
		expect(busy.emitted("submit")).toBeUndefined();
		const plain = mount(DraftReceipt, {
			props: {
				line: lineOf({ submit: null, tone: "error", reason: "Employee is required." }),
			},
		});
		expect(plain.find("button").exists()).toBe(false);
		expect(plain.text()).toContain("Employee is required.");
		expect(plain.classes()).toContain("is-error");
	});

	// Break: the line a real receipt produces does not fit the component (a renamed field).
	it("draws what receiptLines produces", () => {
		const [line] = receiptLines([
			{
				name: "m1",
				role: "tool",
				tool_name: "create_doc",
				action_outcome: "discarded",
				draft_receipt: true,
				tool_args: "{}",
			},
		]);
		const w = mount(DraftReceipt, { props: { line } });
		expect(w.text()).toBe("Draft cancelled");
		expect(w.classes()).toContain("is-muted");
		expect(w.find("a").exists()).toBe(false);
	});
});
