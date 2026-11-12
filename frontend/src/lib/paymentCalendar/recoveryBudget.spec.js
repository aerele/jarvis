import { describe, expect, it } from "vitest";
import { RecoveryBudget } from "./recoveryBudget";

describe("RecoveryBudget", () => {
	const MINUTE = 60000;
	it("grants two recoveries in a window and refuses the third", () => {
		const budget = new RecoveryBudget();
		expect([budget.take(0), budget.take(1000), budget.take(2000)]).toEqual([
			true,
			true,
			false,
		]);
	});
	it("does not spend budget on a refused request", () => {
		const budget = new RecoveryBudget();
		budget.take(0);
		budget.take(1000);
		budget.take(2000);
		expect(budget.take(5 * MINUTE)).toBe(true);
	});
	it("refills each recovery five minutes after it was taken", () => {
		const budget = new RecoveryBudget();
		budget.take(0);
		budget.take(2 * MINUTE);
		expect(budget.take(5 * MINUTE - 1)).toBe(false);
		expect(budget.take(5 * MINUTE)).toBe(true);
		expect(budget.take(5 * MINUTE + 1)).toBe(false);
		expect(budget.take(7 * MINUTE)).toBe(true);
	});
	it("refills fully on reset", () => {
		const budget = new RecoveryBudget();
		budget.take(0);
		budget.take(1);
		budget.reset();
		expect([budget.take(2), budget.take(3), budget.take(4)]).toEqual([true, true, false]);
	});
});
