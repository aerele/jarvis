import { describe, expect, it } from "vitest";
import { accountDetail, accountLabel, matchingAccounts } from "./accountSearch";

const HDFC = {
	name: "HDFC Current",
	account_name: "HDFC Current Account",
	bank: "HDFC Bank",
	reference: "•••• 3281",
	account_currency: "INR",
};
const CASH = {
	name: "Petty Cash",
	account_name: "Petty Cash",
	bank: null,
	reference: "Main office",
	account_currency: "INR",
};
const PLAIN = { name: "Bank A", account_name: "", account_currency: "INR" };

describe("accountLabel", () => {
	it("prefers the account name and falls back to the record name", () => {
		expect(accountLabel(HDFC)).toBe("HDFC Current Account");
		expect(accountLabel(PLAIN)).toBe("Bank A");
	});
});

describe("accountDetail", () => {
	it("joins the bank and the masked reference, skipping what is missing", () => {
		expect(accountDetail(HDFC)).toBe("HDFC Bank · •••• 3281");
		expect(accountDetail(CASH)).toBe("Main office");
	});
	it("falls back to the record name and currency when the catalog has no bank data", () => {
		expect(accountDetail(PLAIN)).toBe("INR");
		expect(
			accountDetail({ name: "ACC-1", account_name: "Ops", account_currency: "INR" })
		).toBe("ACC-1 · INR");
	});
});

describe("matchingAccounts", () => {
	const catalog = [HDFC, CASH, PLAIN];
	it("returns everything for an empty query", () => {
		expect(matchingAccounts(catalog, "  ")).toEqual(catalog);
	});
	it("matches name, bank and the digits of the reference, ignoring case", () => {
		expect(matchingAccounts(catalog, "petty")).toEqual([CASH]);
		expect(matchingAccounts(catalog, "hdfc bank")).toEqual([HDFC]);
		expect(matchingAccounts(catalog, "3281")).toEqual([HDFC]);
		expect(matchingAccounts(catalog, "MAIN")).toEqual([CASH]);
		expect(matchingAccounts(catalog, "nothing")).toEqual([]);
	});
	it("copes with null bank and reference", () => {
		expect(() => matchingAccounts([PLAIN, CASH], "x")).not.toThrow();
	});
});
