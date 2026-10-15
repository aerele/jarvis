// The private-network onboarding's state and actions (design: private-network-onboarding).
//
// A factory in the style of usePaymentFlow: every side effect (the bench calls, sleeping) is
// injected, so the Network step, the held-server step and the Connect panel can be tested
// without mounting the 6000-line OnboardingView. The view owns WHEN things run; this owns
// WHAT they do.
//
// Rules this file holds:
// - Nothing is called at creation. A public bench never reaches the bench's private-network
//   endpoints at all.
// - The Jarvis setup server's address is fetched only by continueFromChoice, after the
//   customer picked "Set up on a private network".
// - The outbound target is never sent from here: startConnectionTest takes no argument.
// - Every action that reaches the bench is a no-op on a public bench (privateBench false).
// - network_status and check_admin_connection are never polled on a timer: each failed probe
//   writes a server Error Log row. They run on a customer action or step entry only. The one
//   loop is the test-result poll, which reads a stored result.
// - The test-result poll carries a run token: resetTest() and dispose() end any poll in flight,
//   so a stale loop can never write into a newer test or release checkout.
// - Checkout stays held (holdsCheckout) until markCleared, which the view calls only after
//   both halves of the test passed and the customer pressed "Continue to payment".
//
// For the views (extras beyond the state list in the plan):
// - state.adminCheckMessage: admin's own sentence from the last runAdminCheck (the busy or
//   switched-off wording), "" otherwise. Show it instead of default copy when set.
// - state.test.retryAfter: seconds from a RATE_LIMITED_PRIVATE_NETWORK refusal (0 when none),
//   alongside state.test.err.
// - dispose(): call when the view unmounts; it ends any running test poll (no further
//   connectionTestStatus calls). resetTest() does the same for the poll it replaces.

import { reactive, computed } from "vue";

import { adminCheckState, refusalOf, testPassed, validateAddress } from "./privateNetwork.js";

// Admin turns a lost job into Unreachable/timeout at 60 s; 45 polls at 2 s is the backstop.
const POLL_MS = 2000;
const POLL_MAX = 45;
const POLL_BUDGET_MS = POLL_MS * POLL_MAX;
const RATE_LIMIT_WAIT_CAP_S = 30;
const CONNECTION_LOST = "We lost the connection while testing. Check your internet and try again.";

const freshTest = () => ({
	phase: "idle",
	inbound: null,
	outbound: null,
	check: "",
	err: "",
	retryAfter: 0,
});

// Where "the customer passed the test and pressed Continue to payment" is remembered. The pay
// page is a full navigation away and back, and a reload would otherwise put the hold back
// over a customer who already cleared it (and over the retry of a declined card).
const CLEARED_KEY = "jvpriv:cleared";

