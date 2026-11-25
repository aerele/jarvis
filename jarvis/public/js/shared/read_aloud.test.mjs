import assert from "node:assert/strict";
import test from "node:test";

import {
  CHUNK_MAX,
  DEFAULT_LANG,
  chunkText,
  createReader,
  langOf,
  pickVoice,
  speakableText,
} from "./read_aloud.mjs";

test("speakableText reads tables, lists and headings as sentences", () => {
  const screen =
    "Overdue invoices\nCustomer\tAmount\nAcme\t₹1,200\n• Call Acme today ✅\n\n**Total** due";
  assert.equal(
    speakableText(screen),
    "Overdue invoices. Customer, Amount. Acme, ₹1,200. Call Acme today. Total due."
  );
});

test("speakableText keeps punctuation and drops empty or symbol-only lines", () => {
  assert.equal(
    speakableText("Done!\n---\n|  |\nNext: pay"),
    "Done! Next: pay."
  );
  assert.equal(speakableText(""), "");
});

test("langOf follows the script, else Indian English", () => {
  assert.equal(langOf("Total due ₹1,200"), DEFAULT_LANG);
  assert.equal(langOf("कुल बकाया ₹1,200"), "hi-IN");
  assert.equal(langOf("மொத்தம் 1,200 rupees"), "ta-IN");
});

test("chunkText keeps every piece under the limit and loses nothing", () => {
  const text = "One short sentence. " + "word, ".repeat(80) + "end. Last one!";
  const parts = chunkText(text);
  assert.ok(parts.length > 2);
  for (const p of parts) assert.ok(p.length <= CHUNK_MAX, p.length);
  assert.equal(
    parts.join(" ").replace(/\s+/g, " "),
    text.trim().replace(/\s+/g, " ")
  );
});

test("pickVoice prefers the exact language, a nicer voice, then Indian English", () => {
  const voices = [
    { name: "Samantha", lang: "en-US" },
    { name: "Rishi", lang: "en-IN" },
    { name: "Lekha", lang: "hi-IN" },
    { name: "Google हिन्दी", lang: "hi-IN" },
    { name: "Daniel", lang: "en_GB" },
  ];
  assert.equal(pickVoice(voices, "hi-IN").name, "Google हिन्दी");
  assert.equal(pickVoice(voices, "en-IN").name, "Rishi");
  assert.equal(
    pickVoice(
      voices.filter((v) => v.lang !== "en-IN"),
      "en-IN"
    ).name,
    "Daniel"
  );
  assert.equal(pickVoice(voices, "ta-IN"), null);
});

function fakeEnv(voices = [{ name: "Rishi", lang: "en-IN" }]) {
  const spoken = [];
  const synth = {
    spoken,
    cancelled: 0,
    getVoices: () => voices,
    speak: (u) => spoken.push(u),
    cancel() {
      this.cancelled++;
      const queued = spoken.splice(0);
      for (const u of queued) u.onerror?.({ error: "interrupted" });
    },
  };
  function SpeechSynthesisUtterance(text) {
    this.text = text;
  }
  return { speechSynthesis: synth, SpeechSynthesisUtterance };
}

test("a reader speaks in chunks with the chosen voice and reports when it ends", () => {
  const env = fakeEnv();
  const r = createReader(env);
  const heard = [];
  r.subscribe((id) => heard.push(id));
  assert.equal(r.supported, true);
  assert.equal(r.speak("m1", "Hello there.\nSecond line"), true);
  const [u] = env.speechSynthesis.spoken;
  assert.equal(u.text, "Hello there. Second line.");
  assert.equal(u.lang, "en-IN");
  assert.equal(u.voice.name, "Rishi");
  assert.equal(r.speakingId, "m1");
  u.onend();
  assert.equal(r.speakingId, null);
  assert.deepEqual(heard, ["m1", null]);
});

test("toggle stops the same reply; another reply replaces it", () => {
  const env = fakeEnv();
  const r = createReader(env);
  r.toggle("m1", "First.");
  r.toggle("m2", "Second.");
  assert.equal(r.speakingId, "m2");
  assert.equal(env.speechSynthesis.spoken.length, 1);
  assert.equal(r.toggle("m2", "Second."), false);
  assert.equal(r.speakingId, null);
});

test("the interrupted chunks of a stopped reading do not end the next one", () => {
  const env = fakeEnv();
  const r = createReader(env);
  r.speak("m1", "a. ".repeat(200));
  r.speak("m2", "b.");
  assert.equal(r.speakingId, "m2");
  env.speechSynthesis.spoken[0].onerror({ error: "synthesis-failed" });
  assert.equal(r.speakingId, null);
});

test("no speechSynthesis means not supported and nothing spoken", () => {
  const r = createReader({});
  assert.equal(r.supported, false);
  assert.equal(r.speak("m1", "Hello"), false);
  assert.equal(r.speakingId, null);
});
