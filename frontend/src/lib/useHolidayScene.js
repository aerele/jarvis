// The play logic of the holiday welcome scene: when it may play, the one overall deadline,
// what ends it, and what the page shows meanwhile. No DOM of its own: the page gives it a
// host for the art, the boxes of the mark, heading and card band, and the beats it should
// act on (scene contract: jarvis/public/js/shared/holiday/scenes/scene-runtime.mjs).
//
// Phases: pending (the scene may still play: cards hidden, normal greeting) -> playing ->
// rest. Or pending -> rest at once when the answer is no, the deadline passes or the art
// will not load. A scene never restarts and never hides the cards again: the replay on the
// mark runs as "replaying", with the cards showing. A replay clicked while the last scene is
// still fading out or the hero is still gliding waits for both (one click is kept).
//
// A tab opened hidden does not begin until it is shown, so a day's showing is not spent on
// a scene nobody sees. `reason` says why the scene is or is not playing, for support.
//
// The hero (mark and greeting) is centred in the stage while the scene is pending or playing
// (`centred`) and glides up to its rest place over `glideMs` when the scene ends.
import { computed, reactive, ref } from "vue";
import { mayPlayLocal, recordLocal } from "@jsshared/holiday/cadence.mjs";
import { recordHolidayShown } from "@/api";
import { report as reportError } from "@/lib/errorReporter";
import { _resetReportedForTests, reportSceneFailure } from "@/lib/holidayReport";
import { hasScene, loadScene } from "@/lib/holidayScenes";

export const DEADLINE_MS = 2500; // import() then record_shown, from mount
const SMILE_MS = 1500;
const FADE_MS = { end: 600, early: 400, instant: 0 }; // how the cards come back
const GLIDE_MS = { end: 600, early: 250, instant: 0 }; // how the hero rises to its rest place

// The answer a reply (or a give-up) was already seen for. A second new chat in the same
// tab reads the same answer, so it must not hide the cards again; the next get_today
// brings a new answer object and with it a new chance.
let spentFor = null;

/** Test-only: forget what was spent, so each spec starts from a fresh page. */
export function _resetSpentForTests() {
	spentFor = null;
	_resetReportedForTests();
}

function safeStorage() {
	try {
		return window.localStorage;
	} catch {
		return null;
	}
}

