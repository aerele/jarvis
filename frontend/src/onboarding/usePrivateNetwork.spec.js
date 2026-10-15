import { describe, it, expect, vi } from "vitest";

import { createPrivateNetwork } from "@/onboarding/usePrivateNetwork";
import { GENERIC_REFUSAL, HOLD_UNAVAILABLE } from "@/onboarding/privateNetwork";

const HOLD = {
	ok: true,
	host_ip: "198.51.100.27",
	reach_url: "https://reach.h3.fleet.example/",
	hold_until: "2026-10-17 09:10:00",
};

function makeApi(over = {}) {
	return {
		networkStatus: vi.fn(async () => ({
			host_public: false,
			admin_reachable: true,
			network_mode: "",
			public_address: "",
			test_mode: false,
		})),
		privateSetupTargets: vi.fn(async () => ({
			admin_host: "fleet.klerk.in",
			admin_ips: ["178.104.214.114"],
		})),
		checkAdminConnection: vi.fn(async () => ({
			reachable: true,
			enabled: true,
			caller_ip: "8.8.8.8",
		})),
		savePublicAddress: vi.fn(async (address) => ({ ok: true, public_address: address })),
		reserveHost: vi.fn(async () => ({ ...HOLD })),
		startConnectionTest: vi.fn(async () => ({
			ok: true,
			check: "JCC-000123",
			outbound: { ok: true, reason: "ok" },
		})),
		connectionTestStatus: vi.fn(async () => ({
			ok: true,
			status: "Verified",
			reason: "ok",
			checked_at: "",
		})),
		...over,
	};
}

function make(over = {}, deps = {}) {
	const api = makeApi(over);
	const net = createPrivateNetwork({ api, hostOk: false, sleep: async () => {}, ...deps });
	return { net, api };
}

describe("which onboarding this bench gets", () => {
	it("a public bench is not private and its host is ok", () => {
		const { net } = make({}, { hostOk: true });
		expect(net.privateBench.value).toBe(false);
		expect(net.onboardingHostOk.value).toBe(true);
		expect(net.holdsCheckout.value).toBe(false);
	});

	it("a bench with no public host is private-capable but not yet allowed to onboard", () => {
		const { net } = make();
		expect(net.privateBench.value).toBe(true);
		expect(net.complete.value).toBe(false);
		expect(net.onboardingHostOk.value).toBe(false);
		expect(net.holdsCheckout.value).toBe(true);
	});

	it("a finished private setup is allowed to onboard", () => {
		const { net } = make({}, { bootMode: "private" });
		expect(net.complete.value).toBe(true);
		expect(net.onboardingHostOk.value).toBe(true);
	});

	it("makes no api call on creation", () => {
		const { api } = make();
		for (const fn of Object.values(api)) expect(fn).not.toHaveBeenCalled();
	});
});

