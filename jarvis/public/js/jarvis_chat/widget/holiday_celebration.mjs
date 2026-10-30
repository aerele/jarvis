// Whether the Desk mini chat may celebrate (play the particles) when its welcome opens.
// The rules are the desktop scene's (frontend/src/lib/useHolidayScene.js), kept in one
// place so the panel only asks and then draws:
//
//  * only on a day or birthday whose scene is on (`scene`), whose cadence allows it
//    (`may_play`), with motion allowed and this browser's own memory agreeing;
//  * the art loads first, then the server is asked (`record_shown`) for the slot, both
//    inside ONE deadline from the ask. Past it there is no call and no play;
//  * `{play: true}` plays. `{play: false}` plays nothing and latches this answer object in
//    memory (never in storage: a decline is not a showing). A failed call (any non-2xx or a
//    thrown fetch) plays and records the showing in this browser instead;
//  * a reply that lands after the caller gave up still sets the latch or the local record,
//    so the next welcome does not ask again for nothing.
// Text only: no art import, so it stays in the always-loaded widget bundle.
import { mayPlayLocal, recordLocal } from "../../shared/holiday/cadence.mjs";

export const DEADLINE_MS = 2500;
const DEADLINE = Symbol("deadline");

export function createCelebrator({
  answer, // () => today's holiday answer (the same object until a refetch replaces it)
  ensureArt, // () => Promise of the art, or null when it failed
  recordShown, // () => Promise of {play}; rejects on a failed call
  reduced = () => false,
  storage = null,
  now = () => new Date(),
  deadlineMs = DEADLINE_MS,
}) {
  let latched = null; // the answer object a reply was last seen for
  let inFlight = null;

  // Why not, or null when the rules allow a try.
  function refusal(a) {
    if (!a || (a.kind !== "day" && a.kind !== "birthday")) return "kind";
    if (a.scene !== true) return "scene";
    if (a.may_play === false) return "may_play";
    if (reduced()) return "reduced";
    if (latched === a) return "latched";
    if (storage && !mayPlayLocal(storage, now(), a.tz)) return "local";
    return null;
  }

  async function run() {
    const a = answer();
    const why = refusal(a);
    if (why) return { play: false, reason: why };
    let timer = null;
    const expired = new Promise((resolve) => {
      timer = setTimeout(resolve, deadlineMs, DEADLINE);
    });
    try {
      const art = await Promise.race([ensureArt(), expired]);
      if (art === DEADLINE) return { play: false, reason: "deadline" };
      if (!art) return { play: false, reason: "load-failed" };
      const outcome = await Promise.race([ask(a), expired]);
      return outcome === DEADLINE
        ? { play: false, reason: "deadline" }
        : outcome;
    } finally {
      clearTimeout(timer);
    }
  }

  // The reply's effects happen here, whoever is still waiting for it.
  function ask(a) {
    return Promise.resolve()
      .then(recordShown)
      .then(
        (reply) => ({ reply }),
        (error) => ({ error })
      )
      .then((result) => {
        latched = a;
        if (result.error) return failedCall(a);
        const play = !!result.reply && result.reply.play === true;
        return { play, reason: play ? "played" : "declined" };
      });
  }

  function failedCall(a) {
    if (storage) recordLocal(storage, now(), a.tz);
    return { play: true, reason: "network" };
  }

  return {
    /** Resolves {play, reason}. Two asks at once share one call. */
    celebrate() {
      inFlight = inFlight || run().finally(() => (inFlight = null));
      return inFlight;
    },
  };
}