export function createHolidayScene({
	answer, // () => today's holiday answer
	host, // () => the element the art is drawn into, or null
	getBoxes, // () => {host, mark, heading, cards}
	composerEmpty = () => true,
	reduced = () => false,
	idPrefix = "jvs",
	recordShown = recordHolidayShown,
	load = loadScene,
	now = () => new Date(),
	storage = safeStorage(),
	report = reportError,
	deadlineMs = DEADLINE_MS,
	fontsReady = () => Promise.resolve(), // () => a promise: the page's fonts are loaded
	doc = typeof document === "undefined" ? null : document, // for the tab's visibility
}) {
	const phase = ref("idle");
	// What the mark shows: the face, the item, the smile, and where the eyes look.
	const mark = reactive({ face: false, item: false, happy: false, look: null });
	// The greeting: swapped to the day heading, and whether that swap is instant.
	const text = reactive({ swapped: false, instant: false });
	const fadeMs = ref(0);
	const glideMs = ref(0);
	// Why the scene is or is not playing: loaded | load-failed | deadline | declined | threw |
	// hidden (null until something is known).
	const reason = ref(null);
	const centred = computed(() => phase.value === "pending" || phase.value === "playing");
	const cardsHidden = centred;

	let scene = null;
	// Every scene started, kept after it is done: one that settled early still has the smile's
	// closing timer, and only a last instant settle on it clears that.
	const started = [];
	let deadline = null;
	let smileTimer = null;
	let run = 0; // bumped whenever a pending decision is abandoned
	let swallowClick = false;
	let rising = false; // the hero is still rising to its rest place
	let glideTimer = null; // ends the rise (and runs a replay clicked during it)
	let queuedReplay = false; // a replay was clicked before the last scene was over
	let waitingToShow = false; // begin() is waiting for the tab to be shown

	function mayPlay() {
		const a = answer();
		if (!a || (a.kind !== "day" && a.kind !== "birthday")) return false;
		if (a.scene !== true || a.may_play === false) return false;
		if (!hasScene(a.design) || spentFor === a || reduced() || !composerEmpty()) return false;
		return storage ? mayPlayLocal(storage, now(), a.tz) : true;
	}

	function rest() {
		Object.assign(mark, { face: true, item: true, happy: false, look: null });
		text.swapped = true;
		text.instant = true;
	}

	// Decided before the first paint, so the cards are never shown and then hidden.
	if (mayPlay()) {
		phase.value = "pending";
	} else {
		rest();
		reason.value = "declined";
		phase.value = "rest";
	}

	function onBeat(name, ...args) {
		if (name === "peek") Object.assign(mark, { face: true, happy: false, look: null });
		else if (name === "look") mark.look = args[0] || args[1] ? [args[0], args[1]] : null;
		else if (name === "deliver") mark.item = true;
		else if (name === "happy") Object.assign(mark, { happy: true, look: null });
		else if (name === "swap") {
			text.swapped = true;
			text.instant = !!(args[0] && args[0].instant);
		} else if (name === "settle") onSettle(args[0]);
	}

	function onSettle(mode) {
		if (phase.value !== "playing" && phase.value !== "replaying") return;
		if (phase.value === "playing") {
			fadeMs.value = FADE_MS[mode] ?? 0;
			glide(mode);
		}
		phase.value = "rest";
	}

	/**
	 * The hero rises to its rest place over the mode's time; a replay waits for it. The end
	 * is a timer, not a comparison of clock readings: the page's clock may be pinned or step
	 * back, and a click waiting for a rise that never ends would be lost.
	 */
	function glide(mode) {
		glideMs.value = GLIDE_MS[mode] ?? 0;
		clearTimeout(glideTimer);
		glideTimer = null;
		rising = glideMs.value > 0;
		if (!rising) return;
		glideTimer = setTimeout(() => {
			glideTimer = null;
			rising = false;
			drain();
		}, glideMs.value);
	}

	// ---- pending: the import, then record_shown, both inside one deadline ------------
	// The server slot is asked for only once there is art to play, so a slow or failed
	// import (or a settle while it is still loading) never uses a slot without a play.
	function begin() {
		if (phase.value !== "pending" || deadline !== null || waitingToShow) return;
		if (doc && doc.visibilityState !== "visible") {
			// Opened in a background tab: nothing to show yet, and no deadline running.
			reason.value = "hidden";
			waitingToShow = true;
			doc.addEventListener("visibilitychange", onShown);
			return;
		}
		const a = answer();
		const mine = ++run;
		deadline = setTimeout(() => giveUp(mine, "early", "deadline"), deadlineMs);
		// The scene waits for the fonts too: a late font swap resizes the heading and the
		// art would be drawn for the wrong layout. Inside the same deadline, so fonts that
		// never arrive mean no scene, like a slow import. A fonts promise that rejects (or a
		// fontsReady that throws) must not stop the scene: the layout is then what it is.
		const fonts = Promise.resolve()
			.then(fontsReady)
			.catch(() => null);
		Promise.all([load(a.design), fonts]).then(([mod]) => {
			if (mine !== run || phase.value !== "pending") return;
			if (!mod) return giveUp(mine, "early", "load-failed");
			Promise.resolve()
				.then(recordShown)
				.then(
					(r) => ({ reply: r }),
					(error) => ({ error })
				)
				.then((result) => {
					spentFor = a; // any reply, or a failure, uses this answer up
					decide(mine, a, mod, result);
				});
		});
	}

	function onShown() {
		if (!doc || doc.visibilityState !== "visible") return;
		stopWaiting();
		begin();
	}

	function stopWaiting() {
		if (!waitingToShow) return;
		waitingToShow = false;
		doc.removeEventListener("visibilitychange", onShown);
	}

	function decide(mine, a, mod, result) {
		if (mine !== run || phase.value !== "pending") return;
		let go = !!result.reply && result.reply.play === true;
		if (result.error) {
			// A fast network error: play, and remember it here instead.
			go = storage ? mayPlayLocal(storage, now(), a.tz) : true;
			if (go && storage) recordLocal(storage, now(), a.tz);
		}
		if (go) startScene(mod, a, false);
		else giveUp(mine, "early", "declined");
	}

	// `why` is the reason for support (see reason); the person's own settle counts as declined.
	function giveUp(mine, mode = "early", why = "declined") {
		if (mine !== run || phase.value !== "pending") return;
		run++;
		clearTimeout(deadline);
		deadline = null;
		stopWaiting();
		spentFor = answer();
		fadeMs.value = 0;
		glide(mode); // the centred hero still has to come up
		reason.value = why;
		rest();
		phase.value = "rest";
	}

	// ---- playing ---------------------------------------------------------------------
	function startScene(mod, a, replay) {
		clearTimeout(deadline);
		deadline = null;
		const el = host();
		if (!el) return giveUp(run, "early", "declined");
		// The scenes before this one are over (a replay waits for that); this closes their
		// last timer (the smile's) and drops them.
		started.splice(0).forEach((old) => old.settle("instant"));
		phase.value = replay ? "replaying" : "playing";
		reason.value = "loaded";
		try {
			const s = mod.play(el, {
				layout: "desktop",
				getBoxes,
				year: a.year,
				idPrefix,
				reduced: false,
				onBeat,
				onError: (error) => failed(a.design, error),
				replay,
			});
			scene = s;
			started.push(s);
			s.done.then(() => {
				if (scene === s) finish();
			});
		} catch (error) {
			failed(a.design, error);
			scene = null;
			rest();
			phase.value = "rest";
		}
	}

	function finish() {
		scene = null;
		if (phase.value === "playing" || phase.value === "replaying") {
			rest();
			phase.value = "rest";
		}
		drain();
	}

	function failed(design, error) {
		reason.value = "threw";
		reportSceneFailure("play", design, error, report);
	}

	// ---- ending it -------------------------------------------------------------------
	/** End the scene: "early" (input, resize, ...) or "instant". Does nothing at rest. */
	function settle(mode = "early") {
		if (phase.value === "pending") return giveUp(run, mode);
		if (phase.value !== "playing" && phase.value !== "replaying") return;
		const was = phase.value;
		if (scene) scene.settle(mode);
		if (phase.value === was) {
			// A scene that did not announce its ending still ends here.
			if (was === "playing") {
				fadeMs.value = FADE_MS[mode] ?? 0;
				glide(mode);
			}
			phase.value = "rest";
		}
	}

	/** The page layout changed under a playing scene: let it read the boxes again. */
	function relayout(boxes) {
		if (phase.value !== "playing" && phase.value !== "replaying") return;
		if (scene && typeof scene.relayout === "function") scene.relayout(boxes);
	}

	/** The first pointerdown anywhere: ends the scene, and the click it starts must not replay. */
	function onPointerDown() {
		swallowClick = phase.value !== "rest" && phase.value !== "idle";
		queuedReplay = false; // a new gesture; an older waiting click is forgotten
		settle("early");
	}

	/** Clicking the mark at rest replays it with the cards showing; mid-scene it only settled. */
	async function replay() {
		if (swallowClick) {
			swallowClick = false;
			return;
		}
		if (phase.value !== "rest") return;
		if (reduced()) return smile();
		// The last scene is still fading out, or the hero still rising: start after both.
		if (scene || rising) {
			queuedReplay = true;
			return;
		}
		await playReplay();
	}

	/** Runs the replay that was clicked too early, once nothing of the last scene is left. */
	function drain() {
		if (!queuedReplay || scene || rising) return;
		queuedReplay = false;
		void playReplay();
	}

	async function playReplay() {
		const a = answer();
		const mod = await load(a.design);
		if (!mod || phase.value !== "rest" || stopped || scene || rising) return;
		mark.item = false;
		startScene(mod, a, true);
	}

	function smile() {
		clearTimeout(smileTimer);
		mark.happy = true;
		smileTimer = setTimeout(() => {
			smileTimer = null;
			mark.happy = false;
		}, SMILE_MS);
	}

	let stopped = false;
	/** The page is leaving: end everything now and leave no timer behind. */
	function stop() {
		stopped = true;
		run++;
		clearTimeout(deadline);
		clearTimeout(smileTimer);
		clearTimeout(glideTimer);
		deadline = null;
		smileTimer = null;
		glideTimer = null;
		rising = false;
		queuedReplay = false;
		stopWaiting();
		started.forEach((s) => s.settle("instant"));
		scene = null;
		if (phase.value !== "rest") phase.value = "rest";
	}

	return {
		phase,
		mark,
		text,
		fadeMs,
		glideMs,
		reason,
		centred,
		cardsHidden,
		begin,
		settle,
		relayout,
		onPointerDown,
		replay,
		stop,
	};
}
