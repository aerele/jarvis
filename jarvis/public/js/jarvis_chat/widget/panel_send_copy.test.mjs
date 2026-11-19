import { test } from "node:test";
import assert from "node:assert/strict";
import { sendRefusalMessage } from "./panel_send_copy.mjs";

test("sendRefusalMessage: release_update_required -> branded update line", () => {
  assert.equal(
    sendRefusalMessage("release_update_required", "Jarvis"),
    "A new Jarvis version is required. Please ask your administrator to update."
  );
});

test("sendRefusalMessage: the update line uses the passed (white-label) brand name", () => {
  // A customer who renamed the assistant must not see "Jarvis" leak.
  assert.equal(
    sendRefusalMessage("release_update_required", "Aida"),
    "A new Aida version is required. Please ask your administrator to update."
  );
});

test("sendRefusalMessage: a blank/missing brand falls back to 'Jarvis'", () => {
  const expected =
    "A new Jarvis version is required. Please ask your administrator to update.";
  assert.equal(sendRefusalMessage("release_update_required", ""), expected);
  assert.equal(sendRefusalMessage("release_update_required", "   "), expected);
  assert.equal(sendRefusalMessage("release_update_required"), expected);
});

test("sendRefusalMessage: maintenance -> branded back-shortly line", () => {
  assert.equal(
    sendRefusalMessage("maintenance", "Aida"),
    "Aida is upgrading and will be back shortly."
  );
  // white-label brand honoured; a blank brand falls back to 'Jarvis'
  assert.equal(
    sendRefusalMessage("maintenance", ""),
    "Jarvis is upgrading and will be back shortly."
  );
});

test("sendRefusalMessage: any other refusal -> the generic try-again line", () => {
  assert.equal(
    sendRefusalMessage("something_else", "Aida"),
    "That couldn't be sent. Please try again."
  );
  // Brand is irrelevant to the generic line - it names no product.
  assert.equal(
    sendRefusalMessage("something_else"),
    "That couldn't be sent. Please try again."
  );
});

test("sendRefusalMessage: an unknown/empty reason -> the generic line, never branded", () => {
  const generic = "That couldn't be sent. Please try again.";
  assert.equal(sendRefusalMessage("", "Aida"), generic);
  assert.equal(sendRefusalMessage(undefined, "Aida"), generic);
  assert.equal(sendRefusalMessage(null, "Aida"), generic);
});

import { isKnownRefusal } from "./panel_send_copy.mjs";

test("sendRefusalMessage: the desktop app's refusal codes read the same here", () => {
  assert.equal(
    sendRefusalMessage("usage_limit", "Aida", { limit_period: "Weekly" }),
    "You've reached this week's usage limit. It resets on Sunday."
  );
  assert.equal(
    sendRefusalMessage("usage_limit", "Aida"),
    "You've reached your usage limit. Ask your Aida admin to raise it."
  );
  assert.match(
    sendRefusalMessage("llm_not_configured"),
    /No AI model is connected/
  );
  assert.match(sendRefusalMessage("subscription_suspended"), /Renew it/);
  assert.match(
    sendRefusalMessage("workspace_resetting", "Aida"),
    /Aida is being reset/
  );
  assert.equal(
    sendRefusalMessage("nonsense"),
    "That couldn't be sent. Please try again."
  );
});

test("isKnownRefusal: our copy outranks a server sentence only for known codes", () => {
  assert.equal(isKnownRefusal("usage_limit"), true);
  assert.equal(isKnownRefusal("maintenance"), true);
  assert.equal(isKnownRefusal("release_update_required"), true);
  assert.equal(isKnownRefusal("nonsense"), false);
});
