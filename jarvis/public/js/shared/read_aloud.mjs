// Read a reply aloud with the browser's own voice (aerele/jarvis#2191), shared by the
// desktop chat and the PWA.
//
// Uses the Web Speech API (speechSynthesis): free, nothing leaves the device, no key.
// The voices are the device's own, so quality and languages vary by device; a device
// without speechSynthesis gets no Read aloud button (`supported`).
//
// The text is what the reply shows on screen (its innerText, or a selection), cleaned
// for speech: table cells become ", ", emoji and Markdown leftovers go, every line ends
// in a pause. It is spoken in short chunks because Chrome stops a long utterance after
// about 15 seconds. The language follows the script (Devanagari -> hi-IN, Tamil ->
// ta-IN, ...), else Indian English.

const SCRIPTS = [
  [/[\u0900-\u097F]/g, "hi-IN"],
  [/[\u0980-\u09FF]/g, "bn-IN"],
  [/[\u0A00-\u0A7F]/g, "pa-IN"],
  [/[\u0A80-\u0AFF]/g, "gu-IN"],
  [/[\u0B00-\u0B7F]/g, "or-IN"],
  [/[\u0B80-\u0BFF]/g, "ta-IN"],
  [/[\u0C00-\u0C7F]/g, "te-IN"],
  [/[\u0C80-\u0CFF]/g, "kn-IN"],
  [/[\u0D00-\u0D7F]/g, "ml-IN"],
];
export const DEFAULT_LANG = "en-IN";
export const CHUNK_MAX = 180;

// The language to speak `text` in: the Indian script it is mostly written in, else
// DEFAULT_LANG.
export function langOf(text) {
  const s = String(text || "");
  let best = DEFAULT_LANG;
  let most = 0;
  for (const [re, lang] of SCRIPTS) {
    const n = (s.match(re) || []).length;
    if (n > most) {
      most = n;
      best = lang;
    }
  }
  return best;
}

// On-screen text made fit to speak.
export function speakableText(text) {
  return String(text || "")
    .replace(/\r\n?/g, "\n")
    .replace(/\p{Extended_Pictographic}\uFE0F?/gu, "")
    .split("\n")
    .map((line) =>
      line
        .replace(/\t+/g, ", ")
        .replace(/[`*_#|>]+/g, " ")
        .replace(/^\s*(?:[-•◦▪●]|\d+[.)])\s+/, "")
        .replace(/\s+/g, " ")
        .replace(/^[\s,]+|[\s,]+$/g, "")
    )
    .filter((line) => /[\p{L}\p{N}]/u.test(line))
    .map((line) => (/[.!?।:;]$/.test(line) ? line : line + "."))
    .join(" ");
}

// `text` in pieces of at most `max` characters, split after a sentence where one
// fits, else after a comma or a space.
export function chunkText(text, max = CHUNK_MAX) {
  const sentences =
    String(text || "").match(/[^.!?।]+(?:[.!?।]+["')\]]*|$)\s*/g) || [];
  const out = [];
  let cur = "";
  const push = (piece) => {
    if (cur && (cur + piece).length > max) {
      out.push(cur.trim());
      cur = "";
    }
    cur += piece;
  };
  for (const s of sentences) {
    if (s.length <= max) {
      push(s);
      continue;
    }
    for (const part of s.match(new RegExp(`.{1,${max}}(?:[,\\s]|$)`, "g")) || [
      s,
    ])
      push(part);
  }
  if (cur.trim()) out.push(cur.trim());
  return out.filter(Boolean);
}

const NICE = /google|natural|neural|premium|enhanced|siri/i;
const norm = (l) =>
  String(l || "")
    .replace("_", "-")
    .toLowerCase();

// The device voice to read `lang` with: that exact language (a better-sounding one
// first), else the same language from another region (Indian English first for
// English), else null (the browser picks from `lang`).
export function pickVoice(voices, lang) {
  const want = norm(lang);
  const base = want.split("-")[0];
  const rank = (v) => (NICE.test(v.name || "") ? 0 : 1);
  const exact = voices
    .filter((v) => norm(v.lang) === want)
    .sort((a, b) => rank(a) - rank(b));
  if (exact.length) return exact[0];
  const order = ["en-in", "en-gb", "en-us"];
  const same = voices
    .filter((v) => norm(v.lang).split("-")[0] === base)
    .sort((a, b) => {
      const ia = order.indexOf(norm(a.lang));
      const ib = order.indexOf(norm(b.lang));
      return (ia < 0 ? 9 : ia) - (ib < 0 ? 9 : ib) || rank(a) - rank(b);
    });
  return same[0] || null;
}

// A reader over `env` (window): {supported, speak(id, text), stop(), toggle(id, text),
// speakingId, subscribe(fn)}. One thing is read at a time; `subscribe` hears the id
// being read, or null when reading ends or stops.
export function createReader(env = globalThis) {
  const synth = env && env.speechSynthesis;
  const Utterance = env && env.SpeechSynthesisUtterance;
  const supported = !!(synth && Utterance);
  const listeners = new Set();
  let current = null;
  let run = 0;

  const set = (id) => {
    if (current === id) return;
    current = id;
    for (const fn of listeners) fn(id);
  };

  function stop() {
    run++;
    if (supported) synth.cancel();
    set(null);
  }

  function speak(id, text) {
    stop();
    const clean = speakableText(text);
    if (!supported || !clean) return false;
    const lang = langOf(clean);
    const voice = pickVoice((synth.getVoices && synth.getVoices()) || [], lang);
    const parts = chunkText(clean);
    const mine = run;
    set(id);
    parts.forEach((part, i) => {
      const u = new Utterance(part);
      u.lang = (voice && voice.lang) || lang;
      if (voice) u.voice = voice;
      if (i === parts.length - 1) {
        u.onend = () => {
          if (mine === run) set(null);
        };
      }
      // cancel() fires "interrupted"/"canceled" on every queued chunk: stop() has
      // already moved `run` on, so only a real failure of THIS reading ends it here.
      u.onerror = () => {
        if (mine !== run) return;
        run++;
        synth.cancel();
        set(null);
      };
      synth.speak(u);
    });
    return true;
  }

  return {
    supported,
    speak,
    stop,
    toggle(id, text) {
      if (current === id) {
        stop();
        return false;
      }
      return speak(id, text);
    },
    get speakingId() {
      return current;
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