export function createPrivateNetwork(deps) {
	const {
		api,
		hostOk = true,
		bootMode = "",
		sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
		storage = { get: () => null, set: () => {} },
	} = deps;

	function readCleared() {
		try {
			return storage.get(CLEARED_KEY) === "1";
		} catch (e) {
			return false;
		}
	}

	let holdInFlight = null;
	let run = 0; // test generation: bumped by resetTest() and dispose()

	const state = reactive({
		hostOk,
		mode: bootMode === "private" ? "private" : "",
		publicAddress: "",
		testMode: false,
		// Network step
		screen: "choose", // choose | allow_admin | address
		pick: "private", // private | public
		statusLoaded: false,
		adminReachable: null, // true | false | null (not asked or unknown)
		targets: null, // { admin_host, admin_ips }
		targetsLoading: false,
		targetsErr: "",
		adminCheck: "idle", // idle | checking | ok | fail | disabled
		adminCheckMessage: "", // admin's own sentence (busy, switched off)
		detectedIp: "",
		draft: "",
		draftTouched: false,
		saving: false,
		saveErr: "",
		// Held server (after the email is confirmed) and the Connect panel
		hold: null, // { hostIp, reachUrl, holdUntil }
		holdLoading: false,
		holdErr: "",
		holdErrCode: "",
		holdEnded: null, // { oldIp, newIp }
		test: freshTest(),
		cleared: readCleared(),
	});

	// A bench is "private" when it has no public host (it may take the private route) or has
	// finished the private setup. A public bench is neither.
	const privateBench = computed(() => !state.hostOk || state.mode === "private");
	const complete = computed(() => state.mode === "private");
	const onboardingHostOk = computed(() => state.hostOk || state.mode === "private");
	const holdsCheckout = computed(() => privateBench.value && !state.cleared);
	const draftError = computed(() => {
		if (!state.draft.trim() && !state.draftTouched) return "";
		return validateAddress(state.draft, { testMode: state.testMode });
	});
	const testsPassed = computed(
		() =>
			state.test.phase === "done" && testPassed(state.test.inbound, state.test.outbound)
	);

	// ---- Network step --------------------------------------------------------------------
	async function loadStatus() {
		if (!privateBench.value) {
			state.statusLoaded = true;
			return;
		}
		try {
			const r = (await api.networkStatus()) || {};
			if (r.ok === false) throw new Error("refused");
			state.adminReachable =
				r.admin_reachable === true ? true : r.admin_reachable === false ? false : null;
			state.testMode = !!r.test_mode;
			if (r.network_mode === "private") {
				state.mode = "private";
				state.publicAddress = r.public_address || "";
				if (!state.draft) state.draft = state.publicAddress;
			}
		} catch (e) {
			state.adminReachable = null;
		} finally {
			state.statusLoaded = true;
		}
	}

	function pickOption(which) {
		state.pick = which === "public" ? "public" : "private";
	}

	async function loadTargets() {
		if (!privateBench.value) return;
		state.targetsLoading = true;
		state.targetsErr = "";
		try {
			const r = (await api.privateSetupTargets()) || null;
			const refusal = refusalOf(r);
			if (refusal) state.targetsErr = refusal.message;
			else state.targets = r;
		} catch (e) {
			state.targetsErr = "We couldn't look up the Jarvis setup server address. Try again.";
		} finally {
			state.targetsLoading = false;
		}
	}

	async function continueFromChoice() {
		if (!privateBench.value || state.pick !== "private") return;
		state.screen = "allow_admin";
		state.adminCheck = "idle";
		await loadTargets();
	}

	async function runAdminCheck() {
		if (!privateBench.value || state.adminCheck === "checking") return;
		state.adminCheck = "checking";
		state.adminCheckMessage = "";
		try {
			const r = adminCheckState(await api.checkAdminConnection());
			state.adminCheckMessage = r.message;
			if (r.state === "unreachable") state.adminCheck = "fail";
			else if (r.state === "unavailable") state.adminCheck = "disabled";
			else {
				state.detectedIp = r.callerIp;
				state.adminCheck = "ok";
			}
		} catch (e) {
			state.adminCheck = "fail";
		}
	}

	function openAddress() {
		state.screen = "address";
		if (!state.draft) {
			state.draft = state.publicAddress || (state.detectedIp ? `https://${state.detectedIp}` : "");
		}
	}

	function useDetected() {
		if (state.detectedIp) state.draft = `https://${state.detectedIp}`;
	}

	async function saveAddress() {
		state.draftTouched = true;
		if (!privateBench.value || draftError.value || state.saving) return false;
		state.saving = true;
		state.saveErr = "";
		try {
			const r = (await api.savePublicAddress(state.draft.trim())) || {};
			const refusal = refusalOf(r);
			if (refusal || !r.ok) {
				state.saveErr = refusal ? refusal.message : "We couldn't save that address.";
				return false;
			}
			state.mode = "private";
			state.publicAddress = r.public_address;
			state.draft = r.public_address;
			return true;
		} catch (e) {
			state.saveErr = "We couldn't save that address. Try again.";
			return false;
		} finally {
			state.saving = false;
		}
	}

	// ---- held server -----------------------------------------------------------------------
	function resetTest() {
		run++;
		state.test = freshTest();
	}

	function dispose() {
		run++;
	}

	// Refreshes the hold. Entering AllowServer and the Connect panel call this even when a hold
	// is already shown (the stored one may be stale after a host move). A concurrent call shares
	// the one in flight.
	function loadHold() {
		if (!privateBench.value) return Promise.resolve(false);
		if (!holdInFlight) holdInFlight = fetchHold().finally(() => (holdInFlight = null));
		return holdInFlight;
	}

	async function fetchHold() {
		state.holdLoading = true;
		state.holdErr = "";
		state.holdErrCode = "";
		try {
			const r = (await api.reserveHost()) || {};
			const refusal = refusalOf(r);
			if (refusal || r.ok !== true) {
				state.holdErr = refusal ? refusal.message : "We couldn't set aside a server just now.";
				state.holdErrCode = refusal ? refusal.code : "";
				return false;
			}
			// `cached: true` is a normal hold.
			const next = { hostIp: r.host_ip || "", reachUrl: r.reach_url || "", holdUntil: r.hold_until || null };
			const prev = state.hold;
			if (prev && prev.hostIp && next.hostIp && prev.hostIp !== next.hostIp) {
				// The rules the customer set up named the old address: nothing about the
				// previous results holds any more.
				state.holdEnded = { oldIp: prev.hostIp, newIp: next.hostIp };
				resetTest();
			}
			state.hold = next;
			return true;
		} catch (e) {
			state.holdErr = "We couldn't set aside a server just now. Try again.";
			return false;
		} finally {
			state.holdLoading = false;
		}
	}

	// First paint: the readiness verdict's network_check or the poll's stored private_hold
	// ({host_ip, reach_url[, hold_until]}). Shown at once, then loadHold() refreshes it.
	function seedHold(stored) {
		if (state.hold || !stored || !stored.host_ip) return;
		state.hold = {
			hostIp: stored.host_ip,
			reachUrl: stored.reach_url || "",
			holdUntil: stored.hold_until || null,
		};
	}

	// ---- the two-way test ------------------------------------------------------------------
	async function runTest() {
		if (!privateBench.value || state.test.phase === "testing") return;
		state.holdEnded = null;
		resetTest(); // bumps the generation: this run owns the token from here
		const mine = run;
		state.test = { ...freshTest(), phase: "testing", inbound: { status: "Checking", reason: "" } };
		let started;
		try {
			started = (await api.startConnectionTest()) || {};
		} catch (e) {
			started = {};
		}
		if (mine !== run) return;
		const refusal = refusalOf(started);
		if (refusal || !started.ok) {
			resetTest();
			if (refusal && refusal.code === "PRIVATE_HOLD_UNAVAILABLE") {
				// The hold ended while the customer was away. Re-reserve; a different host raises
				// the hold-ended banner (loadHold), the same host just needs another click.
				if (!(await loadHold())) {
					state.test.err = state.holdErr;
				} else if (!state.holdEnded) {
					state.test.err = "We’ve set your server aside again. Test the connection once more.";
				}
				return;
			}
			state.test.err = refusal ? refusal.message : "We couldn't start the test. Try again.";
			state.test.retryAfter = refusal ? refusal.retryAfterSeconds : 0;
			return;
		}
		state.test.check = started.check;
		state.test.outbound = started.outbound || { ok: false, reason: "bad_target" };
		await pollResult(mine, started.check);
	}

	// Polls a stored result within one time budget. Every await is followed by the token check.
	async function pollResult(mine, check) {
		let spent = 0;
		let answered = false; // at least one poll got a reply (so the bench was reachable)
		while (spent < POLL_BUDGET_MS) {
			let r = null;
			try {
				r = (await api.connectionTestStatus(check)) || {};
			} catch (e) {
				r = null; // a blip: ask again
			}
			if (mine !== run) return;
			if (r) answered = true;
			const stop = refusalOf(r);
			if (stop && stop.code === "RATE_LIMITED_PRIVATE_NETWORK" && stop.retryAfterSeconds) {
				// Wait as long as asked, then carry on with the same check.
				const wait = Math.min(stop.retryAfterSeconds, RATE_LIMIT_WAIT_CAP_S) * 1000;
				spent += wait;
				await sleep(wait);
				if (mine !== run) return;
				continue;
			}
			if (stop) {
				resetTest();
				state.test.err = stop.message;
				state.test.retryAfter = stop.retryAfterSeconds;
				return;
			}
			if (r && r.status && r.status !== "Checking") {
				state.test.inbound = { status: r.status, reason: r.reason || "" };
				state.test.phase = "done";
				return;
			}
			spent += POLL_MS;
			await sleep(POLL_MS);
			if (mine !== run) return;
		}
		if (!answered) {
			// Every poll failed in transit: that says nothing about the customer's server.
			resetTest();
			state.test.err = CONNECTION_LOST;
			return;
		}
		state.test.inbound = { status: "Unreachable", reason: "timeout" };
		state.test.phase = "done";
	}

	function markCleared() {
		state.cleared = true;
		try {
			storage.set(CLEARED_KEY, "1");
		} catch (e) {
			/* storage unavailable: the hold simply comes back after a reload */
		}
	}

	return {
		state,
		privateBench,
		complete,
		onboardingHostOk,
		holdsCheckout,
		draftError,
		testsPassed,
		loadStatus,
		pickOption,
		continueFromChoice,
		loadTargets,
		runAdminCheck,
		openAddress,
		useDetected,
		saveAddress,
		loadHold,
		seedHold,
		runTest,
		resetTest,
		dispose,
		markCleared,
	};
}