describe("the Network step", () => {
	it("loadStatus records reachability and test mode, and no admin address", async () => {
		const { net, api } = make({
			networkStatus: vi.fn(async () => ({
				host_public: false,
				admin_reachable: false,
				network_mode: "",
				public_address: "",
				test_mode: true,
			})),
		});
		await net.loadStatus();
		expect(net.state.statusLoaded).toBe(true);
		expect(net.state.adminReachable).toBe(false);
		expect(net.state.testMode).toBe(true);
		expect(api.privateSetupTargets).not.toHaveBeenCalled();
	});

	it("a status failure still unblocks the screen, with reachability unknown", async () => {
		const { net } = make({
			networkStatus: vi.fn(async () => {
				throw new Error("boom");
			}),
		});
		await net.loadStatus();
		expect(net.state.statusLoaded).toBe(true);
		expect(net.state.adminReachable).toBeNull();
	});

	it("a completed setup reported by the bench fills the address and the draft", async () => {
		const { net } = make({
			networkStatus: vi.fn(async () => ({
				host_public: false,
				admin_reachable: null,
				network_mode: "private",
				public_address: "https://8.8.8.8",
				test_mode: false,
			})),
		});
		await net.loadStatus();
		expect(net.state.mode).toBe("private");
		expect(net.state.publicAddress).toBe("https://8.8.8.8");
		expect(net.state.draft).toBe("https://8.8.8.8");
	});

	it("the admin address is fetched only after the private option is picked and continued", async () => {
		const { net, api } = make();
		await net.loadStatus();
		net.pickOption("public");
		await net.continueFromChoice();
		expect(api.privateSetupTargets).not.toHaveBeenCalled();
		expect(net.state.screen).toBe("choose");
		net.pickOption("private");
		await net.continueFromChoice();
		expect(api.privateSetupTargets).toHaveBeenCalledTimes(1);
		expect(net.state.screen).toBe("allow_admin");
		expect(net.state.targets).toEqual({
			admin_host: "fleet.klerk.in",
			admin_ips: ["178.104.214.114"],
		});
	});

	it("a failed lookup of the admin address says so and can be retried", async () => {
		const privateSetupTargets = vi
			.fn()
			.mockRejectedValueOnce(new Error("x"))
			.mockResolvedValueOnce({ admin_host: "h", admin_ips: [] });
		const { net } = make({ privateSetupTargets });
		await net.continueFromChoice();
		expect(net.state.targetsErr).toMatch(/couldn't look up/);
		await net.loadTargets();
		expect(net.state.targetsErr).toBe("");
		expect(net.state.targets.admin_host).toBe("h");
	});

	it("runAdminCheck: ok remembers the caller IP", async () => {
		const { net } = make();
		await net.runAdminCheck();
		expect(net.state.adminCheck).toBe("ok");
		expect(net.state.detectedIp).toBe("8.8.8.8");
	});

	it("runAdminCheck: unreachable, switched off, and a thrown call", async () => {
		for (const [reply, expected] of [
			[{ reachable: false, enabled: false, caller_ip: "" }, "fail"],
			[{ reachable: true, enabled: false, caller_ip: "" }, "disabled"],
		]) {
			const { net } = make({ checkAdminConnection: vi.fn(async () => reply) });
			await net.runAdminCheck();
			expect(net.state.adminCheck).toBe(expected);
		}
		const { net } = make({
			checkAdminConnection: vi.fn(async () => {
				throw new Error("x");
			}),
		});
		await net.runAdminCheck();
		expect(net.state.adminCheck).toBe("fail");
	});

	it("runAdminCheck ignores a second click while checking", async () => {
		let release;
		const { net, api } = make({
			checkAdminConnection: vi.fn(() => new Promise((r) => (release = r))),
		});
		const first = net.runAdminCheck();
		net.runAdminCheck();
		expect(api.checkAdminConnection).toHaveBeenCalledTimes(1);
		release({ reachable: true, enabled: true, caller_ip: "8.8.8.8" });
		await first;
	});

	it("openAddress pre-fills the detected address once and never clobbers an edit", async () => {
		const { net } = make();
		await net.runAdminCheck();
		net.openAddress();
		expect(net.state.screen).toBe("address");
		expect(net.state.draft).toBe("https://8.8.8.8");
		net.state.draft = "https://erp.example.com";
		net.openAddress();
		expect(net.state.draft).toBe("https://erp.example.com");
		net.useDetected();
		expect(net.state.draft).toBe("https://8.8.8.8");
	});

	it("draftError is silent for an untouched empty field and live afterwards", () => {
		const { net } = make();
		expect(net.draftError.value).toBe("");
		net.state.draftTouched = true;
		expect(net.draftError.value).toBe("Enter the public address of your ERPNext.");
		net.state.draft = "http://erp.example.com";
		expect(net.draftError.value).toBe("Use an https:// address. Plain HTTP is not allowed.");
		net.state.draft = "https://erp.example.com";
		expect(net.draftError.value).toBe("");
	});

	it("test mode relaxes the address mirror", () => {
		const { net } = make();
		net.state.testMode = true;
		net.state.draft = "http://127.0.0.1:8000";
		expect(net.draftError.value).toBe("");
	});

	it("saveAddress saves the typed address and flips the bench to private", async () => {
		const { net, api } = make();
		net.state.draft = "https://erp.example.com";
		expect(await net.saveAddress()).toBe(true);
		expect(api.savePublicAddress).toHaveBeenCalledWith("https://erp.example.com");
		expect(net.state.mode).toBe("private");
		expect(net.state.publicAddress).toBe("https://erp.example.com");
		expect(net.complete.value).toBe(true);
	});

	it("saveAddress does not call the bench for an address the mirror refuses", async () => {
		const { net, api } = make();
		net.state.draft = "http://erp.example.com";
		expect(await net.saveAddress()).toBe(false);
		expect(api.savePublicAddress).not.toHaveBeenCalled();
		expect(net.state.draftTouched).toBe(true);
	});

	it("saveAddress shows the bench's own refusal", async () => {
		const { net } = make({
			savePublicAddress: vi.fn(async () => ({
				ok: false,
				code: "PRIVATE_ADDRESS_INVALID",
				message: "Nope.",
			})),
		});
		net.state.draft = "https://erp.example.com";
		expect(await net.saveAddress()).toBe(false);
		expect(net.state.saveErr).toBe("Nope.");
		expect(net.state.mode).toBe("");
	});
});

describe("a locked address", () => {
	it("saveAddress keeps the bench sentence and its code, and a later success clears the code", async () => {
		const sentence = "The address can't be changed after sign-up. Contact support.";
		const savePublicAddress = vi
			.fn()
			.mockResolvedValueOnce({ ok: false, code: "PRIVATE_ADDRESS_LOCKED", message: sentence })
			.mockResolvedValueOnce({ ok: true, public_address: "https://erp.example.com" });
		const { net } = make({ savePublicAddress });
		net.state.draft = "https://erp.example.com";
		expect(await net.saveAddress()).toBe(false);
		expect(net.state.saveErr).toBe(sentence);
		expect(net.state.saveErrCode).toBe("PRIVATE_ADDRESS_LOCKED");
		expect(await net.saveAddress()).toBe(true);
		expect(net.state.saveErrCode).toBe("");
	});
});

describe("the held server", () => {
	it("loadHold records the hold", async () => {
		const { net } = make();
		expect(await net.loadHold()).toBe(true);
		expect(net.state.hold).toEqual({
			hostIp: "198.51.100.27",
			reachUrl: "https://reach.h3.fleet.example/",
			holdUntil: "2026-10-17 09:10:00",
		});
	});

	it("a refusal is kept with its code", async () => {
		const { net } = make({
			reserveHost: vi.fn(async () => ({
				ok: false,
				code: "PRIVATE_HOLD_UNAVAILABLE",
				message: "No server fits.",
			})),
		});
		expect(await net.loadHold()).toBe(false);
		expect(net.state.holdErr).toBe("No server fits.");
		expect(net.state.holdErrCode).toBe("PRIVATE_HOLD_UNAVAILABLE");
	});

	it("a different host after a refresh raises the hold-ended banner and clears the old results", async () => {
		const reserveHost = vi
			.fn()
			.mockResolvedValueOnce({ ...HOLD })
			.mockResolvedValueOnce({ ...HOLD, host_ip: "198.51.100.45" });
		const { net } = make({ reserveHost });
		await net.loadHold();
		await net.runTest();
		expect(net.state.test.phase).toBe("done");
		await net.loadHold();
		expect(net.state.holdEnded).toEqual({ oldIp: "198.51.100.27", newIp: "198.51.100.45" });
		expect(net.state.test.phase).toBe("idle");
		expect(net.testsPassed.value).toBe(false);
	});

	it("the same host after a refresh is not a hold-ended event", async () => {
		const { net } = make();
		await net.loadHold();
		await net.loadHold();
		expect(net.state.holdEnded).toBeNull();
	});
});

describe("the two-way test", () => {
	it("passes when the outbound result and the polled inbound result both pass", async () => {
		const { net, api } = make();
		await net.loadHold();
		await net.runTest();
		expect(api.startConnectionTest).toHaveBeenCalledTimes(1);
		expect(api.connectionTestStatus).toHaveBeenCalledWith("JCC-000123");
		expect(net.state.test.phase).toBe("done");
		expect(net.testsPassed.value).toBe(true);
	});

	it("polls through Checking until a result arrives", async () => {
		const connectionTestStatus = vi
			.fn()
			.mockResolvedValueOnce({ ok: true, status: "Checking", reason: "" })
			.mockResolvedValueOnce({ ok: true, status: "Checking", reason: "" })
			.mockResolvedValueOnce({ ok: true, status: "Unreachable", reason: "timeout" });
		const { net } = make({ connectionTestStatus });
		await net.loadHold();
		await net.runTest();
		expect(connectionTestStatus).toHaveBeenCalledTimes(3);
		expect(net.state.test.inbound).toEqual({ status: "Unreachable", reason: "timeout" });
		expect(net.testsPassed.value).toBe(false);
	});

	it("a poll that throws is retried, not fatal", async () => {
		const connectionTestStatus = vi
			.fn()
			.mockRejectedValueOnce(new Error("blip"))
			.mockResolvedValueOnce({ ok: true, status: "Verified", reason: "ok" });
		const { net } = make({ connectionTestStatus });
		await net.loadHold();
		await net.runTest();
		expect(net.testsPassed.value).toBe(true);
	});

	it("a lost job ends as Unreachable/timeout once the poll budget is spent", async () => {
		const connectionTestStatus = vi.fn(async () => ({
			ok: true,
			status: "Checking",
			reason: "",
		}));
		const { net } = make({ connectionTestStatus });
		await net.loadHold();
		await net.runTest();
		expect(connectionTestStatus.mock.calls.length).toBe(45);
		expect(net.state.test.inbound).toEqual({ status: "Unreachable", reason: "timeout" });
		expect(net.state.test.phase).toBe("done");
	});

	it("a failed outbound half fails the test even when the inbound half verifies", async () => {
		const { net } = make({
			startConnectionTest: vi.fn(async () => ({
				ok: true,
				check: "JCC-000123",
				outbound: { ok: false, reason: "timeout" },
			})),
		});
		await net.loadHold();
		await net.runTest();
		expect(net.testsPassed.value).toBe(false);
	});

	it("a second Test click while testing starts nothing", async () => {
		let release;
		const { net, api } = make({
			startConnectionTest: vi.fn(() => new Promise((r) => (release = r))),
		});
		const first = net.runTest();
		await net.runTest();
		expect(api.startConnectionTest).toHaveBeenCalledTimes(1);
		release({ ok: true, check: "JCC-000123", outbound: { ok: true, reason: "ok" } });
		await first;
	});

	it("a start refusal such as BUSY or a rate limit shows its message and stays idle", async () => {
		const { net, api } = make({
			startConnectionTest: vi.fn(async () => ({
				ok: false,
				code: "CONNECTION_CHECK_BUSY",
				message: "A test is already running.",
			})),
		});
		await net.runTest();
		expect(net.state.test.phase).toBe("idle");
		expect(net.state.test.err).toBe("A test is already running.");
		expect(api.connectionTestStatus).not.toHaveBeenCalled();
	});

	it("PRIVATE_HOLD_UNAVAILABLE re-reserves, and a changed host raises the hold-ended banner", async () => {
		const reserveHost = vi
			.fn()
			.mockResolvedValueOnce({ ...HOLD })
			.mockResolvedValueOnce({ ...HOLD, host_ip: "198.51.100.45" });
		const { net } = make({
			reserveHost,
			startConnectionTest: vi.fn(async () => ({
				ok: false,
				code: "PRIVATE_HOLD_UNAVAILABLE",
				message: "x",
			})),
		});
		await net.loadHold();
		await net.runTest();
		expect(reserveHost).toHaveBeenCalledTimes(2);
		expect(net.state.holdEnded).toEqual({ oldIp: "198.51.100.27", newIp: "198.51.100.45" });
		expect(net.state.hold.hostIp).toBe("198.51.100.45");
		expect(net.state.test.phase).toBe("idle");
	});

	it("PRIVATE_HOLD_UNAVAILABLE with the same host says the hold was set aside again", async () => {
		const { net } = make({
			startConnectionTest: vi.fn(async () => ({
				ok: false,
				code: "PRIVATE_HOLD_UNAVAILABLE",
				message: "x",
			})),
		});
		await net.loadHold();
		await net.runTest();
		expect(net.state.holdEnded).toBeNull();
		expect(net.state.test.err).toMatch(/aside again/);
	});

	it("starting a new test clears the hold-ended banner", async () => {
		const { net } = make();
		await net.loadHold();
		net.state.holdEnded = { oldIp: "a", newIp: "b" };
		await net.runTest();
		expect(net.state.holdEnded).toBeNull();
	});

	it("a poll refusal ends the test with its message instead of spinning", async () => {
		const { net } = make({
			connectionTestStatus: vi.fn(async () => ({
				ok: false,
				code: "NOT_PRIVATE_CUSTOMER",
				message: "Not yours.",
			})),
		});
		await net.loadHold();
		await net.runTest();
		expect(net.state.test.phase).toBe("idle");
		expect(net.state.test.err).toBe("Not yours.");
	});

	it("markCleared releases the checkout hold", async () => {
		const { net } = make();
		net.state.publicAddress = "https://8.8.8.8";
		await net.loadHold();
		expect(net.holdsCheckout.value).toBe(true);
		net.markCleared();
		expect(net.holdsCheckout.value).toBe(false);
	});

	it("a cleared checkout survives a reload, but only once the hold and address load and match", async () => {
		const store = new Map();
		const storage = { get: (k) => store.get(k) ?? null, set: (k, v) => store.set(k, v) };
		const first = make({}, { storage });
		first.net.state.publicAddress = "https://8.8.8.8";
		await first.net.loadHold();
		expect(first.net.holdsCheckout.value).toBe(true);
		first.net.markCleared();
		const reloaded = make({}, { storage });
		// Fails closed until both sides have loaded.
		expect(reloaded.net.holdsCheckout.value).toBe(true);
		expect(reloaded.net.clearedPending.value).toBe(true);
		reloaded.net.state.publicAddress = "https://8.8.8.8";
		await reloaded.net.loadHold();
		reloaded.net.state.statusLoaded = true;
		expect(reloaded.net.clearedPending.value).toBe(false);
		expect(reloaded.net.state.cleared).toBe(true);
		expect(reloaded.net.holdsCheckout.value).toBe(false);
	});

	it("after a reload a moved hold raises hold-ended from the stored IP and clears the flag", async () => {
		const storage = {
			get: () => "203.0.113.9|https://8.8.8.8",
			set: vi.fn(),
		};
		const { net } = make({}, { storage });
		net.state.publicAddress = "https://8.8.8.8";
		await net.loadHold();
		expect(net.state.holdEnded).toEqual({ oldIp: "203.0.113.9", newIp: HOLD.host_ip });
		expect(net.state.cleared).toBe(false);
		expect(net.holdsCheckout.value).toBe(true);
	});

	it("a failed hold load is held, not pending", async () => {
		const storage = { get: () => `${HOLD.host_ip}|https://8.8.8.8`, set: () => {} };
		const { net } = make(
			{ reserveHost: vi.fn(async () => ({ ok: false, code: "X", message: "no" })) },
			{ storage }
		);
		net.state.publicAddress = "https://8.8.8.8";
		await net.loadHold();
		net.state.statusLoaded = true;
		expect(net.clearedPending.value).toBe(false);
		expect(net.holdsCheckout.value).toBe(true);
	});

	it("storage that throws neither breaks creation nor markCleared", async () => {
		const storage = {
			get: () => {
				throw new Error("blocked");
			},
			set: () => {
				throw new Error("blocked");
			},
		};
		const { net } = make({}, { storage });
		net.state.publicAddress = "https://8.8.8.8";
		await net.loadHold();
		expect(net.state.cleared).toBe(false);
		net.markCleared();
		expect(net.holdsCheckout.value).toBe(false);
	});

	it("resetTest wipes the results so a second stop starts from a clean panel", async () => {
		const { net } = make();
		await net.loadHold();
		await net.runTest();
		expect(net.testsPassed.value).toBe(true);
		net.resetTest();
		expect(net.state.test.phase).toBe("idle");
		expect(net.testsPassed.value).toBe(false);
	});
});

describe("controller rulings", () => {
	it("a public bench makes no private-network call from any action", async () => {
		const { net, api } = make({}, { hostOk: true });
		await net.loadStatus();
		await net.continueFromChoice();
		await net.loadTargets();
		await net.runAdminCheck();
		net.state.draft = "https://erp.example.com";
		expect(await net.saveAddress()).toBe(false);
		expect(await net.loadHold()).toBe(false);
		await net.runTest();
		for (const fn of Object.values(api)) expect(fn).not.toHaveBeenCalled();
		expect(net.state.statusLoaded).toBe(true);
	});

	it("a cached hold is a normal hold", async () => {
		const { net } = make({ reserveHost: vi.fn(async () => ({ ...HOLD, cached: true })) });
		expect(await net.loadHold()).toBe(true);
		expect(net.state.hold.hostIp).toBe("198.51.100.27");
	});

	it("two overlapping refreshes share one call", async () => {
		const { net, api } = make();
		await Promise.all([net.loadHold(), net.loadHold()]);
		expect(api.reserveHost).toHaveBeenCalledTimes(1);
	});

	it("an unknown refusal code reads as the generic copy", async () => {
		const { net } = make({
			reserveHost: vi.fn(async () => ({
				ok: false,
				code: "SOMETHING_NEW",
				message: "internal",
			})),
			startConnectionTest: vi.fn(async () => ({
				ok: false,
				code: "SOMETHING_NEW",
				message: "internal",
			})),
		});
		await net.loadHold();
		expect(net.state.holdErr).toBe(GENERIC_REFUSAL);
		await net.runTest();
		expect(net.state.test.err).toBe(GENERIC_REFUSAL);
	});

	it("a rate limit carries its retry-after seconds", async () => {
		const { net } = make({
			startConnectionTest: vi.fn(async () => ({
				ok: false,
				code: "RATE_LIMITED_PRIVATE_NETWORK",
				message: "Slow down.",
				retry_after_seconds: 30,
			})),
		});
		await net.runTest();
		expect(net.state.test.err).toBe("Slow down.");
		expect(net.state.test.retryAfter).toBe(30);
	});

	it("a busy admin check surfaces admin's sentence and can be retried", async () => {
		const checkAdminConnection = vi
			.fn()
			.mockResolvedValueOnce({
				reachable: true,
				enabled: false,
				caller_ip: "",
				message: "Another check is running.",
			})
			.mockResolvedValueOnce({ reachable: true, enabled: true, caller_ip: "8.8.8.8" });
		const { net } = make({ checkAdminConnection });
		await net.runAdminCheck();
		expect(net.state.adminCheck).toBe("disabled");
		expect(net.state.adminCheckMessage).toBe("Another check is running.");
		await net.runAdminCheck();
		expect(net.state.adminCheck).toBe("ok");
		expect(net.state.adminCheckMessage).toBe("");
	});

	it("a re-reserve that is itself refused shows that refusal on the test", async () => {
		const reserveHost = vi
			.fn()
			.mockResolvedValueOnce({ ...HOLD })
			.mockResolvedValueOnce({
				ok: false,
				code: "PRIVATE_HOLD_UNAVAILABLE",
				message: "No server fits.",
			});
		const { net } = make({
			reserveHost,
			startConnectionTest: vi.fn(async () => ({
				ok: false,
				code: "PRIVATE_HOLD_UNAVAILABLE",
				message: "x",
			})),
		});
		await net.loadHold();
		await net.runTest();
		expect(reserveHost).toHaveBeenCalledTimes(2);
		expect(net.state.test.err).toBe("No server fits.");
	});

	it("the status and admin check are never called on a timer", async () => {
		const { net, api } = make();
		await net.loadStatus();
		await net.runAdminCheck();
		await net.loadHold();
		await net.runTest();
		expect(api.networkStatus).toHaveBeenCalledTimes(1);
		expect(api.checkAdminConnection).toHaveBeenCalledTimes(1);
	});
});

describe("fix round 1", () => {
	it("reset then rerun mid-poll: only the new test's results land", async () => {
		const gates = [];
		const checks = ["JCC-A", "JCC-B"];
		const startConnectionTest = vi.fn(async () => ({
			ok: true,
			check: checks.shift(),
			outbound: { ok: true, reason: "ok" },
		}));
		const connectionTestStatus = vi.fn((check) => new Promise((r) => gates.push([check, r])));
		const { net } = make({ startConnectionTest, connectionTestStatus });
		const a = net.runTest();
		await vi.waitFor(() => expect(gates.length).toBe(1));
		net.resetTest();
		const b = net.runTest();
		await vi.waitFor(() => expect(gates.length).toBe(2));
		gates[0][1]({ ok: true, status: "Verified", reason: "ok" }); // A answers late
		await a;
		expect(net.state.test.phase).toBe("testing");
		expect(net.testsPassed.value).toBe(false);
		gates[1][1]({ ok: true, status: "Unreachable", reason: "timeout" });
		await b;
		expect(net.state.test.check).toBe("JCC-B");
		expect(net.state.test.inbound).toEqual({ status: "Unreachable", reason: "timeout" });
	});

	it("dispose ends the poll: no further status calls", async () => {
		let n = 0;
		const connectionTestStatus = vi.fn(async () => ({
			ok: true,
			status: "Checking",
			reason: "",
		}));
		const { net } = make(
			{ connectionTestStatus },
			{
				sleep: async () => {
					if (++n === 2) net.dispose();
				},
			}
		);
		await net.runTest();
		expect(connectionTestStatus).toHaveBeenCalledTimes(2);
		expect(net.state.test.phase).toBe("idle");
	});

	it("a blank ok reserve keeps the previous hold, raises no hold-ended, and says contact support", async () => {
		const reserveHost = vi
			.fn()
			.mockResolvedValueOnce({ ...HOLD })
			.mockResolvedValueOnce({ ...HOLD, host_ip: "" });
		const { net } = make({ reserveHost });
		await net.loadHold();
		expect(await net.loadHold()).toBe(false);
		expect(net.state.holdEnded).toBeNull();
		expect(net.state.hold.hostIp).toBe("198.51.100.27");
		expect(net.state.holdErr).toBe(HOLD_UNAVAILABLE.title);
	});

	it("a test started during the re-reserve keeps its own state (no stale err from the first run)", async () => {
		let release;
		const reserveHost = vi
			.fn()
			.mockResolvedValueOnce({ ...HOLD })
			.mockImplementationOnce(() => new Promise((r) => (release = r)));
		const startConnectionTest = vi
			.fn()
			.mockResolvedValueOnce({ ok: false, code: "PRIVATE_HOLD_UNAVAILABLE", message: "x" })
			.mockResolvedValueOnce({
				ok: true,
				check: "JCC-B",
				outbound: { ok: true, reason: "ok" },
			});
		const { net } = make({ reserveHost, startConnectionTest });
		await net.loadHold();
		const a = net.runTest();
		await vi.waitFor(() => expect(release).toBeTypeOf("function"));
		await net.runTest(); // B, while A's re-reserve is in flight
		release({ ...HOLD });
		await a;
		expect(net.state.test.err).toBe("");
		expect(net.state.test.check).toBe("JCC-B");
		expect(net.testsPassed.value).toBe(true);
	});

	it("every poll throwing ends with a connection-lost error, not Unreachable", async () => {
		const { net } = make({
			connectionTestStatus: vi.fn(async () => {
				throw new Error("offline");
			}),
		});
		await net.runTest();
		expect(net.state.test.phase).toBe("idle");
		expect(net.state.test.err).toMatch(/lost the connection/);
		expect(net.state.test.inbound).toBeNull();
	});

	it("a rate-limited poll waits retry_after_seconds and keeps polling the same check", async () => {
		const connectionTestStatus = vi
			.fn()
			.mockResolvedValueOnce({
				ok: false,
				code: "RATE_LIMITED_PRIVATE_NETWORK",
				message: "Slow.",
				retry_after_seconds: 5,
			})
			.mockResolvedValueOnce({ ok: true, status: "Verified", reason: "ok" });
		const sleep = vi.fn(async () => {});
		const { net } = make({ connectionTestStatus }, { sleep });
		await net.runTest();
		expect(sleep).toHaveBeenCalledWith(5000);
		expect(connectionTestStatus).toHaveBeenNthCalledWith(2, "JCC-000123");
		expect(net.testsPassed.value).toBe(true);
	});
});

describe("the cleared checkout is bound to the server and address it was tested against", () => {
	const memory = () => {
		const store = new Map();
		return { get: (k) => store.get(k) ?? null, set: (k, v) => store.set(k, v) };
	};
	async function clearedNet(over = {}, storage = memory()) {
		const { net, api } = make(over, { storage });
		net.state.publicAddress = "https://8.8.8.8";
		await net.loadHold();
		net.markCleared();
		return { net, api, storage };
	}

	it("the same address after a declined card is not held again", async () => {
		const { net } = await clearedNet();
		expect(net.holdsCheckout.value).toBe(false);
		net.state.draft = "https://8.8.8.8";
		await net.saveAddress();
		expect(net.holdsCheckout.value).toBe(false);
	});

	it("a different saved address holds checkout again, in storage too", async () => {
		const { net, storage } = await clearedNet();
		net.state.draft = "https://9.9.9.9";
		expect(await net.saveAddress()).toBe(true);
		expect(net.holdsCheckout.value).toBe(true);
		expect(net.state.cleared).toBe(false);
		const reloaded = make({}, { storage });
		expect(reloaded.net.state.cleared).toBe(false);
	});

	it("a stored flag for another server or address does not clear a reloaded tab", async () => {
		const storage = memory();
		storage.set("jvpriv:cleared", "203.0.113.9|https://8.8.8.8");
		const { net } = make({}, { storage });
		net.state.publicAddress = "https://8.8.8.8";
		await net.loadHold();
		expect(net.holdsCheckout.value).toBe(true);
	});

	it("a bare legacy flag clears nothing", () => {
		const storage = memory();
		storage.set("jvpriv:cleared", "1");
		const { net } = make({}, { storage });
		expect(net.holdsCheckout.value).toBe(true);
	});

	it("a hold that ended clears it", async () => {
		const { net, api } = await clearedNet();
		api.reserveHost.mockResolvedValueOnce({
			ok: true,
			host_ip: "198.51.100.99",
			reach_url: "",
			hold_until: null,
		});
		await net.loadHold();
		expect(net.state.holdEnded).toBeTruthy();
		expect(net.state.cleared).toBe(false);
		expect(net.holdsCheckout.value).toBe(true);
	});

	it("clearCleared (Start again) holds checkout", async () => {
		const { net } = await clearedNet();
		net.clearCleared();
		expect(net.holdsCheckout.value).toBe(true);
	});

	it("dispose ends a test in flight and leaves the panel idle, not stuck on Testing", async () => {
		const { net } = make({ connectionTestStatus: vi.fn(() => new Promise(() => {})) });
		await net.loadHold();
		net.runTest();
		await Promise.resolve();
		await Promise.resolve();
		net.dispose();
		expect(net.state.test.phase).not.toBe("testing");
	});
});

describe("final review hardening", () => {
	const writes = () => {
		const set = vi.fn();
		return { storage: { get: () => null, set }, set };
	};

	it("a public bench never writes the cleared flag, not even to clear it", () => {
		const { storage, set } = writes();
		const { net } = make({}, { hostOk: true, storage });
		net.clearCleared();
		expect(set).not.toHaveBeenCalled();
	});

	it("a private bench still clears the stored flag", () => {
		const { storage, set } = writes();
		const { net } = make({}, { storage });
		net.clearCleared();
		expect(set).toHaveBeenCalledWith("jvpriv:cleared", "");
	});

	it("saving a different address wipes the test results; the same address keeps them", async () => {
		const { net } = make();
		await net.loadHold();
		net.state.publicAddress = "https://8.8.8.8";
		await net.runTest();
		expect(net.state.test.phase).toBe("done");
		net.state.draft = "https://8.8.8.8";
		await net.saveAddress();
		expect(net.state.test.phase).toBe("done");
		net.state.draft = "https://9.9.9.9";
		await net.saveAddress();
		expect(net.state.test.phase).toBe("idle");
		expect(net.testsPassed.value).toBe(false);
	});

	it("overlapping loadStatus calls share one bench call", async () => {
		const { net, api } = make();
		await Promise.all([net.loadStatus(), net.loadStatus()]);
		expect(api.networkStatus).toHaveBeenCalledTimes(1);
		await net.loadStatus();
		expect(api.networkStatus).toHaveBeenCalledTimes(2);
	});

	it("seedHold is gone: the hold only ever comes from the bench", () => {
		const { net } = make();
		expect(net.seedHold).toBeUndefined();
	});
});
